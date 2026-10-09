# P5-3: Personal Collections Made in Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a person say "track my weight too" in a bot's private chat. The bot proposes a personal collection (or new fields for one of theirs) with `data_define`. The person presses Save, and from then on the bot keeps records in it.

**Architecture:** A new toolbox switch `personal_data` (owner-chosen, like `memory`) turns on the data tools even when the bot has no collections and adds `data_define`. `DataService.define` builds the proposed schema and dry-runs it through the real repository calls (`create_collection` / `update_collection_schema`) inside a rolled-back session, so limits, keys and evolution are checked exactly once in `obot.data`. It then hands a `Definition` to a `propose_define` callable. The Telegram side stores the `Definition` in Redis for a day and shows a Save/Cancel card. The Save button applies it with `apply_definition`. The fix parked from P5-2 (catalog read in a savepoint) comes along in Task 1.

**Tech Stack:** Python 3.13, pydantic 2, pydantic-ai tools, SQLAlchemy 2 async, aiogram 3, Redis, React/TypeScript mini app (two-line change).

**Spec:** `docs/SPEC.md` section ۸٫۸, row P5-3: "ربات می‌تواند در لحظه برای خود کاربر مجموعه‌ی تازه بسازد یا فیلد اضافه کند… با تأیید کوتاه در چت. این مجموعه‌ها فقط مال همان کاربرند و قالب را تغییر نمی‌دهند." Builds on P5-1/P5-2 on branch `p5-data-core`.

## Global Constraints

- Collections made in chat are always PERSONAL, owned by the person (`created_by_user_id`), never change the bot's template, and count against that person's `max_data_collections`.
- Only additive changes to a person's own collection (new optional fields, new choice options). `obot.data` (`check_evolution`) decides. The bot's template collections can never be changed from chat.
- Nothing is created until the person presses Save. A proposal lives 24 hours. Only the person it was made for can press it.
- `data_define` exists only when the owner turned on `personal_data` for the bot, and only where data tools exist (never in anything that reaches a group).
- Tool failures return text starting with `ERROR:` and never raise into the agent run.
- Records and collections are read and written only through `obot.data`. Telegram code never queries `Record`/`RecordKey`.
- All outgoing Telegram messages go through `runtime.sender`. User-facing texts are in `obot/i18n.py` (fa/en).
- Commit messages: one plain English imperative sentence, ending with a `Co-Authored-By:` trailer. Never commit the user's untracked `backend/avatars.jsonl` or `backend/scripts/`.
- Dev infra must be up (`docker compose up -d`). Run backend tests from `backend/`.

## File Structure

| File | Responsibility |
|---|---|
| `backend/src/obot/factory/designer.py` (modify) | `ToolName.PERSONAL_DATA` + description |
| `backend/src/obot/i18n.py`, `miniapp/src/api.ts`, `miniapp/src/i18n.ts` (modify) | Toolbox label; proposal card texts |
| `backend/src/obot/core/tools.py` (modify) | `PERSONAL_DATA`, `FieldSpec`, `data_define`, `build_tools(define=...)`, `DataTools.define` |
| `backend/src/obot/data/format.py` (modify) | Catalog: empty-catalog header and the `data_define` hint |
| `backend/src/obot/data/service.py` (modify) | `note(..., can_define)` in a savepoint; `Definition`, `define`, `apply_definition` |
| `backend/src/obot/telegram/data_chat.py` (modify) | `DefineProposer`, `datadef:` button handler |
| `backend/src/obot/telegram/toolkit.py` (modify) | Enable data tools with the switch; wire the proposer |
| Tests: `backend/tests/test_data_format.py`, `test_data_service.py`, `test_data_chat.py` (modify) | |

---

### Task 1: The `personal_data` switch and a safe catalog read

**Files:**
- Modify: `backend/src/obot/factory/designer.py:20-32`, `backend/src/obot/i18n.py` (`tool.*` keys, both languages), `miniapp/src/api.ts:4-5`, `miniapp/src/i18n.ts` (`tool.*` keys, both languages)
- Modify: `backend/src/obot/core/tools.py` (`PERSONAL_DATA`, `build_tools`)
- Modify: `backend/src/obot/data/format.py` (`describe_collections`)
- Modify: `backend/src/obot/data/service.py` (`note`)
- Modify: `backend/src/obot/telegram/toolkit.py`
- Test: `backend/tests/test_data_format.py`, `backend/tests/test_data_service.py`, `backend/tests/test_data_chat.py`

