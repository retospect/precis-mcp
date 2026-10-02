---
status: draft
title: Reconcile duplicate claim hubs cheaply — re-check on embed, watermark, calibrated cutoff, one backfill, pre-publish check
pillar: memory-graph
---

# Reconcile duplicate claim hubs cheaply

Ruled by Reto 2026-10-02 (td461151, review-queue `reto-dup-hub-design-1`):
"Yes — file it", the cheap design, in the order below. Origin: gripe
180306. `fi176861` and `fi178714` were minted as two hubs for one claim,
because at mint time `canon.block()` ANN-retrieved over embeddings the
younger hub did not have yet. Starting code: stranded branch
`gripe_180306`, in `~/work/archive/stranded-gripes-2026-10-01.bundle` on
Reto's Mac (not on melchior). Reuse its winner selection,
merge-into-winner, skip+log path and tests.

## Motivation / why

Mint-time dedup (`taproot/canon.py::block` → `dedup_judge` → `place`)
only sees hubs that already have an embedding. Embeddings come from the
worker, after the mint. Two hubs for one claim split the claim's evidence,
cites and verdicts, and a paper citing either one carries the split.
A second source of twins: an errored dedup judgment is read as
"different" (gr462136), so a failed judge burst also mints a duplicate.
That one is latent: 0 errored of 1,049 `taproot:dedup` calls in the 7
days to 2026-10-02.

## In scope (in Reto's order)

1. **Re-check on embed.** When the worker writes a new hub's embedding,
   run the duplicate check against its nearest live hubs.
2. **Text-version watermark.** Record, per hub, the text version last
   checked, so an unchanged hub is never re-checked.
3. **Calibrated distance cutoff.** Only pairs closer than a cutoff reach
   the LLM judge. Calibrate it on hand-merged twins (e.g.
   fi176861/fi178714) and record the calibration set with the number.
4. **One-time backfill** over existing hubs, using 2 and 3.
5. **Pre-publish check.** Before a draft is published, check the hubs it
   cites for duplicates.

## Explicitly NOT in scope

- Changing mint-time dedup itself (block/judge/place).
- gr462136's error-handling fix (retry, refuse or mark unchecked). It is
  separate and owned by claims-and-evidence; this item only consumes a
  `dedup-unchecked` marker if that fix adds one.
- Re-judging pairs above the cutoff.

## Acceptance criteria

- A hub minted before its twin's embedding existed is merged into the
  winner within one worker pass after its embedding lands. Test with the
  branch's fixtures.
- An unchanged hub is not re-checked: zero judge calls on a second pass.
- The cutoff value and its calibration set are written in the owning
  docstring; fi176861/fi178714 fall inside it.
- The backfill's cost and the count of merges and skips are logged; the
  skip+log path records every pair it declined.
- Publishing a draft that cites a known twin reports it before publish.

## Target + blast radius

`taproot/canon.py` (block, judge), the hub merge path from branch
`gripe_180306`, the embed worker's post-write hook, the nanopub publish
path (item 5), `finding` refs and their `links`.

## Open questions / decisions log

- Where the watermark lives (hub `meta` vs a column): decide at build.
- The branch must be recovered from the bundle on Reto's Mac before
  building.
