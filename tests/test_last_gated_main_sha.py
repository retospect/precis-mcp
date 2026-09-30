"""Tests for ``scripts/last-gated-main-sha`` — the range start check.yml's lane
picker uses on a main push (backlog/main-stays-gated.md).

The defect it closes: the concurrency group cancels superseded main runs, so a
qland burst leaves pushes with no verdict; scoping the lane decision to
``github.event.before`` then lets a docs-only push take the docs lane while main
carries src no shard ran. So the assertions here are all about what is
*refused*: a partially-cancelled matrix, a docs-lane sha with no shards at all,
and an unanswerable ``gh`` — each must decline to name a gated sha, since an
empty answer makes check.yml gate fully.

``gh`` and ``git`` are replaced at the module boundary (``_run``).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "last-gated-main-sha"

GATED = "aaaa111100000000000000000000000000000000"
BURST = "bbbb222200000000000000000000000000000000"
DOCS = "cccc333300000000000000000000000000000000"


def _load() -> ModuleType:
    # Loaded from a `.py`-suffixed copy, not the dotless executable: testmon
    # fingerprints by extension and IndexErrors on a dotless file, which
    # INTERNALERRORs any `--impacted` run touching this test (gr450298) — same
    # convention as tests/test_main_ci_status.py.
    tmp_copy = (
        Path(tempfile.mkdtemp(prefix="last_gated_py_")) / "last_gated_main_sha.py"
    )
    shutil.copyfile(SCRIPT, tmp_copy)
    loader = importlib.machinery.SourceFileLoader("last_gated_main_sha", str(tmp_copy))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _shards(conclusion: str, n: int = 6, *, hours_ago: float = 1.0) -> str:
    """A check-runs payload: `[conclusion, completed_at]` per shard.

    Shards finish at slightly different times, as a real matrix does — spread
    backwards from `hours_ago` so the newest is exactly `hours_ago` old. The
    age of a matrix is the age of its SLOWEST member; staggering them is what
    makes that assertion mean something.
    """
    newest = datetime.now(UTC) - timedelta(hours=hours_ago)
    return json.dumps(
        [
            [conclusion, (newest - timedelta(minutes=2 * i)).isoformat()]
            for i in range(n)
        ]
    )


def _stub(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, checks: dict[str, str]
) -> None:
    """Feed a fixed first-parent walk and a per-sha check-runs answer."""
    order = list(checks)

    def fake_run(*args: str) -> str | None:
        if args[0] != "gh":
            return "\n".join(order)
        sha = next((s for s in order if s in args[2]), None)
        return checks.get(sha or "")

    monkeypatch.setattr(mod, "_run", fake_run)


def test_all_shards_green_is_the_gated_sha(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    _stub(mod, monkeypatch, {GATED: _shards("success")})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_partially_cancelled_matrix_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One green shard beside five cancelled ones is the exact state a burst
    produces. Counting it would hand check.yml a range start that skips
    everything the cancelled shards never ran."""
    mod = _load()
    stamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    mixed = json.dumps([["success", stamp]] + [["cancelled", stamp]] * 5)
    _stub(mod, monkeypatch, {BURST: mixed, GATED: _shards("success")})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_docs_lane_sha_has_no_shards_and_is_walked_past(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A docs-only push runs `test-docs`, no `test-linux` at all. An empty
    conclusion list is not unanimous success — it is no verdict."""
    mod = _load()
    _stub(mod, monkeypatch, {DOCS: "[]", GATED: _shards("success")})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_nothing_gated_in_the_window_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty output is what makes check.yml gate fully — the safe direction."""
    mod = _load()
    _stub(mod, monkeypatch, {BURST: _shards("cancelled"), DOCS: "[]"})
    assert mod.main([]) == 0
    assert capsys.readouterr().out == ""


def test_limit_bounds_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load()
    seen: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> str | None:
        seen.append(args)
        return "" if args[0] != "gh" else None

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main(["--limit", "3", "--start", "deadbeef"]) == 0
    assert seen[0] == ("git", "rev-list", "--first-parent", "-n3", "deadbeef")


@pytest.mark.skipif(sys.platform == "win32", reason="stub gh is a POSIX sh script")
def test_missing_gh_is_silent_and_exits_zero(tmp_path: Path) -> None:
    stub = tmp_path / "gh"
    stub.write_text("#!/bin/sh\nexit 4\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "--limit", "2"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=60,
    )
    assert cp.returncode == 0
    assert cp.stdout == ""


# ── --age-hours: the number scripts/ship --quick refuses past ───────────────
#
# Reto 2026-09-30, "a day or two" → warn at 24h, refuse at 48h. These pin the
# arithmetic and, more importantly, the direction of every unknown: a drift
# guard that refuses on a lookup it could not answer would let a GitHub outage
# stop the whole fleet from landing, which is worse than the ungated main it
# is guarding against.


def test_age_hours_reports_the_newest_verdict_age(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    _stub(mod, monkeypatch, {GATED: _shards("success", hours_ago=30.0)})
    assert mod.main(["--age-hours"]) == 0
    assert abs(float(capsys.readouterr().out.strip()) - 30.0) < 0.2


def test_age_is_the_slowest_shard_not_the_fastest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The matrix is not done until its last shard reports. Taking the oldest
    stamp would age the verdict by however long the slowest shard took and
    could tip a healthy main over the refusal line."""
    mod = _load()
    now = datetime.now(UTC)
    payload = json.dumps(
        [
            ["success", (now - timedelta(hours=50)).isoformat()],
            ["success", (now - timedelta(hours=2)).isoformat()],
        ]
    )
    _stub(mod, monkeypatch, {GATED: payload})
    assert mod.main(["--age-hours"]) == 0
    assert abs(float(capsys.readouterr().out.strip()) - 2.0) < 0.2


def test_age_hours_prints_nothing_when_nothing_is_gated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No answer, not a large one. scripts/ship reads empty as "do not refuse
    on this" — the guard only ever blocks on a number it actually has."""
    mod = _load()
    _stub(mod, monkeypatch, {BURST: _shards("cancelled")})
    assert mod.main(["--age-hours"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_age_hours_prints_nothing_when_gh_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()

    def fake_run(*args: str) -> str | None:
        return None if args[0] == "gh" else GATED

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main(["--age-hours"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_an_unreadable_timestamp_is_unknown_not_older(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A green sha whose stamps will not parse ends the walk empty-handed
    rather than falling through to an older gated sha. Reporting the older
    one's age would overstate the drift and could refuse a ship that should
    have gone through."""
    mod = _load()
    unreadable = json.dumps([["success", "not-a-timestamp"]] * 6)
    _stub(
        mod,
        monkeypatch,
        {BURST: unreadable, GATED: _shards("success", hours_ago=99.0)},
    )
    assert mod.main(["--age-hours"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_age_mode_does_not_change_which_sha_counts_as_gated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both modes walk with the same predicate; only the thing printed differs.
    A partially-cancelled matrix is no more a verdict for the drift guard than
    it is for the lane picker."""
    mod = _load()
    stamp = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    mixed = json.dumps([["success", stamp]] + [["cancelled", stamp]] * 5)
    _stub(
        mod,
        monkeypatch,
        {BURST: mixed, GATED: _shards("success", hours_ago=12.0)},
    )
    assert mod.main(["--age-hours"]) == 0
    assert abs(float(capsys.readouterr().out.strip()) - 12.0) < 0.2
