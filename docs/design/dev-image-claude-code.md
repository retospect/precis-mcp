# Claude Code in the dev image + host UID/GID alignment

**Status**: planned
**Owner**: `docker/Dockerfile`, `infrastructure/compose.yaml`
**Predecessors**:
- ADR 0004 — multi-stage Dockerfile (`docs/decisions/0004-multi-stage-dockerfile.md`)
- ADR 0009 — Dockerfile relocation, container-first (`docs/decisions/0009-dockerfile-relocation-container-first.md`)
**Sibling convention**: `~/work/docker/coding-base/Dockerfile` ships
the same layering for general-purpose Python projects; this slice
imports the relevant lines into the `precis-mcp:dev` target.

## Problem

Two pain points in the current `precis-dev` shell, both visible the
moment you bind-mount the host repo at `/app`:

1. **No agent in the loop.** `precis-mcp:dev` ships pytest, ruff,
   mypy, uv, plantuml, psql, ipython — all the *deterministic*
   tooling. It does not ship `claude` (the Anthropic Claude Code
   CLI). Today the agent runs on the host (Cascade in Windsurf,
   Claude Code from a host terminal) and shells in via
   `scripts/dev`. That works for read-only inspection but not for
   the workflow the rest of the lab uses, where the agent runs
   *inside* the container next to the toolchain it invokes — same
   pattern as `~/work/docker/coding-base` and the projects that
   inherit from it (e.g. `find-pareto-boxel/.devcontainer/devcontainer.json`).

2. **UID mismatch on bind-mounts.** The runtime stage hard-codes
   `useradd -m -u 1000 precis` (`docker/Dockerfile:73`). The host
   user is `501:20` (`reto:staff`) on this Mac. macOS Docker /
   OrbStack does some virtiofs ID translation but it's not perfect
   — files written from the dev container into `/app` (the
   bind-mounted source) sometimes show up as a foreign UID on the
   host, and writes from the watcher into `~/work/corpus`
   (currently unused, but coming online with `precis-watch`) will
   produce the same drift. The accepted fix in `coding-base` is
   to bake the host UID/GID into the image at build time, with
   `useradd -o` allowing the (likely already-allocated) IDs.

Both fixes are dev-image ergonomics. The `runtime` target — which
backs `precis-watch` and `precis-cli` in production — only
inherits the UID/GID change; nothing else moves. The `runtime`
image stays Claude-free.

## Goal

A single rebuild of `precis-mcp:dev` produces an image where:

- `claude --version` works inside the container.
- The Anthropic OAuth login flows through the host's
  `~/.claude/` and `~/.claude.json` via bind-mount; no
  `ANTHROPIC_API_KEY` env var is set, no secret in the image.
- Files written from inside the container into `/app`,
  `/data/corpus`, `/data/notes`, `/inbox` carry the host user's
  UID:GID (`501:20`), so `ls -la` on the host shows `reto staff`
  next to those files instead of a foreign owner.

## Non-goals

- **Claude in the runtime image.** `precis-watch` and
  `precis-cli` are headless services. Adding ~150 MB of Node +
  npm + claude-code to the production image for no runtime
  benefit is wrong. The pin lives in the dev stage only.
- **Project-level `.claude/` template.** The `coding-base`
  template ships `.claude/agents/` (review subagents),
  `.claude/commands/` (slash commands), and `.claude/settings.json`
  (allowed-bash whitelist). `precis-mcp` already has
  `.windsurf/workflows/` for the workflow stages and a fully
  populated `docs/conventions/` + `docs/decisions/` + `docs/design/`
  layout that AGENTS.md points Claude at. A `.claude/` of our own
  is a separate, additive change — not blocked by this slice.
- **Wrapper-script `export UID=$(id -u)` magic.** Tempting but
  broken: `UID` is a *read-only* shell special in bash and zsh,
  so `export UID=…` errors with `UID: readonly variable`. The
  workable equivalent is a `.env` file at the docker-compose
  project directory (`~/work/infrastructure/.env`), which is
  the same pattern `~/work/docker/coding-base/.env` already
  uses on this host. See §Design step 4 for the file shape.

## Design

### Pin choice: match `coding-base` exactly

| Pin | Value | Source |
| --- | --- | --- |
| `NODE_MAJOR` | `20` | `~/work/docker/coding-base/Dockerfile:12` |
| `CLAUDE_CODE_VERSION` | `2.1.143` | `~/work/docker/coding-base/Dockerfile:13` |

Two reasons:

1. **One agent everywhere.** A developer alternating between
   `find-pareto-boxel` (uses `coding-base/python`) and
   `precis-mcp` (uses `precis-mcp:dev`) sees the same `claude`
   binary, same upstream behaviour, same OAuth state. Two
   different versions = two divergent agent personalities, hard
   to debug.
