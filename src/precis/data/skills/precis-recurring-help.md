---
id: precis-recurring-help
title: precis — recurring tasks via level:recurring + meta.schedule
applies-to: put (kind='todo' with level:recurring + meta.schedule); precis worker --only schedule
status: active
---

# precis-recurring-help — scheduled work in the todo tree

Recurring work (dreams, weather pulls, "look for new conferences",
birthday reminders) lives in the same tree as everything else. The
pattern: a `level:recurring` root carries the schedule + the spawn
rule; each tick mints a fresh `level:subtask` child that runs once.
The recurring root never appears in the doable queue — it's the
*pattern*; only its spawned subtasks are *actions*.

## Watches umbrella

A seeded `Watches` ref tagged `level:recurring` with
`meta.builtin='watches-root'` sits at the top of the tree. Every
recurring lands under it by default — leave `parent_id` off your
`put` and the handler wires `parent_id=<watches-root>` for you.

```python
put(kind='todo',
    text='Check arxiv weekly',
    tags=['level:recurring'],
    meta={'schedule': {'cron': '0 9 * * 1'}})
# → parent_id = the Watches root
```

The umbrella itself is a folder (`meta.schedule` is null) and the
spawner skips it. It carries `meta.builtin` so `delete` refuses to
remove it — orphaning every recurring is a real footgun.

Recurring work that serves a goal can nest under a strategic:

```python
put(kind='todo',
    text='Birthday reminders',
    parent_id=56,            # under "Personal life"
    tags=['level:recurring'],
    meta={'schedule': {'every': 'mon 09:00',
                       'backfill_missed': True}})
```

## Schedule format

Two shapes, one canonical:

* `meta.schedule.cron` — a 5-field cron string. The runtime always
  sees this form.
* `meta.schedule.every` — write-time shorthand that the handler
  translates to cron and rewrites in place. Accepted:
  * `Nm` (every N minutes; 1..59)
  * `Nh` (every N hours, on minute 0; 1..23)
  * `1d` (every day at 00:00) — `2d`+ isn't a clean cron field, so
    use a weekly form (`every: mon 09:00`) for slower cadences
  * `mon|tue|...|sun HH:MM` (weekly at HH:MM on that dow)

```python
# These three are equivalent:
meta={'schedule': {'every': '1h'}}
meta={'schedule': {'every': '60m'}}            # invalid — m capped at 59
meta={'schedule': {'cron':  '0 * * * *'}}      # canonical form
```

A bad cron string raises `BadInput` at `put` time — not at the next
tick. The catalogue lists every accepted shape on rejection.

## Tick mechanics

Each tick mints exactly **one** subtask under the recurring root.
Three guards in order:

1. **Idempotency** — the spawned child carries
   `meta.spawned_for_tick='YYYY-MM-DDTHH:MM'`. A second pass on the
   same minute finds the existing child and is a no-op.
2. **Collision** — if any prior spawned child is still open
   (non-done STATUS), the spawner *skips the new tick*. A stalled
   queue doesn't pile up; the nursery sweep surfaces the stuck leaf.
3. **Backfill** — `meta.schedule.backfill_missed` (default `False`)
   controls catch-up after worker downtime. `False` → only the most
   recent tick is considered (weather: yesterday's headlines don't
   matter). `True` → every missed tick mints a child (birthdays:
   you still owe the action).

## Provenance

* `parent_id` chain → `level:recurring` answers "is this
  cron-spawned?"
* `ref_events.source='schedule'`, `event='spawn'` answers "when was
  it spawned, by which recurring, for which tick?"

No new tag invented. The dashboard pulls "last tick" from the
event log directly.

## Views

`view='roots'` grows a second panel:

```
## Watches (3 recurring)
#12 Check arxiv weekly                cron: 0 9 * * 1   last: 2026-06-08T09:00
#13 Weather                           cron: 0 7 * * *   last: 2026-06-14T07:00
#14 Dream nightly                     cron: 0 3 * * *   last: 2026-06-14T03:00
```

Spawned subtasks land in `view='doable'` like any other PRIO-2 work
— cron-spawned defaults to `prio=2` so it preempts the strategic
rotation (PRIO 3-10) but doesn't outrank an explicit chat ask
(PRIO 1).

## Worker

`precis worker --only schedule` runs the spawner alone (useful for
backfills); the default rotation includes it alongside `auto_check`.

## Anti-patterns

* **Subtasks edited mid-pass.** Children minted by a tick are normal
  subtasks — workers may claim them, split them, mark them done.
  Don't re-tag a spawned child as `level:recurring` to "make it
  reschedule" — the spawner walks the recurring *root*, not its
  children. Mint a new recurring instead.
* **Using `STATUS:paused` on the wrong layer.** Pausing the
  recurring root pauses the schedule. Pausing a spawned subtask
  just pauses that one tick; the next tick still mints.
* **`* * * * *` schedules.** Owner-only on purpose — the
  level-gradient guard rejects level:recurring writes from worker
  sources. If you really want minute-by-minute cadence, run it
  inline; the queue isn't built for it.

## Related skills

* `precis-tasks-help` — the tree shape, level gradient, doable rules
* `precis-auto-tasks-help` — `meta.auto_check` leaves (the other
  worker-driven leaf pattern; orthogonal to `meta.schedule`)
* `precis-cron-help` — legacy `kind='cron'` (still works; new code
  uses `level:recurring`)
