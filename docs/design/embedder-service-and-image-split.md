# Embedder-as-service + image split

Status: **proposed** (plan-first artifact; no code landed yet)
Author: (fill in)
Date: 2026-06-06

## Problem

`bge-m3` weights (~2 GB resident, 7–30 s cold load) are loaded
*independently in every process*: `precis serve`, every `precis worker`,
and every ingest subprocess. Three consequences:

1. **Wasted RAM + duplicated warmup.** N processes hold N copies of the
   same weights. The `_warm_embedder_background` thread in
   `src/precis/server.py:484-518` exists *only* because the weights are
   in-process and the first search would otherwise blow past the MCP
   call budget while they load.
2. **Bloated, mis-scoped images.** The runtime image bakes *both* the
   bge-m3 HF cache and the Marker/surya datalab cache (~3.8 GB total;
   `docker/Dockerfile:217-223`) into every container — but `serve`
   never imports Marker, and once the embedder is remote `serve` and
   `worker` don't need `sentence-transformers`/`torch` at all.
3. **Implicit model-version contract.** Each process trusts that its
   locally-loaded model matches the `embedders` table row that
   `chunk_embeddings.embedder` FKs against. Nothing checks the actual
   model identity or dim at the boundary.

The fix: **one always-warm embedder service** behind the existing
`Embedder` Protocol, reached by a URL. Callers are unchanged. The
serve/worker images shed `torch`; only the embedder service and the
ingest image carry heavy model deps.

## Hardware reality that shapes packaging

- **OrbStack / Docker-on-Mac cannot pass through Metal/MPS.** A
  containerized embedder on Apple Silicon runs CPU-only (slow). To use
  MPS the embedder must run as a **native macOS process**, not in a
  container.
- **CUDA passthrough works in Linux Docker.** On a Linux GPU host the
  embedder *can* be a container.

Therefore the embedder has **two packaging forms, one codebase**
(`precis serve-embeddings`): native launchd service on macOS, CUDA
container on Linux. The wire contract is identical. This is an accepted,
hardware-forced deviation from "everything is a container" — captured in
its ADR.

## Decisions (settled in discussion)

- **Break out behind the `Embedder` Protocol.** `BgeM3Embedder`
  (in-process), `MockEmbedder` (tests), and a new `RemoteEmbedder`
  (HTTP client) all satisfy `src/precis/embedder.py:21-33`. The service
  wraps `BgeM3Embedder`, so the encode logic + truncation guard
  (`_BGE_M3_MAX_CHARS`) + registry key stay single-sourced. No caller
  changes.
- **The embedder never touches Postgres.** It is a pure `text → vector`
  function; the *worker* writes vectors to PG (`workers/embed.py:99-128`).
  So "co-locate with Postgres" is a red herring — co-locate with the
  **busiest caller**, which is the bulk embed worker, which in turn
  wants to be near PG.
- **Placement is a URL, not a hard-coded topology.** `PRECIS_EMBEDDER_URL`
  selects the endpoint(s). All-local (laptop) and server-hosted are the
  same code.
- **macOS embedder = native launchd service** (MPS). Linux GPU embedder
  = CUDA container. Single-platform per target; the earlier
  "multiplatform container" framing is dropped for the embedder.
- **Metal needs an Aqua session.** A bare `LaunchDaemon` (no login
  session) is unreliable for Metal/MPS. Use a **`LaunchAgent` + enabled
  auto-login** on the headless metal box so an Aqua session exists at
  boot; `KeepAlive=true` restarts on crash. This is the "comes up at
  boot" answer.
- **Client falls back across an ordered endpoint list.**
  `PRECIS_EMBEDDER_URL=http://127.0.0.1:8181,http://pg-metal.local:8181`
  — prefer local, fall back to the forwarded metal instance.
  Per-endpoint health + circuit-breaker; **exponential backoff** on
  retry (confirmed wanted).
