"""scripts/ship amends only what ruff changed into the shipped commit (gr495021).

The gate's auto-fix amend used to be ``git add -A && git commit --amend``, which
folded any edit made while the gate ran (or any scratch file) into the commit
being landed. Reuses the real-git rig of test_ship_narrow_lock; the gate is the
test-only ``PRECIS_SHIP_TEST_GATE_CMD`` seam, here a command that plays ruff
(rewrites a tracked .py in place) and/or a session editing the tree mid-gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_ship_narrow_lock import Rig, _finish

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _main_file(rig: Rig, rel: str) -> str:
    return rig.g(rig.origin, "show", f"refs/heads/main:{rel}")


def test_ruff_only_fix_is_amended(rig: Rig) -> None:
    wt = rig.worktree("r", {"mod.py": "X=1\n", "keep.txt": "k\n"})
    p, log = rig.ship(
        wt,
        "--quick",
        PRECIS_SHIP_TEST_GATE_CMD="printf 'X = 1\\n' > mod.py",
    )
    out = _finish(p, log)
    assert "amended ruff auto-fixes into the branch (1 file(s))" in out, out
    assert _main_file(rig, "mod.py") == "X = 1"
    assert _main_file(rig, "keep.txt") == "k"


def test_unrelated_dirty_file_present_before_ship_is_refused_not_folded(
    rig: Rig,
) -> None:
    wt = rig.worktree("u", {"mod.py": "X=1\n", "other.py": "Y = 1\n"})
    (wt / "other.py").write_text("Y = 2  # scratch\n", encoding="utf-8")
    (wt / "scratch.py").write_text("tmp = 1\n", encoding="utf-8")
    main_before = rig.origin_ref("main")

    p, log = rig.ship(
        wt,
        "--quick",
        PRECIS_SHIP_TEST_GATE_CMD="printf 'X = 1\\n' > mod.py",
        sleep=1,
    )
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")

    assert rc != 0, out
    assert "uncommitted changes outside .claude/" in out, out
    assert "other.py" in out and "scratch.py" in out, out
    assert rig.origin_ref("main") == main_before
    # Still the builder's, uncommitted.
    assert (wt / "other.py").read_text(encoding="utf-8") == "Y = 2  # scratch\n"
    assert (wt / "scratch.py").exists()


def test_non_ruff_path_changed_mid_gate_dies_and_amends_nothing(rig: Rig) -> None:
    wt = rig.worktree("m", {"mod.py": "X=1\n", "notes.txt": "v1\n"})
    head_before = rig.g(wt, "rev-parse", "HEAD")
    main_before = rig.origin_ref("main")

    p, log = rig.ship(
        wt,
        "--quick",
        # ruff fixes mod.py, while the session edits notes.txt and drops a scratch file.
        PRECIS_SHIP_TEST_GATE_CMD=(
            "printf 'X = 1\\n' > mod.py; echo v2 > notes.txt; echo s > scratch.txt"
        ),
        sleep=1,
    )
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")

    assert rc != 0, out
    assert "refusing to amend them into the shipped commit" in out, out
    assert "notes.txt" in out and "scratch.txt" in out, out
    assert "mod.py" not in out.split("refusing to amend")[1], out
    assert rig.g(wt, "rev-parse", "HEAD") == head_before  # nothing amended
    assert rig.origin_ref("main") == main_before
    assert (wt / "notes.txt").read_text(encoding="utf-8") == "v2\n"
    assert (wt / "scratch.txt").exists()


def test_non_ascii_spaced_ruff_path_is_amended(rig: Rig) -> None:
    wt = rig.worktree("n", {"é mod.py": "X=1\n"})
    p, log = rig.ship(
        wt,
        "--quick",
        PRECIS_SHIP_TEST_GATE_CMD="printf 'X = 1\\n' > 'é mod.py'",
    )
    out = _finish(p, log)
    assert "amended ruff auto-fixes into the branch (1 file(s))" in out, out
    shown = rig.g(rig.origin, "ls-tree", "-z", "refs/heads/main")
    assert "é mod.py" in shown
