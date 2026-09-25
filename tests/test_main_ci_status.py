"""Tests for ``scripts/main-ci-status`` — the "is main red on CI, and who owns
it" read (gr346534).

The script is a ``gh``-backed watcher plus an ownership signal: on a red
post-merge run it names the failing jobs and either the in-flight worktree
whose ``.claude/purpose`` claims the fix, or the exact purpose line to write
so siblings stop shipping the same fix three times. ``gh`` is replaced by a
stub on PATH; the inflight scan is fed directly.
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

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "main-ci-status"

RED_RUN = {
    "databaseId": 1,
    "headSha": "8b881979abcdef0123456789",
    "conclusion": "failure",
    "status": "completed",
    "createdAt": "2026-09-18T11:43:33Z",
}
GREEN_RUN = {**RED_RUN, "databaseId": 2, "conclusion": "success"}
PENDING_RUN = {
    **RED_RUN,
    "databaseId": 3,
    "headSha": "840b178fcafe",
    "conclusion": "",
    "status": "in_progress",
}


def _load() -> ModuleType:
    # scripts/main-ci-status has no .py suffix (it's an executable, not a
    # package module). Load from a byte-identical `.py`-suffixed COPY, not
    # the real dotless path directly: pytest-testmon fingerprints every
    # executed file by extension (`filename.rsplit(".", 1)[1]`) and
    # IndexErrors on one that has none, which INTERNALERRORs any
    # `--impacted` run that touches this test (gr450298, gr449802) — same
    # convention as tests/test_coderef_structural.py.
    tmp_copy = Path(tempfile.mkdtemp(prefix="main_ci_status_py_")) / "main_ci_status.py"
    shutil.copyfile(SCRIPT, tmp_copy)
    loader = importlib.machinery.SourceFileLoader("main_ci_status", str(tmp_copy))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, name: str, purpose: str | None) -> dict:
    d = tmp_path / name
    (d / ".claude").mkdir(parents=True)
    if purpose is not None:
        (d / ".claude" / "purpose").write_text(purpose + "\n", encoding="utf-8")
    return {"name": name, "path": str(d)}


def test_claims_match_only_red_main_phrasing(tmp_path: Path) -> None:
    mod = _load()
    trees = [
        _tree(tmp_path, "a", "fix red main 8b881979: lint"),
        _tree(tmp_path, "b", "spec: design-workbench web tab"),
        _tree(tmp_path, "c", "Fixing main CI lint drift"),
        _tree(tmp_path, "d", None),
    ]
    assert [n for n, _ in mod.claims(trees)] == ["a", "c"]


def test_red_unclaimed_prints_the_purpose_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "latest_main_run", lambda: (RED_RUN, None))
    monkeypatch.setattr(mod, "main_head_sha", lambda: RED_RUN["headSha"])
    monkeypatch.setattr(
        mod, "failing_jobs", lambda _id: ["lint", "test-linux (3.13, 5)"]
    )
    monkeypatch.setattr(mod, "inflight_trees", list)
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "RED on CI: 8b881979 run 1" in out
    assert "failing: lint, test-linux (3.13, 5)" in out
    assert "UNCLAIMED" in out
    assert (
        "echo 'fix red main 8b881979: lint, test-linux (3.13, 5)' > .claude/purpose"
        in out
    )
    assert "gh run view 1 --log-failed" in out


def test_red_claimed_names_the_owner_and_pending_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "latest_main_run", lambda: (RED_RUN, PENDING_RUN))
    monkeypatch.setattr(mod, "main_head_sha", lambda: RED_RUN["headSha"])
    monkeypatch.setattr(mod, "failing_jobs", lambda _id: ["lint"])
    monkeypatch.setattr(
        mod,
        "inflight_trees",
        lambda: [_tree(tmp_path, "wolf", "fix red main 8b881979: lint")],
    )
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "claimed by wolf: fix red main 8b881979: lint" in out
    assert "UNCLAIMED" not in out
    assert "newer run is in_progress on 840b178f" in out


def test_green_is_silent_under_for_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "latest_main_run", lambda: (GREEN_RUN, None))
    monkeypatch.setattr(sys, "argv", ["main-ci-status", "--for-hook"])
    assert mod.main() == 0
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "argv", ["main-ci-status"])
    assert mod.main() == 0
    assert "✓ main green on CI (8b881979, run 2" in capsys.readouterr().out


def test_old_red_on_a_sha_main_left_behind_is_a_stale_listing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The list endpoint once returned a 6-day-old failure first. A red
    older than 12h whose sha is no longer main's head is reported as a
    stale listing — never as a RED to claim and fix."""
    mod = _load()
    old = {**RED_RUN, "databaseId": 7, "createdAt": "2026-09-12T16:54:37Z"}
    monkeypatch.setattr(mod, "latest_main_run", lambda: (old, None))
    monkeypatch.setattr(mod, "main_head_sha", lambda: "3fdae04c0000")
    monkeypatch.setattr(mod, "failing_jobs", lambda _id: ["lint"])
    monkeypatch.setattr(sys, "argv", ["main-ci-status", "--for-hook"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "looks stale" in out
    assert "RED" not in out and "UNCLAIMED" not in out


def test_runs_are_sorted_newest_first_before_picking_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load()
    old_red = {**RED_RUN, "databaseId": 7, "createdAt": "2026-09-12T16:54:37Z"}
    monkeypatch.setattr(mod, "_gh", lambda *a: json.dumps([old_red, GREEN_RUN]))
    verdict, _ = mod.latest_main_run()
    assert verdict is not None and verdict["databaseId"] == 2


def test_cancelled_runs_are_skipped_not_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load()
    cancelled = {**RED_RUN, "databaseId": 9, "conclusion": "cancelled"}
    monkeypatch.setattr(
        mod, "_gh", lambda *a: json.dumps([PENDING_RUN, cancelled, GREEN_RUN, RED_RUN])
    )
    verdict, pending = mod.latest_main_run()
    assert verdict is not None and verdict["databaseId"] == 2
    assert pending is not None and pending["databaseId"] == 3


@pytest.mark.skipif(sys.platform == "win32", reason="stub gh is a POSIX sh script")
def test_offline_is_silent_under_for_hook_and_exits_zero(tmp_path: Path) -> None:
    stub = tmp_path / "gh"
    stub.write_text("#!/bin/sh\nexit 4\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    hook = subprocess.run(
        [sys.executable, str(SCRIPT), "--for-hook"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=60,
    )
    assert hook.returncode == 0
    assert hook.stdout == ""
    plain = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=60,
    )
    assert plain.returncode == 0
    assert "unavailable" in plain.stdout