2. **One refresh tool drives both.** `coding-base/scripts/refresh-pins.sh`
   queries npm + GitHub for the latest `@anthropic-ai/claude-code`
   and writes the new value into `coding-base/Dockerfile`. We
   adopt the same value by hand here. A future small slice can
   teach `precis-mcp` to read the pin out of `coding-base` (or to
   ship its own refresh script) — out of scope for this commit.

The pin lives on its own `ARG` line so the next refresh is a
one-liner sed.

### UID/GID on the runtime user (all stages)

The `useradd` call moves up to a single, parameterised line:

```dockerfile
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" -o precis && \
    useradd -m -u "${UID}" -g "${GID}" -o -s /bin/bash precis && \
    mkdir -p /data /inbox /home/precis/.cache && \
    chown -R precis:precis /data /inbox /home/precis/.cache
```

The `-o` (`--non-unique`) on both `groupadd` and `useradd` is
the load-bearing detail: macOS hosts have UID 501 and GID 20
already taken in a stock `python:3.12-slim-bookworm` image
(GID 20 = `dialout`, UID 501 isn't reserved but the principle
of defensive `-o` is the same). Without `-o` the build fails on
those hosts with `groupadd: GID '20' already exists`.

The change lives in the `runtime` stage so both `runtime`
consumers (`precis-watch`, `precis-cli`) and the `dev` consumer
(`precis-dev`, which `FROM runtime`) inherit the same user. The
dev stage doesn't need to remap anything — it just stays as the
`precis` user that the runtime stage already has.

### Claude Code install in the dev stage

A single new layer in the `dev` stage, sitting next to the
existing apt block at `docker/Dockerfile:99-108`:

```dockerfile
ARG NODE_MAJOR=20
ARG CLAUDE_CODE_VERSION=2.1.143

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
```

This runs as `root` in the dev stage (which is already
root-then-precis around the dev installs — `Dockerfile:92, 144`).
`npm install -g` lands the claude binary at
`/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js` with a
shim at `/usr/bin/claude` on `PATH`. No further config.

### compose.yaml — bind-mount the OAuth state, plumb build args

Three services are touched in `~/work/infrastructure/compose.yaml`:

**`precis-watch`** (runtime image — needs UID/GID match for
corpus writes):

```yaml
build:
  context: ../projects/inbox_code/precis-mcp
  dockerfile: docker/Dockerfile
  target: runtime
  args:
    UID: ${UID:-1000}
    GID: ${GID:-1000}
```

**`precis-cli`** (runtime image — UID/GID match for symmetry,
even though /data is currently ro):

```yaml
build:
  context: ../projects/inbox_code/precis-mcp
  dockerfile: docker/Dockerfile
  target: runtime
  args:
    UID: ${UID:-1000}
    GID: ${GID:-1000}
```

**`precis-dev`** (dev image — UID/GID match + the two
`~/.claude` mounts):


```yaml
build:
  context: ../projects/inbox_code/precis-mcp
  dockerfile: docker/Dockerfile
  target: dev
  args:
    UID: ${UID:-1000}
    GID: ${GID:-1000}
volumes:
  - ${HOME}/.secrets/pw:/secrets:ro
  - ${HOME}/work/projects/inbox_code/precis-mcp:/app:rw
  - ${HOME}/work/corpus:/data/corpus:ro
  - ${HOME}/work:/data/notes:ro
  - precis-dev-cache:/home/precis/.cache
  - ${HOME}/.claude:/home/precis/.claude
  - ${HOME}/.claude.json:/home/precis/.claude.json
```

The two `.claude` mounts default to `rw` (which is what Claude
Code wants — it writes session history to `~/.claude.json` and
project state to `~/.claude/projects/`). The host owns those
files at `501:20`; with the matching in-container UID, no
permission drift.

### Step 4 — `.env` at the infrastructure dir

```env
# ~/work/infrastructure/.env  (gitignored; matches coding-base pattern)
UID=501
GID=20
```

Docker compose substitutes `${UID:-1000}` and `${GID:-1000}` in
`compose.yaml` from (a) the shell environment and (b) the `.env`
file in the project directory, in that order. Bash and zsh do
not export `UID`/`GID` (they are shell-internal specials), so
the `.env` file is the load-bearing one. A committed
`.env.example` next to it documents the pattern.

### File-by-file changes

| File | Change | LoC |
| --- | --- | --- |
| `docker/Dockerfile` (runtime stage) | wrap `useradd` in `ARG UID/GID` + `-o` flags; same for `groupadd` | ~5 |
| `docker/Dockerfile` (dev stage) | `ARG NODE_MAJOR/CLAUDE_CODE_VERSION` + `RUN` block for Node + claude-code | ~10 |
| `infrastructure/compose.yaml` | add `args:` block to three services; add two `~/.claude` volume entries on `precis-dev` | ~15 |
| `infrastructure/.env` (new, gitignored) | `UID=501` / `GID=20` for this host | 2 |
| `infrastructure/.env.example` (new, committed) | documents the pattern + macOS/Linux defaults | ~25 |
| `docs/design/dev-image-claude-code.md` | this file | ~260 |
| `docs/decisions/0011-claude-in-dev-image.md` | ADR for the choice | ~85 |
| `CHANGELOG.md` | one bullet under `## Unreleased` → `### Changed` | ~25 |

