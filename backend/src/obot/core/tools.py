"""Tools a bot can use, independent of Telegram and the database.

Metering is delegated to a ToolMeter so plans and billing stay outside the core.
Tool failures are returned to the model as text starting with "ERROR:", so the
bot can tell the user what went wrong instead of failing the whole reply.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import feedparser
import httpx
import trafilatura
from pydantic import BaseModel
from pydantic_ai import RunContext, Tool

from obot.core.custom_api import API_TOOL, Endpoint, MissingKeys, request
from obot.core.fetch import FetchError, FetchResult, fetch, truncate

log = logging.getLogger(__name__)

MAX_FEED_ITEMS = 10
SEARCH_RESULTS = 5
TAVILY_URL = "https://api.tavily.com/search"


class ToolMeter(Protocol):
    async def before(self, kind: str) -> str | None:
        """Return a refusal message if the owner may not use `kind` right now."""

    async def after(self, kind: str) -> None:
        """Record one use of `kind`."""


class DataTools(Protocol):
    """A bot's collections for the person being helped; see obot.data.service."""

    async def find(
        self,
        collection: str,
        filters: list[dict[str, Any]] | None = None,
        sort: str | None = None,
        descending: bool = True,
        limit: int = 20,
        availability: bool = False,
    ) -> str: ...

    async def add(self, collection: str, data: dict[str, Any]) -> str: ...

    async def update(self, collection: str, record_id: int, changes: dict[str, Any]) -> str: ...

    async def delete(self, collection: str, record_id: int) -> str: ...

    async def summarize(
        self,
        collection: str,
        metric: str,
        field: str | None = None,
        filters: list[dict[str, Any]] | None = None,
        group_by: str | None = None,
        period: str | None = None,
    ) -> str: ...


class DataFilter(BaseModel):
    field: str
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "contains", "in"]
    value: str | float | bool | list[str | float] | None = None


@dataclass
class ToolServices:
    http: httpx.AsyncClient
    meter: ToolMeter
    tavily_api_key: str | None = None
    # Asks another bot of the person being helped (username, question) -> answer; see ask_bot.
    delegate: Callable[[str, str], Awaitable[str]] | None = None
    # Drafts a scheduled task for the person (request) -> outcome; see schedule_task.
    schedule: Callable[[str], Awaitable[str]] | None = None
    # The bot's endpoints for call_api, by name; see obot.core.custom_api.
    apis: dict[str, Endpoint] = field(default_factory=dict)
    # The person's own values for the endpoints' key slots (never shown to the model).
    api_keys: dict[str, str] = field(default_factory=dict, repr=False)
    # Asks the person to set missing keys (names) -> what to tell them; see call_api.
    ask_for_keys: Callable[[list[str]], Awaitable[str]] | None = None
    # The bot's collections (data_* tools); None where data is not available (groups).
    data: DataTools | None = None


def page_text(result: FetchResult) -> str:
    content_type = result.content_type
    if "html" in content_type or not content_type:
        text = trafilatura.extract(
            result.body, include_tables=True, include_links=False, favor_recall=True
        ) or trafilatura.html2txt(result.body)
    elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
        text = result.body.decode("utf-8", errors="replace")
    else:
        raise FetchError(f"the page is not text (content type {content_type})")
    if not text or not text.strip():
        raise FetchError("the page has no readable text (it may need JavaScript)")
    return truncate(text.strip())


async def web_search(ctx: RunContext[ToolServices], query: str) -> str:
    """Search the web for current information. Returns titles, URLs and snippets."""
    services = ctx.deps
    if not services.tavily_api_key:
        return "ERROR: web search is not configured on this server."
    if refusal := await services.meter.before("search"):
        return f"ERROR: {refusal}"
    try:
        response = await services.http.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {services.tavily_api_key}"},
            json={"query": query, "max_results": SEARCH_RESULTS},
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("web search failed: %r", e)
        return f"ERROR: web search failed ({type(e).__name__})."
    await services.meter.after("search")
    results = response.json().get("results", [])
    if not results:
        return "No results."
    return truncate("\n\n".join(f"{r['title']}\n{r['url']}\n{r['content'][:600]}" for r in results))


