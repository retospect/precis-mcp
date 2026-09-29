---
status: draft
title: Curation gate — a read-only reviewer over one run's write set, pass/fail-with-notes, bounded retry, park for Reto; recurring failures become gripes
prio: normal
model: opus
blocked-by: eval-run-spine
---

# Curation gate — review a run's writes, not the corpus

## Motivation / why

The 2026-09-29 research-mesh handoff specifies a curation loop: a writer
session commits, a read-only reviewer sees **only that session's diff**,
returns pass or fail-with-notes, the writer retries at most twice on the
notes, and a still-failing branch is parked for a human. The reviewer's
cost is constant per session because it never re-reads the corpus; it
asks whether a label was defensible at the time, not whether it is ideal;
and when the same failure recurs across sessions it files a bug against
the skill that produced it. Everything else in that handoff already had a
home (`term-taxonomy.md` §Research-mesh reconciliation); this loop did not.

What exists, stated so nobody rebuilds it:

- **The write set already exists.** Every agentic run is an `agentlog`
  ref, and every chunk it writes or moves gets a `touched` link attached
  lazily from `PRECIS_CURRENT_AGENTLOG` (`agentlog.py::touch_from_env`).
  That is chunk-granular, not a diff, but it is exactly "what this session
  changed". The handoff's `session_id` is `agentlog_id`.
- **A per-chunk anchored review tick exists.** The review-mode `plan_tick`
  (`executors/claude_inproc.py::_maybe_record_review_pass`) resolves a
  persona and a `content_sha` at tick start, stamps a `chunk_review`
  approval only when the reviewer filed nothing and the watermark is
  unchanged (anti-self-approval), and never aborts the job. Its anchor is
  one chunk (`meta.anchor = dc<id>`).
- **A read-only reviewer envelope half-exists.** `write:none`
  (`envelope.py::disallowed_tools`) drops the `put` verb and maps to DB
  role `agent_ro`; migration 0079's `file_gripe_readonly` lets that role
  still file a gripe. The ops flip to `agent_ro` on the review container
  is undone — `review-container-readonly-role.md`.
- **Park exists; reviewer-driven retry does not.** `max_job_attempts`
  (default 3) is infra retry for crashed jobs; `bubble_job_failure` tags
  the parent `child-failed:` and a terminal `child-failed-final` a human
  clears; `waiting-for:reto` is the human park. Nothing carries a
  reviewer's *notes* into a retry.
- **Human-vs-machine provenance does not exist on bodies.** `refs.set_by`
  and `chunks.set_by` are left NULL by `insert_ref` / `insert_blocks`
  (schema comment); only `ref_tags.set_by` is stamped, and its usual value
  is the literal `agent` or `system`. The owner/worker gradient is read
  from `$PRECIS_SOURCE` at guard time (`handlers/_todo_guards.py`), never
  persisted. So "protect human-authored content from a rebuild" (handoff
  Q4) has nothing to key on today.
- **`skill_bug` is a `gripe`.** No new kind.

## Design

**Anchor the review tick on an agentlog, not a chunk.** A review-mode todo
with `meta.anchor = al<id>` reviews the set of chunks that agentlog
`touched`, rendered as a working set (`utils/working_set_render.py`) of
before/after cards — the "diff" the handoff wants, at chunk granularity.
The reviewer runs under `write:none`; it may file `finding` children of
the review todo (the notes) and gripes, nothing else.

**Verdict is the run's `verdict`.** `eval-run-spine.md` item 6 splits a
run's mechanical `outcome` from a nullable `verdict`; this gate is the
first writer of that column: `pass` / `fail` / `parked`. Notes are the
`finding` children, as they are for chunk reviews. No new review table.

**Retry is a reviewer decision, counted separately from job attempts.** A
`fail` verdict with notes mints one retry todo for the writer with the
notes pinned, and bumps `meta.curation_attempt` on the agentlog's parent
todo (0 = original, 1..2 = retries). At 2 the next `fail` becomes `parked`:
`waiting-for:reto` on the parent, the review todo closed, no further mint.
`max_job_attempts` is untouched — a crash and a rejected write are
different things and are counted apart.

**Recurring failure ⇒ gripe, deduplicated by signature.** A failure
signature is `(skill named in the notes, failure class)`; the reviewer
searches open gripes for it before filing (the same dedup the gripe filer
already does) and comments on the existing one instead of minting.

**Provenance on write, so protection has a key.** `insert_ref` /
`insert_blocks` stamp `set_by` from the actor the dispatcher already knows
(`$PRECIS_SOURCE` → an `actors` slug; agentic runs stamp the run's
`source`). A ref is **protected** when its `set_by` is a human actor, or
it carries any `chunk_review` row by a human. Protection is surfaced as a
`protected` field in the universal row (`knowledge-mesh.md` §5) so the
curator sees it before acting, and enforced in the handler: a machine
overwrite of a protected body is refused, never silently applied. Rebuild
passes (`graph-gardener.md`) skip protected refs.

