"""Shard partition and CI job-budget invariants for .github/workflows/check.yml."""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path
from types import ModuleType

from tests.conftest import shard_index

WORKFLOW = (
    Path(__file__).parent.parent / ".github" / "workflows" / "check.yml"
).read_text(encoding="utf-8")

SCRIPT = Path(__file__).parent.parent / "scripts" / "ci-shard-plan"
CAP = 40  # GitHub Pro standard-runner concurrency


def _plan() -> ModuleType:
    # `.py` copy: testmon IndexErrors on dotless files (see test_last_gated_main_sha).
    tmp = Path(tempfile.mkdtemp(prefix="ci_shard_plan_")) / "ci_shard_plan.py"
    shutil.copyfile(SCRIPT, tmp)
    spec = importlib.util.spec_from_file_location("ci_shard_plan", tmp)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PLAN = _plan()
COUNTS = (_PLAN.LOADED, _PLAN.IDLE)


def test_choose_shard_count() -> None:
    assert _PLAN.choose(False, 0) == 12
    assert _PLAN.choose(False, _PLAN.IDLE_MAX) == 12
    assert _PLAN.choose(False, _PLAN.IDLE_MAX + 1) == 10
    assert _PLAN.choose(False, None) == 10  # unreadable load -> safe default
    assert _PLAN.choose(True, 0) == 10  # nightly budget assumes 10


def test_workflow_follows_the_plan_output() -> None:
    assert "shard: ${{ fromJSON(needs.plan.outputs.shard_list) }}" in WORKFLOW
    assert "--shard ${{ matrix.shard }}/${{ needs.plan.outputs.shards }}" in WORKFLOW


def test_shards_partition_every_test_exactly_once() -> None:
    ids = [f"tests/test_mod{i % 97}.py::test_case_{i}[p{i % 7}]" for i in range(6000)]
    for n in COUNTS:
        buckets = [{t for t in ids if shard_index(t, n) == k} for k in range(n)]
        assert sum(len(b) for b in buckets) == len(ids)
        assert set().union(*buckets) == set(ids)
        assert all(buckets)


def test_job_budget_fits_the_cap() -> None:
    loaded, idle = COUNTS
    # plan + lint + coverage-db + test-other (macOS x 2 Pythons; the Windows
    # leg was dropped 2026-10-17) + test-linux (2 Pythons x `loaded` shards).
    nightly = 1 + 1 + 1 + 2 + 2 * loaded
    assert nightly + (2 + loaded) <= CAP  # nightly + one loaded gate
    assert 3 * (2 + loaded) <= CAP  # three loaded gates
    # Idle: this 12-shard gate beside IDLE_MAX other gate-shaped runs. Beside
    # the nightly it can exceed the cap by a job or two; jobs queue, not fail.
    assert (2 + idle) * (1 + _PLAN.IDLE_MAX) <= CAP


def test_pinned_weights_cover_every_shard_count() -> None:
    from tests.conftest import _SHARD_PINS

    assert set(_SHARD_PINS) == set(COUNTS), (
        "regenerate tests/shard_weights.json (scripts/shard-weights)"
    )
    for n, pins in _SHARD_PINS.items():
        assert all(0 <= v < n for v in pins.values())
        for t, v in pins.items():
            assert shard_index(t, n) == v
        ids = list(pins) + [f"tests/test_new.py::test_{i}" for i in range(500)]
        assert {shard_index(t, n) for t in ids} == set(range(n))
    # a count without a table ignores the pins and still covers every id once
    assert {shard_index(t, 5) for t in ids} == set(range(5))
