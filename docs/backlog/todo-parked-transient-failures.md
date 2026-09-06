---
status: draft
prio: high
title: Transient child failures park todos permanently; no wake-to-open snooze
---

# Transient child failures park todos permanently; no wake-to-open snooze

Todo-infra review (2026-09-06), goal frame: self-continuing long-running
tasks. Two related gaps where the park mechanism has no self-healing exit.

## child-failed should distinguish retryable from terminal

`child-failed:<job_id>` is the right bubble for "a human must decide", but
spend-limit / rate-limit / outage failures land identically to real
failures, and un-parking has historically meant raw SQL (`DELETE` the
ref_tag — see the spend-limit incident). For a self-continuing system,
transient causes must self-heal:

- Classify at bubble time (the executor knows the failure class):
  `child-failed:<id>` stays the terminal form; a retryable cause gets a
  distinguishable form (e.g. `child-failed:<id>:retryable` or a
  `retry_after` in job meta).
- The auto_check worker (already polling every cycle, SQL-only) clears
  retryable bubbles after a backoff and lets dispatch re-mint — bounded
  by the existing resume-streak cap so a hard-down dependency still
  escalates to the terminal form.

## auto_check needs `on_resolve: open` (wake/snooze)

`time_past` flips a leaf to `STATUS:done` — `precis-auto-todo-help`
Pattern 3 itself admits the workaround ("if the intent is to re-open,
write a sibling"). The honest primitive for "snooze until date" /
"re-check next week" is a resolution target on the spec:

- `auto_check.on_resolve: 'done' (default) | 'open'` — `open` returns the
  leaf to the doable pool instead of completing it.
- Applies to every evaluator, not just `time_past` (e.g. "when the paper
  is ingested, wake the reading task" reads better than done-flipping a
  fake wait-leaf and wiring blocked-by).
- Note: the tree audit rejected rich due-dates "until a real consumer
  asks" — a self-continuing fleet is that consumer; this is the minimal
  form (no new columns, one meta key).

test: retryable child-failed clears after backoff and the leaf re-enters
doable; `time_past` with `on_resolve: open` lands the leaf back in
doable, not done.