- **No remote→in-process fallback.** serve/worker images won't ship
  `torch`, so they *can't* fall back to a local model. If every
  endpoint is down the call fails fast with a clear error; the search
  path times out well under the MCP budget.
- **Monorepo; no separate pip.** The wire schema is a module inside
  `precis` imported by both `RemoteEmbedder` and the service. Since the
  service *is* `precis serve-embeddings`, every form already has
  `precis` installed.
- **Remove `sentence-transformers`/`torch` from the serve + worker
  images.** They survive only in the embedder service (native venv or
  CUDA image) and transitively in the ingest image via `marker`. The
  `models` Docker stage stops baking bge-m3 for serve/worker.
- **Three runtime images: `serve` (tiny), `worker` (medium), `ingest`
  (heavy).** The worker is the fat one of the two backends and absorbs
  the LLM tooling; ingest is split out because Marker is the single
  biggest dep and only `precis watch` needs it. Independent queues
  (below) make this clean.

## What exists today (grounding)

- `make_embedder(name, *, dim)` factory — `src/precis/embedder.py:212`.
  `"mock" | "bge-m3"` today.
- `build_runtime` calls
  `make_embedder(config.embedder, dim=store.embedding_dim())` —
  `src/precis/runtime.py:1215`. Every handler/worker takes an
  `Embedder`; nothing reaches for a concrete class.
- `PrecisConfig.embedder: Literal["mock","bge-m3"]` —
  `src/precis/config.py:14,31`. Env `PRECIS_EMBEDDER`.
- `EmbedHandler.write_ok` writes `(chunk_id, embedder, vector)` with
  `embedder = embedder.model`, FK against the `embedders` table —
  `src/precis/workers/embed.py:99-128`.
- Workers that need an embedder: `embed`, `chunk_keywords`,
  `tag_embeddings`. Workers that **don't**: `chase`, `job_claude_inproc`,
  `fetch_oa`. (`cli/worker.py` wires them as ref-passes.)
- Marker/surya is imported **only** in `src/precis/ingest/marker.py`,
  lazily inside functions; `serve` never loads it. Ingest does *not*
  embed inline — embeddings are populated lazily by the embed worker
  (AGENTS.md ingest guarantees).
- Dockerfile stages: `deps → models → builder`, `system-base →
  {runtime, dev-system → dev-venv → dev}`. `models` bakes HF (bge-m3) +
  datalab (marker); `runtime` COPYs both (`docker/Dockerfile:217-223`).
- `_warm_embedder_background` background-thread warmup —
  `src/precis/server.py:484-518` (deleted by this work).

## The embedder service — `precis serve-embeddings`

A tiny HTTP service (FastAPI/uvicorn or stdlib) wrapping `BgeM3Embedder`.

### Wire API (shared module `precis/embedder_wire.py`)

```
POST /embed
  req:  {"texts": ["..."], "normalize": true}
  resp: {"model": "bge-m3", "dim": 1024, "vectors": [[...], ...]}

GET /model    -> {"model": "bge-m3", "dim": 1024, "revision": "<hf-sha>"}
GET /healthz  -> 200 once the process is up
GET /readyz   -> 200 once weights are loaded (mmap warm)
GET /metrics  -> inflight, queue_depth, p99_latency_ms, rejected_total,
                 batch_size histogram
```

- Request/response dataclasses live in `embedder_wire.py`, imported by
  **both** the service and `RemoteEmbedder`. One source of truth.
- JSON to start; msgpack for the float payload is a later optimization
  (a 32-chunk batch ≈ 128 KB of float32).

### Backpressure + capacity visibility

- Bounded concurrency via a semaphore sized to the device. Over
  capacity → `429` + `Retry-After`; never an unbounded queue.
- "Over capacity" = sustained `queue_depth > 0` or `p99 > threshold`.
  Alert on those, not on CPU%.
