import re

from pydantic_ai.models.test import TestModel

from obot.bots.service import ManagedBotInfo, get_or_create_user, register_managed_bot
from obot.db.models import DraftKind, DraftState
from obot.factory.designer import (
    BotDraft,
    ExistingBot,
    ToolName,
    build_prompt,
    design,
    make_username,
)
from obot.factory.drafts import (
    apply_draft,
    existing_bot_drafts,
    get_active_draft,
    requested_tasks,
    start_draft,
    take_awaiting_draft,
    update_draft,
)
from obot.telegram.factory import render_card

DRAFT = BotDraft(
    name="Dollar Watch",
    username_hint="dollar-watch",
    description="Sends the dollar price every morning.",
    system_prompt="You report the USD/IRR price briefly.",
    language="fa",
    tools=[ToolName.READ_URL],
    tasks=["Every day at 08:00 Asia/Tehran, read tgju.org and send the dollar price to the owner."],
)


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
    await apply_draft(session, taken, bot)

    assert bot.persona["system_prompt"] == "Be formal."
    assert bot.tools == ["read_url"]
    assert row.state == DraftState.APPLIED and row.bot_id == bot.id
    assert await get_active_draft(session, owner) is None
    assert await requested_tasks(session, bot) == DRAFT.tasks


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
    await apply_draft(session, row, bot)

    [existing] = await existing_bot_drafts(session, owner)

    assert existing.username == "dw_bot"
    assert existing.draft.system_prompt == DRAFT.system_prompt
    assert existing.draft.tasks == DRAFT.tasks


async def test_edit_draft_targets_bot(session, cipher) -> None:
    bot = await make_bot(session, cipher)

    row = await start_draft(session, bot.owner, DRAFT, last_reply=None, target=bot)

    assert row.kind == DraftKind.EDIT
    assert row.bot_id == bot.id
    assert row.suggested_username is None


async def test_card_escapes_html_and_lists_tasks(session) -> None:
    owner = await get_or_create_user(session, 1001)
    row = await start_draft(
        session, owner, DRAFT.model_copy(update={"name": "<b>x</b>"}), last_reply=None
    )

    card = render_card("en", row, target_username=None)

    assert "&lt;b&gt;x&lt;/b&gt;" in card
    assert "read web pages" in card
    assert "tgju.org" in card
