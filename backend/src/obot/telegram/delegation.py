"""Delegation between bots of the same owner (SPEC 8.5, P2-4).

A bot with the ask_bots tool can put a question to one of its owner's other
bots. The other bot answers with its own persona, tools and billing, in its own
session, and cannot delegate further (depth 1).
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from obot.billing.usage import UsageMeter, check_llm_quota, record_llm_usage
from obot.bots.byok import reply_model, uses_byok
from obot.core.agent import BotProfile, generate_reply
from obot.core.byok import failure_of
from obot.core.tools import ToolServices, build_tools, truncate
from obot.db.models import Bot, BotStatus
from obot.telegram.byok_notice import notify_key_failure
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

DELEGATION_TOOL = "ask_bots"
MAX_ANSWER_CHARS = 4000


async def owner_peers(session: AsyncSession, bot: Bot) -> list[Bot]:
    """The owner's other active bots."""
    rows = await session.scalars(
        select(Bot)
        .where(Bot.owner_id == bot.owner_id, Bot.id != bot.id, Bot.status == BotStatus.ACTIVE)
        .order_by(Bot.id)
    )
    return list(rows)


def delegation_note(peers: list[Bot]) -> str | None:
    if not peers:
        return None
    lines = []
    for peer in peers:
        description = (peer.persona or {}).get("description") or ""
        lines.append(
            f"- @{peer.username} ({peer.display_name}){': ' + description if description else ''}"
        )
    return (
        "With the ask_bot tool you can consult these other bots of your owner when they "
        "are better suited; they do not see this conversation:\n" + "\n".join(lines)
    )


class Delegator:
    """The `delegate` callable for ToolServices of one reply."""

    def __init__(self, runtime: Runtime, caller: Bot) -> None:
        self._runtime = runtime
        self._caller_id = caller.id
        self._caller_name = caller.display_name
        self._owner_id = caller.owner_id

    async def __call__(self, username: str, question: str) -> str:
        runtime = self._runtime
        now = datetime.now(UTC)
        async with runtime.sessionmaker() as session:
            callee = await session.scalar(
                select(Bot)
                .options(joinedload(Bot.owner))
                .where(
                    Bot.owner_id == self._owner_id,
                    Bot.id != self._caller_id,
                    func.lower(Bot.username) == username.lower(),
                )
            )
            if callee is None or callee.status != BotStatus.ACTIVE:
                return f"ERROR: there is no active bot @{username} you can ask."
            byok = uses_byok(callee)
            quota = await check_llm_quota(
                session, runtime.pricing, callee.owner, callee, now, byok=byok
            )
            if not quota.allowed:
                return f"ERROR: @{username} cannot answer right now (usage limit reached)."

            # Depth 1: the callee gets its tools except delegation itself.
            tools = [t for t in (callee.tools or []) if t != DELEGATION_TOOL]
            services = ToolServices(
                http=runtime.http,
                meter=UsageMeter(session, runtime.pricing, callee.owner, callee, now),
                tavily_api_key=runtime.tavily_api_key,
            )
            try:
                chosen = reply_model(runtime.models, runtime.cipher, runtime.pricing, callee)
                reply = await generate_reply(
                    chosen.model,
                    BotProfile(name=callee.display_name, persona=callee.persona),
                    [],
                    question,
                    tools=build_tools(tools),
                    services=services,
                    note=(
                        f"This question comes from {self._caller_name}, another bot of your "
                        "owner, not from a person. Answer it directly and completely."
                    ),
                )
            except Exception as e:
                log.exception("delegation to bot %s failed", callee.id)
                await session.commit()  # keep metered tool use
                if byok and (failure := failure_of(e)) is not None:
                    await notify_key_failure(runtime, callee, failure, now)
                return f"ERROR: @{username} failed to answer."
            await record_llm_usage(
                session,
                runtime.pricing,
                callee.owner,
                callee,
                reply.model_name,
                reply.input_tokens,
                reply.output_tokens,
                byok=chosen.byok,
            )
            await session.commit()
        log.info(
            "bot %s asked bot %s (%d out tokens)", self._caller_id, callee.id, reply.output_tokens
        )
        return truncate(reply.text, MAX_ANSWER_CHARS)