**Interfaces:**
- Produces: `ToolName.PERSONAL_DATA == "personal_data"`; `obot.core.tools.PERSONAL_DATA = "personal_data"` (stripped by `build_tools` like the other implicit names); `describe_collections(collections, space, now, *, can_define: bool = False)`; `DataService.note(session, now=None, *, can_define: bool = False) -> str | None` (returns a note even with no collections when `can_define`); toolkit enables data tools when the note is non-empty.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_data_format.py`:

```python
def test_empty_catalog_invites_defining_a_collection() -> None:
    note = describe_collections([], space(user_id=2), NOW, can_define=True)
    assert "no data collections yet" in note
    assert "data_define" in note
    assert "Today is" in note
    # The hint is there with collections too, and absent without the switch.
    with_one = describe_collections(
        [collection("expenses", CollectionScope.PERSONAL, EXPENSES, {"en": "Expenses"})],
        space(user_id=2),
        NOW,
        can_define=True,
    )
    assert "data_define" in with_one
    assert "data_define" not in describe_collections(
        [collection("expenses", CollectionScope.PERSONAL, EXPENSES, {"en": "Expenses"})],
        space(user_id=2),
        NOW,
    )
```

Append to `backend/tests/test_data_service.py`:

```python
async def test_note_with_the_switch_and_no_collections(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    service = DataService(sessions, w.pricing, w.bot.id, w.alice.id)
    async with sessions() as s:
        assert await service.note(s) is None
        note = await service.note(s, can_define=True)
    assert "no data collections yet" in note


async def test_a_failing_catalog_leaves_the_callers_session_usable(
    data_world, monkeypatch
) -> None:
    from sqlalchemy import text

    from obot.data import service as service_module

    w = data_world

    async def broken(session, space):
        await session.execute(text("select * from no_such_table"))

    monkeypatch.setattr(service_module, "list_collections", broken)
    service = DataService(None, w.pricing, w.bot.id, w.alice.id)

    assert await service.note(w.session) is None
    # The failed statement was rolled back to a savepoint: the reply can go on.
    assert await w.session.scalar(text("select 1")) == 1
```

Append to `backend/tests/test_data_chat.py`:

```python
def test_personal_data_is_a_toolbox_switch_not_a_tool() -> None:
    from obot.core.tools import PERSONAL_DATA
    from obot.factory.designer import ToolName

    assert ToolName.PERSONAL_DATA.value == PERSONAL_DATA == "personal_data"
    assert [t.name for t in build_tools([PERSONAL_DATA])] == []


async def test_switch_gives_data_tools_without_collections(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    w.bot.tools = ["personal_data"]
    kit = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice)
    assert set(DATA_TOOLS) <= {t.name for t in kit.tools}
    assert "no data collections yet" in kit.note
    in_group = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice, data=False)
    assert not {t.name for t in in_group.tools} & set(DATA_TOOLS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_data_format.py tests/test_data_service.py tests/test_data_chat.py -q`
Expected: the new tests FAIL (`can_define` unexpected keyword, `PERSONAL_DATA` import error, `ToolName.PERSONAL_DATA` missing). The broken-catalog test fails with "current transaction is aborted" on `select 1`.

- [ ] **Step 3: Implement**

`backend/src/obot/factory/designer.py`: add to `ToolName` after `MEMORY = "memory"`:

```python
    PERSONAL_DATA = "personal_data"
```

and to `TOOL_DESCRIPTIONS`:

```python
    ToolName.PERSONAL_DATA: (
        "keep each user's own records they ask for in chat (expenses, weight, habits, lists)"
    ),
```

`backend/src/obot/i18n.py`: next to `"tool.memory"`, add `"tool.personal_data": "personal records",` in `"en"` and `"tool.personal_data": "داده‌ی شخصی",` in `"fa"`.

`miniapp/src/api.ts` lines 4-5 become:

```ts
export type ToolName = 'web_search' | 'read_url' | 'rss' | 'memory' | 'personal_data'
export const TOOLS: ToolName[] = ['web_search', 'read_url', 'rss', 'memory', 'personal_data']
```

`miniapp/src/i18n.ts`: next to each `'tool.memory'`, add `'tool.personal_data': 'Personal records (each user)',` (English) and `'tool.personal_data': 'داده‌ی شخصی هر کاربر',` (Persian).

`backend/src/obot/core/tools.py`: after `DATA_TOOLS = (...)` add

```python
# Toolbox switch (not a tool): users may define their own collections in chat (SPEC 8.8).
PERSONAL_DATA = "personal_data"
```

and in `build_tools` change `implicit = (DELEGATION_TOOL, SCHEDULE_TOOL, *DATA_TOOLS)` to `implicit = (DELEGATION_TOOL, SCHEDULE_TOOL, PERSONAL_DATA, *DATA_TOOLS)`.

`backend/src/obot/data/format.py`: add a constant after `DATA_RULES`:

```python
DEFINE_HINT = (
    "When the user wants you to keep track of something new for them (expenses, weight, "
    "habits, a list), propose a personal collection with data_define; it exists only after "
    "they press Save, so don't add records to it before that."
)
```

Change `describe_collections` to:

```python
def describe_collections(
    collections: list[Collection],
    space: DataSpace,
    now: datetime,
    *,
    can_define: bool = False,
) -> str:
    """The catalog that goes into the bot's instructions. `can_define`: the bot may propose
    personal collections (the personal_data switch)."""
    if collections:
        lines = ["Your data collections (use the data_* tools with these exact keys):"]
    else:
        lines = ["You have no data collections yet for this user."]
```

Keep the rest of the body as it is, and add before the final `return`:

```python
    if can_define:
        lines.append(DEFINE_HINT)
```

`backend/src/obot/data/service.py`: replace `note` with:

```python
    async def note(
        self, session: AsyncSession, now: datetime | None = None, *, can_define: bool = False
    ) -> str | None:
        """The catalog for the bot's instructions: None when there is nothing to say (no
        collections here and no personal_data switch) or the catalog cannot be read. It runs
        in a savepoint of the caller's session, so a failure never breaks the reply."""
        try:
            async with session.begin_nested():
                space = await self._space(session)
                collections = await list_collections(session, space)
            if not collections and not can_define:
                return None
            return describe_collections(
                collections, space, now or datetime.now(UTC), can_define=can_define
            )
        except Exception:
            log.exception("data catalog failed")
            return None
```

`backend/src/obot/telegram/toolkit.py`: import `PERSONAL_DATA` from `obot.core.tools`, and change `data_note = await service.note(session, now)` to:

```python
        data_note = await service.note(
            session, now, can_define=PERSONAL_DATA in (bot.tools or [])
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_data_format.py tests/test_data_service.py tests/test_data_chat.py tests/test_factory.py tests/test_api.py -q`
Expected: all PASS. The API accepts `personal_data` because `api/schemas.py` uses `list[ToolName]`.

- [ ] **Step 5: Check the mini app and lint, then commit**

Run: `cd miniapp && npm run build` (expected: builds without type errors), then `cd ../backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add backend/src/obot/factory/designer.py backend/src/obot/i18n.py backend/src/obot/core/tools.py backend/src/obot/data/format.py backend/src/obot/data/service.py backend/src/obot/telegram/toolkit.py miniapp/src/api.ts miniapp/src/i18n.ts backend/tests/test_data_format.py backend/tests/test_data_service.py backend/tests/test_data_chat.py
git commit -m "Add a personal-records switch that turns on data tools without collections

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 2: Proposing and applying a definition in `obot.data`

**Files:**
- Modify: `backend/src/obot/data/service.py`
- Test: `backend/tests/test_data_service.py`

**Interfaces:**
- Consumes: `create_collection`, `update_collection_schema`, `list_collections`, `collection_by_id`, `collection_schema` (repository); `CollectionSchema`, `KEY_PATTERN` (schema); `space_for`; errors; `obot.i18n.lang_of`.
- Produces:
  - `@dataclass(frozen=True) class Definition: bot_id: int; user_id: int; key: str; title: dict[str, str]; schema: dict[str, Any]; collection_id: int | None; added: list[str]` with `to_json() -> str` and `@classmethod from_json(raw: str | bytes) -> Definition`.
  - `ProposeDefine = Callable[[Definition], Awaitable[str]]`.
  - `DataService(..., propose_define: ProposeDefine | None = None)`.
  - `async DataService.define(collection: str, title: str, fields: list[dict[str, Any]]) -> str`.
  - `async apply_definition(session, pricing, d: Definition) -> Collection` (raises `DataError`; caller commits).

`fields` items are dicts with keys `key`, `label`, `type`, and optional `required`, `options`, `currency`, as the tool layer (Task 3) sends them.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_data_service.py`:

```python
from obot.data.service import Definition, apply_definition

WEIGHT_FIELDS = [
    {"key": "day", "label": "روز", "type": "date", "required": True},
    {"key": "kg", "label": "وزن", "type": "number", "required": True},
]


async def test_define_proposes_and_creates_nothing_until_applied(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="Shown with Save/Cancel.")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)

    told = await alice.define("weight", "وزن من", WEIGHT_FIELDS)

    assert told == "Shown with Save/Cancel."
    d: Definition = propose.await_args.args[0]
    assert (d.key, d.collection_id, d.added) == ("weight", None, ["day", "kg"])
    assert d.title == {"fa": "وزن من"}  # Alice speaks Persian
    assert d.schema["fields"][1]["label"] == {"fa": "وزن"}
    assert Definition.from_json(d.to_json()) == d
    async with sessions() as s:
        assert await alice.note(s) is None  # nothing created yet

        created = await apply_definition(s, w.pricing, d)
        await s.commit()
    assert created.created_by_user_id == w.alice.id
    assert (await alice.add("weight", {"day": "2026-10-09", "kg": "72.5"})).startswith("OK:")
    # Bob does not see Alice's collection.
    assert "no collection named 'weight'" in await DataService(
        sessions, w.pricing, w.bot.id, w.bob.id
    ).find("weight")


async def test_define_adds_fields_to_the_persons_own_collection(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="ok")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)
    await alice.define("weight", "Weight", WEIGHT_FIELDS)
    async with sessions() as s:
        await apply_definition(s, w.pricing, propose.await_args.args[0])
        await s.commit()
    await alice.add("weight", {"day": "2026-10-09", "kg": 72})

    await alice.define(
        "weight",
        "ignored",
        [*WEIGHT_FIELDS, {"key": "note", "label": "Note", "type": "text"}],
    )

    d = propose.await_args.args[0]
    assert d.collection_id is not None and d.added == ["note"]
    async with sessions() as s:
        await apply_definition(s, w.pricing, d)
        await s.commit()
    assert (await alice.add("weight", {"day": "2026-10-10", "kg": 71, "note": "gym"})).startswith(
        "OK:"
    )


