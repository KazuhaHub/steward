"""``steward``: start the Steward service, or print its configuration.

With no MCP server behind it yet, Steward serves accounts, sign-in, the OAuth
authorization server and the account pages; its ``/mcp`` endpoint has no tools.
Forwarding ``/mcp`` to a connector's MCP server comes with the proxy mode.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from fastmcp import FastMCP

from .common.health import __version__
from .common.logging import log_error, log_info, scrub_third_party_logs
from .connectors import ConnectorError, load_connector
from .settings import (
    SelfhostConfigError,
    SelfhostSettings,
    format_duration,
    load_selfhost_settings,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _authz_summary(settings: SelfhostSettings) -> str:
    """The MCP authorization line of --config (the tunables are shown only when they apply)."""
    authz = settings.authz
    return (
        "  MCP authorization: built-in authorization server ( "
        f"CIMD {'on' if authz.cimd_enabled else 'off'}; "
        f"access {format_duration(authz.access_token_ttl)}; "
        f"refresh cap {format_duration(authz.refresh_absolute_ttl)}; "
        f"grace {authz.refresh_reuse_grace_s}s; "
        f"upstream age {format_duration(authz.max_upstream_auth_age)})"
    )


def _providers_summary(settings: SelfhostSettings) -> list[str]:
    """The ``Sign-in providers:`` line of --config, only when it is not the Entra-only default.

    Names the provider ids, their (public) client ids and the issuers of generic OIDC
    providers. Client secrets are never printed.
    """
    login = settings.login_providers
    if not login.order:
        return ["Sign-in providers: database-managed (Admin > Settings)"]
    if login.order == ("entra",):
        return []
    parts: list[str] = []
    for provider_id in login.order:
        if provider_id == "google" and login.google is not None:
            parts.append(f"google (client {login.google.client_id})")
        elif provider_id == "github" and login.github is not None:
            parts.append(f"github (client {login.github.client_id})")
        elif provider_id.startswith("oidc:"):
            cfg = login.oidc_by_id().get(provider_id)
            parts.append(
                f"{provider_id} (issuer {cfg.issuer}, client {cfg.client_id})"
                if cfg is not None
                else provider_id
            )
        else:
            parts.append(provider_id)
    return [f"  Sign-in providers: {', '.join(parts)}"]


def config_summary(settings: SelfhostSettings, connector_name: str) -> list[str]:
    """Configuration lines for --config. Never includes a secret."""
    entra_lines = (
        [
            f"  Entra tenant: {settings.tenant_id}",
            f"  Entra client: {settings.client_id}",
        ]
        if settings.entra_enabled
        else []
    )
    # The Entra app roles mean something only while Entra signs people in.
    role_lines = (
        [f"  Roles: user={settings.required_role}, owner={settings.owner_role}"]
        if settings.entra_enabled
        else []
    )
    return [
        f"  Connector: {connector_name}",
        f"  Public URL: {settings.public_base_url}",
        f"  MCP endpoint: {settings.mcp_url}",
        f"  Account page: {settings.account_url}",
        f"  Account UI: React (built app from {settings.account_web_dist})",
        *_providers_summary(settings),
        *entra_lines,
        f"  API scope: {settings.api_scope}",
        *role_lines,
        f"  Account session lifetime: {settings.account_session_ttl_seconds}s",
        f"  Token store: {settings.database_target.description}",
        "  Database migrations: "
        + ("automatic at start" if settings.auto_migrate else "manual (token_admin db upgrade)"),
        f"  Rate limits and one-time state: {settings.state_backend} (single instance)",
        _authz_summary(settings),
        f"  Redirect URIs: {', '.join(settings.allowed_client_redirect_uris)}",
        "  Upstream instances and public sign-in names: database-managed (Admin > Settings)",
        "  Disabled tools: " + (", ".join(settings.disabled_tools) or "none"),
    ]


def _run_http_server(app: ASGIApp, host: str, port: int) -> None:
    """Serve the app: one process, no trust in forwarded headers.

    ``proxy_headers=False``: every URL comes from PUBLIC_BASE_URL and the Host
    guard compares the real Host header, so X-Forwarded-* is never believed.

    ``access_log=False``: uvicorn's access log prints the full query string, and
    the sign-in callbacks carry their authorization ``code`` and ``state`` there
    (also ``/consent?txn_id=`` and ``/authorize`` state). Logs stay limited to
    opaque ids and reason codes.
    """
    import anyio
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )
    # uvicorn installs its handlers when the config is built; FastMCP's OAuth code
    # logs transaction ids and upstream error text through its own handlers.
    scrub_third_party_logs()
    server = uvicorn.Server(config)
    anyio.run(server.serve)


def _fail(problems: list[str]) -> None:
    for problem in problems:
        log_error(problem)
    sys.exit(1)


def serve(args: argparse.Namespace) -> None:
    """Start Steward, or exit 1 (fail closed)."""
    from .app import (
        apply_disabled_tools,
        build_selfhost_asgi_app,
        install_selfhost,
        prepare_selfhost,
        validate_selfhost_startup,
    )
    from .audit import init_audit_logging
    from .authz.runtime import build_auth_provider

    try:
        connector = load_connector()
    except ConnectorError as exc:
        _fail([str(exc)])
    try:
        settings = load_selfhost_settings()
    except SelfhostConfigError as exc:
        _fail(exc.problems)

    if args.config:
        print("Steward configuration:", file=sys.stderr)
        print(f"  Host: {args.host}", file=sys.stderr)
        print(f"  Port: {args.port}", file=sys.stderr)
        for line in config_summary(settings, connector.id):
            print(line, file=sys.stderr)
        sys.exit(0)

    problems = validate_selfhost_startup(settings)
    if problems:
        _fail(problems)

    try:
        runtime = prepare_selfhost(settings)
        settings = runtime.settings
    except SelfhostConfigError as exc:
        _fail(exc.problems)

    init_audit_logging()
    mcp = FastMCP(name="Steward", auth=build_auth_provider(settings, runtime), version=__version__)
    scrub_third_party_logs()
    disabled_tools = asyncio.run(apply_disabled_tools(mcp, settings.disabled_tools))
    try:
        account_ui = install_selfhost(mcp, runtime)
    except SelfhostConfigError as exc:
        runtime.store.close()
        _fail(exc.problems)

    log_info(
        f"Starting Steward on {args.host}:{args.port}",
        public_url=settings.public_base_url,
        connector=connector.id,
        sign_in_providers=",".join(settings.login_providers.order) or "database",
        disabled_tools=len(disabled_tools),
        account_ui=account_ui,
        token_store=settings.database_target.description,
        enrollments=runtime.store.count(),
    )
    try:
        _run_http_server(build_selfhost_asgi_app(mcp, runtime), host=args.host, port=args.port)
    except KeyboardInterrupt:
        log_info("\nShutting down server...")
    except Exception as e:
        log_error("Server error", exc=e)
        sys.exit(1)
    finally:
        log_info("Server stopped")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="steward",
        description="Steward: per-user upstream credentials, sign-in and tool gating for MCP",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve", help="start the service")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument(
        "--config", action="store_true", help="print the configuration and exit"
    )
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(args)


if __name__ == "__main__":
    main()
