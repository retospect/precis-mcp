# Phase 0 PR 3 — coordinator executor + wake_runner

## Motivation

The `precis-dft` `dft_campaign` job_type orchestrates a catalysis
exploration over hours to weeks. It walks through phases
(propose → screen → confirm → analyze → decide), spawns dozens of
child jobs per phase, waits for them, decides whether to continue.
The existing `claude_inproc` executor cannot host this shape:

- It holds a 30-minute lease per claim. A coordinator that has to
  wait days for child DFT jobs to finish would either pin a
  worker slot for days (blocking other work) or lose its lease
  and get double-claimed.
- It checks cancel once before running, then never again. A
  multi-day coordinator should observe cancel between phases.
- It has no "pause for human approval" affordance. The agent
  steered-with-human-approval steering mode in the precis-dft
  spec needs this.

A new executor — `coordinator` — pairs with a new ref pass —
`wake_runner` — to support yield/resume work without changing the
existing `claude_inproc` invariants.

## Today's shape

- `src/precis/workers/executors/claude_inproc.py:61-108` —
  `_claim_jobs` SQL: filters by `meta->>'executor' = 'claude_inproc'`,
  `STATUS:queued`, no terminal tag, lease expired.
- `claude_inproc.py:204-254` — `run_claude_inproc_pass`: writes
  30-min lease, sets `STATUS:running`, calls `_run_one`.
- `claude_inproc.py:282-293` — single cancel poll before
  dispatch.
- `src/precis/workers/executors/__init__.py:24-38` —
  `EXECUTOR_PROVIDES` capability registry; the dispatcher reads
  this at submit time.
- `src/precis/handlers/_todo_views.py:65-91` —
  `_DOABLE_EXCLUSION_TAGS` and `_doable_exclusion_clause()`. The
  existing `ask-user:*` / `asking-reto:*` / `halt:*` / `child-failed:*`
  open-namespace tags suppress dispatch when present. PR 3
  extends this pattern.
- `src/precis/workers/runner.py:111-223` — `RefPass` typealias
  and `run_loop` round-robin driver. Adding a new background
  worker = add a `RefPass` callable and wire it in
  `cli/worker.py:332-548` under the right profile.
- `src/precis/workers/dispatch.py:270-302` — the dispatch
  worker's claim SQL uses `_doable_exclusion_clause()` inside the
  `FOR UPDATE` to prevent races between candidate enumeration and
  the lock; PR 3 follows the same pattern.

## Design

### 3.1 `coordinator` executor

#### Lifecycle

A coordinator job:

1. Gets claimed (`STATUS:queued` → `STATUS:running`) like any
   other.
2. Its dispatcher (from PR 1) calls `spec.run(ctx, state)` where
   `state` is the previous yield's checkpoint (or `None` on
   first run).
3. `spec.run` returns one of:
   - `Done(outcome)` — terminal. Executor writes
     `job_summary` chunk, transitions `STATUS:succeeded` or
     `STATUS:failed` per outcome.
   - `Yield(state, wake_when)` — non-terminal. Executor writes
     `meta.coordinator_state = state`, writes
     `meta.wake_when = wake_when`, sets a `STATUS:waiting_<reason>`
     value (closed-namespace, see below), releases the slot.
4. On the next `run_coordinator_pass`, claimed-again-only-when
   `STATUS:queued` (re-tagged by the wake_runner). The
   coordinator dispatcher loads `coordinator_state` from
   `meta`, hands it to `spec.run`.

#### Return type contract

A new module `src/precis/workers/executors/_yield.py`:

```python
@dataclass(frozen=True)
class Done:
    summary: str
    success: bool = True
    summary_meta: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Yield:
    state: dict[str, Any]
    wake_when: WakeWhen

@dataclass(frozen=True)
class WakeWhen:
    kind: str      # 'children_done' | 'at_time' | 'tag_cleared' | 'tag_added'
    # children_done: {'child_job_ids': [int, ...]}
    # at_time:       {'ts': int}  (unix seconds)
    # tag_cleared:   {'tag': str} (matches exactly OR glob ending in ':*')
    # tag_added:     {'tag': str}
    payload: dict[str, Any]
```

