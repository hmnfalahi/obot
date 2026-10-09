"""Telegram side of the data tools (SPEC 8.8): a one-time notice that a bot's owner sees
what people save in its shared collections, and a confirm button for deletes."""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.access import can_use
from obot.bots.users import started_user
from obot.data.errors import DataError
from obot.data.repository import collection_by_id, collection_schema, delete_record
from obot.data.space import space_for
from obot.db.models import Bot as BotRecord
from obot.db.models import Collection, Record, User
from obot.i18n import lang_of, t
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="data")


def _title(c: Collection, lang: str) -> str:
    return c.title.get(lang) or next(iter(c.title.values()), c.key)


def record_text(c: Collection, record: Record, lang: str) -> str:
    """The record as 'Label: value' lines in the person's language."""
    fields = collection_schema(c).fields
    labels = {f.key: f.label.get(lang) or f.label.get("en") or f.key for f in fields}
    hidden = {f.key for f in fields if f.hidden}
    return "\n".join(f"{labels.get(k, k)}: {v}" for k, v in record.data.items() if k not in hidden)


class SharedNotice:
    """Tells a person once that the bot's owner sees what they save in shared collections."""

    def __init__(self, runtime: Runtime, bot: BotRecord, user: User) -> None:
        self._runtime = runtime
        self._bot_id, self._tg_bot_id = bot.id, bot.tg_bot_id
        self._user_id, self._tg_user_id = user.id, user.tg_user_id
        self._lang = lang_of(user.language)

    async def __call__(self) -> None:
        runtime = self._runtime
        key = f"data-shared-notice:{self._bot_id}:{self._user_id}"
        client = runtime.transport.client(self._tg_bot_id)
        if client is None or not await runtime.redis.set(key, 1, nx=True):
            return
        await runtime.sender.send_text(
            client, self._tg_user_id, t(self._lang, "data.shared_notice"), group=False
        )


class DeleteConfirmer:
    """Shows a person a record with Delete/Keep buttons; only the button deletes it."""

    def __init__(self, runtime: Runtime, bot: BotRecord, user: User) -> None:
        self._runtime = runtime
        self._tg_bot_id = bot.tg_bot_id
        self._tg_user_id = user.tg_user_id
        self._lang = lang_of(user.language)

    async def __call__(self, c: Collection, record: Record) -> str:
        client = self._runtime.transport.client(self._tg_bot_id)
        if client is None:
            return "ERROR: cannot ask the user to confirm right now; try again later."
        lang = self._lang
        data = f"{c.id}:{record.id}"
        buttons = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t(lang, "btn.data_delete"), callback_data=f"data:del:{data}"
                    ),
                    InlineKeyboardButton(
                        text=t(lang, "btn.data_keep"), callback_data=f"data:keep:{data}"
                    ),
                ]
            ]
        )
        text = t(
            lang,
            "data.delete_confirm",
            collection=_title(c, lang),
            record=record_text(c, record, lang),
        )
        await self._runtime.sender.send_text(
            client, self._tg_user_id, text, group=False, buttons=buttons
        )
        return (
            f"The user was shown record #{record.id} with Delete and Keep buttons. Tell them "
            "in one short sentence to confirm there; it is not deleted yet."
        )


@router.callback_query(F.data.startswith("data:"))
async def on_data_button(
    query: CallbackQuery, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    lang = lang_of(query.from_user.language_code)
    parts = query.data.split(":")
    if len(parts) != 4 or not (parts[2].isdigit() and parts[3].isdigit()):
        await query.answer()
        return
    _, action, collection_id, record_id = parts
    if action == "keep":
        await query.message.edit_text(t(lang, "data.kept"))
        await query.answer()
        return
    user = await started_user(session, bot_record, query.from_user.id)
    try:
        if user is None or not can_use(bot_record, query.from_user.id):
            raise DataError("not a user of this bot")
        space = await space_for(session, bot_record, user)
        c = await collection_by_id(session, space, int(collection_id))
        await delete_record(session, space, c, int(record_id))
    except DataError:
        await query.answer(t(lang, "data.not_yours"), show_alert=True)
        return
    await session.commit()
    await query.message.edit_text(t(lang, "data.deleted"))
    await query.answer()
