# Claude Code — project brief

> **Two surfaces.** This repo **is** the precis MCP server. The session
> `precis` tools and `get(kind='skill')` skills are the *product's* runtime
> surface for cluster agents — not dev docs for this code.

Reading order: `docs/codebase.md` → owning package `__init__.py` docstring
→ an unfamiliar or overloaded term: `search(kind='taxon', q='<term>',
args={'under': 'tn559460'})` (the vocabulary taxonomy; a term you coin is a
taxon under its hub there, never a file) → `docs/backlog/INDEX.md`.
Conventions, workflow, DoD,
repo-dev memory, prose style: `AGENTS.md`. Long form of every rule here:
`docs/conventions/shipping.md` and `AGENTS.md` §Long form (budget 6,000 B).

## Ship workflow

Always a task branch in its own worktree (`claude -w <name>`); never edit or
commit in the primary `main` checkout. Mechanics: `docs/conventions/shipping.md`.

- `/land` — `scripts/ship --remote`: GitHub gate, CAS squash-merge. Harness
  diffs only (`scripts/ship`, `scripts/test`, `scripts/lib/*`, `.github/*`).
- `/go` — `--quick` to main, then `scripts/release`: cut `release/r<N>`,
  gate it, deploy that head pinned.
- `/qland` — `scripts/ship --quick`: lint only, no pytest; the default lane
  for every non-harness diff. `--checked` adds the selected pytest
  (recommended burst lane). Burst-land many trees, then one `/go`.
- `/qgo` — qland + ungated deploy; never for `*/migrations/*.sql` or
  `safe_fetch.py`.

All idempotent — fix and re-run; on red read the failure above the `✖`. Run
ship in the background to a log; never pipe `ship|deploy|bump` into
`tail`/`grep`/`tee`. Refs `main`, `origin/gated`, `origin/prod`,
`release/r<N>` are script-moved. Before starting and before touching shared
files: `scripts/inflight`; then one line to `.claude/purpose`. Open round →
`scripts/round in <sha>|none|eta <text>`. Fleet session: deploys/`/go` are
the coordinator's; questions for Reto → `ask-user` todo
(`precis-work-help`). Thread work updates `docs/backlog/threads/<slug>.md`
in the same commit.

Commits: `type(scope): what changed` aim ≤72 chars, hard limit 120, optional why, then the
Co-Authored-By/session footer; write it to a file and `git commit -F <file>`
(never a quoted `-m`). Devin footer: `Co-Authored-By: Devin SWE-2
<noreply@cognition.ai>` + `Claude-Session: none (Devin builder, <fleet name>)`.

## Orientation

- Specialist work: `search(kind='skill', q='<the task>')` first — assume a
  skill exists until search says otherwise; missing → gripe.
- Code: workers `src/precis/workers/`, ingest `src/precis/ingest/`, web
  `src/precis_web/`, Discord `src/asa_bot/`, SSRF guard
  `src/precis/utils/safe_fetch.py`.
- Backlog `docs/backlog/` (open items, delete-on-ship); pillars
  `docs/roadmap.md`; history `git log`; schema `docs/reference/schema.md`;
  cluster `deploy/README.md`.
- se / CAD / mechanical: the `se-design` skill — the product's se skills
  are the spec.

## Conventions that bite (irreversible, or reddens the ship)

- Forward-only migrations; never edit a sealed one. →
  `docs/conventions/invariants.md`
- Body `chunks` are append-only: DELETE + INSERT, never UPDATE.
- The session `precis` MCP targets PROD (`agent_rw`). Writes need no ask
  unless destructive, outward-facing or over $25
  (`docs/conventions/thresholds.md`); write-path *testing* on the dev DB
  (`scripts/dev`). Ad-hoc SQL: `scripts/prod-psql` (read-only preferred);
  `scripts/db` is local-only.
- Agent-supplied URLs → `safe_get`/`safe_stream`.
- The repo is PUBLIC: no cluster addresses anywhere (gated tree-wide); real
  node hostnames only under `deploy/`; prod coordinates via
  `scripts/lib/pgb-host.sh`. Genuine samples take
  `secret-gate: allow — <reason>`.
- Embeddings come from the worker, never ingest (`fill_embeddings`).
- `uv` for everything; tests via `scripts/test` — never bare
  pytest/pip/mypy.

## Hook/gate-enforced

- Container-first; never `cd`; other trees are not reachable by `git -C` —
  read them with `scripts/inflight --json`. →
  `docs/conventions/container-ops.md`
- One plain command per Bash call: no loops, heredocs, `$(…)`,
  `&&`/`;`/`||` chains, or shell variables holding paths. Write a script,
  run it.
- Text IO names `encoding="utf-8"` (also `subprocess(..., text=True)`).
- MCP handler errors: do what the ack says or refuse with a next step. →
  `docs/conventions/mcp-authoring.md`
- Timestamps UTC, labelled `Z`/`UTC`; never `date.today()`/`utcnow()`. →
  `docs/conventions/time.md`
- Read/Grep tools over cat/sed/grep; no `echo "==="` narration; don't
  re-Read files already in context.
- Structure first: the python kind (`search(kind='python',
  mode='pattern')`, `get(kind='python', id='main::<qualname>')`,
  `wt-<tree>::` for your tree) and `scripts/coderef`; repo prose via the md
  kind (`get(kind='md', id='main/docs/…')`). Grep is truth for code you
  changed. Skills `precis-python-help`, `precis-md-help`.
- Product data through the session MCP first; `scripts/prod-psql` only when
  the MCP has no affordance — then gripe the missing one.
- Cite durable anchors, not line numbers. → `docs/conventions/code-anchors.md`
- Bug intake → the `bug` skill; masked root cause → `root-cause` first.
- Skills are runtime docs: `src/precis/data/skills/`.

## Response style (Reto — busy; BLUF always)

Bottom line first, then detail. Plain, specific language; if a sentence
would fit unchanged in another conversation, cut it. Direct ≠ terse: full
reasoning, no editorializing. No validation-as-move, no reflexive agreement
or praise; agree when earned and say why. No performed insight (aphorisms,
metaphors, named "tensions"). No preamble, recap, tool-call narration,
filler, hedges, pleasantries, emoji, or decorative headers on short answers.
Quote the shortest decisive line + `path:line`; never dump logs/files/diffs.
Each fact once. No invented abbreviations. EXEMPT: security warnings,
confirmations for destructive actions, ordered multi-step instructions.

## Agent sizing

Main loop bills big — delegate down; start cheap. → `AGENTS.md` §Agent
sizing; remits in `.claude/agents/`.
