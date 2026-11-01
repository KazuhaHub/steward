"""The upstream modes must not need the self-hosted data layer.

stdio, X-Canvas-Token, access keys and Easy Auth never touch the database, so
SQLAlchemy, Alembic and psycopg are optional extras. These tests run Python in a
subprocess where those packages cannot be imported at all, and show that every
module the other modes use still imports and works, and that only the
self-hosted store reports the missing extra (with the install command).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

BLOCK = textwrap.dedent(
    """
    import importlib.abc, sys

    class Blocker(importlib.abc.MetaPathFinder):
        BLOCKED = ("sqlalchemy", "alembic", "psycopg", "mako")

        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in self.BLOCKED:
                raise ImportError(f"{name} is blocked for this test")
            return None

    sys.meta_path.insert(0, Blocker())
    """
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONUTF8": "1",
    }
    for name in ("MCP_AUTH_MODE", "DATABASE_URL", "CANVAS_API_TOKEN", "MCP_ACCESS_KEYS"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-c", BLOCK + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_every_non_database_module_import_without_the_extras() -> None:
    result = _run(
        """
        import steward.accounts
        import steward.settings
        import steward.token_store
        import steward.tool_prefs
        import steward.principal_access
        import steward.limits
        import steward.login_state
        import steward.db
        import steward.db.url
        import steward.token_admin
        import steward.authz
        import steward.authz.urls
        assert not any(m.split(".")[0] in ("sqlalchemy", "alembic", "psycopg") for m in sys.modules)
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_parsing_a_database_url_needs_no_database_driver() -> None:
    result = _run(
        """
        from pathlib import Path
        from steward.db.url import parse_database_url
        target = parse_database_url("postgresql+psycopg://u:p@h/db", Path("/data"))
        assert target.kind == "postgresql"
        assert not any(m.split(".")[0] in ("sqlalchemy", "alembic", "psycopg") for m in sys.modules)
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr


def test_building_a_store_without_the_extras_says_how_to_install_them() -> None:
    result = _run(
        """
        from pathlib import Path
        from steward.token_store import Keyring, TokenStore, TokenStoreError
        import base64
        ring = Keyring.parse("k1:" + base64.b64encode(bytes(32)).decode())
        try:
            TokenStore(Path("/tmp/never-created.sqlite3"), ring)
        except TokenStoreError as exc:
            assert "canvas-mcp[selfhost]" in str(exc)
            print("refused")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "refused" in result.stdout


def test_the_self_hosted_start_reports_the_missing_extra_instead_of_crashing() -> None:
    result = _run(
        """
        import base64
        from steward.app import prepare_selfhost
        from steward.settings import SelfhostConfigError, load_selfhost_settings
        settings = load_selfhost_settings({
            "PUBLIC_BASE_URL": "https://canvas.example.test",
            "ENTRA_TENANT_ID": "11111111-2222-3333-4444-555555555555",
            "ENTRA_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "ENTRA_CLIENT_SECRET": "s3cret-value-of-entra-client",
            "ACCOUNT_SESSION_SECRET": base64.b64encode(bytes(range(32))).decode(),
            "CANVAS_TOKEN_KEYS": "k1:" + base64.b64encode(bytes(32)).decode(),
        })
        try:
            prepare_selfhost(settings)
        except SelfhostConfigError as exc:
            assert "canvas-mcp[selfhost]" in str(exc)
            print("refused")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "refused" in result.stdout


def test_the_operator_cli_reports_the_missing_extra_in_one_line() -> None:
    result = _run(
        """
        import base64, os, sys, tempfile
        os.environ["SELFHOST_DATA_DIR"] = tempfile.mkdtemp()
        os.environ["CANVAS_TOKEN_KEYS"] = "k1:" + base64.b64encode(bytes(32)).decode()
        from steward import token_admin
        for argv in (["list"], ["check"], ["db", "current"], ["db", "upgrade"]):
            code = token_admin.main(argv)
            assert code == token_admin.EXIT_CONFIG, (argv, code)
        print("reported")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "reported" in result.stdout
    assert result.stderr.count("canvas-mcp[selfhost]") == 4
    assert "Traceback" not in result.stderr
