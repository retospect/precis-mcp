"""scripts/ship syncs by squash+rebase, and reaps absorbed agent branches.

Reuses the real-git rig of test_ship_narrow_lock (origin.git, primary,
racer, worktrees); see that module's docstring.
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


def _commit(rig: Rig, wt: Path, rel: str, body: str, msg: str) -> None:
    (wt / rel).parent.mkdir(parents=True, exist_ok=True)
    (wt / rel).write_text(body, encoding="utf-8")
    rig.g(wt, "add", "-A")
    rig.g(wt, "commit", "-q", "-m", msg)


def test_two_commit_branch_rebases_onto_moved_main(rig: Rig) -> None:
    wt = rig.worktree("a", {"a1.txt": "1\n"})
    _commit(rig, wt, "a2.txt", "2\n", "second")
    racer_sha = rig.race({"racer.txt": "r\n"})
    p, log = rig.ship(wt, "--quick", sleep=1)
    out = _finish(p, log)
    assert "forward-merged" not in out, out
    main = "refs/heads/main"
    assert rig.g(rig.origin, "rev-parse", f"{main}^") == racer_sha
    parents = rig.g(rig.origin, "log", "-1", "--format=%P", main).split()
    assert parents == [racer_sha]
    files = set(rig.g(rig.origin, "ls-tree", "-r", "--name-only", main).splitlines())
    assert {"a1.txt", "a2.txt", "racer.txt"} <= files


def test_conflict_leaves_rebase_in_progress(rig: Rig) -> None:
    wt = rig.worktree("c", {"shared.txt": "mine\n"})
    rig.race({"shared.txt": "theirs\n"})
    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "rebase conflict" in out, out
    gitdir = Path(rig.g(wt, "rev-parse", "--absolute-git-dir"))
    assert (gitdir / "rebase-merge").is_dir() or (gitdir / "rebase-apply").is_dir()
    p2, log2 = rig.ship(wt, "--quick", sleep=1)
    assert p2.wait(timeout=120) != 0
    assert "a rebase is in progress" in log2.read_text(encoding="utf-8")


def _agent(rig: Rig, name: str) -> Path:
    wt = rig.primary / ".claude" / "worktrees" / name
    rig.g(rig.primary, "worktree", "add", "-q", "-b", f"worktree-{name}", str(wt))
    _commit(rig, wt, f"{name}.txt", f"{name}\n", f"agent {name}")
    return wt


def _take(rig: Rig, wt: Path, name: str) -> None:
    """What scripts/take-agent does: rebase onto the agent, record it."""
    branch = f"worktree-{name}"
    rig.g(wt, "rebase", "-q", branch)
    record = Path(rig.g(wt, "rev-parse", "--git-path", "taken-agents"))
    if not record.is_absolute():
        record = wt / record
    with record.open("a", encoding="utf-8") as fh:
        fh.write(f"{branch} {rig.g(wt, 'rev-parse', branch)}\n")


def _branches(rig: Rig) -> set[str]:
    out = rig.g(rig.primary, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return set(out.split())


def test_absorbed_agent_is_reaped_others_survive(rig: Rig) -> None:
    ax = _agent(rig, "agent-x")
    ay = _agent(rig, "agent-y")
    wt = rig.worktree("f", {"f1.txt": "1\n"})
    _take(rig, wt, "agent-x")
    _commit(rig, wt, "f2.txt", "2\n", "more")
    p, log = rig.ship(wt, "--quick", sleep=1, PRECIS_NO_AUTOREAP="0")
    _finish(p, log)
    record = Path(rig.g(wt, "rev-parse", "--git-path", "taken-agents"))
    assert not (record if record.is_absolute() else wt / record).exists()
    br = _branches(rig)
    assert "worktree-agent-x" not in br
    assert not ax.exists()
    assert "worktree-agent-y" in br
    assert ay.exists()


def test_absorbed_agent_with_dirty_tree_survives(rig: Rig) -> None:
    ax = _agent(rig, "agent-x")
    wt = rig.worktree("f", {"f1.txt": "1\n"})
    _take(rig, wt, "agent-x")
    _commit(rig, wt, "f2.txt", "2\n", "more")
    (ax / "dirty.txt").write_text("wip\n", encoding="utf-8")
    p, log = rig.ship(wt, "--quick", sleep=1, PRECIS_NO_AUTOREAP="0")
    out = _finish(p, log)
    assert "worktree-agent-x" in _branches(rig)
    assert (ax / "dirty.txt").exists()
    assert "dirty" in out


def test_untaken_agent_off_our_head_is_left_alone(rig: Rig) -> None:
    # An agent branched off our unshipped HEAD is contained in it but may
    # still be running: only take-agent's record makes ship reap a branch.
    wt = rig.worktree("f", {"f1.txt": "1\n"})
    idle = rig.primary / ".claude" / "worktrees" / "agent-idle"
    rig.g(
        rig.primary,
        "worktree",
        "add",
        "-q",
        "-b",
        "worktree-agent-idle",
        str(idle),
        "feat-f",
    )
    p, log = rig.ship(wt, "--quick", sleep=1, PRECIS_NO_AUTOREAP="0")
    _finish(p, log)
    assert "worktree-agent-idle" in _branches(rig)
    assert idle.exists()
