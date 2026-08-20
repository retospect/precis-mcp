---
status: draft
title: "dedup sweep over the repaired block() index — 24 near-duplicate hub pairs, 9 actionable"
---

# The convergences that never happened

`block()` retrieved candidates over `card_combined` (`ord=-1`) until
2026-08-20, and only 187 of ~1,524 hubs had one — 12.3% coverage. So `place()`
almost never saw a candidate, and the merge path it exists to run has
effectively never run at scale. The index now reads `finding_body` (`ord=0`),
which every hub has. This item is the one-time backward sweep over the
duplicates that accumulated while the index was blind; `place()` handles the
forward direction from here.

## The cohort, measured 2026-08-20 — **contaminated, re-measure first**

The measurement below used `TAPROOT:claim` alone as the hub predicate, which is
`block()`'s definition and includes **280 rows that are not hubs** — see
`claim-hub-definition-divergence.md`. The strict population is 1,244, not 1,527.
Re-run the all-pairs scan against `TAPROOT:claim` **+ `STATUS:canonical`** before
acting on any band; pairs whose either side is a chase-tree finding are not
duplicates and must not reach the merge path. The table stands only as evidence
that a real cohort exists.

Cosine distance between the `finding_body` chunk embeddings of every live
`TAPROOT:claim` hub pair (1,527 embedded, permissive predicate, all-pairs):

| band | pairs |
|---|---|
| < 0.02 | 4 |
| < 0.05 | 9 |
| < 0.10 | 24 |

The nine under 0.05 — the actionable set:

| dist | pair | shape |
|---|---|---|
| 0.0000 | `fi191179` / `fi191260` | identical sentence, **forked on `scope`** |
| 0.0000 | `fi191192` / `fi191262` | identical sentence, **forked on `scope`** |
| 0.0008 | `fi191256` / `fi191263` | same result, one framed "Ravi et al. synthesized", one "Fluorescence measurements on" |
| 0.0088 | `fi176714` / `fi178555` | same light-gated STOP-GO shuttle claim |
| 0.0205 | `fi177386` / `fi177399` | Top7 — "designed via Rosetta fragment assembly and energy minimisation" vs "designed entirely by energy minimisation with Rosetta" |
| 0.0325 | `fi176432` / `fi177486` | HKUST-1 Young's modulus **9–12 GPa vs ~9 GPa** |
| 0.0333 | `fi176861` / `fi178714` | two-stage kinetic-proofreading — "can eliminate" vs "eliminates" (`gripe #180306`) |
| 0.0396 | `fi176667` / `fi176669` | largest spiroligomer **macrocycles** vs **structures** |
| 0.0445 | `fi176919` / `fi177522` | contact resistance "at a single metal-molecule-metal interface" vs "per molecular interface" |

Note the corpus is not only nanobuds — most of this cohort is the
molecular-machines material, so the sweep is not a nanobud-campaign task and
should not wait on it.

## These are four different problems wearing one shape

Distance does not tell you which. Each band needs a different verdict, and
three of the four are **not** merges:

1. **Scope forks** (the two 0.0000 pairs). Same sentence, different `scope`,
   so they are distinct `pub_id`s but collide on AIDA URI at publication —
   see `aida-uri-ignores-scope.md`. Either they are one claim (merge) or the
   scope is load-bearing and the *sentences* are inadmissible as written.
2. **True paraphrases** (`fi176861`/`fi178714`, `fi176714`/`fi178555`,
   `fi191256`/`fi191263`). Merge: keep one hub, move evidence, retire the
   other. This is what `place()` would have done.
3. **Neither claim is grounded** (`fi176432`/`fi177486`: 9–12 GPa vs ~9 GPa).
   Read as a point-vs-range disagreement until the sources were checked
   2026-08-20; it is not one. `fi176432` grounds to pa1698, whose passage
   reports *hardness* "at least 130% greater than … conventional MOF
   counterparts" and gives no modulus figure at all. `fi177486` grounds to
   pa4246 — *ZIF-8 films as low-κ dielectrics*, a different MOF — with a NULL
   grounding chunk. **Do not merge and do not harmonise into a range**: two
   unsupported claims averaged together are still unsupported. Reground against
   a source that measures HKUST-1's modulus, or retract both. The only other
   edge each carries is a `cites` from draft 42995, which is the drafting
   document, not evidence.
4. **Scope-differing near-synonyms** (`fi176667`/`fi176669`: macrocycles vs
   structures; `fi176919`/`fi177522`: per-interface vs single-interface).
   These may be a `refines` relationship, not a merge — one is strictly
   narrower than the other.

## Constraints on the pass

- **Merging changes `pub_id`** (identity is sentence + scope), so it must run
  while hubs are `candidate` and **before** re-approval — the same ordering
  constraint the notation and scope backfills carry.
- **Dry-run first, reviewed by a human before any write.** This is the first
  large run of a merge path that has never executed at scale; the standing
  rule is a confidence floor, an idempotency story and a dry-run before any
  corpus-wide automated writer commits.
- **Over-merge is the one dangerous direction** — `eval_canon`'s live gate
  requires zero false `same`. Under-merge is tolerated. Bands 3 and 4 above
  are exactly where an automated judge would over-merge, so they should route
  to a human rather than to `merge_confirm`.
- `gripe #180306` filed pair 7 independently; closing it is part of done.

## Why 24 and not more

All-pairs cosine over 1,527 hubs is a floor, not a census. Embedding
proximity measures topical similarity, so it finds paraphrases and misses
duplicates phrased in different vocabularies — the same structural bias
documented for the opposition finder in
`disputes-edge-nonblocking-disagreement.md`. Do not report the post-sweep
count as "the corpus is now deduplicated".
