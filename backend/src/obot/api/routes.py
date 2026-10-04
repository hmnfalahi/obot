"""Mini app API. Every route acts as the Telegram user from the request's initData."""

from datetime import UTC, datetime, timedelta

from aiogram import Bot as TelegramBot
from fastapi import APIRouter, HTTPException
from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.orm import joinedload

from obot.api.deps import ApiState, CurrentUser, OwnedApi, OwnedBot, OwnedTask, Session, State
from obot.api.schemas import (
    AllowedUserOut,
    ApiIn,
    ApiOut,
    BotDetail,
    BotSummary,
    BotUpdate,
    ByokIn,
    ByokOut,
    ChatOut,
    InvoiceIn,
    InvoiceOut,
    MeOut,
    MeUpdate,
    NewBotIn,
    NewBotOut,
    PackageOut,
    Quota,
    RejectedOut,
    StepOut,
    TaskAddOut,
    TaskOut,
    TaskRequest,
    TaskUpdate,
    UsageDay,
)
from obot.billing.usage import count_messages, start_of_day
from obot.bots.access import sync_access
from obot.bots.apis import ensure_room, list_endpoints, read_headers, write_endpoint
from obot.bots.byok import clear_key, key_hint, read_key, set_key, uses_byok
from obot.bots.users import bot_settings
from obot.chat.membership import list_memberships
from obot.core.byok import KeyCheckFailed
from obot.core.custom_api import PLACEHOLDER, InvalidEndpoint
from obot.core.fetch import FetchError, ensure_public_host
from obot.crypto import TokenCipher
from obot.db.models import (
    ApiEndpoint,
    Bot,
    BotChat,
    BotUser,
    BotVisibility,
    DraftState,
    Task,
    TaskRun,
    UsageEvent,
    UsageKind,
    User,
)
from obot.factory.designer import BotDraft
from obot.factory.drafts import start_draft
from obot.factory.links import create_link
from obot.i18n import lang_of
from obot.tasks.apply import PlanningFailed, add_tasks
from obot.tasks.schedule import InvalidSchedule, zone
from obot.tasks.service import cancel_task, live_tasks, set_paused
from obot.telegram.payments import invoice_args
from obot.telegram.profile import sync_profile

router = APIRouter(prefix="/api")

MAX_USAGE_DAYS = 31


# --- me ------------------------------------------------------------------------------------


async def _count_today(session: Session, user: User, kind: UsageKind) -> int:
    since = start_of_day(datetime.now(UTC), user.timezone)
    if kind == UsageKind.LLM:
        return await count_messages(session, user, since)
    return await session.scalar(
        select(func.count()).where(
            UsageEvent.payer_id == user.id, UsageEvent.kind == kind, UsageEvent.created_at >= since
        )
    )


async def _me(session: Session, state: State, user: User) -> MeOut:
    plan = state.pricing.plan(user.plan)
    return MeOut(
        tg_user_id=user.tg_user_id,
        model_tiers=plan.model_tiers,
        language=lang_of(user.language),
        timezone=user.timezone,
        plan=user.plan,
        credit_balance=user.credit_balance,
        max_bots=plan.max_bots,
        max_tasks=plan.max_scheduled_tasks,
        max_apis=plan.max_api_endpoints,
        messages_today=Quota(
            used=await _count_today(session, user, UsageKind.LLM), limit=plan.daily_llm_messages
        ),
        searches_today=Quota(
            used=await _count_today(session, user, UsageKind.SEARCH),
            limit=plan.daily_web_searches,
        ),
        factory_username=state.factory_username,
    )


@router.get("/me")
async def get_me(user: CurrentUser, session: Session, state: State) -> MeOut:
    return await _me(session, state, user)


@router.patch("/me")
async def update_me(body: MeUpdate, user: CurrentUser, session: Session, state: State) -> MeOut:
    if body.timezone is not None:
        try:
            zone(body.timezone)
        except InvalidSchedule as e:
            raise HTTPException(422, str(e)) from e
        user.timezone = body.timezone
    if body.language is not None:
        user.language = body.language
    await session.flush()
    return await _me(session, state, user)


# --- bots ------------------------------------------------------------------------------------


def _summary(bot: Bot) -> BotSummary:
    persona = bot.persona or {}
    return BotSummary(
        id=bot.id,
        username=bot.username,
        display_name=bot.display_name,
        status=bot.status,
        description=persona.get("description", ""),
        tools=bot.tools or [],
        owner_started=bot.owner_started,
        visibility=bot.visibility,
    )


