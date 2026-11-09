# SPDX-License-Identifier: GPL-3.0-only
"""Stdio entry point. Stdout is reserved exclusively for MCP protocol traffic."""

import sys

from .config import ConfigurationError, Settings


def main() -> int:
    try:
        settings = Settings.from_env()
    except (ConfigurationError, OSError, ValueError):
        # ConfigurationError messages contain variable names, never supplied values.
        error = sys.exception()
        message = str(error) if isinstance(error, ConfigurationError) else "invalid directory configuration"
        print(f"kapa-mcp: configuration error: {message}", file=sys.stderr)
        return 2
    try:
        from .server import create_server

        create_server(settings).run(transport="stdio")
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("kapa-mcp: server failed; verify the pinned SDK and local adapter installation", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
