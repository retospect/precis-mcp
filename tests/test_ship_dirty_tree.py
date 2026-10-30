"""scripts/ship refuses a tree with uncommitted edits outside .claude/ (gr489288).

Reuses the real-git rig of test_ship_narrow_lock (origin.git, primary,
worktrees); see that module's docstring.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_ship_narrow_lock import (
    Rig,
    _finish,
    _install_racing_pre_push,
    _tree_files,
)

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


# ───────────── the branch lands its own squash commit ─────────────


def _is_ancestor(rig: Rig, wt: Path, a: str, b: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", a, b],
            cwd=wt,
            env=rig._git_env,
            check=False,
        ).returncode
        == 0
    )


_NOTES = Path(".claude") / "notes.md"


def _tracked_notes_worktree(rig: Rig, name: str) -> Path:
    """A worktree whose branch also tracks ``.claude/notes.md`` (force-added:
    the rig's .gitignore ignores .claude/). Dirt there passes both the
    clean-tree guard and the post-gate amend check, so it survives to land."""
    wt = rig.worktree(name, {f"{name}.txt": "x\n"})
    (wt / _NOTES).parent.mkdir(exist_ok=True)
    (wt / _NOTES).write_text("committed\n", encoding="utf-8")
    rig.g(wt, "add", "-f", str(_NOTES))
    rig.g(wt, "commit", "-q", "-m", f"notes {name}")
    return wt


def _land_dirty(rig: Rig, wt: Path, *flags: str) -> str:
    """Ship ``wt`` and edit its tracked notes file while the gate is in
    flight, so the tree is dirty when the final section lands."""
    p, log = rig.ship(wt, *flags, hold=True)
    rig.wait_lint_started(wt)
    (wt / _NOTES).write_text("edited during the gate\n", encoding="utf-8")
    rig.release()
    return _finish(p, log)


def test_quick_land_leaves_branch_equal_to_origin_main(rig: Rig) -> None:
    wt = rig.worktree("eq", {"eq.txt": "eq\n"})
    p, log = rig.ship(wt, "--quick", sleep=1)
    _finish(p, log)
    assert rig.g(wt, "rev-parse", "HEAD") == rig.origin_ref("main")
    assert "feat-eq" in rig.g(wt, "branch", "--merged", "origin/main")


def test_dirty_tracked_file_at_land_keeps_branch_on_landed_sha(rig: Rig) -> None:
    """A dirty tree skips the post-ship hard reset: the branch must already
    sit on the landed sha, and the edit must survive."""
    wt = _tracked_notes_worktree(rig, "dy")
    out = _land_dirty(rig, wt, "--quick")
    landed = rig.origin_ref("main")
    assert "already sits on the landed" in out, out
    assert "skipping branch reset" not in out, out
    assert rig.g(wt, "rev-parse", "HEAD") == landed
    assert _is_ancestor(rig, wt, "HEAD", "origin/main")
    assert "feat-dy" in rig.g(wt, "branch", "--merged", "origin/main")
    assert (wt / _NOTES).read_text(encoding="utf-8") == "edited during the gate\n"
    assert rig.g(wt, "status", "--porcelain") == "M .claude/notes.md"
    # the landed tree is the committed one, not the late edit
    assert rig.g(rig.origin, "show", "main:.claude/notes.md") == "committed"


@pytest.mark.parametrize("dirty", [False, True], ids=["clean", "dirty"])
def test_lost_cas_race_still_ends_with_branch_on_main(rig: Rig, dirty: bool) -> None:
    """A racer lands between the ship's fetch and push: the lease loses, the
    in-lock retry forward-merges and mints a fresh squash; the branch ends on
    the one that landed (== origin/main unless a later push moved main)."""
    wt = _tracked_notes_worktree(rig, "cas")
    counter = _install_racing_pre_push(rig, limit=1)
    if dirty:
        out = _land_dirty(rig, wt, "--quick")
    else:
        p, log = rig.ship(wt, "--quick", sleep=1)
        out = _finish(p, log)
    assert counter.read_text(encoding="utf-8").strip() == "1"
    assert "in-lock try 1/5" in out, out
    landed = rig.origin_ref("main")
    assert {"cas.txt", "hook_1.txt"} <= _tree_files(rig)
    assert rig.g(wt, "rev-parse", "HEAD") == landed
    assert _is_ancestor(rig, wt, "HEAD", "origin/main")
    # one parent: the landed commit is a squash on the racer's main, not a merge
    assert len(rig.g(wt, "rev-list", "--parents", "-1", "HEAD").split()) == 2
    if dirty:
        assert (wt / _NOTES).read_text(encoding="utf-8") == "edited during the gate\n"