Net: ~430 lines, almost all docs.

## Thresholds review (`docs/conventions/thresholds.md`)

- **Schema**: untouched. ✅
- **API**: no CLI surface change. ✅
- **Cross-package — new image dependency** (Node 20 + npm +
  `@anthropic-ai/claude-code@2.1.143` in the dev stage only):
  this is exactly the kind of choice an ADR is for. Recording
  in `docs/decisions/0011-claude-in-dev-image.md`. ✅ (with ADR)
- **Performance**: no runtime-image change. The dev image grows
  by ~150 MB; build time grows by ~30 s of npm install on a
  warm cache. Acceptable. ✅
- **Operational**:
  - First rebuild after this change requires a one-time
    `docker volume rm precis-infra_precis-dev-cache` because
    the existing volume's contents are owned by the old in-
    container UID (1000) and the new container (UID 501)
    can't write there. The cache holds pip/uv/ruff/pytest
    caches only — rebuilds in seconds.
  - `precis-cache` (the runtime cache for BGE-M3 + Marker
    models) does **not** yet exist on this host (probed
    `docker volume inspect precis-infra_precis-cache` —
    no such volume). First `precis-watch` start will create
    it with the correct UID. No migration. ✅
  - `~/work/corpus`, `~/work/new_papers`, `~/.secrets/pw`
    on the host are all owned by `reto:staff` already, so
    the new in-container UID matches and bind-mount writes
    are seamless. ✅
- **Secrets**: `~/.claude.json` is mode `0600` on the host and
  contains a refresh token. Bind-mounting it at the same mode
  inside is exactly the security model `coding-base` uses. ✅

No threshold trips beyond the ADR-required dependency add.

## Test plan

1. **Build**: `docker compose -f ~/work/infrastructure/compose.yaml --profile dev build precis-dev`. Expect ~3-5 min on warm cache.
2. **Smoke A — Claude is on PATH**:
   ```
   scripts/dev claude --version
   ```
   Expect `claude-code 2.1.143` (or whatever the pin says).
3. **Smoke B — auth state survives**:
   ```
   scripts/dev claude --print --model haiku 'say hi in one word'
   ```
   No login prompt; Claude responds. Verifies the OAuth bind-
   mount is wired and writable.
4. **Smoke C — UID match on writes**:
   ```
   scripts/dev bash -lc 'touch /app/.uid-probe && stat -c "%u:%g" /app/.uid-probe'
   ```
   Expect `501:20`. On the host, `ls -la /Users/reto/work/projects/inbox_code/precis-mcp/.uid-probe` should show `reto staff`. Then `rm /Users/reto/work/projects/inbox_code/precis-mcp/.uid-probe`.
5. **Smoke D — existing tooling unaffected**:
   ```
   PG_PW=...; scripts/dev bash -lc "PRECIS_TEST_PG_URL=... uv run pytest tests/ingest/test_add.py -q"
   ```
   Expect `9 passed`. (Same as before this slice.)
6. **No regression on runtime image**: `docker compose -f ~/work/infrastructure/compose.yaml build precis-cli` rebuilds clean. Optional, only if the user wants the production image rebuilt now.

## Rollout

1. Land design doc + ADR (this commit).
2. Edit Dockerfile + compose.yaml in one commit titled
   `dev-image: Claude Code + host UID/GID alignment`.
3. CHANGELOG entry under `## Unreleased` → `### Changed`.
4. One-time `docker volume rm precis-infra_precis-dev-cache` on
   the developer's host (called out in the CHANGELOG bullet).
5. Rebuild + run the four smokes above.
6. Continue with the original test-ingest plan (10 PDFs through
   `precis watch`).

## Risk

- **Pin drift between coding-base and precis-mcp.** Two places
  to update on a Claude Code refresh. Mitigation: same `ARG`
  name, same value; the next `coding-base/scripts/refresh-pins.sh`
  output should be applied here in the same commit. Long-term:
  factor a single `pins.env` consumed by both repos. Out of
  scope here.
- **`-o` flag masks GID collisions.** Inside the container,
  `groups precis` will show `precis dialout` (because GID 20 is
  `dialout`'s GID on Debian). Cosmetic; doesn't affect file
  ownership semantics.
- **`~/.claude.json` is mode 0600 on the host.** If the host
  runs Docker Desktop with userns-remap, the in-container
  process may not be able to read it even with matching UID.
  Mitigation: noted but not present on this host (OrbStack
  default does not userns-remap). If a user reports it, the
  workaround is to copy the file in via `cp ~/.claude.json
  ~/.claude.json.dev && chmod 0644 ~/.claude.json.dev` and
  bind-mount the copy.
