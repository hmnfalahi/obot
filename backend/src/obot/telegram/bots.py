"""Handlers shared by every managed bot."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from obot.billing.usage import QuotaReason, check_llm_quota, record_llm_usage
from obot.bots.byok import reply_model, uses_byok
from obot.bots.service import mark_owner_started
from obot.chat.history import ChatScope, recent_messages, record_message
from obot.chat.membership import bots_in_chat, upsert_membership
from obot.core.agent import BotProfile, Turn, generate_reply
from obot.core.byok import failure_of
from obot.core.rules import rule_note
from obot.db.models import Bot as BotRecord
from obot.db.models import BotStatus, SenderKind, TriggerKind
from obot.db.models import Message as StoredMessage
from obot.i18n import lang_of, t
from obot.memory.jobs import recall_facts
from obot.memory.store import MEMORY_TOOL, forget_chat
from obot.notices import first_today
from obot.tasks.events import chat_trigger_tasks, enqueue_event, keyword_hit
from obot.tasks.service import resume_waiting
from obot.telegram.addressing import is_addressed, strip_mention
from obot.telegram.byok_notice import notify_key_failure
from obot.telegram.conversations import (
    Thread,
    addresses,
    bot_note,
    is_new_in_thread,
    is_skip,
    peers_note,
)
from obot.telegram.moderation import report_from_bot
from obot.telegram.reply_rules import pick_rule, welcome_rule
from obot.telegram.runtime import Runtime
from obot.telegram.toolkit import toolkit

log = logging.getLogger(__name__)

router = Router(name="bots")

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
MEMBER_STATUSES = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.RESTRICTED,
}


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def on_start(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    first_time = await mark_owner_started(session, bot_record, message.from_user.id)
    if first_time:
        await resume_waiting(session, bot_record, datetime.now(UTC))
    text = (
        "All set — I can message you now." if first_time else f"Hi! I'm {bot_record.display_name}."
    )
    await runtime.sender.send_text(message.bot, message.chat.id, text, group=False)


@router.message(Command("report"))
async def on_report(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot_record: BotRecord,
    runtime: Runtime,
) -> None:
    await report_from_bot(message, session, runtime, bot_record, command.args)


@router.message(Command("forget"))
async def on_forget(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    lang = lang_of(message.from_user.language_code)
    group = message.chat.type in GROUP_TYPES
    if group:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            await runtime.sender.send_text(
                message.bot,
                message.chat.id,
                t(lang, "forget.admins_only"),
                group=True,
                reply_to=message.message_id,
            )
            return
    await forget_chat(session, ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id))
    await session.commit()
    await runtime.sender.send_text(
        message.bot, message.chat.id, t(lang, "forget.done"), group=group
    )


@router.my_chat_member()
async def on_membership(
    event: ChatMemberUpdated, session: AsyncSession, bot_record: BotRecord
) -> None:
    status = event.new_chat_member.status
    if event.chat.type == ChatType.PRIVATE:
        # The owner blocked the bot: it can no longer message them until they start it again.
        if status == ChatMemberStatus.KICKED and event.from_user.id == bot_record.owner.tg_user_id:
            bot_record.owner_started = False
        return
    await upsert_membership(
        session,
        bot_record.id,
        event.chat.id,
        chat_type=event.chat.type,
        title=event.chat.title,
        is_member=status in MEMBER_STATUSES,
        is_admin=status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR),
    )
    log.info("bot %s is now %s in chat %s", bot_record.id, status, event.chat.id)


@router.message(F.text, F.chat.type == ChatType.PRIVATE)
async def on_private_text(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    # Any private message from the owner opens the chat, not only /start.
    if await mark_owner_started(session, bot_record, message.from_user.id):
        await resume_waiting(session, bot_record, datetime.now(UTC))
    scope = ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id)
    incoming = await _record_incoming(session, scope, message)
    await _respond(Inbound.of(message), message.text, incoming, scope, session, bot_record, runtime)


@router.message(F.text, F.chat.type.in_(GROUP_TYPES))
async def on_group_text(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    scope = ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id)
    incoming = await _record_incoming(session, scope, message)
    # Bots' messages only reach us through route_to_bots, never via Telegram.
    if message.from_user.is_bot:
        return
    await fire_chat_triggers(
        session,
        bot_record,
        message.chat.id,
        TriggerKind.GROUP_KEYWORD,
        f"{message.from_user.full_name} wrote in {message.chat.title}: {message.text}",
        text=message.text,
    )
    note = None
    if not is_addressed(message, message.bot.id, bot_record.username):
        # Not asked directly: answer only if one of the owner's reply rules says so.
        rule = await pick_rule(
            session, runtime, bot_record, message.chat.id, message.text, datetime.now(UTC)
        )
        if rule is None:
            await session.commit()  # keep the metered rule checks
            return
        note = rule_note(rule)
    text = strip_mention(message.text, bot_record.username) or message.text
    await _respond(
        Inbound.of(message),
        text,
        incoming,
        scope,
        session,
        bot_record,
        runtime,
        note=note,
        thread=Thread(root=message.message_id),
    )


@router.message(F.new_chat_members, F.chat.type.in_(GROUP_TYPES))
async def on_new_members(
    message: Message, session: AsyncSession, bot_record: BotRecord, runtime: Runtime
) -> None:
    people = [u for u in message.new_chat_members if not u.is_bot]
    if people:
        names = ", ".join(u.full_name for u in people)
        await fire_chat_triggers(
            session,
            bot_record,
            message.chat.id,
            TriggerKind.NEW_MEMBER,
            f"{names} joined {message.chat.title}.",
        )
        await session.commit()
    rule = welcome_rule(bot_record)
    if not people or rule is None:
        return
    names = ", ".join(u.full_name for u in people)
    scope = ChatScope(bot_id=bot_record.id, tg_chat_id=message.chat.id)
    text = f"{names} joined the group."
    incoming = await record_message(
        session,
        scope,
        sender_kind=SenderKind.HUMAN,
        text=text,
        tg_message_id=message.message_id,
        sender_tg_id=people[0].id,
        sender_name=names,
    )
    await session.commit()
    await _respond(
        Inbound.of(message),
        text,
        incoming,
        scope,
        session,
        bot_record,
        runtime,
        note=rule_note(rule),
        thread=Thread(root=message.message_id),
    )


async def fire_chat_triggers(
    session: AsyncSession,
    bot_record: BotRecord,
    chat_id: int,
    kind: TriggerKind,
    payload: str,
    text: str | None = None,
) -> None:
    """Queue the bot's tasks triggered by this group event (run by run_task_events)."""
    for task in await chat_trigger_tasks(session, bot_record.id, chat_id, kind):
        if kind == TriggerKind.GROUP_KEYWORD and not keyword_hit(task, text or ""):
            continue
        await enqueue_event(session, task, payload)