async def read_url(ctx: RunContext[ToolServices], url: str) -> str:
    """Read the main text of a web page, including tables (e.g. prices)."""
    services = ctx.deps
    if refusal := await services.meter.before("fetch"):
        return f"ERROR: {refusal}"
    try:
        result = await fetch(services.http, url)
        text = page_text(result)
    except FetchError as e:
        log.info("read_url %r failed: %s", url, e)
        return f"ERROR: could not read {url}: {e}."
    await services.meter.after("fetch")
    return f"Content of {result.url}:\n\n{text}"


async def read_rss(ctx: RunContext[ToolServices], url: str) -> str:
    """Read the latest items of an RSS or Atom feed."""
    services = ctx.deps
    if refusal := await services.meter.before("fetch"):
        return f"ERROR: {refusal}"
    try:
        result = await fetch(services.http, url)
    except FetchError as e:
        log.info("read_rss %r failed: %s", url, e)
        return f"ERROR: could not read the feed {url}: {e}."
    feed = feedparser.parse(result.body)
    if not feed.entries:
        return f"ERROR: {url} is not a valid RSS/Atom feed or has no items."
    await services.meter.after("fetch")
    items = []
    for entry in feed.entries[:MAX_FEED_ITEMS]:
        published = entry.get("published") or entry.get("updated") or ""
        summary = trafilatura.html2txt(entry.get("summary", "")) if entry.get("summary") else ""
        items.append(
            f"{entry.get('title', '(no title)')} {published}\n{entry.get('link', '')}\n"
            f"{summary[:300]}"
        )
    title = feed.feed.get("title", url)
    return truncate(f"Feed: {title}\n\n" + "\n\n".join(items))


async def ask_bot(ctx: RunContext[ToolServices], username: str, question: str) -> str:
    """Ask another bot of the person you are helping (by @username) and get its answer.

    Use it when that bot is better suited, e.g. a translator or a specialist. Give
    it a complete, self-contained question: it cannot see this conversation.
    """
    if ctx.deps.delegate is None:
        return "ERROR: asking other bots is not available here."
    return await ctx.deps.delegate(username.strip().lstrip("@"), question)


async def schedule_task(ctx: RunContext[ToolServices], request: str) -> str:
    """Schedule a recurring or one-off job for the person, e.g. 'every day at 8:00 send me
    the gold price' or 'tomorrow at 9 remind me to call Sara'.

    Pass their request as one precise sentence with what to do, when, and where to send it
    if not to them. They confirm it with a button; do not ask them to confirm in words.
    """
    if ctx.deps.schedule is None:
        return "ERROR: scheduling is only available in a private chat with the bot."
    return await ctx.deps.schedule(request)


