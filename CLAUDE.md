# Claude Code — project brief

This repo is the precis MCP server. The session `precis` tools and
`get(kind='skill')` skills are the product's runtime surface for cluster
agents, not dev docs for this code.

Start with memory: `search(kind='memory', tags=['SPACE:repo-dev'],
q='<your task>')`, then `docs/codebase.md` and the owning package's
`__init__.py` docstring. Terms: `search(kind='taxon', q='<term>',
args={'under': 'tn559460'})`. Conventions, workflow, DoD: `AGENTS.md`.

## Hard rules

- Own worktree per task (`claude -w <name>`); never edit or commit in the
  primary `main` checkout.
- The repo is PUBLIC: no cluster addresses, node hostnames or secrets in
  committed text.
- The session `precis` MCP writes to PROD; destructive, outward-facing or
  >$25 actions need Reto (`docs/conventions/thresholds.md`).
- Migrations are forward-only; never edit a sealed one. Body `chunks` are
  append-only (DELETE + INSERT).
- Agent-supplied URLs go through `safe_get`/`safe_stream`.

## Ship

`scripts/ship --quick` for code, `--remote` for harness diffs
(`scripts/ship`, `scripts/test`, `scripts/lib/*`, `.github/*`), then
`scripts/release`. Run it in the background to a log; on red read above
the `✖`. Tests via `scripts/test`, Python via `uv`. Commit with
`git commit -F <file>` plus the session footer. Long form:
`docs/conventions/shipping.md`.