async def test_define_refusals_are_error_text(data_world) -> None:
    w = data_world
    await create_collection(
        w.session, w.pricing, bot=w.bot, key="expenses", title={"en": "E"},
        scope=CollectionScope.PERSONAL, schema=EXPENSES,
    )
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="ok")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)

    cases = {
        "template": await alice.define("expenses", "E", WEIGHT_FIELDS),
        "bad type": await alice.define("w", "W", [{"key": "a", "label": "A", "type": "color"}]),
        "choice": await alice.define("w", "W", [{"key": "a", "label": "A", "type": "choice"}]),
        "bad key": await alice.define("My List", "W", WEIGHT_FIELDS),
        "no fields": await alice.define("w", "W", []),
        "not a list": await alice.define("w", "W", "day, kg"),
    }
    for name, text in cases.items():
        assert text.startswith("ERROR: "), (name, text)
    assert "bot's own collections" in cases["template"]
    propose.assert_not_awaited()
    assert (await DataService(sessions, w.pricing, w.bot.id, w.alice.id).define(
        "w", "W", WEIGHT_FIELDS
    )).startswith("ERROR: ")


async def test_define_respects_the_persons_collection_limit(data_world) -> None:
    w = data_world
    await w.session.commit()
    w.pricing.plan("free").max_data_collections = 1
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="ok")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)
    await alice.define("weight", "W", WEIGHT_FIELDS)
    async with sessions() as s:
        await apply_definition(s, w.pricing, propose.await_args.args[0])
        await s.commit()

    assert "limit" in await alice.define("sleep", "S", WEIGHT_FIELDS)


