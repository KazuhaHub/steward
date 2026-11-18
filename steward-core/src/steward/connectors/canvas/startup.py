"""What the Canvas MCP server behind Steward must be configured to refuse.

These checks used to run in-process, when the account layer and the Canvas
tools shared one server. Behind Steward the Canvas server is a separate
process, so they describe its required configuration instead (the design's
``backend_requirements``). ``backend_problems`` takes that server's settings as
a mapping of environment variables, the form a deployment declares them in.

The pilot profile also requires the backend to refuse downloads from hosts
other than the user's Canvas school; the Canvas server has no setting for that
yet (design section 4.5), so it is not checked here. ``MCP_ACCESS_KEYS`` is no longer refused: behind
Steward the Canvas server needs one so that it trusts only Steward.
"""

from __future__ import annotations

from collections.abc import Mapping

_FALSE_WORDS = frozenset({"", "0", "false", "f", "no", "n", "off"})


def _truthy(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() not in _FALSE_WORDS


def backend_problems(env: Mapping[str, str], *, profile: str, course_state: str) -> list[str]:
    """Every reason this Canvas server configuration must not run behind Steward."""
    problems: list[str] = []

    if env.get("CANVAS_API_TOKEN"):
        problems.append(
            "CANVAS_API_TOKEN must NOT be set: every user brings their own Canvas "
            "token, and a server token could be used by mistake"
        )
    if _truthy(env, "ENTRA_AUTH_ENABLED"):
        problems.append(
            "ENTRA_AUTH_ENABLED must not be set: the Easy Auth header path is "
            "spoofable off Azure and is never consulted behind Steward"
        )
    if _truthy(env, "MCP_ALLOW_UNAUTHENTICATED"):
        problems.append("MCP_ALLOW_UNAUTHENTICATED must not be set behind Steward")
    if _truthy(env, "ACCESS_REQUEST_ENABLED"):
        problems.append("ACCESS_REQUEST_ENABLED must not be set behind Steward")

    allowed = {
        part.strip().lower()
        for part in env.get("ALLOWED_WRITE_TOOLS", "").replace(",", " ").split()
        if part.strip()
    }
    if _truthy(env, "EXECUTE_TYPESCRIPT_ENABLED") or "execute_typescript" in allowed:
        problems.append(
            "EXECUTE_TYPESCRIPT_ENABLED must be false and ALLOWED_WRITE_TOOLS must "
            "not name execute_typescript: code execution would run with users' "
            "tokens on the shared host"
        )

    if profile == "pilot":
        if env.get("ALLOWED_WRITE_TOOLS"):
            problems.append("SELFHOST_PROFILE=pilot requires ALLOWED_WRITE_TOOLS to be unset")
        if course_state != "request_local":
            problems.append("SELFHOST_PROFILE=pilot requires SELFHOST_COURSE_STATE=request_local")

    return problems
