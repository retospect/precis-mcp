# syntax=docker/dockerfile:1.7
# precis-mcp — multi-stage Dockerfile
#
# Stage layout (top → bottom):
#
#   premodels    — placeholder for the model-cache seed image. Defaults
#                  to an empty scratch FS; override at build time with
#                  `--build-context premodels=docker-image://precis-mcp:premodels`
#                  to seed Stage `models` from a prior image.
#
#   deps         — /opt/venv populated from pyproject.toml + uv.lock.
#                  Inputs: lockfile only. Source edits don't touch this.
#
#   models       — Marker (surya) + bge-m3 weights baked under
#                  /opt/precis/models/. With a premodels seed, the bake
#                  no-ops. Inputs: deps + bake-models.py + the seed.
#
#   builder      — precis-mcp itself installed into deps' venv (snapshot
#                  install for runtime). Inputs: models + full repo.
#
#   system-base  — minimal runtime apt stack + precis user + entrypoint
#                  script. Shared base for the runtime and dev images.
#                  Source-independent — cached unless the runtime apt
#                  list or UID/GID changes.
#
#   runtime      — production image. system-base + COPY venv from
#                  builder + COPY models from models. ENTRYPOINT
#                  `precis serve`.
#
#   dev-system   — dev apt stack (psql, graphviz, plantuml, jre) + node
#                  + claude-code + uv. Source-independent — cached
#                  unless the dev apt list / node / claude pin changes.
#
#   dev-venv     — runtime venv + dev Python tools (pytest, ruff, mypy,
#                  …). Source-independent — cached unless pyproject /
#                  uv.lock / the dev pip list changes.
#
#   dev          — final dev image. dev-system + COPY dev-venv +
#                  COPY models + COPY source + editable install of
#                  precis-mcp. The source COPY and editable install
#                  are the only steps that invalidate on every source
#                  edit; everything upstream stays cached.
#
# Build the prod image:
#   docker build --target runtime -t precis-mcp:latest -f docker/Dockerfile .
#
# Build the dev image:
#   docker build --target dev -t precis-mcp:dev -f docker/Dockerfile .
#
# Build context is the precis-mcp repo root. Builds native to the host
# (ARM64 on Apple Silicon, AMD64 on intel hosts); multi-arch via
# `docker buildx --platform` for release builds. See
# docs/decisions/0004-multi-stage-dockerfile.md,
# docs/decisions/0009-dockerfile-relocation-container-first.md, and
# docs/design/bake-models-into-image.md (premodels seed mechanism).

ARG PYTHON_DIGEST=sha256:d193c6f51a7dbd10395d6328de3a7edb0516fb0608ca138036576f574c3e07d2
ARG UV_VERSION=0.11.14

# Default empty stage for `premodels`. Override at build time with
# --build-context premodels=docker-image://precis-mcp:premodels
# (or similar) to seed the model cache from a prior image and skip
# the multi-GB HF / datalab download. See Stage `models` below.
FROM scratch AS premodels

# =============================================================================
# Stage: deps — install dependencies from the lockfile into /opt/venv.
# Inputs are ONLY pyproject.toml + uv.lock — touching any `.py` under
# `src/precis/**` does NOT invalidate this layer, so the ~1.5 GB model
# bake in Stage `models` also stays cached across source edits.
# =============================================================================
FROM python:3.12-slim-bookworm@${PYTHON_DIGEST} AS deps

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_COLOR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

ARG UV_VERSION
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        git \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /build/precis-mcp
# Copy ONLY the dep manifests — these are what invalidate the deps cache.
COPY pyproject.toml uv.lock /build/precis-mcp/

# Install every locked dep into /opt/venv, but NOT precis-mcp itself
# (--no-install-project). Stage `builder` does that on top.
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv venv /opt/venv && \
    uv sync --frozen --no-install-project --all-extras

# Marker writes GoNotoCurrent into site-packages/static/fonts/ on first
# converter construction (see marker/util.py:download_font). Pre-populate
# it here so the venv COPYed into runtime already has the font and the
# unprivileged ``precis`` user never tries to write into the read-only
# venv at runtime.
RUN /opt/venv/bin/python -c "from marker.util import download_font; download_font()"

