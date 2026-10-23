"""Gate/dev containers carry a hard per-container memory ceiling (2026-10-21).

The gate-slot semaphore (``scripts/lib/gate-slot.sh``) bounds how many gates
run at once; ``mem_limit`` on the gate-facing services bounds ONE runaway, so
it dies inside its own cgroup (exit 137, named by ``gate_oom_note``) instead
of the VM OOM-killing a random sibling's gate (gr202193). Sized ~1.7x the
dev-run's measured ~11 GiB peak on the OrbStack VM (2026-10-21) — CPU, not
memory, is the slot limit now, so no CPU cap. Both compose files must carry it: the mirror
variant is the fallback when the default registry is unreachable, and a gate
under it would otherwise run unbounded.
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
#: The services gate workloads run in: `compose run` dev containers
#: (scripts/test, ship's bind fallback) and the warm gate (scripts/ship).
GATE_SERVICES = ("precis-dev", "precis-gate")


@pytest.mark.parametrize("compose_file", COMPOSE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("service", GATE_SERVICES)
def test_gate_service_has_mem_limit(compose_file: Path, service: str) -> None:
    doc = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    limit = doc["services"][service].get("mem_limit")
    assert limit == "24g", (
        f"{compose_file.name}: {service} mem_limit={limit!r} — the per-container "
        "OOM bound must stay ~24 GiB on every gate-facing service (gr202193)"
    )
