---
status: in-progress
title: Gripe-fix loop state lives on the gripe; one skill runs the loop
pillar: platform
prio: high
claimed-by: agent-a48ee681e0f92df42
---

# Gripe-fix loop state lives on the gripe; one skill runs the loop

gr519361. Reto's ruling (typed, 2026-10-26): the GRIPE_FIX coordinator's
working state — ~524 loose files under a scratch directory (briefs,
handoffs, review verdicts, queue/awaiting-deploy/close lists, census and
triage dumps) — belongs in the DB, **on the gripe itself**, not in todos or
files. A reboot wiped `/tmp` and destroyed the handoffs and reviews.
Two deliverables:

1. **An integrated gripe-fixing skill** (`precis-gripe-loop-help`, a
   product skill under `src/precis/data/skills/`) that carries the whole
   loop end to end — triage, batch into a brief, launch a builder, handoff,
   review verdict, land queue, close on deploy — so any coordinator session
   runs a wave from it alone. `.claude/skills/gripe-fleet` becomes a thin
   pointer plus the repo-dev specifics (tmux, Devin/Codex launch, scripts).
2. **A precis-compatible management surface**: the gripe kind's own verbs
   manage the loop. Brief, handoff, verdict and queue state live on the
   gripe; queue views are `get`/`search` filters; state changes are one
   `edit` mode. Conventions: `docs/conventions/mcp-authoring.md` (ack is
   the effect, refusals carry `next=`/`options=`, a fisheye view).
   No side script bypasses the MCP for state.

Two further rulings shape the data model: **store the smallest set of
fields** and derive everything else by query; **record who is working the
gripe as host + session**, reusing the fleet kind's identifiers.

## Motivation / why

The loop today is coordinator memory plus files: `batches.txt` (batch →
gripes → lane), `briefs/<batch>.md`, `handoff-<tree>.md`, `<tree>-review.md`
(`Verdict: LAND` / `FIX`), `queue.txt` (narrative), `awaiting-deploy.txt`
(`sha: ids`), `close-batch-r<N>.txt` (`id sha batch desc`), census TSVs and
scratch aggregators. None of it is queryable, none survives the machine, and
a second coordinator cannot resume it. Every one of those facts is about a
gripe, and the gripe kind already has the carriers: tags (indexed, strict
AND filter), append-only chunks (searchable, timestamped, ordered), `meta`
jsonb (patch-merged by `update_ref`), the STATUS axis, and `ref_events`.

## In scope

### Stored state — every field and why it cannot be derived

| Field | Carrier | Why stored |
|---|---|---|
| batch membership | tag `batch:<slug>` (one at a time; the edit replaces) | the coordinator's grouping decision; nothing else records which gripes share a brief |
| brief text | chunk `gripe_brief` (one per round; the newest is in force) | the text itself; several gripes in a batch carry the same text — Reto: the gripe carries the brief(s) that assigned it |
| handoff text | chunk `gripe_handoff` | the text itself: where the builder stopped, what is next |
| review verdict + findings | chunk `gripe_review`, first line `LAND — …` or `FIX — …` | the text itself; the verdict is the first token so it parses without a second field |
| who | `meta.fix.host`, `meta.fix.session` | fleet rows (`kind='fleet'`) carry host and vendor/tree today but are retired 24 h after the agent dies (`fleet.gc_dead_rows`); the gripe must still say who built it after the tree is gone. `host` is the fleet host (`socket.gethostname().split('.')[0]`, what `fleet-report` sends); `session` is `<vendor>:<window>` — the builder's tmux window (`claude:claude-<tree>`, `devin:devin-<tree>`, `codex:codex-<tree>`, what `fleet-wave launch` records), or `claude:<session id>` with no window — vendor-prefixed so the fleet row `vendor:host:project:tree` resolves from it |
| tree | `meta.fix.tree` | the git identity of the work (`worktree-<tree>`); not derivable from `session` because a Devin is re-pointed at a second tree mid-wave (devin-tables-s2 → worktree-misc-c) |
| landed sha | `meta.fix.landed` | a git fact, not a DB fact; the deploy check (`workers/gripe_triage.collect_fix_evidence`: ancestor of `origin/prod` or in `deployed_history.txt`) runs on it |

Nothing else is stored. Derived by query:

