---
id: precis-recurring-help
title: precis — scheduled work (recurring + one-shot + push delivery) via level:recurring
summary: recurring task patterns — Watches umbrella, cron/every/at schedule shapes, per-tick subtask spawning or push delivery (meta.deliver, folded from the retired kind='cron', ADR 0061)
applies-to: put (kind='todo' with level:recurring + meta.schedule [+ meta.deliver]); precis worker --only schedule
status: active
---

# precis-recurring-help — scheduled work in the todo tree

All scheduled work — recurring (dreams, weather pulls, "look for new
conferences", birthday reminders) **and** one-shot ("remind me in 10
minutes") — lives in the same tree as everything else, under
`level:recurring`. ADR 0061 (superseding ADR 0030) folded the formerly
separate `kind='cron'` push-notification mechanism onto this same tag: a
one-shot schedule is a `level:recurring` node that fires exactly once and
retires itself; a schedule's `meta.deliver` decides whether a due tick
mints a subtask into the doable queue (the original Slice-4 behaviour) or
fires a push notification instead (the retired cron kind's behaviour).

The recurring root never appears in the doable queue itself — it's the
*pattern*; only a queue-mode tick's spawned subtask is an *action*.
Delivery-mode ticks have no subtask at all (see below).

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

Three shapes:

* `meta.schedule.cron` — a 5-field cron string (recurring). The
  runtime always sees this form.
* `meta.schedule.every` — write-time shorthand that the handler
  translates to cron and rewrites in place. Accepted:
  * `Nm` (every N minutes; 1..59)
  * `Nh` (every N hours, on minute 0; 1..23)
  * `1d` (every day at 00:00) — `2d`+ isn't a clean cron field, so
    use a weekly form (`every: mon 09:00`) for slower cadences
  * `mon|tue|...|sun HH:MM` (weekly at HH:MM on that dow)
* `meta.schedule.at` — a one-shot ISO 8601 absolute fire time (ADR
  0061's "remind me in/at" case, folded from the retired `cron`
  kind). Mutually exclusive with `cron`/`every` — a schedule either
  repeats or fires exactly once.

```python
# These three are equivalent (recurring):
meta={'schedule': {'every': '1h'}}
meta={'schedule': {'every': '60m'}}            # invalid — m capped at 59
meta={'schedule': {'cron':  '0 * * * *'}}      # canonical form

# One-shot ("remind me at/in N"):
meta={'schedule': {'at': '2026-06-12T09:00:00Z'}}
```

A bad cron string (or a malformed `at=`) raises `BadInput` at `put`
time — not at the next tick. The catalogue lists every accepted shape
on rejection.

`meta.schedule.backfill_missed` (recurring only, default `False`)
controls catch-up after worker downtime: `False` → only the most
recent tick is considered (weather: yesterday's headlines don't
matter); `True` → every missed tick mints a child (birthdays: you
still owe the action). `meta.schedule.catch_up` (`at`-schedules only,
default `True`) is the one-shot sibling: `True` → an overdue reminder
still fires late; `False` → a reminder missed by more than ~90s is
marked expired instead of firing.

## Push delivery — `meta.deliver` (ADR 0061)

`meta.deliver = {'target': 'conv:discord/<g>/<c>/<t>'}` marks a
recurring for **push** delivery instead of queue-mode spawning: a due
tick fires a synthetic prompt at asa_bot
(`pg_notify('precis.cron', {cron_id, payload, target})` — the exact
wire shape the retired `kind='cron'` used, so asa_bot's listener needed
no change) built from the recurring's own title/text. asa_bot drives a
full Claude turn against it and posts the response to the target
conversation — **no subtask lands in the doable queue** for a
delivery-mode tick; the tick's action *is* the delivery.

```python
# Recurring reminder, pushed rather than queued:
put(kind='todo', text='check the api monitor',
    tags=['level:recurring'],
    meta={'schedule': {'every': '15m'},
          'deliver': {'target': 'conv:discord/<g>/<c>/<t>'}})

# One-shot reminder ("in 10 minutes"):
put(kind='todo', text='ask about the PR status',
    tags=['level:recurring'],
    meta={'schedule': {'at': '2026-06-12T09:10:00Z'},
          'deliver': {'target': 'conv:discord/<g>/<c>/<t>'}})
```

Omit `meta.deliver` for the original Slice-4 behaviour (spawn a
`level:subtask` child into the doable queue — someone/something works
it). A recurring with neither `meta.deliver` nor an
`executor`/`job_type` pair still spawns a plain, unexecuted subtask.

## Tick mechanics

**Queue-mode** (no `meta.deliver`) mints exactly **one** subtask under
the recurring root per due tick. Three guards in order:

1. **Idempotency** — the spawned child carries
   `meta.spawned_for_tick='YYYY-MM-DDTHH:MM'`. A second pass on the
   same minute finds the existing child and is a no-op.
2. **Collision** — if any prior spawned child is still open
   (non-done STATUS), the spawner *skips the new tick*. A stalled
   queue doesn't pile up; the nursery sweep surfaces the stuck leaf.
3. **Backfill** — see `backfill_missed` above.

**Delivery-mode** (`meta.deliver` set) fires the push notify directly
— no child, so no collision-skip guard (nothing to collide with,
matching the retired cron kind). Idempotency lives on a
`ref_events(source='schedule', event='deliver')` row instead of a
child stamp.

**One-shot** (`meta.schedule.at` set, either mode) resolves to fire /
skip-not-yet-due / expire exactly once, then tags the recurring root
`STATUS:done` so it never re-fires — a one-shot is a `level:recurring`
node that retires itself after its one tick.

## Provenance

* `parent_id` chain → `level:recurring` answers "is this
  schedule-spawned?" (queue-mode only — delivery-mode has no child).
* `ref_events.source='schedule'`, `event='spawn'` (queue-mode) or
  `event='deliver'` (delivery-mode / one-shot resolution) answers
  "when did this recurring last tick, and how?"

No new tag invented. The dashboard pulls "last tick" from the
event log directly (both event kinds).

## Views

`view='roots'` grows a second panel:

```
## Watches (3 recurring)
td12 Check arxiv weekly               cron: 0 9 * * 1   last: 2026-06-08T09:00
td13 Weather                          cron: 0 7 * * *   last: 2026-06-14T07:00
td14 Dream nightly                    cron: 0 3 * * *   last: 2026-06-14T03:00
```

Queue-mode spawned subtasks land in `view='doable'` like any other
PRIO-2 work — a spawned tick defaults to `prio=2` so it preempts the
strategic rotation (PRIO 3-10) but doesn't outrank an explicit chat
ask (PRIO 1). Delivery-mode ticks never appear in `view='doable'` —
there's no subtask to show.

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
* `precis-automations` — the standing-automation convention
  (`automation` tag) for a push- or job-driven recurring
* `precis-cron-help` — **retired**: `kind='cron'` is gone. See ADR
  0061 (superseding ADR 0030) for why the push mechanism folded onto
  `meta.deliver` here instead of staying a second kind.