**Diff-aware review state (absorbed from `draft-review-state-diff-aware.md`,
2026-09-29).** The same primitive — compare a stored assertion's
`content_sha` / quoted span to the chunk's current text — drives two
existing pains: a forked draft carries its source's `chunk_review` ledger
forward with each entry marked stale-if-`content_sha`-changed, so only
divergent chunks show as unreviewed; and an anchored concern `finding`
whose quoted span no longer occurs in the chunk auto-reports `gone`, so an
agent judges only the live ones. That check is what makes a reviewer's
notes from attempt N still meaningful at attempt N+1.

## In scope

1. `agentlog` as a review anchor: `_review_meta` / `_anchor_chunk_snapshot`
   accept `al<id>`; the snapshot is the sha set of touched chunks; the
   render is the before/after working set.
2. Verdict write: `pass`/`fail`/`parked` into the run's `verdict` column
   (from `eval-run-spine`), notes as `finding` children.
3. Reviewer-driven retry: retry todo with pinned notes,
   `meta.curation_attempt`, cap 2, park at 3 via `waiting-for:reto`.
4. Signature-deduplicated gripe on recurrence.
5. `set_by` stamped on `refs` and `chunks` at insert; `protected`
   derivation; `protected` in the universal row; handler refusal on
   machine overwrite; gardener skip.
6. Diff-aware review state: ledger carry-forward on fork with sha
   staleness; `live / stale / gone` verdict per anchored finding, exposed
   on `view='review'`.
7. Runtime docs: `precis-review-help` (gate section), `precis-gripe-help`
   (signature dedup), `precis-overview` (the `protected` field).

## Explicitly NOT in scope

- The scheduled whole-graph passes — merge/split/relink/prune/tag-sweep
  are `graph-gardener.md`. This gate is per-run, triggered by a run
  finishing; the gardener is per-schedule, triggered by time.
- The ops flip of the review container to `agent_ro` and the
  gripe-under-`write:none` tool-layer call —
  `review-container-readonly-role.md`, a prerequisite, not absorbed.
- Vocabulary quality. The reviewer asks "defensible at the time"; whether
  a taxon should exist is `taxonomy-bootstrap.md` and the gardener.
- Run versioning, the `verdict` column itself, payload capture —
  `eval-run-spine.md` (blocked-by).
- A textual diff of chunk bodies. The write set is chunk-granular by
  design; a line diff is a render choice inside the working set, later.

## Acceptance criteria

1. A review todo anchored on `al<id>` renders exactly the chunks that
   agentlog `touched`, each with its pre-run and current card; a chunk
   touched by a different run is absent.
2. A reviewer that files nothing ⇒ `verdict='pass'` on the run and no
   retry todo; a reviewer that files one `finding` ⇒ `verdict='fail'`,
   one retry todo carrying that finding, `curation_attempt=1`.
3. Three consecutive `fail`s ⇒ `verdict='parked'`, `waiting-for:reto` on
   the parent, no fourth retry todo; `max_job_attempts` never changes.
4. Two runs failing with the same `(skill, class)` signature ⇒ one gripe
   with two comments, not two gripes.
5. A ref inserted from a `web:*` / `user` source has a human `set_by`; a
   machine `edit` of its body is refused naming `protected`; the same edit
   from a human source succeeds. A ref inserted by an agentic run has the
   run's `source` as `set_by`.
6. A forked draft with one edited chunk shows one unreviewed chunk, not
   all; a finding whose quoted span was deleted reports `gone`.
7. Under `write:none` the reviewer can file a `finding` child and a
   gripe and nothing else (the seven-verbs invariant in `server.py` holds).

## Target + blast radius

Touched: `workers/executors/claude_inproc.py` (`_review_meta`,
`_anchor_chunk_snapshot`, `_maybe_record_review_pass`),
`quest/review_guard.py`, `handlers/_job_bubble.py` (park path),
`store/_refs_ops.py::insert_ref` + `_chunks_ops.py::insert_blocks`
(`set_by`), the `protected` derivation in the row renderer, `handlers/draft.py`
(`copy_of` ledger carry-forward, `view='review'`), `handlers/gripe.py`
(signature dedup). One migration if `curation_attempt` needs a column
rather than meta (default: meta). Skills as listed.

Blast radius is the review-mode tick and every insert path (the `set_by`
stamp). The stamp must be a pure addition: an insert with no resolvable
actor stamps `system`, never fails.

## Open questions / decisions log

- **[decided 2026-09-29, Reto]** Own item, not a section of
  `graph-gardener.md` — different trigger (run-finished vs schedule) and
  different scope (one run's writes vs the whole graph).
- **[decided 2026-09-29]** Verdict lives on the run (`eval-run-spine`'s
  column), not in a new `review` table. Notes are `finding` children, as
  chunk reviews already do.
- **[decided 2026-09-29]** `skill_bug` = `gripe`; recurrence is a comment
  on the existing gripe, found by signature.
- **[open]** Whether the reviewer sees the writer's prompt/skills (to
  judge "defensible at the time") or only the write set. Leaning: the
  agentlog's `meta.prompt` is in the render, skills are not.
- **[open]** The failure-class vocabulary for signatures — start from
  `eval-run-spine`'s `cause:*` tags rather than a new list.
