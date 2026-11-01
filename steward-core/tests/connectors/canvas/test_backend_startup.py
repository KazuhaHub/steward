"""The Canvas server behind Steward must be configured to refuse what a shared host cannot allow."""

import pytest

from steward.connectors.canvas.startup import backend_problems


def problems(env: dict[str, str], *, profile: str = "", course_state: str = "request_local") -> list[str]:
    return backend_problems(env, profile=profile, course_state=course_state)


class TestForbiddenSettings:
    def test_an_empty_configuration_has_no_problems(self) -> None:
        assert problems({}) == []

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("CANVAS_API_TOKEN", "a-server-side-canvas-token-1234567890"),
            ("ENTRA_AUTH_ENABLED", "true"),
            ("MCP_ALLOW_UNAUTHENTICATED", "true"),
            ("ACCESS_REQUEST_ENABLED", "true"),
            ("EXECUTE_TYPESCRIPT_ENABLED", "true"),
            ("ALLOWED_WRITE_TOOLS", "execute_typescript"),
        ],
    )
    def test_forbidden_settings_are_named(self, name: str, value: str) -> None:
        text = " ".join(problems({name: value}))
        assert name in text
        if value.endswith("1234567890"):  # the secret-looking ones
            assert value not in text

    def test_access_keys_are_no_longer_refused(self) -> None:
        # Behind Steward the Canvas server needs a key so that it trusts only Steward.
        assert problems({"MCP_ACCESS_KEYS": "shared-access-key-1234567890"}) == []

    def test_legacy_flags_each_produce_a_problem(self) -> None:
        text = " ".join(
            problems(
                {
                    "CANVAS_API_TOKEN": "tok-1234567890abcdef-1234",
                    "ENTRA_AUTH_ENABLED": "true",
                }
            )
        )
        for name in ("CANVAS_API_TOKEN", "ENTRA_AUTH_ENABLED"):
            assert name in text
        assert "tok-1234567890" not in text

    @pytest.mark.parametrize("value", ["", "false", "0", "no", "off"])
    def test_a_false_flag_is_fine(self, value: str) -> None:
        assert problems({"ENTRA_AUTH_ENABLED": value, "EXECUTE_TYPESCRIPT_ENABLED": value}) == []

    def test_allowing_all_write_tools_is_fine_outside_the_pilot_profile(self) -> None:
        assert problems({"ALLOWED_WRITE_TOOLS": "all"}) == []


@pytest.mark.parametrize(
    ("setting", "value"),
    [("ALLOWED_WRITE_TOOLS", "all"), ("SELFHOST_COURSE_STATE", "per_principal")],
)
def test_pilot_startup_refuses_scope_expansion(setting: str, value: str) -> None:
    env = {setting: value} if setting == "ALLOWED_WRITE_TOOLS" else {}
    state = value if setting == "SELFHOST_COURSE_STATE" else "request_local"
    text = " ".join(problems(env, profile="pilot", course_state=state))
    assert "SELFHOST_PROFILE" in text and setting in text
