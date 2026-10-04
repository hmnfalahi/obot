import base64
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, PhotoSize, User
from PIL import Image
from redis.asyncio import Redis

from obot.bots.avatar import InvalidImage, square_jpeg
from obot.config import get_settings
from obot.telegram import avatars
from tests.test_api import ALICE, BOB, auth
from tests.test_api import make_bot as make_api_bot
from tests.test_history import make_bot

OWNER = User(id=1001, is_bot=False, first_name="Owner", language_code="en")


def png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), (200, 50, 50, 128)).save(out, "PNG")
    return out.getvalue()


def test_any_image_becomes_a_square_jpeg() -> None:
    big = Image.open(io.BytesIO(square_jpeg(png(1200, 800))))
    small = Image.open(io.BytesIO(square_jpeg(png(300, 500))))

    assert (big.format, big.size) == ("JPEG", (640, 640))
    assert small.size == (300, 300)
    with pytest.raises(InvalidImage):
        square_jpeg(b"not an image")


async def test_owner_uploads_an_avatar_in_the_panel(api, monkeypatch) -> None:
    from obot.api import routes

    sent = []

    async def fake_set_avatar(token: str, jpeg: bytes) -> bool:
        sent.append((token, jpeg[:2]))
        return True

    monkeypatch.setattr(routes, "set_avatar", fake_set_avatar)
    bot_id = await make_api_bot(api, ALICE, 80)
    body = {"image": base64.b64encode(png(400, 400)).decode()}

    ok = await api.post(f"/api/bots/{bot_id}/avatar", json=body, headers=auth(ALICE))
    stranger = await api.post(f"/api/bots/{bot_id}/avatar", json=body, headers=auth(BOB))
    junk = await api.post(
        f"/api/bots/{bot_id}/avatar", json={"image": "bm90IGFuIGltYWdl"}, headers=auth(ALICE)
    )

    assert ok.status_code == 204 and stranger.status_code == 404 and junk.status_code == 422
    assert sent == [("80:token", b"\xff\xd8")]  # the bot's own token, a JPEG


@pytest.fixture
async def redis():
    client = Redis.from_url(get_settings().redis_url, db=15)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


def photo_message(caption: str | None = None) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=OWNER.id, type="private"),
        from_user=OWNER,
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=500, height=500)],
        caption=caption,
    )


async def test_photos_go_to_the_bot_the_owner_picked_or_named(session, cipher, redis) -> None:
    from obot.db.models import Bot as BotRecord

    record = await session.get(BotRecord, await make_bot(session, cipher, 1))
    runtime = SimpleNamespace(redis=redis, cipher=cipher)
    target = avatars.AvatarTarget()

    assert await target(photo_message(), session=session, runtime=runtime) is False
    named = await target(photo_message(f"@{record.username}"), session=session, runtime=runtime)
    await redis.set(avatars.waiting_key(OWNER.id), record.id)
    picked = await target(photo_message(), session=session, runtime=runtime)

    assert named["avatar_bot"].id == picked["avatar_bot"].id == record.id


async def test_a_received_photo_becomes_the_avatar(session, cipher, redis, monkeypatch) -> None:
    from obot.db.models import Bot as BotRecord

    record = await session.get(BotRecord, await make_bot(session, cipher, 1))
    set_with = []

    async def fake_set_avatar(token: str, jpeg: bytes) -> bool:
        set_with.append(token)
        return True

    async def download(file, destination) -> None:
        destination.write(png(500, 300))

    monkeypatch.setattr(avatars, "set_avatar", fake_set_avatar)
    factory = AsyncMock(download=download)
    message = photo_message().as_(factory)
    await redis.set(avatars.waiting_key(OWNER.id), record.id)

    await avatars.on_avatar_image(
        message, factory, SimpleNamespace(redis=redis, cipher=cipher), record
    )

    assert set_with == ["1:token"]
    assert await redis.get(avatars.waiting_key(OWNER.id)) is None
