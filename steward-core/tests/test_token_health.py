"""Detecting a dead Canvas token: the single flight, the cooldown and the verdict.

``TokenHealth`` runs against the real Canvas probe and the real token store; only the
Canvas servers are faked (mock transports), so no network is used. The tests of the
Canvas client that raised the suspicion in the first place are in ``pending_proxy``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from dbbackend import make_store

from steward import audit
from steward.connectors.canvas.probe import probe_token
from steward.request_context import token_rejected_message
from steward.request_state import RequestCredentials
from steward.token_health import TokenHealth
from steward.token_store import (
    REASON_CANVAS_TOKEN_REJECTED,
    STATUS_ACTIVE,
    STATUS_INVALID,
    TokenStore,
)

from .conftest import OID_A, TENANT, acct_key, store_put
from .test_token_store import Clock as StoreClock
from .test_token_store import _ring

CANVAS_HOST = "canvas.example.edu"
API_URL = f"https://{CANVAS_HOST}/api/v1"
ACCOUNT_URL = "https://mcp.example.test/account"
TOKEN = "canvas-token-for-user-A-0123456789"
REPLACEMENT = "the-replacement-token-0123456789"
PRINCIPAL_KEY = acct_key(OID_A)
EXPECTED_MESSAGE = token_rejected_message(ACCOUNT_URL)

DEAD_HEADERS = {"WWW-Authenticate": 'Bearer realm="canvas-lms"'}
DEAD_BODY = {"errors": [{"message": "Invalid access token."}]}
PERMISSION_BODY = {
    "status": "unauthorized",
    "errors": [{"message": "user not authorized to perform that action"}],
}


class MonoClock:
    """The probe cooldown clock, advanced by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class ProbeLog:
    """The ``Authorization`` header of every probe the fake Canvas saw."""

    probes: list[str] = field(default_factory=list)


@dataclass
class Env:
    store: TokenStore
    health: TokenHealth
    mono: MonoClock
    store_clock: StoreClock
    canvas: ProbeLog
    probe: dict[str, Any]
    updated_at: int

    def set_probe(self, handler: Callable[[httpx.Request], Any]) -> None:
        """Answer the ``/users/self`` probe with ``handler`` (sync or async)."""

        def recording(request: httpx.Request) -> Any:
            self.canvas.probes.append(request.headers.get("authorization", ""))
            return handler(request)

        self.probe["handler"] = recording

    def row(self) -> Any:
        info = self.store.info(PRINCIPAL_KEY)
        assert info is not None
        return info

    def credentials(self, token: str = TOKEN) -> RequestCredentials:
        return RequestCredentials(api_token=token, api_url=API_URL)

    async def suspect(self, *, generation: int | None = None) -> str | None:
        """What the Canvas client does after a suspicious 401: ask for a verdict."""
        return await self.health.confirm_dead_token(
            PRINCIPAL_KEY, self.credentials(), self.updated_at, generation
        )


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def _raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


def _raise_connect(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def _put(store: TokenStore, token: str) -> Any:
    return store_put(
        store,
        tenant_id=TENANT,
        object_id=OID_A,
        api_token=token,
        canvas_user_id="42",
        canvas_user_name="Ada",
        entra_display_name="Ada",
        entra_upn="ada@example.test",
        canvas_host=CANVAS_HOST,
    )


@pytest.fixture
def env(tmp_path: pathlib.Path) -> Iterator[Env]:
    store_clock = StoreClock()
    store = make_store(tmp_path / "tokens.sqlite3", _ring(("k1", 1)), clock=store_clock)
    store.initialize()
    info = _put(store, TOKEN)
    mono = MonoClock()
    log = ProbeLog()
    holder: dict[str, Any] = {}

    def transport(request: httpx.Request) -> Any:
        return holder["handler"](request)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(transport))

    health = TokenHealth(
        store,
        account_url=ACCOUNT_URL,
        clock=mono,
        client_factory=factory,
        probe=probe_token,
    )

    def default_probe(request: httpx.Request) -> httpx.Response:
        log.probes.append(request.headers.get("authorization", ""))
        return _json(401, DEAD_BODY, DEAD_HEADERS)

    holder["handler"] = default_probe
    yield Env(
        store=store,
        health=health,
        mono=mono,
        store_clock=store_clock,
        canvas=log,
        probe=holder,
        updated_at=info.updated_at,
    )


