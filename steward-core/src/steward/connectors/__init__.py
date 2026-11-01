"""The connector interface: what Steward needs to know about one upstream service.

A connector describes the credential a user brings for an upstream service and
how Steward checks it. Steward's core never imports a connector module
directly: the deployment names one in ``STEWARD_CONNECTOR`` and it is loaded by
name, so the core stays free of any one service's vocabulary.

This is the first cut of the interface in the design (section 4.3). Credential
fields, the instance directory, identity checks at enrollment, header injection
and the tool catalog follow as the Canvas code moves behind it.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from steward.request_state import RequestCredentials

Verdict = Literal["ok", "rejected", "unknown"]
OK: Verdict = "ok"
REJECTED: Verdict = "rejected"
UNKNOWN: Verdict = "unknown"

CONNECTOR_ENV = "STEWARD_CONNECTOR"
DEFAULT_CONNECTOR = "canvas"


class CredentialProbe(Protocol):
    async def __call__(
        self,
        credentials: RequestCredentials,
        *,
        client_factory: Callable[[], httpx.AsyncClient],
        timeout: float,
    ) -> Verdict:
        """Ask the upstream service whether a stored credential still works.

        Only an answer that positively says the credential is dead is
        ``rejected``; a timeout, a network error or any other answer is
        ``unknown`` and never invalidates the stored credential.
        """


@dataclass(frozen=True)
class Connector:
    """One upstream service as Steward sees it."""

    id: str
    display_name: str
    probe: CredentialProbe


class ConnectorError(Exception):
    """The configured connector cannot be loaded."""


def load_connector(name: str | None = None) -> Connector:
    """The connector named by ``name`` or ``STEWARD_CONNECTOR`` (default ``canvas``).

    A connector is the module ``steward.connectors.<name>`` exposing ``CONNECTOR``.
    """
    name = (name or os.environ.get(CONNECTOR_ENV) or DEFAULT_CONNECTOR).strip()
    if not name.isidentifier():
        raise ConnectorError(f"{CONNECTOR_ENV} must be a connector module name")
    try:
        module = importlib.import_module(f"steward.connectors.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"steward.connectors.{name}":
            raise ConnectorError(
                f"connector {name!r} cannot be loaded: missing module {exc.name!r}"
            ) from exc
        raise ConnectorError(f"unknown connector {name!r}") from exc
    connector = getattr(module, "CONNECTOR", None)
    if not isinstance(connector, Connector):
        raise ConnectorError(f"connector {name!r} does not define CONNECTOR")
    return connector
