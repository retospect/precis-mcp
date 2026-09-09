---
status: idea
title: contextual retrieval — prepend document context to chunk text before embedding
---

# Contextual chunk embeddings

Both embedding paths embed bare chunk text: the worker
(`src/precis/workers/embed.py`, `embed([row.text ...])`) and ingest-side
(`src/precis/ingest/blocks.py::fill_embeddings`). Anthropic's contextual
retrieval result (claude-cookbooks RAG recipes): prepending a short
document-level context blurb (title, section, what the chunk is about in the
document's terms) to each chunk before embedding cuts retrieval failures
substantially, more when combined with BM25 hybrid + rerank. We already have
the ingredients — `chunk_summaries`, `chunk_keywords` worker, paper metadata —
but the vector is computed from the context-free chunk body.

Fit with existing invariants: the contextual text must NOT mutate
`chunks.text` (append-only body rows); it's an embed-time transform or a
stored variant alongside `chunk_embeddings`, and the DELETE+INSERT cascade
already re-triggers embedding, so no new invalidation machinery.

Costs / open questions:

- Re-embedding the corpus: `chunk_embeddings` is >1M rows
  ([improve-hnsw-recall-check](./improve-hnsw-recall-check.md)); needs a
  sweep-job rollout, probably newest-first, old vectors serve until replaced.
- Generating the blurb per chunk is an LLM pass (that's the expensive
  variant); a cheap deterministic variant (title + section path prefix) may
  capture much of the win — measure both.
- Measure before building (memory: corpus-detector-measure-base-rate-first):
  a fixed retrieval-failure set from prod search logs, recall@k before/after
  on a sample, before any full-corpus sweep.

test: on the sampled eval set, contextual variant beats bare-text recall@10
by a stated margin, or the item dies with the numbers attached.
