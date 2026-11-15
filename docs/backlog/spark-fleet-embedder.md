---
status: draft
title: The GPU node serves embeddings for the fleet — one LAN embedder replaces the per-node loopback copies
pillar: local-compute
prio: high
quest: qu635084
---

# The GPU node serves embeddings for the fleet — one LAN embedder replaces the per-node loopback copies

Reto, 2026-10-02 (review items local-compute-4 and local-compute-5): of the
three Sparks, the GPU node runs the embeddings.

## Motivation / why

Today every worker host runs its own bge-m3: `deploy/playbooks/19a-precis-embedder.yml`
deploys `precis serve-embeddings` on loopback to `gateway`, `scheduler` and
`inference`, and each worker embeds against `127.0.0.1:8181`. On the agent host
that copy competes with the MCP server, the gates and dev work. Reto's
ruling is not to wear the agent host down. One GPU embedder on the GPU node takes
that load off the Macs and gives the fleet one capacity number. That number
is the one `src/precis/embedder_service.py` (capacity decided 2026-10-02) measures (provisional 12.7–13.6
texts/s, CPU/MPS floor).

## In scope

- `precis serve-embeddings` on the GPU node, LAN-bound (not loopback), behind the
  same API. The GB10 GPU should raise the floor well past 13 texts/s;
  measure it.
- Workers and the MCP server point `--embedder-url` at the GPU node. The local
  loopback embedder stays as the fallback when the GPU node is unreachable, so an
  outage on the GPU node degrades rather than stops ingest and search.
- Inventory: a new `embedder` group holding the GPU node. Do not use `inference`:
  that group drives the worker, watch and dft plays, so joining it would put
  science lanes on the GPU node. 19a targets `embedder`.
- The embedder watchdog (`41-precis-embedder-watchdog.yml`) covers the LAN
  service.
- Query latency on the MCP server's path (p50 ~2 s / p95 ~5 s today) is
  measured before and after. A LAN hop must not make search slower.

## Explicitly NOT in scope

- Changing the embedding model or dimension (that would be a re-embed of
  the corpus).
- The N-client load test (`src/precis/embedder_service.py` (capacity decided 2026-10-02), which this item
  feeds).

## Acceptance criteria

- With the GPU node up, `llm_call_log`-adjacent embed timings (or the embedder's own
  metrics) show the fleet's embeds served from the GPU node, and the agent host's local
  embedder idle.
- With the GPU node's service stopped, ingest and search keep working through the
  loopback fallback, with a warning, not an error.
- `tests/test_deploy_tree_no_secrets.py` green (group names only).

## Target + blast radius

`deploy/playbooks/19a-precis-embedder.yml` · inventory overlay (new
`embedder` group) · worker/MCP `--embedder-url` wiring · the embedder
client's fallback path. Deploy-role change: goes to the orchestrator as a
branch.

## Open questions / decisions log

- Blocked on the GPU node host prep (`spark-provisioning.md` §Host items) and
  on the `/mnt/cluster` NFS hang (review item local-compute-6) only as far
  as the GPU node's `nfs_clients` membership goes. The embedder itself does not
  need the share.
