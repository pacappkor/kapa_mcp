<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Verify and maintain

## Verify installation

1. Run `claude mcp list` for Claude Code health. In a session, `/mcp` shows server
   status and tools. For Codex, `codex mcp list` confirms registration only; use
   `/mcp` in a session to inspect discovered tools. In Desktop inspect the local
   server status/tool list after restarting.
2. Confirm initialization succeeds and **17 tools** are discovered, including
   `kapa_list_repositories` and `kapa_route_task`.
3. Ask the client to call `kapa_list_repositories()` and compare all four revision
   SHAs with the installed adapter's
   [upstream_pins.json](https://github.com/pacappkor/kapa_mcp/blob/main/src/kapa_mcp/upstream_pins.json)
   at **your selected ref**, not necessarily current main. Check dirty flags and
   all warnings. Then call `kapa_route_task` with a read-only research task and
   read a returned entry document. Do not include rendering in this health check.

A registration listing alone does not prove a protocol handshake. SDK tests are
not native chat acceptance; successful reads are not legal verification.

### Important current dependency warning

The bundled `kapa_skills` main snapshot is
`f06f552573655cbab70480b2785caa6c738939ac`, while the pinned knowledge
snapshot's `deps.lock` references skills
`b8d9d8d93e59d0a7da29d0fe120177ea9b0d1b02`. Therefore a successful setup can
still report `version_unresolved`. This is intentional transparency: setup pins
are installation snapshots, not a replacement for upstream dependency locks.
Do not edit the lock, suppress warnings, or claim full evidence reproducibility.
Pinned vendored metadata has its own provenance checks and still does not prove
historical article text. A future reviewed upstream release must reconcile the
pins; the installer does not modify upstream content to make checks pass.

## Update

Stop your owned servers and all other writers first; preserve existing jobs and
local changes. Choose a new reviewed adapter ref, inspect its pins, and use the
same workspace with the explicit flag. `--update` means **bundled pins**, never
fetch-and-follow-latest or `git pull`.

PowerShell (reuse the variables from installation, change `$ref` first):

```powershell
$ref = 'NEW_REVIEWED_TAG_OR_FULL_COMMIT'
$package = "git+https://github.com/pacappkor/kapa_mcp@$ref"
uvx --from $package kapa-mcp-setup --workspace $workspace --update
if ($LASTEXITCODE -ne 0) { throw 'Update failed; inspect before restarting' }
```

macOS/Linux:

```sh
ref='NEW_REVIEWED_TAG_OR_FULL_COMMIT'
package="git+https://github.com/pacappkor/kapa_mcp@$ref"
uvx --from "$package" kapa-mcp-setup --workspace "$workspace" --update || exit 1
```

Manually change the client command's ref too, then restart and repeat verification.
No client configuration is automatically changed. Updates require a clean
installer-managed detached HEAD and matching origin/previous marker. Modified
HEADs, branches, untracked/ignored files and hidden index flags are refused.
Use a new workspace instead of resetting a personal checkout. Installation is
not a four-repository transaction: a network failure can leave earlier repos
updated or a partial clone. Inspect it; retry only once the state is understood,
or choose a new workspace. Nothing is automatically deleted.

Existing evidence/plans may be stale after updates: validate bundles, reread
sources and create new plans when needed. Preserve warnings and output files.

## Uninstall

Stop your owned server/client. Run `claude mcp remove kapa --scope local` or
`codex mcp remove kapa` for the registration you created; for Desktop remove only
its `mcpServers.kapa` entry manually. For manual Codex TOML remove only the KAPA
section. Restart the client. uvx uses a cache rather than a permanent tool
installation; removing the registration stops automatic launches. Keep workspace
and jobs for archival, or manually delete only those dedicated directories after
backing up needed evidence/PDFs. The adapter never cleans them automatically.

## Troubleshooting

- **Executable not found:** check Git/uvx on PATH. For GUI clients use absolute
  executable paths; restart the app after installing prerequisites.
- **Missing environment/configuration error:** supply both absolute directory
  variables in the client configuration; shell exports alone may not reach a GUI.
- **Wrong jobs path:** choose `workspace/_kapa_jobs`, outside every source repo.
  Symlinks, Windows junctions and redirected directories are rejected.
- **Target mismatch/dirty:** preserve your files; use a new dedicated workspace.
  Do not bypass the refusal with force/reset/clean or edit the setup marker.
- **Git ownership error:** use a workspace owned by your account. The installer
  never changes global safe-directory settings.
- **Missing pin/network failure:** confirm the chosen adapter revision and public
  GitHub availability. Do not replace the SHA with a branch or disable TLS checks.
- **Startup appears idle in a terminal:** stdio waits for MCP messages, not typed
  chat. Let the client launch it. stdout is protocol-only.
- **Missing TeX/fonts:** reading still works; install the trusted upstream rendering
  prerequisites separately. Do not treat missing glyphs as successful rendering.
- **HTTP:** optional only; see the repository's
  [authenticated loopback guide](https://github.com/pacappkor/kapa_mcp/blob/main/examples/private-http.md).
  Keep exact numeric loopback host, valid bearer, Host/Origin checks and no tunnel.

## Documentation maintenance

Docs dependencies are exact direct pins in `requirements-docs.txt`, separate
from runtime dependencies (transitive packages are not hash-locked). In an
isolated venv install that file and run `mkdocs build --strict`. CI builds on PRs;
the Pages workflow deploys only from main. A maintainer must select **GitHub
Actions** as the repository Pages source. This publishes static documentation,
never the MCP server, and does not require a package upload.
