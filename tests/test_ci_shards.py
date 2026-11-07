"""Shard partition and CI job-budget invariants for .github/workflows/check.yml."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

from tests._script import load_script
from tests.conftest import shard_index

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = (REPO / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")

SCRIPT = REPO / "scripts" / "ci-shard-plan"
LIB = REPO / "scripts" / "lib"
PARTITION = load_script("shard_partition", LIB / "shard_partition.py")
CAP = 40  # GitHub Pro standard-runner concurrency


def _plan() -> ModuleType:
    # `.py` copies: testmon IndexErrors on dotless files (see
    # test_last_gated_main_sha). The copy's runpy import resolves
    # ``<tmp>/lib/shard_partition.py``, so stage the lib file beside it.
    tmp = Path(tempfile.mkdtemp(prefix="ci_shard_plan_"))
    (tmp / "lib").mkdir()
    shutil.copyfile(SCRIPT, tmp / "ci_shard_plan.py")
    shutil.copyfile(LIB / "shard_partition.py", tmp / "lib" / "shard_partition.py")
    spec = importlib.util.spec_from_file_location(
        "ci_shard_plan", tmp / "ci_shard_plan.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The copy's ROOT resolves off the tmpdir; point it back at the repo so
    # its git ls-files list is the real one. Its shard_partition copy reads
    # no pins file there — fine: FORCE runs at untabled counts partition by
    # plain crc32 anyway.
    setattr(mod, "ROOT", REPO)
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
    A forced N with no table in shard_weights.json (e.g. 20) partitions by
    plain crc32 on the file path (conftest.shard_index); tabled counts
    (GATE_SHARD_COUNTS) run pinned."""
    monkeypatch.setenv("FORCE_SHARDS", forced)
    capsys.readouterr()
    assert _PLAN.main() == 0
    out = capsys.readouterr().out
    n_line = next(line for line in out.splitlines() if line.startswith("n="))
    assert int(n_line.split("=", 1)[1]) == expected
    list_line = next(line for line in out.splitlines() if line.startswith("list="))
    assert len(json.loads(list_line.split("=", 1)[1])) == expected
    files_line = next(
        line for line in out.splitlines() if line.startswith("shard_files=")
    )
    files = json.loads(files_line.split("=", 1)[1])
    assert sorted(files, key=int) == [str(k) for k in range(1, expected + 1)]


