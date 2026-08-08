# Backlinks panel — text-scan coverage + deep page

The Meta-tab "Referenced by" panel shipped over the materialized
`links.dst_ref_id` reverse index. Blocker for a naive inbound text-scan:
there is no trigram GIN on `chunks.text`, so a read-time scan is a full seq
scan on the paper hot path. Preferred fix (ii): materialize inline
`[pa]`/`[pc]` cites into `links` at draft-save + one-time backfill — no
per-page scan, and it fixes the undercount (repeat cites collapse to count=1
today). Alternative (i): a `gin_trgm_ops` index (measure size/write cost
first). Also open: a deep `/papers/<id>/backlinks` page with per-kind filter
+ the citing sentence via `src_chunk_id`. Owner
`src/precis_web/routes/papers.py::_backlinks`. Needs design.
