"""Handlers shared by every managed bot."""

import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message
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
from obot.core.agent import BotProfile, Turn, generate_reply
from obot.core.tools import ToolServices, build_tools
from obot.db.models import Bot as BotRecord
from obot.db.models import SenderKind
from obot.i18n import lang_of, t
from obot.notices import first_today
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="bots")

TELEGRAM_TEXT_LIMIT = 4096


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split on paragraph/line/space boundaries so each chunk fits one Telegram message."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind(sep, 0, limit) for sep in ("\n\n", "\n", " "))
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


@router.message(CommandStart())
async def on_start(message: Message, session: AsyncSession, bot_record: BotRecord) -> None:
    first_time = await mark_owner_started(session, bot_record, message.from_user.id)
    if first_time:
        await message.answer("All set — I can message you now.")
    else:
        await message.answer(f"Hi! I'm {bot_record.display_name}.")


# Groups arrive in SPEC task 7.
@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def on_private_text(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    scope = ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id)
    incoming = await record_message(
        session,
        scope,
        sender_kind=SenderKind.HUMAN,
        text=message.text,
        tg_message_id=message.message_id,
        sender_tg_id=message.from_user.id,
        sender_name=message.from_user.full_name,
    )
    # Persist the incoming message even if the model call fails below.
    await session.commit()

    owner = bot_record.owner
    now = datetime.now(UTC)
    quota = await check_llm_quota(session, runtime.pricing, owner, bot_record, now)
    if not quota.allowed:
        await notify_quota_exhausted(message, bot_record, quota.reason, runtime, now)
        return

    history = [
        Turn(role="assistant" if m.sender_kind == SenderKind.SELF else "user", text=m.text)
        for m in await recent_messages(session, scope, runtime.history_limit, before_id=incoming.id)
    ]
    profile = BotProfile(name=bot_record.display_name, persona=bot_record.persona)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
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
            message.text,
            tools=build_tools(bot_record.tools or []),
            services=services,
        )
    except Exception:
        log.exception("agent failed for bot %s chat %s", bot_record.id, message.chat.id)
        await message.answer("Sorry, something went wrong. Please try again.")
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
        "bot %s replied with %s (%d in / %d out tokens, %d credits)",
        bot_record.id,
        reply.model_name,
        reply.input_tokens,
        reply.output_tokens,
        usage.credits,
    )
    sent = None
    for chunk in split_text(reply.text):
        sent = await message.answer(chunk)
    await record_message(
        session,
        scope,
        sender_kind=SenderKind.SELF,
        text=reply.text,
        tg_message_id=sent.message_id if sent else None,
        sender_tg_id=message.bot.id,
        sender_name=bot_record.display_name,
    )


async def notify_quota_exhausted(
    message: Message,
    bot_record: BotRecord,
    reason: QuotaReason,
    runtime: Runtime,
    now: datetime,
) -> None:
    """Tell the chat (once per day) and the owner (once per day per reason)."""
    owner = bot_record.owner
    chat_key = f"quota:{bot_record.id}:{message.chat.id}"
    if await first_today(runtime.redis, chat_key, now, owner.timezone):
        await message.answer(t(lang_of(message.from_user.language_code), f"quota.{reason}"))

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
