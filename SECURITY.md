<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Security policy

## Report a vulnerability privately

Use GitHub's private vulnerability reporting for this repository:
[Report a vulnerability](https://github.com/pacappkor/kapa_mcp/security/advisories/new).
If that option is unavailable, ask maintainers to enable private vulnerability reporting
without disclosing vulnerability details in a public issue or pull request.

Include the affected commit/version, expected and observed behavior, impact, and a
minimal reproduction using synthetic data. Do not submit tokens, credentials,
personal paths, retrieved source content, or real datasets. Coordinate disclosure
through the private advisory; no response-time guarantee is made.

## Security boundaries

The MCP server is local-only: stdio by default, or explicitly enabled authenticated
HTTP bound to `127.0.0.1`. Public endpoints and tunnels are not supported.
Repository visibility is not an authorization signal and does not relax bearer,
Host, Origin, filesystem, or upstream read-only boundaries. The adapter does not
publish content or write to upstream repositories. Keep credentials outside all
repositories and provide secrets only through the documented local mechanism.

Only run reviewed, trusted upstream checkouts and TeX on a trusted host. Renderer
limits are not an OS or network sandbox. Public source availability does not
establish legal accuracy, official-answer status, or permission to redistribute
retrieved material. See [README](README.md) for limitations and local tests.
