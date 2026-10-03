"""Spike: validate Telegram Managed Bots end to end (SPEC task 1).

Runs the factory bot with long polling (no public URL needed) and answers:

1. Can the factory create a bot via KeyboardButtonRequestManagedBot?
2. Is the token retrievable with getManagedBotToken (also after restart)?
3. Can the new bot message its creator before they press Start?
4. What are the default access settings of a managed bot?
5. Does replaceManagedBotToken invalidate the old token (kill switch)?

Usage:
    cd backend && uv run python -m obot.spike.managed_bots

Factory commands: /start (show create button), /bots (list), /kill (revoke last token).
Findings are logged with the "[SPIKE]" prefix.
"""

import asyncio
import json
import logging
import secrets
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.methods import (
    GetManagedBotAccessSettings,
    GetManagedBotToken,
    ReplaceManagedBotToken,
)
from aiogram.types import (
    KeyboardButton,
    KeyboardButtonRequestManagedBot,
    ManagedBotUpdated,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from obot.config import get_settings

log = logging.getLogger("spike")

# Only bot ids are stored, never tokens: tokens are re-fetched on every start,
# which also checks that getManagedBotToken is repeatable.
STATE_FILE = Path(".spike_bots.json")

child_tasks: dict[int, asyncio.Task] = {}


def load_bot_ids() -> list[int]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return []


def save_bot_id(bot_id: int) -> None:
    ids = load_bot_ids()
    if bot_id not in ids:
        ids.append(bot_id)
        STATE_FILE.write_text(json.dumps(ids))


def child_router() -> Router:
    router = Router()

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        log.info("[SPIKE] child bot got /start from %s", message.from_user.id)
        await message.answer("Hi! I'm a managed bot created by Obot. Send me anything.")

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        await message.answer(f"echo: {message.text}")

    return router


async def start_child(factory: Bot, bot_id: int, creator_id: int | None) -> None:
    token = await factory(GetManagedBotToken(user_id=bot_id))
    log.info("[SPIKE] getManagedBotToken OK for bot %s", bot_id)

    try:
        access = await factory(GetManagedBotAccessSettings(user_id=bot_id))
        log.info("[SPIKE] default access settings for %s: %r", bot_id, access)
    except TelegramAPIError as e:
        log.warning("[SPIKE] getManagedBotAccessSettings failed: %s", e)

    child = Bot(token)
    me = await child.get_me()
    log.info("[SPIKE] child bot online: @%s (%s)", me.username, me.id)

    if creator_id is not None:
        try:
            await child.send_message(creator_id, "Hello from your new bot (sent before /start).")
            log.info("[SPIKE] RESULT: creation counts as start; child CAN message creator")
        except TelegramAPIError as e:
            log.info("[SPIKE] RESULT: child CANNOT message creator before /start: %s", e)

    dp = Dispatcher()
    dp.include_router(child_router())
    child_tasks[bot_id] = asyncio.create_task(dp.start_polling(child, handle_signals=False))


def factory_router(factory: Bot) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        suffix = secrets.token_hex(3)
        button = KeyboardButton(
            text="Create my bot",
            request_managed_bot=KeyboardButtonRequestManagedBot(
                request_id=1,
                suggested_name="Obot Spike",
                suggested_username=f"obot_spike_{suffix}_bot",
            ),
        )
        await message.answer(
            "Tap the button to create a managed bot.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True),
        )

    @router.message(F.managed_bot_created)
    async def on_created_message(message: Message) -> None:
        bot_user = message.managed_bot_created.bot_user
        log.info("[SPIKE] managed_bot_created service message: @%s", bot_user.username)
        await message.answer(
            f"Created @{bot_user.username}. Check whether it already messaged you.",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.managed_bot()
    async def on_managed_bot(update: ManagedBotUpdated) -> None:
        bot_id = update.bot_user.id
        log.info(
            "[SPIKE] managed_bot update: bot=@%s (%s) user=%s",
            update.bot_user.username,
            bot_id,
            update.user.id,
        )
        save_bot_id(bot_id)
        if bot_id in child_tasks:
            log.info("[SPIKE] bot %s already running (token/owner change?)", bot_id)
            return
        await start_child(factory, bot_id, creator_id=update.user.id)

    @router.message(Command("bots"))
    async def on_bots(message: Message) -> None:
        ids = load_bot_ids()
        await message.answer(f"Managed bots: {ids or 'none'}")

    @router.message(Command("kill"))
    async def on_kill(message: Message) -> None:
        ids = load_bot_ids()
        if not ids:
            await message.answer("No managed bots.")
            return
        bot_id = ids[-1]
        old_token = await factory(GetManagedBotToken(user_id=bot_id))
        if task := child_tasks.pop(bot_id, None):
            task.cancel()
        await factory(ReplaceManagedBotToken(user_id=bot_id))
        try:
            await Bot(old_token).get_me()
            result = "old token STILL WORKS"
        except TelegramAPIError as e:
            result = f"old token rejected: {e}"
        log.info("[SPIKE] RESULT kill switch for %s: %s", bot_id, result)
        await message.answer(f"Revoked token of {bot_id}: {result}")

    return router


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    token = get_settings().factory_bot_token
    if token is None:
        raise SystemExit("Set OBOT_FACTORY_BOT_TOKEN in backend/.env")

    factory = Bot(token.get_secret_value())
    me = await factory.get_me()
    log.info("[SPIKE] factory bot: @%s", me.username)

    for bot_id in load_bot_ids():
        try:
            await start_child(factory, bot_id, creator_id=None)
        except TelegramAPIError as e:
            log.warning("[SPIKE] could not restart bot %s: %s", bot_id, e)

    dp = Dispatcher()
    dp.include_router(factory_router(factory))
    await dp.start_polling(factory)


if __name__ == "__main__":
    asyncio.run(main())
