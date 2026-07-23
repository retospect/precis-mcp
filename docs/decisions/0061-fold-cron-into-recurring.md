# ADR 0061 — fold `kind='cron'` into `level:recurring` (supersedes ADR 0030's cron ruling)

- **Status**: accepted (2026-07-22)

**Supersedes**: [ADR 0030](./0030-job-finding-cron-stay-separate.md)'s
`kind='cron'` section only. 0030's `job` and `finding` rulings are
untouched — this ADR revisits the *cron* leg alone, at Reto's explicit
direction, after reviewing 0030's original reasoning.

## Context

ADR 0030 audited three "should this fold into `todo`?" candidates and
rejected all three, including `cron`, on the grounds that cron is a
**push** notification system (a launchd timer fires
`pg_notify('precis.cron', ...)`; asa_bot LISTENs and delivers a
synthetic prompt to a Discord conversation) while `level:recurring`
todos are a **pull-into-queue** system (a worker mints subtasks into
the doable queue where they're worked, refined, delegated). Different
consumers, different lifecycles — 0030 concluded "keep separate."

That reasoning was sound at the time and remains sound as a
description of the *mechanism difference*. It is being reversed
anyway, at Reto's call, for a reason 0030 didn't weigh: **maintaining
two mental models for "a thing that happens on a schedule" is itself
a cost**, independent of whether the two mechanisms differ. An agent
(or Reto) reaching for "schedule a reminder" has to first decide which
of two kinds it needs, each with its own put-shape, its own view
surface, its own skill. The visible symptom motivating this: the
consolidated `/refs?kinds=cron` browse route was a second, thinner
surface over data that mostly duplicated what `level:recurring` todos
already model (a schedule + a fire history) — two thin surfaces where
one full one would do.

## Decision

**Retire `kind='cron'`.** Its two responsibilities move onto
`level:recurring` todos as fields, not a second kind:

1. **Scheduling.** `meta.schedule` gains a third shape, `at` (an
   absolute one-shot ISO 8601 timestamp), alongside the existing
   `cron`/`every` recurring forms. A one-shot is a `level:recurring`
   todo whose schedule fires exactly once — the worker resolves it
   (fire / expire) and tags the root `STATUS:done` so it never
   re-fires. `meta.schedule.catch_up` (default `True`) is the
   one-shot sibling of the recurring `backfill_missed` knob, matching
   the retired cron kind's default ("late is better than never" for
   one-shot; "don't burst-fire on catch-up" for recurring).
