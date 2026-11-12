<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Install and connect

## Choose a reviewed revision

Replace `REVIEWED_TAG_OR_FULL_COMMIT` below with a published tag or full commit
SHA containing this installer. Prefer an immutable full SHA; do not use `main`
for reproducible installations. Use the **same ref** for setup and every client.
These instructions become available when this change is merged/published; no
release tag is assumed to exist.

`uvx --from git+https://github.com/pacappkor/kapa_mcp@<ref> kapa-mcp` runs the
existing console entry point. Git installation resolves Python dependencies;
it does **not** apply the project's `uv.lock` to transitive dependencies. For
locked dependencies, clone the reviewed adapter revision and use
`uv sync --locked --python 3.12`, then the installed `kapa-mcp` executable.
The SDK direct requirement stays `mcp==2.3.0`.

## Windows PowerShell

Run in a normal terminal. Choose a new dedicated directory on a local disk,
not a redirected/symlinked home folder. Replace the ref before copying commands.

```powershell
$ref = 'REVIEWED_TAG_OR_FULL_COMMIT'
$package = "git+https://github.com/pacappkor/kapa_mcp@$ref"
$workspace = Join-Path $HOME 'kapa-workspace'
$jobs = Join-Path $workspace '_kapa_jobs'
uvx --from $package kapa-mcp-setup --workspace $workspace
if ($LASTEXITCODE -ne 0) { throw 'Setup failed; do not register the server' }
$env:KAPA_WORKSPACE = $workspace
$env:KAPA_JOBS_DIR = $jobs
$env:KAPA_TRANSPORT = 'stdio'
```

Choose either or both CLI clients (these commands intentionally register a server
only when **you** run them; the installer never edits client configuration).

```powershell
claude mcp add --scope local --transport stdio --env "KAPA_WORKSPACE=$workspace" --env "KAPA_JOBS_DIR=$jobs" --env KAPA_TRANSPORT=stdio kapa -- uvx --from $package kapa-mcp
codex mcp add kapa --env "KAPA_WORKSPACE=$workspace" --env "KAPA_JOBS_DIR=$jobs" --env KAPA_TRANSPORT=stdio -- uvx --from $package kapa-mcp
```

## macOS / Linux

```sh
ref='REVIEWED_TAG_OR_FULL_COMMIT'
package="git+https://github.com/pacappkor/kapa_mcp@$ref"
workspace="$HOME/kapa-workspace"
jobs="$workspace/_kapa_jobs"
uvx --from "$package" kapa-mcp-setup --workspace "$workspace" || exit 1
export KAPA_WORKSPACE="$workspace"
export KAPA_JOBS_DIR="$jobs"
export KAPA_TRANSPORT=stdio
claude mcp add --scope local --transport stdio --env "KAPA_WORKSPACE=$workspace" --env "KAPA_JOBS_DIR=$jobs" --env KAPA_TRANSPORT=stdio kapa -- uvx --from "$package" kapa-mcp
codex mcp add kapa --env "KAPA_WORKSPACE=$workspace" --env "KAPA_JOBS_DIR=$jobs" --env KAPA_TRANSPORT=stdio -- uvx --from "$package" kapa-mcp
```

Claude Code local scope applies to the current project. Run its command in the
project where you want KAPA available. The installer prints both shell forms of
the required environment variables. It clones all four public repos, checks out
the bundled full SHAs in detached HEAD mode and verifies each HEAD. Existing
non-matching directories are refused, not reset or cleaned.

## Claude Desktop (local stdio)

Use Settings → Developer → Edit Config, then manually merge the following entry
into `mcpServers`; preserve other servers. The usual file is
`%APPDATA%\Claude\claude_desktop_config.json` on Windows or
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS.
This is a local Desktop integration, not a cloud remote connector. Desktop's
availability/support on Linux is client-dependent.

Replace all placeholders with absolute paths and the same reviewed ref. GUI apps
may not inherit terminal PATH: use the full path from `Get-Command uvx` on
PowerShell or `command -v uvx` on macOS/Linux. JSON does not expand `$HOME` or `~`.
Use forward slashes for Windows paths, or escape backslashes as `\\`.

```json
{
  "mcpServers": {
    "kapa": {
      "command": "ABSOLUTE_PATH_TO_UVX",
      "args": ["--from", "git+https://github.com/pacappkor/kapa_mcp@REVIEWED_TAG_OR_FULL_COMMIT", "kapa-mcp"],
      "env": {
        "KAPA_WORKSPACE": "ABSOLUTE_WORKSPACE_DIRECTORY",
        "KAPA_JOBS_DIR": "ABSOLUTE_WORKSPACE_DIRECTORY/_kapa_jobs",
        "KAPA_TRANSPORT": "stdio"
      }
    }
  }
}
```

Quit and restart Desktop after saving. No token or model API key belongs here.

## Codex TOML alternative

Instead of `codex mcp add`, manually merge into `~/.codex/config.toml` (or your
explicit `CODEX_HOME/config.toml`). Do not duplicate an existing `kapa` entry.
Replace placeholders; TOML literal single-quoted paths can contain Windows
backslashes. Neither shell variables nor `~` expand in these values.

```toml
[mcp_servers.kapa]
command = 'ABSOLUTE_PATH_TO_UVX'
args = ['--from', 'git+https://github.com/pacappkor/kapa_mcp@REVIEWED_TAG_OR_FULL_COMMIT', 'kapa-mcp']

[mcp_servers.kapa.env]
KAPA_WORKSPACE = 'ABSOLUTE_WORKSPACE_DIRECTORY'
KAPA_JOBS_DIR = 'ABSOLUTE_WORKSPACE_DIRECTORY/_kapa_jobs'
KAPA_TRANSPORT = 'stdio'
```

## Command sources

CLI flags were checked against installed `claude mcp add --help` and
`codex mcp add --help`. Official references:
[Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), and
[local Desktop server configuration](https://modelcontextprotocol.io/docs/develop/connect-local-servers).
Follow [verification](maintenance.md) before relying on a connection. The
instructions do not claim new native client handshake acceptance.
