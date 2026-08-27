---
status: draft
title: "evidence-edge pushback: re-verify the born-released corpus (1252 stamped-unverified edges) against prod, coordinated with active minting"
---

# What remains (the write-path fix shipped)

The code half is done: no attach path writes `support` without
`support_reason` + `verified_by` (+ `verified_at` / `verified_claim_sha`);
mechanical mints are born withheld; `hub_refine` re-verifies a hub's
attached-but-unverified edges per pass; the preflight withholds a
sha-stale verdict. What's left is **operational**, against prod:

1. **Run the sweep over the standing corpus.** `precis taproot
   verify-edges` (default withheld cohort — 264 edges / 248 hubs measured
   2026-08-27), then `--unverified-stamped` (the born-released cohort:
   `support` set, no `verified_by` — 1252 edges measured 2026-08-21; a
   non-corroborating verdict STRIPS the stamp and returns the edge to
   withheld). Dry-run first; `--apply` **must be coordinated with active
   minting sessions** — other sessions are minting against these hubs
   right now, and stripping mid-flight blocks their publishes without
   warning. Run it as its own announced pass, per-claim-set if needed
   (the nanobuds set was already pushed back 2026-08-21; ~1208 edges
   outside it remain). `hub_refine` chips at the same debt
   (`_REVERIFY_PER_PASS` per hub per pass) but never strips — the sweep
   is the completion mechanism.
2. **The 209 legacy verified edges carry no `verified_claim_sha`.**
   Verdicts from the 2026-08-21 retro-verify pass predate the sha stamp;
   invalidation is forward-only by design, so an edit to those claims
   will NOT withhold their (now possibly stale) verdicts. Optional
   tightening once the sweep above has run clean: re-stamp them (any
   verify pass over them adds the sha) or accept the exposure for the
   fixed historical cohort.

Acceptance for closing this file: both sweeps run `--apply` against prod
with the strip counts recorded, and
`support IS NOT NULL AND NOT meta ? 'verified_by'` returns 0 corpus-wide
(the write-path guarantee already holds it at 0 for new edges).