- **queue** — from the newest fix chunk, `session` and `landed`, on a live
  (non-terminal) gripe: no brief → `unbriefed`; newest is a brief and no
  session → `queued`; brief + session → `in-build`; newest is a handoff →
  `in-review`; newest is a review `LAND` → `landing`; review `FIX` →
  `fix-round` (needs a fix brief; posting one makes it `in-build`);
  `landed` set → `awaiting-deploy`; terminal STATUS → none.
- **census / counts / ages** — group by `batch:` tag; ages from chunk
  `created_at` and `refs.updated_at`.
- **release / deployed** — git, from `landed` (`scripts/release`'s
  `scripts/gripe-note` already comments `deployed r<N>`; the triage tick
  closes a deployed fix).
- **builder vendor** — the `session` prefix.
- **round number** — count of `gripe_brief` chunks.

Already carried elsewhere and reused as-is: `fix:<type>`, `size:`,
`cluster:`, PRIO and `STATUS:` (pipeline v2 / triage tick), `thread:`,
`in progress <tree> — covers all asks` comments, `resolved:` close
comments, `supersedes` links, `ref_events` audit rows for tag changes.

**STATUS side effect (announced in the ack).** A brief or batch on an
`open`/`triaged` gripe sets `STATUS:ready_for_fix`; a session, handoff,
review or landed sha on anything below `in_review` sets `STATUS:in_review`.
This is not a second state axis: the triage tick leases `STATUS:open`
gripes, so a gripe entering the loop must leave `open`. A terminal gripe
refuses the edit with the reopen call as `next=`.

### Migration 0254 (forward-only; 0252/0253 are claimed by unshipped siblings)

Seeds `chunk_kinds` rows `gripe_brief`, `gripe_handoff`, `gripe_review`
(the 0005 `gripe_comment` shape) and re-declares
`precis_trust_subject_chunk` (0214) with the three added to its exclusion
list — appended timeline entries, not the gripe's own assertion, so they
never re-stamp its trust subject. Idempotent (`ON CONFLICT DO NOTHING`,
`CREATE OR REPLACE`).

### MCP surface

- `edit(kind='gripe', id=N | [ids], mode='fix', args={...})` — the one
  state door; `id=[...]` applies the same patch to every id in one
  transaction (`_edit_batch`). Keys, each optional, at least one required:
  `batch` (slug; `''` removes), `host`, `session`, `tree` (`''` clears),
  `brief` (text → `gripe_brief`), `handoff` (text → `gripe_handoff`),
  `review` (`'land'`|`'fix'`) with optional `findings` text →
  `gripe_review`, `landed` (7–40 hex; `''` clears), `dry_run`. Validation
  before any write; the ack lists what changed including the derived queue
  before → after and any STATUS flip.
- `get(kind='gripe', id=N)` — the title card gains one `fix:` line
  (`batch · queue · session@host · tree · brief #k · review LAND #k ·
  landed <sha>`) naming `view='fix'`.
- `get(kind='gripe', id=N, view='fix')` — the dossier: the fields plus
  the newest brief, handoff and review in full; older rounds listed by
  ord. Works over `id=[...]`.
- `get(kind='gripe', id='/queue')` — the fisheye: one line per queue with
  counts, then the rows (`gr<id> · queue · batch · session@host · tree ·
  age · title`) grouped by queue. `args={'queue': ..., 'host': ...,
  'batch': ...}` filter; `args={'page': N}` pages.
- `get(kind='gripe', id='/batch')` — the census: one row per batch
  (gripes, queue mix, host/session, tree, landed). `args={'batch': slug}`
  renders that batch's dossier: the brief in force (once — identical text
  across the batch is shown once) and every gripe's title, body and
  comments. This is the text a builder without MCP access receives.
- `search(kind='gripe', tags=['batch:<slug>'], status='*')` is
  membership; `view='list'` rows of a gripe in the loop append
  `· <queue> · <session>@<host>`.
- `tag()` on a terminal STATUS leaves the `batch:` tag (history) — the
  queue views exclude terminal gripes, so nothing needs removing.

### Builder without MCP access — brief delivery

Recommendation: **render a thin file per batch at launch**, from the DB.
Devin and Codex builders have no MCP and tier R has no prod write either,
so read access would mean per-builder DSN provisioning for three agent
TUIs and would still not give W-tier builders their write path. The file
is derived and disposable: `precis gripe-fix brief <slug>` (the CLI mirror
of `get(id='/batch', args={'batch': slug})`, same handler) writes it;
`scripts/fleet-wave launch NAME --batch SLUG` calls it, launches with that
file as the brief and records host/session/tree on every gripe of the
batch through `edit(mode='fix')`. The DB stays the truth; the file is a
view of it.

