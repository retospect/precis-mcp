# precis-mcp — Codex shim

Codex-facing entry. Claude Code reads `CLAUDE.md`; both share one doc system.
This file holds only what is specific to Codex and similar tools.

## Read order

`docs/README.md` (the doc-system contract) → `docs/codebase.md` (shape,
lifecycle, seams) → the owning package's `__init__.py` docstring →
`docs/glossary.md` → `docs/backlog/INDEX.md`. Runtime kinds and verbs:
`get(kind='skill', id='precis-overview')`.

## Rules by pointer

- **Ship workflow** (worktree per task, `/land`, `/go`, `/qland`, gate,
  refs): `CLAUDE.md` §Ship workflow. Do not hand-roll a ship; use
  `scripts/ship`.
- **Invariants** (forward-only migrations, append-only body chunks,
  `safe_fetch`, `uv`): `docs/conventions/invariants.md`.
- **Conventions**: `docs/conventions/` (start with `thresholds.md`; prose
  style `llm-facing-prose.md`; code anchors `code-anchors.md`).
- **Tests and gate shape**: `docs/conventions/testing.md`. Run tests with
  `scripts/test`; never bare `pytest`, `pip` or `mypy`.
- **Plans**: non-trivial changes get a spec in `docs/backlog/<slug>.md`
  first; it is deleted in the shipping commit.

## Definition of done

- Lint, types and tests green (`uv run ruff check . && uv run ruff format
  --check .`, `scripts/test --typecheck`, `scripts/test`).
- The owning package docstring carries the "why" of any non-obvious
  trade-off; `docs/codebase.md` and affected skills updated if the shape
  changed.
- Schema change: a new numbered migration, old ones untouched.
- No secrets and no cluster addresses in the tree (the repo is public).
- Commit message: `type(scope): what changed`, ≤72 chars, optional short why
  as body (rules in `CLAUDE.md` §Conventions that bite; `scripts/ship` enforces
  them). No CHANGELOG; `git log` is the record.

## Agent sizing

Delegate down: start each task on the cheapest agent that fits. Per-agent
remits are in `.claude/agents/`. Mechanical work (search, extraction, test
runs, tidying) goes to the small tier; a decided edit to a coder, test
author or documenter; design, schema and domain judgment stay with the
main loop. Subagent prompts must forbid `docker ... prune` and forbid
writing scripts in the shared `/tmp`.
