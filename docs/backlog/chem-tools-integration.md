# Chemistry & protein tool-packs — integration design

> Design-of-record for folding external chemistry / protein compute
> tools into precis. Companion to `autocatpath-integration.md` and the
> sandbox-run substrate. The decisions log is authoritative.

Shipped portion: see the `precis_chem` and `precis_bio` package
docstrings; full slice narratives in git history. Built (precis side,
dark-gated): the `route` kind + `retrosynth` job + route-graph IR +
stub engine + content-addressed cache (`PRECIS_CHEM_ENABLED`); the
AiZynth container path + wrapper `docker/aizynth/`; LinChemIn
normalization in-container → canonical `route.json` +
`get(view='metrics')`; the ASKCOS `service` transport
(`engine.py`: `inprocess`/`container`/`service`) + standalone
`docker/normalizer`; the `protein` kind + `fold` job_type +
`AlphaFold3Engine` (container transport, `PRECIS_BIO_ENABLED`); the
`precis-lab-help` recipes skill (slice 6). Core seams: `can_own_jobs`
+ the open relation vocabulary.

Thesis (decided): **precis is already the facade** — each tool = a
kind (legible IR) + a `job_type` executor (heavy engine off the
request path, ADR 0044 compute lane). No per-engine MCP servers.

## Open scope

### Cluster ops to go live (via the `deploy/` tree)

- **Retrosynth (1b):** per-node `podman build docker/aizynth`;
  `config.yml` + policy/stock models on the NAS (mounted at `/models`,
  never baked); set `PRECIS_CHEM_ROUTE_NODE` (+
  `PRECIS_CHEM_MODELS_DIR`) on a Linux node; flip
  `PRECIS_CHEM_ENABLED`. Until then the stub inline path is the only
  live engine.
- **LinChemIn (2):** rebuild the image on the node
  (`aizynth_build_image=true`).
- **ASKCOS (3):** stand up an ASKCOS v2 deployment, set
  `PRECIS_ASKCOS_URL`, build the normalizer image, and **verify the
  Tree-Builder request/response schema against the instance's
  `/docs`** (flagged in `askcos.py`).
- **AlphaFold (4b):** `roles/alphafold` asserts the
  `alphafold3:ready` image + models on spark; wire
  `PRECIS_FOLD_NODE=spark`, `PRECIS_FOLD_MODELS_DIR`,
  `PRECIS_FOLD_IMAGE`, an XLA cache mount; un-dark the kind. Verify
  at first live run (flagged best-effort in `alphafold.py`): output
  subdir naming, `summary_confidences.json` key names, de-novo
  accuracy vs MSA.

### Unbuilt slices

- **4c — `structure` convergence:** `cif → ASE → Scene.from_ase`
  (ADR 0043) for a 3D viewer / graph probes; a ColabFold MSA-mode
  engine for real accuracy.
- **5 — sequence design:** a `sequence` kind
  (ProteinMPNN/RFdiffusion), another `job_type`, GPU on spark.
- **Dedicated chem/bio `plan_tick` executor** that auto-drives the
  research loop end-to-end — deferred; the `precis-lab-help` skill
  already lets the generic planner do it.

### Known limitations / follow-ups

- **Read-time inverse rewrite for plugin relations:** `links_for`'s
  inverse rewrite uses the Python `_INVERSE_RELATIONS` dict, which
  doesn't know plugin relations — plugin relations must stay symmetric
  (or query the stored direction) until the read-time inverse map is
  sourced from `relations.inverse_slug`.
- **nvidia-container-runtime not wired** — GPU tools stay in-process
  until it is; wire once for uniform containerization.
- **Registry vs build-on-demand** — revisit if the compute fleet
  grows.

## Decisions log (authoritative)

- precis is the facade; no broker / per-engine MCP servers.
- One canonical `route` kind; engines normalize to it via LinChemIn at
  ingest — not per-engine kinds, not folded into `pathway`.
- Plugin tool-packs, not core kinds — ship dark behind a flag via
  entry points, like autocatpath.
- Two engine styles split by GPU-native-in-process vs
  portable-CPU-container (AF3 became a container in practice — see
  slice 4a note in git history).
- Build-on-demand containers; no tarball store, no registry (yet);
  wrapper Dockerfiles in precis; weights mounted from NAS.
- podman on Linux compute nodes; Macs orchestrate only (shared prereq
  with `sandbox_run`).
- Repo split: shareable plugin + Dockerfiles + portable roles in
  precis-mcp; fleet-private inventory/secrets in the gitignored
  `deploy/inventory/` overlay.
