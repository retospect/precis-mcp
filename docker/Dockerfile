# syntax=docker/dockerfile:1.7
# precis-mcp — multi-stage Dockerfile (deps / models / builder / runtime / dev)
#
# Layering goal: source edits → ~5-second rebuild (only Stage 3 reruns).
# Only `pyproject.toml` / `uv.lock` / marker / bge-m3 version changes
# trigger the 1.5 GB model re-download. See the rationale below.
#
# Targets:
#   deps     — venv populated from lockfile, no source (intermediate)
#   models   — pre-populates marker + bge-m3 weights (intermediate)
#   builder  — installs precis-mcp itself on top of models (intermediate)
#   runtime  — production image; ENTRYPOINT precis (serve|watch|worker|...)
#   dev      — adds pytest, ruff, mypy, plantuml, psql on top of runtime
#
# Build context is the precis-mcp repo root. Build the prod image:
#   docker build --target runtime -t precis-mcp:latest -f docker/Dockerfile .
#
# Build the dev image:
#   docker build --target dev -t precis-mcp:dev -f docker/Dockerfile .
#
# Architecture note: builds native to the host (ARM64 on Apple Silicon,
# AMD64 on intel hosts). Multi-arch via `docker buildx --platform` for
# release builds. See docs/decisions/0004-multi-stage-dockerfile.md and
# docs/decisions/0009-dockerfile-relocation-container-first.md.

ARG PYTHON_DIGEST=sha256:d193c6f51a7dbd10395d6328de3a7edb0516fb0608ca138036576f574c3e07d2
ARG UV_VERSION=0.11.14

# =============================================================================
# Stage 1 (deps): install dependencies from the lockfile into /opt/venv.
# This stage's inputs are ONLY pyproject.toml + uv.lock — touching any
# `.py` under `src/precis/**` does NOT invalidate this layer, so the
# ~1.5 GB model bake in Stage 2 also stays cached across source edits.
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
# (--no-install-project). Stage 4 (builder) does that on top.
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
# Stage 2 (models): pre-populate the HuggingFace cache with the model
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

RUN mkdir -p "${HF_HOME}" "${MODEL_CACHE_DIR}" && \
    /opt/venv/bin/python -c \
        "from marker.models import create_model_dict; create_model_dict()" && \
    /opt/venv/bin/python -c \
        "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"

# =============================================================================
# Stage 3 (builder): install precis-mcp itself on top of cached deps + models.
# This stage's inputs are the full repo, so it reruns on every source edit
# — but the install is cheap (`--no-deps` because all deps already in the
# venv) and the heavy marker/bge-m3 download from Stage 2 is preserved.
# =============================================================================
FROM models AS builder

COPY . /build/precis-mcp/
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv pip install --python /opt/venv --no-deps "/build/precis-mcp[all]"

# =============================================================================
# Stage 2 (runtime): production image, lean
# =============================================================================
FROM python:3.12-slim-bookworm@${PYTHON_DIGEST} AS runtime

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

COPY --from=builder /opt/venv /opt/venv

# Non-root user + pre-created cache dirs (so volume mounts inherit perms).
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

# Baked-in HuggingFace cache (marker surya + bge-m3). ~3.8 GB on disk;
# mmap'd lazily on first embed / first PDF, so RAM behaviour is unchanged
# from the previous lazy-download model. Must come after `useradd precis`
# so the --chown can resolve the user. See
# docs/design/bake-models-into-image.md.
COPY --from=models --chown=precis:precis /opt/precis/models /opt/precis/models

# Secrets reader (lives next to this Dockerfile)
COPY docker/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER precis
WORKDIR /data

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["precis", "serve"]

# =============================================================================
# Stage 3 (dev): runtime + developer tooling + editable source
# =============================================================================
FROM runtime AS dev

USER root

# Tell uv to reuse /opt/venv as the project env (instead of creating
# /app/.venv, which would pollute the bind-mounted host repo).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# Dev system tools
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

# Install dev Python deps into the same venv
ARG UV_VERSION
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    pip install --no-cache-dir "uv==${UV_VERSION}" && \
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

# Bring the source tree in for live editing convenience; bind-mounts in
# compose will override this so it's just a sane fallback.
COPY --chown=precis:precis . /app
WORKDIR /app

# Editable install of precis-mcp itself. Replaces the snapshot from the
# builder stage. The resulting .pth file in /opt/venv references
# /app/src/precis — at runtime the bind mount makes /app live, so
# `import precis` resolves to the host source. Edit and re-run; no
# container rebuild.
RUN uv pip install --python /opt/venv --no-deps -e /app

# Hand /opt/venv to the precis user. uv run's sync pass needs write
# access at runtime to refresh metadata; in the runtime stage the venv
# stays root-owned for safety.
RUN chown -R precis:precis /opt/venv

USER precis

# Drop into a shell by default. Override with `--command pytest` etc.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]
