# ADR 0019 — Premodels build context for cold-build avoidance

- **Status**: accepted (2026-06-04)
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0012 — Bake Marker + bge-m3 weights into `precis-mcp:latest`](./0012-bake-models-into-runtime-image.md)
- **Plan artefact**: `docs/design/bake-models-into-image.md` (new
  section: *"Cold-build deadlock + premodels seed"*)

## Context

ADR 0012 introduced a `models` Dockerfile stage that bakes the Marker
surya stack and `BAAI/bge-m3` into the runtime image. The stage's
inputs are the `deps` venv + `docker/bake-models.py`; its outputs are
`/opt/precis/models/hf/` (HuggingFace cache) and
`/opt/precis/models/datalab/models/` (surya cache). On a warm cache it
is a no-op layer; on a cold cache it downloads ~3.8 GB.

In the months after 0012 landed, cold-cache builds increasingly hung
indefinitely inside the `bge-m3` fetch. The failure mode was always
the same:

- `sentence_transformers.SentenceTransformer("BAAI/bge-m3")` or the
  follow-up rewrite using `huggingface_hub.snapshot_download(...)`
  reached `Fetching 22 files: 18%` and stopped emitting progress.
- Wall-clock build elapsed grew to 11+ hours with no further log
  output, no process exit, no error.
- `HF_HUB_DOWNLOAD_TIMEOUT=120` did not help.
- `ps -ef` showed the Python interpreter alive but consuming sub-second
  CPU.

Inspection of the buildkit-VM-resident process via
`/proc/<pid>/task/*/stack` showed **all 19 threads parked in
`futex_wait`** with zero socket I/O across `lsof | grep :443`. The
hang is not a slow download — it is a deadlock inside HuggingFace's
xet-bridge protocol (`cas-bridge.xethub.hf.co`), reproducible at least
on Apple Silicon hosts through OrbStack, on multiple network paths,
across multiple `huggingface_hub` versions. The root cause is upstream
and not something we can fix at the application layer.

Meanwhile, the *exact same model weights* were already present in the
existing `precis-mcp:latest` image (the previous successful build).
Re-baking them in every cold build means re-paying a download that
deadlocks.

## Decision

Seed the `models` stage from a prior image via Docker BuildKit's
`--build-context` mechanism. The new flow:

1. A reference image named `precis-mcp:premodels` (typically a tag of
   the last known-good `precis-mcp:latest`) holds the populated
   `/opt/precis/models/` tree.
2. The Dockerfile declares a default no-op `premodels` stage and
   COPYs `/` from it into `/tmp/premodels-root/` at the start of the
   models stage. A follow-up `RUN` conditionally seeds
   `/opt/precis/models/` from the temp copy and removes the temp.
3. `docker/bake-models.py` checks whether the bge-m3 cache directory
   is populated and **skips the download entirely** when it is. The
   verification `SentenceTransformer("BAAI/bge-m3")` call runs with
   `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` so it cannot fall
   back to a network revision-check.
4. `infrastructure/compose.yaml` passes `additional_contexts:
   premodels: docker-image://precis-mcp:premodels` for all three
   build blocks (`precis-watch`, `precis-cli`, `precis-dev`).

The default `FROM scratch AS premodels` stage in the Dockerfile is the
safety net: if no premodels image is available (truly cold first-ever
build), the COPY succeeds with an empty source, the conditional cp is
a no-op, and bake-models.py falls through to the real (deadlock-prone)
download path. The user can then either retry until the xet-bridge
gods are kind, or pre-populate the cache by other means.

Operationally: after any successful build, tag the result —

```sh
docker tag precis-mcp:latest precis-mcp:premodels
```

— so the next cold rebuild has a seed to draw from.

## Consequences

### Positive

- **Cold-cache builds complete in ~3 minutes** instead of 11+ hours
  (or never). Marker datalab models still download (~120 s — they
  use a separate HTTP fetch that does not hit xet-bridge) but bge-m3
  is read from the seed.
