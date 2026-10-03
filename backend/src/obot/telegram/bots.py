"""Handlers shared by every managed bot."""

import logging

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.service import mark_owner_started
from obot.chat.history import ChatScope, recent_messages, record_message
from obot.core.agent import BotProfile, Turn, generate_reply
from obot.db.models import Bot as BotRecord
from obot.db.models import SenderKind
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

    history = [
        Turn(role="assistant" if m.sender_kind == SenderKind.SELF else "user", text=m.text)
        for m in await recent_messages(session, scope, runtime.history_limit, before_id=incoming.id)
    ]
    profile = BotProfile(name=bot_record.display_name, persona=bot_record.persona)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        reply = await generate_reply(
            runtime.models.for_tier(bot_record.model_tier), profile, history, message.text
        )
    except Exception:
        log.exception("agent failed for bot %s chat %s", bot_record.id, message.chat.id)
        await message.answer("Sorry, something went wrong. Please try again.")
        return

    log.info(
        "bot %s replied with %s (%d in / %d out tokens)",
        bot_record.id,
        reply.model_name,
        reply.input_tokens,
        reply.output_tokens,
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