async def test_nothing_new_to_add_is_refused(data_world) -> None:
    w = data_world
    await w.session.commit()
    sessions = async_sessionmaker(w.session.bind, expire_on_commit=False)
    propose = AsyncMock(return_value="ok")
    alice = DataService(sessions, w.pricing, w.bot.id, w.alice.id, propose_define=propose)
    await alice.define("weight", "W", WEIGHT_FIELDS)
    async with sessions() as s:
        await apply_definition(s, w.pricing, propose.await_args.args[0])
        await s.commit()

    assert "nothing new" in await alice.define("weight", "W", WEIGHT_FIELDS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_data_service.py -q`
Expected: FAIL with `ImportError: cannot import name 'Definition'`.

- [ ] **Step 3: Implement**

In `backend/src/obot/data/service.py`:

- Add imports: `from dataclasses import asdict, dataclass`; `from pydantic import ValidationError`; `from obot.data.errors import InvalidSchema` (merge with the existing errors import); `from obot.data.repository import collection_by_id, collection_schema, create_collection, update_collection_schema` (merge); `from obot.data.schema import CollectionSchema` (merge if present); `from obot.i18n import lang_of`.
- Add the constructor parameter `propose_define: ProposeDefine | None = None` and store it as `self._propose_define`.
- Add, after the type aliases near the top:

```python
@dataclass(frozen=True)
class Definition:
    """A personal collection a person is asked to confirm: a new one (collection_id None) or
    new fields for one of theirs. Stored as JSON until they press Save."""

    bot_id: int
    user_id: int
    key: str
    title: dict[str, str]
    schema: dict[str, Any]  # CollectionSchema as JSON
    collection_id: int | None
    added: list[str]  # keys of new fields, or of choice fields that got new options

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Definition":
        return cls(**json.loads(raw))


ProposeDefine = Callable[[Definition], Awaitable[str]]

FIELD_SPEC_KEYS = {"key", "label", "type", "required", "options", "currency"}


def parse_field_specs(fields: Any, lang: str) -> list[dict[str, Any]]:
    """The model's field list as FieldDef JSON, labels in the person's language."""
    if not isinstance(fields, list) or not fields:
        raise InvalidSchema("fields must be a non-empty list of {key, label, type}")
    specs = []
    for item in fields:
        if not isinstance(item, dict) or not {"key", "label", "type"} <= item.keys():
            raise InvalidSchema("each field needs key, label and type")
        spec = {k: v for k, v in item.items() if k in FIELD_SPEC_KEYS and v is not None}
        spec["label"] = {lang: str(item["label"]).strip() or str(item["key"])}
        specs.append(spec)
    return specs


def _schema(data: dict[str, Any]) -> CollectionSchema:
    try:
        return CollectionSchema.model_validate(data)
    except ValidationError as e:
        reasons = "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        raise InvalidSchema(reasons) from None


def _merge(old: CollectionSchema, specs: list[dict[str, Any]]) -> tuple[dict, list[str]]:
    """Old fields plus new ones; choice fields gain new options. Returns (schema, added)."""
    merged = old.model_dump(mode="json")
    by_key = {f["key"]: f for f in merged["fields"]}
    added = []
    for spec in specs:
        current = by_key.get(spec["key"])
        if current is None:
            merged["fields"].append(spec)
            added.append(spec["key"])
            continue
        new_options = [o for o in spec.get("options", []) if o not in current["options"]]
        if current["type"] == "choice" and new_options:
            current["options"] += new_options
            added.append(spec["key"])
    return merged, added
```

- Add the methods to `DataService`:

```python
    async def _definition(
        self, session: AsyncSession, key: str, title: str, fields: Any
    ) -> Definition:
        bot = await session.get(Bot, self._bot_id)
        user = await session.get(User, self._user_id)
        if bot is None or user is None:
            raise CollectionNotFound(key)
        space = await space_for(session, bot, user)
        lang = lang_of(user.language)
        specs = parse_field_specs(fields, lang)
        key = str(key).strip()
        visible = await list_collections(session, space)
        own = next((c for c in visible if c.key == key and c.created_by_user_id == user.id), None)
        if own is None and any(c.key == key for c in visible):
            raise InvalidSchema(
                f"{key!r} is one of the bot's own collections; add records to it instead"
            )
        if own is not None:
            merged, added = _merge(collection_schema(own), specs)
            if not added:
                raise InvalidSchema(f"nothing new to add to {key!r}")
            schema = _schema(merged)
            # Dry run: the same checks Save will run (evolution, unique rules).
            await update_collection_schema(session, own, schema)
            return Definition(
                bot.id, user.id, key, dict(own.title), schema.model_dump(mode="json"), own.id,
                added,
            )
        schema = _schema({"fields": specs})
        title_text = str(title).strip() or key
        # Dry run: key format, clashes and the person's collection limit.
        await create_collection(
            session,
            self._pricing,
            bot=bot,
            key=key,
            title={lang: title_text},
            scope=CollectionScope.PERSONAL,
            schema=schema,
            created_by=user,
        )
        return Definition(
            bot.id, user.id, key, {lang: title_text}, schema.model_dump(mode="json"), None,
            [f.key for f in schema.fields],
        )

    async def define(self, collection: str, title: str, fields: list[dict[str, Any]]) -> str:
        """Propose a personal collection (or new fields for one of the person's own); nothing
        changes until they press Save (apply_definition)."""
        if self._propose_define is None:
            return "ERROR: new collections need the user's confirmation in their private chat."
        try:
            async with self._sessionmaker() as session:
                try:
                    definition = await self._definition(session, collection, title, fields)
                finally:
                    await session.rollback()  # the checks above are a dry run
        except DataError as e:
            return describe_error(e)
        except Exception:
            log.exception("data_define failed")
            return "ERROR: the collection could not be prepared; try again later."
        try:
            return await self._propose_define(definition)
        except Exception:
            log.exception("proposing a collection failed")
            return "ERROR: could not ask the user to confirm; try again later."
```

- Add the module-level function after the class:

```python
async def apply_definition(
    session: AsyncSession, pricing: Pricing, d: Definition
) -> Collection:
    """Create or extend the person's collection as proposed; the caller commits. All checks
    run again, so a proposal that went stale (limit reached, collection changed) fails."""
    bot = await session.get(Bot, d.bot_id)
    user = await session.get(User, d.user_id)
    if bot is None or user is None:
        raise CollectionNotFound(d.key)
    schema = _schema(d.schema)
    if d.collection_id is None:
        return await create_collection(
            session,
            pricing,
            bot=bot,
            key=d.key,
            title=d.title,
            scope=CollectionScope.PERSONAL,
            schema=schema,
            created_by=user,
        )
    space = await space_for(session, bot, user)
    c = await collection_by_id(session, space, d.collection_id)
    if c.created_by_user_id != user.id:
        raise NotAllowed("only the person's own collections can be changed in chat")
    await update_collection_schema(session, c, schema)
    return c
```

Check `CollectionNotFound` and `NotAllowed` are imported in service.py (they are used elsewhere in the module; add them to the errors import if not).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_data_service.py tests/test_data_records.py tests/test_data_collections.py -q`
Expected: all PASS. If `_schema` error texts carry pydantic's "Value error, " prefix differently in this pydantic version, keep the stripping minimal and check only `startswith("ERROR: ")` in tests (they already do).

- [ ] **Step 5: Lint and commit**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add backend/src/obot/data/service.py backend/tests/test_data_service.py
git commit -m "Let a person's collection be proposed in chat and applied on confirmation

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

### Task 3: The `data_define` tool and the Save/Cancel card

**Files:**
- Modify: `backend/src/obot/core/tools.py`
- Modify: `backend/src/obot/i18n.py`
- Modify: `backend/src/obot/telegram/data_chat.py`
- Modify: `backend/src/obot/telegram/toolkit.py`
- Test: `backend/tests/test_data_chat.py`

**Interfaces:**
- Consumes: Task 2 `Definition`, `DataService(propose_define=...)`, `apply_definition`; Task 1 `PERSONAL_DATA`.
- Produces: `FieldSpec` model, `DEFINE_TOOL = "data_define"`, `data_define` tool, `build_tools(..., define: bool = False)`, `DataTools.define`; `DefineProposer(runtime, bot, user)`; `on_define_button` handler for callback data `datadef:ok:<token>` / `datadef:no:<token>`; Redis key `data-define:<token>`, 24 h.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_data_chat.py`:

```python
from obot.core.tools import DEFINE_TOOL, data_define
from obot.telegram.data_chat import DefineProposer


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        return int(self.data.pop(key, None) is not None)


WEIGHT = [
    {"key": "day", "label": "Day", "type": "date", "required": True},
    {"key": "kg", "label": "Weight", "type": "number", "required": True},
]


def test_define_tool_only_with_the_switch() -> None:
    assert DEFINE_TOOL not in {t.name for t in build_tools([], data=True)}
    assert DEFINE_TOOL in {t.name for t in build_tools([], data=True, define=True)}


async def test_define_tool_needs_a_data_service() -> None:
    ctx = SimpleNamespace(deps=ToolServices(http=None, meter=None))
    assert (await data_define(ctx, "weight", "Weight", [])).startswith("ERROR:")


async def test_toolkit_offers_data_define_only_with_the_switch(data_world) -> None:
    w = data_world
    runtime = runtime_for(w)
    await expenses(w)
    plain = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice)
    w.bot.tools = ["personal_data"]
    switched = await toolkit(w.session, runtime, w.bot, datetime.now(UTC), w.alice)
    assert DEFINE_TOOL not in {t.name for t in plain.tools}
    assert DEFINE_TOOL in {t.name for t in switched.tools}


