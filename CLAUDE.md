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

Work happens in worktrees (`claude -w <name>`). **`/land`** = ship with the
gate on GitHub (`scripts/ship --remote`: commit WIP → sync main → push
`ci/<branch>` → wait for the check.yml gate (lint + 6 Linux shards, ~12 min;
docs-only diffs get a ~5 min docs lane; 3.12/macOS/Windows run nightly — run
ship in background, output to a log) → atomic CAS squash-merge to `main` —
a full-gate ship lands an exactly-tested tree, or says it did not and pins
nothing. The repo-wide ship lock is narrow: held only for the seconds of
fetch → squash → CAS push → local-main ff, never across sync, lint, a gate
or the CI wait. If main moves meanwhile, ship re-syncs and re-runs CI
(up to 2×), then one hybrid local gate (~10 min, also unlocked); when that
budget is spent it lands by an in-lock forward merge of main instead,
prints "not a deploy warrant", writes no `.ship-sha` and moves no `gated`
ref, and the squash carries a `Gate: forward-merged over N commits` trailer
(`--quick` forward-merges the same way when main moved, with a
`Gate: lint only` trailer, or `Gate: none` under `PRECIS_QLAND_LINT=0`). Squawk on new
migration SQL stays host-side. `--remote --impacted` = opt-in local impacted
pre-gate first; bare `--impacted` = legacy local-only gate). **`/go`** = ship
with the LOCAL suite + diff-coverage gate (changed src lines need
tests; the gate's default lane is `-m 'not slow'` — the slow cluster is
covered by check.yml's unfiltered 6 shards on every push, `--slow` /
`PRECIS_GATE_SLOW=1` restores the full set) + `scripts/deploy` of the
**gated sha** (`--pinned`, never bare —
bare re-resolves `main` and can ship an ungated sibling qland), plus a
budgeted advisory mutation pass
(`scripts/mutate-diff`). **`/qland`** = pytest-ungated burst-land
(`scripts/ship --quick`: commit WIP → sync → **drift guard** (warns when main's
last all-green shard matrix is 24h old, refuses at 48h — a burst that outran
its verdicts wants a `/go`, not another qland; never refuses on an age it
could not look up) → **pre-qland lint** (ruff · mypy ·
import contracts · DB-free hygiene tests (type-ignore ratchet, posix/encoding guards, doc pointers, secret scan) — no full pytest, no gate slot, ~3 min; `PRECIS_QLAND_LINT=0` to
skip) → squash-merge) for when
many trees are in flight — qland them one by one, then one
`/go` gates the integrated `main` + deploys (ship skips the push when the
tree already equals main). **`/qgo`** = the fast dev cycle: qland + deploy
that sha **ungated**, then a repair gate only if a slot is free (never
queued — queuing starves the 2-slot semaphore). Prod may run broken code
until the next pass: accepted on a dev cluster, and `scripts/qgo-guard`
hard-refuses the two things prod cannot take back (any
`*/migrations/*.sql`, `safe_fetch.py`) — those take `/go`.
All abort+report on failure and are idempotent —
fix and re-run. Merge target is `main` (no `master`). Red gate: the failure
is printed above the `✖` — read *that*, never `scripts/ship` (remote-gate
red: ship prints the failing jobs + `gh run view <id> --log-failed`).
Where a commit has got to is three refs: `main` (landed), `origin/gated`
(last full-gate green), `origin/prod` (what the cluster runs) — script-moved,
fast-forward only, never committed to (`deploy/README.md`).

Many sibling sessions run at once: scan the injected `scripts/inflight` table
for overlap; once your task is clear, write one line to `.claude/purpose`.
When a coordinator has a peer round open (`scripts/round status`), end your
land by marking it from your own tree — `scripts/round in <sha>`,
`scripts/round none`, or `scripts/round eta <text>` — instead of messaging;
leave deploys to the coordinator (`/round`: deploys the newest main sha with a green CI verdict via `scripts/round gate|deploy` — no local gate, no ship lock). The fleet itself — one tmux
window per active thread plus Reto's `review` window — comes up, and
recovers after a crash, with `/fleet` (`scripts/fleet up`); in a fleet
session a question for Reto is a review-queue item
(`.claude/fleet/review-protocol.md`), not a stop in the pane.
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

- **Forward-only migrations.** Never edit a sealed
  `src/precis/migrations/*.sql` — ship a new one. Baseline regen via
  `scripts/bump` / `precis db dump-schema`, never by hand.
- **Don't mutate body chunks.** `chunks` is append-only for corpus body rows
  (`ord >= 0`); "update" = DELETE + INSERT so the embedding/summary cascade
  re-runs — in-place UPDATE strands `chunk_embeddings`/`chunk_summaries`.
  Only `ord < 0` card variants DELETE/re-INSERT, via a registered synthesis
  pass. Exception: *draft* chunks edit in place by design, via the draft-edit
  store ops only (they log `chunk_events`, which drives the cascade).
- **Session `precis` MCP targets PROD** (write-capable `agent_rw`). Writes
  land in production and need no ask unless destructive, outward-facing or
  over $25 (`docs/conventions/thresholds.md`, Reto 2026-10-01); do
  write-path *testing* on the dev DB (`scripts/dev`) — never this MCP. Ad-hoc SQL: `scripts/prod-psql "SELECT …"` (prefer
  read-only); `scripts/db` is local-only.
- **Agent-supplied-URL fetches → `safe_get`/`safe_stream`**
  (`utils/safe_fetch.py`); raw follow-redirects httpx is an SSRF.
- **No cluster addresses anywhere — the repo is PUBLIC.** Tailnet/LAN IPs and
  vault blobs are gated across the *whole* tree
  (`tests/test_deploy_tree_no_secrets.py`); real node hostnames are gated under
  `deploy/` only (there they mean an un-parameterised role). Prod coordinates
  come from the gitignored overlay via `scripts/lib/pgb-host.sh` — use
  `scripts/prod-psql`, never a literal. Genuine ranges/samples take a
  `secret-gate: allow — <reason>` marker.
- **Embeddings come from the worker, not ingest** — ingest stores chunks
  `embedding IS NULL`; never call `fill_embeddings` from ingest.
- **`uv` for everything; tests via `scripts/test`** (container-mounted;
  `--impacted` narrows) — never bare pytest/pip/mypy. mypy scope is
  `src tests`.
- **Commit messages: one-line subject, no body** + the required
  Co-Authored-By/session footer.

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
- Structure-aware first: `search_code` (MAIN repo path; index is lazy — Grep
  is truth for new code) and `scripts/coderef callers|deps <file.py::Sym>`
  before grepping bare symbols.
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