async def _record_incoming(
    session: AsyncSession, scope: ChatScope, message: Message
) -> StoredMessage:
    stored = await record_message(
        session,
        scope,
        sender_kind=SenderKind.BOT if message.from_user.is_bot else SenderKind.HUMAN,
        text=message.text,
        tg_message_id=message.message_id,
        sender_tg_id=message.from_user.id,
        sender_name=message.from_user.full_name,
    )
    # Persist the incoming message even if the model call fails later.
    await session.commit()
    return stored


def to_turn(stored: StoredMessage, group: bool) -> Turn:
    if stored.sender_kind == SenderKind.SELF:
        return Turn(role="assistant", text=stored.text)
    text = f"{stored.sender_name}: {stored.text}" if group else stored.text
    return Turn(role="user", text=text)


@dataclass(frozen=True)
class Inbound:
    """What a reply needs to know about the message it answers.

    Built from a Telegram update, or by the server when one Obot bot addresses
    another in a group (Telegram does not deliver bots' messages to bots).
    """

    client: Bot  # the replying bot's Telegram client
    chat_id: int
    message_id: int
    sender_name: str
    sender_language: str | None
    group: bool
    from_bot: bool = False
    # Telegram id of whoever wrote the message being answered.
    sender_tg_id: int | None = None

    @classmethod
    def of(cls, message: Message) -> "Inbound":
        return cls(
            client=message.bot,
            chat_id=message.chat.id,
            message_id=message.message_id,
            sender_name=message.from_user.full_name,
            sender_language=message.from_user.language_code,
            group=message.chat.type in GROUP_TYPES,
            from_bot=message.from_user.is_bot,
            sender_tg_id=message.from_user.id,
        )


