# SPDX-License-Identifier: GPL-3.0-only
"""Authenticated loopback Streamable HTTP, without proxy or public bind modes."""

import hmac

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse

from .config import HTTPSettings


class _PrivateGate:
    def __init__(self, app, settings: HTTPSettings):
        self.app = app
        self.authority = f"127.0.0.1:{settings.port}".encode("ascii")
        self.origin = b"http://" + self.authority
        self._credential = b"Bearer " + settings.token.encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Validate before SDK security logging; never echo supplied headers/URL.
        headers = scope.get("headers", [])
        def values(name):
            return [v for k, v in headers if k.lower() == name]
        status, message = 0, ""
        if values(b"host") != [self.authority]:
            status, message = 421, "Invalid Host"
        elif values(b"origin") not in ([], [self.origin]):
            status, message = 403, "Invalid Origin"
        elif scope.get("query_string"):
            status, message = 400, "Query strings are not accepted"
        else:
            credentials = values(b"authorization")
            if len(credentials) != 1 or not hmac.compare_digest(credentials[0], self._credential):
                status, message = 401, "Bearer authentication required"
        if status:
            response = PlainTextResponse(message, status_code=status, headers={
                "Cache-Control": "no-store",
                **({"WWW-Authenticate": "Bearer"} if status == 401 else {}),
            })
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_http_app(server, settings: HTTPSettings):
    """Wrap the official SDK ASGI app, retaining its lifespan/session handling."""
    authority = f"127.0.0.1:{settings.port}"
    app = server.streamable_http_app(
        host="127.0.0.1", json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[authority], allowed_origins=[f"http://{authority}"],
        ),
    )
    return _PrivateGate(app, settings)


def run_http(server, settings: HTTPSettings) -> None:
    import uvicorn

    uvicorn.run(create_http_app(server, settings), host="127.0.0.1", port=settings.port,
                log_level="critical", access_log=False, proxy_headers=False, server_header=False)
