"""Telegram side of the data tools (SPEC 8.8): a one-time notice that a bot's owner sees
what people save in its shared collections, and a confirm button for deletes."""

import json
import logging
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.access import can_use
from obot.bots.users import started_user
from obot.data.errors import DataError, RecordNotFound
from obot.data.format import describe_error
from obot.data.repository import (
    MAX_ID,
    collection_by_id,
    collection_schema,
    delete_record,
    was_deleted,
)
from obot.data.schema import FieldDef, FieldType
from obot.data.service import Definition, apply_definition
from obot.data.space import space_for
from obot.db.models import Bot as BotRecord
from obot.db.models import Collection, CollectionScope, Record, User
from obot.i18n import lang_of, t
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="data")


def _title(c: Collection, lang: str) -> str:
    return c.title.get(lang) or next(iter(c.title.values()), c.key)


def _shown(f: FieldDef, value: object, tz: ZoneInfo) -> object:
    if f.type == FieldType.DATETIME and isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    return value


def record_text(c: Collection, record: Record, lang: str, tz: ZoneInfo) -> str:
    """The record as 'Label: value' lines in the person's language, without hidden fields;
    datetimes in `tz` (the collection's time zone)."""
    fields = {f.key: f for f in collection_schema(c).fields}
    lines = []
    for key, value in record.data.items():
        f = fields.get(key)
        if f is None:
            lines.append(f"{key}: {value}")
        elif not f.hidden:
            label = f.label.get(lang) or f.label.get("en") or key
            lines.append(f"{label}: {_shown(f, value, tz)}")
    return "\n".join(lines)


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

    def __init__(
        self, runtime: Runtime, bot: BotRecord, user: User, *, owner_timezone: str | None = None
    ) -> None:
        self._runtime = runtime
        self._tg_bot_id = bot.tg_bot_id
        self._tg_user_id = user.tg_user_id
        self._timezone = user.timezone
        self._owner_timezone = owner_timezone or user.timezone
        self._lang = lang_of(user.language)

    def _tz(self, c: Collection) -> ZoneInfo:
        personal = c.scope == CollectionScope.PERSONAL
        return ZoneInfo(self._timezone if personal else self._owner_timezone)

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
            record=record_text(c, record, lang, self._tz(c)),
        )
        await self._runtime.sender.send_text(
            client, self._tg_user_id, text, group=False, buttons=buttons
        )
        return (
            f"The user was shown record #{record.id} with Delete and Keep buttons. Tell them "
            "in one short sentence to confirm there; it is not deleted yet."
        )


async def _edit(query: CallbackQuery, text: str) -> None:
    if not isinstance(query.message, Message):
        return  # too old for Telegram to hand over
    try:
        await query.message.edit_text(text)
    except TelegramAPIError:
        log.warning("could not edit a data card", exc_info=True)


