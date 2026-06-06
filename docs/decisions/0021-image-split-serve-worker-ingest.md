# ADR 0021 — Split the runtime image into `serve` / `worker` / `ingest`

- **Status**: **accepted** (2026-06-06) — `serve` / `worker` / `ingest`
  / `embedder` targets landed in `docker/Dockerfile`; `sentence-
  transformers` split into the `embed` extra so serve/worker are
  torch-free; `scripts/build-all` builds all four. Tracks
  `docs/design/embedder-service-and-image-split.md`.
- **Deciders**: Reto + agent
- **Extends**: [ADR 0004 — multi-stage Dockerfile](./0004-multi-stage-dockerfile.md),
  [ADR 0009 — Dockerfile relocation](./0009-dockerfile-relocation-container-first.md),
  [ADR 0012 — bake models into runtime image](./0012-bake-models-into-runtime-image.md).
- **Depends on**: [ADR 0020 — embedder as a service](./0020-embedder-as-service.md)
  (once the embedder is remote, serve/worker no longer need `torch`).

## Context

The single `runtime` image bakes both the bge-m3 HF cache and the
Marker/surya datalab cache (~3.8 GB; `docker/Dockerfile:217-223`) into
every container. But `serve` never imports Marker
(`ingest/marker.py` is the only importer, lazily), and once the embedder
is remote (ADR 0020) neither `serve` nor `worker` needs
`sentence-transformers`/`torch` at all. The current image is heavy and
mis-scoped: a tiny MCP server carries a 3.8 GB model payload it never
loads.

## Decision

Three runtime images off the shared base stages:

| Image    | Command           | Heavy deps              |
|----------|-------------------|-------------------------|
| `serve`  | `precis serve`    | none (no torch/marker)  |
| `worker` | `precis worker`   | node + claude-code      |
| `ingest` | `precis watch`    | marker/surya (`torch`)  |

`serve` and `worker` build **without `torch`** (they embed via
`RemoteEmbedder`). `torch` survives only in the embedder service (ADR
0020) and, via `marker`, in `ingest`. `ingest` stays its own image
because Marker is the single biggest dependency and only `precis watch`
needs it. Extras stay single-sourced in `pyproject.toml`; the `models`
Docker stage stops baking bge-m3 for the serve/worker targets.

## Alternatives considered

- **Keep one fat runtime image.** Rejected: mis-scoped payload, large
  attack surface on the server, slow pulls.
- **Two images (serve + fat backend that runs both worker and ingest).**
  Considered; rejected for v1 because Marker's footprint and the desire
  to schedule ingest (GPU-relevant, bursty) separately from the LLM/IO
  worker queues argue for splitting ingest out. Re-foldable later if
  operational simplicity wins.

## Consequences

- **Positive**: tiny serve image; worker without torch; ingest isolates
  the Marker leak surface (ADR 0015) in the one place that needs it.
- **Negative**: three images to build/publish instead of one; CI matrix
  grows.
- **Neutral**: enables independent queue deployment (ADR 0022).

## See also

- `docs/design/embedder-service-and-image-split.md`
- [ADR 0020 — embedder as a service](./0020-embedder-as-service.md)
- [ADR 0022 — independent worker queues](./0022-independent-worker-queues.md)
