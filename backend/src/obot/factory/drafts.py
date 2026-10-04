"""Factory drafts: one open design per user, applied to a bot on create or save."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from obot.core.rules import parse_rules
from obot.db.models import Bot, DraftKind, DraftState, FactoryDraft, User
from obot.factory.designer import BotDraft, ExistingBot, make_username

ACTIVE_STATES = (DraftState.OPEN, DraftState.AWAITING_CREATION)


async def get_active_draft(session: AsyncSession, user: User) -> FactoryDraft | None:
    return await session.scalar(
        select(FactoryDraft)
        .where(FactoryDraft.user_id == user.id, FactoryDraft.state.in_(ACTIVE_STATES))
        .order_by(FactoryDraft.id.desc())
        .limit(1)
    )


async def start_draft(
    session: AsyncSession,
    user: User,
    draft: BotDraft,
    last_reply: str | None,
    target: Bot | None = None,
) -> FactoryDraft:
    """Open a new draft, cancelling any other active draft of the user."""
    await session.execute(
        update(FactoryDraft)
        .where(FactoryDraft.user_id == user.id, FactoryDraft.state.in_(ACTIVE_STATES))
        .values(state=DraftState.CANCELLED)
    )
    row = FactoryDraft(
        user_id=user.id,
        bot_id=target.id if target else None,
        kind=DraftKind.EDIT if target else DraftKind.NEW,
        draft=draft.model_dump(mode="json"),
        suggested_username=None if target else make_username(draft.username_hint),
        last_reply=last_reply,
    )
    session.add(row)
    await session.flush()
    return row


async def update_draft(
    session: AsyncSession, row: FactoryDraft, draft: BotDraft, last_reply: str | None
) -> FactoryDraft:
    row.draft = draft.model_dump(mode="json")
    row.last_reply = last_reply
    # An edited draft needs a fresh "Create" press.
    row.state = DraftState.OPEN
    if row.kind == DraftKind.NEW:
        row.suggested_username = make_username(draft.username_hint)
    await session.flush()
    return row


def draft_of(row: FactoryDraft) -> BotDraft:
    return BotDraft.model_validate(row.draft)


async def take_awaiting_draft(session: AsyncSession, user: User) -> FactoryDraft | None:
    """The new-bot draft whose "Create" button the user pressed last."""
    return await session.scalar(
        select(FactoryDraft)
        .where(
            FactoryDraft.user_id == user.id,
            FactoryDraft.kind == DraftKind.NEW,
            FactoryDraft.state == DraftState.AWAITING_CREATION,
        )
        .order_by(FactoryDraft.id.desc())
        .limit(1)
    )


async def apply_draft(session: AsyncSession, row: FactoryDraft, bot: Bot) -> None:
    draft = draft_of(row)
    bot.display_name = draft.name
    bot.persona = {
        "system_prompt": draft.system_prompt,
        "language": draft.language,
        "description": draft.description,
        "username_hint": draft.username_hint,
    }
    bot.tools = [t.value for t in draft.tools]
    bot.reply_rules = [r.model_dump(mode="json", exclude_defaults=True) for r in draft.reply_rules]
    row.bot_id = bot.id
    row.state = DraftState.APPLIED
    await session.flush()


async def requested_tasks(session: AsyncSession, bot: Bot) -> list[str]:
    """Tasks of the latest applied draft. Scheduling them arrives in SPEC task 9."""
    row = await session.scalar(
        select(FactoryDraft)
        .where(FactoryDraft.bot_id == bot.id, FactoryDraft.state == DraftState.APPLIED)
        .order_by(FactoryDraft.id.desc())
        .limit(1)
    )
    return draft_of(row).tasks if row else []


async def existing_bot_drafts(session: AsyncSession, user: User) -> list[ExistingBot]:
    bots = await session.scalars(select(Bot).where(Bot.owner_id == user.id).order_by(Bot.id))
    result = []
    for bot in bots:
        persona = bot.persona or {}
        draft = BotDraft(
            name=bot.display_name,
            username_hint=persona.get("username_hint") or bot.username.removesuffix("_bot"),
            description=persona.get("description", ""),
            system_prompt=persona.get("system_prompt", ""),
            language=persona.get("language", "en"),
            tools=bot.tools or [],
            tasks=await requested_tasks(session, bot),
            reply_rules=parse_rules(bot.reply_rules or []),
        )
        result.append(ExistingBot(username=bot.username, draft=draft))
    return result
