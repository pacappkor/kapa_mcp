# SPDX-License-Identifier: GPL-3.0-only
"""Stdio by default; explicit authenticated loopback HTTP is optional."""

import sys

from .config import ConfigurationError, HTTPSettings, Settings, transport_from_env


def main() -> int:
    try:
        settings = Settings.from_env()
        transport = transport_from_env()
        http = HTTPSettings.from_env() if transport == "streamable-http" else None
    except (ConfigurationError, OSError, ValueError):
        # ConfigurationError messages contain variable names, never supplied values.
        error = sys.exception()
        message = str(error) if isinstance(error, ConfigurationError) else "invalid directory configuration"
        print(f"kapa-mcp: configuration error: {message}", file=sys.stderr)
        return 2
    try:
        from .server import create_server

        server = create_server(settings)
        if http is None:
            server.run(transport="stdio")
        else:
            from .remote import run_http

            run_http(server, http)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("kapa-mcp: server failed; verify the pinned SDK and local adapter installation", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
