<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Authenticated loopback HTTP readiness

The default remains stdio. HTTP must be enabled explicitly and binds only
`127.0.0.1`; there is no public-bind, proxy-header trust, TLS, OAuth, account change,
deployment, tunnel setup, or client configuration replacement in this adapter.
The adapter and upstream repositories may be public; repository visibility is not
an authorization signal. Local-only use and existing authentication/loopback boundaries
remain mandatory; package uploads and publication from the adapter are not allowed.
Binding localhost does not stop an authorized client from sharing retrieved content
with its chosen model service.

## Start locally

Set the existing absolute `KAPA_WORKSPACE` and isolated `KAPA_JOBS_DIR` first.
Provision a random bearer secret of at least 32 printable ASCII characters into
the process environment through your local secret mechanism; do not paste it into
commands, URLs, documents, source files, or committed client configuration.
The adapter reads only `KAPA_HTTP_TOKEN`, never credential files or model/API keys.

```powershell
$env:KAPA_TRANSPORT = 'streamable-http'
$env:KAPA_HTTP_PORT = '8765'
# KAPA_HTTP_TOKEN must already be set securely in this process environment.
& '<ABSOLUTE_VENV_PYTHON>' -I -B -m kapa_mcp
```

Endpoint: `http://127.0.0.1:8765/mcp`. Ports 1024–65535 are accepted.
Use that exact numeric host and port: `localhost`, IPv6 and arbitrary Host values
are rejected. Missing Origin is allowed for native clients; a supplied Origin
must equal `http://127.0.0.1:8765`. Host and Origin duplicates are rejected.
Every HTTP method requires one `Authorization: Bearer <secret>` header. Missing,
wrong or duplicate credentials return 401; invalid Host returns 421; invalid
Origin returns 403. Query strings return 400 and cannot carry authentication.
Access logs and forwarded-header trust are disabled. Errors never echo credentials
or supplied headers. Stop and restart with a new environment secret to rotate it.
This is a single-user local shared secret, not an OAuth authorization server.

## Protocol verification

From the installed adapter directory:

```powershell
& .venv/Scripts/python.exe -I -B -m pytest -p no:cacheprovider tests/test_remote.py tests/test_server.py
```

The HTTP test launches a separate real adapter process on an ephemeral loopback
port with synthetic repositories and a generated temporary bearer. The official
installed SDK `mcp==2.3.0` client performs legacy MCP `initialize`, tool listing and
repository reads through `streamable_http_client`. Negative HTTP requests verify
the authentication/Host/Origin boundaries and absence of secrets in captured logs.
This is protocol evidence, not a ChatGPT/Claude conversation handshake or a PDF check.

## Local clients and remaining chat setup

For a future **local** Codex registration, the documented configuration shape is:

```toml
[mcp_servers.kapa_http]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "KAPA_HTTP_TOKEN"
```

Both server and client need the same securely provisioned environment secret.
This example is documentation only; no actual client configuration is changed.
The existing [stdio examples](client-registration.md) remain available for Codex
and Claude Code. Local client product versions must pass initialization, all 17
tools' discovery, and a read before acceptance is recorded. The SDK legacy-mode
test does not establish either product's protocol negotiation behavior.

**ChatGPT/Claude chat acceptance: not yet verified.** Repository visibility does not
authorize remote access. Remote chat integration would require a separately
configured reachable authenticated endpoint or supported tunnel, client/workspace
permissions, transport/auth compatibility, and an actual tool handshake. None is
deployed here, and the current authorization excludes public endpoints/tunnels.
Current OpenAI guidance offers a public HTTPS endpoint or Secure MCP Tunnel;
the raw loopback URL alone is not a completed ChatGPT registration. Claude's
remote connectors originate in Anthropic's cloud and require reachability from
there, including when configured through Claude Desktop. Local Desktop/Code
stdio is a separate mechanism. This static bearer service has no OAuth discovery
or token refresh; do not assume a cloud connector supports it without testing.
Any bridge must preserve authentication and the explicit Host/Origin boundary;
this adapter intentionally provides no bypass for external hostnames.

Official sources checked 2026-11-09/10:

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [ChatGPT custom MCP connections](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
- [Connect and test a ChatGPT plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [Claude remote connector network requirements](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [MCP Python SDK deployment and security](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/deploy.md)

Installed SDK source was inspected first: `MCPServer.streamable_http_app`,
`TransportSecurityMiddleware`, `TransportSecuritySettings` and the client transport.
