"""Handlers of the factory bot (@ObotFactory): design, create and edit bots by chat."""

import html
import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.methods import GetManagedBotToken
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestManagedBot,
    KeyboardButtonRequestUsers,
    ManagedBotUpdated,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.access import MAX_ALLOWED_USERS, sync_access
from obot.bots.service import (
    ManagedBotInfo,
    RegisterOutcome,
    count_active_bots,
    get_or_create_user,
    register_managed_bot,
)
from obot.data.describe import collection_text
from obot.data.errors import InvalidSchema
from obot.db.models import Bot as BotRecord
from obot.db.models import (
    BotStatus,
    BotVisibility,
    CollectionScope,
    DraftKind,
    DraftState,
    FactoryDraft,
    User,
)
from obot.factory.collections import Problem, to_schema
from obot.factory.designer import BotDraft, design
from obot.factory.drafts import (
    apply_draft,
    draft_of,
    existing_bot_drafts,
    get_active_draft,
    start_draft,
    take_awaiting_draft,
    update_draft,
)
from obot.factory.links import create_link
from obot.i18n import lang_of, t
from obot.notices import count_today
from obot.telegram.avatars import offer_keyboard
from obot.telegram.profile import sync_profile
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="factory")
router.message.filter(F.chat.type == ChatType.PRIVATE)

# Signed 32-bit id echoed back by Telegram; only one create button exists at a time.
CREATE_REQUEST_ID = 1


# --- rendering -------------------------------------------------------------------------


def render_card(lang: str, row: FactoryDraft, target_username: str | None) -> str:
    draft = draft_of(row)
    title = (
        t(lang, "card.edit", username=target_username) if target_username else t(lang, "card.new")
    )
    lines = [f"<b>{title}</b>", "", f"🤖 <b>{html.escape(draft.name)}</b>"]
    if draft.description:
        lines.append(html.escape(draft.description))
    if draft.tools:
        tools = ", ".join(t(lang, f"tool.{tool}") for tool in draft.tools)
        lines += ["", f"🧰 {t(lang, 'card.tools')}: {tools}"]
    if draft.collections:
        lines += ["", f"📋 {t(lang, 'card.data')}:"]
        for c in draft.collections:
            try:
                schema = to_schema(c, draft.language)
            except InvalidSchema:
                # The designer's validator makes this rare; never break the card.
                lines.append(f"• {html.escape(t(lang, 'card.fixing', title=c.title))}")
                continue
            lines.append(
                html.escape(collection_text(c.title, CollectionScope(c.scope), schema, lang))
            )
    lines += ["", f"<i>{t(lang, 'card.hint')}</i>"]
    return "\n".join(lines)


def _data_problems(lang: str, problems: list[Problem]) -> str:
    listed = "\n".join(
        f"• {html.escape(p.title)}: "
        + html.escape(t(lang, f"factory.problem.{p.reason}", detail=p.detail or ""))
        for p in problems
    )
    return t(lang, "factory.data_problems", problems=listed)


def panel_button(lang: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=t(lang, "btn.panel"), web_app=WebAppInfo(url=url))


