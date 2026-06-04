# Bake Marker + bge-m3 weights into the runtime image

**Status**: implemented (ADR 0012, 2026-05-23); cold-build deadlock
mitigation added (ADR 0019, 2026-06-04)
**Owner**: `docker/Dockerfile`, `docker/bake-models.py`, `infrastructure/compose.yaml`
**Predecessors**:
- ADR 0004 — multi-stage Dockerfile (`docs/decisions/0004-multi-stage-dockerfile.md`)
- ADR 0009 — Dockerfile relocation, container-first (`docs/decisions/0009-dockerfile-relocation-container-first.md`)
- ADR 0011 — Claude Code in dev image + UID/GID alignment (`docs/decisions/0011-claude-in-dev-image.md`)
**Successors**:
- ADR 0019 — Premodels build context for cold-build avoidance
  (`docs/decisions/0019-premodels-build-context.md`)

## Problem

`precis_add(PdfInput(...))` and the embed worker both depend on model
weights that are downloaded lazily from HuggingFace Hub on first use:

- **Marker layout / OCR stack** — `create_model_dict()`
  (`src/precis/ingest/marker.py:283-286`) pulls the surya detect /
  recognise / layout / order / table-rec models + texify on the first
  PDF through the pipeline. ~1.5 GB on disk.
- **BGE-M3 embedder** — `SentenceTransformer('BAAI/bge-m3')`
  (`src/precis/embedder.py:160-167`) pulls ~2.3 GB on the first
  `embed()` call.

In the current Dockerfile layout the runtime image (`precis-mcp:latest`,
target=`runtime`) ships only the Python venv. The model weights arrive
at runtime, into `/home/precis/.cache/huggingface/`, and are persisted
across container restarts via the named volume
`precis-cache:/home/precis/.cache` mounted by `precis-watch` in
`infrastructure/compose.yaml`.

Two pains follow from that:

1. **Every fresh host pays the download.** New machine, wiped volume,
   CI runner, throwaway container — first PDF blocks for ~5 minutes on
   ~3.8 GB of HF traffic before any ingest progress.
2. **The cache volume is load-bearing for production behaviour.**
   `precis-watch` is meant to be a self-contained service; relying on
   a host-managed volume to hold its primary working data is a
   surprising coupling and a footgun for `docker compose down -v`.

Disk space is not a constraint on the target hosts (the operator's
workstation has 8 TB; production deployments are still notional). The
container's RAM behaviour does not change either way — models are
mmap'd lazily on first use regardless of whether they came from a
freshly downloaded cache or a baked-in layer.

## Goal

A `docker build --target runtime` produces an image that can run
`precis add /inbox/<some>.pdf` end-to-end with no network access to
`huggingface.co` after the build completes. Same for `precis watch`
on a fresh host with no `precis-cache` volume.

## Non-goals

- **No new model selection logic.** The bake covers exactly the two
  identifiers wired into `src/precis/`: `BAAI/bge-m3` for embeddings
  and whatever `marker.models.create_model_dict()` resolves to (the
  surya stack + texify, as of marker-pdf 1.10.2). If a future
  embedder or layout pipeline lands, the bake stage gets a parallel
  line — not a config knob.
- **No image-size optimisation.** ONNX export, FP16 quantisation,
  and selective layer pruning are all viable later; out of scope here.
  Disk space is plentiful.
- **No change to `precis serve` startup.** The MCP server already keeps
  the embedder lazy (see `src/precis/embedder.py:113-134`) so the
  handshake budget isn't affected.

## Design

### 1. New `models` stage between `builder` and `runtime`

Insert a fourth stage in `docker/Dockerfile`:

```dockerfile
FROM builder AS models

ENV HF_HOME=/opt/precis/models/hf \
    MODEL_CACHE_DIR=/opt/precis/models/datalab/models

RUN mkdir -p "${HF_HOME}" "${MODEL_CACHE_DIR}" && \
    /opt/venv/bin/python -c \
        "from marker.models import create_model_dict; create_model_dict()" && \
    /opt/venv/bin/python -c \
        "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"
```

Two caches matter:

- **`HF_HOME`** — read by `huggingface_hub` and (transitively)
  `sentence-transformers`. Catches `BAAI/bge-m3` (~4.3 GB on disk for
  the safetensors + ColBERT/sparse heads).
- **`MODEL_CACHE_DIR`** — read by `surya`'s `pydantic-settings`
  config (`surya/settings.py: MODEL_CACHE_DIR`). Catches the Marker
  surya stack: text detection / recognition, layout, table-rec,
  ocr-error-detection (~1 GB total, pulled from
  `https://models.datalab.to/`, **not** from HuggingFace). Without
  this var, `marker.models.create_model_dict()` writes those models
  to `~/.cache/datalab/models/` which would end up under `/root/` in
  the build stage and never reach the runtime image.