2. **Push delivery.** `meta.deliver = {'target':
   'conv:discord/<g>/<c>/<t>'}` marks a recurring (or its ticks) for
   push delivery instead of (or in addition to a folder-level
   automation) minting a subtask into the doable queue. A due tick
   with `meta.deliver` set fires the **identical wire payload** the
   retired cron kind fired — `pg_notify('precis.cron', {cron_id,
   payload, target})` — so asa_bot's listener
   (`asa_bot/pg_listen.py` + `bot.py::_handle_cron`) needs **zero
   changes**: it never read back a `kind='cron'` ref in the first
   place, only the notify payload. This is the load-bearing fact that
   makes the fold cheap — the "delivery address" ADR 0030 worried
   would "leak into every code path that walks recurring todos"
   turns out to be one optional dict on `meta`, checked in exactly one
   place (the schedule spawner's per-tick loop), not a pervasive
   concern.

Both fields live on the same `level:recurring` node. A recurring with
neither `meta.deliver` nor an `executor`/`job_type` pair behaves
exactly as it always did — a plain queue-mode spawn. Nothing about
the existing Slice-4 mechanics (Watches umbrella, idempotency stamp,
collision-skip, backfill policy) changes for that path.

Delivery-mode ticks skip queue-mode's collision-skip guard entirely —
there's no subtask to collide with, matching how the retired cron kind
never touched the todo queue either. Idempotency for a delivery-mode
tick lives on a `ref_events(source='schedule', event='deliver')` row
(there's no child ref to stamp `meta.spawned_for_tick` on).

## Why this reverses 0030 rather than confirming it

0030's rejection rested on "different consumers, different
lifecycles, different metadata shapes." Revisiting each:

- **Different consumers** — true, but the consumer-facing contract
  (`pg_notify('precis.cron', {cron_id, payload, target})`) turns out
  to be a thin, stable wire format that doesn't care what row fired
  it. Folding the *producer* side (which row decides to fire) onto
  `level:recurring` doesn't touch the *consumer* side (asa_bot) at
  all.
- **Different lifecycles** — a one-shot ("fire once, retire") looked
  irreducibly different from a recurring ("fire forever on a
  cadence") at first glance. In practice a one-shot is just a
  recurring whose schedule matches exactly one instant, and "retiring
  after the one tick" is a two-line addition (tag `STATUS:done`) to
  a worker that already tags terminal states (`paused`) as
  ineligible for the next sweep.
- **Different metadata shapes** — `meta.deliver` (one dict, one key)
  is not meaningfully more metadata surface than `meta.executor` +
  `meta.job_type` + `meta.params`, all of which already coexist on
  `level:recurring` today (see `docs/design/todo-tree-plan.md`
  §"Recurring + schedule" and the `briefing`/`card_forge`/
  `meditation`/`reading_brief` job_types). The recurring root was
  already a small polymorphic dispatch table on `meta`; `deliver` is
  one more entry in that table, not a new axis of complexity.

0030's own methodology note stands as written: "does X carry
mechanisms the destination kind would have to reimplement?" For
`cron`, the answer turns out to be **no** — the mechanism (schedule
tracking + a fire-and-forget notify) was *already* being reimplemented,
piecemeal, inside `level:recurring`'s job_type dispatch
(`briefing`/`card_forge` etc. already fire deliveries via the sibling
`message` kind's `pg_notify('precis.messages', ...)` path). `cron`'s
distinct wire channel (`precis.cron`, driving a full synthetic-prompt
Claude turn rather than posting pre-composed text) was the one piece
actually missing from that table — and it slots in as a single
optional field, not a parallel state machine.

## Migration

No schema migration — `kind` is a plain string column on `refs`;
retiring a kind is a Python-level change (drop the `KindSpec`
registration), not a `*.sql` change. Existing `kind='cron'` rows are
converted to `level:recurring` todos by a one-off, `--commit`-gated
backfill script (`scripts/migrate_cron_to_recurring.py`), not a
forward-only migration — the row-shape translation involves judgment
calls (best-effort mapping of the old free-form recurrence vocabulary
`hourly`/`daily`/`weekly`/`every <N> <unit>` onto the new 5-field cron
grammar; `weekly` in particular had no fixed day-of-week and defaults
to Monday post-migration) that don't belong in a schema migration, and
the script is designed to be reviewed and re-run rather than applied
blind on every boot. **This script has not been run against prod** —
running it is a follow-up (tracked in `OPEN-ITEMS.md`), since it needs
human review of the translated schedules before commit, not something
this change should do unattended.

## Consequences

- `src/precis/handlers/cron.py` is deleted. `CronHandler`'s
  `dispatch.py` registration is removed.
- `src/precis/cli/cron.py` (`precis cron tick`) is **kept** as a thin
  CLI shim delegating to
  `precis.workers.schedule.worker.run_schedule_pass` — the same
  engine the default worker rotation and the decentralized
  `scheduler` pass's `cron_tick` cadence now share. The launchd timer
  invoking `precis cron tick` every 60s needs no immediate update;
  retiring that standalone timer in favour of the decentralized
  `scheduler` pass (`PRECIS_SCHEDULER_ENABLED`) is an orthogonal ops
  cleanup, not required by this change.
- `src/precis_web/routes/refs.py`'s `/refs?kinds=cron` route is
  retired along with the kind; recurring todos (delivery-mode or
  queue-mode) browse through the existing `/refs?kinds=todo` /
  `search(kind='todo', view='roots')` surfaces.
- The handle registry (`src/precis/utils/handle_registry.py`) drops
  the `cron`/`cp` record and chunk codes.
- `precis-cron-help` is retired to a redirect stub pointing at
  `precis-recurring-help` (kept, not deleted, so an agent that
  remembers the old skill id still finds the new surface).
  `precis-recurring-help` gains the `at`/`deliver` sections.
  `precis-automations` is rewritten for the unified model.
- `docs/design/todo-tree-plan.md`'s foldability table row for `cron`
  changes from "No — see ADR 0030" to "Yes — see ADR 0061."

## Alternatives considered

- **Do nothing (keep 0030's ruling).** Rejected per Reto's explicit
  direction: the two-kinds-for-one-concept cost outweighs the
  mechanism-purity argument, now that the delivery field has been
  shown to be cheap to bolt on.
- **Model one-shot as a bare `meta.wake_at` on any todo (not
  `level:recurring`-tagged).** Considered and rejected: it would add a
  *second* schedule-bearing shape outside the `level:recurring`
  candidate-enumeration path the worker already walks, meaning two
  code paths instead of one. Folding one-shot into the same `at`
  schedule shape (still under `level:recurring`) keeps exactly one
  enumeration query, one worker pass, one skill.
- **Route delivery through the `message` kind instead of a raw
  `pg_notify('precis.cron', ...)`.** Rejected: `message` posts
  pre-composed text verbatim (no agent turn); `cron`'s defining
  behaviour is driving a *live* Claude turn from a synthetic prompt.
  These are genuinely different asa_bot code paths
  (`_handle_outbound` vs `_handle_cron`/`_deliver_cron_prompt`) and
  conflating them would lose the "reminder that makes Asa go look
  something up and respond" use case. Reusing the exact existing wire
  format keeps that distinction while still killing the second kind.

## References

- ADR 0030 (superseded, cron section only) —
  `docs/decisions/0030-job-finding-cron-stay-separate.md`
- `src/precis/workers/schedule/worker.py` — the unified spawn/deliver
  engine (`_process_one_shot`, `_fire_delivery_conn`)
- `src/precis/workers/schedule/parse.py` — the `Schedule.at`/
  `catch_up` shape + `one_shot_action`
- `src/precis/handlers/_todo_guards.py::check_deliver_in_meta` —
  write-time validation for `meta.deliver`
- `src/asa_bot/bot.py::_handle_cron` /
  `src/asa_bot/pg_listen.py` — the unchanged delivery-layer consumer
- `scripts/migrate_cron_to_recurring.py` — the one-off backfill
  (not yet run against prod)
