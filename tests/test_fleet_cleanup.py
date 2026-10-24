"""scripts/fleet-cleanup: squash-merge detection and never-touch list."""

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fleet-cleanup"


def _load():
    loader = importlib.machinery.SourceFileLoader("fleet_cleanup", str(SCRIPT))
    spec = importlib.util.spec_from_loader("fleet_cleanup", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fc = _load()


def _g(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _commit(r, name, text, msg):
    (r / name).write_text(text, encoding="utf-8")
    _g(r, "add", ".")
    _g(r, "commit", "-qm", msg)


def test_squash_merge_detected(tmp_path):
    r = tmp_path
    _g(r, "init", "-q", "-b", "main")
    _commit(r, "a.txt", "1\n", "init")
    _g(r, "checkout", "-qb", "feat")
    _commit(r, "b.txt", "x\n", "b1")
    _commit(r, "b.txt", "xy\n", "b2")
    _g(r, "checkout", "-qb", "other", "main")
    _commit(r, "c.txt", "c\n", "c")
    _g(r, "checkout", "-q", "main")
    assert not fc.is_merged(str(r), "feat", "main")
    _g(r, "merge", "--squash", "feat")
    _g(r, "commit", "-qm", "squash feat")
    assert fc.is_merged(str(r), "feat", "main")
    assert not fc.is_merged(str(r), "other", "main")


def test_classify_worktree():
    assert fc.classify_worktree(True, False, False, False) == "remove"
    assert fc.classify_worktree(True, False, True, False) == "locked"
    assert fc.classify_worktree(True, False, False, True) == "live"
    assert fc.classify_worktree(True, True, False, False) == "dirty"
    assert fc.classify_worktree(False, False, False, False) == "unmerged"


def test_protected_and_deletable_names():
    for b in (
        "main",
        "master",
        "HEAD",
        "gated",
        "prod",
        "release/r1",
        "deployed/x",
        "ci/x",
    ):
        assert fc.is_protected_branch(b)
        assert not fc.is_remote_deletable(b)
    assert fc.is_remote_deletable("worktree-foo")
    assert not fc.is_remote_deletable("random-thing")


def test_apply_remote_never_deletes_script_refs(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    r = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(r)], check=True, capture_output=True
    )
    _g(r, "checkout", "-qb", "main")
    _commit(r, "a.txt", "1\n", "init")
    names = ("release/r1", "gated", "prod", "ci/x", "worktree-foo", "oddball")
    for b in names:
        _g(r, "branch", b)
    _g(r, "push", "-q", "origin", "main", *names)
    stats = {
        k: 0
        for k in (
            "prunable",
            "wt_remove",
            "wt_locked",
            "wt_live",
            "wt_dirty",
            "wt_unmerged",
            "br_merged",
            "br_unmerged",
            "rem_merged",
            "rem_unmerged",
        )
    }
    fc.sweep_repo("t", str(r), True, True, stats, [])
    heads = subprocess.run(
        ["git", "-C", str(origin), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.split()
    assert set(heads) == {"main", "release/r1", "gated", "prod", "ci/x", "oddball"}


def test_never_touch_windows():
    for w in ("CHAT", "COORDINATOR", "GRIPE_FIX", "BUILDER", "random"):
        assert fc.is_protected_window(w)
    assert not fc.is_protected_window("devin-gf-pagefold")
    assert not fc.is_protected_window("codex-foo")
    assert not fc.is_protected_window("local-bar")
