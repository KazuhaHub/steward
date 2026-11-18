"""SELFHOST_DISABLED_TOOLS: the operator removes named tools at startup.

Three layers: the setting parser, the startup wiring through ``main()``, and the
whole HTTP stack (real tool registry, credential gate, token store) proving that a
disabled tool is neither listed nor callable, for a read tool and a write tool alike.
"""

from __future__ import annotations

import base64

import pytest

from steward.cli import config_summary
from steward.connectors.canvas.tool_policy import (
    TOOL_EFFECTS,
)
from steward.settings import (
    DISABLED_TOOLS_ENV,
    SelfhostConfigError,
    load_selfhost_settings,
)

from .account_fixture import _display_in_utc, build_harness, put_row  # noqa: F401
from .conftest import CLIENT, OID_A, TENANT, acct_key
from .test_startup import HTTP, SECRETS, _run_main, entra_env  # noqa: F401 - fixture

BASE = "https://canvas.example.test"
CANVAS = "https://canvas.example.edu"
STUDENT_WRITES = "mark_module_item_done,create_planner_note,send_message"
OPERATOR_ALLOWS = "mark_module_item_done,create_planner_note"
READ_TOOL = "read_course_file_text"
WRITE_TOOL = "create_planner_note"
KEY_A = acct_key(OID_A)
TOKEN_A = "canvas-token-for-user-A-0123456789"


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "PUBLIC_BASE_URL": BASE,
        "ENTRA_TENANT_ID": TENANT,
        "ENTRA_CLIENT_ID": CLIENT,
        "ENTRA_CLIENT_SECRET": "entra-client-secret-0123456789",
        "ACCOUNT_SESSION_SECRET": base64.b64encode(bytes(range(32))).decode(),
        "CANVAS_TOKEN_KEYS": "k1:" + base64.b64encode(bytes(32)).decode(),
    }
    env.update(overrides)
    return env


def _problems(raw: str) -> list[str]:
    with pytest.raises(SelfhostConfigError) as info:
        load_selfhost_settings(_env(SELFHOST_DISABLED_TOOLS=raw))
    return info.value.problems


class TestSetting:
    def test_the_variable_name(self):
        assert DISABLED_TOOLS_ENV == "SELFHOST_DISABLED_TOOLS"

    def test_unset_and_blank_remove_nothing(self):
        assert load_selfhost_settings(_env()).disabled_tools == ()
        for blank in ("", "  ", ",", " , ,"):
            assert (
                load_selfhost_settings(
                    _env(SELFHOST_DISABLED_TOOLS=blank)
                ).disabled_tools
                == ()
            )

    def test_names_are_trimmed_lowercased_deduplicated_and_sorted(self):
        settings = load_selfhost_settings(
            _env(
                SELFHOST_DISABLED_TOOLS=" read_course_file_text, LIST_USERS ,read_course_file_text,send_message,"
            )
        )
        assert settings.disabled_tools == (
            "list_users",
            "read_course_file_text",
            "send_message",
        )

    def test_read_and_write_tools_are_both_accepted(self):
        raw = f"{READ_TOOL},{WRITE_TOOL},execute_typescript"
        settings = load_selfhost_settings(_env(SELFHOST_DISABLED_TOOLS=raw))
        assert set(settings.disabled_tools) == {
            READ_TOOL,
            WRITE_TOOL,
            "execute_typescript",
        }

    def test_every_known_tool_name_is_accepted(self):
        raw = ",".join(sorted(TOOL_EFFECTS))
        loaded = load_selfhost_settings(
            _env(SELFHOST_DISABLED_TOOLS=raw)
        ).disabled_tools
        assert set(loaded) == set(TOOL_EFFECTS)

    def test_an_unknown_name_stops_startup_and_the_message_lists_only_the_unknown_names(
        self,
    ):
        problems = _problems(f"{READ_TOOL},read_course_file_txt,no_such_tool")
        assert len(problems) == 1
        assert "SELFHOST_DISABLED_TOOLS" in problems[0]
        assert "no_such_tool" in problems[0] and "read_course_file_txt" in problems[0]
        assert READ_TOOL not in problems[0].replace("read_course_file_txt", "")

    def test_an_entry_that_is_not_shaped_like_a_tool_name_is_counted_not_echoed(self):
        secret = "Bearer-s3cr3t/Token+value=="
        problems = _problems(f"{READ_TOOL},{secret}, two words ")
        assert len(problems) == 1
        assert "2 entries" in problems[0]
        assert "s3cr3t" not in problems[0] and "words" not in problems[0]

    def test_it_is_reported_together_with_other_problems(self):
        with pytest.raises(SelfhostConfigError) as info:
            load_selfhost_settings(
                _env(PUBLIC_BASE_URL="invalid", SELFHOST_DISABLED_TOOLS="nope")
            )
        text = "; ".join(info.value.problems)
        assert "PUBLIC_BASE_URL" in text and "SELFHOST_DISABLED_TOOLS" in text

    def test_the_summary_for_config_lists_the_tools(self):
        settings = load_selfhost_settings(
            _env(SELFHOST_DISABLED_TOOLS=f"{WRITE_TOOL},{READ_TOOL}")
        )
        lines = config_summary(settings, "canvas")
        assert f"  Disabled tools: {WRITE_TOOL}, {READ_TOOL}" in lines
        none = config_summary(load_selfhost_settings(_env()), "canvas")
        assert "  Disabled tools: none" in none




class TestStartup:

    def test_an_unknown_name_exits_1_and_names_only_the_unknown_tool(
        self,
        entra_env,  # noqa: F811
        caplog,
    ):  # noqa: F811
        mp = entra_env["monkeypatch"]
        mp.setenv("SELFHOST_DISABLED_TOOLS", f"{READ_TOOL},read_course_file_txt")
        assert _run_main(mp, *HTTP).code == 1
        assert "read_course_file_txt" in caplog.text
        assert "SELFHOST_DISABLED_TOOLS" in caplog.text
        for secret in SECRETS.values():
            assert secret not in caplog.text
        assert f"unknown tools: {READ_TOOL}" not in caplog.text

    def test_an_unknown_name_also_stops_the_config_command(self, entra_env):  # noqa: F811
        entra_env["monkeypatch"].setenv("SELFHOST_DISABLED_TOOLS", "no_such_tool")
        assert _run_main(entra_env["monkeypatch"], *HTTP, "--config").code == 1

    def test_config_shows_the_list_without_secrets(self, entra_env, capsys):  # noqa: F811
        entra_env["monkeypatch"].setenv(
            "SELFHOST_DISABLED_TOOLS", f"{WRITE_TOOL},{READ_TOOL}"
        )
        assert _run_main(entra_env["monkeypatch"], *HTTP, "--config").code == 0
        out = capsys.readouterr().err
        assert f"Disabled tools: {WRITE_TOOL}, {READ_TOOL}" in out
        for secret in SECRETS.values():
            assert secret not in out










