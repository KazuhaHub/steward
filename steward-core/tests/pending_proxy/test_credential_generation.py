"""Credential generations: state is bound to the Canvas credential's lifecycle.

A principal's credential generation is raised by the token store whenever the
stored Canvas credential is saved, replaced, removed, found dead or restored, or the
principal is disabled or enabled. Course caches, course-policy decisions,
pseudonyms, health verdicts, pending write confirmations and background refreshes
are all bound to it, so nothing learned under one credential is used under another,
even for the same Entra identity at the same school.
"""

from __future__ import annotations

import asyncio
import base64
import pathlib
import threading
from typing import Any

import httpx
import pytest
from dbbackend import make_store as backend_store
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken

from canvas_mcp.core import anonymization, cache, course_policy
from steward.request_state import (
    RequestCredentials,
    RequestTokenState,
    current_principal_key,
    get_request_credential_generation,
    get_request_credentials,
    get_request_token_state,
    known_credential_generation,
    note_credential_generation,
    register_credential_purge_listener,
    request_credential_is_stale,
    reset_credential_generations,
    run_pending_credential_purges,
    set_http_request_active,
    set_request_credential_generation,
    set_request_credentials,
    set_request_principal,
    set_request_token_state,
)
from steward import tool_gate
from steward.db.repos import Repositories
from steward.mcp_state import MCP_READ, McpRead
from steward.request_context import SelfhostRequestContextMiddleware
from steward.schools import School, SchoolPolicy
from steward.token_health import TokenHealth
from steward.token_store import (
    DISABLE_REASON_OPERATOR,
    OPERATOR,
    REASON_CANVAS_TOKEN_REJECTED,
    STATUS_ACTIVE,
    STATUS_INVALID,
    Keyring,
    TokenStore,
)
from steward.tool_gate import SelfhostCredentialGate
from canvas_mcp.core.write_confirmation import ConfirmationGuard
from canvas_mcp.tools import discussions

from ..conftest import (
    OID_A,
    OID_B,
    TENANT,
    acct_key,
    make_principal,
    store_put,
)
from ..test_request_context import (
    ACCOUNT_URL,
    GRANT,
    Probe,
    _claims,
    _middleware,
    _run,
    _user,
    native_snapshot,
)

KEY_A = acct_key(OID_A)
KEY_B = acct_key(OID_B)
TOKEN_1 = "canvas-token-first-0123456789abcdef"
TOKEN_2 = "canvas-token-second-0123456789abcdef"
HOST = "canvas.example.edu"
API_URL = f"https://{HOST}/api/v1"






# ---------------------------------------------------------------------- the store




# ---------------------------------------------------------------- the process registry




# --------------------------------------------------------------------------- the key






# ------------------------------------------------------------------- course caches


COURSES_A = [{"id": 101, "course_code": "ICS 33", "name": "Intermediate Python"}]
COURSES_B = [{"id": 909, "course_code": "BIO 1", "name": "Biology"}]


