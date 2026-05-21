# syntax=docker/dockerfile:1.7
# precis-mcp — multi-stage Dockerfile (builder / runtime / dev)
#
# Targets:
#   builder  — installs deps into a venv (intermediate, not used directly)
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
# Stage 1 (builder): install dependencies into /opt/venv
# =============================================================================
FROM python:3.12-slim-bookworm@${PYTHON_DIGEST} AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_COLOR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG UV_VERSION
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        git \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /build
COPY . /build/precis-mcp/

# Install precis-mcp[all] into a fresh venv at /opt/venv. Editable install
# is fine here because the runtime stage copies the venv but not /build,
# so the editable path is replaced by a wheel-style install in stage 2.
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv "/build/precis-mcp[all]"

# =============================================================================
# Stage 2 (runtime): production image, lean
# =============================================================================
FROM python:3.12-slim-bookworm@${PYTHON_DIGEST} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_COLOR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}"

# Runtime libs only (no build-essential)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
        tini

COPY --from=builder /opt/venv /opt/venv

# Non-root user + pre-created cache dirs (so volume mounts inherit perms)
RUN useradd -m -u 1000 precis && \
    mkdir -p /data /inbox /home/precis/.cache && \
    chown -R precis:precis /data /inbox /home/precis/.cache

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

# Install dev Python deps into the same venv
ARG UV_VERSION
RUN pip install --no-cache-dir "uv==${UV_VERSION}" && \
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