# =============================================================================
# Stage: models — pre-populate the HuggingFace cache with the model
# weights precis loads at runtime (Marker surya stack + BAAI/bge-m3).
#
# Inputs: the `deps` venv only. Source edits under src/precis/** do NOT
# invalidate this layer — only marker-pdf / sentence-transformers
# version bumps or model ID changes force a re-download.
# See docs/design/bake-models-into-image.md.
# =============================================================================
FROM deps AS models

# Two caches to populate:
#   * HF_HOME — sentence-transformers / huggingface_hub. Holds bge-m3.
#   * MODEL_CACHE_DIR — surya / datalab. Holds the Marker layout, OCR
#     detection / recognition, table-rec, and ocr-error-detection
#     models (~1.5 GB; downloaded from s3://models.datalab.to, not HF).
# surya reads MODEL_CACHE_DIR directly off the env via pydantic-settings
# (see surya/settings.py: `MODEL_CACHE_DIR: str = ...`).
ENV HF_HOME=/opt/precis/models/hf \
    MODEL_CACHE_DIR=/opt/precis/models/datalab/models

# Seed marker + HF caches from a prior image via the `premodels` build
# context BEFORE running bake-models.py. snapshot_download / create_model_dict
# are both idempotent — they no-op when the cache is already populated.
# This dodges the multi-hour silent hang on bge-m3 shard fetch through HF.
# Pass `--build-context premodels=docker-image://precis-mcp:premodels`
# (or any image with /opt/precis/models populated) to skip the download.
# If no premodels context is provided, COPY --from is a no-op (the named
# stage exists but holds an empty scratch tree).
COPY --from=premodels / /tmp/premodels-root/
RUN mkdir -p "${HF_HOME}" "${MODEL_CACHE_DIR}" && \
    if [ -d /tmp/premodels-root/opt/precis/models ]; then \
        cp -r /tmp/premodels-root/opt/precis/models/. /opt/precis/models/; \
    fi && \
    rm -rf /tmp/premodels-root
COPY docker/bake-models.py /tmp/bake-models.py
RUN /opt/venv/bin/python /tmp/bake-models.py && \
    rm /tmp/bake-models.py

# =============================================================================
# Stage: builder — install precis-mcp itself on top of cached deps + models.
# This stage's inputs are the full repo, so it reruns on every source edit
# — but the install is cheap (`--no-deps` because all deps already in the
# venv) and the heavy marker/bge-m3 download from Stage `models` is preserved.
# Output is consumed by the `runtime` stage (snapshot install). The `dev`
# stage uses `dev-venv` + an editable install instead, so it doesn't depend
# on builder.
# =============================================================================
FROM models AS builder

COPY . /build/precis-mcp/
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv pip install --python /opt/venv --no-deps "/build/precis-mcp[all]"

# =============================================================================
# Stage: system-base — shared minimal apt stack + precis user.
# Source-independent: cached unless the runtime apt list or UID/GID changes.
# Consumed by both `runtime` and `dev-system` so the apt layer is computed
# once for both image variants.
# =============================================================================
FROM python:3.12-slim-bookworm@${PYTHON_DIGEST} AS system-base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_COLOR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HF_HOME="/opt/precis/models/hf" \
    MODEL_CACHE_DIR="/opt/precis/models/datalab/models"

# Runtime libs only (no build-essential).
# ``procps`` ships /usr/bin/pgrep + /usr/bin/ps which the compose
# healthchecks use to verify the long-running CLI loop is still
# resident. python:3.12-slim-bookworm omits procps by default, so
# without this line ``HEALTHCHECK ["CMD","pgrep","-f","..."]`` fails
# with ``executable file not found in $PATH`` on every probe.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
        tini \
        procps \
        libimage-exiftool-perl

# Non-root user.
#
# UID/GID are build args so the in-container ``precis`` user can match
# the host user that owns bind-mounted directories (~/work, ~/.secrets,
# ~/.claude). On macOS the host is typically 501:20; on Linux dev hosts
# it's often 1000:1000. ``-o`` (--non-unique) on both groupadd and
# useradd is load-bearing: a stock python:3.12-slim-bookworm has GID 20
# already taken (`dialout`), so without ``-o`` the build fails on macOS.
# See docs/decisions/0011-claude-in-dev-image.md.
ARG UID=501
ARG GID=20
RUN groupadd -g "${GID}" -o precis && \
    useradd -m -u "${UID}" -g "${GID}" -o -s /bin/bash precis && \
    mkdir -p /data /inbox /home/precis/.cache && \
    chown -R precis:precis /data /inbox /home/precis/.cache

