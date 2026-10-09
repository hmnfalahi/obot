import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from obot.billing.plans import Pricing
from obot.bots.service import ManagedBotInfo, get_or_create_user, register_managed_bot
from obot.data import schema as schema_module
from obot.data.repository import add_record, template_collections
from obot.data.repository import collection_schema as schema_of
from obot.db.models import DraftKind, DraftState
from obot.factory.collections import sync_collections
from obot.factory.designer import (
    BotDraft,
    DesignerOutput,
    ExistingBot,
    ToolName,
    build_prompt,
    check_output,
    design,
    make_username,
)
from obot.factory.drafts import (
    apply_draft,
    existing_bot_drafts,
    get_active_draft,
    start_draft,
    take_awaiting_draft,
    update_draft,
)
from obot.i18n import MESSAGES
from obot.telegram import factory
from obot.telegram.factory import render_card
from tests.test_factory_collections import BOOKINGS

DRAFT = BotDraft(
    name="Dollar Watch",
    username_hint="dollar-watch",
    description="Sends the dollar price every morning.",
    system_prompt="You report the USD/IRR price briefly.",
    language="fa",
    tools=[ToolName.READ_URL],
)
PLANS = Pricing.load(Path(__file__).parent.parent / "plans.yaml")
DENTIST = DRAFT.model_copy(update={"name": "Dr. Sara", "collections": [BOOKINGS]})


async def make_bot(session, cipher, tg_bot_id: int = 7):
    async def fetch_token(_: int) -> str:
        return "7:token"

    result = await register_managed_bot(
        session, cipher, fetch_token, 1001, ManagedBotInfo(tg_bot_id, "dw_bot", "Dollar"), 10
    )
    return result.bot


def test_make_username_is_valid_telegram_username() -> None:
    for hint in ["dollar-watch", "Crypto Brief!", "123", "", "a" * 50]:
        username = make_username(hint)
        assert re.fullmatch(r"[a-z][a-z0-9_]{3,28}bot", username), username
        assert len(username) <= 32
    assert make_username("dollar_morning_bot").startswith("dollar_morning_")
    assert make_username("dollar_morning_bot").count("bot") == 1


def test_prompt_includes_context() -> None:
    prompt = build_prompt(
        "make it formal", DRAFT, [ExistingBot("dw_bot", DRAFT)], last_reply="Which site?"
    )

    assert "dw_bot" in prompt
    assert "Current draft" in prompt
    assert "Which site?" in prompt
    assert prompt.endswith("User message:\nmake it formal")


async def test_design_returns_structured_draft() -> None:
    model = TestModel(
        custom_output_args={
            "action": "draft",
            "draft": DRAFT.model_dump(mode="json"),
            "questions": [],
            "reply": "ساختمش!",
        }
    )

    result = await design(model, "قیمت دلار رو هر روز صبح بفرست")

    assert result.output.action == "draft"
    assert result.output.draft == DRAFT


async def test_new_draft_flow_applies_to_created_bot(session, cipher) -> None:
    owner = await get_or_create_user(session, 1001)
    row = await start_draft(session, owner, DRAFT, last_reply="ok")
    assert row.kind == DraftKind.NEW and row.suggested_username.endswith("_bot")

    formal = DRAFT.model_copy(update={"system_prompt": "Be formal."})
    await update_draft(session, row, formal, last_reply="done")
    row.state = DraftState.AWAITING_CREATION
    await session.flush()

    bot = await make_bot(session, cipher)
    taken = await take_awaiting_draft(session, owner)
    assert taken is row
    await apply_draft(session, taken, bot, PLANS)

    assert bot.persona["system_prompt"] == "Be formal."
    assert bot.tools == ["read_url"]
    assert row.state == DraftState.APPLIED and row.bot_id == bot.id
    assert await get_active_draft(session, owner) is None


async def test_starting_a_draft_cancels_the_previous_one(session) -> None:
    owner = await get_or_create_user(session, 1001)
    first = await start_draft(session, owner, DRAFT, last_reply=None)

    second = await start_draft(session, owner, DRAFT, last_reply=None)
    await session.refresh(first)

    assert first.state == DraftState.CANCELLED
    assert await get_active_draft(session, owner) is second