- **Source-only rebuilds are ~55 seconds.** Stage 2 (`models`)
  is fully cache-hit; only Stage 3 (`builder`) + Stage 4 (`runtime`)
  rerun for `src/precis/**` edits. Validated by touching
  `src/precis/handlers/paper.py` and timing the rebuild.
- **No application code change.** The xet-bridge workaround lives
  entirely in `docker/` and `infrastructure/`; the runtime image
  semantics are unchanged.
- **The bake script is now defensive.** Even if a future build
  reintroduces the xet-bridge call (e.g. by bumping marker-pdf's
  bge-m3 dep to a new revision), the offline-flag verification path
  surfaces the cache miss as a fast clear error rather than an
  indefinite hang.

### Negative

- **Operational coupling.** A successful first build still requires
  *some* mechanism to populate the seed. The current process is
  "the user had a working image before the deadlock started"; if
  every prior image is lost, the only fallback is the deadlock-prone
  cold path. Mitigation options if/when this matters: publish a
  signed `precis-mcp:premodels` image to a registry, or ship the
  bake-stage layer as an OCI artifact.
- **Premodels image must exist locally for the optimised path.** If
  the tag is missing, builds silently fall through to the cold path
  (no premodels seed). The first symptom is "the build started
  re-downloading bge-m3 from xet-bridge." Mitigation: a CI/CD smoke
  check that the seed tag is present before invoking the build.
- **Two Dockerfile stages share `/tmp/premodels-root/` semantics.**
  The conditional `if [ -d /tmp/premodels-root/opt/precis/models ]`
  in the COPY-and-cp step makes the absence case explicit, but it
  adds a layer of indirection a future reader has to follow.

### Neutral

- **Image size unchanged.** The seed populates the same directories
  the bake stage would have populated; the final layer hash is
  identical when the seed matches the upstream HF revision.
- **No change to runtime behaviour.** Whatever the bake stage
  produces, the runtime stage `COPY --from=models` picks up — the
  source of the cache (seed vs. fresh download) is invisible to the
  application.

## Migration

1. Land `docker/Dockerfile` edits (default `FROM scratch AS
   premodels`, `COPY --from=premodels / /tmp/premodels-root/`,
   conditional cp + rm, bake invocation unchanged).
2. Land `docker/bake-models.py` edits (`_bake_bge_m3` skip-if-
   populated guard, `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`
   wrapping the verification load).
3. Land `infrastructure/compose.yaml` edits (`additional_contexts:
   premodels: docker-image://precis-mcp:premodels` for all three
   build blocks).
4. **Bootstrap step (one-off per host):** `docker tag precis-mcp:latest
   precis-mcp:premodels` against whatever working image the host
   already has, OR pull a known-good image from a registry.
5. Rebuild: `docker compose -f ~/work/infrastructure/compose.yaml
   build precis-cli`. Expect ~3 min on first run with seed available;
   ~55 s on warm source-only iterations.
6. **Maintenance:** after any deliberate rebuild that succeeds,
   `docker tag precis-mcp:latest precis-mcp:premodels` to refresh
   the seed. Stale seeds (where the user wants a newer bge-m3
   revision) require dropping the seed tag and accepting the cold
   path — which still works if xet-bridge happens to be healthy.

## Open questions (non-blocking)

- **Publish `precis-mcp:premodels` to a registry?** Today the seed
  is purely local; a fresh host with no prior image cannot benefit.
  Pushing the seed somewhere durable (GHCR, a private registry,
  even a tarball in S3) would close the bootstrap gap. Out of scope
  until/unless we onboard a second build host.
- **Detect xet-bridge availability before falling through?** A
  pre-flight HEAD against `cas-bridge.xethub.hf.co` could decide
  whether to even try the download path. Adds complexity for a
  benefit that mostly only matters on truly cold builds we don't
  expect to hit often.
- **Pin `huggingface_hub` to a pre-xet release?** Older versions
  used direct LFS fetches that do not deadlock. Trade-off is
  losing improvements in the rest of the HF stack; out of scope
  until we have a reason to want them.