def card_keyboard(
    lang: str, row: FactoryDraft, panel_url: str | None = None
) -> InlineKeyboardMarkup:
    primary = (
        InlineKeyboardButton(text=t(lang, "btn.save"), callback_data=f"draft:save:{row.id}")
        if row.kind == DraftKind.EDIT
        else InlineKeyboardButton(
            text=t(lang, "btn.create"), callback_data=f"draft:create:{row.id}"
        )
    )
    cancel = InlineKeyboardButton(
        text=t(lang, "btn.cancel"), callback_data=f"draft:cancel:{row.id}"
    )
    rows = [[primary, cancel]]
    if panel_url:
        rows.append([panel_button(lang, panel_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- commands --------------------------------------------------------------------------


@router.message(CommandStart())
async def on_start(
    message: Message, command: CommandObject, session: AsyncSession, runtime: Runtime
) -> None:
    user = message.from_user
    owner = await get_or_create_user(session, user.id, user.language_code)
    lang = lang_of(user.language_code)
    # The panel sends owners here to choose who may use a private bot: start=access_<bot id>
    if (command.args or "").startswith("access_"):
        await ask_for_people(message, session, owner, lang, command.args.removeprefix("access_"))
        return
    # Bots send people here for their own settings of that bot: start=mine_<bot id>
    bot_id = (command.args or "").removeprefix("mine_")
    if command.args and command.args.startswith("mine_") and bot_id.isdigit():
        url = runtime.settings_url(int(bot_id))
        if url:
            await message.answer(
                t(lang, "factory.my_settings"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[panel_button(lang, url)]]),
            )
            return
    await message.answer(t(lang, "factory.welcome"))


async def _owned_bot(session: AsyncSession, owner: User, bot_id: int) -> BotRecord | None:
    record = await session.get(BotRecord, bot_id)
    return record if record is not None and record.owner_id == owner.id else None


async def ask_for_people(
    message: Message, session: AsyncSession, owner: User, lang: str, bot_id: str
) -> None:
    record = await _owned_bot(session, owner, int(bot_id)) if bot_id.isdigit() else None
    if record is None:
        await message.answer(t(lang, "access.unknown_bot"))
        return
    picker = KeyboardButton(
        text=t(lang, "btn.pick_people"),
        request_users=KeyboardButtonRequestUsers(
            request_id=record.id,
            user_is_bot=False,
            max_quantity=MAX_ALLOWED_USERS,
            request_name=True,
        ),
    )
    await message.answer(
        t(lang, "access.pick", limit=MAX_ALLOWED_USERS, username=record.username),
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[picker]], resize_keyboard=True, one_time_keyboard=True
        ),
    )


@router.message(F.users_shared)
async def on_people_shared(message: Message, bot: Bot, session: AsyncSession) -> None:
    """The owner chose who may use one of their private bots (replaces the list)."""
    lang = lang_of(message.from_user.language_code)
    owner = await get_or_create_user(session, message.from_user.id)
    shared = message.users_shared
    record = await _owned_bot(session, owner, shared.request_id)
    if record is None:
        await message.answer(t(lang, "access.unknown_bot"), reply_markup=ReplyKeyboardRemove())
        return
    record.allowed_users = [
        {"id": u.user_id, "name": " ".join(filter(None, (u.first_name, u.last_name))) or "?"}
        for u in shared.users[:MAX_ALLOWED_USERS]
        if u.user_id != message.from_user.id
    ]
    await session.flush()
    synced = record.visibility != BotVisibility.PRIVATE or await sync_access(bot, record)
    names = ", ".join(u["name"] for u in record.allowed_users) or "—"
    text = t(lang, "access.saved", username=record.username, names=names)
    if not synced:
        text += "\n\n" + t(lang, "access.not_synced")
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


@router.message(Command("mybots"))
async def on_mybots(message: Message, session: AsyncSession, runtime: Runtime) -> None:
    lang = lang_of(message.from_user.language_code)
    owner = await get_or_create_user(session, message.from_user.id)
    bots = list(
        await session.scalars(
            select(BotRecord).where(BotRecord.owner_id == owner.id).order_by(BotRecord.id)
        )
    )
    if not bots:
        await message.answer(t(lang, "factory.mybots_empty"))
        return
    lines = [t(lang, "factory.mybots_header"), ""]
    for bot in bots:
        status = "" if bot.status == BotStatus.ACTIVE else f" ({bot.status})"
        lines.append(f"• @{bot.username} — {bot.display_name}{status}")
    url = runtime.panel_url()
    markup = InlineKeyboardMarkup(inline_keyboard=[[panel_button(lang, url)]]) if url else None
    await message.answer("\n".join(lines), reply_markup=markup)


@router.message(Command("cancel"))
async def on_cancel(message: Message, session: AsyncSession) -> None:
    lang = lang_of(message.from_user.language_code)
    owner = await get_or_create_user(session, message.from_user.id)
    row = await get_active_draft(session, owner)
    if row is None:
        await message.answer(t(lang, "factory.nothing_to_cancel"))
        return
    row.state = DraftState.CANCELLED
    await message.answer(t(lang, "factory.cancelled"), reply_markup=ReplyKeyboardRemove())


