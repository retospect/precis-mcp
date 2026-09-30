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


def _shards(conclusion: str, n: int = 6) -> str:
    return json.dumps([conclusion] * n)


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
    mixed = json.dumps(["success"] + ["cancelled"] * 5)
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
