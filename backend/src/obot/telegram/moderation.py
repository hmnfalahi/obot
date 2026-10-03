"""Abuse reports and admin commands (kill switch, bans), served by the factory bot."""

import html
import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject, Filter
from aiogram.methods import ReplaceManagedBotToken
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from obot.bots.service import get_or_create_user
from obot.db.models import Bot as BotRecord
from obot.db.models import BotStatus, Report, ReportStatus, User
from obot.i18n import lang_of, t
from obot.moderation.service import (
    ban_user,
    create_report,
    disable_bot,
    enable_bot,
    find_bot,
    open_reports,
    resolve_reports,
    unban_user,
)
from obot.notices import count_today
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="moderation")
router.message.filter(F.chat.type == ChatType.PRIVATE)


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery, runtime: Runtime) -> bool:
        return event.from_user.id in runtime.admins


# --- actions shared by commands and buttons ------------------------------------------------


async def kill(session: AsyncSession, runtime: Runtime, bot: BotRecord) -> None:
    async def replace_token(tg_bot_id: int) -> str:
        return await runtime.factory_bot(ReplaceManagedBotToken(user_id=tg_bot_id))

    await disable_bot(session, runtime.cipher, replace_token, bot)
    await runtime.stop_bot(bot.tg_bot_id)
    await resolve_reports(session, bot, ReportStatus.ACTIONED)
    await _tell_owner(runtime, bot.owner, "owner.bot_disabled", username=bot.username)
    log.warning("bot @%s (%s) disabled by an admin", bot.username, bot.id)


async def revive(session: AsyncSession, runtime: Runtime, bot: BotRecord) -> BotStatus:
    status = await enable_bot(session, bot, runtime.pricing.plan(bot.owner.plan).max_bots)
    if status == BotStatus.ACTIVE:
        await runtime.launch_bot(runtime.cipher.decrypt(bot.token_encrypted, str(bot.tg_bot_id)))
    await _tell_owner(runtime, bot.owner, "owner.bot_enabled", username=bot.username)
    return status


async def _tell_owner(runtime: Runtime, owner: User, key: str, **kwargs: object) -> None:
    try:
        await runtime.factory_bot.send_message(
            owner.tg_user_id, t(lang_of(owner.language), key, **kwargs)
        )
    except Exception:
        log.warning("could not notify owner %s", owner.id, exc_info=True)


async def file_report(
    session: AsyncSession,
    runtime: Runtime,
    bot: BotRecord,
    reporter_id: int,
    reporter_timezone: str,
    chat_id: int | None,
    reason: str,
) -> bool:
    """Store a report and alert the admins; False when the reporter hit the daily limit."""
    used = await count_today(
        runtime.redis, f"reports:{reporter_id}", datetime.now(UTC), reporter_timezone
    )
    if used > runtime.daily_reports:
        return False
    report = await create_report(session, bot, reporter_id, chat_id, reason)
    await session.commit()
    text = (
        f"🚩 Report #{report.id} about @{html.escape(bot.username)} "
        f"(owner {bot.owner.tg_user_id})\n\n{html.escape(report.reason)}"
    )
    buttons = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⛔ Disable bot", callback_data=f"mod:disable:{report.id}"
                ),
                InlineKeyboardButton(text="Dismiss", callback_data=f"mod:dismiss:{report.id}"),
            ]
        ]
    )
    for admin in runtime.admins:
        try:
            await runtime.factory_bot.send_message(
                admin, text, parse_mode=ParseMode.HTML, reply_markup=buttons
            )
        except Exception:
            log.warning("could not alert admin %s about report %s", admin, report.id, exc_info=True)
    return True


# --- anyone: reporting a bot from the factory ---------------------------------------------


@router.message(Command("report"))
async def on_factory_report(
    message: Message, command: CommandObject, session: AsyncSession, runtime: Runtime
) -> None:
    lang = lang_of(message.from_user.language_code)
    username, _, reason = (command.args or "").strip().partition(" ")
    if not username.startswith("@") or not reason.strip():
        await message.answer(t(lang, "report.factory_usage"))
        return
    bot = await find_bot(session, username)
    if bot is None:
        await message.answer(t(lang, "report.not_found"))
        return
    reporter = await get_or_create_user(
        session, message.from_user.id, message.from_user.language_code
    )
    ok = await file_report(
        session, runtime, bot, reporter.tg_user_id, reporter.timezone, None, reason
    )
    await message.answer(t(lang, "report.thanks" if ok else "report.limit"))


# --- admins ----------------------------------------------------------------------------------


