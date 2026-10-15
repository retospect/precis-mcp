"""scripts/ship refuses a tree with uncommitted edits outside .claude/ (gr489288).

Reuses the real-git rig of test_ship_narrow_lock (origin.git, primary,
worktrees); see that module's docstring.
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


def test_quick_ship_refuses_dirty_tree_and_keeps_edits(rig: Rig) -> None:
    wt = rig.worktree("d", {"a.txt": "committed\n"})
    main_before = rig.origin_ref("main")
    head_before = rig.g(wt, "rev-parse", "HEAD")
    (wt / "a.txt").write_text("edited, not committed\n", encoding="utf-8")
    (wt / "sub").mkdir()
    (wt / "sub" / "new.txt").write_text("untracked\n", encoding="utf-8")

    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")

    assert rc != 0, out
    assert "uncommitted changes outside .claude/" in out, out
    assert "a.txt" in out and "sub/new.txt" in out, out
    # Nothing moved, nothing reset: the edits are still on disk, uncommitted.
    assert rig.origin_ref("main") == main_before
    assert rig.g(wt, "rev-parse", "HEAD") == head_before
    assert (wt / "a.txt").read_text(encoding="utf-8") == "edited, not committed\n"
    assert (wt / "sub" / "new.txt").exists()


def test_dirt_only_under_dot_claude_still_ships(rig: Rig) -> None:
    wt = rig.worktree("c", {"c.txt": "c\n", ".claude/notes.md": "v1\n"})
    (wt / ".claude" / "notes.md").write_text("v2\n", encoding="utf-8")
    p, log = rig.ship(wt, "--quick", sleep=1)
    out = _finish(p, log)
    assert "uncommitted changes" not in out, out
    files = set(rig.g(rig.origin, "ls-tree", "-r", "--name-only", "main").splitlines())
    assert "c.txt" in files