JSON-serialised into `meta.wake_when` directly — no envelope.

#### New STATUS values

Add four closed-namespace `STATUS:` values:

- `STATUS:waiting_children` — wake when all child_job_ids
  terminal.
- `STATUS:waiting_time` — wake at ts.
- `STATUS:waiting_ask_user` — wake when the matching open-
  namespace `ask-user:*` tag is cleared.
- `STATUS:waiting_manual_kick` — wake when the matching
  open-namespace tag is added.

These join the existing `STATUS:` vocabulary
(`queued / running / succeeded / failed / cancelled /
cancel_requested`). None is terminal. None is `STATUS:queued`
(so the claim SQL doesn't pick them up).

#### Claim SQL

`src/precis/workers/executors/coordinator.py:_claim_jobs` mirrors
`claude_inproc._claim_jobs` with three changes:

- `meta->>'executor' = 'coordinator'` (not `'claude_inproc'`).
- Shorter lease: 5 minutes (each active slice is meant to be
  brief — heavy work happens in child jobs).
- Already-skips waiting STATUSes naturally because the existing
  claim only matches `STATUS:queued`.

Cancel handling: the dispatcher polls `_is_cancel_requested`
once at the top of each slice, same as today. A multi-slice
job effectively gets cancel-polled at every yield, satisfying
the "observe cancel between phases" requirement.

#### Capability registry

`workers/executors/__init__.py:24-38`:

```python
EXECUTOR_PROVIDES["coordinator"] = frozenset()
```

The coordinator executor advertises no host-side capabilities.
A job_type compatible with `coordinator` declares
`COMPATIBLE_EXECUTORS = frozenset({"coordinator"})` and
`REQUIRES = frozenset()` (it dispatches; it doesn't compute).

### 3.2 `wake_runner` ref pass

A `RefPass` that scans `STATUS:waiting_*` jobs whose `wake_when`
is satisfied, re-tags them `STATUS:queued`. The next coordinator
pass picks them up.

#### Wake-condition queries

`run_wake_pass(store, *, limit) -> BatchResult` runs four
specialised SELECTs, one per `wake_when.kind`, each bounded by
`LIMIT`:

```sql
-- children_done
SELECT r.ref_id
  FROM refs r
 WHERE r.kind = 'job'
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'STATUS'
                  AND t.value = 'waiting_children')
   AND r.meta->'wake_when'->>'kind' = 'children_done'
   AND NOT EXISTS (
         -- a child still non-terminal
         SELECT 1 FROM refs c
          WHERE c.ref_id::text = ANY (
                SELECT jsonb_array_elements_text(
                    r.meta->'wake_when'->'payload'->'child_job_ids'))
            AND c.kind = 'job'
            AND COALESCE(
                  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                  'open'
                ) NOT IN ('succeeded', 'failed', 'cancelled')
       )
 LIMIT %s
   FOR UPDATE OF r SKIP LOCKED;
```

Analogous SQL for `at_time`, `tag_cleared` (uses
`_doable_exclusion_clause`-style LIKE for glob tags ending
`:*`), and `tag_added`.

For each row returned, the wake_runner:

1. Re-tags `STATUS:queued` (replace_prefix).
2. Optionally clears `meta.wake_when` (for hygiene; not
   required for correctness — `coordinator.dispatcher` reads
   it from `state`, not `wake_when`).
3. Writes a `job_event` chunk: `"wake_runner: wake_when
   satisfied ({kind})"`.

#### Wiring

`src/precis/cli/worker.py:428-446` already wires
`run_claude_inproc_pass` for the system profile. PR 3 adds:

```python
# After the claude_inproc wiring:
from precis.workers.executors.coordinator import (
    run_coordinator_pass,
)
from precis.workers.wake_runner import run_wake_pass

ref_passes.append(_named("coordinator", run_coordinator_pass))
ref_passes.append(_named("wake_runner", run_wake_pass))
```

Both run on the system profile (every cluster node, per
CLAUDE.md's worker-consolidation note).

### 3.3 Integration with existing exclusion tags

Coordinator-style human pauses use the existing
`ask-user:<question>` open-namespace tag PLUS the new
`STATUS:waiting_ask_user` closed-namespace value:

```python
ctx.add_tag(f"ask-user:propose:approve_batch_n=3")
ctx.set_status("waiting_ask_user")
return Yield(state={...}, wake_when=WakeWhen(
    kind="tag_cleared",
    payload={"tag": "ask-user:propose:*"},
))
```

The `ask-user:propose:*` tag:
- Suppresses any sibling dispatch attempt via
  `_doable_exclusion_clause` (the existing mechanism, no
  change).
- Surfaces to the human in the asa-bot attention view.

The `STATUS:waiting_ask_user`:
- Keeps the closed STATUS namespace honest (one value per ref).
- Makes the wake_runner's claim SQL cheap (`tag.value =
  'waiting_ask_user'` is exact match, not LIKE).

The two together give the dispatcher's exclusion + the wake
runner's selectivity without conflict.

### 3.4 Failure modes

- **Coordinator's `spec.run` raises mid-phase**: caught by
  `run_coordinator_pass` exactly like `run_claude_inproc_pass`
  catches dispatcher exceptions today; `STATUS:failed`,
  `job_event` chunk, failure-bubble via `_job_bubble`.
- **A `children_done` wait references a child that was deleted**:
  the wake check NOT EXISTS clause treats a missing child as
  terminal (the join misses); coordinator wakes, sees a child
  is gone, can handle in its phase logic. Document as expected.
- **wake_when.kind unknown**: wake_runner logs a warning and
  leaves the job in `STATUS:waiting_*`; an operator can hand-
  retag `STATUS:queued` to force resumption.
- **Wake runner crash mid-row**: `FOR UPDATE SKIP LOCKED`
  releases the row on tx end. Next pass re-evaluates.
- **Cancel during wait**: `STATUS:cancel_requested` is the
  existing primitive. The coordinator pass picks the row up
  next slice and the dispatcher's cancel-poll observes it. (A
  job-in-waiting-state can't be claimed by the coordinator pass
  because its STATUS isn't `queued`; we add a one-line
  `STATUS:waiting_*` → `STATUS:queued` shortcut in the
  wake_runner when it sees `STATUS:cancel_requested` overlay,
  so cancel propagates within one tick.)

Actually — `STATUS:` is closed-namespace, one value at a time.
A `STATUS:cancel_requested` tag on a `STATUS:waiting_children`
row replaces the waiting tag. The wake_runner needs to also
scan for `STATUS:cancel_requested` rows with `wake_when` set
and re-queue them so the coordinator sees the cancel. Add this
as a fifth wake query:

```sql
-- cancel_requested overrides any wait
SELECT r.ref_id FROM refs r
 WHERE r.kind = 'job'
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'STATUS'
                  AND t.value = 'cancel_requested')
   AND r.meta ? 'wake_when'
 LIMIT %s
   FOR UPDATE OF r SKIP LOCKED;
```

For these, re-tag `STATUS:queued` and let the coordinator's
cancel-poll handle the actual transition to `STATUS:cancelled`.

### 3.5 Dispatcher signature for coordinator job_types

A coordinator-shaped `spec.dispatch` (from PR 1) implements:

```python
def dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    state = ctx.meta.get("coordinator_state")
    try:
        result = spec.run(ctx=ctx, state=state)
    except Exception as exc:
        ctx.record_failure(f"coordinator raised: {exc!r}")
        return

    if isinstance(result, Done):
        ctx.append_chunk("job_summary", result.summary)
        ctx.set_status("succeeded" if result.success else "failed")
        ctx.set_meta(**result.summary_meta)
        return

    if isinstance(result, Yield):
        ctx.set_meta(
            coordinator_state=result.state,
            wake_when=asdict(result.wake_when),
        )
        ctx.set_status(_STATUS_FOR_WAKE_KIND[result.wake_when.kind])
        return

    ctx.record_failure(f"coordinator returned unknown type: {type(result)!r}")
```

`_STATUS_FOR_WAKE_KIND = {'children_done': 'waiting_children',
'at_time': 'waiting_time', 'tag_cleared': 'waiting_ask_user',
'tag_added': 'waiting_manual_kick'}`. (Note: `tag_cleared`
defaulting to `waiting_ask_user` works because the only v1
tag_cleared use case is human-resume; if other use cases arise,
generalize the mapping.)

The `precis-dft` package ships its own `dft_campaign.dispatch`
that follows this pattern.

## What does not change

- `claude_inproc` keeps its 30-min lease, cancel-once-before-run,
  STATUS lifecycle, claim SQL.
- The 7-verb surface.
- `JobHandler.put` validation order.
- `_doable_exclusion_clause` content.
- Existing `ask-user:` / `asking-reto:` / `halt:` / `child-failed:`
  semantics.
- Worker profiles (system / agent / dream / cron-tick).

## Risk and rollback

- **Heaviest of the four Phase 0 PRs**: new executor, new ref
  pass, new STATUS values, new wake-when contract.
- **Confined to new files**: `workers/executors/coordinator.py`,
  `workers/wake_runner.py`, `workers/executors/_yield.py` are
  new; only `workers/executors/__init__.py` (capability registry)
  and `cli/worker.py` (wiring) get touched in existing code.
- **Schema-free**: no migration. The new STATUS values live in
  the unified `tags` table on first use; existing rows are
  unaffected.
- Rollback = revert; the new ref passes drop out of the system
  worker's loop, the executor is unregistered, the four
  STATUS values become semantically inert (any existing
  in-flight `STATUS:waiting_*` rows would have to be hand-
  resumed, but at rollback time there are none).

## Tests

- `tests/workers/executors/test_coordinator_yield_resume.py`
  (new): a fake coordinator job_type that yields once, returns
  Done on second slice; assert state survives the round trip
  and the wake_runner re-queues at the right moment.
- `tests/workers/test_wake_runner_children_done.py` (new):
  spawn a parent coordinator + two child jobs; mark one done,
  one running; assert wake_runner does not wake. Mark both
  done; assert wake_runner re-queues.
- `tests/workers/test_wake_runner_at_time.py` (new): yield with
  `ts = now() - 1`; assert immediate wake.
- `tests/workers/test_wake_runner_tag_cleared.py` (new): yield
  with `ask-user:test:foo`; assert no wake; clear the tag;
  assert wake.
- `tests/workers/test_wake_runner_cancel.py` (new): yield;
  set `STATUS:cancel_requested`; assert wake-runner re-queues;
  next coordinator pass transitions to `STATUS:cancelled`.

## Files touched

| File | Change |
|---|---|
| `src/precis/workers/executors/_yield.py` | New: `Done` / `Yield` / `WakeWhen` dataclasses. |
| `src/precis/workers/executors/coordinator.py` | New: `_claim_jobs`, `run_coordinator_pass`. Mirrors `claude_inproc.py` structure. |
| `src/precis/workers/executors/__init__.py` | Add `EXECUTOR_PROVIDES["coordinator"]`. |
| `src/precis/workers/wake_runner.py` | New: `run_wake_pass` (5 SELECTs, one per wake kind + cancel override). |
| `src/precis/cli/worker.py` | Wire `run_coordinator_pass` + `run_wake_pass` on the system profile. |
| `tests/workers/executors/test_coordinator_yield_resume.py` | New. |
| `tests/workers/test_wake_runner_*.py` | New (4 files). |
| `CHANGELOG.md` | Entry under `## Unreleased`. |
| `pyproject.toml` | Version bump. |

## Out of scope (separate PRs)

- Plugin registries for job_types / migrations (PR 1).
- Idempotency hardening, MCP frame chunking, `meta.no_index`
  filter (PR 2).
- `precis.ref_passes` entry-point group (PR 4).

## Open questions

- **5-minute lease on `coordinator`** — is it enough? A
  yield-and-relinquish cycle that includes hundreds of child
  submits might exceed 5 minutes. Bump to 15 if profiling shows
  it; document the floor.
- **wake_runner cadence** — currently piggy-backs on the
  system worker's 2-second idle poll. For latency-sensitive
  flows (human ack → resume), this is fine. If we later need
  sub-second wake latency for some flow, run wake_runner in a
  tighter loop.
- **`tag_cleared` for non-`ask-user:` tags** — generalize
  `_STATUS_FOR_WAKE_KIND` to read from `wake_when.payload.status`
  if a coordinator wants a custom waiting status. Defer until a
  caller asks.
