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
from obot.bots.access import can_use
from obot.bots.byok import reply_model
from obot.bots.service import mark_owner_started
from obot.bots.users import join_bot, leave_bot, started_user
from obot.chat.history import ChatScope, recent_messages, record_message
from obot.chat.membership import upsert_membership
from obot.core.agent import BotProfile, Turn, generate_reply
from obot.core.byok import failure_of
from obot.db.models import Bot as BotRecord
from obot.db.models import Message as StoredMessage
from obot.db.models import SenderKind, User
from obot.i18n import lang_of, t
from obot.memory.jobs import recall_facts
from obot.memory.store import MEMORY_TOOL, forget_chat
from obot.notices import first_today
from obot.telegram.addressing import is_addressed, strip_mention
from obot.telegram.api_keys import KeyRequester
from obot.telegram.moderation import report_from_bot
from obot.telegram.runtime import Runtime
from obot.telegram.scheduling import Scheduler
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
    if not await _allowed_in_private(message, bot_record, runtime):
        return
    await join_bot(session, bot_record, message.from_user.id, message.from_user.language_code)
    await mark_owner_started(session, bot_record, message.from_user.id)
    text = t(
        lang_of(message.from_user.language_code),
        "bot.hello",
        name=bot_record.display_name,
        description=(bot_record.persona or {}).get("description", ""),
    )
    await runtime.sender.send_text(message.bot, message.chat.id, text.strip(), group=False)


@router.message(Command("report"))
async def on_report(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot_record: BotRecord,
    runtime: Runtime,
) -> None:
    await report_from_bot(message, session, runtime, bot_record, command.args)


@router.message(Command("settings"), F.chat.type == ChatType.PRIVATE)
async def on_settings(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    """Own key, model and daily cap live in the panel, opened from the factory chat."""
    if not await _allowed_in_private(message, bot_record, runtime):
        return
    lang = lang_of(message.from_user.language_code)
    button = InlineKeyboardButton(
        text=t(lang, "btn.my_settings"),
        url=f"https://t.me/{runtime.factory_username}?start=mine_{bot_record.id}",
    )
    await runtime.sender.send_text(
        message.bot,
        message.chat.id,
        t(lang, "bot.settings"),
        group=False,
        buttons=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )


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
    if not await _allowed_in_private(message, bot_record, runtime):
        return
    # Any private message starts using the bot, not only /start.
    user, _ = await join_bot(
        session, bot_record, message.from_user.id, message.from_user.language_code
    )
    await mark_owner_started(session, bot_record, message.from_user.id)
    scope = ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id)
    incoming = await _record_incoming(session, scope, message)
    await _respond(
        Inbound.of(message), message.text, incoming, scope, session, bot_record, user, runtime
    )


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
    if not can_use(bot_record, message.from_user.id):  # private bot: stay quiet in groups
        return
    user = await started_user(session, bot_record, message.from_user.id)
    if user is None:
        await ask_to_start(message, bot_record, runtime)
        return
    text = strip_mention(message.text, bot_record.username) or message.text
    await _respond(Inbound.of(message), text, incoming, scope, session, bot_record, user, runtime)


async def _allowed_in_private(message: Message, bot_record: BotRecord, runtime: Runtime) -> bool:
    """A private bot tells strangers it is private, without a model call."""
    if can_use(bot_record, message.from_user.id):
        return True
    await runtime.sender.send_text(
        message.bot,
        message.chat.id,
        t(lang_of(message.from_user.language_code), "bot.private"),
        group=False,
    )
    return False


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
    payer: User,
    runtime: Runtime,
) -> None:
    """Answer `payer`'s message; they pay for it (SPEC 8.7)."""
    if payer.is_disabled:  # suspended by an admin: no bot serves them
        return
    group = inbound.group
    now = datetime.now(UTC)
    try:
        chosen = await reply_model(
            session, runtime.models, runtime.cipher, runtime.pricing, bot_record, payer
        )
    except Exception:
        # E.g. a wallet key that no longer decrypts: still answer, never go silent.
        log.exception("choosing the model failed for bot %s user %s", bot_record.id, payer.id)
        await runtime.sender.send_text(
            inbound.client,
            inbound.chat_id,
            "Sorry, something went wrong. Please try again.",
            group=group,
            reply_to=inbound.message_id if group else None,
        )
        return
    quota = await check_llm_quota(
        session, runtime.pricing, payer, bot_record, now, byok=chosen.byok
    )
    if not quota.allowed:
        await notify_quota_exhausted(inbound, bot_record, payer, quota.reason, runtime, now)
        return
    if chosen.downgraded:
        await suggest_stronger_model(inbound, bot_record, payer, runtime)

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
                session, runtime.pricing, bot_record, payer, scope, text, runtime.embedder
            )
        # People make their own scheduled tasks in a private chat (SPEC 8.7).
        schedule = (
            None
            if group
            else Scheduler(
                runtime, bot_record, payer, inbound.client, lang_of(inbound.sender_language)
            )
        )
        ask_for_keys = KeyRequester(
            runtime, bot_record, inbound.client, inbound.chat_id, lang_of(inbound.sender_language)
        )
        kit = await toolkit(
            session, runtime, bot_record, now, payer, schedule=schedule, ask_for_keys=ask_for_keys
        )
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
        # Never fall back to our model: the person chose to pay with their own key.
        failure = failure_of(e) if chosen.byok else None
        lang = lang_of(inbound.sender_language)
        await runtime.sender.send_text(
            inbound.client,
            inbound.chat_id,
            t(lang, "byok.chat_failed", reason=t(lang, f"byok.reason.{failure}"))
            if failure is not None
            else "Sorry, something went wrong. Please try again.",
            group=group,
            reply_to=reply_to,
        )
        return

    usage = await record_llm_usage(
        session,
        runtime.pricing,
        payer,
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


async def suggest_stronger_model(
    inbound: Inbound, bot_record: BotRecord, payer: User, runtime: Runtime
) -> None:
    """The bot's recommended model is beyond the person's plan: say so, once ever."""
    key = f"model-hint:{payer.id}:{bot_record.id}"
    if not await runtime.redis.set(key, 1, nx=True):
        return
    await runtime.sender.send_text(
        inbound.client,
        inbound.chat_id,
        t(lang_of(inbound.sender_language), "model.suggest"),
        group=inbound.group,
        reply_to=inbound.message_id if inbound.group else None,
    )


async def notify_quota_exhausted(
    inbound: Inbound,
    bot_record: BotRecord,
    payer: User,
    reason: QuotaReason,
    runtime: Runtime,
    now: datetime,
) -> None:
    """Tell the person their own quota is used up, with a way to top up; once a day."""
    key = f"quota:{bot_record.id}:{inbound.chat_id}:{payer.id}:{reason}"
    if not await first_today(runtime.redis, key, now, payer.timezone):
        return
    lang = lang_of(inbound.sender_language)
    buttons = None
    if reason != QuotaReason.BOT_DAILY_CAP:
        top_up = InlineKeyboardButton(
            text=t(lang, "btn.top_up"), url=f"https://t.me/{runtime.factory_username}?start=buy"
        )
        buttons = InlineKeyboardMarkup(inline_keyboard=[[top_up]])
    await runtime.sender.send_text(
        inbound.client,
        inbound.chat_id,
        t(lang, f"quota.{reason}"),
        group=inbound.group,
        reply_to=inbound.message_id if inbound.group else None,
        buttons=buttons,
    )