async def press_define(w, runtime, data: str, who: TgUser) -> AsyncMock:
    client = AsyncMock()
    message = Message(
        message_id=4, date=datetime.now(UTC), chat=Chat(id=who.id, type="private"), text="card"
    ).as_(client)
    query = CallbackQuery(
        id="q", from_user=who, chat_instance="c", data=data, message=message
    ).as_(client)
    await data_chat.on_define_button(query, w.session, w.bot, runtime)
    return client


async def test_save_creates_the_collection_only_for_its_person(data_world) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    runtime.redis = FakeRedis()
    await w.session.commit()
    service = DataService(
        runtime.sessionmaker, w.pricing, w.bot.id, w.alice.id,
        propose_define=DefineProposer(runtime, w.bot, w.alice),
    )

    told = await service.define("weight", "My weight", WEIGHT)

    assert "Save" in told
    call = runtime.sender.send_text.await_args
    assert call.args[1] == ALICE.id and "My weight" in call.args[2]
    save, cancel = (b.callback_data for b in call.kwargs["buttons"].inline_keyboard[0])
    assert save.startswith("datadef:ok:") and cancel.startswith("datadef:no:")

    stranger = await press_define(w, runtime, save, BOB)
    alert = stranger.await_args.args[0]
    assert isinstance(alert, AnswerCallbackQuery) and alert.show_alert
    assert "no collection named 'weight'" in await service.find("weight")

    await press_define(w, runtime, save, ALICE)
    assert await service.find("weight") == "No records."  # it exists now
    again = await press_define(w, runtime, save, ALICE)  # second press: expired, no crash
    assert again.await_count >= 1


