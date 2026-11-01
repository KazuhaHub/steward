"""Telling a dead Canvas token from a permission error.

A 401 from Canvas does not always mean the token is dead: Canvas also answers
401 when a live token lacks permission for a resource. Steward confirms a
suspected dead token with a separate ``GET /users/self`` and only a 401 that
says the token is invalid, expired or revoked counts. "Insufficient scope"
never does.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx

from steward.connectors import OK, REJECTED, UNKNOWN, Verdict
from steward.request_state import RequestCredentials

_DEAD_TEXT_RE = re.compile(r"invalid access token|expired", re.IGNORECASE)
_INSUFFICIENT_SCOPE_RE = re.compile(r"insufficient[ _]scope", re.IGNORECASE)
_INVALID_TOKEN_HEADER_RE = re.compile(r"invalid_token", re.IGNORECASE)
_MAX_BODY_CHARS = 4096

USER_AGENT = "mcp-steward (https://github.com/KazuhaHub/steward)"


def auth_headers(api_token: str) -> dict[str, str]:
    """The Canvas auth and User-Agent headers for a token."""
    return {"Authorization": f"Bearer {api_token}", "User-Agent": USER_AGENT}


def _insufficient_scope(response: httpx.Response, body: str) -> bool:
    """A 401 that says the token is fine but lacks scope: never a dead token."""
    return bool(
        _INSUFFICIENT_SCOPE_RE.search(response.headers.get("www-authenticate", ""))
        or _INSUFFICIENT_SCOPE_RE.search(body)
    )


def says_token_is_dead(response: httpx.Response) -> bool:
    """A 401 whose headers or body say the token is invalid, expired or revoked.

    ``WWW-Authenticate`` alone is not enough (Canvas sends it on any 401), and
    "insufficient scope" overrides everything.
    """
    if response.status_code != 401:
        return False
    try:
        body = response.text[:_MAX_BODY_CHARS]
    except httpx.ResponseNotRead:
        return False
    if _insufficient_scope(response, body):
        return False
    return bool(
        _INVALID_TOKEN_HEADER_RE.search(response.headers.get("www-authenticate", ""))
        or _DEAD_TEXT_RE.search(body)
    )


async def probe_token(
    credentials: RequestCredentials,
    *,
    client_factory: Callable[[], httpx.AsyncClient],
    timeout: float,
) -> Verdict:
    """``GET /users/self`` with the token; the verdict of that one answer."""
    url = credentials.api_url.rstrip("/") + "/users/self"
    headers = auth_headers(credentials.api_token)
    headers["Accept"] = "application/json"
    try:
        async with client_factory() as client:
            response = await client.get(
                url, headers=headers, timeout=timeout, follow_redirects=False
            )
    except (httpx.HTTPError, ValueError):
        return UNKNOWN
    if response.status_code == 401:
        return REJECTED if says_token_is_dead(response) else UNKNOWN
    if response.status_code == 200:
        return OK
    return UNKNOWN
