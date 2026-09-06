---
status: draft
prio: high
title: Transient child failures park todos permanently (retryable child-failed)
---

# Transient child failures park todos permanently

Todo-infra review (2026-09-06), goal frame: self-continuing long-running
tasks. (The sibling gap — no wake-to-open snooze — shipped as
`auto_check.on_resolve: 'open'`; see git log.)

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

test: retryable child-failed clears after backoff and the leaf re-enters
doable.