class TestSuspicion:
    async def test_a_401_probe_invalidates_and_gives_the_reenroll_message(self, env: Env) -> None:
        assert await env.suspect() == EXPECTED_MESSAGE

        row = env.row()
        assert row.status == STATUS_INVALID
        assert row.invalid_reason == REASON_CANVAS_TOKEN_REJECTED
        assert row.invalid_since is not None
        assert env.canvas.probes == [f"Bearer {TOKEN}"]
        # Nothing but the encrypted token is kept, and it is still readable for a re-check.
        assert env.store.get(PRINCIPAL_KEY).api_token == TOKEN  # type: ignore[union-attr]

    @pytest.mark.parametrize("text", ["Invalid access token.", "The access token has expired."])
    async def test_error_text_alone_confirms(self, env: Env, text: str) -> None:
        env.set_probe(lambda request: _json(401, {"errors": [{"message": text}]}))
        assert await env.suspect() == EXPECTED_MESSAGE
        assert env.row().status == STATUS_INVALID
        assert len(env.canvas.probes) == 1

    async def test_a_permission_401_from_the_probe_changes_nothing(self, env: Env) -> None:
        env.set_probe(lambda request: _json(401, PERMISSION_BODY))
        assert await env.suspect() is None
        assert env.row().status == STATUS_ACTIVE

    async def test_the_probe_succeeding_means_a_permission_problem(self, env: Env) -> None:
        env.set_probe(lambda request: _json(200, {"id": 42, "name": "Ada"}))

        assert await env.suspect() is None  # the original error stays the caller's

        assert len(env.canvas.probes) == 1
        assert env.row().status == STATUS_ACTIVE

    @pytest.mark.parametrize(
        "outcome",
        [
            lambda request: _json(500, {"errors": []}),
            lambda request: _json(503, {"errors": []}),
            lambda request: _json(403, {"errors": []}),
            lambda request: _json(404, {"errors": []}),
            _raise_timeout,
            _raise_connect,
        ],
        ids=["500", "503", "403", "404", "timeout", "connect-error"],
    )
    async def test_an_unclear_probe_never_changes_state(self, env: Env, outcome: Any) -> None:
        env.set_probe(outcome)
        assert await env.suspect() is None
        assert env.row().status == STATUS_ACTIVE

    async def test_the_probe_goes_to_the_users_own_school_without_following_redirects(
        self, env: Env
    ) -> None:
        seen: list[httpx.Request] = []

        def probe(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json(401, DEAD_BODY, DEAD_HEADERS)

        env.set_probe(probe)
        await env.suspect()
        assert [str(r.url) for r in seen] == [f"{API_URL}/users/self"]
        assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"

    async def test_a_failing_probe_changes_nothing_and_does_not_raise(self, env: Env) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("transport on fire")

        env.set_probe(boom)
        assert await env.suspect() is None
        assert env.row().status == STATUS_ACTIVE


class TestSingleFlightAndCooldown:
    async def test_siblings_share_one_probe_and_one_verdict(self, env: Env) -> None:
        release = asyncio.Event()
        order: list[str] = []

        async def slow_probe(request: httpx.Request) -> httpx.Response:
            order.append("probe-started")
            await release.wait()
            order.append("probe-finished")
            return _json(401, DEAD_BODY, DEAD_HEADERS)

        env.set_probe(slow_probe)
        tasks = [asyncio.ensure_future(env.suspect()) for _ in range(6)]
        for _ in range(50):
            await asyncio.sleep(0)
            if env.health._flights:  # a probe is in flight
                break
        await asyncio.sleep(0.05)
        release.set()
        results = await asyncio.gather(*tasks)

        assert results == [EXPECTED_MESSAGE] * 6
        assert env.health.probe_count == 1
        assert len(env.canvas.probes) == 1
        assert order == ["probe-started", "probe-finished"]
        assert env.row().status == STATUS_INVALID

    async def test_a_sibling_that_asks_late_reuses_the_verdict(self, env: Env) -> None:
        first = await env.suspect()
        second = await env.suspect()
        assert first == second == EXPECTED_MESSAGE
        assert env.health.probe_count == 1 and len(env.canvas.probes) == 1

    async def test_a_second_suspicion_within_the_cooldown_reuses_the_verdict(self, env: Env) -> None:
        env.set_probe(lambda request: _json(200, {"id": 42}))

        await env.suspect()
        await env.suspect()
        assert env.health.probe_count == 1

        env.mono.now += 59
        await env.suspect()
        assert env.health.probe_count == 1

        env.mono.now += 2  # 61 s after the probe finished
        await env.suspect()
        assert env.health.probe_count == 2

    async def test_an_unclear_probe_is_not_repeated_within_the_cooldown_either(self, env: Env) -> None:
        env.set_probe(lambda request: _json(503, {}))
        await env.suspect()
        await env.suspect()
        assert env.health.probe_count == 1
        assert env.row().status == STATUS_ACTIVE

    async def test_a_restored_token_is_probed_again_instead_of_reusing_a_rejected_verdict(
        self, env: Env
    ) -> None:
        assert await env.suspect() == EXPECTED_MESSAGE
        assert env.row().status == STATUS_INVALID
        assert env.health.probe_count == 1

        # "Check again" succeeds: the row is active again with the same version.
        env.mono.now += 20
        assert env.store.restore_active(PRINCIPAL_KEY, expected_updated_at=env.updated_at)
        env.health.forget(PRINCIPAL_KEY)

        env.mono.now += 20  # still inside the 60 s cooldown of the old verdict
        env.set_probe(lambda request: _json(200, {"id": 42}))
        result = await env.suspect()

        assert env.health.probe_count == 2  # the old REJECTED verdict was not reused
        assert result is None  # a plain permission error
        assert env.row().status == STATUS_ACTIVE

    async def test_a_restored_token_that_died_again_is_marked_invalid_again(self, env: Env) -> None:
        await env.suspect()
        env.mono.now += 20
        assert env.store.restore_active(PRINCIPAL_KEY, expected_updated_at=env.updated_at)
        env.health.forget(PRINCIPAL_KEY)

        assert await env.suspect() == EXPECTED_MESSAGE
        assert env.health.probe_count == 2
        assert env.row().status == STATUS_INVALID

    async def test_forget_only_drops_the_finished_verdicts_of_that_principal(self, env: Env) -> None:
        await env.suspect()
        other = ("entra:other:principal", env.updated_at)
        env.health._flights[other] = env.health._flights[(PRINCIPAL_KEY, env.updated_at)]

        env.health.forget(PRINCIPAL_KEY)

        assert list(env.health._flights) == [other]

    async def test_a_new_token_is_probed_even_inside_the_cooldown(self, env: Env) -> None:
        env.set_probe(lambda request: _json(200, {"id": 42}))
        await env.suspect()
        assert env.health.probe_count == 1

        env.store_clock.now += 30
        info = _put(env.store, REPLACEMENT)
        await env.health.confirm_dead_token(
            PRINCIPAL_KEY, env.credentials(REPLACEMENT), info.updated_at
        )
        assert env.health.probe_count == 2

    async def test_a_token_replaced_while_the_probe_runs_is_not_invalidated(self, env: Env) -> None:
        def probe(request: httpx.Request) -> httpx.Response:
            # While Canvas is being asked about the old token, the user enrolls a new one.
            env.store_clock.now += 30
            _put(env.store, REPLACEMENT)
            return _json(401, DEAD_BODY, DEAD_HEADERS)

        env.set_probe(probe)
        result = await env.suspect()

        assert result is None  # not reported as a dead token
        assert env.row().status == STATUS_ACTIVE
        assert env.store.get(PRINCIPAL_KEY).api_token == REPLACEMENT  # type: ignore[union-attr]

    async def test_a_cancelled_leader_does_not_strand_the_waiters(self, env: Env) -> None:
        started = asyncio.Event()

        async def hanging_probe(request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.sleep(30)
            return _json(401, DEAD_BODY, DEAD_HEADERS)  # pragma: no cover

        env.set_probe(hanging_probe)
        leader = asyncio.ensure_future(env.suspect())
        await asyncio.wait_for(started.wait(), 5)
        waiter = asyncio.ensure_future(env.suspect())
        await asyncio.sleep(0.05)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        assert await asyncio.wait_for(waiter, 5) is None
        assert env.row().status == STATUS_ACTIVE


class TestGenerations:
    async def test_the_verdict_is_kept_per_credential_generation(self, env: Env) -> None:
        env.set_probe(lambda request: _json(200, {"id": 42}))
        await env.suspect(generation=1)
        await env.suspect(generation=1)
        assert env.health.probe_count == 1
        await env.suspect(generation=2)  # a new credential is asked about afresh
        assert env.health.probe_count == 2

    async def test_a_rejection_about_a_stale_generation_invalidates_nothing(self, env: Env) -> None:
        current = env.row().credential_generation
        assert await env.suspect(generation=current + 5) is None
        assert env.row().status == STATUS_ACTIVE

    async def test_a_rejection_of_the_current_generation_raises_it(self, env: Env) -> None:
        current = env.row().credential_generation
        assert await env.suspect(generation=current) == EXPECTED_MESSAGE
        assert env.row().status == STATUS_INVALID
        assert env.row().credential_generation == current + 1


class TestVerifiedBookkeeping:
    async def test_last_verified_is_written_at_most_once_per_interval(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writes: list[str] = []
        real = env.store.mark_verified

        def counting(*args: Any, **kwargs: Any) -> None:
            writes.append("write")
            real(*args, **kwargs)

        monkeypatch.setattr(env.store, "mark_verified", counting)

        for _ in range(8):
            await env.health.note_success(PRINCIPAL_KEY)
        assert writes == ["write"]

        env.mono.now += 601
        await env.health.note_success(PRINCIPAL_KEY)
        assert writes == ["write", "write"]

    async def test_a_success_updates_the_stored_time(self, env: Env) -> None:
        env.store_clock.now += 1200
        await env.health.note_success(PRINCIPAL_KEY)
        assert env.row().last_verified_at == int(env.store_clock.now)

    async def test_a_successful_probe_counts_as_verification(self, env: Env) -> None:
        env.set_probe(lambda request: _json(200, {"id": 42}))
        env.store_clock.now += 1200
        await env.suspect()
        assert env.row().last_verified_at == int(env.store_clock.now)

    async def test_a_failing_bookkeeping_write_never_fails_the_caller(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(env.store, "mark_verified", boom)
        env.set_probe(lambda request: _json(200, {"id": 42}))
        assert await env.suspect() is None
        assert env.row().status == STATUS_ACTIVE


class TestAudit:
    @pytest.fixture
    def lines(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        recorded: list[str] = []

        class Recorder:
            def info(self, line: str) -> None:
                recorded.append(line)

        monkeypatch.setattr(audit, "_audit_logger", Recorder())
        monkeypatch.setattr(audit, "_access_events_enabled", True)
        return recorded

    async def test_invalidation_is_audited_with_the_principal_and_reason_only(
        self, env: Env, lines: list[str]
    ) -> None:
        await env.suspect()
        await env.suspect()  # the cooldown verdict: no second event

        events = [json.loads(line) for line in lines if '"canvas_token"' in line]
        assert len(events) == 1
        event = events[0]
        assert event["action"] == "invalidated"
        assert event["reason"] == "canvas_token_rejected"
        assert event["principal"] == PRINCIPAL_KEY
        blob = json.dumps(event)
        assert TOKEN not in blob and "ada@example.test" not in blob and CANVAS_HOST not in blob

    async def test_nothing_is_audited_when_the_audit_log_is_off(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[str] = []

        class Recorder:
            def info(self, line: str) -> None:
                recorded.append(line)

        monkeypatch.setattr(audit, "_audit_logger", Recorder())
        monkeypatch.setattr(audit, "_access_events_enabled", False)
        await env.suspect()
        assert recorded == []
        assert env.row().status == STATUS_INVALID

    async def test_a_permission_401_writes_no_token_event(self, env: Env, lines: list[str]) -> None:
        env.set_probe(lambda request: _json(401, PERMISSION_BODY))
        await env.suspect()
        assert not [line for line in lines if '"canvas_token"' in line]
