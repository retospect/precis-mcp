# ADR 0004 — Multi-stage Dockerfile (builder / runtime / dev)

- **Status**: accepted (2026-05-21)
- **Deciders**: Reto + agent
- **Supersedes**: nothing

## Context

precis-mcp had two Dockerfiles in `infrastructure/precis-mcp/`:

- `Dockerfile` — single-stage, used by `compose.yaml`. Installs the
  package as `pip install -e .` then purges build deps. Editable but
  larger than necessary; mixes build and runtime layers.
- `Dockerfile.prod` — multi-stage with a wheel build, but unused;
  not referenced from `compose.yaml`.

Two issues:

1. **No dev image.** Running `pytest`, `ruff`, `mypy`, etc. requires
   either installing them on the host (against the user's "all in
   docker" rule) or piggybacking on `acatome-watch`, which has the
   wrong dep set.
2. **Two Dockerfiles drift.** New ML deps land in one, not the
   other; only one is exercised by CI.

## Decision

Consolidate into a **single multi-stage Dockerfile** with three
targets:

- `builder` — installs `precis-mcp[all]` into `/opt/venv` using `uv`.
- `runtime` — copies `/opt/venv` from builder, drops build tools,
  adds the non-root `precis` user, sets up the secrets entrypoint.
  This is the production image.
- `dev` — extends `runtime` with developer tooling: pytest,
  pytest-cov, pytest-xdist, hypothesis, ruff, mypy, pylint,
  bandit, pip-audit, pyreverse (via pylint), ipython, ipdb. Drops
  the runtime ENTRYPOINT in favour of a bash shell so the user
  can run any tool interactively.

Both targets ship from the same source-of-truth Dockerfile;
build-cache layers between them are shared.

`compose.yaml` gains a `precis-dev` service with `target: dev` and
`profiles: ["dev"]` so it does not start with `docker compose up`.
The host's precis-mcp source is bind-mounted at `/app` so edits
take effect without a rebuild.

## Consequences

### Positive

- Single Dockerfile is the single source of truth.
- Prod image stays lean (no pytest, no pylint, no graphviz).
- Dev tooling is one `docker compose --profile dev run` away — no
  host installs required.
- Build cache between `runtime` and `dev` is shared via the
  `builder` stage; iteration on dev tools doesn't rebuild the venv.
- Adding new dev tools is a single `uv pip install` line in the
  `dev` stage.

### Negative

- Total image storage on the developer machine doubles (runtime ≈
  500 MB, dev ≈ 1.2 GB). Acceptable; the difference is tooling.
- The bind-mount in `precis-dev` means editing `pyproject.toml` on
  the host requires a rebuild to pick up new deps. Documented.
- We retire `Dockerfile.prod` (legacy, unused). Anyone with a stale
  reference must rebuild against `Dockerfile` with `--target runtime`.

### Build commands

```bash
# Production image
docker compose -f infrastructure/compose.yaml build precis-cli

# Dev image
docker compose -f infrastructure/compose.yaml --profile dev build precis-dev

# Open a dev shell
docker compose -f infrastructure/compose.yaml --profile dev run --rm precis-dev

# Run pytest
docker compose -f infrastructure/compose.yaml --profile dev run --rm precis-dev \
    uv run pytest tests/test_tool_registry.py -v

# Full check (ruff + mypy + pytest)
docker compose -f infrastructure/compose.yaml --profile dev run --rm precis-dev \
    bash -lc "uv run ruff check . && uv run mypy src tests && uv run pytest"
```

### Architecture

Builds default to the host's native architecture (ARM64 on Apple
Silicon, AMD64 on intel). `buildx` for cross-arch is on the
backlog; no CI deploy target requires it yet. When a target does:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  --target runtime -t precis-mcp:latest \
  -f infrastructure/precis-mcp/Dockerfile ..
```

Add when needed; not now.

## Alternatives considered

- **Single image (dev tools always present).** Simpler but bloats
  prod with 700 MB of unused deps and a bigger attack surface.
- **`FROM coding-base` for the dev stage.** Would inherit a curated
  toolchain but pin precis-mcp's dev cadence to coding-base's
  release cadence and force two repos to track. Rejected.
- **Two separate Dockerfiles (one per target).** Drifts. Rejected.

## Follow-ups

- Add `pyreverse`-based `scripts/refresh-diagrams.sh` so class /
  package diagrams stay current with `src/precis/`. Tracked
  separately; do once the v2 schema settles.
- Add a `pre-commit` hook that runs the dev container's full check
  on staged files. Defer until after storage-v2 lands so we don't
  thrash hooks during the refactor.
- Multi-arch buildx setup. List item, not now.
