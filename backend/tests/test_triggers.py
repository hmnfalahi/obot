from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from obot.billing.plans import ModelPrice, Pricing
from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.chat.membership import upsert_membership
from obot.db.models import BotStatus, Task, TaskEvent, TaskStatus, TriggerKind
from obot.tasks.events import (
    MAX_HOOK_EVENTS_PER_HOUR,
    HookRejected,
    accept_webhook,
    chat_trigger_tasks,
    claim_events,
    enqueue_event,
    keyword_hit,
)
from obot.tasks.planner import KnownChat, PlannedTask
from obot.tasks.service import build_tasks, claim_due, hook_url, replace_bot_tasks
from obot.tasks.watchers import check_feed, check_page, check_price
from obot.telegram.task_runner import task_prompt
from tests.test_api import ALICE
from tests.test_api import make_bot as make_api_bot

PLANS_FILE = Path(__file__).parent.parent / "plans.yaml"
# A real-sized Telegram group id: it does not fit in a 32-bit integer.
GROUP = -5410294025
NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
FREE = Pricing.load(PLANS_FILE).plan("free")


def planned(trigger: str, **fields) -> PlannedTask:
    return PlannedTask(
        instruction="Tell me.",
        trigger=trigger,
        timezone="Asia/Tehran",
        destination="owner",
        summary=trigger,
        **fields,
    )


async def make_bot(session, cipher, in_group: bool = True):
    async def fetch_token(_: int) -> str:
        return "9:t"

    result = await register_managed_bot(
        session, cipher, fetch_token, 1001, ManagedBotInfo(9, "w_bot", "W"), 10
    )
    result.bot.owner_started = True
    if in_group:
        await upsert_membership(
            session,
            result.bot.id,
            GROUP,
            chat_type="supergroup",
            title="G",
            is_member=True,
            is_admin=True,
        )
    await session.flush()
    return result.bot


CHATS = [KnownChat(GROUP, "G", "supergroup")]


# --- building and validating ---------------------------------------------------------------


async def test_watchers_poll_soon_and_respect_plan_interval(session, cipher) -> None:
    bot = await make_bot(session, cipher)

    [rss, price], rejected = build_tasks(
        [
            planned("rss", url="example.com/feed", every_minutes=5),
            planned(
                "price", url="https://p.test", what="USD rate", threshold=1e6, direction="above"
            ),
        ],
        bot,
        CHATS,
        FREE,
        0,
        NOW,
    )

    assert rejected == []
    assert rss.trigger_config == {"url": "https://example.com/feed", "every_minutes": 60}
    assert rss.next_run_at == NOW
    assert price.trigger_config["direction"] == "above"


async def test_invalid_triggers_are_rejected_with_reasons(session, cipher) -> None:
    bot = await make_bot(session, cipher)

    tasks, rejected = build_tasks(
        [
            planned("rss"),
            planned("price", url="https://p.test", what="x"),
            planned("group_keyword", source_chat=str(GROUP)),
            planned("group_keyword", source_chat="-555", keywords=["a"]),
            planned("new_member", source_chat="owner"),
            planned("webhook"),
        ],
        bot,
        CHATS,
        FREE,
        0,
        NOW,
    )

    assert tasks == []
    reasons = [r.reason for r in rejected]
    assert "web address" in reasons[0]
    assert "threshold" in reasons[1]
    assert "keywords" in reasons[2]
    assert "not a member" in reasons[3]
    assert "groups" in reasons[4]
    assert "public address" in reasons[5]


async def test_push_triggers_have_no_time(session, cipher) -> None:
    bot = await make_bot(session, cipher)

    [keyword, webhook], _ = build_tasks(
        [
            planned("group_keyword", source_chat=str(GROUP), keywords=["refund", " "]),
            planned("webhook"),
        ],
        bot,
        CHATS,
        FREE,
        0,
        NOW,
        public_url="https://obot.test/",
    )

    assert keyword.next_run_at is None and keyword.trigger_config["keywords"] == ["refund"]
    assert webhook.hook_secret and webhook.next_run_at is None
    webhook.id = 7
    assert (
        hook_url("https://obot.test/", webhook)
        == f"https://obot.test/api/hooks/7/{webhook.hook_secret}"
    )


async def add(session, bot, *items, **kwargs) -> list[Task]:
    tasks, rejected = build_tasks(list(items), bot, CHATS, FREE, 0, NOW, **kwargs)
    assert rejected == []
    await replace_bot_tasks(session, bot, tasks)
    return tasks


