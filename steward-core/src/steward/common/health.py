"""The unauthenticated liveness probe, shared by every HTTP mode.

``GET /healthz`` answers ``{"ok": true, "version": "<package version>"}`` and
nothing else: no configuration, no tool list, no upstream URL. The version is
the installed ``mcp-steward`` distribution's.
"""

from __future__ import annotations

import json

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mcp-steward")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"

HEALTH_PATH = "/healthz"


def health_body() -> bytes:
    """The JSON body of a healthy response."""
    return json.dumps({"ok": True, "version": __version__}).encode("utf-8")
