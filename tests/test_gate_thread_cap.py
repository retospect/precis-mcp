"""gr345784: the local gate caps BLAS/OpenMP threads per xdist worker.

Uncapped, every worker that touches torch/numpy sizes its pool to all
container cores, so ``-n 6`` on 15 cores runs ~190 runnable threads and a
torch test that takes seconds alone crawls for tens of minutes under
coverage — the hour-long "wedged" gate with no wedged test to name. Both
entry points must carry the cap: ``scripts/test`` (bind-mount container,
passes ``THREAD_CAP_ENV``) and the warm ``precis-gate`` service (ship execs
into it without passing that array, so the cap lives on the service).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAP_KEYS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


@pytest.mark.parametrize("key", CAP_KEYS)
def test_scripts_test_passes_the_cap(key: str) -> None:
    text = (ROOT / "scripts" / "test").read_text(encoding="utf-8")
    assert f"-e {key}=1" in text
    assert '"${THREAD_CAP_ENV[@]}"' in text


@pytest.mark.parametrize("key", CAP_KEYS)
def test_warm_gate_service_carries_the_cap(key: str) -> None:
    text = (ROOT / "docker" / "dev" / "compose.yaml").read_text(encoding="utf-8")
    gate = text[text.index("  precis-gate:") :]
    assert f'{key}: "1"' in gate
