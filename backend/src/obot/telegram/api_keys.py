"""People's own keys for a bot's APIs, asked for when a call needs them (SPEC 8.7).

When call_api needs a key the person has not set, the bot shows a button per
key. Pressed, the bot waits for the next private message: it is encrypted and
stored, never recorded, remembered or shown to the model, and deleted from the
chat right away. Keys are never taken in groups. The panel can set them too.
"""

import logging
from typing import Any

from aiogram import Bot as Client
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.access import can_use
from obot.bots.apis import bot_key_slots, set_key_value
from obot.bots.users import started_user
from obot.db.models import Bot as BotRecord
from obot.db.models import User
from obot.i18n import lang_of, t
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="api_keys")

# How long the bot waits for a key after its button was pressed.
WAIT_SECONDS = 600


def waiting_key(bot_id: int, tg_user_id: int) -> str:
    return f"api-key-wait:{bot_id}:{tg_user_id}"


class KeyRequester:
    """The `ask_for_keys` callable for ToolServices of one reply.

    In a private chat it shows a button per missing key; in a group it only
    tells the model to send the person to the private chat.
    """

    def __init__(
        self, runtime: Runtime, bot: BotRecord, client: Client, chat_id: int, lang: str
    ) -> None:
        self._runtime = runtime
        self._bot_id = bot.id
        self._client = client
        self._chat_id = chat_id
        self._lang = lang

    async def __call__(self, names: list[str]) -> str:
        listed = ", ".join(names)
        if self._chat_id < 0:
            return (
                f"ERROR: the user has not set their {listed} for you yet. Tell them to set it in "
                "a private chat with you (never in the group)."
            )
        lang = self._lang
        rows = [
            [
                InlineKeyboardButton(
                    text=t(lang, "btn.key_in_chat", name=name), callback_data=f"apikey:{name}"
                )
            ]
            for name in names
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(lang, "btn.key_in_panel"),
                    url=f"https://t.me/{self._runtime.factory_username}?start=mine_{self._bot_id}",
                )
            ]
        )
        await self._runtime.sender.send_text(
            self._client,
            self._chat_id,
            t(lang, "apikey.needed", names=listed),
            group=False,
            buttons=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return (
            f"The user must first set their {listed} for you; a message with buttons to do it "
            "was sent. Tell them in one short sentence and stop here."
        )


@router.callback_query(F.data.startswith("apikey:"))
async def on_key_button(
    query: CallbackQuery, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    lang = lang_of(query.from_user.language_code)
    name = query.data.removeprefix("apikey:")
    private = query.message is not None and query.message.chat.type == ChatType.PRIVATE
    if not private or name not in await bot_key_slots(session, bot_record):
        await query.answer()
        return
    await runtime.redis.set(waiting_key(bot_record.id, query.from_user.id), name, ex=WAIT_SECONDS)
    await query.answer()
    await runtime.sender.send_text(
        query.bot, query.from_user.id, t(lang, "apikey.send_now", name=name), group=False
    )


class WaitingForKey(BaseFilter):
    """A private text while the bot waits for one of the person's keys.

    A command instead cancels the wait and is handled as usual.
    """

    async def __call__(
        self, message: Message, runtime: Runtime, bot_record: BotRecord, **_: Any
    ) -> bool | dict[str, Any]:
        key = waiting_key(bot_record.id, message.from_user.id)
        name = await runtime.redis.get(key)
        if name is None:
            return False
        if message.text.startswith("/"):
            await runtime.redis.delete(key)
            return False
        return {"key_name": name.decode() if isinstance(name, bytes) else name}


@router.message(F.text, F.chat.type == ChatType.PRIVATE, WaitingForKey())
async def on_key_value(
    message: Message,
    session: AsyncSession,
    bot_record: BotRecord,
    runtime: Runtime,
    key_name: str,
) -> None:
    lang = lang_of(message.from_user.language_code)
    await runtime.redis.delete(waiting_key(bot_record.id, message.from_user.id))
    # The message holds a secret: delete it first, whatever happens next.
    try:
        await message.delete()
    except TelegramAPIError:
        log.warning("could not delete a key message in bot %s", bot_record.id)
    user: User | None = await started_user(session, bot_record, message.from_user.id)
    if user is None or not can_use(bot_record, message.from_user.id):
        return
    await set_key_value(session, runtime.cipher, user, bot_record, key_name, message.text.strip())
    await session.commit()
    await runtime.sender.send_text(
        message.bot, message.chat.id, t(lang, "apikey.saved", name=key_name), group=False
    )
