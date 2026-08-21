# Claude Code — project brief

> **Two surfaces.** This repo **is** the precis MCP server. The session
> `precis` tools and `get(kind='skill')` skills are the *product's* runtime
> surface for cluster agents — not dev docs for this code.

Lean router: ship workflow, conventions that bite, pointers. The doc system
(where truth lives, how to keep it true) is defined once in `docs/README.md`.
Reading order: `docs/codebase.md` → owning package `__init__.py` docstring →
`docs/glossary.md` → `docs/backlog/INDEX.md`. Conventions/workflow/DoD:
`AGENTS.md`. Prose style: `docs/conventions/llm-facing-prose.md`.

## Ship workflow

Work happens in worktrees (`claude -w <name>`). **`/land`** = ship
(`scripts/ship --impacted`: commit WIP → sync main → container gate ruff +
mypy + impacted pytest → squash-merge to `main`). **`/go`** = ship with the
full suite + `scripts/deploy`. Both abort+report on gate failure and are
idempotent — fix and re-run. Merge target is `main` (no `master`). Red gate:
the failure is printed above the `✖` — read *that*, never `scripts/ship`.

Many sibling sessions run at once: scan the injected `scripts/inflight` table
for overlap; once your task is clear, write one line to `.claude/purpose`.
Merged+clean+sessionless worktrees auto-reap.

## Orientation

`docs/codebase.md` (shape, lifecycle, seams) → the owning package's
`__init__.py` docstring. Runtime kinds/affordances: skills `precis-overview`,
`precis-toolpath-help`. Coined terms: `docs/glossary.md`. Planned work:
`docs/backlog/` (open items only, delete-on-ship). Dated history: `git log`
(no CHANGELOG). Schema: `docs/reference/schema.md` (generated). Mission:
`docs/mission.md`. Replicate this setup: `docs/how-to-setup-like-this.md`.
Code: workers `src/precis/workers/`, ingest `src/precis/ingest/`, web UI
`src/precis_web/`, Discord bridge `src/asa_bot/`, SSRF guard
`src/precis/utils/safe_fetch.py`.

## Conventions that bite (irreversible, or reddens the ship)

- **Forward-only migrations.** Never edit a sealed
  `src/precis/migrations/*.sql` — ship a new one. Baseline regen via
  `scripts/bump` / `precis db dump-schema`, never by hand.
- **Don't mutate body chunks.** `chunks` is append-only for body rows
  (`ord >= 0`); "update" = DELETE + INSERT so the embedding/summary cascade
  re-runs — in-place UPDATE strands `chunk_embeddings`/`chunk_summaries`.
  Only `ord < 0` card variants DELETE/re-INSERT, via a registered synthesis
  pass.
- **Session `precis` MCP targets PROD** (write-capable `agent_rw`). Writes
  are sanctioned but land in production: write deliberately, prefer reads for
  exploration, and do write-path *testing* on the dev DB (`scripts/dev`) —
  never this MCP. Ad-hoc SQL: `scripts/prod-psql "SELECT …"` (prefer
  read-only); `scripts/db` is local-only.
- **Agent-supplied-URL fetches → `safe_get`/`safe_stream`**
  (`utils/safe_fetch.py`); raw follow-redirects httpx is an SSRF.
- **Embeddings come from the worker, not ingest** — ingest stores chunks
  `embedding IS NULL`; never call `fill_embeddings` from ingest.
- **`uv` for everything; tests via `scripts/test`** (container-mounted;
  `--impacted` narrows) — never bare pytest/pip/mypy. mypy scope is
  `src tests`.
- **Commit messages: one-line subject, no body** + the required
  Co-Authored-By/session footer.

## Hook/gate-enforced — one-liners, detail on demand

- Container-first; shell cwd is already this worktree — never `cd`; other
  trees via `git -C`. → `docs/conventions/container-ops.md`
- Text IO names `encoding="utf-8"` (ruff PLW1514 + AST-walk test; also
  `subprocess(..., text=True)`).
- `rtk` digests noisy Bash output — you see a filtered digest. →
  `docs/conventions/rtk.md`
- Read/Grep tools over cat/sed/bash-grep; no `echo "==="` narration; don't
  re-Read files already in context.
- Structure-aware first: `search_code` (MAIN repo path; index is lazy — Grep
  is truth for new code) and `scripts/coderef callers|deps <file.py::Sym>`
  before grepping bare symbols.
- Cite durable anchors, not line numbers, in docs/memory. →
  `docs/conventions/code-anchors.md`
- Bug intake → the `bug` skill before fixing; masked root cause → dispatch
  `root-cause` first.
- Skills are runtime docs: `src/precis/data/skills/`, served via
  `get(kind='skill')`.
- Sibling branches' trivial drift (needs `ruff`): just fix it.

## Agent sizing

Main loop bills big — delegating down is the primary cost lever; start
cheap. → `AGENTS.md` §Agent sizing; per-agent remits in `.claude/agents/`.
