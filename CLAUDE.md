# Claude Code — project brief

> **Two surfaces.** This repo **is** the precis MCP server. The session
> `precis` tools and `get(kind='skill')` skills are the *product's* runtime
> surface for cluster agents — not dev docs for this code.

Lean router: ship workflow, conventions that bite, pointers. The doc system
(where truth lives, how to keep it true) is defined once in `docs/README.md`.
Reading order: `docs/codebase.md` → owning package `__init__.py` docstring →
`docs/glossary.md` → `docs/backlog/INDEX.md`. Conventions/workflow/DoD:
`AGENTS.md`. Prose style: `docs/conventions/llm-facing-prose.md`.

Repo-dev recall, durable writes and outage behavior:
[AGENTS.md — Repo-dev graph memory](AGENTS.md#repo-dev-graph-memory).

## Ship workflow

Work always happens on a task branch in a separate worktree (`claude -w <name>`).
Never edit or commit in the primary `main` checkout; integrate branches in
a dedicated integration worktree. Mechanics: `docs/conventions/shipping.md`.

- **`/land`** — `scripts/ship --remote`: GitHub's check.yml gate, then CAS
  squash-merge to `main`. Run ship in background, output to a log.
- **`/go`** — local suite + diff-coverage gate, then `scripts/deploy --pinned`
  of the gated sha (never bare `scripts/deploy`).
- **`/qland`** — `scripts/ship --quick`: lint only, no pytest. Burst-land many
  trees, then one `/go`.
- **`/qgo`** — qland + ungated deploy. Never for `*/migrations/*.sql` or
  `safe_fetch.py` (`scripts/qgo-guard` refuses; those take `/go`).

All are idempotent — fix and re-run. Red gate: read the failure above the
`✖`, not `scripts/ship`. Refs: `main` (landed), `origin/gated` (last
full-gate green), `origin/prod` (what runs) — script-moved, never committed to.

Many sibling sessions run at once: scan the injected `scripts/inflight` table
for overlap; once your task is clear, write one line to `.claude/purpose`.
When a coordinator has a round open (`scripts/round status`), mark it from
your tree (`scripts/round in <sha>|none|eta <text>`) instead of messaging,
and leave deploys to the coordinator. Fleet: `/fleet`; in a fleet session a
question for Reto goes to the review queue (`.claude/fleet/review-protocol.md`).
Merged+clean+sessionless worktrees auto-reap. Work that belongs to a thread
(`docs/backlog/threads/<slug>.md`) updates that file in the same commit —
delete what shipped, insert what you filed at its rank (README there).

## Orientation

`docs/codebase.md` (shape, lifecycle, seams) → the owning package's
`__init__.py` docstring. Runtime kinds/affordances: skills `precis-overview`,
`precis-toolpath-help`. Coined terms: `docs/glossary.md`. Planned work:
`docs/backlog/` (open items only, delete-on-ship); pillars + the active
thread set: `docs/roadmap.md`. Dated history: `git log`
(no CHANGELOG). Schema: `docs/reference/schema.md` (generated). Mission:
`docs/mission.md`. Replicate this setup: `docs/how-to-setup-like-this.md`.
Install/run the product: `docs/setup-single-machine.md` · cluster:
`deploy/README.md`.
Code: workers `src/precis/workers/`, ingest `src/precis/ingest/`, web UI
`src/precis_web/`, Discord bridge `src/asa_bot/`, SSRF guard
`src/precis/utils/safe_fetch.py`.

## Conventions that bite (irreversible, or reddens the ship)

- **Forward-only migrations.** Never edit a sealed migration; ship a new one.
  → `docs/conventions/invariants.md`
- **Don't mutate body chunks.** Body `chunks` rows are append-only: DELETE +
  INSERT, never UPDATE. → `docs/conventions/invariants.md`
- **Session `precis` MCP targets PROD** (write-capable `agent_rw`). Writes
  land in production and need no ask unless destructive, outward-facing or
  over $25 (`docs/conventions/thresholds.md`, Reto 2026-10-01); do
  write-path *testing* on the dev DB (`scripts/dev`) — never this MCP. Ad-hoc SQL: `scripts/prod-psql "SELECT …"` (prefer
  read-only); `scripts/db` is local-only.
- **Agent-supplied-URL fetches → `safe_get`/`safe_stream`.**
  → `docs/conventions/invariants.md`
- **No cluster addresses anywhere — the repo is PUBLIC.** Tailnet/LAN IPs and
  vault blobs are gated across the *whole* tree
  (`tests/test_deploy_tree_no_secrets.py`); real node hostnames are gated under
  `deploy/` only (there they mean an un-parameterised role). Prod coordinates
  come from the gitignored overlay via `scripts/lib/pgb-host.sh` — use
  `scripts/prod-psql`, never a literal. Genuine ranges/samples take a
  `secret-gate: allow — <reason>` marker.
- **Embeddings come from the worker, not ingest** — ingest stores chunks
  `embedding IS NULL`; never call `fill_embeddings` from ingest.
- **`uv` for everything; tests via `scripts/test`** — never bare
  pytest/pip/mypy. → `docs/conventions/invariants.md`
- **Commit messages: `type(scope): what changed`** (≤72 chars;
  feat/fix/docs/test/chore/refactor…; `docs(backlog)` for specs), optional
  short why as body, then the required Co-Authored-By/session footer.
  `scripts/ship` reuses your branch's single commit message or takes `-m`; it
  refuses `ship(...)`/`wip(...)` placeholders and unprefixed subjects.

## Hook/gate-enforced — one-liners, detail on demand

- Container-first; shell cwd is already this worktree — never `cd`. Other
  trees are NOT reachable by `git -C` (the harness refuses it) — read them
  with `scripts/inflight --json`. → `docs/conventions/container-ops.md`
- Text IO names `encoding="utf-8"` (ruff PLW1514 + AST-walk test; also
  `subprocess(..., text=True)`).
- **Timestamps are UTC, labelled `Z`/`UTC`** — `date -u`, `datetime.now(UTC)`,
  never `date.today()`/`utcnow()` (`date +%s` is fine). →
  `docs/conventions/time.md`
- `rtk` digests noisy Bash output — you see a filtered digest. →
  `docs/conventions/rtk.md`
- Never pipe `scripts/ship|deploy|bump` into `tail`/`grep`/`tee` — a pipeline
  reports the *filter's* status, so a red gate reads as exit 0. Redirect to a
  log, or `set -o pipefail`.
- Read/Grep tools over cat/sed/bash-grep; no `echo "==="` narration; don't
  re-Read files already in context.
- Structure-aware first: the session MCP's python kind
  (`search(kind='python', mode='pattern', q=...)`, `get(kind='python',
  id='main::<qualname>')` for signature + callers + callees) and
  `scripts/coderef callers|deps <file.py::Sym>` before grepping bare symbols.
  It reads MAIN (`main::` is the live main checkout), not this worktree — Grep
  is truth for code you changed here. Skill `precis-python-help`.
- Cite durable anchors, not line numbers, in docs/memory. →
  `docs/conventions/code-anchors.md`
- Bug intake → the `bug` skill before fixing; masked root cause → dispatch
  `root-cause` first.
- Skills are runtime docs: `src/precis/data/skills/`, served via
  `get(kind='skill')`.
- Sibling branches' trivial drift (needs `ruff`): just fix it, in its own commit.

## Response style (Reto — busy; BLUF always)

Bottom line up front, then supporting detail. Plain, specific language over
quotable phrasing — if a sentence would fit unchanged in a different
conversation, cut it or make it specific. Direct ≠ terse: give full
reasoning, minus the editorializing.

- No validation-as-move ("that's valid", "not your fault") and no reflexive
  agreement/praise ("you're absolutely right", "great question"). Agree when
  earned and say why; don't manufacture disagreement either.
- No performed insight: aphorisms, metaphors, named "tensions".
- Cut ceremony, not reasoning: no preamble/recap, no tool-call narration, no
  filler/hedges (just, really, basically, actually, it's worth noting), no
  pleasantries, no emoji, no decorative headers on short answers.
- Quote the shortest decisive line + `path:line`; never dump logs/files/diffs.
- Each fact once per response; don't re-derive what's established.
- No invented abbreviations (cfg, impl, req) — they save nothing.
- EXEMPT from compression: security warnings, confirmations for
  destructive/irreversible actions, and ordered multi-step instructions.

## Agent sizing

Main loop bills big — delegating down is the primary cost lever; start
cheap. → `AGENTS.md` §Agent sizing; per-agent remits in `.claude/agents/`.
