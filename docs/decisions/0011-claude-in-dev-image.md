# ADR 0011 — Claude Code in the dev image; host UID/GID alignment

- **Status**: accepted (2026-05-22)
- **Deciders**: Reto + agent
- **Builds on**:
  - ADR 0004 — multi-stage Dockerfile (`docs/decisions/0004-multi-stage-dockerfile.md`)
  - ADR 0009 — Dockerfile relocation, container-first
    (`docs/decisions/0009-dockerfile-relocation-container-first.md`)
- **Plan artefact**: `docs/design/dev-image-claude-code.md`

## Context

`precis-mcp:dev` ships a complete Python toolchain (uv, pytest,
ruff, mypy, ipython, ipdb, plantuml, psql) but no AI agent. The
canonical workflow elsewhere on this host —
`~/work/docker/coding-base` and any project that inherits from it,
e.g. `~/work/projects/code/find-pareto-boxel/.devcontainer/devcontainer.json`
— bakes Claude Code into the image and bind-mounts the host's
`~/.claude/` and `~/.claude.json` for OAuth state. The agent then
runs *inside* the same container as ruff and pytest, with no
network shim or API-key wrangling.

A second, related friction surfaced in the same review: the dev
image hard-codes `useradd -u 1000 precis` (`docker/Dockerfile:73`).
The host user is `501:20` (`reto:staff`). macOS Docker / OrbStack
do partial virtiofs ID translation, but not perfect — files
written from inside the dev container into the bind-mounted
`/app` (host source) sometimes show up with foreign ownership on
the host. The watcher's writes to `/data/corpus` (rw bind-mount)
will produce the same drift once `precis-watch` starts running
in earnest.

Both fixes are dev-image ergonomics. The runtime image
(`precis-mcp:latest`) is consumed by `precis-watch` and
`precis-cli`; it stays Claude-free, but it does want the UID/GID
alignment so the watcher's corpus writes carry the host owner.

## Decision

### 1. Claude Code lands in the **dev** stage only

Add to `docker/Dockerfile` after the existing dev apt block:

```dockerfile
ARG NODE_MAJOR=20
ARG CLAUDE_CODE_VERSION=2.1.143

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
```

Pins match `~/work/docker/coding-base/Dockerfile:12-13` exactly so
a developer alternating between the two image families sees the
same agent binary. A future small slice can teach `precis-mcp` to
read a single `pins.env` shared with `coding-base`; deferred.

The runtime stage stays untouched — production containers
(`precis-watch`, `precis-cli`) do not need an LLM CLI on PATH.

### 2. Authentication via host bind-mount, **no API key**

`compose.yaml`'s `precis-dev` block adds:

```yaml
volumes:
  - ${HOME}/.claude:/home/precis/.claude
  - ${HOME}/.claude.json:/home/precis/.claude.json
```

Same shape as `~/work/docker/coding-base/compose.yaml:44-45`. The
host runs `claude /login` once via a real browser; the OAuth
refresh token writes to `~/.claude/.credentials.json`. Every
container that bind-mounts those two paths inherits the login.
We do **not** set `ANTHROPIC_API_KEY` in any image or compose
service.

The two paths are bind-mounted `rw` because Claude Code writes
session history into `~/.claude.json` and per-project state into
`~/.claude/projects/`. With the host UID matching the in-
container UID (per §3 below), there is no permission drift.

### 3. UID/GID match the host at build time, all stages

The runtime stage's `useradd` becomes parameterised:

```dockerfile
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" -o precis && \
    useradd -m -u "${UID}" -g "${GID}" -o -s /bin/bash precis && \
    mkdir -p /data /inbox /home/precis/.cache && \
    chown -R precis:precis /data /inbox /home/precis/.cache
```

The `-o` (`--non-unique`) flag on both `groupadd` and `useradd`
is load-bearing: a stock `python:3.12-slim-bookworm` image has
GID 20 already taken (`dialout`); without `-o` the build fails on
macOS hosts where `${GID}` resolves to 20. Same fix
`coding-base/Dockerfile:48-49` already carries.

`compose.yaml` plumbs the build args for all three services
(`precis-watch`, `precis-cli`, `precis-dev`):

```yaml
build:
  context: ../projects/inbox_code/precis-mcp
  dockerfile: docker/Dockerfile
  target: <runtime|dev>
  args:
    UID: ${UID:-1000}
    GID: ${GID:-1000}
```

