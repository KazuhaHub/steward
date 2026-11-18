"""Course-state mode of a request, as the request state reports it.

A verified principal behaves like the upstream HTTP modes with
``SELFHOST_COURSE_STATE=request_local`` (the default) and keeps per-principal state
only with the explicit opt-in ``per_principal``. stdio keeps its single local cache.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from steward.request_state import (
    RequestCredentials,
    clear_http_request_context,
    request_local_principal_state,
    set_http_request_active,
    set_request_credentials,
    set_request_local_principal_state,
    set_request_principal,
    uses_request_local_course_state,
)

from .conftest import OID_A, make_principal

CANVAS_URL = "https://canvas.example.test/api/v1"


async def request_as(
    fn: Callable[[], Awaitable[Any]],
    *,
    token: str,
    oid: str | None = None,
    request_local: bool = False,
) -> Any:
    """Run ``fn`` as one HTTP request (call inside its own task: own context).

    With ``oid`` it is a verified self-hosted principal (selfhost); without,
    one of the upstream HTTP modes. ``request_local`` is what the request
    middleware does for a principal: True for ``SELFHOST_COURSE_STATE=request_local``,
    False (the default here) for ``per_principal``.
    """
    set_http_request_active(True)
    if oid is not None:
        set_request_principal(make_principal(oid))
        set_request_local_principal_state(request_local)
    set_request_credentials(RequestCredentials(api_token=token, api_url=CANVAS_URL))
    try:
        return await fn()
    finally:
        clear_http_request_context()


def fresh_request(fn: Callable[[], Awaitable[Any]], **kwargs: Any) -> Awaitable[Any]:
    """A separate request: its own task, so its own ContextVar copy."""
    return asyncio.create_task(request_as(fn, **kwargs))


def _entra_request(
    fn: Callable[[], Awaitable[Any]], *, request_local: bool, token: str = "tok-a"
) -> Awaitable[Any]:
    return fresh_request(fn, token=token, oid=OID_A, request_local=request_local)


class TestMode:
    def test_the_predicate_by_mode(self):
        assert uses_request_local_course_state() is False  # stdio
        set_http_request_active(True)
        assert uses_request_local_course_state() is True  # token / access key / Easy Auth
        set_request_principal(make_principal(OID_A))
        assert uses_request_local_course_state() is False  # selfhost


class TestEntraOauthRequestLocal:
    async def test_the_predicate_is_true_for_a_verified_principal(self):
        async def probe() -> tuple[bool, bool]:
            return uses_request_local_course_state(), request_local_principal_state() is not None

        assert await _entra_request(probe, request_local=True) == (True, True)
        assert await _entra_request(probe, request_local=False) == (False, False)
