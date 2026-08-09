# Embedder-as-service + image split

Shipped portion: see ADR 0020 and the
`src/precis/embedder_service.py` / `embedder_wire.py` module
docstrings; full plan in git history. Live: `precis serve-embeddings`
(native launchd + MPS on Macs, CUDA container form on Linux),
`RemoteEmbedder` client with exponential backoff, the shared
monorepo wire schema, torch removed from the serve/worker images
(the `ingest` image keeps Marker/torch), idle-unload residency
(`PRECIS_EMBEDDER_IDLE_S`, cluster-scheduling §F). Decided: every
node runs its own local embedder; cross-node forwarding is fallback
only.

## Open scope

- **`chunk_keywords` embedder-load follow-up** (not v1-blocking,
  revisit with the service live): the pass embeds ~40 candidate
  phrases per chunk — heavier than the embed pass itself. Cross-chunk
  batch coalescing + a phrase→vector cache (phrases repeat across
  chunks/papers) would cut remote round-trips.
- **Wire format** — stay JSON, or msgpack the float payload? Decide
  if payload size ever shows up in profiles.
- **Auth on forwarded endpoints** — bearer token vs tunnel identity.
  Moot in the all-local topology; only matters if a node ever borrows
  another's embedder.
- **Image split refinement (deferred):** split `ingest` back out of
  `worker`, or fold a CPU-only worker into `serve`'s base — the
  shipped serve/worker/ingest/embedder table is the v1 target.
