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
    if field_type not in NUMERIC_TYPES and text.startswith(_FORMULA_STARTS):
        return "'" + text  # a spreadsheet would run this as a formula
    return text


async def build_csv(session: AsyncSession, space: DataSpace, c: Collection, lang: str) -> str:
    """The records this space can read, visible fields only, with a BOM for Excel."""
    fields = [f for f in collection_schema(c).fields if not f.hidden]
    tz = tz_for(space, c)
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out)
    writer.writerow([label_of(f.label, lang, f.key) for f in fields])
    offset = 0
    while True:
        rows = await find_records(
            session, space, c, descending=False, limit=MAX_FIND, offset=offset
        )
        for r in rows:
            writer.writerow([_cell(f.type, r.data.get(f.key), tz) for f in fields])
        if len(rows) < MAX_FIND:
            break
        offset += MAX_FIND
    return out.getvalue()