class TestCourseCache:
    async def test_a_replaced_token_never_sees_the_old_courses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lists = {TOKEN_1: COURSES_A, TOKEN_2: COURSES_B}
        reads: list[str] = []

        async def fake_list(path: str, params: Any = None) -> Any:
            creds = get_request_credentials()
            assert creds is not None
            reads.append(creds.api_token)
            return lists[creds.api_token]

        monkeypatch.setattr(cache, "fetch_all_paginated_results", fake_list)
        as_user(OID_A, 1, TOKEN_1)
        assert await cache.resolve_numeric_course_id("ICS 33") == ("101", None)
        assert await cache.resolve_numeric_course_id("ICS 33") == ("101", None)
        assert reads == [TOKEN_1]  # the second lookup came from this generation's cache

        # Same Entra identity, same host: now another Canvas user's token.
        as_user(OID_A, 2, TOKEN_2)
        numeric, error = await cache.resolve_numeric_course_id("ICS 33")
        assert numeric is None and error is not None  # the old alias is gone
        assert await cache.resolve_numeric_course_id("BIO 1") == ("909", None)
        assert reads == [TOKEN_1, TOKEN_2]

        # Only the new token's list is cached for the new generation.
        assert cache.current_cache_state().records == cache.course_records(COURSES_B)

    def test_the_purge_listener_drops_every_generation_of_a_principal(self) -> None:
        for generation in (1, 2):
            as_user(OID_A, generation)
            cache.current_cache_state().code_to_id["X"] = "1"
        as_user(OID_B, 1)
        cache.current_cache_state().code_to_id["Y"] = "2"
        note_credential_generation(KEY_A, 1)
        note_credential_generation(KEY_A, 3)
        run_pending_credential_purges()
        assert [k for k in cache._STATES if k.startswith(KEY_A)] == []
        assert [k for k in cache._STATES if k.startswith(KEY_B)] != []

    async def test_a_refresh_that_finishes_after_the_token_was_replaced_publishes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_list(path: str, params: Any = None) -> Any:
            started.set()
            await release.wait()
            return COURSES_A

        monkeypatch.setattr(cache, "fetch_all_paginated_results", slow_list)
        as_user(OID_A, 1, TOKEN_1)
        old_state = cache.current_cache_state()
        task = asyncio.get_running_loop().create_task(cache.refresh_course_cache())
        await started.wait()

        # The user replaces the token while the old refresh is still reading.
        note_credential_generation(KEY_A, 1)
        note_credential_generation(KEY_A, 2)
        release.set()
        assert await task is False

        assert old_state.records == [] and old_state.last_refresh_at is None
        as_user(OID_A, 2, TOKEN_2)
        fresh = cache.current_cache_state()
        assert fresh is not old_state
        assert fresh.records == [] and fresh.code_to_id == {}

    async def test_a_request_on_a_replaced_token_cannot_fill_the_new_cache(
        self,
    ) -> None:
        as_user(OID_A, 1)
        note_credential_generation(KEY_A, 1)
        note_credential_generation(KEY_A, 2)  # replaced while this request runs
        cache.remember_course_code("101", "ICS 33")
        state = cache.current_cache_state()
        state.code_to_id["ICS 33"] = "101"  # throwaway: nobody else can see it
        assert cache._STATES == {}  # nothing was registered for the replaced generation
        as_user(OID_A, 2)
        assert cache.current_cache_state().code_to_id == {}

    async def test_the_upstream_request_local_modes_are_unaffected(self) -> None:
        set_http_request_active(True)
        set_request_credentials(RequestCredentials(api_token=TOKEN_1, api_url=API_URL))
        first = cache.current_cache_state()
        assert first is not cache.current_cache_state()  # still a throwaway every time
        assert request_credential_is_stale() is False


# ------------------------------------------------------------ the other caches


class TestPolicyCache:
    async def test_a_cached_allow_is_not_served_to_a_token_with_fewer_permissions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bodies = {
            TOKEN_1: {"syllabus_body": "agent_writes: allow\nnote: teacher's view"},
            TOKEN_2: {"syllabus_body": "agent_writes: deny\nnote: reduced permissions"},
        }
        reads: list[str] = []

        async def fake_request(method: str, endpoint: str, **kwargs: Any) -> Any:
            creds = get_request_credentials()
            assert creds is not None
            reads.append(creds.api_token)
            return bodies[creds.api_token]

        monkeypatch.setattr(course_policy, "make_canvas_request", fake_request)
        as_user(OID_A, 1, TOKEN_1)
        assert (await course_policy.get_course_policy(7)).allow_writes is True
        assert (await course_policy.get_course_policy(7)).allow_writes is True
        assert reads == [TOKEN_1]

        as_user(OID_A, 2, TOKEN_2)  # same identity, same school, reduced permissions
        replaced = await course_policy.get_course_policy(7)
        assert replaced.allow_writes is False and replaced.note == "reduced permissions"
        assert reads == [TOKEN_1, TOKEN_2]

    def test_the_purge_listener_drops_the_decisions_of_every_generation(self) -> None:
        for principal, generation in ((OID_A, 1), (OID_A, 2), (OID_B, 1)):
            as_user(principal, generation)
            course_policy._policy_cache[(current_principal_key(), "7")] = (
                float("inf"),
                None,
            )  # type: ignore[assignment]
        note_credential_generation(KEY_A, 1)
        note_credential_generation(KEY_A, 5)
        run_pending_credential_purges()
        assert [
            k[0] for k in course_policy._policy_cache if k[0].startswith(KEY_A)
        ] == []
        assert [
            k[0] for k in course_policy._policy_cache if k[0].startswith(KEY_B)
        ] != []


