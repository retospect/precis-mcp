"""What is left of ``_slice_park`` after the attempt workspaces (gr741695):
the plumbing snapshot of a working tree and the ``wip:`` subject test. The
save/resume machinery these tests used to pin is retired; the push, the
secret refusal and the next attempt's start are ``tests/test_slice_ws.py``'s
and ``tests/test_run_slice_park.py``'s."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from precis.workers.job_types import _slice_park

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _rg(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env={**os.environ, **_ENV},
    )
    return proc.stdout.strip()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _rg(repo, "init", "-q", "-b", "slice/7")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    _rg(repo, "add", "-A")
    _rg(repo, "commit", "-qm", "feat(x): base (td7)")
    return repo


def test_a_clean_tree_snapshots_to_nothing(tree: Path) -> None:
    assert _slice_park._snapshot_commit(tree, 7, "run ended") is None


def test_the_snapshot_holds_every_edit_and_leaves_the_folder_alone(tree: Path) -> None:
    head = _rg(tree, "rev-parse", "HEAD")
    (tree / "a.txt").write_text("changed\n", encoding="utf-8")
    (tree / "new.py").write_text("x = 1\n", encoding="utf-8")
    (tree / "scratch").mkdir()
    (tree / "scratch" / "ignored.txt").write_text("no\n", encoding="utf-8")
    snap = _slice_park._snapshot_commit(
        tree, 7, "session killed", subject="wip: snapshot session killed (td7)"
    )
    assert snap is not None
    assert _rg(tree, "rev-parse", f"{snap}^") == head
    assert _rg(tree, "log", "-1", "--format=%s", snap) == (
        "wip: snapshot session killed (td7)"
    )
    shown = _rg(tree, "show", "--name-only", "--format=", snap).split()
    assert sorted(shown) == ["a.txt", "new.py"]
    # the folder's own branch, index and files did not move
    assert _rg(tree, "rev-parse", "HEAD") == head
    assert _rg(tree, "status", "--porcelain").count("\n") == 1  # M a.txt, ?? new.py
    assert (tree / "a.txt").read_text(encoding="utf-8") == "changed\n"


def test_the_default_subject_is_the_parked_one(tree: Path) -> None:
    (tree / "a.txt").write_text("changed\n", encoding="utf-8")
    snap = _slice_park._snapshot_commit(tree, 7, "needs an answer")
    assert snap is not None
    assert _rg(tree, "log", "-1", "--format=%s", snap) == (
        "wip: parked needs an answer (td7)"
    )


@pytest.mark.parametrize(
    "name", [".env", ".env.local", "deploy.pem", "my.key", "id_rsa", "api_token.txt"]
)
def test_untracked_secret_named_files_never_ride_the_snapshot(
    tree: Path, name: str
) -> None:
    (tree / name).write_text("SECRET=1\n", encoding="utf-8")
    (tree / "ok.txt").write_text("fine\n", encoding="utf-8")
    snap = _slice_park._snapshot_commit(tree, 7, "run ended")
    assert snap is not None
    assert _rg(tree, "show", "--name-only", "--format=", snap).split() == ["ok.txt"]


def test_a_tree_holding_only_secret_named_files_has_nothing_to_snapshot(
    tree: Path,
) -> None:
    (tree / ".env").write_text("SECRET=1\n", encoding="utf-8")
    assert _slice_park._snapshot_commit(tree, 7, "run ended") is None


def test_a_snapshot_in_an_attempt_folder_never_acts_on_the_host(
    tmp_path: Path,
) -> None:
    """gr742505: the snapshot's plumbing runs on an env of its own (a
    private index), so it must carry ``run_slice._git``'s ceiling itself.
    An attempt folder that lost its ``.git`` is "not a git repository" to
    it — never the host checkout above, whose index, locks and worktree
    would otherwise answer for it."""
    host = tmp_path / "host"
    _rg(tmp_path, "init", "-q", "-b", "main", str(host))
    (host / "a.txt").write_text("host\n", encoding="utf-8")
    _rg(host, "add", "-A")
    _rg(host, "commit", "-qm", "host")
    head = _rg(host, "rev-parse", "HEAD")
    folder = host / ".claude" / "worktrees" / "slice-td7-j1"
    folder.mkdir(parents=True)
    (folder / "stray.txt").write_text("x\n", encoding="utf-8")
    # the hazard: plain git in the folder answers for the host
    bare = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=str(folder),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={k: v for k, v in os.environ.items() if k != "GIT_CEILING_DIRECTORIES"},
    )
    assert Path(bare.stdout.strip()).resolve() == (host / ".git").resolve()
    with pytest.raises(RuntimeError, match="not a git repository"):
        _slice_park._snapshot_commit(folder, 7, "run ended", parent=head)
    # nothing of the host was read, staged or committed
    assert _rg(host, "rev-parse", "HEAD") == head
    assert _rg(host, "status", "--porcelain") == "?? .claude/"
    # a real clone under an attempt name: the ceiling changes nothing
    clone = folder.with_name("slice-td7-j2")
    _rg(tmp_path, "clone", "-q", str(host), str(clone))
    (clone / "new.txt").write_text("n\n", encoding="utf-8")
    snap = _slice_park._snapshot_commit(clone, 7, "run ended")
    assert snap is not None
    assert _rg(clone, "show", "--name-only", "--format=", snap).split() == ["new.txt"]


def test_is_wip_reads_the_subject(tree: Path) -> None:
    head = _rg(tree, "rev-parse", "HEAD")
    assert _slice_park.is_wip(tree, head) is False
    (tree / "a.txt").write_text("changed\n", encoding="utf-8")
    snap = _slice_park._snapshot_commit(tree, 7, "x", subject="wip: snapshot x (td7)")
    assert snap is not None and _slice_park.is_wip(tree, snap) is True
    parked = _slice_park._snapshot_commit(tree, 7, "x")
    assert parked is not None and _slice_park.is_wip(tree, parked) is True
