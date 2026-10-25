"""gr515069: container tool caches must live off the bind-mounted worktree.

``scripts/test --typecheck`` runs mypy in a ``compose run`` container with
the worktree bind-mounted at /app. With the default cache dir mypy wrote
``/app/.mypy_cache`` — the SAME directory host ``uv run mypy`` uses — and
alternating host/container runs corrupt each other's cache (mypy exits 2
with a bare "INTERNAL ERROR", no traceback). Every gate-facing service
therefore pins MYPY_CACHE_DIR (and RUFF_CACHE_DIR, same failure mode) into
the per-worktree ``gate-cache`` volume, outside /app. Asserted on both
compose files: the mirror variant is the fallback stack and must not drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = (
    ROOT / "docker" / "dev" / "compose.yaml",
    ROOT / "docker" / "dev" / "compose.mirror.local.yaml",
)
#: The services any mypy/ruff run can land in: `compose run` dev containers
#: (scripts/test --typecheck, ship's bind fallback) and the warm gate.
GATE_SERVICES = ("precis-dev", "precis-gate")
CACHE_ENVS = ("MYPY_CACHE_DIR", "RUFF_CACHE_DIR")


@pytest.mark.parametrize("compose_file", COMPOSE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("service", GATE_SERVICES)
@pytest.mark.parametrize("key", CACHE_ENVS)
def test_gate_service_tool_cache_off_worktree(
    compose_file: Path, service: str, key: str
) -> None:
    doc = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    env = doc["services"][service].get("environment", {})
    cache_dir = env.get(key, "")
    assert cache_dir.startswith("/home/precis/.cache/"), (
        f"{compose_file.name}: {service} {key}={cache_dir!r} — must live on "
        f"the per-worktree gate-cache volume, never under /app (gr515069)"
    )
    volumes = doc["services"][service].get("volumes", [])
    assert any(v.startswith("gate-cache:") for v in volumes), (
        f"{compose_file.name}: {service} must mount the gate-cache volume "
        f"or {key} points at the container overlay and never persists"
    )