### One-shot import of the existing state directory

`precis gripe-fix import <dir>` — **dry-run by default**, `--apply`
writes, through `GripeHandler.edit(mode='fix')` only (no SQL). Mapping:

| File | Becomes |
|---|---|
| `batches.txt` (`name\|ids\|files\|lane\|notes`) | `batch:<name>` on each id; brief = `briefs/<name>.md`, else `briefs/<name>-start.md`, else a synthesized brief from the row's files/lane/notes marked `[imported from batches.txt]` |
| `briefs/<slug>.md` not named by `batches.txt` | batch `<slug>` (`-start`, `-r<N>` suffixes stripped); member ids = ids the brief cites, reported as *inferred* |
| `<slug>-fix.md`, `<slug>-r<N>-fix.md` | a fix-round `gripe_brief` on batch `<slug>` |
| `handoff-<name>.md` | `gripe_handoff` on batch `<name>` (or the `worktree-<x>` its head names) |
| `<slug>-review.md`, `<slug>-r<N>-review.md` | `gripe_review`; verdict from the first `Verdict: LAND\|FIX…` line |
| `awaiting-deploy.txt` (`sha: ids`) | `landed=sha` |
| `close-batch-*.txt` (`id sha batch: …`) | `landed=sha` + `batch` |
| `queue.txt`, `closes.tsv`, `decisions*.md`, `census/`, `triage*/`, `r4_scripts/`, `*.sh` | not imported; listed in the report (census and triage are queries now; r4 is a one-off paper repair) |

Idempotent: a chunk whose exact text is already on the gripe is skipped;
closed gripes are reported and left alone. The dry run prints the plan per
gripe and a summary; nothing in this build runs it against prod.

### Scratch scripts worth keeping

GRIPE_FIX's launchers and watchers (its job scratch dir) fold into
`scripts/fleet-wave`: `launch3.sh`/`launch-codex.sh` → `launch --batch`
(Devin/Claude) and `launch --codex` (locked worktree, `codex-<name>`
window, the brief copied to `scratch/codex-brief.md`); `handoff-wait.sh`
→ `handoff-wait NAME...` (`/clear` + resume on a bare `DONE-HANDOFF`
line); `reassign.sh` → `reassign NAME --batch SLUG|--brief FILE`;
`dupcheck.sh` → `cites ID...` (unlanded branch commits naming a gripe);
`peek.sh`/`peekall.sh` → the existing `peek`; `idle-watch.sh` →
`scripts/fleet panes status` (state column) and `stuck-watch`;
`ctx-guard.sh` → the `ctx-guard` subcommand in flight on
worktree-misc-d2 (gr515267), not duplicated here; `recover_gf.py`
(rebuild the scratch dir from transcripts) — obsolete, the state is in
the DB. From the state dir: `census/agg.py`, `agg2.py`,
`split_batches.py` — replaced by `/batch` and `/queue`;
`mkbriefs.py`/`census/mkbrief.py` — replaced by `precis gripe-fix brief`;
`r4_scripts/*` — the gr513878 paper repair, one-off, done;
`triage-dry/*` — superseded by the shipped tick; `find.py`/`net.py`/
`cov.py`/`mt.py` — `scripts/branch-audit` covers them; `scan.sh`/`do.sh`
— `scripts/fleet panes status` and `fleet-wave close` cover them.

## Explicitly NOT in scope

- No new STATUS value and no change to 0176's vocabulary; `verify` and the
  triage tick's close logic stay as pipeline v2 defines them. Teaching the
  tick to read `meta.fix.landed` as fix evidence is a follow-up.
- No new ref kind (`batch`/`brief`); the brief is on each gripe by ruling.
- No automation of launching, reviewing or landing — the skill describes
  the loop, the surface records it, the coordinator still drives it.
- No change to builder authority tiers, footers or approvals.
- No web UI rendering of the fix chunks (the `/gripes` route lists only
  `gripe_comment` chunks and keeps doing so).
- No prod import run; no prod writes from this build.

## Acceptance criteria

