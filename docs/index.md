<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Local KAPA MCP

Connect stored Korean appraisal-exam questions and research sources to Claude Code,
Claude Desktop or Codex. Start with [installation](install.md), then [verification](maintenance.md).

## Safety boundaries

- Local stdio by default. Optional HTTP is authenticated and binds only to
  `127.0.0.1`; no public endpoint, proxy or tunnel is provided.
- Public repository visibility is not authorization. Client tools can send retrieved
  material to their chosen model service; check your sharing policy.
- The running server reads upstream repositories only. It does not collect data,
  refresh sources, call law/model APIs or edit client configurations.
- The separate, explicitly invoked installer creates dedicated snapshots and can
  update only clean installer-managed detached checkouts. Never point it at a
  working research workspace. Stop servers and other writers before updating.
- PyPI uploads remain blocked (`Private :: Do Not Upload`). Installation is from
  reviewed Git source, not publication of a package.
- Rendering writes only isolated job directories. TeX and source checkouts must
  be trusted; file/time limits and disabled shell escape are not an OS sandbox.

**No legal accuracy or official-answer guarantee.** Search relevance, pinned
commits and reproducible evidence do not establish legal correctness, complete
historical law coverage or official second-exam answers. Preserve warnings and
`legal_verified=false`.

## Prerequisites

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
[Git](https://git-scm.com/downloads), and your chosen MCP client. Python 3.12+
is required (uv can provision it). Check `uv --version` and `git --version`.
Installation needs GitHub and Python-package-index access. No API key or Git token
is required for these public repositories.

Optional PDF rendering also needs XeLaTeX, the upstream TeX packages and Korean
fonts. Reading/searching needs no TeX. Do not enable rendering for untrusted inputs.

The server needs four sibling folders under `KAPA_WORKSPACE`: `kapa_edu_1`,
`kapa_edu_2`, `kapa_skills`, `kapa_knowledge`. Jobs belong in the separate sibling
`_kapa_jobs`, never within any source repository or at the workspace root.
Paths must be absolute and must not contain symlinks/reparse points.

## Market data: coming later

`kapa-data`, the market-data MCP from `pacappkor/kapa_runtime`, is **not yet
available to outside users (coming later)**. It is not installed or enabled by
these commands.
