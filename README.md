# Steward

Steward keeps each user's upstream credentials for an MCP server, and decides who may use which tools.

It sits between an MCP client or hub (such as [MCPHub](https://github.com/samanhappy/mcphub)) and an MCP server. Steward does four things:

- It signs users in. Local accounts, OIDC, GitHub, Google and Entra are supported, and an MCPHub session can be used too.
- It runs the OAuth authorization server that MCP clients connect through.
- It stores each user's credential for the upstream service, such as a Canvas LMS API token. The credential is sealed with AES-256-GCM, and the user is reminded before it expires. Detecting a credential that has stopped working needs an MCP server behind Steward, so it arrives with the proxy mode.
- It lets each user switch write tools on or off, within the limit the operator sets.

The hub connects the client. Steward keeps the credentials.

## Status

Pre-alpha. This repository was extracted, with its history, from the self-hosted mode of the [Canvas MCP fork](https://github.com/KKazuhaK/canvas-mcp). At the moment Steward serves accounts, sign-in, the authorization server and the account pages. Its `/mcp` endpoint has no tools yet. Forwarding `/mcp` to a connector's MCP server comes next, in proxy mode.

[docs/DESIGN.md](docs/DESIGN.md) has the plan and the migration steps (in Chinese). The documents in [deploy/](deploy/) still describe the Canvas MCP self-hosted deployment, and they are being adapted.

## Layout

| Path | Contents |
| --- | --- |
| `steward-core/` | The Python service (`mcp-steward`): accounts, sign-in, the authorization server, the credential store, tool gating and the account API |
| `steward-core/src/steward/connectors/` | The connector interface, and the Canvas connector as the first one |
| `steward-web/` | The account and admin web app (React) |
| `deploy/` | Compose, reverse-proxy and operator documents |
| `scripts/check_canvas_boundary.py` | Fails the build when Canvas vocabulary spreads outside the connector; see `scripts/canvas-allowlist.txt` |

## Development

```sh
cd steward-core
python -m venv .venv && . .venv/bin/activate
pip install -e ".[postgres]" pytest pytest-asyncio pyyaml respx
python -m pytest -q
steward serve --config     # print the configuration without starting
```

```sh
cd steward-web
npm ci --ignore-scripts
npm test && npm run build
```

`STEWARD_CONNECTOR` picks the connector. The default is `canvas`. The other settings are the same as the Canvas MCP self-hosted mode's; see `deploy/CONFIGURATION.md`.

## License

MIT. Steward started as part of Canvas MCP, created by Vishal Sachdev, and keeps that project's copyright notice. See [LICENSE](LICENSE); `steward-core/LICENSE` is the same text, shipped with the Python package.

## Custody

Steward decrypts a user's stored credential only to use it for that user's request. The operator holds the keyring, so the operator can technically decrypt every stored credential: users must trust the operator, and each user acknowledges a custody notice before storing a credential.