async def _task_out(session: Session, task: Task, owner_tg_id: int) -> TaskOut:
    last_error = await session.scalar(
        select(TaskRun.error).where(TaskRun.task_id == task.id).order_by(TaskRun.id.desc()).limit(1)
    )
    peer_ids = {s["bot_id"] for s in task.steps or [] if s.get("bot_id")}
    usernames: dict[int, str] = {}
    if peer_ids:
        rows = await session.execute(select(Bot.id, Bot.username).where(Bot.id.in_(peer_ids)))
        usernames = dict(rows.all())
    return TaskOut(
        id=task.id,
        summary=task.summary,
        instruction=task.instruction,
        schedule_kind=task.schedule_kind,
        cron=task.cron,
        timezone=task.timezone,
        destination_chat_id=task.destination_chat_id,
        to_owner=task.destination_chat_id == owner_tg_id,
        status=task.status,
        next_run_at=task.next_run_at,
        last_run_at=task.last_run_at,
        last_error=last_error,
        steps=[
            StepOut(instruction=s["instruction"], bot_username=usernames.get(s.get("bot_id")))
            for s in task.steps or []
        ],
    )


def _byok(cipher: TokenCipher, bot: Bot) -> ByokOut | None:
    if not uses_byok(bot):
        return None
    return ByokOut(provider=bot.byok_provider, model=bot.byok_model, key_hint=key_hint(cipher, bot))


async def _detail(session: Session, state: ApiState, bot: Bot) -> BotDetail:
    persona = bot.persona or {}
    owner_tg_id = bot.owner.tg_user_id
    return BotDetail(
        **_summary(bot).model_dump(),
        system_prompt=persona.get("system_prompt", ""),
        language=persona.get("language", "fa"),
        model_tier=bot.model_tier,
        byok=_byok(state.cipher, bot),
        apis=[
            ApiOut(
                id=row.id,
                name=row.name,
                description=row.description,
                method=row.method,
                url=row.url,
                header_names=list(read_headers(state.cipher, row)),
            )
            for row in await list_endpoints(session, bot)
        ],
        # The owner's own cap: everyone else sets theirs (SPEC 8.7).
        daily_credit_cap=(await bot_settings(session, bot.owner, bot)).daily_credit_cap,
        allowed_users=[AllowedUserOut(**u) for u in bot.allowed_users or []],
        users_count=await session.scalar(
            select(func.count()).where(BotUser.bot_id == bot.id, BotUser.left_at.is_(None))
        ),
        groups_count=await session.scalar(
            select(func.count()).where(
                BotChat.bot_id == bot.id,
                BotChat.is_member.is_(True),
                BotChat.chat_type.in_(("group", "supergroup")),
            )
        ),
        chats=[
            ChatOut(id=m.tg_chat_id, title=m.title, type=m.chat_type, is_admin=m.is_admin)
            for m in await list_memberships(session, bot.id)
        ],
        tasks=[await _task_out(session, t, owner_tg_id) for t in await live_tasks(session, bot)],
    )


async def _sync_access(state: ApiState, bot: Bot) -> bool:
    factory = TelegramBot(state.factory_token)
    try:
        return await sync_access(factory, bot)
    finally:
        await factory.session.close()


@router.get("/bots")
async def list_bots(user: CurrentUser, session: Session) -> list[BotSummary]:
    bots = await session.scalars(select(Bot).where(Bot.owner_id == user.id).order_by(Bot.id))
    return [_summary(b) for b in bots]


@router.get("/bots/{bot_id}")
async def get_bot(bot: OwnedBot, session: Session, state: State) -> BotDetail:
    return await _detail(session, state, bot)


