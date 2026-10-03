import asyncio
import socket

import httpx
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from obot.core.agent import BotProfile, generate_reply
from obot.core.fetch import FetchError, FetchResult, ensure_public_host, fetch
from obot.core.tools import ToolServices, build_tools, page_text

PAGE = b"""<html><body><nav>menu</nav><article><h1>Dollar price</h1>
<p>The free market dollar rate today.</p>
<table><tr><td>USD</td><td>1,020,000</td></tr></table></article></body></html>"""

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>News</title>
<item><title>First</title><link>https://example.com/1</link></item>
<item><title>Second</title><link>https://example.com/2</link></item>
</channel></rss>"""


class FakeMeter:
    def __init__(self, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.used: list[str] = []

    async def before(self, kind: str) -> str | None:
        return self.refuse

    async def after(self, kind: str) -> None:
        self.used.append(kind)


@pytest.fixture
async def public_dns(monkeypatch):
    """Resolve every host to a public address so tests need no network."""

    async def fake_getaddrinfo(host, port, **kwargs):
        address = {"internal.test": "10.0.0.5"}.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)


def client_for(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://10.1.2.3/",
        "file:///etc/passwd",
    ],
)
async def test_private_and_non_http_urls_are_blocked(url) -> None:
    with pytest.raises(FetchError):
        await ensure_public_host(url)


async def test_redirect_to_private_host_is_blocked(public_dns) -> None:
    client = client_for(
        {"https://site.test/": httpx.Response(302, headers={"location": "http://internal.test/"})}
    )

    with pytest.raises(FetchError, match="not a public address"):
        await fetch(client, "https://site.test/")


async def test_forbidden_mentions_region_blocking(public_dns) -> None:
    client = client_for({"https://site.test/": httpx.Response(403)})

    with pytest.raises(FetchError, match="block our server's region"):
        await fetch(client, "https://site.test/")


async def test_oversized_page_is_rejected(public_dns) -> None:
    client = client_for({"https://site.test/": httpx.Response(200, content=b"x" * 100)})

    with pytest.raises(FetchError, match="larger than"):
        await fetch(client, "https://site.test/", max_bytes=10)


def test_page_text_keeps_tables_and_drops_navigation() -> None:
    text = page_text(FetchResult("https://site.test/", 200, "text/html", PAGE))

    assert "1,020,000" in text
    assert "Dollar price" in text


def test_page_text_rejects_binary() -> None:
    with pytest.raises(FetchError, match="not text"):
        page_text(FetchResult("https://site.test/", 200, "image/png", b"\x89PNG"))


def test_build_tools_skips_unknown_names() -> None:
    names = [tool.name for tool in build_tools(["web_search", "memory", "rss"])]

    assert names == ["web_search", "read_rss"]


def call_tool_then_answer(tool_name: str, args: dict):
    """A FunctionModel that calls one tool, then answers with the tool's output."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [
            part.content for m in messages for part in m.parts if part.part_kind == "tool-return"
        ]
        if returns:
            return ModelResponse(parts=[TextPart(str(returns[-1]))])
        return ModelResponse(parts=[ToolCallPart(tool_name, args)])

    return FunctionModel(respond)


async def run_tool(tool: str, args: dict, services: ToolServices) -> str:
    reply = await generate_reply(
        call_tool_then_answer(tool, args),
        BotProfile("T"),
        [],
        "go",
        tools=build_tools(["web_search", "read_url", "rss"]),
        services=services,
    )
    return reply.text


async def test_read_url_tool_is_metered(public_dns) -> None:
    meter = FakeMeter()
    client = client_for(
        {
            "https://site.test/": httpx.Response(
                200, content=PAGE, headers={"content-type": "text/html"}
            )
        }
    )

    text = await run_tool("read_url", {"url": "https://site.test/"}, ToolServices(client, meter))

    assert "1,020,000" in text
    assert meter.used == ["fetch"]


async def test_rss_tool_lists_items(public_dns) -> None:
    meter = FakeMeter()
    client = client_for({"https://site.test/feed": httpx.Response(200, content=FEED)})

    text = await run_tool(
        "read_rss", {"url": "https://site.test/feed"}, ToolServices(client, meter)
    )

    assert "First" in text and "https://example.com/2" in text


async def test_web_search_respects_quota_refusal() -> None:
    meter = FakeMeter(refuse="the owner's daily web search limit is reached")
    services = ToolServices(client_for({}), meter, tavily_api_key="key")

    text = await run_tool("web_search", {"query": "news"}, services)

    assert text.startswith("ERROR: the owner's daily web search limit")
    assert meter.used == []


async def test_web_search_formats_results() -> None:
    meter = FakeMeter()
    results = {"results": [{"title": "Oil", "url": "https://o.test", "content": "Brent $100"}]}
    client = client_for({"https://api.tavily.com/search": httpx.Response(200, json=results)})

    text = await run_tool("web_search", {"query": "oil"}, ToolServices(client, meter, "key"))

    assert "Brent $100" in text
    assert meter.used == ["search"]