# Secrets reader (lives next to this Dockerfile).
COPY docker/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# =============================================================================
# Stage: runtime — production image. system-base + venv (from builder)
# + models (from models). Lean, no dev tooling.
# =============================================================================
FROM system-base AS runtime

COPY --from=builder /opt/venv /opt/venv

# Baked-in HuggingFace cache (marker surya + bge-m3). ~3.8 GB on disk;
# mmap'd lazily on first embed / first PDF, so RAM behaviour is unchanged
# from the previous lazy-download model. See
# docs/design/bake-models-into-image.md.
COPY --from=models --chown=precis:precis /opt/precis/models /opt/precis/models

USER precis
WORKDIR /data

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["precis", "serve"]

# =============================================================================
# Stage: dev-system — system-base + dev apt + node + claude-code + uv.
# Source-independent: cached unless the dev apt list, NODE_MAJOR, or
# CLAUDE_CODE_VERSION changes. Sibling of `runtime`, not a child of it,
# so source edits don't invalidate this layer through builder's venv.
# =============================================================================
FROM system-base AS dev-system

ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# Dev system tools.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        jq \
        postgresql-client \
        graphviz \
        plantuml \
        default-jre-headless

# Node + Claude Code CLI. Pins match ~/work/docker/coding-base/Dockerfile
# so the agent binary is identical between coding-base-derived projects
# and precis-mcp's dev shell. OAuth state arrives via bind-mounted
# ~/.claude and ~/.claude.json from the host (no API key in the image
# or env). See docs/decisions/0011-claude-in-dev-image.md.
ARG NODE_MAJOR=20
ARG CLAUDE_CODE_VERSION=2.1.143
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

# uv — used by `dev` to install dev Python tools into /opt/venv and to
# do the editable precis-mcp install at the tail.
ARG UV_VERSION
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    pip install --no-cache-dir "uv==${UV_VERSION}"

# =============================================================================
# Stage: dev-venv — deps' runtime venv with dev Python tools added on top.
# Source-independent: cached unless pyproject.toml / uv.lock or the dev pip
# list changes. Output (/opt/venv) is COPYed into `dev`. Kept off the
# `models` chain because dev's editable install of precis-mcp replaces the
# snapshot install — no need to drag the snapshot through.
# =============================================================================
FROM deps AS dev-venv

RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv pip install --python /opt/venv \
        pytest \
        pytest-cov \
        pytest-xdist \
        hypothesis \
        ruff \
        mypy \
        pylint \
        bandit \
        pip-audit \
        ipython \
        ipdb \
        rich

# =============================================================================
# Stage: dev — final developer image. dev-system + venv (from dev-venv)
# + models (from models) + editable source install. Only the source
# COPY and editable install invalidate per source edit; everything
# upstream stays cached.
# =============================================================================
FROM dev-system AS dev

# Bring in the runtime venv (deps + dev pip tools) and the baked model
# cache. Both COPYs are cached unless their source stages changed.
COPY --from=dev-venv /opt/venv /opt/venv
COPY --from=models --chown=precis:precis /opt/precis/models /opt/precis/models

# Bring the source tree in for live editing convenience; bind-mounts in
# compose will override this so it's just a sane fallback.
COPY --chown=precis:precis . /app
WORKDIR /app

# Editable install of precis-mcp itself. The resulting .pth file in
# /opt/venv references /app/src/precis — at runtime the bind mount
# makes /app live, so `import precis` resolves to the host source.
# Edit and re-run; no container rebuild.
RUN uv pip install --python /opt/venv --no-deps -e /app

# Hand /opt/venv to the precis user. uv run's sync pass needs write
# access at runtime to refresh metadata; in the runtime stage the venv
# stays root-owned for safety.
RUN chown -R precis:precis /opt/venv

USER precis

# Drop into a shell by default. Override with `--command pytest` etc.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]