@router.patch("/bots/{bot_id}")
async def update_bot(body: BotUpdate, bot: OwnedBot, session: Session, state: State) -> BotDetail:
    persona = dict(bot.persona or {})
    name_changed = description_changed = False
    if body.display_name is not None and body.display_name != bot.display_name:
        bot.display_name = body.display_name
        name_changed = True
    if body.description is not None and body.description != persona.get("description"):
        persona["description"] = body.description
        description_changed = True
    if body.system_prompt is not None:
        persona["system_prompt"] = body.system_prompt
    if body.language is not None:
        persona["language"] = body.language
    bot.persona = persona
    if body.tools is not None:
        bot.tools = [t.value for t in body.tools]
    if body.model_tier is not None:
        if body.model_tier not in state.pricing.plan(bot.owner.plan).model_tiers:
            raise HTTPException(403, "this model needs the paid plan")
        bot.model_tier = body.model_tier
    if body.clear_daily_credit_cap:
        (await bot_settings(session, bot.owner, bot)).daily_credit_cap = None
    elif body.daily_credit_cap is not None:
        (await bot_settings(session, bot.owner, bot)).daily_credit_cap = body.daily_credit_cap
    access_changed = False
    if body.visibility is not None and body.visibility != bot.visibility:
        bot.visibility = BotVisibility(body.visibility)
        access_changed = True
    if body.allowed_user_ids is not None:
        keep = set(body.allowed_user_ids)
        kept = [u for u in bot.allowed_users or [] if u["id"] in keep]
        access_changed |= kept != (bot.allowed_users or [])
        bot.allowed_users = kept
    await session.flush()
    if access_changed and not await _sync_access(state, bot):
        # Not committed: what we store must match what Telegram enforces.
        raise HTTPException(502, "Telegram did not accept the access change; try again")
    if name_changed or description_changed:
        token = state.cipher.decrypt(bot.token_encrypted, str(bot.tg_bot_id))
        draft = BotDraft(
            name=bot.display_name,
            username_hint=bot.username,
            description=persona.get("description", ""),
            system_prompt=persona.get("system_prompt", ""),
            language=persona.get("language", "fa"),
        )
        await sync_profile(token, draft, set_name=name_changed, set_description=description_changed)
    return await _detail(session, state, bot)


@router.put("/bots/{bot_id}/byok")
async def set_byok(body: ByokIn, bot: OwnedBot, session: Session, state: State) -> BotDetail:
    """Store the bot's own provider key after checking that it works with the model.

    A failed check answers 422 with the reason (auth, quota, model, provider) as detail.
    """
    api_key = body.api_key
    if api_key is None:
        if not uses_byok(bot) or bot.byok_provider != body.provider:
            raise HTTPException(422, "api_key is required")
        api_key = read_key(state.cipher, bot)
    api_key = api_key.strip()
    try:
        await state.models.byok.check(body.provider, body.model, api_key)
    except KeyCheckFailed as e:
        raise HTTPException(422, e.failure.value) from e
    set_key(state.cipher, bot, body.provider, body.model, api_key)
    await session.flush()
    return await _detail(session, state, bot)


@router.delete("/bots/{bot_id}/byok")
async def delete_byok(bot: OwnedBot, session: Session, state: State) -> BotDetail:
    clear_key(bot)
    await session.flush()
    return await _detail(session, state, bot)


@router.get("/bots/{bot_id}/usage")
async def bot_usage(bot: OwnedBot, session: Session, days: int = 7) -> list[UsageDay]:
    days = max(1, min(days, MAX_USAGE_DAYS))
    timezone = bot.owner.timezone
    since = start_of_day(datetime.now(UTC), timezone) - timedelta(days=days - 1)
    local_day = cast(func.timezone(timezone, UsageEvent.created_at), Date)
    rows = await session.execute(
        select(
            local_day,
            func.sum(case((UsageEvent.kind == UsageKind.LLM, 1), else_=0)),
            func.sum(UsageEvent.credits),
        )
        .where(UsageEvent.bot_id == bot.id, UsageEvent.created_at >= since)
        .group_by(local_day)
        .order_by(local_day)
    )
    by_day = {d: (m, c) for d, m, c in rows}
    first = since.date()
    # Every day of the range, oldest first, so clients need no date arithmetic.
    return [
        UsageDay(day=day, messages=by_day.get(day, (0, 0))[0], credits=by_day.get(day, (0, 0))[1])
        for day in (first + timedelta(days=i) for i in range(days))
    ]


@router.post("/bots/drafts")
async def new_bot(body: NewBotIn, user: CurrentUser, session: Session, state: State) -> NewBotOut:
    """Prepare a bot from the panel; Telegram's dialog (via the link) creates it."""
    draft = BotDraft(
        name=body.name,
        username_hint=body.name if body.name.isascii() else "obot",
        description=body.description,
        system_prompt=body.system_prompt,
        language=body.language,
        tools=body.tools,
    )
    row = await start_draft(session, user, draft, last_reply=None)
    row.state = DraftState.AWAITING_CREATION
    await session.flush()
    return NewBotOut(
        create_link=create_link(state.factory_username, row.suggested_username, body.name),
        suggested_username=row.suggested_username,
    )


