"""Handlers of the factory bot (@ObotFactory)."""

import logging
import secrets
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.methods import GetManagedBotToken
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestManagedBot,
    ManagedBotUpdated,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.service import (
    ManagedBotInfo,
    RegisterOutcome,
    get_or_create_user,
    register_managed_bot,
)
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="factory")

# Signed 32-bit id echoed back by Telegram; only one create button exists for now.
CREATE_REQUEST_ID = 1


SUGGESTED_NAME = "My Obot"


def suggest_username() -> str:
    return f"obot_{secrets.token_hex(3)}_bot"


def create_bot_keyboard(suggested_username: str) -> ReplyKeyboardMarkup:
    button = KeyboardButton(
        text="Create my bot",
        request_managed_bot=KeyboardButtonRequestManagedBot(
            request_id=CREATE_REQUEST_ID,
            suggested_name=SUGGESTED_NAME,
            suggested_username=suggested_username,
        ),
    )
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True)


def create_bot_link(factory_username: str, suggested_username: str) -> str:
    # Fallback for clients without request_managed_bot support (seen on iOS).
    return (
        f"https://t.me/newbot/{factory_username}/{suggested_username}?name={quote(SUGGESTED_NAME)}"
    )


@router.message(CommandStart())
async def on_start(message: Message, bot: Bot, session: AsyncSession) -> None:
    user = message.from_user
    await get_or_create_user(session, user.id, user.language_code)
    username = suggest_username()
    await message.answer(
        "Hi! I build AI bots for you. Tap the button to create one.",
        reply_markup=create_bot_keyboard(username),
    )
    me = await bot.me()
    await message.answer(
        "Button not working? Use this link instead:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Create my bot (link)", url=create_bot_link(me.username, username)
                    )
                ]
            ]
        ),
    )


@router.managed_bot()
async def on_managed_bot(
    update: ManagedBotUpdated, bot: Bot, session: AsyncSession, runtime: Runtime
) -> None:
    async def fetch_token(tg_bot_id: int) -> str:
        return await bot(GetManagedBotToken(user_id=tg_bot_id))

    result = await register_managed_bot(
        session,
        runtime.cipher,
        fetch_token,
        creator_tg_id=update.user.id,
        info=ManagedBotInfo(
            tg_bot_id=update.bot_user.id,
            username=update.bot_user.username,
            display_name=update.bot_user.first_name,
        ),
    )
    log.info("managed bot @%s: %s", update.bot_user.username, result.outcome)

    if result.outcome == RegisterOutcome.IGNORED_DISABLED:
        await runtime.stop_bot(update.bot_user.id)
    elif result.outcome != RegisterOutcome.UNCHANGED or not runtime.polling.is_running(
        update.bot_user.id
    ):
        await runtime.launch_bot(result.token)


@router.message(F.managed_bot_created)
async def on_managed_bot_created(message: Message) -> None:
    username = message.managed_bot_created.bot_user.username
    await message.answer("✅ Your bot is ready.", reply_markup=ReplyKeyboardRemove())
    # A managed bot cannot message its owner until they press Start (see docs/spikes).
    await message.answer(
        f"Open @{username} and press Start so it can message you.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Open @{username}", url=f"https://t.me/{username}?start=init"
                    )
                ]
            ]
        ),
    )
