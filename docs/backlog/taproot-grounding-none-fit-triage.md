# Taproot evidence-edge grounding — triage the 292 none-fit edges

The semantic backfill took grounding 22%→63%; remaining ref-level edges:
(a) 292 "none-fit" — a mix of genuinely spurious edges (remove, don't
ground), real-but-diffuse whole-paper support, and retrieval misses; needs
top-10 retrieval + full-claim embedding + an explicit "is this edge spurious
at all?" judgment before any removal (candidate set regenerable via the
pgvector LATERAL query); (b) 67 papers have no body-chunk embeddings —
reground after embed:bge-m3 catches up; (c) 9 low-confidence (<0.5)
groundings deliberately held. Semantic rows are tagged
`meta.src_grounding.method='semantic_backfill'` — reversible as a set.