Both model loads run as **root** in the builder image; the runtime
stage then `COPY --chown=precis:precis`'s the resulting tree so the
unprivileged `precis` user can read it.

### 2. Runtime stage: COPY the cache + set `HF_HOME`

Two edits in the `runtime` stage:

1. **ENV block** (existing `ENV PYTHONUNBUFFERED=1 …` near
   `docker/Dockerfile:56`): append both
   `HF_HOME=/opt/precis/models/hf` and
   `MODEL_CACHE_DIR=/opt/precis/models/datalab/models`.
2. **After** the `useradd precis` block (so `--chown=precis:precis`
   can resolve the user), add:

   ```dockerfile
   COPY --from=models --chown=precis:precis /opt/precis/models /opt/precis/models
   ```

   The COPY must follow `useradd` — `--chown=name:name` resolves at
   build time, not deferred.

`dev` inherits from `runtime` (`FROM runtime AS dev` at
`docker/Dockerfile:101`) — no change needed there. The dev shell
gets the same baked-in models for free.

### 3. Filesystem layout

```
/opt/
└── precis/
    └── models/                          # owned by precis:precis
        ├── hf/                          # HF_HOME
        │   └── hub/
        │       └── models--BAAI--bge-m3/     # ~4.3 GB
        └── datalab/                     # MODEL_CACHE_DIR (parent)
            └── models/
                ├── text_detection/<rev>/
                ├── text_recognition/<rev>/
                ├── layout/<rev>/
                ├── table_recognition/<rev>/
                └── ocr_error_detection/<rev>/
```

