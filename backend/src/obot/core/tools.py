"""Tools a bot can use, independent of Telegram and the database.

Metering is delegated to a ToolMeter so plans and billing stay outside the core.
Tool failures are returned to the model as text starting with "ERROR:", so the
bot can tell the user what went wrong instead of failing the whole reply.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

import feedparser
import httpx
import trafilatura
from pydantic_ai import RunContext, Tool

from obot.core.fetch import FetchError, FetchResult, fetch

log = logging.getLogger(__name__)

MAX_TOOL_TEXT = 8000
MAX_FEED_ITEMS = 10
SEARCH_RESULTS = 5
TAVILY_URL = "https://api.tavily.com/search"


class ToolMeter(Protocol):
    async def before(self, kind: str) -> str | None:
        """Return a refusal message if the owner may not use `kind` right now."""

    async def after(self, kind: str) -> None:
        """Record one use of `kind`."""


@dataclass
class ToolServices:
    http: httpx.AsyncClient
    meter: ToolMeter
    tavily_api_key: str | None = None
    # Asks another bot of the same owner (username, question) -> answer; see ask_bot.
    delegate: Callable[[str, str], Awaitable[str]] | None = None


def truncate(text: str, limit: int = MAX_TOOL_TEXT) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[...truncated]"


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
    """Ask another bot of your owner (by @username) and get its answer.

    Use it when that bot is better suited, e.g. a translator or a specialist. Give
    it a complete, self-contained question: it cannot see this conversation.
    """
    if ctx.deps.delegate is None:
        return "ERROR: asking other bots is not available here."
    return await ctx.deps.delegate(username.strip().lstrip("@"), question)


TOOLS = {
    "web_search": Tool(web_search),
    "read_url": Tool(read_url),
    "rss": Tool(read_rss),
    "ask_bots": Tool(ask_bot),
}


def build_tools(enabled: Iterable[str]) -> list[Tool[ToolServices]]:
    """Tools for a bot's configured tool names; unknown names (e.g. future tools) are skipped."""
    return [TOOLS[name] for name in enabled if name in TOOLS]
