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


def test_nightly_plus_one_gate_fits_the_cap() -> None:
    n = _shards()
    gate = 2 + n  # plan + lint + shards
    m = re.search(r"max-parallel: \$\{\{ [^}]*&& (\d+) \|\| (\d+) \}\}", WORKFLOW)
    assert m, "test-linux needs a nightly max-parallel"
    nightly_linux, gate_linux = int(m.group(1)), int(m.group(2))
    assert gate_linux >= n
    nightly_peak = 1 + 1 + 1 + 4 + nightly_linux  # plan, lint, coverage-db, test-other
    assert nightly_peak + gate <= CAP