async def test_cancel_drops_the_proposal(data_world) -> None:
    w = data_world
    w.bot.visibility = BotVisibility.PUBLIC
    runtime = runtime_for(w)
    runtime.redis = FakeRedis()
    await w.session.commit()
    service = DataService(
        runtime.sessionmaker, w.pricing, w.bot.id, w.alice.id,
        propose_define=DefineProposer(runtime, w.bot, w.alice),
    )
    await service.define("weight", "My weight", WEIGHT)
    save, cancel = (
        b.callback_data
        for b in runtime.sender.send_text.await_args.kwargs["buttons"].inline_keyboard[0]
    )

    await press_define(w, runtime, cancel, ALICE)
    await press_define(w, runtime, save, ALICE)

    assert runtime.redis.data == {}
    assert "no collection named 'weight'" in await service.find("weight")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_data_chat.py -q`
Expected: FAIL with `ImportError: cannot import name 'DEFINE_TOOL'`.

- [ ] **Step 3: The core tool**

In `backend/src/obot/core/tools.py`:

Add to the `DataTools` protocol:

```python
    async def define(
        self, collection: str, title: str, fields: list[dict[str, Any]]
    ) -> str: ...
```

After `DataFilter`, add:

```python
class FieldSpec(BaseModel):
    key: str  # lowercase latin slug, e.g. "kg"
    label: str  # in the user's language
    type: Literal[
        "text", "number", "money", "date", "time", "datetime", "choice", "boolean", "phone"
    ]
    required: bool = False
    options: list[str] = []  # choice only
    currency: str | None = None  # money only, e.g. "IRT"
