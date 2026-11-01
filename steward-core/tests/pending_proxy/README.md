# Tests set aside until proxy mode

These tests came with the self-hosted layer from the Canvas MCP server. They
cannot run in this package: they exercise the Canvas tools, client or server in
the same process as Steward, or they check a file this repository does not have
yet. `tests/conftest.py` excludes this directory from collection (`collect_ignore`).
These files are not byte-for-byte the originals:

- Every file imports the account layer from `steward.*` (the extraction rewrote
  the old `canvas_mcp.core.selfhost.*` imports) and the Canvas code from
  `canvas_mcp`, which is not installed here.
- Relative imports that pointed at `tests/` were changed from `.` to `..`
  because the files now sit one level down.
- The files were otherwise left as they were, except: `test_dependency_isolation.py`,
  `test_disabled_tools.py`, `test_startup.py`, `test_compose_postgres.py` and
  `test_docs.py` keep only the tests that were moved out of the collected file of
  the same name; `test_credentials_principal.py` has a new header (it only
  contains the client tests of the original); `test_credential_generation.py`
  also holds `TestMiddlewareDropsTheOldGeneration`, one test moved out of
  `TestMiddlewarePublishesTheGeneration` together with its `build` helper;
  `test_token_health.py` imports `set_token_health_monitor` from
  `canvas_mcp.core.token_health`, where the in-process hook lives.
- They also need two fixtures of the Canvas `conftest.py` (`isolated_course_cache`,
  `real_course_list`) that this repository does not have.
- `test_token_health.py` is the original file. The tests of `TokenHealth` itself
  (single flight, cooldown, restored token, generations, bookkeeping, audit) now
  run in `tests/test_token_health.py` against the Canvas probe, and the probe has
  its own tests in `tests/connectors/canvas/test_probe.py`. What stays here is the
  Canvas client raising the suspicion through the removed `set_token_health_monitor`
  hook.

Skipped until a lock file exists: `tests/authz/test_authz_compat_contract.py::test_locked_versions_satisfy_the_declared_ranges`
checks the locked versions of fastmcp, mcp and joserfc and skips with
"no lock file in this repository yet" while `steward-core/uv.lock` does not exist.
It runs once the lock file is added.

In proxy mode Steward forwards `/mcp` to a separate Canvas MCP server, so these
become tests against a Canvas MCP server running as its own process (a fake
upstream or the real one) and a check of what Steward sends to it.

| File | Behaviour it covers | What it needs in proxy mode |
| --- | --- | --- |
| `test_compose_postgres.py` | The CI workflow runs the compose override's image and both dependency runtimes on PostgreSQL. | The Steward CI workflow (`.github/workflows/ci.yml`) with those jobs, then re-point the file name and job names. |
| `test_course_state_modes.py` | Course lists, labels, policy decisions, pseudonyms and discussion hints are request-local or per principal in every auth mode. | Moves to the Canvas server's own suite; Steward only forwards the principal and the `SELFHOST_COURSE_STATE` choice, so a test that the right header and state mode reach the upstream. |
| `test_credential_generation.py` | Course cache, policy cache, pseudonyms and pending write confirmations are dropped when a credential generation changes. | A purge signal from Steward to the Canvas server (or per-credential keying there) and a test of that contract. |
| `test_credentials_principal.py` | The Canvas client fails closed with the missing-credentials message on an HTTP request without a token. | The same message produced by the proxy for an unenrolled user. |
| `test_dependency_isolation.py` | The Canvas server modules and the stdio/access-key configuration import without SQLAlchemy, Alembic or psycopg. | A test in the Canvas server repository; not a Steward concern. |
| `test_disabled_tools.py` | `SELFHOST_DISABLED_TOOLS` removes named tools from the real Canvas registry (list and call), including after the write-tool policy. | The tool catalog from the connector, then a gate test against the proxied `tools/list` and `tools/call`. |
| `test_dockerfile.py` | Properties of `Dockerfile.selfhost` (pinned digests, non-root, healthcheck, web build, workflow job). | A Steward `Dockerfile`; the file does not exist in this layout yet. |
| `test_docs.py` | `test_independent_fork_notice_and_unclaimed_client_table`: the independent-fork notice, client table and custody anchor in the README. `test_custody_documentation_explains_the_operator_decryption_boundary`: the security notice that the operator can decrypt every stored credential, and that the root README links to it. | The final root README and a "Custody and privacy boundary" section in Steward's own docs (`deploy/README.md`) with the same facts (decryption by a compromised operator, plaintext SQL, audit history, `ACCOUNT_SESSION_SECRET`, AES-256-GCM); then re-point the assertions. Do not drop the custody test: it guards a security notice. |
| `test_isolation.py` | Two principals interleaved share no Canvas cache, policy, pseudonym or confirmation state; school change separates them. | Canvas server suite; Steward keeps its own principal-key tests. |
| `test_multiuser_e2e.py` | Two users through the whole HTTP stack with the Canvas middleware, tools and anonymization. | A Steward-to-Canvas-server end-to-end run with two enrolled users. |
| `test_selfhost_integration.py` | Native authorization server to MCP to Canvas: bearer checks, enrollment, disablement, per-user tokens. | The same flows against a fake upstream MCP server; the bearer and enrollment parts can then run without Canvas code. |
| `test_startup.py` | The Canvas launcher refuses stdio, `--test`, `--list-grants`, `--revoke`; legacy startup is undisturbed. (`test_unknown_auth_mode` is back in `tests/test_startup.py`.) | Belongs to the Canvas server's CLI; Steward's `serve` has no stdio or legacy mode. |
| `test_token_health.py` | A 401 from the in-process Canvas client triggers the suspicion, probe and verdict that mark a token dead, including the short-circuit of later calls, file downloads and uploads. | The proxy reporting upstream 401s to `TokenHealth` (the in-process monitor hook was removed). |
| `test_token_invalid_stack.py` | A dead Canvas token through FastMCP auth, the gate, the store and the real Canvas client. | The proxy path with a fake upstream that answers 401. |
| `test_write_tool_optin_stack.py` | Per-user write-tool opt-in through the whole stack with the real tool registry and `ALLOWED_WRITE_TOOLS`. | The tool catalog and policy from the connector plus a proxied `tools/list`. |
