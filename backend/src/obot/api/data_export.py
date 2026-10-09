"""CSV export of a collection, fetched with a short-lived signed link so Telegram's
downloadFile can get it without our auth header (SPEC 8.8)."""

import base64
import csv
import hashlib
import hmac
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from obot.data.describe import label_of
from obot.data.query import MAX_FIND, find_records
from obot.data.repository import collection_schema
from obot.data.schema import NUMERIC_TYPES, FieldType
from obot.data.space import DataSpace, tz_for
from obot.db.models import Collection

EXPORT_TTL = 300  # seconds
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _sign(secret: str, payload: str) -> str:
    key = hashlib.sha256(f"obot-export:{secret}".encode()).digest()
    mac = hmac.new(key, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def make_token(secret: str, bot_id: int, collection_id: int, user_id: int, now: datetime) -> str:
    payload = f"{bot_id}.{collection_id}.{user_id}.{int(now.timestamp()) + EXPORT_TTL}"
    return f"{payload}.{_sign(secret, payload)}"


def read_token(secret: str, token: str, now: datetime) -> tuple[int, int, int] | None:
    """(bot_id, collection_id, user_id) of a valid, unexpired token; None otherwise."""
    if not token.isascii():
        return None
    payload, _, sig = token.rpartition(".")
    if not payload or not hmac.compare_digest(sig, _sign(secret, payload)):
        return None
    try:
        bot_id, collection_id, user_id, expires = (int(x) for x in payload.split("."))
    except ValueError:
        return None
    if now.timestamp() > expires:
        return None
    return bot_id, collection_id, user_id


def _safe(text: str) -> str:
    """Text a spreadsheet would run as a formula, quoted so it stays text."""
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


def _cell(field_type: FieldType, value, tz: ZoneInfo) -> str:
    if value is None:
        return ""
    if field_type == FieldType.DATETIME and isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value)
    # Numbers and phone numbers ("+98...", "-5") are kept as they are.
    if field_type in NUMERIC_TYPES or field_type == FieldType.PHONE:
        return text
    return _safe(text)


async def build_csv(session: AsyncSession, space: DataSpace, c: Collection, lang: str) -> str:
    """The records this space can read, visible fields only, with a BOM for Excel."""
    fields = [f for f in collection_schema(c).fields if not f.hidden]
    tz = tz_for(space, c)
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out)
    writer.writerow([_safe(label_of(f.label, lang, f.key)) for f in fields])
    after_id = None
    while True:
        rows = await find_records(
            session, space, c, descending=False, limit=MAX_FIND, after_id=after_id
        )
        for r in rows:
            writer.writerow([_cell(f.type, r.data.get(f.key), tz) for f in fields])
        if len(rows) < MAX_FIND:
            break
        after_id = rows[-1].id
    return out.getvalue()