- `edit(kind='gripe', id=N, mode='fix', args={'batch': 'x', 'brief': '…'})`
  on an open gripe tags `batch:x`, appends a `gripe_brief` chunk, flips
  `STATUS:open → STATUS:ready_for_fix`, and the ack names all three;
  `id=[...]` applies it to every id or none.
- The same door records session/host/tree, handoff, review verdict and
  landed sha; each step's derived queue is `in-build`, `in-review`,
  `landing`/`fix-round`, `awaiting-deploy` respectively, and closing the
  gripe removes it from every queue view.
- A terminal gripe, an unknown key, a bad verdict, a non-hex sha, an empty
  patch each refuse with `next=`/`options=` and write nothing.
- `get(kind='gripe', id='/queue', args={'host': 'h1'})` lists only gripes
  with `meta.fix.host == 'h1'`; `args={'queue': 'in-build'}` filters by the
  derived queue; the plain `/queue` shows counts per queue.
- `get(kind='gripe', id='/batch', args={'batch': 'x'})` renders the brief
  once and every member's body; plain `/batch` lists batches with counts.
- The title card shows the `fix:` line; `view='fix'` shows the texts.
- `precis gripe-fix import <dir>` on a fixture directory shaped like the
  state directory prints a plan and writes nothing; `--apply` on the dev
  DB produces the tags, chunks and meta the plan named, and a second
  `--apply` is a no-op.
- `precis-gripe-loop-help` exists, passes the skill gates
  (`tests/test_skill_size.py`, `test_skill_prose.py`), and
  `precis-gripe-help` points to it; `.claude/skills/gripe-fleet/SKILL.md`
  is a pointer plus repo-dev mechanics.
- Migration 0254 applies idempotently; `scripts/migration-check` clean.

## Target + blast radius

`src/precis/handlers/gripe.py` (edit, get, `_render_one`, list views),
new `src/precis/handlers/_gripe_fix.py` (state model + derivation),
`src/precis/migrations/0254_gripe_fix_chunk_kinds.sql`,
`src/precis/cli/gripe_fix.py` (`precis gripe-fix import|brief|assign`),
`scripts/fleet-wave` (`launch --batch`), skills `precis-gripe-loop-help`
(new), `precis-gripe-help`, `.claude/skills/gripe-fleet/SKILL.md`, tests
`tests/test_gripe_fix_state.py`, `tests/test_gripe_fix_import.py`.
Post-deploy check: `get(kind='gripe', id='/queue')` renders empty without
error; an `edit(mode='fix')` on a dev gripe round-trips.

## Build notes (branch worktree-agent-a48ee681e0f92df42)

Built: migration 0254; `handlers/_gripe_fix.py` + the `gripe.py` doors
(`edit(mode='fix')`, `view='fix'`, `/queue`, `/batch`, the card line);
`cli/gripe_fix.py` (`precis gripe-fix import|brief|assign`, dry run
default); `scripts/fleet-wave launch --batch`; skills
`precis-gripe-loop-help` (new), `precis-gripe-help`, `precis-overview`,
`precis-toolpath-help`, `.claude/skills/gripe-fleet`. Not built: the
triage tick reading `meta.fix.landed` (follow-up); the prod import run.
Importer decisions: a brief with no `batches.txt` row takes the gripe ids
its text cites (`gr<N>` or a bare six-digit id) as members, reported as
inferred; a close list's third token is a batch only when the plan
already knows that batch; `-start` briefs are the batch item and
`-r<N>`/`-rulings`/`-add-` siblings later rounds.

## Open questions / decisions log

- **Decided (conservative):** no STATUS value is added; the loop reuses
  `ready_for_fix`/`in_review` and the finer queue is derived.
- **Decided:** `session` is vendor-prefixed free text (`claude:<id>`,
  `devin:<window>`, `codex:<lane>`); the handler does not validate the id
  part. Reto may want a fixed grammar.
- **Decided:** a `batch:` tag is replaced, not accumulated; the previous
  batch is recoverable from `view='log'` (tag audit) and the briefs.
- **Open (Reto):** should the triage tick treat `meta.fix.landed` as fix
  evidence so an awaiting-deploy gripe closes itself on the next deployed
  tick, or does the coordinator keep closing by hand with `resolved:`?
  Recommendation: the tick, as a follow-up slice.
- **Open (Reto):** import `--apply` on prod — who runs it and when; the
  dry-run report is in this item's hand-back.
- **Open (Reto):** builders' brief file at launch (recommended) vs read
  access for builders.