@router.message(Command("disable"), IsAdmin())
async def on_disable(
    message: Message, command: CommandObject, session: AsyncSession, runtime: Runtime
) -> None:
    bot = await find_bot(session, command.args or "")
    if bot is None:
        await message.answer("No such bot. Usage: /disable @bot_username")
        return
    if bot.status == BotStatus.DISABLED:
        await message.answer(f"@{bot.username} is already disabled.")
        return
    await kill(session, runtime, bot)
    await message.answer(f"⛔ @{bot.username} disabled and its token revoked.")


@router.message(Command("enable"), IsAdmin())
async def on_enable(
    message: Message, command: CommandObject, session: AsyncSession, runtime: Runtime
) -> None:
    bot = await find_bot(session, command.args or "")
    if bot is None:
        await message.answer("No such bot. Usage: /enable @bot_username")
        return
    if bot.owner.is_disabled:
        await message.answer(f"The owner of @{bot.username} is banned; /unban them first.")
        return
    if bot.status != BotStatus.DISABLED:
        await message.answer(f"@{bot.username} is not disabled ({bot.status}).")
        return
    status = await revive(session, runtime, bot)
    await message.answer(f"✅ @{bot.username} is now {status}.")


async def _user_by_tg_id(session: AsyncSession, raw: str | None) -> User | None:
    try:
        tg_id = int((raw or "").strip())
    except ValueError:
        return None
    return await session.scalar(select(User).where(User.tg_user_id == tg_id))


@router.message(Command("ban"), IsAdmin())
async def on_ban(
    message: Message, command: CommandObject, session: AsyncSession, runtime: Runtime
) -> None:
    user = await _user_by_tg_id(session, command.args)
    if user is None:
        await message.answer("No such user. Usage: /ban <telegram user id>")
        return
    if user.tg_user_id in runtime.admins:
        await message.answer("Admins cannot be banned.")
        return

    async def replace_token(tg_bot_id: int) -> str:
        return await runtime.factory_bot(ReplaceManagedBotToken(user_id=tg_bot_id))

    killed = await ban_user(session, runtime.cipher, replace_token, user)
    for bot in killed:
        await runtime.stop_bot(bot.tg_bot_id)
        await resolve_reports(session, bot, ReportStatus.ACTIONED)
    await _tell_owner(runtime, user, "owner.banned")
    await message.answer(f"⛔ User {user.tg_user_id} banned; {len(killed)} bot(s) disabled.")


@router.message(Command("unban"), IsAdmin())
async def on_unban(message: Message, command: CommandObject, session: AsyncSession) -> None:
    user = await _user_by_tg_id(session, command.args)
    if user is None:
        await message.answer("No such user. Usage: /unban <telegram user id>")
        return
    await unban_user(session, user)
    await message.answer(
        f"User {user.tg_user_id} unbanned. Their bots stay disabled; use /enable for each."
    )


@router.message(Command("reports"), IsAdmin())
async def on_reports(message: Message, session: AsyncSession) -> None:
    reports = await open_reports(session)
    if not reports:
        await message.answer("No open reports.")
        return
    lines = []
    for report in reports:
        bot = await session.get(BotRecord, report.bot_id)
        lines.append(f"#{report.id} @{bot.username}: {report.reason[:120]}")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("mod:"), IsAdmin())
async def on_report_button(query: CallbackQuery, session: AsyncSession, runtime: Runtime) -> None:
    _, action, report_id = query.data.split(":")
    report = await session.get(Report, int(report_id))
    if report is None:
        await query.answer("Report not found")
        return
    bot = await session.get(BotRecord, report.bot_id, options=[joinedload(BotRecord.owner)])
    if action == "disable":
        if bot.status != BotStatus.DISABLED:
            await kill(session, runtime, bot)
        result = f"⛔ @{bot.username} disabled"
    else:
        report.status = ReportStatus.DISMISSED
        report.resolved_at = datetime.now(UTC)
        result = "Dismissed"
    await query.answer(result)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(f"Report #{report.id}: {result}")


async def report_from_bot(
    message: Message, session: AsyncSession, runtime: Runtime, bot: BotRecord, reason: str | None
) -> None:
    """/report sent inside a managed bot (private or group)."""
    lang = lang_of(message.from_user.language_code)
    reply = t(lang, "report.usage")
    if reason and reason.strip():
        reporter = await get_or_create_user(
            session, message.from_user.id, message.from_user.language_code
        )
        ok = await file_report(
            session, runtime, bot, reporter.tg_user_id, reporter.timezone, message.chat.id, reason
        )
        reply = t(lang, "report.thanks" if ok else "report.limit")
    await runtime.sender.send_text(
        message.bot,
        message.chat.id,
        reply,
        group=message.chat.type != ChatType.PRIVATE,
        reply_to=message.message_id if message.chat.type != ChatType.PRIVATE else None,
    )
