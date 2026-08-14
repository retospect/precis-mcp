---
status: draft
title: Taproot backfill — LLM outage degrades to no-claim silently, spans never retried
model: opus
---

# Taproot backfill: LLM-outage extraction failure is silent claim loss

`extract_claim` (`src/precis/taproot/canon.py`) fail-safes a **dispatch
error** to the empty extraction — deliberately ("no claim rather than a bad
one"). In `backfill.py`'s cascade (`_run_cascade` → `extraction.is_empty` →
`action="no-claim"`, note "span asserts nothing groundable") that turns an
LLM outage into a *final semantic verdict*: the span's `[pc…]` markers stay
in prose, but nothing re-runs backfill over an already-processed draft, so
the claims are lost with no retry and no error signal anywhere.

Found 2026-08-14 while diagnosing the taproot-migrate pilot, where the same
masking produced a 25/25 NO-CLAIM garbage report (every dispatch
ECONNREFUSED). The migration dry-run got fixed (strict extraction +
consecutive-failure breaker); **backfill still has the hole**.

## Fix direction (needs design, not just a swap)

- `extract_claim_strict` now exists (raises `ExtractionUnavailable` on
  `res.error`; unparseable-but-successful output still degrades to empty —
  that's genuinely semantic). Backfill should distinguish the two:
  infra failure → retryable (fail the group/job so the queue retries, or
  mark the span retryable), semantic empty → final no-claim as today.
- Mind the existing per-group isolation comment in `backfill.py` (mid-loop
  transient failures must not strand earlier groups' prose rewrites) — a
  raising extractor at *plan* time needs its own isolation/retry story,
  not the apply-phase one.
- Sizing: the no-claim degrade also feeds `chase.py`'s bridge and
  `hub_refine` paths via the same helper — audit every `extract_claim`
  call-site for the same infra/semantic conflation before picking the seam.
