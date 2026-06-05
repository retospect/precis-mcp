# ADR 0009 — Dockerfile moves into `precis-mcp/docker/`; container-first dev workflow

- **Status**: accepted (2026-05-21)
- **Deciders**: Reto + agent
- **Supersedes**:
  - ADR 0004 §"Location" — the Dockerfile is no longer at
    `infrastructure/precis-mcp/Dockerfile`. The multi-stage layout
    (builder / runtime / dev) and the rationale for it remain in
    force; only the path changes.

## Context

We are about to begin the B-track work (greenfield schema, identity
module, ingest rewrite, …) and that work will involve heavy use of
Python tooling: `uv`, `pytest`, `ruff`, `mypy`, `psql`, `plantuml`,
`marker`, etc. Two questions surfaced during the schema-v2 lock review:

1. **Where does the Dockerfile live?** Currently at
   `infrastructure/precis-mcp/Dockerfile`. The infrastructure repo
   owns it, with build context `..` (the workspace root) so the
   Dockerfile can `COPY projects/inbox_code/precis-mcp/`. This
   pattern works but couples the application's image to the
   infrastructure repo: cloning the precis-mcp source alone is not
   enough to build the image; releasing a pip package and matching
   Docker image becomes a cross-repo coordination problem.

2. **What runs on the host?** The user's standing rule is that
   *all dev tools live in the container, never on the host*. Git
   credentials live on the host (SSH keys, GPG keys, `~/.gitconfig`),
   so git itself runs on the host. Everything else — `uv`, `pytest`,
   `ruff`, `mypy`, `psql`, `plantuml`, `marker` — must run inside
   the `precis-dev` container.

These two questions are coupled. Co-locating the Dockerfile with the
source means the precis-mcp repo is self-sufficient for image builds
*and* the container-first workflow has an obvious home for wrapper
scripts (`scripts/dev`, `scripts/db`) next to the Dockerfile they
target.

## Decision

### 1. Dockerfile lives in the precis-mcp repo

```
precis-mcp/
├── docker/
│   ├── Dockerfile              ← moved from infrastructure/precis-mcp/Dockerfile
│   └── docker-entrypoint.sh    ← moved from infrastructure/precis-mcp/docker-entrypoint.sh
├── .dockerignore               ← new; keeps build context lean
├── scripts/
│   ├── dev                     ← new; run anything in precis-dev
│   ├── db                      ← new; psql / SQL ops in precis-dev
│   └── render-uml              ← already there; renders PlantUML in precis-dev
└── …
```

Build context is the **precis-mcp repo root** (not the workspace
root). The Dockerfile's `COPY .` references the local repo only, so
the image build is self-contained: `git clone precis-mcp && cd
precis-mcp && docker build -f docker/Dockerfile --target runtime .`
produces the runtime image with no external paths involved.

### 2. `infrastructure/compose.yaml` references the new location

```yaml
precis-cli:
  build:
    context: ../projects/inbox_code/precis-mcp
    dockerfile: docker/Dockerfile
    target: runtime

precis-dev:
  build:
    context: ../projects/inbox_code/precis-mcp
    dockerfile: docker/Dockerfile
    target: dev
```

The acatome-watch and corpus-server services keep their Dockerfiles in
`infrastructure/` for now — they are operational services that don't
ship as pip packages and aren't part of the precis-mcp release loop.
This will be revisited once those services merge into precis-mcp per
the pip-merge plan.

### 3. Host runs git only; container runs everything else

| Tool | Where | Why |
|---|---|---|
| `git`, IDE | host | needs SSH/GPG keys, IDE integration |
| `uv`, `pytest`, `ruff`, `mypy`, `bandit`, `pip-audit` | container | reproducible Python env, no host pollution |
| `psql`, `pg_dump`, `pg_restore` | container | matches server major version |
| `plantuml`, `default-jre-headless` | container | avoid JRE on host |
| `docker buildx` (release image build) | host | host docker daemon; CI eventually |
| `uv build`, `uv publish` (pip release) | CI (already configured on GitHub) | tag push triggers PyPI publish |

