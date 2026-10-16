"""Shard partition and CI job-budget invariants for .github/workflows/check.yml."""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import shard_index

WORKFLOW = (
    Path(__file__).parent.parent / ".github" / "workflows" / "check.yml"
).read_text(encoding="utf-8")

CAP = 40  # GitHub Pro standard-runner concurrency


def _shards() -> int:
    m = re.search(r"shard: \[([\d, ]+)\]", WORKFLOW)
    assert m, "no shard matrix in check.yml"
    return len(m.group(1).split(","))


def test_shards_partition_every_test_exactly_once() -> None:
    ids = [f"tests/test_mod{i % 97}.py::test_case_{i}[p{i % 7}]" for i in range(6000)]
    n = _shards()
    buckets = [{t for t in ids if shard_index(t, n) == k} for k in range(n)]
    assert sum(len(b) for b in buckets) == len(ids)
    assert set().union(*buckets) == set(ids)
    assert all(buckets)


def test_workflow_shard_count_is_spelled_consistently() -> None:
    n = _shards()
    assert f"--shard ${{{{ matrix.shard }}}}/{n}" in WORKFLOW


def test_nightly_plus_two_gates_fit_the_cap() -> None:
    n = _shards()
    gate = 2 + n  # plan + lint + shards
    nightly = 1 + 1 + 1 + 4 + 2 * n  # plan, lint, coverage-db, test-other, 2 pythons
    assert nightly + 2 * gate <= CAP


def test_pinned_weights_match_the_shard_count_and_partition() -> None:
    from tests.conftest import _SHARD_PIN_N, _SHARD_PINS

    n = _shards()
    assert n == _SHARD_PIN_N, (
        "regenerate tests/shard_weights.json (scripts/shard-weights)"
    )
    assert all(0 <= v < n for v in _SHARD_PINS.values())
    ids = list(_SHARD_PINS) + [f"tests/test_new.py::test_{i}" for i in range(500)]
    assert all(0 <= shard_index(t, n) < n for t in ids)
    for t, v in _SHARD_PINS.items():
        assert shard_index(t, n) == v
    # a different N ignores the pins and still covers every id once
    assert {shard_index(t, 5) for t in ids} == set(range(5))
