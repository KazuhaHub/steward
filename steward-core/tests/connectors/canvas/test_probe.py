"""Telling a dead Canvas token from a permission error, and the probe built on it."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from steward.connectors import OK, REJECTED, UNKNOWN
from steward.connectors.canvas.probe import probe_token, says_token_is_dead
from steward.request_state import RequestCredentials

API_URL = "https://canvas.example.edu/api/v1"
TOKEN = "canvas-token-for-user-A-0123456789"
DEAD_BODY = {"errors": [{"message": "Invalid access token."}]}
DEAD_HEADERS = {"WWW-Authenticate": 'Bearer realm="canvas-lms"'}
SCOPE_BODY = {"errors": [{"message": "Insufficient scopes on access token."}]}


def response(status: int, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {}, headers=headers)


class TestSaysTokenIsDead:
    @pytest.mark.parametrize(
        ("headers", "body"),
        [
            ({"WWW-Authenticate": 'Bearer error="invalid_token"'}, {}),
            ({}, {"errors": [{"message": "Invalid access token."}]}),
            ({}, {"errors": [{"message": "The access token expired."}]}),
            ({}, {"errors": [{"message": "The access token has expired."}]}),
        ],
    )
    def test_each_dead_token_marker_says_dead(self, headers, body) -> None:
        assert says_token_is_dead(response(401, body, headers)) is True

    def test_a_plain_challenge_header_alone_is_not_dead(self) -> None:
        assert says_token_is_dead(response(401, {}, {"WWW-Authenticate": "Bearer"})) is False
        assert says_token_is_dead(response(401, {}, DEAD_HEADERS)) is False

    @pytest.mark.parametrize(
        ("headers", "body"),
        [
            ({"WWW-Authenticate": 'Bearer error="insufficient_scope"'}, SCOPE_BODY),
            ({}, SCOPE_BODY),
            # Insufficient scope overrides a dead-token marker in the same answer.
            (
                {"WWW-Authenticate": 'Bearer error="invalid_token", error_description="insufficient_scope"'},
                {},
            ),
            ({}, {"errors": [{"message": "Insufficient scopes: the token has expired scope"}]}),
            ({"WWW-Authenticate": "Bearer realm=canvas"}, {"errors": [{"message": "user authorization required"}]}),
            ({}, {}),
        ],
    )
    def test_a_401_that_is_not_about_a_dead_token_is_not_dead(self, headers, body) -> None:
        assert says_token_is_dead(response(401, body, headers)) is False

    @pytest.mark.parametrize("status", [200, 403, 404, 500, 503])
    def test_only_a_401_can_say_dead(self, status: int) -> None:
        assert says_token_is_dead(response(status, DEAD_BODY, {"WWW-Authenticate": 'Bearer error="invalid_token"'})) is False

    def test_an_unread_streaming_body_is_not_dead(self) -> None:
        streaming = httpx.Response(401, stream=httpx.AsyncByteStream())
        assert says_token_is_dead(streaming) is False


def credentials() -> RequestCredentials:
    return RequestCredentials(api_token=TOKEN, api_url=API_URL)


async def probe(handler) -> Any:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(recording))

    verdict = await probe_token(credentials(), client_factory=factory, timeout=5)
    return verdict, seen


class TestProbeToken:
    async def test_it_asks_the_users_own_school_with_the_token_and_no_redirects(self) -> None:
        verdict, seen = await probe(lambda request: response(200, {"id": 42}))
        assert verdict == OK
        assert [str(r.url) for r in seen] == [f"{API_URL}/users/self"]
        assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
        assert seen[0].headers["accept"] == "application/json"

    async def test_a_trailing_slash_in_the_api_url_is_tolerated(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return response(200, {"id": 1})

        verdict = await probe_token(
            RequestCredentials(api_token=TOKEN, api_url=API_URL + "/"),
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            timeout=5,
        )
        assert verdict == OK and str(seen[0].url) == f"{API_URL}/users/self"

    async def test_a_200_is_ok(self) -> None:
        assert (await probe(lambda request: response(200, {"id": 42})))[0] == OK

    @pytest.mark.parametrize(
        ("headers", "body"),
        [
            ({"WWW-Authenticate": 'Bearer error="invalid_token"'}, {}),
            ({}, {"errors": [{"message": "Invalid access token."}]}),
            ({}, {"errors": [{"message": "The access token expired."}]}),
        ],
    )
    async def test_a_dead_401_is_rejected(self, headers, body) -> None:
        assert (await probe(lambda request: response(401, body, headers)))[0] == REJECTED

    @pytest.mark.parametrize(
        ("headers", "body"),
        [
            ({"WWW-Authenticate": 'Bearer error="insufficient_scope"'}, SCOPE_BODY),
            ({}, SCOPE_BODY),
            ({"WWW-Authenticate": "Bearer realm=canvas"}, {"errors": [{"message": "user authorization required"}]}),
            ({}, {}),
        ],
    )
    async def test_any_other_401_is_unknown(self, headers, body) -> None:
        assert (await probe(lambda request: response(401, body, headers)))[0] == UNKNOWN

    @pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
    async def test_other_statuses_are_unknown(self, status: int) -> None:
        assert (await probe(lambda request: response(status, DEAD_BODY, DEAD_HEADERS)))[0] == UNKNOWN

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ReadTimeout("slow"),
            httpx.ConnectTimeout("slow"),
            httpx.ConnectError("refused"),
        ],
        ids=["read-timeout", "connect-timeout", "connect-error"],
    )
    async def test_a_timeout_or_connection_error_is_unknown(self, error: Exception) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise error

        assert (await probe(raising))[0] == UNKNOWN

    async def test_a_redirect_is_not_followed_and_is_unknown(self) -> None:
        redirect = response(302, {}, {"Location": "https://elsewhere.example.test/users/self"})

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: redirect))

        verdict = await probe_token(credentials(), client_factory=factory, timeout=5)
        assert verdict == UNKNOWN
