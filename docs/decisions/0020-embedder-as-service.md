# ADR 0020 — Embedder as a network service (behind the `Embedder` Protocol)

- **Status**: **proposed** (2026-06-06) — plan-first stub; no code
  landed yet. Tracks `docs/design/embedder-service-and-image-split.md`.
- **Deciders**: Reto + agent
- **Extends**: [ADR 0012 — Bake models into runtime image](./0012-bake-models-into-runtime-image.md)
  and [ADR 0019 — premodels build context](./0019-premodels-build-context.md).
  Those keep weights *inside* each image; this ADR moves the embedder
  weights into one shared always-warm service instead.

## Context

`bge-m3` (~2 GB resident, 7–30 s cold load) is loaded independently in
every process — `serve`, every `worker`, every ingest subprocess. This
duplicates RAM, forces the `_warm_embedder_background` warmup hack in
`src/precis/server.py:484-518` (it exists only because weights are
in-process and the first search would blow past the MCP call budget),
and leaves the model-version contract implicit (each process trusts its
local weights match the `embedders` table row that
`chunk_embeddings.embedder` FKs against).

The `Embedder` Protocol (`src/precis/embedder.py:21-33`) is already the
seam: every handler/worker takes an `Embedder`; nothing reaches for a
concrete class.

## Decision

Introduce a `RemoteEmbedder(Embedder)` HTTP client and a
`precis serve-embeddings` service wrapping `BgeM3Embedder`. Callers are
unchanged — `make_embedder` gains a `"remote"` branch selected by
`PRECIS_EMBEDDER=remote` + `PRECIS_EMBEDDER_URL`. The encode logic,
truncation guard, and registry key stay single-sourced in
`BgeM3Embedder`; a shared `precis/embedder_wire.py` module defines the
request/response schema for both client and service (monorepo, no
separate package).

**Packaging is hardware-forced and dual.** OrbStack/Docker-on-Mac
cannot pass through Metal/MPS, so on macOS the embedder runs as a
**native launchd service** (uv venv, `LaunchAgent` + auto-login for the
Aqua session MPS needs). On Linux it runs as a **CUDA container**. One
codebase, two packaging forms, identical wire contract — an accepted
deviation from "everything is a container".

**Correctness contract.** The service exposes `/model` (`{name, dim,
revision}`), `/healthz`, `/readyz`, `/metrics`. `RemoteEmbedder`
asserts `dim == store.embedding_dim()` and `name ==` the embedders-table
row before its first encode (catching a wrong/upgraded model that would
otherwise silently corrupt vectors). Backpressure via a bounded
semaphore (`429 + Retry-After`); client uses exponential backoff + a
per-endpoint circuit-breaker and a short deadline so the search path
fails fast. The model revision (HF commit SHA) is pinned.

## Alternatives considered

- **Keep in-process (status quo).** Rejected: duplicated RAM + the
  warmup hack + implicit version contract are exactly what this fixes.
- **Co-locate the embedder with Postgres.** Rejected as a *driver*: the
  embedder never touches PG (`workers/embed.py:99-128` writes the
  vectors). Co-locate with the busiest *caller* (the bulk embed worker),
  which itself wants to be near PG.
- **Multiplatform single container.** Rejected for the embedder: MPS is
  unreachable from a Mac container, so the macOS form must be native.
- **Remote→in-process fallback.** Rejected: serve/worker images won't
  ship `torch` (see ADR 0021), so they can't fall back to a local model.
  Fail fast instead.

## Consequences

- **Positive**: one warm copy; warmup hack deleted; explicit, verified
  model contract; serve/worker shed `torch`.
- **Negative**: a new network failure mode + service to operate;
  multiplatform float diffs (~1e-4) mean the contract test must use
  tolerance, not exact equality.
- **Neutral**: placement becomes a `PRECIS_EMBEDDER_URL` knob; the
  all-local fleet topology (every node runs its own embedder) is the
  default.

## See also

- `docs/design/embedder-service-and-image-split.md`
- [ADR 0021 — serve/worker/ingest image split](./0021-image-split-serve-worker-ingest.md)
