# ADR 0030 — `job`, `finding`, `cron` stay separate from `todo`

**Status:** Accepted (2026-06-15)
**Context:** Planner-coroutine cascade work (slices T1–T3) made the
`kind='todo'` surface look like a generic workspace for "things with
a STATUS and a worker." Three adjacent kinds — `job`, `finding`,
`cron` — kept getting flagged as potentially foldable. This ADR
records the audit and the decision.

## Context

When `kind='todo'` gained `meta.workspace`, `meta.executor`,
parent_id-based ancestry, and the dispatch-worker that mints a
child `kind='job'` per LLM-tick, the boundaries between todo and
its neighbours began to feel arbitrary:

- A `kind='job'` ref is always parented to a `kind='todo'`; its
  STATUS axis is just a different lifecycle on the same shape.
- A `kind='finding'` is "I need to find paper X" — semantically a
  todo with `executor='chase'`.
- A `kind='cron'` ref carries `meta.next_fire_at` + a schedule,
  resembling `level:recurring` on todos.

The natural question: collapse them into kind='todo' and let the
existing meta + dispatch infrastructure carry the load.

A parallel multi-agent audit (4 investigators + 1 synthesis pass)
read the full surface of each kind end-to-end. The unanimous
finding was **don't collapse**, for kind-specific reasons that
generalise to: *separate kinds earn their keep when they carry
mechanisms the destination kind would have to reimplement*.

## Decision

**`kind='job'` stays.**

- The dispatch worker (`src/precis/workers/dispatch.py`) claims
  todos via `FOR UPDATE SKIP LOCKED`, mints a child job in the
  same transaction, and lets the executor claim that job row
  *independently* via its own `FOR UPDATE SKIP LOCKED`. Folding
  ticks into chunks on the todo means either holding the todo's
  row lock for the full multi-minute tick (lock contention with
  every other write to the same todo) or maintaining a parallel
  `tick_status` axis on the todo that has to coexist with the
  user-facing STATUS axis. Two state machines on one ref is
  worse than two kinds.
- The `child_job_succeeded` auto_check evaluator and the
  `child-failed:<job_id>` failure-bubble (`_job_bubble.py`) both
  rely on the parent/child kind boundary as a simple tag-based
  queryable. Folding moves these into chunk_kind filtering —
  slower and less direct.
- The `meta.executor` + `meta.job_type` pair is genuinely
  pluggable; today `plan_tick` and `fix_gripe` register
  independently. A separate kind lets future executors register
  without touching the todo handler's dispatch logic.

**`kind='finding'` stays.**

- The deterministic `pub_id` (hash over `body + scope +
  initial_cite`, via `src/precis/identity.py`) is a *content-
  dedup mechanism*: two agents claiming the same paper under the
  same setup collapse to one chase. Todos have no content-based
  dedup — they collide only on explicit dedup tags.
- The STATUS axis is specialised:
  `tracing|established|dead_chain|multi_candidate|cycle`. The
  `multi_candidate` state (with candidate links tagged
  `meta.candidate=true`) is a first-class concept; todos have
  no equivalent branching resolver.
- `meta.chain` is a mutable provenance journal appended by the
  chase worker on each hop. Todos don't carry mutation-tracked
  journey metadata of this shape.
- `precis resolve` substitutes pub_ids into draft prose at
  finalisation — a doc-integration loop with no todo analog.

**`kind='cron'` stays.**

- Cron is a *push* notification system: a launchd timer (`precis
  cron tick`) wakes up, emits `pg_notify('precis.cron', ...)`,
  and asa_bot LISTENs and delivers the payload as a synthetic
  prompt to a Discord conversation. `meta.target` is a delivery
  address for an external system.
- `level:recurring` todos are a *pull-into-queue* system: a
  worker spawns subtasks into the doable queue where they can
  be worked, refined, delegated.
- Different consumers, different lifecycles, different metadata
  shapes. Slice 4 introduced recurring; it did not deprecate
  cron, and the `precis-recurring-help.md` skill comment calling
  cron "legacy" was misleading (fixed in this ADR's accompanying
  commit).

## Consequences

- The `docs/design/todo-tree-plan.md` foldability table that
  read "Already folded (Slice 5)" / "Already folded (Slice 4)"
  is updated to "No — see ADR 0030" with the kind-specific
  reasoning.
- The `precis-recurring-help.md` reference to cron-as-legacy is
  rewritten to "different use case".
- Future "could we collapse X into todo?" audits should consult
  the *mechanisms* test: does X carry locking, dedup, scheduling,
  provenance journals, or delivery addresses that would have to
  be reimplemented on todo? If yes, keep separate.
- Two minor renames *did* land alongside this ADR (commit
  `924c67d`): `fc → flashcard`, `think → perplexity-reasoning`,
  `research → perplexity-research`. Those were honest-naming
  cleanups, not collapses — the kinds themselves remain
  distinct.

## Alternatives considered

- **Collapse job → todo, ticks as `chunk_kind='tick_run'`
  chunks.** Rejected as above: lock contention or a second
  STATUS axis. Slice 5 (jobs-as-children-of-todos) is the right
  depth — no further fold.
- **Collapse finding → todo with `executor='chase'`.** Rejected:
  pub_id content-dedup + multi_candidate branching + provenance
  chain + resolve-into-prose loop are all finding-specific
  mechanisms that don't generalise to todos.
- **Collapse cron into `level:recurring` + `target=` field on
  todo.** Rejected: cron and recurring serve different
  consumers (external Discord delivery vs in-queue work). The
  delivery-address abstraction would leak into every code path
  that walks recurring todos.

## References

- Audit transcript: workflow `wf_aee72c5a-6ea` (4 investigators
  + 1 synthesis), 2026-06-15
- `src/precis/workers/dispatch.py` — todo-claim + job-mint
- `src/precis/workers/executors/claude_inproc.py` — job-claim +
  executor dispatch
- `src/precis/handlers/_todo_guards.py:413-549` —
  `check_status_done_artifact` (counts succeeded child job as
  evidence)
- `src/precis/identity.py:322-401` — `make_finding_paper_id` /
  `make_pub_id` (finding content-dedup)
- `src/precis/cli/cron.py` — cron push-notification tick
- `src/precis/workers/schedule/worker.py` — recurring
  pull-into-queue spawner