async def test_editing_a_draft_reopens_it(session) -> None:
    owner = await get_or_create_user(session, 1001)
    row = await start_draft(session, owner, DRAFT, last_reply=None)
    row.state = DraftState.AWAITING_CREATION

    await update_draft(session, row, DRAFT, last_reply=None)

    assert row.state == DraftState.OPEN
    assert await take_awaiting_draft(session, owner) is None


async def test_existing_bots_are_described_for_the_designer(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    owner = bot.owner
    row = await start_draft(session, owner, DRAFT, last_reply=None)
    await apply_draft(session, row, bot, PLANS)

    [existing] = await existing_bot_drafts(session, owner)

    assert existing.username == "dw_bot"
    assert existing.draft.system_prompt == DRAFT.system_prompt


async def test_edit_draft_targets_bot(session, cipher) -> None:
    bot = await make_bot(session, cipher)

    row = await start_draft(session, bot.owner, DRAFT, last_reply=None, target=bot)

    assert row.kind == DraftKind.EDIT
    assert row.bot_id == bot.id
    assert row.suggested_username is None


async def test_card_escapes_html(session) -> None:
    owner = await get_or_create_user(session, 1001)
    row = await start_draft(
        session, owner, DRAFT.model_copy(update={"name": "<b>x</b>"}), last_reply=None
    )

    card = render_card("en", row, target_username=None)

    assert "&lt;b&gt;x&lt;/b&gt;" in card
    assert "read web pages" in card


def test_check_output_asks_the_model_to_fix_invalid_collections() -> None:
    ok = DesignerOutput(action="draft", draft=DENTIST, reply="ok")
    check_output(ok)

    broken = BOOKINGS.model_copy(update={"unique": [["phone"]]})
    bad = DesignerOutput(
        action="draft", draft=DRAFT.model_copy(update={"collections": [broken]}), reply="ok"
    )
    with pytest.raises(ModelRetry) as caught:
        check_output(bad)
    assert "appointments" in str(caught.value)

    twice = DesignerOutput(
        action="draft",
        draft=DRAFT.model_copy(update={"collections": [BOOKINGS, BOOKINGS]}),
        reply="ok",
    )
    with pytest.raises(ModelRetry):
        check_output(twice)


async def test_design_accepts_collections() -> None:
    model = TestModel(
        custom_output_args={
            "action": "draft",
            "draft": DENTIST.model_dump(mode="json"),
            "questions": [],
            "reply": "ساختمش!",
        }
    )
    result = await design(model, "دندانپزشکم، بیمارها نوبت بگیرند")
    assert result.output.draft.collections[0].key == "appointments"


async def test_card_shows_the_data_section(session) -> None:
    owner = await get_or_create_user(session, 1001)
    row = await start_draft(session, owner, DENTIST, last_reply=None)

    card = render_card("fa", row, target_username=None)

    assert "📋" in card and "نوبت‌ها" in card
    assert "همه ثبت می‌کنند، فقط تو همه را می‌بینی" in card
    assert "هر تاریخ + ساعت فقط یک بار" in card


async def test_applying_a_draft_creates_its_collections(session, cipher) -> None:
    bot = await make_bot(session, cipher)
    row = await start_draft(session, bot.owner, DENTIST, last_reply=None)

    problems = await apply_draft(session, row, bot, PLANS)

    assert problems == []
    [c] = await template_collections(session, bot.id)
    assert c.key == "appointments"
    [existing] = await existing_bot_drafts(session, bot.owner)
    assert existing.draft.collections[0].key == "appointments"


def _reply_with(*drafts: BotDraft) -> FunctionModel:
    """A model that answers each request with the next draft (the last one repeats)."""
    calls = iter(range(100))

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        draft = drafts[min(next(calls), len(drafts) - 1)]
        args = {"action": "draft", "draft": draft.model_dump(mode="json"), "reply": "ok"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return FunctionModel(respond)


BROKEN = BOOKINGS.model_copy(update={"unique": [["phone"]]})


async def test_design_drops_collections_that_stay_invalid() -> None:
    bad = DRAFT.model_copy(
        update={"collections": [BROKEN, BOOKINGS.model_copy(update={"key": "b"})]}
    )

    result = await design(_reply_with(bad), "x")

    assert [c.key for c in result.output.draft.collections] == ["b"]
    assert result.dropped_collections == ["نوبت‌ها"]  # titles, not keys


async def test_design_keeps_the_fixed_collection_after_a_retry() -> None:
    bad = DRAFT.model_copy(update={"collections": [BROKEN]})

    result = await design(_reply_with(bad, DENTIST), "x")

    assert [c.key for c in result.output.draft.collections] == ["appointments"]
    assert result.dropped_collections == []


def test_duplicate_key_retry_names_the_key() -> None:
    twice = DesignerOutput(
        action="draft",
        draft=DRAFT.model_copy(update={"collections": [BOOKINGS, BOOKINGS]}),
        reply="ok",
    )
    with pytest.raises(ModelRetry, match="appointments"):
        check_output(twice)


async def test_card_survives_and_escapes_collections(session) -> None:
    evil = BOOKINGS.model_copy(
        update={
            "title": "<b>x</b>",
            "fields": [
                BOOKINGS.fields[0].model_copy(update={"label": "<i>n</i>"}),
                *BOOKINGS.fields[1:],
            ],
        }
    )
    broken = BROKEN.model_copy(update={"title": "<u>bad</u>"})
    owner = await get_or_create_user(session, 1001)
    row = await start_draft(
        session, owner, DRAFT.model_copy(update={"collections": [evil, broken]}), last_reply=None
    )

    card = render_card("en", row, target_username=None)

    assert "<b>x</b>" not in card and "&lt;b&gt;x&lt;/b&gt;" in card
    assert "<i>n</i>" not in card
    assert "&lt;u&gt;bad&lt;/u&gt;" in card and "needs fixing" in card


def test_the_model_cannot_see_or_set_the_dropped_note() -> None:
    assert "dropped_collections" not in DesignerOutput.model_json_schema()["properties"]
    assert "dropped_collections" not in DesignerOutput.model_fields


async def test_saving_a_change_that_breaks_records_tells_the_owner_in_persian(
    data_world, cipher, monkeypatch
) -> None:
    w = data_world
    await sync_collections(w.session, w.pricing, w.bot, [BOOKINGS], "fa")
    [c] = await template_collections(w.session, w.bot.id)
    await add_record(
        w.session,
        w.pricing,
        await w.space(w.alice),
        c,
        {"name": "Ali", "date": "2026-10-10", "time": "17:00"},
    )
    retyped = BOOKINGS.model_copy(
        update={
            "fields": [
                f.model_copy(update={"type": "text"}) if f.key == "date" else f
                for f in BOOKINGS.fields
            ],
            "allowed_hours": None,
        }
    )
    row = await start_draft(
        w.session,
        w.owner,
        DENTIST.model_copy(update={"collections": [retyped]}),
        last_reply=None,
        target=w.bot,
    )
    monkeypatch.setattr(factory, "sync_profile", AsyncMock())
    client = AsyncMock()
    owner = User(id=1001, is_bot=False, first_name="Owner", language_code="fa")
    message = Message(
        message_id=3, date=datetime.now(UTC), chat=Chat(id=1001, type="private"), text="card"
    ).as_(client)
    query = CallbackQuery(
        id="q", from_user=owner, chat_instance="c", data=f"draft:save:{row.id}", message=message
    ).as_(client)

    await factory.on_save(query, w.session, SimpleNamespace(pricing=w.pricing, cipher=cipher))

    sent = [m.text for (m,), _ in client.await_args_list if isinstance(m, SendMessage)]
    assert sent[-1] == (
        "⚠️ این فهرست‌ها تغییر نکردند:\n"
        "• نوبت‌ها: نوع «تاریخ» عوض نمی‌شود چون داده دارد؛ یک فیلد تازه اضافه کن"
    )
    assert schema_of(c).field("date").type == "date"


def test_every_refused_change_and_problem_has_words_in_both_languages() -> None:
    changes = re.findall(r'change="(\w+)"', Path(schema_module.__file__).read_text())
    assert len(set(changes)) == 9
    reasons = ["limit", "plan_limit", "kind_change", "too_many_fields", "invalid", "failed"]
    for lang in ("fa", "en"):
        for change in changes:
            assert f"factory.change.{change}" in MESSAGES[lang]
        for reason in reasons:
            assert f"factory.problem.{reason}" in MESSAGES[lang]
