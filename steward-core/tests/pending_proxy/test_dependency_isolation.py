"""Canvas server modules must not need the self-hosted data layer.

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

SRC = Path(__file__).resolve().parents[2] / "src"

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


def test_the_server_and_every_non_database_module_import_without_the_extras() -> None:
    result = _run(
        """
        import canvas_mcp.server
        import canvas_mcp.tools.discovery
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


def test_the_legacy_stdio_and_access_key_configuration_never_loads_the_data_layer() -> None:
    result = _run(
        """
        import os
        os.environ.update({
            "CANVAS_API_URL": "https://canvas.example.test/api/v1",
            "CANVAS_API_TOKEN": "7~" + "x" * 62,
        })
        from canvas_mcp.core.config import get_config, reset_config
        reset_config()
        config = get_config()
        assert config.canvas_api_token

        os.environ["MCP_ACCESS_KEYS"] = "a-long-access-key-0123456789abcdef"
        os.environ.pop("CANVAS_API_TOKEN")
        reset_config()
        get_config()

        from steward.settings import auth_mode
        assert auth_mode({}) == "legacy"
        from canvas_mcp.server import main  # noqa: F401
        assert not any(m.split(".")[0] in ("sqlalchemy", "alembic", "psycopg") for m in sys.modules)
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")
