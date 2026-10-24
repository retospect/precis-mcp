"""Shard partition and CI job-budget invariants for .github/workflows/check.yml."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

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
COUNTS = (_PLAN.SHARDS,)


def test_shard_count_is_fixed_10() -> None:
    """Fixed 10 for every shard run (Reto 2026-10-23: "no fewer" — the
    release-train spec's job-budget arithmetic assumes it)."""
    assert _PLAN.SHARDS == 10


@pytest.mark.parametrize(
    ("forced", "expected"),
    [("", 10), ("6", 10), ("10", 10), ("12", 12), ("20", 20)],
)
def test_force_shards_floors_at_10(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forced: str,
    expected: int,
) -> None:
    """FORCE_SHARDS stays as a measurement lever but never goes below 10.
    Widths above 10 run unpinned: shard_weights.json has only the 10 table,
    so a forced N > 10 partitions by plain crc32 (conftest.shard_index)."""
    monkeypatch.setenv("FORCE_SHARDS", forced)
    capsys.readouterr()
    assert _PLAN.main() == 0
    out = capsys.readouterr().out
    n_line = next(line for line in out.splitlines() if line.startswith("n="))
    assert int(n_line.split("=", 1)[1]) == expected
    list_line = next(line for line in out.splitlines() if line.startswith("list="))
    assert len(json.loads(list_line.split("=", 1)[1])) == expected


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
    # plan + lint + 10 shards = 12 jobs for every gate/release run.
    gate = 2 + _PLAN.SHARDS
    assert gate == 12
    # plan + lint + coverage-db + test-other (macOS x 2 Pythons; the Windows
    # leg was dropped 2026-10-17) + test-linux (2 Pythons x 10 shards) = 25.
    nightly = 1 + 1 + 1 + 2 + 2 * _PLAN.SHARDS
    assert nightly == 25
    # A main push runs the lint_only lane = plan + lint only; grounded by
    # test-linux's `if` skipping lint_only rather than a bare literal.
    assert "needs.plan.outputs.lint_only != 'true'" in WORKFLOW
    main_push = 2
    # release (12) + two harness gates (24) + a main lint run (2) = 38.
    assert gate + 2 * gate + main_push == 38 <= CAP
    # nightly (25) + one gate (12) = 37.
    assert nightly + gate == 37 <= CAP
    # Three gates at once is the max; a fourth queues.
    assert 3 * gate <= CAP < 4 * gate


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


#: Most of one test-linux shard's budget a single test may take. A test
#: pinned whole to one shard can't be balanced away: an 8-seed loop at 31 min
#: held its shard at the 40 min cap (gr495187). 0.3 is the tightest round
#: share green at the 2026-10-23 regeneration (r2 nano baseline 678 s = 28%);
#: four tests sit at 20-28% (481-678 s) and are the next to split or speed up.
MAX_TEST_SHARE = 0.3


def test_no_single_test_eats_a_shard() -> None:
    import re

    match = re.search(
        r"\n  test-linux:\n(?:    .*\n)*?    timeout-minutes: (\d+)", WORKFLOW
    )
    assert match is not None, "test-linux job lost its timeout-minutes"
    cap_s = int(match.group(1)) * 60 * MAX_TEST_SHARE
    data = json.loads(
        (Path(__file__).parent / "shard_weights.json").read_text(encoding="utf-8")
    )
    seconds = data.get("seconds")
    assert seconds, "regenerate tests/shard_weights.json (scripts/shard-weights)"
    over = {t: s for t, s in seconds.items() if s > cap_s}
    assert not over, (
        f"tests over {MAX_TEST_SHARE:.0%} of a shard's budget ({cap_s:.0f} s); "
        f"split them per case (parametrize) or speed them up: {over}"
    )
