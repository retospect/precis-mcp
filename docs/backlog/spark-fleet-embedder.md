---
status: draft
title: spark serves embeddings for the fleet — one LAN embedder replaces the per-node loopback copies
pillar: local-compute
prio: high
---

# spark serves embeddings for the fleet — one LAN embedder replaces the per-node loopback copies

Reto, 2026-10-02 (review items local-compute-4 and local-compute-5): of the
three Sparks, spark runs the embeddings.

## Motivation / why

Today every worker host runs its own bge-m3: `deploy/playbooks/19a-precis-embedder.yml`
deploys `precis serve-embeddings` on loopback to `gateway`, `scheduler` and
`inference`, and each worker embeds against `127.0.0.1:8181`. On melchior
that copy competes with the MCP server, the gates and dev work. Reto's
ruling is not to wear melchior down. One GPU embedder on spark takes
that load off the Macs and gives the fleet one capacity number. That number
is the one `embedder-capacity-ownership.md` measures (provisional 12.7–13.6
texts/s, CPU/MPS floor).

## In scope

- `precis serve-embeddings` on spark, LAN-bound (not loopback), behind the
  same API. The GB10 GPU should raise the floor well past 13 texts/s;
  measure it.
- Workers and the MCP server point `--embedder-url` at spark. The local
  loopback embedder stays as the fallback when spark is unreachable, so an
  outage on spark degrades rather than stops ingest and search.
- Inventory: a new `embedder` group holding spark. Do not use `inference`:
  that group drives the worker, watch and dft plays, so joining it would put
  science lanes on spark. 19a targets `embedder`.
- The embedder watchdog (`41-precis-embedder-watchdog.yml`) covers the LAN
  service.
- Query latency on the MCP server's path (p50 ~2 s / p95 ~5 s today) is
  measured before and after. A LAN hop must not make search slower.

## Explicitly NOT in scope

- Changing the embedding model or dimension (that would be a re-embed of
  the corpus).
- The N-client load test (`embedder-capacity-ownership.md`, which this item
  feeds).

## Acceptance criteria

- With spark up, `llm_call_log`-adjacent embed timings (or the embedder's own
  metrics) show the fleet's embeds served from spark, and melchior's local
  embedder idle.
- With spark's service stopped, ingest and search keep working through the
  loopback fallback, with a warning, not an error.
- `tests/test_deploy_tree_no_secrets.py` green (group names only).

## Target + blast radius

`deploy/playbooks/19a-precis-embedder.yml` · inventory overlay (new
`embedder` group) · worker/MCP `--embedder-url` wiring · the embedder
client's fallback path. Deploy-role change: goes to the orchestrator as a
branch.

## Open questions / decisions log

- Blocked on spark host prep (`serving-programme-followups.md` item 2) and
  on the `/mnt/cluster` NFS hang (review item local-compute-6) only as far
  as spark's `nfs_clients` membership goes. The embedder itself does not
  need the share.
