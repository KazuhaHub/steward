# Tests set aside until proxy mode

These tests came with the self-hosted layer from the Canvas MCP server. They
cannot run in this package: they exercise the Canvas tools, client or server in
the same process as Steward, or they check a file this repository does not have
yet. `tests/conftest.py` excludes this directory from collection (`collect_ignore`).
None of them was edited to fit; each still imports what it used (`canvas_mcp`,
the `real_course_list` and `isolated_course_cache` fixtures of the Canvas
`conftest.py`, relative imports from `tests/`). Where a file was split, the part
that does not need Canvas stayed in `tests/` under the same name.

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
| `test_docs.py` | The independent-fork notice, client table and custody anchor in the root README. | The final root README, then re-point the assertions at its text. |
| `test_isolation.py` | Two principals interleaved share no Canvas cache, policy, pseudonym or confirmation state; school change separates them. | Canvas server suite; Steward keeps its own principal-key tests. |
| `test_lock_file.py` | The locked versions of fastmcp, mcp and joserfc satisfy the declared ranges. | A `uv.lock` in `steward-core/` (none exists yet). |
| `test_multiuser_e2e.py` | Two users through the whole HTTP stack with the Canvas middleware, tools and anonymization. | A Steward-to-Canvas-server end-to-end run with two enrolled users. |
| `test_selfhost_integration.py` | Native authorization server to MCP to Canvas: bearer checks, enrollment, disablement, per-user tokens. | The same flows against a fake upstream MCP server; the bearer and enrollment parts can then run without Canvas code. |
| `test_startup.py` | The Canvas launcher refuses stdio, `--test`, `--list-grants`, `--revoke` and an unknown `MCP_AUTH_MODE`; legacy startup is undisturbed. | Belongs to the Canvas server's CLI. Steward's `serve` has no stdio or legacy mode and ignores `MCP_AUTH_MODE`; decide whether it should still reject unknown values. |
| `test_token_health.py` | A 401 from the in-process Canvas client triggers the suspicion, probe, single flight and verdict that mark a token dead. | The proxy reporting upstream 401s to `TokenHealth` (the in-process monitor hook was removed). |
| `test_token_invalid_stack.py` | A dead Canvas token through FastMCP auth, the gate, the store and the real Canvas client. | The proxy path with a fake upstream that answers 401. |
| `test_write_tool_optin_stack.py` | Per-user write-tool opt-in through the whole stack with the real tool registry and `ALLOWED_WRITE_TOOLS`. | The tool catalog and policy from the connector plus a proxied `tools/list`. |
