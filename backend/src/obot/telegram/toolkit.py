"""Everything a bot's agent run needs besides the model: tools, their services, notes."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from pydantic_ai import Tool
from sqlalchemy.ext.asyncio import AsyncSession

from obot.billing.usage import UsageMeter
from obot.bots.apis import key_values, load_endpoints
from obot.bots.users import user_bots
from obot.core.custom_api import endpoints_note
from obot.core.tools import ToolServices, build_tools
from obot.data.service import DataService
from obot.db.models import Bot, User
from obot.telegram.data_chat import DeleteConfirmer, SharedNotice
from obot.telegram.delegation import Delegator, delegation_note
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
    schedule: Callable[[str], Awaitable[str]] | None = None,
    ask_for_keys: Callable[[list[str]], Awaitable[str]] | None = None,
    data: bool = True,
) -> Toolkit:
    """The bot's tools, metered against `payer` (whoever started the request).

    The bot may consult the payer's other bots (ask_bot) unless `delegation` is False: a
    bot answering for another must not delegate again. `schedule` (private chats only)
    lets it draft scheduled tasks for the payer, and `ask_for_keys` lets it ask them for
    keys its APIs need. The payer's own keys fill the APIs' key slots. `data` False (group
    chats) leaves out the bot's collections; elsewhere the data_* tools work on the payer's
    own space.
    """
    apis = await load_endpoints(session, bot)
    services = ToolServices(
        http=runtime.http,
        meter=UsageMeter(session, runtime.pricing, payer, bot, now),
        tavily_api_key=runtime.tavily_api_key,
        apis=apis,
        api_keys=await key_values(session, runtime.cipher, payer, bot) if apis else {},
        ask_for_keys=ask_for_keys,
        schedule=schedule,
    )
    peers = await user_bots(session, payer, exclude=bot) if delegation else []
    if peers:
        services.delegate = Delegator(runtime, bot, payer, data=data)
    data_note = None
    if data:
        owner = await session.get(User, bot.owner_id) or payer
        service = DataService(
            runtime.sessionmaker,
            runtime.pricing,
            bot.id,
            payer.id,
            notice_shared=SharedNotice(runtime, bot, payer),
            confirm_delete=DeleteConfirmer(runtime, bot, payer, owner_timezone=owner.timezone),
        )
        data_note = await service.note(session, now)
        if data_note:
            services.data = service
    return Toolkit(
        tools=build_tools(
            bot.tools or [],
            apis=bool(apis),
            delegation=bool(peers),
            scheduling=schedule is not None,
            data=services.data is not None,
        ),
        services=services,
        note=join_notes(note, delegation_note(peers), endpoints_note(apis), data_note),
    )