- Pin the HF model **revision (commit SHA)** in the image/venv so
  "always warm" also means "always the same weights".

### Keeping it enclosed without a container (macOS)

- Dedicated **uv-managed venv** at a fixed path, pinned by `uv.lock` →
  reproducible install without Docker.
- Runs as a **dedicated, non-admin user**; HF cache pinned via
  `HF_HOME`.
- **Bind `127.0.0.1` only.** Cross-host access is via an explicit
  tunnel/forward, optionally guarded by `PRECIS_EMBEDDER_TOKEN`.
- `LaunchAgent` plist (`~/Library/LaunchAgents/com.precis.embedder.plist`)
  with `RunAtLoad=true`, `KeepAlive=true`; auto-login enables the Aqua
  session MPS needs. `SoftResourceLimits` caps memory.
- Lifecycle: `launchctl bootstrap/bootout`; logs to a fixed path. A
  `scripts/embedder/` install helper writes the plist and venv.

## The client — `RemoteEmbedder(Embedder)`

- Implements `embed`, `embed_one`, `dim`, `model` — drop-in for
  `make_embedder`. Adds a `"remote"` branch to the factory; selected by
  `PRECIS_EMBEDDER=remote`.
- **No `torch` import** anywhere in this path.
- Ordered endpoint list from `PRECIS_EMBEDDER_URL` (comma-separated).
  Try the first healthy endpoint; per-endpoint circuit-breaker;
  **exponential backoff** with jitter; short overall deadline so the
  search path fails fast.
- **Startup contract check** (see below) on first use.

## Image split

| Image    | Command           | Heavy deps                | Notes |
|----------|-------------------|---------------------------|-------|
| `serve`  | `precis serve`    | none (no torch/marker)    | tiny; talks to embedder URL |
| `worker` | `precis worker`   | node + claude-code        | medium; LLM passes (`chase`, `job_claude_inproc`); embeds via `RemoteEmbedder` |
| `ingest` | `precis watch`    | marker/surya (`torch`)    | heavy; only PDF extraction |
| embedder | `precis serve-embeddings` | sentence-transformers/torch | **native launchd on macOS**, CUDA container on Linux |

`serve` and `worker` shed `torch` entirely. `torch` survives only in
the embedder and (via marker) in `ingest`. Open option: split `ingest`
back out of `worker`, or fold a CPU-only worker into `serve`'s base —
deferred; the table above is the v1 target.

## Independent queues (confirmed)

Promote `precis worker --only <pass>` from a flag to first-class
deployment units, scheduled by resource class:

- **light** (`embed`, `chunk_keywords`, `tag_embeddings`, `fetch_oa`) —
  remote-embedder client + PG; tiny.
- **llm** (`chase --with-llm`, `job_claude_inproc`) — node/claude-code;
  multi-minute subprocesses; must not starve the light queue.

`job_claude_inproc` (spawns Claude Code subprocesses from a queue
worker) gets its own isolation boundary — tracked in its own ADR.

## DRY

- **One Dockerfile, more targets** off the shared `deps`/`system-base`
  bases (the `runtime`/`dev` split already proves the pattern).
- **One package everywhere.** serve/worker/ingest/embedder are the same
  `precis` install with different `CMD` + extras.
- **The `Embedder` Protocol is the seam**; the encode logic exists once
  in `BgeM3Embedder`.
- **`embedder_wire.py` shared** by client + service. Monorepo, no
  separate PyPI artifact.
- **Extras stay single-sourced** in `pyproject.toml`; the image
  difference is which extra is installed.

## Plumbing correctness contract

1. **Model/dim/identity check at the boundary.** `RemoteEmbedder` calls
   `GET /model` on first use and asserts `dim == store.embedding_dim()`
   **and** `model == the embedders-table row`. The FK already guards the
   name on *write*; this catches a wrong/upgraded model *before* the
   first encode — the scariest silent-corruption mode.
