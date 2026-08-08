# OA acquisition + structured ingest + external search (roadmap)

Root cause of "OA but we don't have it": publisher-side TLS/IP-reputation
403s (not a UA gate). Cascade: free legs first (publisher-deterministic →
PMC-OA JATS → arXiv → oa_url), then the OpenAlex Content API (~$0.01/file;
built unshipped as `_try_openalex_content`, double-gated default-OFF) ahead
of a paid web-unlocker (last resort; never Sci-Hub). Prefer GROBID TEI for
chunks, keep the PDF. Roadmap: PMC/Europe-PMC leg (keystone) · bioRxiv S3 ·
unlocker · supplementary/SI ingestion · JATS/TEI structured ingest (Phase-2
re-ingest is a hazard — citations anchor by `source_handle`; reanchor by
quote first) · parallel scholarly-graph providers, RRF-fused (Lens adds
patents) · Chinese-lit abstract discovery · historical archive import
(Chemische Berichte pilot; copyright gating) · measure bge-m3 cn↔en placement
(probe the live embedder, don't assume). Bulk arm: BulkSource adapters
s2orc → core → oai → openalex_snapshot (metadata-only) → IA/HathiTrust/
J-STAGE. §E embed-prioritization deliberately unsolved: bulk chunks must
trickle behind live traffic — no bulk pass without a queue policy. Also
built unshipped: `precis enrich-openalex` §G metadata enrichment (edge
materialization rides the provider fan-out; topics→tags waits on
open-namespace-teardown), `precis fetch-openalex`, /papers-needed
failure-reason surfacing. Owner `src/precis/workers/fetch_oa.py`, `ingest/`.
