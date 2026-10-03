"""Handlers shared by every managed bot. Agent replies arrive in SPEC task 3."""

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.service import mark_owner_started
from obot.db.models import Bot as BotRecord

router = Router(name="bots")


@router.message(CommandStart())
async def on_start(message: Message, session: AsyncSession, bot_record: BotRecord) -> None:
    first_time = await mark_owner_started(session, bot_record, message.from_user.id)
    if first_time:
        await message.answer("All set — I can message you now.")
    else:
        await message.answer(f"Hi! I'm {bot_record.display_name}.")


@router.message(F.text)
async def on_text(message: Message, bot_record: BotRecord) -> None:
    await message.answer(f"{bot_record.display_name} is not wired to an AI model yet.")