`/opt/precis/` is the standard FHS slot for vendored, app-shipped
artifacts and is the same convention the broader ML container
ecosystem uses (NVIDIA NGC images, HuggingFace's `text-generation-
inference`, datalab's own published images). The path
`/home/precis/.cache/...` is deliberately *not* used so the named
volume mount `precis-cache:/home/precis/.cache` in compose
(currently mounted by `precis-watch`) cannot mask the baked-in
weights. The volume becomes optional, kept only for transient caches
(pip / uv / ruff) the user may still want persistent.

### 4. Compose adjustment

Drop the `precis-cache:/home/precis/.cache` line from the
`precis-watch` service block in `infrastructure/compose.yaml`. The
volume definition under `volumes:` at the bottom of the file can
stay (it's still referenced by `precis-dev-cache`) but the now-unused
`precis-cache:` volume entry should be removed in the same edit.

```diff
   volumes:
     - ${HOME}/.secrets/pw:/secrets:ro
     - ${HOME}/work/new_papers:/inbox:rw
     - ${HOME}/work/corpus:/data/corpus:rw
-    # Persistent cache for the bge-m3 model + Marker layout models
-    # (~2 GB on first run; subsequent starts reuse this volume).
-    - precis-cache:/home/precis/.cache
```

```diff
 volumes:
-  precis-cache:
-    driver: local
   precis-dev-cache:
     driver: local
```

The named volume `precis-infra_precis-cache` may already exist on
hosts where the old service ran. A follow-up `docker volume rm
precis-infra_precis-cache` cleans it up; called out in the CHANGELOG.

### 5. Cache freshness

`marker-pdf` and `sentence-transformers` pin their model loads via
the model identifier string only — there's no per-call version
selector that would force a refresh once the cache exists. That
matches what we want: as long as the IDs in the Dockerfile and the
application code agree, the runtime never reaches over the network
for model files.

If a future marker-pdf release changes the surya version it loads
under the same ID, the cache will be transparently usable (HF Hub
revisions are pinned by content addressing, so the new revision
becomes a new directory under `models--…/snapshots/`; old ones stay
addressable). If a model is *renamed*, the bake stage's RUN line
needs an update — that's a code change with an obvious failure
mode (the import will reach for HF at runtime, fail without
network, and surface a clear error).

## Why not …

### …bake into the `builder` stage and skip the new stage?

`builder` installs `precis-mcp[all]` and copies the application
source. Touching any `src/precis/**` file invalidates the layer
*after* the model download line, which is fine, but moving the
model download *into* `builder` would put the 3 GB download on the
same cache key as the source — every iteration over `src/` would
re-pay the 3 GB on cold caches. A dedicated `models` stage keeps
the model layer keyed only on the model identifier line plus the
upstream venv.

### …bake into the `runtime` stage directly?

Same objection on a smaller scale: the `runtime` stage has the
`useradd` + entrypoint copy, which we touch occasionally. We don't
want a 3 GB download tied to that cache key.

### …keep the warmup outside the image, in a `precis warmup`
###    subcommand the user runs once?

Option C in the chat thread. Image stays at 6.4 GB; every new host
or wiped volume pays the download cost manually. Loses the
"always there" property the user explicitly wants.

### …ONNX-export bge-m3 to ~half the size?

Defer. Disk is cheap on the target hosts and the export-and-verify
loop is its own slice. If we ever ship `precis-mcp` to PyPI consumers
who pull the Docker image (currently aspirational), revisit.

### …bind-mount `/opt/precis/models` from the host instead of
###    copying it into the image?

Loses portability — the image becomes useless without a host that
has the exact same model layout pre-populated. The whole point of
the bake is to make the image self-contained.

## Consequences

### Positive

- **Fresh hosts work offline.** `docker pull precis-mcp:latest && docker
  run` produces a working ingest pipeline with zero HF traffic.
- **`precis-watch` is self-contained.** Stopping `precis-watch`,
  removing its named volume, and starting it again on a different
  host produces identical behaviour.
- **First-PDF latency drops from ~5 min to ~10 s.** The HF download
  is replaced by an mmap of a local file.
- **Layer cache is honest.** Iterations over `src/precis/` no longer
  risk wiping a multi-GB model cache.

### Negative

- **Image size grows ~3 GB** (6.43 GB → ~9.5 GB). Acceptable given
  the user's storage profile; revisited if/when we publish to a
  public registry.
- **Build time grows by 3–5 minutes on a cold cache** (one-off; warm
  rebuilds skip the layer entirely).
- **Pin drift potential.** If a future marker-pdf upgrade quietly
  changes the model IDs the bake stage references, we won't notice
  at build time — only at runtime. Mitigation: the failure mode is
  loud (HF call without network) and the fix is one-line.

### Neutral

- **No change to RAM behaviour.** Lazy loads stay lazy. Steady-state
  resident set after first PDF: ~3.8 GB (bge-m3 + surya) + ~1–2 GB
  transient per page (Marker working memory). Matches today.
- **No change to the application code.** This is purely a packaging
  slice.

## Migration

1. Land `docker/Dockerfile` edits (new `models` stage + runtime
   COPY/ENV).
2. Land `infrastructure/compose.yaml` edits (drop the
   `/home/precis/.cache` mount + volume definition).
3. `docker compose -f infrastructure/compose.yaml build precis-watch`
   — expect a 3–5 min download on first build, ~30 s on warm
   rebuilds.
4. Verify with `docker compose run --rm --no-deps precis-cli bash -c \
     'ls -la /opt/precis/models/hf/hub/'` — should list six or seven
   `models--…` directories totalling ~3.8 GB.
5. Negative test: `docker compose run --rm --no-deps precis-cli env \
     HF_HUB_OFFLINE=1 precis add /data/notes/new_papers/<smallest>.pdf`
   — must complete without reaching HF.
6. Bump `version = "X.Y.Z"` in `pyproject.toml` and add a
   `CHANGELOG.md` entry; supersede the
   `precis-cache:/home/precis/.cache` bullet from ADR 0011's
   "Negative" section in a new ADR if we file one.
7. On any host where `precis-infra_precis-cache` already exists:
   `docker volume rm precis-infra_precis-cache` (optional cleanup).

## Open questions (non-blocking)

- **ADR or no ADR?** This change is dev-ops only — no application
  surface change, no schema change, no API contract change. The
  precedent in `docs/decisions/` is to ADR substantive trade-offs;
  the trade-off here (image size vs. self-containment) is real but
  small. Recommend filing ADR 0012 once the implementation lands so
  the decision is searchable; not blocking the merge.
- **Pin the `marker-pdf` minimum higher to match the model IDs we
  bake?** Today the dep is `marker-pdf>=1.0`; the bake uses whatever
  `create_model_dict()` resolves at build time. Probably fine until
  a marker upgrade breaks the contract; tackle then.
- **Share the bake stage with `coding-base`?** Out of scope; the
  coding-base image is a generic Python shell with no ML deps.

---

## Addendum: cold-build deadlock + premodels seed (2026-06-04)

### Problem

The bake stage as designed above worked for ~10 days, then started
hanging indefinitely on the bge-m3 fetch. Every cold build reached
`Fetching 22 files: 18% (4/22)` and stopped emitting progress. Wall-
clock build elapsed grew to 11+ hours with no further log output, no
process exit, no error.

Investigation:

- `HF_HUB_DOWNLOAD_TIMEOUT=120` did not help — the deadlock is not a
  slow socket. `lsof -i :443` showed zero active connections from the
  buildkit container.
- `/proc/<pid>/task/*/stack` for every one of the Python interpreter's
  19 threads showed `futex_wait`. The process held no socket fds in
  read state.
- Reproduces on Apple Silicon hosts (OrbStack 1.x), across multiple
  `huggingface_hub` versions, across multiple network paths.
- The actual deadlock is inside HuggingFace's xet-bridge protocol
  (`cas-bridge.xethub.hf.co`), which `huggingface_hub` adopted as
  the default download backend at some point in 2026. Not something
  we can patch.

Meanwhile, the *exact* model weights were already present in the
previous `precis-mcp:latest` image (which had been built before
xet-bridge became the default). Re-baking them in every cold build
re-paid a download that no longer terminates.

### Resolution

Seed the models stage from a prior image via BuildKit's
`--build-context` mechanism, and make `bake-models.py` defensive
against the network path entirely. Full rationale in
[ADR 0019](../decisions/0019-premodels-build-context.md). Sketch:

#### Dockerfile

Add a default empty stage named `premodels` so the COPY resolves
without configuration:

```dockerfile
ARG PYTHON_DIGEST=…
ARG UV_VERSION=0.11.14

FROM scratch AS premodels

# … deps stage unchanged …

FROM deps AS models
ENV HF_HOME=… MODEL_CACHE_DIR=…

# Seed the model cache from a prior image. With no premodels
# build-context, this is a no-op (scratch has an empty FS).
COPY --from=premodels / /tmp/premodels-root/
RUN mkdir -p "${HF_HOME}" "${MODEL_CACHE_DIR}" && \
    if [ -d /tmp/premodels-root/opt/precis/models ]; then \
        cp -r /tmp/premodels-root/opt/precis/models/. /opt/precis/models/; \
    fi && \
    rm -rf /tmp/premodels-root

COPY docker/bake-models.py /tmp/bake-models.py
RUN /opt/venv/bin/python /tmp/bake-models.py && rm /tmp/bake-models.py
```

#### bake-models.py

Skip the bge-m3 download when the cache directory already has a
snapshot, and run the verification load with offline flags so it
cannot fall back to the network:

```python
def _bake_bge_m3() -> None:
    hf_home = pathlib.Path(os.environ.get("HF_HOME", "/opt/precis/models/hf"))
    snapshots = hf_home / "hub" / "models--BAAI--bge-m3" / "snapshots"
    if snapshots.is_dir() and any(snapshots.iterdir()):
        print(f"[bake] bge-m3 cache already populated — skipping download")
        return
    # … original snapshot_download() call for the truly-cold path …

def main() -> None:
    _patch_get_text_config()
    _patch_surya_config()
    from marker.models import create_model_dict
    create_model_dict()
    _bake_bge_m3()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from sentence_transformers import SentenceTransformer
    SentenceTransformer("BAAI/bge-m3")
```

#### infrastructure/compose.yaml

Pass the premodels context for all three build blocks:

```yaml
  precis-watch:
    build:
      context: ../projects/code/precis-mcp
      dockerfile: docker/Dockerfile
      target: runtime
      additional_contexts:
        premodels: docker-image://precis-mcp:premodels
      args:
        UID: ${UID:-1000}
        GID: ${GID:-1000}
```

(Identical block under `precis-cli` and `precis-dev`.)

#### Bootstrap and maintenance

```sh
# One-off bootstrap: tag whatever image already works as the seed.
docker tag precis-mcp:latest precis-mcp:premodels

# After any successful build that you'd like to seed from later:
docker tag precis-mcp:latest precis-mcp:premodels
```

### Measured outcome

- Cold cache with seed available: ~3 minutes (Marker datalab models
  re-download — those use a separate HTTP path that does not hit
  xet-bridge — and bge-m3 is read from the seed).
- Source-only rebuild (touch `src/precis/handlers/paper.py`): 54.6 s
  (timed). All stages CACHED including Stage 2; only the
  `precis-mcp:latest` retag fires.
- No-seed cold build (`docker buildx ... --no-cache`, no premodels
  tag): falls through to the original deadlock-prone path. The
  failure mode is a long hang followed by no progress; the build
  log explicitly says "[bake] bge-m3 cache empty — fetching from HF"
  before the deadlock so the diagnostic is unambiguous.

### Known limitations

- The seed has to come from somewhere on the first-ever build of a
  fresh host. Publishing `precis-mcp:premodels` to a registry would
  close that gap; out of scope today (only one build host).
- A future marker-pdf upgrade that changes the surya revisions
  shipped in the seed produces a stale cache — `create_model_dict()`
  detects this and downloads the newer revisions over the
  non-xet-bridge datalab path. No xet-bridge involvement, no hang.
- A future bge-m3 revision bump would surface as a cache miss in
  the seed. With `HF_HUB_OFFLINE=1` we'd get a clear `OSError` at
  the verification load rather than a deadlock. The fix is to drop
  the seed tag and accept the cold path (which works when
  xet-bridge happens to be healthy), or to pre-populate the cache
  by other means.
