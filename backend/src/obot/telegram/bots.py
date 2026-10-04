"""Handlers shared by every managed bot."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.usage import QuotaReason, check_llm_quota, record_llm_usage
from obot.bots.byok import reply_model, uses_byok
from obot.bots.service import mark_owner_started
from obot.bots.users import has_started, join_bot, leave_bot
from obot.chat.history import ChatScope, recent_messages, record_message
from obot.chat.membership import upsert_membership
from obot.core.agent import BotProfile, Turn, generate_reply
from obot.core.byok import failure_of
from obot.db.models import Bot as BotRecord
from obot.db.models import Message as StoredMessage
from obot.db.models import SenderKind
from obot.i18n import lang_of, t
from obot.memory.jobs import recall_facts
from obot.memory.store import MEMORY_TOOL, forget_chat
from obot.notices import first_today
from obot.tasks.service import resume_waiting
from obot.telegram.addressing import is_addressed, strip_mention
from obot.telegram.byok_notice import notify_key_failure
from obot.telegram.moderation import report_from_bot
from obot.telegram.runtime import Runtime
from obot.telegram.toolkit import toolkit

log = logging.getLogger(__name__)

router = Router(name="bots")

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
MEMBER_STATUSES = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.RESTRICTED,
}


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def on_start(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    await join_bot(session, bot_record, message.from_user.id, message.from_user.language_code)
    first_time = await mark_owner_started(session, bot_record, message.from_user.id)
    if first_time:
        await resume_waiting(session, bot_record, datetime.now(UTC))
    text = (
        "All set — I can message you now." if first_time else f"Hi! I'm {bot_record.display_name}."
    )
    await runtime.sender.send_text(message.bot, message.chat.id, text, group=False)


@router.message(Command("report"))
async def on_report(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot_record: BotRecord,
    runtime: Runtime,
) -> None:
    await report_from_bot(message, session, runtime, bot_record, command.args)


@router.message(Command("forget"))
async def on_forget(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    lang = lang_of(message.from_user.language_code)
    group = message.chat.type in GROUP_TYPES
    if group:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            await runtime.sender.send_text(
                message.bot,
                message.chat.id,
                t(lang, "forget.admins_only"),
                group=True,
                reply_to=message.message_id,
            )
            return
    await forget_chat(session, ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id))
    await session.commit()
    await runtime.sender.send_text(
        message.bot, message.chat.id, t(lang, "forget.done"), group=group
    )


@router.my_chat_member()
async def on_membership(
    event: ChatMemberUpdated, session: AsyncSession, bot_record: BotRecord
) -> None:
    status = event.new_chat_member.status
    if event.chat.type == ChatType.PRIVATE:
        # Blocked: the person stops using the bot until they start it again.
        if status == ChatMemberStatus.KICKED:
            await leave_bot(session, bot_record, event.from_user.id)
            if event.from_user.id == bot_record.owner.tg_user_id:
                bot_record.owner_started = False
        return
    await upsert_membership(
        session,
        bot_record.id,
        event.chat.id,
        chat_type=event.chat.type,
        title=event.chat.title,
        is_member=status in MEMBER_STATUSES,
        is_admin=status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR),
    )
    log.info("bot %s is now %s in chat %s", bot_record.id, status, event.chat.id)


@router.message(F.text, F.chat.type == ChatType.PRIVATE)
async def on_private_text(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    # Any private message starts using the bot, not only /start.
    await join_bot(session, bot_record, message.from_user.id, message.from_user.language_code)
    if await mark_owner_started(session, bot_record, message.from_user.id):
        await resume_waiting(session, bot_record, datetime.now(UTC))
    scope = ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id)
    incoming = await _record_incoming(session, scope, message)
    await _respond(Inbound.of(message), message.text, incoming, scope, session, bot_record, runtime)


@router.message(F.text, F.chat.type.in_(GROUP_TYPES))
async def on_group_text(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    # Other bots are ignored: bots only work together behind the scenes (ask_bots).
    if message.from_user.is_bot:
        return
    scope = ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id)
    incoming = await _record_incoming(session, scope, message)
    if not is_addressed(message, message.bot.id, bot_record.username):
        return
    if not await has_started(session, bot_record, message.from_user.id):
        await ask_to_start(message, bot_record, runtime)
        return
    text = strip_mention(message.text, bot_record.username) or message.text
    await _respond(Inbound.of(message), text, incoming, scope, session, bot_record, runtime)


async def ask_to_start(message: Message, bot_record: BotRecord, runtime: Runtime) -> None:
    """Someone who never started the bot addressed it in a group: point them to Start.

    At most once a day per person and group, so the group is not flooded.
    """
    key = f"start-first:{bot_record.id}:{message.chat.id}:{message.from_user.id}"
    if not await first_today(runtime.redis, key, datetime.now(UTC), "UTC"):
        return
    lang = lang_of(message.from_user.language_code)
    button = InlineKeyboardButton(
        text=t(lang, "btn.start_bot"), url=f"https://t.me/{bot_record.username}?start=g"
    )
    await runtime.sender.send_text(
        message.bot,
        message.chat.id,
        t(lang, "group.start_first"),
        group=True,
        reply_to=message.message_id,
        buttons=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )


async def _record_incoming(
    session: AsyncSession, scope: ChatScope, message: Message
) -> StoredMessage:
    stored = await record_message(
        session,
        scope,
        sender_kind=SenderKind.HUMAN,
        text=message.text,
        tg_message_id=message.message_id,
        sender_tg_id=message.from_user.id,
        sender_name=message.from_user.full_name,
    )
    # Persist the incoming message even if the model call fails later.
    await session.commit()
    return stored


def to_turn(stored: StoredMessage, group: bool) -> Turn:
    if stored.sender_kind == SenderKind.SELF:
        return Turn(role="assistant", text=stored.text)
    text = f"{stored.sender_name}: {stored.text}" if group else stored.text
    return Turn(role="user", text=text)


@dataclass(frozen=True)
class Inbound:
    """What a reply needs to know about the message it answers."""

    client: Bot  # the replying bot's Telegram client
    chat_id: int
    message_id: int
    sender_name: str
    sender_language: str | None
    group: bool

    @classmethod
    def of(cls, message: Message) -> "Inbound":
        return cls(
            client=message.bot,
            chat_id=message.chat.id,
            message_id=message.message_id,
            sender_name=message.from_user.full_name,
            sender_language=message.from_user.language_code,
            group=message.chat.type in GROUP_TYPES,
        )


async def _respond(
    inbound: Inbound,
    text: str,
    incoming: StoredMessage,
    scope: ChatScope,
    session: AsyncSession,
    bot_record: BotRecord,
    runtime: Runtime,
) -> None:
    owner = bot_record.owner
    group = inbound.group
    now = datetime.now(UTC)
    byok = uses_byok(bot_record)
    quota = await check_llm_quota(session, runtime.pricing, owner, bot_record, now, byok=byok)
    if not quota.allowed:
        await notify_quota_exhausted(inbound, bot_record, quota.reason, runtime, now)
        return

    history = [
        to_turn(m, group)
        for m in await recent_messages(session, scope, runtime.history_limit, before_id=incoming.id)
    ]
    user_text = f"{inbound.sender_name}: {text}" if group else text
    profile = BotProfile(name=bot_record.display_name, persona=bot_record.persona)
    memories: list[str] = []
    reply_to = inbound.message_id if group else None

    await inbound.client.send_chat_action(inbound.chat_id, ChatAction.TYPING)
    try:
        if MEMORY_TOOL in (bot_record.tools or []):
            memories = await recall_facts(
                session, runtime.pricing, bot_record, scope, text, runtime.embedder
            )
        chosen = reply_model(runtime.models, runtime.cipher, runtime.pricing, bot_record)
        kit = await toolkit(session, runtime, bot_record, now)
        reply = await generate_reply(
            chosen.model,
            profile,
            history,
            user_text,
            tools=kit.tools,
            services=kit.services,
            group=group,
            memories=memories,
            note=kit.note,
        )
    except Exception as e:
        log.exception("agent failed for bot %s chat %s", bot_record.id, inbound.chat_id)
        # Never fall back to our model: the owner chose to pay with their own key.
        failure = failure_of(e) if byok else None
        if failure is not None:
            await notify_key_failure(runtime, bot_record, failure, now)
        await runtime.sender.send_text(
            inbound.client,
            inbound.chat_id,
            t(lang_of(inbound.sender_language), "byok.chat_failed")
            if failure is not None
            else "Sorry, something went wrong. Please try again.",
            group=group,
            reply_to=reply_to,
        )
        return

    usage = await record_llm_usage(
        session,
        runtime.pricing,
        owner,
        bot_record,
        reply.model_name,
        reply.input_tokens,
        reply.output_tokens,
        byok=chosen.byok,
    )
    log.info(
        "bot %s replied in %s with %s (%d in / %d out tokens, %d credits)",
        bot_record.id,
        "group" if group else "private",
        reply.model_name,
        reply.input_tokens,
        reply.output_tokens,
        usage.credits,
    )
    sent = await runtime.sender.send_text(
        inbound.client, inbound.chat_id, reply.text, group=group, reply_to=reply_to
    )
    await record_message(
        session,
        scope,
        sender_kind=SenderKind.SELF,
        text=reply.text,
        tg_message_id=sent[-1].message_id if sent else None,
        sender_tg_id=inbound.client.id,
        sender_name=bot_record.display_name,
    )


async def notify_quota_exhausted(
    inbound: Inbound,
    bot_record: BotRecord,
    reason: QuotaReason,
    runtime: Runtime,
    now: datetime,
) -> None:
    """Tell the chat (once per day) and the owner (once per day per reason)."""
    owner = bot_record.owner
    chat_key = f"quota:{bot_record.id}:{inbound.chat_id}"
    if await first_today(runtime.redis, chat_key, now, owner.timezone):
        await runtime.sender.send_text(
            inbound.client,
            inbound.chat_id,
            t(lang_of(inbound.sender_language), f"quota.{reason}"),
            group=inbound.group,
            reply_to=inbound.message_id if inbound.group else None,
        )

    owner_key = f"quota-owner:{owner.id}:{reason}"
    if reason == QuotaReason.BOT_DAILY_CAP:
        owner_key += f":{bot_record.id}"
    if await first_today(runtime.redis, owner_key, now, owner.timezone):
        limit = runtime.pricing.plan(owner.plan).daily_llm_messages
        text = t(
            lang_of(owner.language),
            f"owner.quota.{reason}",
            limit=limit,
            username=bot_record.username,
        )
        try:
            await runtime.factory_bot.send_message(owner.tg_user_id, text)
        except Exception:
            log.warning("could not notify owner %s about quota", owner.id, exc_info=True)