async def _respond(
    inbound: Inbound,
    text: str,
    incoming: StoredMessage,
    scope: ChatScope,
    session: AsyncSession,
    bot_record: BotRecord,
    runtime: Runtime,
    *,
    note: str | None = None,
    thread: Thread | None = None,
) -> None:
    owner = bot_record.owner
    group = inbound.group
    now = datetime.now(UTC)
    byok = uses_byok(bot_record)
    quota = await check_llm_quota(session, runtime.pricing, owner, bot_record, now, byok=byok)
    if not quota.allowed:
        # Another bot cannot read a quota notice; only people get one.
        if not inbound.from_bot:
            await notify_quota_exhausted(inbound, bot_record, quota.reason, runtime, now)
        return

    history = [
        to_turn(m, group)
        for m in await recent_messages(session, scope, runtime.history_limit, before_id=incoming.id)
    ]
    user_text = f"{inbound.sender_name}: {text}" if group else text
    profile = BotProfile(name=bot_record.display_name, persona=bot_record.persona)
    if group:
        peers = await bots_in_chat(session, inbound.chat_id, exclude=bot_record.id)
        about_peers = peers_note([(p.username, p.display_name) for p in peers])
        note = "\n\n".join(n for n in (note, about_peers) if n) or None
    memories: list[str] = []
    reply_to = inbound.message_id if group else None

    await inbound.client.send_chat_action(inbound.chat_id, ChatAction.TYPING)
    try:
        if MEMORY_TOOL in (bot_record.tools or []):
            memories = await recall_facts(
                session, runtime.pricing, bot_record, scope, text, runtime.embedder
            )
        chosen = reply_model(runtime.models, runtime.cipher, runtime.pricing, bot_record)
        kit = await toolkit(session, runtime, bot_record, now, note=note)
        reply = await generate_reply(
            chosen.model,
            profile,
            history,
            user_text,
            tools=kit.tools,
            services=kit.services,
            group=group,
            memories=memories,
            note=kit.note,
        )
    except Exception as e:
        log.exception("agent failed for bot %s chat %s", bot_record.id, inbound.chat_id)
        # Never fall back to our model: the owner chose to pay with their own key.
        failure = failure_of(e) if byok else None
        if failure is not None:
            await notify_key_failure(runtime, bot_record, failure, now)
        if not inbound.from_bot:
            await runtime.sender.send_text(
                inbound.client,
                inbound.chat_id,
                t(lang_of(inbound.sender_language), "byok.chat_failed")
                if failure is not None
                else "Sorry, something went wrong. Please try again.",
                group=group,
                reply_to=reply_to,
            )
        return

    usage = await record_llm_usage(
        session,
        runtime.pricing,
        owner,
        bot_record,
        reply.model_name,
        reply.input_tokens,
        reply.output_tokens,
        byok=chosen.byok,
    )
    log.info(
        "bot %s replied in %s with %s (%d in / %d out tokens, %d credits)",
        bot_record.id,
        "group" if group else "private",
        reply.model_name,
        reply.input_tokens,
        reply.output_tokens,
        usage.credits,
    )
    if inbound.from_bot and is_skip(reply.text):
        log.info("bot %s ended a bot conversation in chat %s", bot_record.id, inbound.chat_id)
        return
    sent = await runtime.sender.send_text(
        inbound.client, inbound.chat_id, reply.text, group=group, reply_to=reply_to
    )
    await record_message(
        session,
        scope,
        sender_kind=SenderKind.SELF,
        text=reply.text,
        tg_message_id=sent[-1].message_id if sent else None,
        sender_tg_id=inbound.client.id,
        sender_name=bot_record.display_name,
    )
    if group and thread is not None and sent:
        await session.commit()
        # Replies go to the message's author; we know it, so routing does not depend
        # on Telegram echoing reply_to_message back.
        await route_to_bots(
            runtime,
            bot_record,
            inbound.chat_id,
            sent[-1],
            reply.text,
            thread,
            replied_to=inbound.sender_tg_id,
        )


# --- bot-to-bot routing ----------------------------------------------------------------------