class TestPseudonymsAndHints:
    def test_pseudonym_maps_are_per_generation_and_purged(self) -> None:
        as_user(OID_A, 1)
        anonymization.generate_anonymous_id(5)
        assert anonymization.get_anonymization_stats()["total_anonymized_ids"] == 1
        as_user(OID_A, 2)
        assert anonymization.get_anonymization_stats()["total_anonymized_ids"] == 0
        anonymization.generate_anonymous_id(6)
        note_credential_generation(KEY_A, 1)
        note_credential_generation(KEY_A, 2)
        run_pending_credential_purges()
        assert [
            k for k in anonymization._anonymization_cache if k.startswith(KEY_A)
        ] == []

    def test_discussion_hints_learned_under_one_token_are_not_used_under_the_next(
        self,
    ) -> None:
        as_user(OID_A, 1)
        discussions._unservable_topics[(current_principal_key(), "/courses/1", "5")] = (
            float("inf")
        )
        assert discussions._is_known_unservable("/courses/1", 5)
        as_user(OID_A, 2)
        assert not discussions._is_known_unservable("/courses/1", 5)
        as_user(OID_A, 1)
        note_credential_generation(KEY_A, 1)
        note_credential_generation(KEY_A, 2)
        run_pending_credential_purges()
        assert discussions._unservable_topics == {}


# ------------------------------------------------------- pending write confirmations


class TestPendingConfirmations:
    def preview(self, guard: ConfirmationGuard) -> tuple[str, str]:
        fingerprint = guard.fingerprint("delete_page", "course-1", "page-9")
        return guard.issue(fingerprint), fingerprint

    def test_a_preview_is_redeemable_while_the_credential_is_unchanged(self) -> None:
        guard = ConfirmationGuard()
        as_user(OID_A, 4)
        token, fingerprint = self.preview(guard)
        as_user(OID_A, 4)  # a later request of the same user
        assert (
            guard.check(token, guard.fingerprint("delete_page", "course-1", "page-9"))
            is None
        )
        assert fingerprint == guard.fingerprint("delete_page", "course-1", "page-9")

    @pytest.mark.parametrize(
        "after",
        [
            pytest.param({"token": TOKEN_1}, id="the-same-token-enrolled-again"),
            pytest.param({"token": TOKEN_2}, id="another-canvas-user-at-the-same-host"),
        ],
    )
    def test_a_preview_does_not_survive_re_enrollment_at_the_same_school(
        self, after: dict[str, str]
    ) -> None:
        guard = ConfirmationGuard()
        as_user(OID_A, 4)
        token, _ = self.preview(guard)
        as_user(OID_A, 5, after["token"])
        fingerprint = guard.fingerprint("delete_page", "course-1", "page-9")
        refusal = guard.check(token, fingerprint)
        assert refusal is not None and "does not match" in refusal
        assert "Canvas connection changed" in refusal
        # The mismatch burned the token: going back does not bring it back.
        as_user(OID_A, 4)
        again = guard.check(
            token, guard.fingerprint("delete_page", "course-1", "page-9")
        )
        assert again is not None and "already used" in again

    def test_removal_disablement_and_death_also_void_a_preview(
        self, tmp_path: pathlib.Path
    ) -> None:
        store = make_store(tmp_path)
        guard = ConfirmationGuard()
        for change in (
            lambda: store.delete(KEY_A),
            lambda: store.disable_principal(
                KEY_A, actor=OPERATOR, reason=DISABLE_REASON_OPERATOR
            ),
            lambda: store.enable_principal(KEY_A, actor=OPERATOR),
            lambda: store.mark_invalid(KEY_A, reason=REASON_CANVAS_TOKEN_REJECTED),
        ):
            put(store) if store.get(KEY_A) is None else None
            as_user(OID_A, store.credential_generation(KEY_A))
            token, _ = self.preview(guard)
            change()
            as_user(OID_A, store.credential_generation(KEY_A))
            assert guard.check(
                token, guard.fingerprint("delete_page", "course-1", "page-9")
            )


# ---------------------------------------------------------------------- token health








# ---------------------------------------------------------------- the request context








# ------------------------------------------------------------------------- the gate












# Moved out of TestMiddlewarePublishesTheGeneration: it reads the Canvas course cache.
class TestMiddlewareDropsTheOldGeneration:
    def build(
        self, store: TokenStore, probe: Probe
    ) -> SelfhostRequestContextMiddleware:
        return _middleware(
            probe,
            RealStore(store),
            SchoolPolicy(featured=(School(HOST, API_URL, HOST),), search_enabled=False),
            course_state="per_principal",
        )

    async def test_a_new_generation_drops_what_the_old_one_cached(
        self, tmp_path: pathlib.Path
    ) -> None:
        store = make_store(tmp_path)
        put(store, host="canvas.example.edu")
        probe = GenerationProbe()
        mw = self.build(store, probe)
        await _run(mw, user=_user(_claims()))
        as_user(OID_A, 1)
        cache.current_cache_state().code_to_id["ICS 33"] = "101"
        assert any("g1" in k for k in cache._STATES)

        put(store, TOKEN_2, user="2", host="canvas.example.edu")
        await _run(mw, user=_user(_claims()))
        assert not any(k.startswith(KEY_A) for k in cache._STATES)
