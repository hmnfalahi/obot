from unittest.mock import AsyncMock

from aiogram import Dispatcher

from obot.telegram.webhook import WebhookManager

FACTORY_ID = 100
BOT_ID = 200
UPDATE = {
    "update_id": 1,
    "message": {
        "message_id": 5,
        "date": 1_790_000_000,
        "chat": {"id": 7, "type": "private"},
        "from": {"id": 7, "is_bot": False, "first_name": "A"},
        "text": "hi",
    },
}


class FakeQueue:
    def __init__(self) -> None:
        self.submitted = []

    async def submit(self, bot, dispatcher, update) -> None:
        self.submitted.append((bot.id, update.update_id))


def fake_bot(tg_id: int) -> AsyncMock:
    bot = AsyncMock()
    bot.id = tg_id
    return bot


def manager(queue=None) -> WebhookManager:
    return WebhookManager("https://obot.example/", b"k" * 32, FACTORY_ID, queue or FakeQueue())


async def test_start_registers_webhook_with_secret() -> None:
    webhooks = manager()
    bot = fake_bot(BOT_ID)

    await webhooks.start(bot, Dispatcher())

    kwargs = bot.set_webhook.await_args.kwargs
    assert kwargs["url"] == f"https://obot.example/tg/bot/{BOT_ID}"
    assert kwargs["secret_token"] == webhooks.secret_for(BOT_ID)
    assert webhooks.is_running(BOT_ID)


async def test_factory_uses_its_own_path() -> None:
    assert manager().url_for(FACTORY_ID) == "https://obot.example/tg/factory"


async def test_secrets_differ_per_bot_and_key() -> None:
    a, b = manager(), WebhookManager("https://x", b"z" * 32, FACTORY_ID, FakeQueue())

    assert a.secret_for(1) != a.secret_for(2)
    assert a.secret_for(1) != b.secret_for(1)
    assert len(a.secret_for(1)) == 64


async def test_handle_accepts_only_the_right_secret() -> None:
    queue = FakeQueue()
    webhooks = manager(queue)
    await webhooks.start(fake_bot(BOT_ID), Dispatcher())

    assert not await webhooks.handle(BOT_ID, None, UPDATE)
    assert not await webhooks.handle(BOT_ID, webhooks.secret_for(999), UPDATE)
    assert not await webhooks.handle(999, webhooks.secret_for(999), UPDATE)
    assert queue.submitted == []

    assert await webhooks.handle(BOT_ID, webhooks.secret_for(BOT_ID), UPDATE)
    assert queue.submitted == [(BOT_ID, 1)]


async def test_stop_removes_bot_and_deletes_webhook() -> None:
    webhooks = manager()
    bot = fake_bot(BOT_ID)
    await webhooks.start(bot, Dispatcher())

    await webhooks.stop(BOT_ID)

    assert not webhooks.is_running(BOT_ID)
    bot.delete_webhook.assert_awaited_once()
    assert not await webhooks.handle(BOT_ID, webhooks.secret_for(BOT_ID), UPDATE)


async def test_stop_all_keeps_webhooks_registered() -> None:
    webhooks = manager()
    bot = fake_bot(BOT_ID)
    await webhooks.start(bot, Dispatcher())

    await webhooks.stop_all()

    bot.delete_webhook.assert_not_awaited()