async def call_api(
    ctx: RunContext[ToolServices],
    name: str,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> str:
    """Call one of the APIs by name (see the list in your instructions).

    path_params fills the {placeholders} of its address, query adds URL parameters,
    body is JSON sent with POST, PUT and PATCH.
    """
    services = ctx.deps
    endpoint = services.apis.get(name.strip())
    if endpoint is None:
        return f"ERROR: there is no API named '{name}'."
    if refusal := await services.meter.before("api"):
        return f"ERROR: {refusal}"
    try:
        text = await request(
            services.http, endpoint, path_params or {}, query or {}, body, services.api_keys
        )
    except MissingKeys as e:
        if services.ask_for_keys is not None:
            return await services.ask_for_keys(e.names)
        return f"ERROR: the user has not set their {', '.join(e.names)} for this bot yet."
    except FetchError as e:
        log.info("call_api %s failed: %s", endpoint.name, e)
        return f"ERROR: calling {endpoint.name} failed: {e}."
    await services.meter.after("api")
    return text


NO_DATA = "ERROR: data is not available here; the user can use it in a private chat."


def _filters(filters: list[DataFilter] | None) -> list[dict[str, Any]] | None:
    return [f.model_dump() for f in filters] if filters else None


async def data_find(
    ctx: RunContext[ToolServices],
    collection: str,
    filters: list[DataFilter] | None = None,
    sort: str | None = None,
    descending: bool = True,
    limit: int = 20,
    availability: bool = False,
) -> str:
    """Find records of a collection (newest first unless sorted by a field).

    availability=true, for shared collections: what anyone has taken, public fields only.
    """
    if ctx.deps.data is None:
        return NO_DATA
    return await ctx.deps.data.find(
        collection, _filters(filters), sort, descending, limit, availability
    )


async def data_add(ctx: RunContext[ToolServices], collection: str, data: dict[str, Any]) -> str:
    """Save a new record in a collection; data maps field keys to values."""
    if ctx.deps.data is None:
        return NO_DATA
    return await ctx.deps.data.add(collection, data)


async def data_update(
    ctx: RunContext[ToolServices], collection: str, record_id: int, changes: dict[str, Any]
) -> str:
    """Change some fields of a record (by its #id); null clears an optional field."""
    if ctx.deps.data is None:
        return NO_DATA
    return await ctx.deps.data.update(collection, record_id, changes)


async def data_delete(ctx: RunContext[ToolServices], collection: str, record_id: int) -> str:
    """Delete a record (by its #id). The user confirms with a button; do not ask in words."""
    if ctx.deps.data is None:
        return NO_DATA
    return await ctx.deps.data.delete(collection, record_id)


async def data_summarize(
    ctx: RunContext[ToolServices],
    collection: str,
    metric: Literal["count", "sum", "avg", "min", "max"],
    field: str | None = None,
    filters: list[DataFilter] | None = None,
    group_by: str | None = None,
    period: Literal["day", "week", "month"] | None = None,
) -> str:
    """Count records, or sum/avg/min/max a number field, optionally grouped by a field or by
    day/week/month of the first date field."""
    if ctx.deps.data is None:
        return NO_DATA
    return await ctx.deps.data.summarize(
        collection, metric, field, _filters(filters), group_by, period
    )


DELEGATION_TOOL = "ask_bots"
SCHEDULE_TOOL = "schedule_task"
DATA_TOOLS = ("data_find", "data_add", "data_update", "data_delete", "data_summarize")

TOOLS = {
    "web_search": Tool(web_search),
    "read_url": Tool(read_url),
    "rss": Tool(read_rss),
    DELEGATION_TOOL: Tool(ask_bot),
    SCHEDULE_TOOL: Tool(schedule_task),
    API_TOOL: Tool(call_api),
    "data_find": Tool(data_find),
    "data_add": Tool(data_add),
    "data_update": Tool(data_update),
    "data_delete": Tool(data_delete),
    "data_summarize": Tool(data_summarize),
}


def build_tools(
    enabled: Iterable[str],
    *,
    apis: bool = False,
    delegation: bool = False,
    scheduling: bool = False,
    data: bool = False,
) -> list[Tool[ToolServices]]:
    """Tools for a bot's configured tool names; unknown names (e.g. future tools) are skipped.

    The rest are never configured per bot: `apis` (the bot has endpoints of its owner) adds
    call_api, `delegation` (the person has other bots) ask_bot, `scheduling` (a private
    chat) schedule_task, and `data` (the bot has collections here) the data_* tools.
    """
    implicit = (DELEGATION_TOOL, SCHEDULE_TOOL, *DATA_TOOLS)
    names = [t for t in enabled if t not in implicit]
    names += [API_TOOL] if apis else []
    names += [DELEGATION_TOOL] if delegation else []
    names += [SCHEDULE_TOOL] if scheduling else []
    names += list(DATA_TOOLS) if data else []
    return [TOOLS[name] for name in dict.fromkeys(names) if name in TOOLS]
