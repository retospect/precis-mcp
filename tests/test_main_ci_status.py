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
from datetime import UTC, datetime, timedelta
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


def _iso_minutes_ago(mins: int) -> str:
    """A `createdAt` the script will read as recent. Relative to now, not a
    literal: STALE_AFTER_MIN is measured against the wall clock, so a frozen
    timestamp would silently cross the threshold as the test ages."""
    then = datetime.now(UTC) - timedelta(minutes=mins)
    return then.strftime("%Y-%m-%dT%H:%M:%SZ")


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
    # GREEN_RUN's sha IS main's head, so the staleness guard passes it
    # through. Pinned rather than left to the real `gh`: the guard now runs
    # on the green path too, and an unpinned head would make this test
    # depend on a network call.
    monkeypatch.setattr(mod, "main_head_sha", lambda: GREEN_RUN["headSha"])
    monkeypatch.setattr(sys, "argv", ["main-ci-status", "--for-hook"])
    assert mod.main() == 0
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "argv", ["main-ci-status"])
    assert mod.main() == 0
    assert "✓ main green on CI (8b881979, run 2" in capsys.readouterr().out


def test_old_green_on_a_sha_main_left_behind_is_a_stale_listing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gr456236: the staleness guard used to run only on the red path, so a
    cached page whose newest run was an old SUCCESS printed a confident
    "✓ main green" for a sha main had long left behind. Policy is to read
    this script before any local gate, so that green was licence to skip the
    gate — the costlier of the two staleness failures, and the unguarded one.
    """
    mod = _load()
    old = {**GREEN_RUN, "databaseId": 9, "createdAt": "2026-09-12T16:54:37Z"}
    monkeypatch.setattr(mod, "latest_main_run", lambda: (old, None))
    monkeypatch.setattr(mod, "main_head_sha", lambda: "3fdae04c0000")
    monkeypatch.setattr(sys, "argv", ["main-ci-status"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "looks stale" in out
    assert "success on 8b881979" in out, "names the outcome it is refusing to trust"
    assert "green on CI" not in out, "must not announce a verdict it does not have"


def test_stale_green_is_not_silent_under_for_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real green is silent at SessionStart by design. A stale green must
    not be: the hook is the one place the watcher is meant to speak up, and
    silence there is indistinguishable from a healthy main."""
    mod = _load()
    old = {**GREEN_RUN, "databaseId": 9, "createdAt": "2026-09-12T16:54:37Z"}
    monkeypatch.setattr(mod, "latest_main_run", lambda: (old, PENDING_RUN))
    monkeypatch.setattr(mod, "main_head_sha", lambda: "3fdae04c0000")
    monkeypatch.setattr(sys, "argv", ["main-ci-status", "--for-hook"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "looks stale" in out
    assert "a run is in_progress on 840b178f" in out


def test_fresh_green_on_a_sha_main_left_behind_still_reports_green(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sha mismatch alone is not staleness. Main moves fast enough that a
    minutes-old green on an ancestor is the normal case, not a cached page —
    calling that stale would make the script useless during a qland burst."""
    mod = _load()
    fresh = {**GREEN_RUN, "createdAt": _iso_minutes_ago(30)}
    monkeypatch.setattr(mod, "latest_main_run", lambda: (fresh, None))
    monkeypatch.setattr(mod, "main_head_sha", lambda: "3fdae04c0000")
    monkeypatch.setattr(sys, "argv", ["main-ci-status"])
    assert mod.main() == 0
    assert "green on CI" in capsys.readouterr().out


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


def test_viewer_check_red_prints_even_under_for_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A red nightly browser check must reach the next session's SessionStart
    hook — that hook is how a red reaches anyone at all."""
    mod = _load()
    red = {**RED_RUN, "databaseId": 41, "createdAt": _iso_minutes_ago(60)}
    monkeypatch.setattr(mod, "_gh", lambda *a: json.dumps([red]))
    monkeypatch.setattr(sys, "argv", ["main-ci-status", "--for-hook"])
    mod.viewer_check_report()
    out = capsys.readouterr().out
    assert "viewer-check RED: run 41 on 8b881979" in out
    assert "gh run download 41" in out


def test_viewer_check_green_is_silent_under_for_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    green = {**GREEN_RUN, "createdAt": _iso_minutes_ago(60)}
    monkeypatch.setattr(mod, "_gh", lambda *a: json.dumps([green]))
    monkeypatch.setattr(sys, "argv", ["main-ci-status", "--for-hook"])
    mod.viewer_check_report()
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "argv", ["main-ci-status"])
    mod.viewer_check_report()
    assert "viewer-check: ✓ green (8b881979, run 2" in capsys.readouterr().out


def test_viewer_check_gone_quiet_is_not_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A nightly that stopped running looks exactly like a green one unless
    its age is read; silence is the failure mode this line exists for."""
    mod = _load()
    old = {**GREEN_RUN, "createdAt": _iso_minutes_ago(3 * 24 * 60)}
    monkeypatch.setattr(mod, "_gh", lambda *a: json.dumps([old]))
    monkeypatch.setattr(sys, "argv", ["main-ci-status", "--for-hook"])
    mod.viewer_check_report()
    assert "gone quiet" in capsys.readouterr().out


def test_viewer_check_skips_cancelled_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load()
    cancelled = {**RED_RUN, "databaseId": 9, "conclusion": "cancelled"}
    monkeypatch.setattr(mod, "_gh", lambda *a: json.dumps([cancelled, GREEN_RUN]))
    run = mod.latest_viewer_run()
    assert run is not None and run["databaseId"] == 2