# --- designing ---------------------------------------------------------------------------


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, session: AsyncSession, runtime: Runtime) -> None:
    lang = lang_of(message.from_user.language_code)
    owner = await get_or_create_user(session, message.from_user.id, message.from_user.language_code)
    now = datetime.now(UTC)
    used = await count_today(runtime.redis, f"factory:{owner.id}", now, owner.timezone)
    if used > runtime.factory_daily_messages:
        await message.answer(t(lang, "factory.daily_limit"))
        return

    active = await get_active_draft(session, owner)
    existing = await existing_bot_drafts(session, owner)
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        result = await design(
            runtime.models.for_tier("cheap"),
            message.text,
            current=draft_of(active) if active else None,
            existing=existing,
            last_reply=active.last_reply if active else None,
        )
    except Exception:
        log.exception("designer failed for user %s", owner.id)
        await message.answer(t(lang, "factory.error"))
        return
    log.info(
        "designer for user %s: %s (%d in / %d out tokens)",
        owner.id,
        result.output.action,
        result.input_tokens,
        result.output_tokens,
    )

    output = result.output
    reply = "\n".join([output.reply, *(f"• {q}" for q in output.questions)])
    if output.action != "draft" or output.draft is None:
        await message.answer(reply)
        return

    target = await _find_target(session, owner, output.target_bot)
    row = await _save_designer_draft(session, owner, active, output.draft, reply, target)
    target_bot = await session.get(BotRecord, row.bot_id) if row.kind == DraftKind.EDIT else None

    await message.answer(reply)
    if result.dropped_collections:
        names = ", ".join(result.dropped_collections)
        await message.answer(t(lang, "factory.dropped", names=names))
    await message.answer(
        render_card(lang, row, target_bot.username if target_bot else None),
        parse_mode=ParseMode.HTML,
        # Edits of an existing bot can also be finished in the panel.
        reply_markup=card_keyboard(
            lang, row, runtime.panel_url(row.bot_id) if row.kind == DraftKind.EDIT else None
        ),
    )


async def _find_target(
    session: AsyncSession, owner: User, username: str | None
) -> BotRecord | None:
    if not username:
        return None
    return await session.scalar(
        select(BotRecord).where(
            BotRecord.owner_id == owner.id, BotRecord.username == username.lstrip("@")
        )
    )


async def _save_designer_draft(
    session: AsyncSession,
    owner: User,
    active: FactoryDraft | None,
    draft: BotDraft,
    reply: str,
    target: BotRecord | None,
) -> FactoryDraft:
    """Continue the active draft unless the designer switched to another bot."""
    if active is not None and (target is None or active.bot_id == target.id):
        return await update_draft(session, active, draft, reply)
    return await start_draft(session, owner, draft, reply, target=target)


# --- draft buttons -----------------------------------------------------------------------


async def _own_open_draft(
    session: AsyncSession, query: CallbackQuery, draft_id: int
) -> tuple[User, FactoryDraft] | None:
    owner = await get_or_create_user(session, query.from_user.id)
    row = await session.get(FactoryDraft, draft_id)
    if (
        row is None
        or row.user_id != owner.id
        or row.state
        not in (
            DraftState.OPEN,
            DraftState.AWAITING_CREATION,
        )
    ):
        await query.answer(t(lang_of(query.from_user.language_code), "factory.stale"))
        return None
    return owner, row


@router.callback_query(F.data.startswith("draft:create:"))
async def on_create(
    query: CallbackQuery, bot: Bot, session: AsyncSession, runtime: Runtime
) -> None:
    lang = lang_of(query.from_user.language_code)
    found = await _own_open_draft(session, query, int(query.data.rsplit(":", 1)[1]))
    if found is None:
        return
    owner, row = found
    limit = runtime.pricing.plan(owner.plan).max_bots
    if await count_active_bots(session, owner) >= limit:
        await query.answer()
        await query.message.answer(t(lang, "factory.at_limit", limit=limit))
        return

    row.state = DraftState.AWAITING_CREATION
    draft = draft_of(row)
    await query.answer()
    await query.message.answer(
        t(lang, "factory.press_create"),
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text=t(lang, "btn.create_keyboard", name=draft.name),
                        request_managed_bot=KeyboardButtonRequestManagedBot(
                            request_id=CREATE_REQUEST_ID,
                            suggested_name=draft.name,
                            suggested_username=row.suggested_username,
                        ),
                    )
                ]
            ],
            resize_keyboard=True,
        ),
    )
    me = await bot.me()
    link = create_link(me.username, row.suggested_username, draft.name)
    await query.message.answer(
        t(lang, "factory.link_fallback"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t(lang, "btn.create_link", name=draft.name), url=link)]
            ]
        ),
    )


