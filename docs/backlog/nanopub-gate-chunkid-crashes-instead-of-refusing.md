---
status: draft
title: mint gate 500s on a non-numeric chunk_id instead of refusing it
---

# mint gate 500s on a non-numeric chunk_id instead of refusing it

Found by the pre-ship reviewer on d5e1d342 (quote-contiguity ship),
2026-09-15. Pre-existing, not a regression from that ship.

`gates.py::_check_passage` does a bare `int(chunk_id)` on
reviewer-submitted payload JSON. A non-numeric `chunk_id` raises an
uncaught `ValueError`/`TypeError` out of `run_mint_gates` rather than
returning a `GateViolation`, so `approve()` dies with a 500 instead of
showing the reviewer a gate rejection they can act on. Every other
malformed field in that payload produces a clean violation.

Two downstream things now lean on this call site, which is why it's
worth closing:

- `mint._freeze_contiguity` documents its "every passage already has a
  resolvable chunk_id" assumption as resting on this gate.
- The gate accepts a JSON *float* (`int(12.0)` works) that Postgres
  rejects (`'12.0'::bigint`). `workers/context_sentence._BACKFILL_SQL`
  already defends against that with a `CASE` guard, but the gate is the
  place the bad value should never have got past.

Fix: wrap the coercion, emit a `GateViolation` naming the offending
value, and require an integral `chunk_id` (reject `12.0` as well as
`"abc"`) so the frozen payload can never carry one Postgres can't cast.

test: a payload with `chunk_id: "abc"` and one with `chunk_id: 12.0`
each produce a gate violation, not an exception.
