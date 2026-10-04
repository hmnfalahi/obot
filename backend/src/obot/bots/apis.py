"""A bot's endpoints (defined by its owner) and each person's keys for them (SPEC 8.7).

Endpoint headers are templates and not secret; the keys people fill into their
slots are, AES-GCM encrypted and bound to the person, bot and key name.
"""

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.core.custom_api import Endpoint, InvalidEndpoint, key_slots, validate_endpoint
from obot.crypto import TokenCipher
from obot.db.models import ApiEndpoint, ApiKeyValue, Bot, User


def write_endpoint(
    row: ApiEndpoint,
    *,
    name: str,
    description: str,
    method: str,
    url: str,
    headers: dict[str, str],
) -> None:
    """Validate and set the endpoint's fields."""
    validate_endpoint(name, method, url, headers)
    row.name, row.description, row.method, row.url = name, description, method, url
    row.headers = headers


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


async def load_endpoints(session: AsyncSession, bot: Bot) -> dict[str, Endpoint]:
    """The bot's endpoints, for the call_api tool."""
    return {
        row.name: Endpoint(
            name=row.name,
            description=row.description,
            method=row.method,
            url=row.url,
            headers=dict(row.headers or {}),
        )
        for row in await list_endpoints(session, bot)
    }


async def bot_key_slots(session: AsyncSession, bot: Bot) -> list[str]:
    """Every key a person may need to set to use the bot's endpoints."""
    names: list[str] = []
    for row in await list_endpoints(session, bot):
        names += key_slots(row.headers or {})
    return list(dict.fromkeys(names))


# --- each person's keys -------------------------------------------------------------------


def _context(user_id: int, bot_id: int, name: str) -> str:
    return f"api-key:{user_id}:{bot_id}:{name}"


async def _key_rows(session: AsyncSession, user: User, bot: Bot) -> list[ApiKeyValue]:
    rows = await session.scalars(
        select(ApiKeyValue).where(ApiKeyValue.user_id == user.id, ApiKeyValue.bot_id == bot.id)
    )
    return list(rows)


async def key_values(
    session: AsyncSession, cipher: TokenCipher, user: User, bot: Bot
) -> dict[str, str]:
    """The person's keys for the bot, decrypted, by name."""
    return {
        row.name: cipher.decrypt(row.value_encrypted, _context(user.id, bot.id, row.name))
        for row in await _key_rows(session, user, bot)
    }


async def keys_set(session: AsyncSession, user: User, bot: Bot) -> set[str]:
    return {row.name for row in await _key_rows(session, user, bot)}


async def set_key_value(
    session: AsyncSession, cipher: TokenCipher, user: User, bot: Bot, name: str, value: str
) -> None:
    row = await session.scalar(
        select(ApiKeyValue).where(
            ApiKeyValue.user_id == user.id, ApiKeyValue.bot_id == bot.id, ApiKeyValue.name == name
        )
    )
    if row is None:
        row = ApiKeyValue(user_id=user.id, bot_id=bot.id, name=name)
        session.add(row)
    row.value_encrypted = cipher.encrypt(value, _context(user.id, bot.id, name))
    await session.flush()


async def delete_key_value(session: AsyncSession, user: User, bot: Bot, name: str) -> None:
    await session.execute(
        delete(ApiKeyValue).where(
            ApiKeyValue.user_id == user.id, ApiKeyValue.bot_id == bot.id, ApiKeyValue.name == name
        )
    )