@router.callback_query(F.data.startswith("data:"))
async def on_data_button(
    query: CallbackQuery, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    lang = lang_of(query.from_user.language_code)
    parts = query.data.split(":")
    if len(parts) != 4 or not (parts[2].isdecimal() and parts[3].isdecimal()):
        await query.answer()
        return
    _, action, collection_id, record_id = parts
    if not (1 <= int(collection_id) <= MAX_ID and 1 <= int(record_id) <= MAX_ID):
        await query.answer()
        return
    if action == "keep":
        await query.answer()
        await _edit(query, t(lang, "data.kept"))
        return
    user = await started_user(session, bot_record, query.from_user.id)
    if user is not None:
        lang = lang_of(user.language)
    try:
        if user is None or not can_use(bot_record, query.from_user.id):
            raise DataError("not a user of this bot")
        space = await space_for(session, bot_record, user)
        c = await collection_by_id(session, space, int(collection_id))
        await delete_record(session, space, c, int(record_id))
    except RecordNotFound:
        if not await was_deleted(session, space, c, int(record_id)):
            await query.answer(t(lang, "data.not_yours"), show_alert=True)
            return
        # Pressed twice: it is already deleted.
        await query.answer()
        await _edit(query, t(lang, "data.deleted"))
        return
    except DataError:
        await query.answer(t(lang, "data.not_yours"), show_alert=True)
        return
    await session.commit()
    await query.answer()
    await _edit(query, t(lang, "data.deleted"))


DEFINE_TTL = 24 * 3600

TYPE_NAMES = {
    "en": {
        "text": "text",
        "number": "number",
        "money": "amount",
        "date": "date",
        "time": "time",
        "datetime": "date and time",
        "choice": "choice",
        "boolean": "yes/no",
        "phone": "phone",
    },
    "fa": {
        "text": "متن",
        "number": "عدد",
        "money": "مبلغ",
        "date": "تاریخ",
        "time": "ساعت",
        "datetime": "تاریخ و ساعت",
        "choice": "انتخابی",
        "boolean": "بله/خیر",
        "phone": "تلفن",
    },
}


def definition_text(d: Definition, lang: str) -> str:
    fields = {f["key"]: f for f in d.schema["fields"]}
    lines = []
    for key in d.added:
        f = fields[key]
        label = f["label"].get(lang) or next(iter(f["label"].values()), key)
        kind = TYPE_NAMES[lang].get(f["type"], f["type"])
        if f.get("options"):
            separator = "، " if lang == "fa" else ", "
            kind += ": " + separator.join(f["options"])
        lines.append(f"• {label} ({kind})")
    title = d.title.get(lang) or next(iter(d.title.values()), d.key)
    template = "data.define_new" if d.collection_id is None else "data.define_add"
    return t(lang, template, title=title, fields="\n".join(lines))


class DefineProposer:
    """Shows a person a proposed collection with Save/Cancel; the proposal waits in Redis."""

    def __init__(self, runtime: Runtime, bot: BotRecord, user: User) -> None:
        self._runtime = runtime
        self._tg_bot_id = bot.tg_bot_id
        self._tg_user_id = user.tg_user_id
        self._lang = lang_of(user.language)

    async def __call__(self, d: Definition) -> str:
        runtime = self._runtime
        client = runtime.transport.client(self._tg_bot_id)
        if client is None:
            return "ERROR: cannot ask the user to confirm right now; try again later."
        token = secrets.token_urlsafe(9)
        key = f"data-define:{token}"
        lang = self._lang
        buttons = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t(lang, "btn.data_save"), callback_data=f"datadef:ok:{token}"
                    ),
                    InlineKeyboardButton(
                        text=t(lang, "btn.data_cancel"), callback_data=f"datadef:no:{token}"
                    ),
                ]
            ]
        )
        await runtime.sender.send_text(
            client, self._tg_user_id, definition_text(d, lang), group=False, buttons=buttons
        )
        await runtime.redis.set(key, d.to_json(), ex=DEFINE_TTL)
        return (
            f"The user was shown the proposed collection {d.key!r} with Save and Cancel "
            "buttons. Tell them in one short sentence to press Save; it does not exist yet."
        )


@router.callback_query(F.data.startswith("datadef:"))
async def on_define_button(
    query: CallbackQuery, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    lang = lang_of(query.from_user.language_code)
    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] not in ("ok", "no"):
        await query.answer()
        return
    _, action, token = parts
    key = f"data-define:{token}"
    raw = await runtime.redis.get(key)
    d = None
    if raw is not None:
        try:
            d = Definition.from_json(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            await runtime.redis.delete(key)
    if d is None:
        await query.answer()
        await _edit(query, t(lang, "data.define_expired"))
        return
    user = await started_user(session, bot_record, query.from_user.id)
    if (
        user is None
        or user.id != d.user_id
        or d.bot_id != bot_record.id
        or not can_use(bot_record, query.from_user.id)
    ):
        await query.answer(t(lang, "data.not_for_you"), show_alert=True)
        return
    lang = lang_of(user.language)
    if not await runtime.redis.delete(key):  # someone else pressed first
        await query.answer()
        await _edit(query, t(lang, "data.define_expired"))
        return
    if action == "no":
        await query.answer()
        await _edit(query, t(lang, "data.define_cancelled"))
        return
    try:
        c = await apply_definition(session, runtime.pricing, d)
    except DataError as e:
        await session.rollback()
        await query.answer()
        reason = describe_error(e).removeprefix("ERROR: ")
        await _edit(query, t(lang, "data.define_failed", reason=reason))
        return
    except Exception:
        log.exception("could not apply a collection proposal")
        await session.rollback()
        await query.answer()
        await _edit(query, t(lang, "data.define_failed", reason="try again later"))
        return
    await session.commit()
    title = c.title.get(lang) or next(iter(c.title.values()), c.key)
    await query.answer()
    await _edit(query, t(lang, "data.define_saved", title=title))
