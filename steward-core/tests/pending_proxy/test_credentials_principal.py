"""The Canvas client's use of the request-state missing-credentials message."""

from steward.request_state import (
    set_http_request_active,
    set_missing_credentials_message,
)

LEGACY_TEXT = "Canvas token required for HTTP request"


class TestClientUsesTheMessage:
    async def test_client_fails_closed_with_the_selfhost_text(self):
        from canvas_mcp.core.client import make_canvas_request

        set_http_request_active(True)
        set_missing_credentials_message("Enroll at https://x.test/account")
        assert await make_canvas_request("get", "/users/self") == {
            "error": "Enroll at https://x.test/account"
        }

    async def test_client_keeps_the_legacy_text_by_default(self):
        from canvas_mcp.core.client import make_canvas_request

        set_http_request_active(True)
        assert await make_canvas_request("get", "/users/self") == {"error": LEGACY_TEXT}

    async def test_authenticated_client_raises_the_message(self):
        import pytest

        from canvas_mcp.core.client import canvas_authenticated_client

        set_http_request_active(True)
        set_missing_credentials_message("Enroll at https://x.test/account")
        with pytest.raises(PermissionError, match="Enroll at https://x.test/account"):
            async with canvas_authenticated_client():
                pass