```

After `data_summarize`, add:

```python
async def data_define(
    ctx: RunContext[ToolServices], collection: str, title: str, fields: list[FieldSpec]
) -> str:
    """Propose a new personal collection for this user, or new fields for one of theirs
    (same key). collection is a lowercase latin key like "weight", title is in their
    language. They confirm with a Save button; do not ask in words, and add records only
    after they saved it."""
    if ctx.deps.data is None:
        return NO_DATA
    return await ctx.deps.data.define(collection, title, [f.model_dump() for f in fields])
```

Add `DEFINE_TOOL = "data_define"` after `PERSONAL_DATA`, register `DEFINE_TOOL: Tool(data_define)` in `TOOLS`, add `DEFINE_TOOL` to the `implicit` tuple, and give `build_tools` a keyword `define: bool = False` with `names += [DEFINE_TOOL] if define else []` after the data line. Extend its docstring: "`define` (the personal_data switch) data_define."

- [ ] **Step 4: i18n**

`backend/src/obot/i18n.py`, `"en"`:

```python
        "data.define_new": "📋 New list «{title}» for you:\n{fields}",
        "data.define_add": "📋 Add to «{title}»:\n{fields}",
        "btn.data_save": "✅ Save",
        "btn.data_cancel": "❌ Cancel",
        "data.define_saved": "✅ Saved «{title}».",
        "data.define_cancelled": "Cancelled.",
        "data.define_expired": "This proposal has expired. Ask the bot again.",
        "data.define_failed": "⚠️ Could not save it: {reason}",
        "data.not_for_you": "This isn't for you.",
```

`"fa"`:

```python
        "data.define_new": "📋 فهرست تازه‌ی «{title}» برای تو:\n{fields}",
        "data.define_add": "📋 اضافه به «{title}»:\n{fields}",
        "btn.data_save": "✅ ذخیره",
        "btn.data_cancel": "❌ لغو",
        "data.define_saved": "✅ «{title}» ذخیره شد.",
        "data.define_cancelled": "لغو شد.",
        "data.define_expired": "این پیشنهاد منقضی شده. دوباره از ربات بخواه.",
        "data.define_failed": "⚠️ ذخیره نشد: {reason}",
        "data.not_for_you": "این مال تو نیست.",
```

- [ ] **Step 5: The Telegram side**

In `backend/src/obot/telegram/data_chat.py` add imports `import secrets`, `from obot.data.errors import DataError` (already there), `from obot.data.service import Definition, apply_definition`, `from obot.data.format import describe_error` and add:

```python
DEFINE_TTL = 24 * 3600

TYPE_NAMES = {
    "en": {
        "text": "text", "number": "number", "money": "amount", "date": "date",
        "time": "time", "datetime": "date and time", "choice": "choice",
        "boolean": "yes/no", "phone": "phone",
    },
    "fa": {
        "text": "متن", "number": "عدد", "money": "مبلغ", "date": "تاریخ",
        "time": "ساعت", "datetime": "تاریخ و ساعت", "choice": "انتخابی",
        "boolean": "بله/خیر", "phone": "تلفن",
    },
}


def definition_text(d: Definition, lang: str) -> str:
    fields = {f["key"]: f for f in d.schema["fields"]}
    lines = []
    for key in d.added:
        f = fields[key]
        label = f["label"].get(lang) or next(iter(f["label"].values()), key)
        kind = TYPE_NAMES[lang].get(f["type"], f["type"])
        if f.get("options"):
            kind += ": " + "، ".join(f["options"]) if lang == "fa" else ", ".join(f["options"])
        lines.append(f"• {label} ({kind})")
    title = d.title.get(lang) or next(iter(d.title.values()), d.key)
    template = "data.define_new" if d.collection_id is None else "data.define_add"
    return t(lang, template, title=title, fields="\n".join(lines))