def test_test_files_falls_back_to_walk_when_git_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the ship container the worktree's `.git` points outside the mount,
    so `git ls-files` exits 128; test_files() must walk tests/ instead."""
    real_run = _PLAN.subprocess.run
    try:
        real = real_run(
            ["git", "ls-files", "-z", "--", "tests"],
            cwd=REPO,
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout
        tracked: list[str] | None = sorted(
            p for p in real.split("\0") if _PLAN.TEST_FILE.search(p)
        )
    except (subprocess.CalledProcessError, OSError):
        tracked = None  # git unavailable here too; skip the equality check

    def boom(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(128, ["git"])

    monkeypatch.setattr(_PLAN.subprocess, "run", boom)
    walked = _PLAN.test_files()
    assert walked
    assert "tests/test_ci_shards.py" in walked
    assert all(_PLAN.TEST_FILE.search(p) for p in walked)
    if tracked is not None:
        # Superset, not equality: the walk also sees untracked test files.
        assert set(tracked) <= set(walked)


def test_workflow_follows_the_plan_output() -> None:
    assert "shard: ${{ fromJSON(needs.plan.outputs.shard_list) }}" in WORKFLOW
    assert "--shard ${{ matrix.shard }}/${{ needs.plan.outputs.shards }}" in WORKFLOW
    # Each shard collects only its own files: plan emits the partition map,
    # test-linux reads its bucket out of it (jq, never word-splitting).
    assert "shard_files: ${{ steps.shards.outputs.shard_files }}" in WORKFLOW
    assert "SHARD_FILES: ${{ needs.plan.outputs.shard_files }}" in WORKFLOW
    assert "jq -r --arg k" in WORKFLOW


def test_gate_shards_deselect_nightly_full_shape_runs_them() -> None:
    """The `nightly` marker (release-train §3, Reto 2026-10-24: the >240 s
    tests that don't split move to the nightly run) is deselected in every
    gate-shape shard run — but only via this conditional, so the nightly
    full matrix still runs them."""
    assert (
        "needs.plan.outputs.full != 'true' && ' -m \"not nightly\"' || ''" in WORKFLOW
    )


def test_shards_partition_every_test_exactly_once() -> None:
    ids = [f"tests/test_mod{i % 97}.py::test_case_{i}[p{i % 7}]" for i in range(6000)]
    for n in COUNTS:
        buckets = [{t for t in ids if shard_index(t, n) == k} for k in range(n)]
        assert sum(len(b) for b in buckets) == len(ids)
        assert set().union(*buckets) == set(ids)


def test_partition_is_total_and_file_cohesive() -> None:
    """Every id maps to exactly one shard, and every test FILE to exactly
    one: two node ids of one file — different tests, classes, parametrized
    ids with spaces — always agree (me580555: module fixtures build once
    per gate, not once per shard)."""
    ids = [
        f"tests/test_mod{i % 97}.py::TestK{j}::test_case_{i}[p {i % 7}]"
        for i in range(6000)
        for j in range(2)
    ]
    for n in COUNTS:
        by_file: dict[str, int] = {}
        for t in ids:
            idx = shard_index(t, n)
            assert 0 <= idx < n
            path = t.split("::", 1)[0]
            assert by_file.setdefault(path, idx) == idx


def test_ci_shard_files_matches_conftest_partition() -> None:
    """scripts/lib/shard_partition.py is conftest's shard_index duplicated
    for the workflow side (pytest can't import scripts/): pinned and
    unpinned ids agree, and the script's emitted file map puts every test
    file in exactly the bucket conftest would."""
    n = _PLAN.SHARDS
    pins = PARTITION.load_pins().get(n, {})
    samples = [f"{p}::test_x" for p in list(pins)[:25]] + [
        f"tests/test_unpinned_{i}.py::test_y" for i in range(25)
    ]
    assert len(samples) == 50
    for nid in samples:
        assert PARTITION.shard_index(nid, n) == shard_index(nid, n)
    out = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO,
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "FORCE_SHARDS": ""},
    ).stdout
    line = next(line for line in out.splitlines() if line.startswith("shard_files="))
    files_map = json.loads(line.split("=", 1)[1])
    for k_s, files in files_map.items():
        assert files, f"shard {k_s} got no files"
        for f in files:
            assert shard_index(f, n) == int(k_s) - 1
    all_files = sorted(f for files in files_map.values() for f in files)
    assert len(all_files) == len(set(all_files))
    assert len(all_files) > 2000  # the suite's ~2450 test files


def test_weights_table_balances_within_tolerance() -> None:
    """The shipped table's predicted shard seconds (from its embedded
    per-file totals) stay within 5% of the mean — the LPT pack's target;
    no network needed."""
    data = json.loads(
        (REPO / "tests" / "shard_weights.json").read_text(encoding="utf-8")
    )
    seconds = data["seconds"]
    for n_s, table in data["tables"].items():
        n = int(n_s)
        load = [0.0] * n
        for path, k in table.items():
            load[k] += seconds[path]
        mean = sum(load) / n
        assert max(load) <= mean * 1.05, (
            f"n={n} predicted max {max(load):.0f}s vs mean {mean:.0f}s — "
            "regenerate tests/shard_weights.json (scripts/shard-weights)"
        )


def test_shard_weights_regenerates_from_junit(tmp_path: Path) -> None:
    """Two junit files are one run; per-file sums drive a predictable
    longest-first pack (10→0, 8→1, 4→1, 2→0 = 12/12 at n=2)."""
    out = tmp_path / "weights.json"
    fixtures = sorted((REPO / "tests" / "fixtures").glob("junit-shard-*.xml"))
    assert len(fixtures) == 2, "add the two tiny junit fixtures"
    subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "shard-weights"),
            "--out",
            str(out),
            "--ns",
            "2",
            *[str(f) for f in fixtures],
        ],
        cwd=REPO,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["tables"]["2"] == {
        "tests/test_ci_shards.py": 0,
        "tests/test_posix_only_guards.py": 1,
        "tests/test_text_io_encoding.py": 1,
        "tests/test_type_ignore_ratchet.py": 0,
    }
    assert data["seconds"]["tests/test_ci_shards.py"] == 10.0


def test_job_budget_fits_the_cap() -> None:
    # plan + lint + 10 shards = 12 jobs for every gate/release run.
    gate = 2 + _PLAN.SHARDS
    assert gate == 12
    # plan + lint + coverage-db + test-other (2 Pythons x 2 macOS shards)
    # + test-linux (2 Pythons x 10 shards) = 27.
    nightly = 1 + 1 + 1 + 2 * 2 + 2 * _PLAN.SHARDS
    assert nightly == 27
    # A main push runs the lint_only lane = plan + lint only; grounded by
    # test-linux's `if` skipping lint_only rather than a bare literal.
    assert "needs.plan.outputs.lint_only != 'true'" in WORKFLOW
    main_push = 2
    # release (12) + two harness gates (24) + a main lint run (2) = 38.
    assert gate + 2 * gate + main_push == 38 <= CAP
    # nightly (27) + one gate (12) = 39.
    assert nightly + gate == 39 <= CAP
    # Three gates at once is the max; a fourth queues.
    assert 3 * gate <= CAP < 4 * gate


def test_pinned_weights_cover_every_shard_count() -> None:
    from tests.conftest import _SHARD_PINS

    assert set(_SHARD_PINS) == set(COUNTS), (
        "regenerate tests/shard_weights.json (scripts/shard-weights)"
    )
    for n, pins in _SHARD_PINS.items():
        assert all(0 <= v < n for v in pins.values())
        for path, v in pins.items():
            assert shard_index(path, n) == v
        paths = list(pins) + [f"tests/test_new_{i}.py" for i in range(500)]
        assert {shard_index(t, n) for t in paths} == set(range(n))
    # a count without a table ignores the pins and still covers every file once
    assert {shard_index(t, 5) for t in paths} == set(range(5))


def test_no_single_file_eats_a_shard() -> None:
    """A test FILE is the partition's atomic unit: one fatter than a shard's
    fair share (mean test-seconds at n=10) can never be packed under the
    mean, so it must be split per module, not pinned whole — the file-level
    form of gr495187's 31-minute single-test pin."""
    data = json.loads(
        (REPO / "tests" / "shard_weights.json").read_text(encoding="utf-8")
    )
    seconds = data.get("seconds")
    assert seconds, "regenerate tests/shard_weights.json (scripts/shard-weights)"
    mean = sum(seconds.values()) / _PLAN.SHARDS
    over = {f: s for f, s in seconds.items() if s > mean}
    assert not over, (
        f"files over one shard's fair share ({mean:.0f}s); split the module "
        f"per case or speed it up: {over}"
    )