async def route_to_bots(
    runtime: Runtime,
    sender: BotRecord,
    chat_id: int,
    sent: Message,
    text: str,
    thread: Thread,
    replied_to: int | None = None,
) -> None:
    """Hand a bot's group message to the Obot bots it addresses (see conversations.py)."""
    next_thread = thread.next()
    if not next_thread.can_continue:
        log.info("bot conversation in chat %s reached its turn limit", chat_id)
        return
    if not await is_new_in_thread(runtime.redis, chat_id, thread, text):
        log.info("bot conversation in chat %s is repeating itself; stopping", chat_id)
        return
    if replied_to is None and sent.reply_to_message and sent.reply_to_message.from_user:
        replied_to = sent.reply_to_message.from_user.id
    async with runtime.sessionmaker() as session:
        peers = await bots_in_chat(session, chat_id, exclude=sender.id)
    for peer in peers:
        if not addresses(text, replied_to, peer.username, peer.tg_bot_id):
            continue
        client = runtime.transport.client(peer.tg_bot_id)
        if client is None:
            log.warning("bot %s is addressed in chat %s but not running", peer.id, chat_id)
            continue
        log.info(
            "routing bot %s -> bot %s in chat %s (turn %d)",
            sender.id,
            peer.id,
            chat_id,
            next_thread.depth,
        )

        async def deliver(peer_id: int = peer.id, client: Bot = client) -> None:
            await _deliver_from_bot(
                runtime, peer_id, client, chat_id, sent, text, sender, next_thread
            )

        runtime.updates.spawn(peer.tg_bot_id, ("chat", chat_id), deliver)


async def _deliver_from_bot(
    runtime: Runtime,
    bot_id: int,
    client: Bot,
    chat_id: int,
    sent: Message,
    text: str,
    sender: BotRecord,
    thread: Thread,
) -> None:
    async with runtime.sessionmaker() as session:
        record = await session.get(BotRecord, bot_id, options=[joinedload(BotRecord.owner)])
        if record is None or record.status != BotStatus.ACTIVE or record.owner.is_disabled:
            return
        scope = ChatScope(bot_id=record.id, tg_chat_id=chat_id)
        incoming = await record_message(
            session,
            scope,
            sender_kind=SenderKind.BOT,
            text=text,
            tg_message_id=sent.message_id,
            sender_tg_id=sender.tg_bot_id,
            sender_name=sender.display_name,
        )
        await session.commit()
        inbound = Inbound(
            client=client,
            chat_id=chat_id,
            message_id=sent.message_id,
            sender_name=sender.display_name,
            sender_language=None,
            group=True,
            from_bot=True,
            sender_tg_id=sender.tg_bot_id,
        )
        text_for_bot = strip_mention(text, record.username) or text
        await _respond(
            inbound,
            text_for_bot,
            incoming,
            scope,
            session,
            record,
            runtime,
            note=bot_note(sender.display_name, thread),
            thread=thread,
        )
        await session.commit()


async def notify_quota_exhausted(
    inbound: Inbound,
    bot_record: BotRecord,
    reason: QuotaReason,
    runtime: Runtime,
    now: datetime,
) -> None:
    """Tell the chat (once per day) and the owner (once per day per reason)."""
    owner = bot_record.owner
    chat_key = f"quota:{bot_record.id}:{inbound.chat_id}"
    if await first_today(runtime.redis, chat_key, now, owner.timezone):
        await runtime.sender.send_text(
            inbound.client,
            inbound.chat_id,
            t(lang_of(inbound.sender_language), f"quota.{reason}"),
            group=inbound.group,
            reply_to=inbound.message_id if inbound.group else None,
        )

    owner_key = f"quota-owner:{owner.id}:{reason}"
    if reason == QuotaReason.BOT_DAILY_CAP:
        owner_key += f":{bot_record.id}"
    if await first_today(runtime.redis, owner_key, now, owner.timezone):
        limit = runtime.pricing.plan(owner.plan).daily_llm_messages
        text = t(
            lang_of(owner.language),
            f"owner.quota.{reason}",
            limit=limit,
            username=bot_record.username,
        )
        try:
            await runtime.factory_bot.send_message(owner.tg_user_id, text)
        except Exception:
            log.warning("could not notify owner %s about quota", owner.id, exc_info=True)
