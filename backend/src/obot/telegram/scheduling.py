"""People's own scheduled tasks, made in a private chat with the bot (SPEC 8.7).

The bot drafts a task with the schedule_task tool; the person confirms it with a
button. /tasks lists their tasks on this bot. Nobody else using the bot sees them.
"""

import logging
from datetime import UTC, datetime

from aiogram import Bot as Client
from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.access import can_use
from obot.bots.users import join_bot
from obot.chat.membership import list_memberships
from obot.db.models import Bot as BotRecord
from obot.db.models import Task, TaskStatus, User
from obot.i18n import lang_of, t
from obot.tasks.apply import PlanningFailed, draft_task
from obot.tasks.planner import KnownChat
from obot.tasks.schedule import zone
from obot.tasks.service import cancel_task, confirm_draft, set_paused, user_tasks
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="scheduling")

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


async def is_chat_admin(client: Client, chat_id: int, tg_user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, tg_user_id)
    except TelegramAPIError:
        return False
    return member.status in ADMIN_STATUSES


async def admin_chats(
    session: AsyncSession, client: Client, bot: BotRecord, tg_user_id: int
) -> list[KnownChat]:
    """The bot's groups and channels where the person is an admin: where they may post."""
    return [
        KnownChat(m.tg_chat_id, m.title, m.chat_type)
        for m in await list_memberships(session, bot.id)
        if await is_chat_admin(client, m.tg_chat_id, tg_user_id)
    ]


def next_run_text(task: Task) -> str:
    if task.next_run_at is None:
        return "—"
    return task.next_run_at.astimezone(zone(task.timezone)).strftime("%Y-%m-%d %H:%M")


def destination_text(lang: str, task: Task, user: User, chats: list[KnownChat]) -> str:
    if task.destination_chat_id == user.tg_user_id:
        return t(lang, "task.to_me")
    title = next((c.title for c in chats if c.id == task.destination_chat_id), None)
    return title or str(task.destination_chat_id)


class Scheduler:
    """The `schedule` callable for ToolServices of one private reply."""

    def __init__(
        self, runtime: Runtime, bot: BotRecord, user: User, client: Client, lang: str
    ) -> None:
        self._runtime = runtime
        self._bot_id = bot.id
        self._user_id = user.id
        self._client = client
        self._lang = lang

    async def __call__(self, request: str) -> str:
        runtime, lang = self._runtime, self._lang
        async with runtime.sessionmaker() as session:
            bot = await session.get(BotRecord, self._bot_id)
            user = await session.get(User, self._user_id)
            chats = await admin_chats(session, self._client, bot, user.tg_user_id)
            try:
                sync = await draft_task(
                    session,
                    runtime.pricing,
                    runtime.models.for_tier("cheap"),
                    bot,
                    user,
                    request,
                    chats,
                )
            except PlanningFailed:
                await session.commit()  # keep the metered planning call
                return "ERROR: could not understand the schedule; ask the person to rephrase it."
            await session.commit()
            if not sync.scheduled:
                reasons = "; ".join(r.reason for r in sync.rejected) or "unknown reason"
                return f"ERROR: the task cannot be scheduled: {reasons}."
            task = sync.scheduled[0]
            text = t(
                lang,
                "task.draft",
                summary=task.summary,
                when=next_run_text(task),
                where=destination_text(lang, task, user, chats),
            )
        buttons = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t(lang, "btn.task_confirm"), callback_data=f"task:ok:{task.id}"
                    ),
                    InlineKeyboardButton(
                        text=t(lang, "btn.task_cancel"), callback_data=f"task:no:{task.id}"
                    ),
                ]
            ]
        )
        await runtime.sender.send_text(
            self._client, user.tg_user_id, text, group=False, buttons=buttons
        )
        return (
            f"A draft was shown to the person with Confirm and Cancel buttons: {task.summary}, "
            f"next run {next_run_text(task)}. Tell them in one short sentence to confirm it."
        )


# --- /tasks and the task buttons --------------------------------------------------------------


def tasks_keyboard(lang: str, tasks: list[Task]) -> InlineKeyboardMarkup | None:
    rows = []
    for i, task in enumerate(tasks, 1):
        toggle = (
            InlineKeyboardButton(text=f"▶️ {i}", callback_data=f"task:resume:{task.id}")
            if task.status == TaskStatus.PAUSED
            else InlineKeyboardButton(text=f"⏸ {i}", callback_data=f"task:pause:{task.id}")
        )
        rows.append(
            [toggle, InlineKeyboardButton(text=f"🗑 {i}", callback_data=f"task:del:{task.id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def tasks_text(lang: str, tasks: list[Task]) -> str:
    if not tasks:
        return t(lang, "tasks.list_empty")
    lines = [t(lang, "tasks.list_header")]
    for i, task in enumerate(tasks, 1):
        state = (
            t(lang, "tasks.paused")
            if task.status == TaskStatus.PAUSED
            else t(lang, "tasks.next_run", when=next_run_text(task))
        )
        lines.append(f"{i}. {task.summary} ({state})")
    return "\n".join(lines)


@router.message(Command("tasks"), F.chat.type == ChatType.PRIVATE)
async def on_tasks(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    lang = lang_of(message.from_user.language_code)
    if not can_use(bot_record, message.from_user.id):
        await runtime.sender.send_text(
            message.bot, message.chat.id, t(lang, "bot.private"), group=False
        )
        return
    user, _ = await join_bot(
        session, bot_record, message.from_user.id, message.from_user.language_code
    )
    tasks = await user_tasks(session, user, bot_record)
    await runtime.sender.send_text(
        message.bot,
        message.chat.id,
        tasks_text(lang, tasks),
        group=False,
        buttons=tasks_keyboard(lang, tasks),
    )


@router.callback_query(F.data.startswith("task:"))
async def on_task_button(
    query: CallbackQuery, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    lang = lang_of(query.from_user.language_code)
    _, action, task_id = query.data.split(":", 2)
    task = await session.get(Task, int(task_id)) if task_id.isdigit() else None
    user = await session.get(User, task.user_id) if task else None
    if (
        task is None
        or task.bot_id != bot_record.id
        or user.tg_user_id != query.from_user.id
        or not can_use(bot_record, query.from_user.id)
    ):
        await query.answer(t(lang, "task.not_yours"), show_alert=True)
        return
    now = datetime.now(UTC)
    if action == "ok":
        problem = await confirm_draft(session, task, user, runtime.pricing.plan(user.plan), now)
        text = (
            t(lang, "task.cannot_confirm", reason=problem)
            if problem
            else t(lang, "task.confirmed", summary=task.summary, when=next_run_text(task))
        )
        await session.commit()
        await query.message.edit_text(text)
    elif action == "no":
        await cancel_task(session, task)
        await session.commit()
        await query.message.edit_text(t(lang, "task.cancelled"))
    elif action in ("pause", "resume", "del") and task.status in (
        TaskStatus.ACTIVE,
        TaskStatus.PAUSED,
    ):
        if action == "del":
            await cancel_task(session, task)
        else:
            await set_paused(session, task, action == "pause", now)
        await session.commit()
        tasks = await user_tasks(session, user, bot_record)
        await query.message.edit_text(
            tasks_text(lang, tasks), reply_markup=tasks_keyboard(lang, tasks)
        )
    await query.answer()
