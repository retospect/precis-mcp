---
status: draft
title: "evidence edges are born past the publish gate — three attach paths write support:\"yes\" as a default, 1252 of 1461 stamped edges were never verified"
---

# The publish gate is open, corpus-wide

`nanopub.preflight.withheld_edges` withholds an evidence edge on
`meta->>'support' IS NULL`. Support is therefore not a verdict the gate waits for —
it is a key whose mere presence releases the edge. Three attach paths write it at
**mint time**, before anything has read the passage:

| writer | `meta.origin` | value written |
|---|---|---|
| `taproot/backfill.py` `_edge_meta` | `draft-backfill` | hard-coded `"support": "yes"`, `caveats: []` |
| `taproot/directed.py` `edge_meta` | `directed-mint` | hard-coded `"support": "yes"` (carries `quote` from the qualify step — the least bad of the three) |
| `taproot/authoring.py` | *(none)* | `supporter.get("support", "yes")` — the minting agent self-reports, defaulting to yes on omission |

## Measured on prod, 2026-08-21

Over all `establishes`/`corroborates` edges:

| shape | edges |
|---|---|
| `support` set, **no** `support_reason`, **no** `verified_by` | **1252** |
| ↳ of which `origin='draft-backfill'` | 57 |
| ↳ of which the authoring on-ramp (no origin) | 1195 |
| carries a real verdict (`support_reason` + `verified_by`) | 209 |
| `support IS NULL` — actually withheld | 48 |

**86% of stamped evidence edges assert support that nothing checked**, across 1174
hubs, written continuously `2026-07-30 → 2026-08-20` (still live). The 209 verified
ones are *all* from the one-off retro-verify pass in
`nanobud-retro-verify-2026-08-21.md` — no scheduled verifier has ever run against
this corpus.

The consequence is not that the gate is leaky. It is that the gate has, so far,
only ever withheld edges the mint path happened to leave blank. `hub_refine`'s
`if source_ref_id in attached … continue` precheck means turning the verifier on
will not fix any of the 1252 either — an already-attached edge is skipped.

## Distinct from the July cohort

`evidence-edges-assert-support-with-no-passage.md` + `taproot/repair_evidence.py`
cover 369 edges that assert support with `src_chunk_id IS NULL` — support for a
passage nobody identified. That docstring's "the writing path is fixed" refers to
the *ungrounded* half. These 1252 are grounded (a chunk is named); what is missing
is that anyone read it. Same gate, different hole.

## Decided so far (2026-08-21)

Scoped to the nanobuds draft only — other claim sets belong to other people and
were deliberately left alone. Within nanobuds the 44 `draft-backfill` auto-`yes`
edges are being **pushed back**: re-judged from their grounding chunk, a real
verdict written where they hold, the stamp **stripped** where they don't (which
returns the edge to withheld, behind the gate, rather than leaving a `yes`
nobody stands behind).

**The rest of the corpus still needs the same pushback**, eventually — 1208
edges outside nanobuds, plus the write-path fix so the number stops growing.
Deliberately deferred, not forgotten: other sessions are actively minting
against those hubs right now, and pushing their edges back mid-flight would
block their publishes without warning. Do it as its own coordinated pass.

## What to decide

1. **Stop minting released edges.** The default has to be `support` absent, so a new
   edge is withheld until verified. Cheap, but it makes every future
   `nanopub publish` block on a verifier actually running — which is the point, and
   is a workload decision, not a code decision.
2. **The 1252 already written.** Either re-verify (the retro-verify pass generalizes
   — 239 edges cost ~12 opus agents), or strip `support` on the unverified cohort
   (`NOT meta ? 'verified_by'`) and let the verifier refill it. Stripping is the
   honest state but instantly blocks every pending publish.
3. **`hub_refine`'s skip-if-attached precheck** must gain a "…unless unverified"
   arm, or the verifier can never reach this cohort.

Do not read "auto-review is happy" as a signal on any claim set until 1 and 3 land.

## Acceptance

- No attach path writes `support` without a `support_reason` + `verified_by`.
- A query for `support IS NOT NULL AND NOT meta ? 'verified_by'` returns 0 on
  edges created after the fix.
- `hub_refine` re-verifies an attached-but-unverified edge.
