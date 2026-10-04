"""Bots consulting each other behind the scenes (SPEC 8.7).

While answering someone, a bot can put a question to any other bot of that
person: the ones they made and the ones of others they use (see user_bots).
It is always available, not a per-bot tool. The other bot answers with its own
persona and tools, in its own session, and cannot delegate further (depth 1).
The person who asked pays for the answer too.
"""

import logging
from datetime import UTC, datetime

from obot.billing.usage import check_llm_quota, record_llm_usage
from obot.bots.byok import pays_with_key, reply_model
from obot.bots.users import user_bots
from obot.core.agent import BotProfile, generate_reply
from obot.core.byok import failure_of
from obot.core.tools import truncate
from obot.db.models import Bot, User
from obot.telegram.byok_notice import notify_key_failure
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

MAX_ANSWER_CHARS = 4000


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
        "With the ask_bot tool you can consult these other bots of the person you are "
        "helping when they are better suited; they do not see this conversation:\n"
        + "\n".join(lines)
    )


class Delegator:
    """The `delegate` callable for ToolServices of one reply."""

    def __init__(self, runtime: Runtime, caller: Bot, payer: User) -> None:
        self._runtime = runtime
        self._payer_id = payer.id
        self._caller_id = caller.id
        self._caller_name = caller.display_name

    async def __call__(self, username: str, question: str) -> str:
        runtime = self._runtime
        now = datetime.now(UTC)
        async with runtime.sessionmaker() as session:
            payer = await session.get(User, self._payer_id)
            name = username.strip().lstrip("@").lower()
            callee = next(
                (
                    b
                    for b in await user_bots(session, payer)
                    if b.id != self._caller_id and b.username.lower() == name
                ),
                None,
            )
            if callee is None:
                return f"ERROR: there is no active bot @{username} you can ask."
            byok = pays_with_key(callee, payer)
            quota = await check_llm_quota(session, runtime.pricing, payer, callee, now, byok=byok)
            if not quota.allowed:
                return f"ERROR: @{username} cannot answer right now (usage limit reached)."

            # Imported here: the toolkit builds Delegators itself.
            from obot.telegram.toolkit import toolkit

            # Depth 1: the callee gets its tools except delegation itself.
            kit = await toolkit(
                session,
                runtime,
                callee,
                now,
                payer,
                delegation=False,
                note=(
                    f"This question comes from {self._caller_name}, another bot working for "
                    "someone who uses you, not from a person. Answer it directly and "
                    "completely."
                ),
            )
            try:
                chosen = reply_model(runtime.models, runtime.cipher, runtime.pricing, callee, payer)
                reply = await generate_reply(
                    chosen.model,
                    BotProfile(name=callee.display_name, persona=callee.persona),
                    [],
                    question,
                    tools=kit.tools,
                    services=kit.services,
                    note=kit.note,
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
                payer,
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
