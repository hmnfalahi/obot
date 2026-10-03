"""Handlers shared by every managed bot."""

import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.usage import (
    QuotaReason,
    UsageMeter,
    allowed_tier,
    check_llm_quota,
    record_llm_usage,
)
from obot.bots.service import mark_owner_started
from obot.chat.history import ChatScope, recent_messages, record_message
from obot.chat.membership import upsert_membership
from obot.core.agent import BotProfile, Turn, generate_reply
from obot.core.tools import ToolServices, build_tools
from obot.db.models import Bot as BotRecord
from obot.db.models import Message as StoredMessage
from obot.db.models import SenderKind
from obot.i18n import lang_of, t
from obot.memory.jobs import recall_facts
from obot.memory.store import MEMORY_TOOL, forget_chat
from obot.notices import first_today
from obot.telegram.addressing import is_addressed, strip_mention
from obot.telegram.runtime import Runtime

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
    first_time = await mark_owner_started(session, bot_record, message.from_user.id)
    text = (
        "All set — I can message you now." if first_time else f"Hi! I'm {bot_record.display_name}."
    )
    await runtime.sender.send_text(message.bot, message.chat.id, text, group=False)


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
        # The owner blocked the bot: it can no longer message them until they start it again.
        if status == ChatMemberStatus.KICKED and event.from_user.id == bot_record.owner.tg_user_id:
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
    scope = ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id)
    incoming = await _record_incoming(session, scope, message)
    await _respond(message, message.text, incoming, scope, session, bot_record, runtime)


@router.message(F.text, F.chat.type.in_(GROUP_TYPES))
async def on_group_text(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    scope = ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id)
    incoming = await _record_incoming(session, scope, message)
    # Bot-to-bot conversations arrive in phase 2.
    if message.from_user.is_bot:
        return
    if not is_addressed(message, message.bot.id, bot_record.username):
        return
    text = strip_mention(message.text, bot_record.username) or message.text
    await _respond(message, text, incoming, scope, session, bot_record, runtime, group=True)


async def _record_incoming(
    session: AsyncSession, scope: ChatScope, message: Message
) -> StoredMessage:
    stored = await record_message(
        session,
        scope,
        sender_kind=SenderKind.BOT if message.from_user.is_bot else SenderKind.HUMAN,
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


async def _respond(
    message: Message,
    text: str,
    incoming: StoredMessage,
    scope: ChatScope,
    session: AsyncSession,
    bot_record: BotRecord,
    runtime: Runtime,
    *,
    group: bool = False,
) -> None:
    owner = bot_record.owner
    now = datetime.now(UTC)
    quota = await check_llm_quota(session, runtime.pricing, owner, bot_record, now)
    if not quota.allowed:
        await notify_quota_exhausted(message, bot_record, quota.reason, runtime, now, group)
        return

    history = [
        to_turn(m, group)
        for m in await recent_messages(session, scope, runtime.history_limit, before_id=incoming.id)
    ]
    user_text = f"{message.from_user.full_name}: {text}" if group else text
    profile = BotProfile(name=bot_record.display_name, persona=bot_record.persona)
    memories: list[str] = []
    reply_to = message.message_id if group else None

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        if MEMORY_TOOL in (bot_record.tools or []):
            memories = await recall_facts(
                session, runtime.pricing, bot_record, scope, text, runtime.embedder
            )
        tier = allowed_tier(runtime.pricing, owner, bot_record.model_tier)
        services = ToolServices(
            http=runtime.http,
            meter=UsageMeter(session, runtime.pricing, owner, bot_record, now),
            tavily_api_key=runtime.tavily_api_key,
        )
        reply = await generate_reply(
            runtime.models.for_tier(tier),
            profile,
            history,
            user_text,
            tools=build_tools(bot_record.tools or []),
            services=services,
            group=group,
            memories=memories,
        )
    except Exception:
        log.exception("agent failed for bot %s chat %s", bot_record.id, message.chat.id)
        await runtime.sender.send_text(
            message.bot,
            message.chat.id,
            "Sorry, something went wrong. Please try again.",
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
        message.bot, message.chat.id, reply.text, group=group, reply_to=reply_to
    )
    await record_message(
        session,
        scope,
        sender_kind=SenderKind.SELF,
        text=reply.text,
        tg_message_id=sent[-1].message_id if sent else None,
        sender_tg_id=message.bot.id,
        sender_name=bot_record.display_name,
    )


async def notify_quota_exhausted(
    message: Message,
    bot_record: BotRecord,
    reason: QuotaReason,
    runtime: Runtime,
    now: datetime,
    group: bool,
) -> None:
    """Tell the chat (once per day) and the owner (once per day per reason)."""
    owner = bot_record.owner
    chat_key = f"quota:{bot_record.id}:{message.chat.id}"
    if await first_today(runtime.redis, chat_key, now, owner.timezone):
        await runtime.sender.send_text(
            message.bot,
            message.chat.id,
            t(lang_of(message.from_user.language_code), f"quota.{reason}"),
            group=group,
            reply_to=message.message_id if group else None,
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
