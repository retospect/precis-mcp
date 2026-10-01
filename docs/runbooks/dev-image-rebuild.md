# Dev image rebuilds: lineages, model seed, "why is it re-downloading models"

**When.** The gate/dev image needs a rebuild, a build is re-downloading ~3.8 GB
of models, or dev tooling re-runs on every source edit. Gate usage:
[`worktree-container-gate`](./worktree-container-gate.md).

## Two image lineages

- `scripts/build-image precis-dev` → tag `precis-dev`. **This is what the gate
  uses** (`scripts/test`, `scripts/ship`, via `docker/dev/compose.yaml`). It
  exports `GH_TOKEN` (from `gh auth token`, caller-set `GH_TOKEN` wins) as the
  BuildKit secret `gh_token`. It does **not** seed `premodels`, so every
  `uv.lock` change cascades into a full ~3.8 GB model re-bake (~10 min) —
  expected here, not a wrong-flag mistake.
- `scripts/precis-shell --rebuild` → tag `precis-mcp:dev`. Seeds `premodels`
  and now also threads `GH_TOKEN` as `--secret id=gh_token` (current
  `scripts/precis-shell`). The private-repo `autocatpath` layer used to die on
  `could not read Username for 'https://github.com'` when this path passed no
  secret (2026-08-19Z; the rebuild-paths fix shipped 2026-08-22Z). If that
  error reappears, a missing `gh` login / empty `GH_TOKEN` is the cause.
  (`scripts/build-image` targets a compose *service* in `docker/dev`; it is not
  the MCP-image path.)

Everything below describes the `precis-mcp:*` lineage and its seed mechanism.

## Multi-stage layout and the seed

`docker/Dockerfile` is multi-stage with an empty `FROM scratch AS premodels`
placeholder. The real model bake is seeded via
`--build-context premodels=docker-image://precis-mcp:premodels`. Without it the
seed resolves to empty scratch and stage `models` re-downloads ~3.8 GB of
Marker/datalab + bge-m3 weights.

Images: `precis-mcp:premodels` (`--target models`; deps venv + baked HF/datalab
caches under `/opt/precis/models/`; rebuilt rarely) and the derived
`precis-mcp:dev` / `precis-mcp:latest` (source/dep changes rebuild on top of the
seed).

Stages:

    premodels   — empty scratch placeholder, overridden by the build-context
    deps        — venv from pyproject.toml + uv.lock (cached on src edits)
    models      — premodels seed + bake (cached on src edits)
    builder     — full source + snapshot install (rebuilds per source edit; feeds runtime only)
    system-base — shared apt + user + entrypoint (parent of runtime and dev-system)
    runtime     — system-base + builder venv + models. Production.
    dev-system  — system-base + dev apt + node + claude-code + uv. Sibling of
                  runtime, NOT a child. Source-independent.
    dev-venv    — deps + dev pip tools (pytest/ruff/mypy/…). Source-independent.
    dev         — dev-system + dev-venv + models + source + editable install.
                  Only the source COPY and editable install rerun per source edit.

## Entry points

- `scripts/precis-shell --rebuild` — dev image; passes the seed arg
  automatically. The first rebuild with the flag has uncached `[models]` steps
  (a different cache key than the prior empty-scratch build); later rebuilds
  without source/dep changes cache everything except the dev source COPY.
- `scripts/precis-shell --rebuild-base` — bumps `:premodels`. Self-seeds from
  the existing tag to dodge the bge-m3 cold-fetch deadlock (the retired
  bake-models design doc is in git history). Only needed on marker-pdf or bge-m3
  pin bumps.
- A runtime image outside this repo (`infrastructure/compose.yaml`,
  `docker compose build`) uses the same seed mechanism for its
  `precis-watch` / `precis-cli` / `precis-dev` service blocks.

## Diagnostics

- **"Rebuild is re-downloading models"** → was
  `--build-context premodels=docker-image://precis-mcp:premodels` passed? The
  build log shows `[models 1/4] COPY --from=premodels /` finishing in <1 s when
  seeded vs ~0 s when not (empty scratch), then `[models 4/4] RUN
  .../bake-models.py` either no-ops or starts "Downloading …".
- **"Dev tooling re-runs on a source change"** → should not happen. If it does,
  the parent of `dev-system` or `dev-venv` is invalidating — check whether
  someone re-introduced a `FROM runtime` or `FROM builder` upstream of them.
- **Staleness of the dev image** is read from `precis-status` `source_drift`,
  never an in-container stat/grep/import.