Wrappers in `precis-mcp/scripts/`:

- `scripts/dev [cmd…]` — run any command in `precis-dev` (default:
  interactive bash). Replaces ad-hoc `docker compose --profile dev
  run --rm precis-dev …`.
- `scripts/db psql | query "SQL;" | url` — DB ops inside `precis-dev`,
  reading `$PRECIS_DATABASE_URL` from `/secrets/`.
- `scripts/render-uml` — unchanged; already follows this pattern.

The wrappers honour `PRECIS_COMPOSE` env var to override the compose
file path; default is `~/work/infrastructure/compose.yaml`.

### 4. Releases stay where they are

Pip release flow is already wired in `precis-mcp/.github/workflows/`:
push a release tag, CI builds & publishes to PyPI. No host-side
`uv publish`, no tokens locally. Docker images: tag-based release on
the same trigger, multi-platform via `docker buildx` in CI (future
work; not part of this ADR).

## Consequences

### Positive

- **Self-contained builds**: `git clone precis-mcp && docker build`
  works without the infrastructure repo on disk.
- **Single source of truth**: the Dockerfile lives next to the code
  it images. No cross-repo drift between Dockerfile and `pyproject.toml`.
- **Container-first discipline is automatic**: `scripts/dev` is
  shorter to type than `docker compose --profile dev run --rm
  precis-dev`, so it actually gets used.
- **Host stays clean**: no Python, no JRE, no postgres-client,
  no uv on the host. Only Docker + Git + the IDE.

### Negative

- **Two-step move**: this commit lands the new location and updates
  `compose.yaml`; a follow-up commit in the infrastructure repo
  deletes the now-orphaned `infrastructure/precis-mcp/Dockerfile`
  and `docker-entrypoint.sh`.
- **Build context is the entire repo**: `.dockerignore` matters more
  now (without it `.venv`, `.git`, caches get copied into the build
  context).

### Neutral

- The `.devcontainer/` configuration in `infrastructure/precis-mcp/`
  is **not moved** in this ADR. It's a separate workflow (VSCode-style
  in-IDE container) that we don't currently use day-to-day; revisit if
  we adopt it.
- The host wrappers `scripts/precis` and `scripts/precis-mcp-stdio.sh`
  in `infrastructure/precis-mcp/scripts/` are **not moved** in this
  ADR. The stdio wrapper is referenced by absolute path from the
  Windsurf MCP config; moving it would require coordinated config
  updates. Defer until the MCP config is also being changed.

## Migration

1. Land `precis-mcp/docker/Dockerfile`, `precis-mcp/docker/docker-entrypoint.sh`,
   `precis-mcp/.dockerignore`, `precis-mcp/scripts/dev`,
   `precis-mcp/scripts/db`, and this ADR in the precis-mcp repo.
2. Update `infrastructure/compose.yaml` build contexts; verify
   `docker compose --profile dev build precis-dev` succeeds.
3. Delete `infrastructure/precis-mcp/Dockerfile` and
   `infrastructure/precis-mcp/docker-entrypoint.sh` from the
   infrastructure repo. Optionally leave a stub README pointing at
   the new location.
4. From this point on, all dev tool invocations route through
   `scripts/dev` or `scripts/db`. The B-track work begins.

## Open questions

- **When to merge acatome-extract / corpus-server into precis-mcp?**
  The pip-merge plan (`docs/design/pip-merge.md`) covers this. When
  it happens, their Dockerfiles also relocate.
- **Release-image multi-platform builds**: defer until first
  prod-target deployment; CI is the natural home for `buildx`.
- **Dev/prod compose split**: defer until first deployment surfaces
  the need; currently a single compose file with profiles is
  sufficient.