@router.callback_query(F.data.startswith("draft:save:"))
async def on_save(query: CallbackQuery, session: AsyncSession, runtime: Runtime) -> None:
    lang = lang_of(query.from_user.language_code)
    found = await _own_open_draft(session, query, int(query.data.rsplit(":", 1)[1]))
    if found is None:
        return
    _, row = found
    target = await session.get(BotRecord, row.bot_id)
    draft = draft_of(row)
    old_name = target.display_name
    old_description = (target.persona or {}).get("description")
    problems = await apply_draft(session, row, target, runtime.pricing)
    target.display_name = draft.name
    token = runtime.cipher.decrypt(target.token_encrypted, str(target.tg_bot_id))
    await sync_profile(
        token,
        draft,
        set_name=draft.name != old_name,
        set_description=draft.description != old_description,
    )
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(t(lang, "factory.saved", username=target.username))
    if problems:
        await query.message.answer(_data_problems(lang, problems), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("draft:cancel:"))
async def on_cancel_button(query: CallbackQuery, session: AsyncSession) -> None:
    lang = lang_of(query.from_user.language_code)
    found = await _own_open_draft(session, query, int(query.data.rsplit(":", 1)[1]))
    if found is None:
        return
    _, row = found
    row.state = DraftState.CANCELLED
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(t(lang, "factory.cancelled"), reply_markup=ReplyKeyboardRemove())


# --- managed bot lifecycle -----------------------------------------------------------------


@router.managed_bot()
async def on_managed_bot(
    update: ManagedBotUpdated, bot: Bot, session: AsyncSession, runtime: Runtime
) -> None:
    async def fetch_token(tg_bot_id: int) -> str:
        return await bot(GetManagedBotToken(user_id=tg_bot_id))

    owner = await get_or_create_user(session, update.user.id, update.user.language_code)
    lang = lang_of(update.user.language_code)

    result = await register_managed_bot(
        session,
        runtime.cipher,
        fetch_token,
        creator_tg_id=update.user.id,
        info=ManagedBotInfo(
            tg_bot_id=update.bot_user.id,
            username=update.bot_user.username,
            display_name=update.bot_user.first_name,
        ),
        max_bots=runtime.pricing.plan(owner.plan).max_bots,
    )
    log.info("managed bot @%s: %s", update.bot_user.username, result.outcome)

    created_draft = None
    problems: list[Problem] = []
    if result.outcome == RegisterOutcome.CREATED:
        if row := await take_awaiting_draft(session, owner):
            created_draft = draft_of(row)
            problems = await apply_draft(session, row, result.bot, runtime.pricing)
            # Keep the name the user confirmed in Telegram's creation dialog.
            result.bot.display_name = update.bot_user.first_name
            await sync_profile(result.token, created_draft, set_name=False)
        # New bots are private until their owner shares them (SPEC 8.7).
        await sync_access(bot, result.bot)

    if not result.should_run:
        await runtime.stop_bot(update.bot_user.id)
    elif result.outcome != RegisterOutcome.UNCHANGED or not runtime.transport.is_running(
        update.bot_user.id
    ):
        await runtime.launch_bot(result.token)

    if result.outcome != RegisterOutcome.CREATED:
        return
    if result.bot.status == BotStatus.SUSPENDED:
        limit = runtime.pricing.plan(owner.plan).max_bots
        await bot.send_message(
            update.user.id,
            t(lang, "factory.bot_limit", limit=limit),
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    username = update.bot_user.username
    await bot.send_message(
        update.user.id,
        t(lang, "factory.ready", username=username),
        reply_markup=ReplyKeyboardRemove(),
    )
    if problems:
        await bot.send_message(
            update.user.id, _data_problems(lang, problems), parse_mode=ParseMode.HTML
        )
    await bot.send_message(
        update.user.id,
        t(lang, "avatar.offer", username=username),
        reply_markup=offer_keyboard(lang, result.bot.id),
    )
    # A managed bot cannot message its owner until they press Start (see docs/spikes).
    await bot.send_message(
        update.user.id,
        t(lang, "factory.open_hint"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t(lang, "btn.open", username=username),
                        url=f"https://t.me/{username}?start=init",
                    )
                ]
            ]
        ),
    )
