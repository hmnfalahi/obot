"""Fetching user-chosen URLs safely.

Bots are configured by users, so every URL is untrusted: only public hosts are
reachable (no localhost, private networks or cloud metadata endpoints), on every
redirect hop, with size and time limits.

Known limitation: the address is checked before httpx resolves it again, so a
DNS-rebinding host could still slip through. Pinning the resolved IP needs a
custom transport and is tracked in docs/SPEC.md.
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 2_000_000
TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; ObotFetcher/1.0; +https://t.me/obotfactorybot)"


class FetchError(Exception):
    """A fetch failed in a way the bot should explain to the user."""


@dataclass(frozen=True)
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content_type: str
    body: bytes


def normalize_url(url: str) -> str:
    """Models often pass bare hosts like 'tgju.org/page'; assume https for those."""
    url = url.strip()
    if "://" not in url:
        url = f"https://{url.lstrip('/')}"
    return url


async def ensure_public_host(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"only http and https URLs are supported, not '{parts.scheme}'")
    if not parts.hostname:
        raise FetchError("the URL has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise FetchError(f"the host {parts.hostname} could not be found") from e
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise FetchError(f"the host {parts.hostname} is not a public address")


async def fetch(client: httpx.AsyncClient, url: str, max_bytes: int = MAX_BYTES) -> FetchResult:
    url = normalize_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        await ensure_public_host(url)
        try:
            async with client.stream("GET", url, follow_redirects=False) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code >= 400:
                    raise FetchError(
                        f"the site answered with HTTP {response.status_code}"
                        + (
                            " (access denied, it may block our server's region)"
                            if response.status_code in (401, 403, 451)
                            else ""
                        )
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise FetchError(f"the page is larger than {max_bytes // 1_000_000} MB")
                return FetchResult(
                    url=str(response.url),
                    status=response.status_code,
                    content_type=response.headers.get("content-type", "").lower(),
                    body=bytes(body),
                )
        except httpx.TimeoutException as e:
            raise FetchError("the site did not respond in time") from e
        except httpx.HTTPError as e:
            raise FetchError(f"the site could not be reached ({type(e).__name__})") from e
    raise FetchError("the site redirected too many times")


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "fa,en;q=0.8"},
    )