class DefineProposer:
    """Shows a person a proposed collection with Save/Cancel; the proposal waits in Redis."""

    def __init__(self, runtime: Runtime, bot: BotRecord, user: User) -> None:
        self._runtime = runtime
        self._tg_bot_id = bot.tg_bot_id
        self._tg_user_id = user.tg_user_id
        self._lang = lang_of(user.language)

    async def __call__(self, d: Definition) -> str:
        runtime = self._runtime
        client = runtime.transport.client(self._tg_bot_id)
        if client is None:
            return "ERROR: cannot ask the user to confirm right now; try again later."
        token = secrets.token_urlsafe(9)
        await runtime.redis.set(f"data-define:{token}", d.to_json(), ex=DEFINE_TTL)
        lang = self._lang
        buttons = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t(lang, "btn.data_save"), callback_data=f"datadef:ok:{token}"
                    ),
                    InlineKeyboardButton(
                        text=t(lang, "btn.data_cancel"), callback_data=f"datadef:no:{token}"
                    ),
                ]
            ]
        )
        await runtime.sender.send_text(
            client, self._tg_user_id, definition_text(d, lang), group=False, buttons=buttons
        )
        return (
            f"The user was shown the proposed collection {d.key!r} with Save and Cancel "
            "buttons. Tell them in one short sentence to press Save; it does not exist yet."
        )


@router.callback_query(F.data.startswith("datadef:"))
async def on_define_button(
    query: CallbackQuery, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    lang = lang_of(query.from_user.language_code)
    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] not in ("ok", "no"):
        await query.answer()
        return
    _, action, token = parts
    key = f"data-define:{token}"
    raw = await runtime.redis.get(key)
    if raw is None:
        await query.answer()
        await _edit(query, t(lang, "data.define_expired"))
        return
    d = Definition.from_json(raw)
    user = await started_user(session, bot_record, query.from_user.id)
    if (
        user is None
        or user.id != d.user_id
        or d.bot_id != bot_record.id
        or not can_use(bot_record, query.from_user.id)
    ):
        await query.answer(t(lang, "data.not_for_you"), show_alert=True)
        return
    lang = lang_of(user.language)
    await runtime.redis.delete(key)
    if action == "no":
        await query.answer()
        await _edit(query, t(lang, "data.define_cancelled"))
        return
    try:
        c = await apply_definition(session, runtime.pricing, d)
    except DataError as e:
        await session.rollback()
        await query.answer()
        reason = describe_error(e).removeprefix("ERROR: ")
        await _edit(query, t(lang, "data.define_failed", reason=reason))
        return
    await session.commit()
    title = c.title.get(lang) or next(iter(c.title.values()), c.key)
    await query.answer()
    await _edit(query, t(lang, "data.define_saved", title=title))
```

`_edit(query, text)` is the existing helper in this module (it skips inaccessible messages and logs failed edits); reuse it. Fix the operator precedence in `definition_text`'s option join: write it as

```python
        if f.get("options"):
            separator = "، " if lang == "fa" else ", "
            kind += ": " + separator.join(f["options"])
```

- [ ] **Step 6: Wire the toolkit**

In `backend/src/obot/telegram/toolkit.py`: import `DEFINE_TOOL` is not needed; import `DefineProposer` from `obot.telegram.data_chat`. Compute `personal = PERSONAL_DATA in (bot.tools or [])` once, pass `propose_define=DefineProposer(runtime, bot, payer) if personal else None` to `DataService(...)`, pass `can_define=personal` to `note`, and add `define=personal and services.data is not None,` to the `build_tools(...)` call.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_data_chat.py tests/test_data_service.py -q`
Expected: all PASS.

- [ ] **Step 8: Full suite, lint, commit**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

```bash
git add backend/src/obot/core/tools.py backend/src/obot/i18n.py backend/src/obot/telegram/data_chat.py backend/src/obot/telegram/toolkit.py backend/tests/test_data_chat.py
git commit -m "Let people save a personal collection the bot proposes in chat

Co-Authored-By: <your model> <noreply@anthropic.com>"
```

---

## Spec coverage (P5-3)

| Spec ۸٫۸ requirement | Task |
|---|---|
| Toolbox switch «داده‌ی شخصی» | 1 |
| Bot creates a personal collection in the moment, with a short confirmation | 2, 3 |
| …or adds a field to one of the person's own | 2, 3 |
| Only theirs; the template never changes | 2 (`created_by`, template keys refused) |
| Additive changes only | 2 (`update_collection_schema` → `check_evolution`) |
| Limits on the person's plan | 2 (`create_collection` dry run and again on Save) |
| P5-2 parked: catalog read in a savepoint | 1 |