`${UID}` / `${GID}` are *not* auto-exported by bash or zsh
(they're shell-internal read-only specials), so the values
must come from `~/work/infrastructure/.env` — gitignored,
host-specific, two lines (`UID=501` / `GID=20` on macOS;
`UID=1000` / `GID=1000` on most Linux dev hosts). A committed
`~/work/infrastructure/.env.example` documents the pattern.
The `:-1000` defaults keep the build working in CI where no
`.env` exists.

## Why not...

### …a single API-key path with `ANTHROPIC_API_KEY`?

OAuth-via-bind-mount has three advantages over a key in env:

1. **Revocation in one place.** If a key leaks, the host's
   `claude /logout` revokes it for every container that mounted
   the credentials. With `ANTHROPIC_API_KEY` we'd be sweeping
   `.env` files across multiple repos.
2. **No secret in image layers.** The image is shareable; the
   host's `~/.claude/` is not.
3. **Consistent with `coding-base`.** Same model, same workflow,
   same agent personality across project families.

`ANTHROPIC_API_KEY` stays as a documented escape hatch (per the
`coding-base/.env.example` precedent) for headless / CI cases
where the OAuth callback can't reach a host browser.

### …Claude in the runtime image too?

The runtime image is a service binary. `precis-watch` watches
PDFs; `precis-cli` runs `precis migrate` / `precis worker` /
`precis maintenance`. Neither calls into an LLM at runtime
(LLM-driven summarisation is a future worker handler that ships
its own credentials, not a generic claude-code dependency). Adding
~150 MB of Node + npm + claude-code to a production image we ship
to PyPI consumers (eventually) for zero runtime gain is wrong.

### …only patching the dev stage's UID, leaving runtime at 1000?

`precis-watch` writes to `/data/corpus` (which maps to the host's
`~/work/corpus`). Files written there with UID 1000 show up on
the host as a foreign user; `ls -la` is unreadable, and any host-
side script that filters by owner breaks. Aligning all three
images to the same host UID is one extra `args:` block per
service; the cost is trivial and the consistency is worth it.

### …userns-remap on Docker Desktop instead?

userns-remap is a host-level setting that re-maps container UIDs
to a private range (e.g. `100000+container_uid`). It works, but
it's per-host config that every developer would need to apply,
and OrbStack (which this user runs) does not support it. Build-
time UID parameterisation is portable; userns-remap is not.

## Consequences

### Positive

- One agent everywhere: `claude` works inside `precis-dev` exactly
  like it works inside `coding-base`-derived shells.
- Bind-mount writes are seamless: files created from inside the
  container show up on the host as `reto staff`, no chown sweeps.
- Production images stay lean: the +150 MB lives only in
  `precis-mcp:dev`.

### Negative

- One-time `docker volume rm precis-infra_precis-dev-cache` on
  any host where the volume already exists. The volume's contents
  were written by UID 1000 and the new container (UID 501) can't
  write there. The volume holds caches only (uv / pip / ruff /
  pytest); first use after rebuild repopulates it in seconds.
  Called out in the CHANGELOG bullet that ships with this slice.
- Pin drift potential: `CLAUDE_CODE_VERSION` lives in two
  Dockerfiles now (`coding-base/Dockerfile` and
  `precis-mcp/docker/Dockerfile`). Mitigation: same `ARG` name,
  same value; future refresh PRs touch both. A shared `pins.env`
  is a future small slice.
- A future Linux developer with host UID ≠ 1000 must rebuild the
  image (the build args bake in at build time, not runtime). This
  is no different from the existing `coding-base` workflow.

## Open questions (non-blocking)

- Do we want a project-level `.claude/settings.json` (allowed-
  bash whitelist + maybe subagents) in the precis-mcp repo? The
  `coding-base/templates/.claude/` shape is the obvious starting
  point. Currently every bash command requires y/n in Claude
  Code; a small whitelist (`uv *`, `pytest *`, `ruff *`, `mypy *`,
  `precis *`, `git status`, etc.) would dramatically reduce
  prompts. Defer to a separate slice when the friction shows up.
- Should `precis-mcp` ship its own `scripts/refresh-pins.sh`
  parallel to `coding-base`'s? Or import the value from a shared
  `pins.env`? Both are tractable; the shared file is cleaner once
  there's a third image family that wants the same pin.
