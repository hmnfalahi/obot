"""Everything a bot's agent run needs besides the model: tools, their services, notes."""

from dataclasses import dataclass
from datetime import datetime

from pydantic_ai import Tool
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.usage import UsageMeter
from obot.bots.apis import load_endpoints
from obot.core.custom_api import endpoints_note
from obot.core.tools import ToolServices, build_tools
from obot.db.models import Bot, User
from obot.telegram.delegation import DELEGATION_TOOL, Delegator, delegation_note, owner_peers
from obot.telegram.runtime import Runtime


@dataclass
class Toolkit:
    tools: list[Tool[ToolServices]]
    services: ToolServices
    note: str | None


def join_notes(*notes: str | None) -> str | None:
    return "\n\n".join(n for n in notes if n) or None


async def toolkit(
    session: AsyncSession,
    runtime: Runtime,
    bot: Bot,
    now: datetime,
    payer: User,
    *,
    delegation: bool = True,
    note: str | None = None,
) -> Toolkit:
    """The bot's tools, metered against `payer` (whoever started the request).

    `delegation` False drops ask_bots (a bot answering for another must not delegate).
    """
    names = [t for t in (bot.tools or []) if delegation or t != DELEGATION_TOOL]
    apis = await load_endpoints(session, runtime.cipher, bot)
    services = ToolServices(
        http=runtime.http,
        meter=UsageMeter(session, runtime.pricing, payer, bot, now),
        tavily_api_key=runtime.tavily_api_key,
        apis=apis,
    )
    peers = None
    if DELEGATION_TOOL in names:
        services.delegate = Delegator(runtime, bot, payer)
        peers = delegation_note(await owner_peers(session, bot))
    return Toolkit(
        tools=build_tools(names, apis=bool(apis)),
        services=services,
        note=join_notes(note, peers, endpoints_note(apis)),
    )
