# HNSW recall check at current scale

`chunk_embeddings` >1M rows on default build params and default
`ef_search=40`, never benchmarked — measure recall@k; consider
`SET LOCAL hnsw.ef_search` per query. Sonnet-shaped run + a judgment
read of the results.
