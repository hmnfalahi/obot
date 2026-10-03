"""Telegram Mini App authentication (initData signed with the factory bot's token).

See https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

# initData older than this is rejected, so a leaked string stops working.
MAX_AGE_SECONDS = 24 * 3600


class InvalidInitData(Exception):
    pass


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str
    language_code: str | None


def _secret(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def _check_string(fields: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))


def validate_init_data(init_data: str, bot_token: str, now: float | None = None) -> TelegramUser:
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = fields.pop("hash", None)
    if not received:
        raise InvalidInitData("missing hash")
    expected = hmac.new(
        _secret(bot_token), _check_string(fields).encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InvalidInitData("bad signature")
    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
    except (KeyError, ValueError) as e:
        raise InvalidInitData("missing fields") from e
    if (now or time.time()) - auth_date > MAX_AGE_SECONDS:
        raise InvalidInitData("expired")
    return TelegramUser(
        id=int(user["id"]),
        first_name=user.get("first_name", ""),
        language_code=user.get("language_code"),
    )


def sign_init_data(user: dict, bot_token: str, auth_date: int | None = None) -> str:
    """Build valid initData; for tests and local development outside Telegram."""
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    fields["hash"] = hmac.new(
        _secret(bot_token), _check_string(fields).encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)
