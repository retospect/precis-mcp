---
status: idea
pillar: memory-graph
title: Memory retrieval learning — a reranker from search traces, an embedder fine-tune only if the oracle says so
prio: low
---

# Memory retrieval learning — reranker from traces, embedder last

What: once the search trace rows (gr548261, gr548263) have accumulated for
about 90 days and the recall set has grown past 300 queries (gr548269), train
a feature reranker over the per-candidate features the trace already stores
(lexical rank, semantic distance, fused rank, kind, touched-age band, tag
overlap, taxon match from gr548267) with label = used/followed vs explicit
miss, and deploy it as the fusion step in place of fixed RRF + the cut.
Fine-tune the embedder (bge-m3) only as a last step.

Why: this is the §3 tail of gr547901 (closed 2026-11-01). The evidence
gathered then says it is not a given: a 2026 deployed-system study found a
cross-encoder *reduced* Recall@10 on all four datasets over a strong
retriever, and an embedder change forces re-embedding ~5M chunks plus an
HNSW rebuild — a multi-day fleet job. So the item carries a decision rule,
not a plan.

Decision rule (in order; each step needs the measurement from gr548268):

1. **Oracle rerank first.** Over the recall set, compute recall@k if the
   candidate pool (pre-cut, from the trace row) were perfectly reordered.
   If oracle recall@5 is not materially above the live number, there is no
   reranker headroom — stop; the gap is candidate retrieval, not ordering.
2. **Feature reranker** (LightGBM/XGBoost or a small MLP over the stored
   features) only when step 1 shows headroom. Train on within-query
   preferences (used/followed over rendered-but-unfollowed); unrendered
   candidates are unlabeled, not negative; memory-review outcomes are gold
   negatives. Time-split train/validation; keep the hand-written part of
   the recall set as the untouched test. Ship behind a flag, judged on the
   recall-vs-tokens curve, per kind.
3. **Cross-encoder** only if the feature model plateaus and the misses are
   textual-interaction cases (negation, exact conditions, numbers).
4. **Embedder fine-tune** only if the oracle in step 1 cannot reach the
   target because relevant items are missing from the pool — i.e. candidate
   recall, not ordering, is the ceiling. Hard-negative mining from the same
   traces; the reindex cost is the acceptance bar.

Sample sizes: ~300–500 labelled queries for directional comparison, 1,000+
to claim a 2–3 point gain; thousands of distinct queries before a trained
reranker is expected to beat the deterministic fusion. The 36-query set is a
smoke test.

Owner anchor: fusion lives in `src/precis/utils/ref_hybrid.py` and the
memory index/recall path in `src/precis/handlers/memory.py`; the eval is
`precis memory index --set` (`src/precis/workers/memory_recall.py`).

test: the oracle-rerank number is printed by `precis memory index --set
--oracle` (slice 0 of this item); a reranker ships only with a paired
offline win on the held-out set and no token regression on the curve.

Closest existing items: `skill-search-rerank.md` (the same decision for
skill search, slice 2 deferred on the same grounds), `graph-health-metrics.md`.