2. **Health vs readiness.** `/healthz` + `/readyz`; compose uses
   `depends_on: condition: service_healthy`. Deletes
   `_warm_embedder_background`.
3. **Contract test in CI.** Boot the service (or a fake honoring
   `embedder_wire`) and assert `RemoteEmbedder` vectors match in-process
   `BgeM3Embedder` **within tolerance** (multiplatform arm64/amd64 float
   diffs ~1e-4 are expected; cosine ranking is robust, exact-equality is
   not). Also assert `RemoteEmbedder` satisfies the `Embedder` Protocol.
4. **Backpressure + fast failure** (service 429; client backoff +
   breaker + deadline).
5. **Capacity metrics** (`/metrics`) with alert thresholds.
6. **Determinism pin** (HF revision SHA) + a single canonical embedding
   deployment for the corpus.

## Config knobs

- `PRECIS_EMBEDDER` — `mock | bge-m3 | remote` (adds `remote`).
- `PRECIS_EMBEDDER_URL` — ordered, comma-separated endpoint list.
- `PRECIS_EMBEDDER_TIMEOUT` — per-call deadline.
- `PRECIS_EMBEDDER_TOKEN` — optional bearer for forwarded endpoints.

## Test plan

- `RemoteEmbedder`: Protocol conformance; endpoint fallback ordering;
  exponential-backoff + circuit-breaker (mocked transport); model/dim
  assertion fails loud on mismatch; fast-fail when all endpoints down.
- Service: `/embed` shape; `normalize` honored; `/model` + `/readyz`;
  429 under semaphore saturation; `len(texts)==len(vectors)`.
- Contract test (tolerance-based) service vs in-process.
- Factory: `make_embedder("remote")` wires `RemoteEmbedder` from env.
- No-regression: existing `MockEmbedder`/`BgeM3Embedder` suites
  unchanged.

## Definition of done (per AGENTS.md)

- Plan reviewed (this doc).
- ADRs in `docs/decisions/`: (a) embedder-as-service behind the
  Protocol + native-vs-container packaging tradeoff (MPS/OrbStack
  rationale); (b) serve/worker/ingest image split + dropping `torch`
  from serve/worker; (c) independent worker queues by resource class +
  `job_claude_inproc` isolation.
- `RemoteEmbedder` + `embedder_wire` + `precis serve-embeddings` land;
  `make_embedder` gains `"remote"`; `_warm_embedder_background` removed.
- launchd plist + install helper under `scripts/embedder/`; CUDA
  Dockerfile target for Linux.
- Image split implemented; serve/worker images build without `torch`.
- Full check green (`ruff check`, `ruff format --check`, `mypy`,
  `pytest`); version bump + `CHANGELOG` entry; README + `--help` for
  the new subcommand.

## Suggested sequencing

1. `embedder_wire` + `RemoteEmbedder` + `make_embedder("remote")` +
   config knobs (in-process service-free; tested against a fake).
2. `precis serve-embeddings` wrapping `BgeM3Embedder`; `/healthz`,
   `/readyz`, `/model`, `/embed`.
3. Boundary contract check + CI contract test; delete
   `_warm_embedder_background`.
4. Backpressure + `/metrics`.
5. launchd packaging (macOS) + CUDA Dockerfile target (Linux); install
   helper.
6. Image split: tiny serve, worker without torch, ingest with marker.
7. Independent queues as deployment units; `job_claude_inproc`
   isolation (own ADR).

## Open questions for the reviewer

1. Wire format: stay JSON, or msgpack the float payload from day one?
2. Does the laptop run its *own* local embedder (prefer-local, fall
   back to metal) — 2× weights in RAM — or always forward to the metal
   box?
3. Auth on forwarded endpoints: bearer token now, or rely on the tunnel
   (tailscale/cloudflared) for identity?
4. Keep `ingest` as its own image, or fold it into a "fat backend" that
   also runs the worker queues?
