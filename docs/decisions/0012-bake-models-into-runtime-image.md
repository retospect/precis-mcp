# ADR 0012 — Bake Marker + bge-m3 weights into `precis-mcp:latest`

- **Status**: accepted (2026-05-23); cold-build pathology mitigated
  by [ADR 0019](./0019-premodels-build-context.md) (2026-06-04)
- **Deciders**: Reto + agent
- **Builds on**:
  - ADR 0004 — multi-stage Dockerfile (`docs/decisions/0004-multi-stage-dockerfile.md`)
  - ADR 0009 — Dockerfile relocation, container-first
    (`docs/decisions/0009-dockerfile-relocation-container-first.md`)
  - ADR 0011 — Claude Code in dev image + UID/GID
    (`docs/decisions/0011-claude-in-dev-image.md`)
- **Followed by**:
  - ADR 0019 — Premodels build context for cold-build avoidance
    (`docs/decisions/0019-premodels-build-context.md`). The bake-
    stage *cold-cache* download became unreliable after HuggingFace
    switched to xet-bridge as the default backend; 0019 seeds the
    cache from a prior image to keep the bake idempotent.
- **Plan artefact**: `docs/design/bake-models-into-image.md`

## Context

`precis_add()` and the embed worker depend on two model caches that
are otherwise downloaded lazily on first use:

- `BAAI/bge-m3` (~4.3 GB on disk) via `sentence-transformers`,
  populating `HF_HOME`.
- The Marker surya stack (text detection / recognition / layout /
  table-rec / ocr-error-detection, ~3 GB on disk) via
  `marker.models.create_model_dict()`, populating `MODEL_CACHE_DIR`
  (surya's own pydantic-settings field, not `HF_HOME`).

Before this ADR the runtime image (`precis-mcp:latest`) shipped neither
cache. The compose service `precis-watch` mounted
`precis-cache:/home/precis/.cache` so that the ~3.8 GB of weights
downloaded on the first PDF survived restarts. Three friction points:

1. **Every fresh host pays the ~5-minute download** before the first
   PDF can be processed.
2. **The watcher is not self-contained** — its primary working data
   lives in a named volume managed outside the image, surprising for a
   service whose code lives inside the image.
3. **`docker compose down -v` silently wipes 3.8 GB of model weights**
   that have to be re-downloaded on next bring-up.

The user's target hosts have ~8 TB of free disk; image size is a
non-constraint. Runtime RAM behaviour is identical whether models live
in the image layer or in a host-managed volume — both are mmap'd
lazily on first use.

## Decision

Insert a fourth Docker stage, `models`, between `builder` and
`runtime`. The stage inherits the builder's venv, sets
`HF_HOME=/opt/precis/models/hf` and
`MODEL_CACHE_DIR=/opt/precis/models/datalab/models`, runs the two
model-load entry points (`create_model_dict()` + `SentenceTransformer
('BAAI/bge-m3')`), and produces a populated `/opt/precis/models/` tree.
The runtime stage COPYs that tree (with `--chown=precis:precis`) and
sets the same two env vars in its `ENV` block so application code finds
the baked weights at runtime.

A small companion change: the `builder` stage now also calls
`marker.util.download_font()` so the GoNoto font Marker writes into
`site-packages/static/fonts/` on first converter construction is
pre-populated in the venv before it gets COPYed to `runtime`. Without
this, the unprivileged `precis` user trips on a permission error
trying to write into the root-owned venv at runtime.

`infrastructure/compose.yaml`'s `precis-watch` service drops the
`precis-cache:/home/precis/.cache` mount. The named volume definition
under `volumes:` is removed too; the `precis-dev-cache` named volume
stays (still used by `precis-dev`).

Full design with rejected alternatives (warmup CLI subcommand;
runtime-only bake; bind-mount from host) and verification protocol in
`docs/design/bake-models-into-image.md`.

## Consequences

### Positive

- **First container start is instant.** No 5-minute HF/datalab download
  on a fresh host.
- **Watcher is self-contained.** `docker compose down -v` no longer
  costs 3.8 GB of re-downloads.
- **Offline-capable runtime.** Smoke test passes with
  `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` on a fresh container.

### Negative

- **Runtime image grows from ~6.4 GB to ~10 GB.** Acceptable on the
  target hosts; revisit if we ever push to a public registry where
  pulls matter.
- **Build time grows by ~3–5 minutes on a cold cache** (the new
  `models` stage). Warm rebuilds skip the layer entirely.
- **Pin drift risk.** If a future marker-pdf upgrade quietly changes
  the model IDs the bake stage references, the build still succeeds
  and the runtime tries to fetch the new IDs over the network. The
  failure mode is loud (HF/datalab call without permission to write
  the runtime cache dir) and the fix is one-line. Mitigation: monitor
  marker-pdf release notes; consider a runtime self-check that asserts
  the expected directories exist.

### Neutral

- **RAM behaviour unchanged.** Models still load lazily on first use;
  the steady-state resident set after the first PDF is unchanged from
  pre-ADR-0012 behaviour.
- **No application code change.** Application paths read the same
  pydantic-settings fields they did before; the env vars supply the
  baked-cache locations.

## Migration

1. Land the Dockerfile edits (`docker/Dockerfile`: new `models` stage,
   `HF_HOME` + `MODEL_CACHE_DIR` in runtime ENV, post-useradd
   `COPY --from=models --chown=precis:precis`, builder-stage
   `download_font` call).
2. Land the compose edits (drop `precis-cache:/home/precis/.cache`
   mount + the unreferenced `precis-cache` volume definition).
3. `docker compose -f infrastructure/compose.yaml build precis-watch`.
   First run ~5 min for the model downloads; subsequent runs ~30 s.
4. Verify offline: `docker compose run --rm
   -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 precis-cli
   precis add /data/notes/new_papers/<smallest>.pdf`. Should succeed
   without any HF/datalab HTTP calls in the log.
5. CHANGELOG entry under `### Changed` calling out the image-size
   delta and the volume cleanup step for existing hosts.
6. Optional cleanup on existing hosts:
   `docker volume rm precis-infra_precis-cache`.

## Open questions (non-blocking)

- **Quantise or ONNX-export `bge-m3` to halve the image size?**
  Defer. The full-precision weights match what the embed worker
  expects today; trimming the image is a separable slice if we ever
  publish to a public registry.
- **Pin `marker-pdf` minimum higher to match the surya revisions
  the bake produces?** Today the dep is `marker-pdf>=1.0`; the bake
  uses whatever `create_model_dict()` resolves at build time. Tackle
  the day a marker upgrade breaks the contract.
- **Mirror the model bake to a separate `precis-mcp:models` stage so
  CI can build the venv image without paying the download cost?**
  Currently CI doesn't build images; revisit if/when it does.
