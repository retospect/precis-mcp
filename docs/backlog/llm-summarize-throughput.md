# llm_summarize backlog throughput tuning

Sustained slot contention between the two melchior workers is benign (the
pass keeps producing; the per-chunk ERROR log-flood is fixed) — but if the
backlog needs to clear faster, the knobs are
`precis_worker_summarize_concurrency` (host_vars) or the llama-swap
`--parallel` for that model, weighed against melchior's known RAM/jetsam
fragility before bumping. Ops tuning.