async def test_claim_due_polls_watchers_and_skips_push(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    [watcher, push] = await add(
        session,
        bot,
        planned("page_change", url="https://p.test", every_minutes=90),
        planned("new_member", source_chat=str(GROUP)),
    )

    claimed = await claim_due(session, NOW, 10)

    assert claimed == [watcher]
    assert watcher.next_run_at == NOW + timedelta(minutes=90)
    assert push.status == TaskStatus.ACTIVE and push.next_run_at is None


# --- events ----------------------------------------------------------------------------------


async def test_events_are_claimed_once_and_only_for_active_bots(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    [task] = await add(session, bot, planned("new_member", source_chat=str(GROUP)))
    await enqueue_event(session, task, "Ali joined")

    first = await claim_events(session, 10)
    second = await claim_events(session, 10)
    assert [e.payload for e in first] == ["Ali joined"] and second == []

    await enqueue_event(session, task, "Sara joined")
    bot.status = BotStatus.DISABLED
    await session.flush()
    assert await claim_events(session, 10) == []


async def test_chat_triggers_match_group_and_keywords(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    [task] = await add(
        session, bot, planned("group_keyword", source_chat=str(GROUP), keywords=["Refund"])
    )

    found = await chat_trigger_tasks(session, bot.id, GROUP, TriggerKind.GROUP_KEYWORD)
    assert found == [task]
    assert await chat_trigger_tasks(session, bot.id, -200, TriggerKind.GROUP_KEYWORD) == []
    assert keyword_hit(task, "I want a REFUND please")
    assert not keyword_hit(task, "thanks")


async def test_webhook_acceptance(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    [task] = await add(session, bot, planned("webhook"), public_url="https://o.test")

    with pytest.raises(HookRejected) as wrong:
        await accept_webhook(session, task.id, "nope", "x")
    assert wrong.value.status == 404

    await accept_webhook(session, task.id, task.hook_secret, '{"order": 5}')
    events = list(await session.scalars(select(TaskEvent)))
    assert [e.payload for e in events] == ['{"order": 5}']

    for _ in range(MAX_HOOK_EVENTS_PER_HOUR - 1):
        await accept_webhook(session, task.id, task.hook_secret, "x")
    with pytest.raises(HookRejected) as limited:
        await accept_webhook(session, task.id, task.hook_secret, "x")
    assert limited.value.status == 429

    task.status = TaskStatus.PAUSED
    with pytest.raises(HookRejected) as paused:
        await accept_webhook(session, task.id, task.hook_secret, "x")
    assert paused.value.status == 409


async def test_hook_endpoint(api) -> None:
    await make_api_bot(api, ALICE, 11)
    async with api.state.sessionmaker() as session:
        from obot.db.models import Bot

        bot = (await session.scalars(select(Bot))).one()
        await session.refresh(bot, ["owner"])
        [task] = await add(session, bot, planned("webhook"), public_url="https://o.test")
        await session.commit()
        task_id, secret = task.id, task.hook_secret

    ok = await api.post(f"/api/hooks/{task_id}/{secret}", content=b"paid")
    bad = await api.post(f"/api/hooks/{task_id}/wrong", content=b"paid")

    assert ok.status_code == 202
    assert bad.status_code == 404


def test_task_prompt_marks_event_data_as_data() -> None:
    prompt = task_prompt(Task(summary="s", instruction="Notify me."), "fa", "Ali joined")

    assert "Ali joined" in prompt and "not instructions" in prompt


# --- watchers ---------------------------------------------------------------------------------


def feed(*ids: str) -> bytes:
    items = "".join(
        f"<item><title>T{i}</title><link>https://n.test/{i}</link><guid>{i}</guid></item>"
        for i in ids
    )
    channel = f"<channel><title>N</title>{items}</channel>"
    return f'<?xml version="1.0"?><rss version="2.0">{channel}</rss>'.encode()


def http_serving(body: bytes) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    )


async def test_feed_watcher_baseline_then_new_items(public_dns) -> None:
    state: dict = {}

    assert await check_feed(http_serving(feed("1", "2")), "https://n.test/rss", state) == []
    fired = await check_feed(http_serving(feed("3", "1", "2")), "https://n.test/rss", state)

    assert len(fired) == 1 and "T3" in fired[0]
    assert await check_feed(http_serving(feed("3", "1", "2")), "https://n.test/rss", state) == []


async def test_page_watcher_fires_on_change(public_dns) -> None:
    page = (
        b"<html><body><article><p>Price list version one, unchanged today.</p>"
        b"</article></body></html>"
    )
    changed = page.replace(b"one", b"two")
    state: dict = {}

    assert await check_page(http_serving(page), "https://p.test", state) == []
    assert await check_page(http_serving(page), "https://p.test", state) == []
    [event] = await check_page(http_serving(changed), "https://p.test", state)

    assert "version one" in event and "version two" in event


async def test_price_watcher_fires_on_crossing_only(session, cipher, public_dns) -> None:
    bot = await make_bot(session, cipher, in_group=False)
    pricing = Pricing.load(PLANS_FILE)
    pricing.models["test"] = ModelPrice(input_per_mtok=1, output_per_mtok=1)
    page = http_serving(b"<html><body><p>USD rate today is shown here.</p></body></html>")
    config = {"url": "https://p.test", "what": "USD rate", "threshold": 100.0, "direction": "above"}
    state: dict = {}

    async def poll(value: float) -> list[str]:
        model = TestModel(custom_output_args={"value": value})
        runtime = SimpleNamespace(
            http=page, pricing=pricing, models=SimpleNamespace(for_tier=lambda t: model)
        )
        return await check_price(runtime, session, bot, config, state)

    assert await poll(90) == []  # below: nothing yet
    assert len(await poll(120)) == 1  # crossed above
    assert await poll(130) == []  # still above: no repeat
    assert await poll(80) == []  # back below
    assert len(await poll(101)) == 1  # crossed again