# --- custom APIs (call_api tool) ----------------------------------------------------------


async def _save_api(session: Session, state: ApiState, row: ApiEndpoint, body: ApiIn) -> None:
    name = body.name.strip().lower()
    clash = await session.scalar(
        select(ApiEndpoint.id).where(
            ApiEndpoint.bot_id == row.bot_id, ApiEndpoint.name == name, ApiEndpoint.id != row.id
        )
    )
    if clash is not None:
        raise HTTPException(409, f"there is already an API named {name}")
    url = body.url.strip()
    try:
        write_endpoint(
            state.cipher,
            row,
            name=name,
            description=body.description.strip(),
            method=body.method,
            url=url,
            headers={k.strip(): v.strip() for k, v in body.headers.items()}
            if body.headers is not None
            else None,
        )
        # Fail early on private or unknown hosts; calls check again every time.
        await ensure_public_host(PLACEHOLDER.sub("x", url))
    except (InvalidEndpoint, FetchError) as e:
        raise HTTPException(422, str(e)) from e
    await session.flush()


@router.post("/bots/{bot_id}/apis")
async def add_api(body: ApiIn, bot: OwnedBot, session: Session, state: State) -> BotDetail:
    try:
        await ensure_room(session, bot, state.pricing.plan(bot.owner.plan).max_api_endpoints)
    except InvalidEndpoint as e:
        raise HTTPException(403, str(e)) from e
    row = ApiEndpoint(bot_id=bot.id)
    await _save_api(session, state, row, body)
    session.add(row)
    await session.flush()
    return await _detail(session, state, bot)


@router.put("/apis/{api_id}")
async def update_api(body: ApiIn, row: OwnedApi, session: Session, state: State) -> BotDetail:
    await _save_api(session, state, row, body)
    bot = await session.get(Bot, row.bot_id, options=[joinedload(Bot.owner)])
    return await _detail(session, state, bot)


@router.delete("/apis/{api_id}")
async def delete_api(row: OwnedApi, session: Session, state: State) -> BotDetail:
    bot = await session.get(Bot, row.bot_id, options=[joinedload(Bot.owner)])
    await session.delete(row)
    await session.flush()
    return await _detail(session, state, bot)


# --- billing ------------------------------------------------------------------------------


@router.get("/billing/packages")
async def packages(state: State, _: CurrentUser) -> list[PackageOut]:
    return [PackageOut(**p.model_dump()) for p in state.pricing.star_packages]


@router.post("/billing/invoice")
async def create_invoice(body: InvoiceIn, user: CurrentUser, state: State) -> InvoiceOut:
    """Invoice link for Telegram.WebApp.openInvoice; payment lands in the factory bot."""
    package = state.pricing.package(body.package_id)
    if package is None:
        raise HTTPException(404, "unknown package")
    bot = TelegramBot(state.factory_token)
    try:
        link = await bot.create_invoice_link(**invoice_args(lang_of(user.language), package))
    finally:
        await bot.session.close()
    return InvoiceOut(link=link)


# --- tasks ------------------------------------------------------------------------------------


@router.post("/bots/{bot_id}/tasks")
async def add_task(body: TaskRequest, bot: OwnedBot, session: Session, state: State) -> TaskAddOut:
    try:
        sync = await add_tasks(
            session,
            state.pricing,
            state.models.for_tier("cheap"),
            bot,
            [body.request],
        )
    except PlanningFailed as e:
        raise HTTPException(502, "could not understand the task; please rephrase") from e
    owner_tg_id = bot.owner.tg_user_id
    return TaskAddOut(
        scheduled=[await _task_out(session, t, owner_tg_id) for t in sync.scheduled],
        rejected=[RejectedOut(summary=r.summary, reason=r.reason) for r in sync.rejected],
    )


@router.patch("/tasks/{task_id}")
async def update_task(body: TaskUpdate, task: OwnedTask, session: Session) -> TaskOut:
    await set_paused(session, task, body.paused, datetime.now(UTC))
    bot = await session.get(Bot, task.bot_id)
    owner = await session.get(User, bot.owner_id)
    return await _task_out(session, task, owner.tg_user_id)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task: OwnedTask, session: Session) -> None:
    await cancel_task(session, task)
