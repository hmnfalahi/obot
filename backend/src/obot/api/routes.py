"""Mini app API. Every route acts as the Telegram user from the request's initData."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import Date, case, cast, func, select

from obot.api.deps import CurrentUser, OwnedBot, OwnedTask, Session, State
from obot.api.schemas import (
    BotDetail,
    BotSummary,
    BotUpdate,
    ChatOut,
    MeOut,
    MeUpdate,
    NewBotIn,
    NewBotOut,
    Quota,
    RejectedOut,
    TaskAddOut,
    TaskOut,
    TaskRequest,
    TaskUpdate,
    UsageDay,
)
from obot.billing.usage import start_of_day
from obot.chat.membership import list_memberships
from obot.db.models import Bot, DraftState, Task, TaskRun, UsageEvent, UsageKind, User
from obot.factory.designer import BotDraft
from obot.factory.drafts import start_draft
from obot.factory.links import create_link
from obot.i18n import lang_of
from obot.tasks.apply import add_tasks
from obot.tasks.schedule import InvalidSchedule, zone
from obot.tasks.service import cancel_task, live_tasks, set_paused
from obot.telegram.profile import sync_profile

router = APIRouter(prefix="/api")

MAX_USAGE_DAYS = 31


# --- me ------------------------------------------------------------------------------------


async def _count_today(session: Session, user: User, kind: UsageKind) -> int:
    since = start_of_day(datetime.now(UTC), user.timezone)
    return await session.scalar(
        select(func.count()).where(
            UsageEvent.owner_id == user.id, UsageEvent.kind == kind, UsageEvent.created_at >= since
        )
    )


async def _me(session: Session, state: State, user: User) -> MeOut:
    plan = state.pricing.plan(user.plan)
    return MeOut(
        tg_user_id=user.tg_user_id,
        language=lang_of(user.language),
        timezone=user.timezone,
        plan=user.plan,
        credit_balance=user.credit_balance,
        max_bots=plan.max_bots,
        max_tasks=plan.max_scheduled_tasks,
        messages_today=Quota(
            used=await _count_today(session, user, UsageKind.LLM), limit=plan.daily_llm_messages
        ),
        searches_today=Quota(
            used=await _count_today(session, user, UsageKind.SEARCH),
            limit=plan.daily_web_searches,
        ),
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
    )


async def _task_out(session: Session, task: Task, owner_tg_id: int) -> TaskOut:
    last_error = await session.scalar(
        select(TaskRun.error).where(TaskRun.task_id == task.id).order_by(TaskRun.id.desc()).limit(1)
    )
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
    )


async def _detail(session: Session, bot: Bot) -> BotDetail:
    persona = bot.persona or {}
    owner_tg_id = bot.owner.tg_user_id
    return BotDetail(
        **_summary(bot).model_dump(),
        system_prompt=persona.get("system_prompt", ""),
        language=persona.get("language", "fa"),
        model_tier=bot.model_tier,
        daily_credit_cap=bot.daily_credit_cap,
        chats=[
            ChatOut(id=m.tg_chat_id, title=m.title, type=m.chat_type, is_admin=m.is_admin)
            for m in await list_memberships(session, bot.id)
        ],
        tasks=[await _task_out(session, t, owner_tg_id) for t in await live_tasks(session, bot)],
    )


@router.get("/bots")
async def list_bots(user: CurrentUser, session: Session) -> list[BotSummary]:
    bots = await session.scalars(select(Bot).where(Bot.owner_id == user.id).order_by(Bot.id))
    return [_summary(b) for b in bots]


@router.get("/bots/{bot_id}")
async def get_bot(bot: OwnedBot, session: Session) -> BotDetail:
    return await _detail(session, bot)


@router.patch("/bots/{bot_id}")
async def update_bot(body: BotUpdate, bot: OwnedBot, session: Session, state: State) -> BotDetail:
    persona = dict(bot.persona or {})
    profile_changed = False
    if body.display_name is not None and body.display_name != bot.display_name:
        bot.display_name = body.display_name
        profile_changed = True
    if body.description is not None and body.description != persona.get("description"):
        persona["description"] = body.description
        profile_changed = True
    if body.system_prompt is not None:
        persona["system_prompt"] = body.system_prompt
    if body.language is not None:
        persona["language"] = body.language
    bot.persona = persona
    if body.tools is not None:
        bot.tools = [t.value for t in body.tools]
    if body.clear_daily_credit_cap:
        bot.daily_credit_cap = None
    elif body.daily_credit_cap is not None:
        bot.daily_credit_cap = body.daily_credit_cap
    await session.flush()
    if profile_changed:
        token = state.cipher.decrypt(bot.token_encrypted, str(bot.tg_bot_id))
        draft = BotDraft(
            name=bot.display_name,
            username_hint=bot.username,
            description=persona.get("description", ""),
            system_prompt=persona.get("system_prompt", ""),
            language=persona.get("language", "fa"),
        )
        await sync_profile(token, draft, set_name=True)
    return await _detail(session, bot)


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
    return [UsageDay(day=d, messages=m, credits=c) for d, m, c in rows]


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


# --- tasks ------------------------------------------------------------------------------------


@router.post("/bots/{bot_id}/tasks")
async def add_task(body: TaskRequest, bot: OwnedBot, session: Session, state: State) -> TaskAddOut:
    sync = await add_tasks(
        session, state.pricing, state.models.for_tier("cheap"), bot, [body.request]
    )
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
