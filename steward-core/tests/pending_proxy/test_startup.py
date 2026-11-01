"""The Canvas launcher refuses stdio and its admin flags, and legacy startup is undisturbed."""

import base64
import logging
from pathlib import Path
from typing import Any

import fastmcp
import pytest

from canvas_mcp import server as server_module
from canvas_mcp.core import config as config_module
from steward.db.migrations.provider_environment import (
    load_provider_environment,
)

from ..conftest import CLIENT, TENANT, store_put
from ..spa_fixture import make_dist

SECRETS = {
    "ENTRA_CLIENT_SECRET": "entra-client-secret-0123456789",
    "OAUTH_JWT_SIGNING_KEY": "jwt-signing-key-" + "z" * 40,
    "ACCOUNT_SESSION_SECRET": base64.b64encode(bytes(range(32))).decode(),
    "CANVAS_TOKEN_KEYS": "k1:" + base64.b64encode(bytes(32)).decode(),
}
LEGACY_VARS = (
    "CANVAS_API_TOKEN",
    "MCP_ACCESS_KEYS",
    "ENTRA_AUTH_ENABLED",
    "MCP_ALLOW_UNAUTHENTICATED",
    "ACCESS_REQUEST_ENABLED",
    "EXECUTE_TYPESCRIPT_ENABLED",
    "ALLOWED_WRITE_TOOLS",
    "STUDENT_WRITE_TOOLS",
    "CANVAS_ROLE",
)


@pytest.fixture
def entra_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """A complete, valid selfhost environment rooted in tmp_path."""
    for name in (
        *LEGACY_VARS,
        "CANVAS_API_URL",
        "CANVAS_FEATURED_SCHOOLS",
        "CANVAS_SCHOOL_SEARCH",
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "fastmcp"
    data = tmp_path / "data"
    values = {
        "MCP_AUTH_MODE": "selfhost",
        "PUBLIC_BASE_URL": "https://canvas.example.test",
        "ENTRA_TENANT_ID": TENANT,
        "ENTRA_CLIENT_ID": CLIENT,
        "ACCOUNT_WEB_DIST": str(make_dist(tmp_path / "web")),
        "FASTMCP_HOME": str(home),
        "SELFHOST_DATA_DIR": str(data),
        **{
            name: value
            for name, value in SECRETS.items()
            if name != "OAUTH_JWT_SIGNING_KEY"
        },
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    # FastMCP reads FASTMCP_HOME once, when it is imported; mirror a process
    # that was started with the variable already set.
    monkeypatch.setattr(fastmcp.settings, "home", home)
    config_module.reset_config()
    return {"home": home, "data": data, "monkeypatch": monkeypatch}


def _run_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> SystemExit:
    monkeypatch.setattr("sys.argv", ["canvas-mcp-server", *argv])
    config_module.reset_config()
    reached_http = False

    def unexpected_http(*args, **kwargs):
        nonlocal reached_http
        reached_http = True
        raise RuntimeError("A refusal test unexpectedly reached HTTP serving")

    try:
        with monkeypatch.context() as guard:
            if (
                server_module._run_selfhost_http_server.__module__
                == "canvas_mcp.server"
            ):
                guard.setattr(
                    server_module, "_run_selfhost_http_server", unexpected_http
                )
            with pytest.raises(SystemExit) as info:
                server_module.main()
    finally:
        config_module.reset_config()
    assert not reached_http, "A startup-refusal test reached the live HTTP server"
    return info.value


HTTP = ("--transport", "streamable-http")


class TestRefusals:
    def test_stdio_is_refused(self, entra_env, caplog):
        assert _run_main(entra_env["monkeypatch"]).code == 1
        assert "streamable-http" in caplog.text

    @pytest.mark.parametrize(
        "flag", [["--test"], ["--list-grants"], ["--revoke", "oid"]]
    )
    def test_admin_and_test_commands_are_refused(self, entra_env, flag, caplog):
        assert _run_main(entra_env["monkeypatch"], *HTTP, *flag).code == 1
        assert "not available" in caplog.text


class TestLegacyUntouched:
    def test_create_server_defaults_to_no_auth(self):
        assert server_module.create_server().auth is None

    def test_legacy_http_still_requires_its_gate(self, monkeypatch):
        for name in ("MCP_AUTH_MODE", *LEGACY_VARS):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CANVAS_API_URL", "https://canvas.example.edu")
        assert _run_main(monkeypatch, *HTTP, "--config").code == 1

    def test_explicit_legacy_mode_is_the_same_as_unset(self, monkeypatch):
        for name in LEGACY_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MCP_AUTH_MODE", "legacy")
        monkeypatch.setenv("CANVAS_API_URL", "https://canvas.example.edu")
        monkeypatch.setenv("MCP_ALLOW_UNAUTHENTICATED", "true")
        assert _run_main(monkeypatch, *HTTP, "--config").code == 0
