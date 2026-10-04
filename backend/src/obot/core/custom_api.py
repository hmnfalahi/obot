"""Owner-defined HTTP endpoints a bot may call (the call_api tool).

The owner fixes the address, method and secret headers; the model only fills
placeholders in the address, adds query parameters and, for methods with a
body, sends JSON. Secret headers never reach the model. Like read_url, only
public hosts are reachable; redirects are not followed, so the headers cannot
be carried to another host.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from obot.core.fetch import FetchError, ensure_public_host, truncate

log = logging.getLogger(__name__)

API_TOOL = "call_api"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
BODY_METHODS = ("POST", "PUT", "PATCH")
NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,39})\}")
HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
# Set by the HTTP client itself; letting owners set them breaks requests or routing.
RESERVED_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}
MAX_HEADERS = 10
MAX_HEADER_VALUE = 2000
MAX_RESPONSE_BYTES = 1_000_000


class InvalidEndpoint(ValueError):
    """The owner's endpoint definition is not acceptable."""


@dataclass(frozen=True)
class Endpoint:
    name: str
    description: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)


def placeholders(url: str) -> list[str]:
    return list(dict.fromkeys(PLACEHOLDER.findall(url)))


def validate_endpoint(name: str, method: str, url: str, headers: dict[str, str]) -> None:
    if not NAME.fullmatch(name):
        raise InvalidEndpoint("name: lowercase latin letters, digits and _, starting with a letter")
    if method not in METHODS:
        raise InvalidEndpoint(f"method must be one of {', '.join(METHODS)}")
    # Placeholders may stand in the path or query, never in the host.
    parts = urlsplit(PLACEHOLDER.sub("x", url))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidEndpoint("the address must start with https:// or http:// and name a host")
    if "{" in urlsplit(url).netloc:
        raise InvalidEndpoint("placeholders like {id} cannot be in the host")
    if "{" in parts.geturl() or "}" in parts.geturl():
        raise InvalidEndpoint("placeholders look like {name}: latin letters, digits and _")
    if len(headers) > MAX_HEADERS:
        raise InvalidEndpoint(f"at most {MAX_HEADERS} headers")
    for key, value in headers.items():
        if not HEADER_NAME.fullmatch(key) or key.lower() in RESERVED_HEADERS:
            raise InvalidEndpoint(f"header '{key}' is not allowed")
        if len(value) > MAX_HEADER_VALUE or "\n" in value or "\r" in value:
            raise InvalidEndpoint(f"the value of header '{key}' is not allowed")


def fill(url: str, params: dict[str, Any]) -> str:
    missing = [p for p in placeholders(url) if str(params.get(p, "")).strip() == ""]
    if missing:
        raise FetchError(f"missing values for {', '.join(missing)}")
    return PLACEHOLDER.sub(lambda m: quote(str(params[m.group(1)]).strip(), safe=""), url)


def endpoints_note(endpoints: dict[str, Endpoint]) -> str | None:
    """Tells the model which endpoints exist; addresses stay hidden (they may hold keys)."""
    if not endpoints:
        return None
    lines = []
    for e in endpoints.values():
        params = placeholders(e.url)
        needs = f"; path_params: {', '.join(params)}" if params else ""
        body = "; takes a JSON body" if e.method in BODY_METHODS else ""
        lines.append(f"- {e.name} ({e.method}{needs}{body}): {e.description}")
    return "APIs of your owner you can call with call_api:\n" + "\n".join(lines)


async def request(
    http: httpx.AsyncClient,
    endpoint: Endpoint,
    path_params: dict[str, Any],
    query: dict[str, Any],
    body: Any,
) -> str:
    """Call the endpoint; returns the response as text for the model (raises FetchError)."""
    # Merged into the query the owner's address may already have (params= would replace it).
    url = str(
        httpx.URL(fill(endpoint.url, path_params)).copy_merge_params(
            {k: str(v) for k, v in query.items()}
        )
    )
    await ensure_public_host(url)
    send_body = endpoint.method in BODY_METHODS and body is not None
    try:
        async with http.stream(
            endpoint.method,
            url,
            json=body if send_body else None,
            headers=endpoint.headers,
            follow_redirects=False,
        ) as response:
            if response.is_redirect:
                raise FetchError(f"the API redirected (HTTP {response.status_code}), not followed")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise FetchError("the response is larger than 1 MB")
    except httpx.TimeoutException as e:
        raise FetchError("the API did not respond in time") from e
    except httpx.HTTPError as e:
        raise FetchError(f"the API could not be reached ({type(e).__name__})") from e

    text = data.decode("utf-8", errors="replace")
    if "json" in response.headers.get("content-type", ""):
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except ValueError:
            pass
    if response.status_code >= 400:
        raise FetchError(f"the API answered HTTP {response.status_code}: {truncate(text, 1000)}")
    return f"HTTP {response.status_code}\n{truncate(text)}"
