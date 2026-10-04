"""A bot's owner-defined API endpoints: storage with encrypted headers."""

import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.core.custom_api import Endpoint, InvalidEndpoint, validate_endpoint
from obot.crypto import TokenCipher
from obot.db.models import ApiEndpoint, Bot


def _context(bot_id: int) -> str:
    return f"api-headers:{bot_id}"


def read_headers(cipher: TokenCipher, row: ApiEndpoint) -> dict[str, str]:
    if row.headers_encrypted is None:
        return {}
    return json.loads(cipher.decrypt(row.headers_encrypted, _context(row.bot_id)))


def write_endpoint(
    cipher: TokenCipher,
    row: ApiEndpoint,
    *,
    name: str,
    description: str,
    method: str,
    url: str,
    headers: dict[str, str] | None,
) -> None:
    """Validate and set the endpoint's fields; `headers` None keeps the stored ones."""
    kept = read_headers(cipher, row) if headers is None else headers
    validate_endpoint(name, method, url, kept)
    row.name, row.description, row.method, row.url = name, description, method, url
    if headers is not None:
        row.headers_encrypted = (
            cipher.encrypt(json.dumps(headers), _context(row.bot_id)) if headers else None
        )


async def list_endpoints(session: AsyncSession, bot: Bot) -> list[ApiEndpoint]:
    rows = await session.scalars(
        select(ApiEndpoint).where(ApiEndpoint.bot_id == bot.id).order_by(ApiEndpoint.id)
    )
    return list(rows)


async def ensure_room(session: AsyncSession, bot: Bot, limit: int) -> None:
    count = await session.scalar(
        select(func.count()).select_from(ApiEndpoint).where(ApiEndpoint.bot_id == bot.id)
    )
    if count >= limit:
        raise InvalidEndpoint(f"your plan allows {limit} APIs per bot")


async def load_endpoints(
    session: AsyncSession, cipher: TokenCipher, bot: Bot
) -> dict[str, Endpoint]:
    """The bot's endpoints with decrypted headers, for the call_api tool."""
    return {
        row.name: Endpoint(
            name=row.name,
            description=row.description,
            method=row.method,
            url=row.url,
            headers=read_headers(cipher, row),
        )
        for row in await list_endpoints(session, bot)
    }
