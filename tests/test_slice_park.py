"""Park = save, resume anywhere (``_slice_park``): real git in temp repos
against a bare ``origin``. The push to origin's hidden ``refs/factory/``
namespace is the one seam (the real one speaks to GitHub under the factory
credential): tests swap it for a plain ``git push`` to the bare repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from precis.workers.job_types import _slice_park, land_slice, run_slice

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
TD = 42


def _rg(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env={**os.environ, **_GIT_ENV},
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str, text: str = "x") -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _rg(repo, "add", name)
    _rg(repo, "commit", "-q", "-m", f"add {name}")
    return _rg(repo, "rev-parse", "HEAD")


class _Rig:
    """A bare origin, host A's clone (with the slice's tree) and a fresh
    host B clone with no tree."""

    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        _rg(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.a = tmp_path / "host-a"
        _rg(tmp_path, "clone", "-q", str(self.origin), str(self.a))
        _rg(self.a, "checkout", "-q", "-b", "main")
        self.main = _commit(self.a, "first.txt")
        _rg(self.a, "push", "-q", "origin", "main")
        self.b = tmp_path / "host-b"
        _rg(tmp_path, "clone", "-q", str(self.origin), str(self.b))

    def origin_ref(self, ref: str) -> str | None:
        proc = subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", "--verify", ref],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    r = _Rig(tmp_path)

    def push(tree: Path, todo_id: int, sha: str, expected_tip: str) -> str:
        ref = _slice_park.saved_ref(todo_id)
        _rg(
            tree,
            "push",
            "-q",
            f"--force-with-lease={ref}:{expected_tip}",
            "origin",
            f"{sha}:{_slice_park.saved_ref(todo_id)}",
        )
        return f"branch push of slice/{todo_id}: on origin"

    monkeypatch.setattr(_slice_park, "_push", push)
    monkeypatch.setenv("PRECIS_NODE", "host-a")
    return r


@pytest.mark.parametrize("has_snapshot", [False, True])
def test_park_save_leases_the_observed_ref(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, has_snapshot: bool
) -> None:
    tree, _ = _tree_with_work(rig)
    ref = _slice_park.saved_ref(TD)
    if has_snapshot:
        assert _slice_park.save_work(tree, TD, "previous failure") is not None
    other_tip = _commit(rig.b, "other.txt")
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(rig.a))
    monkeypatch.delenv("PRECIS_FACTORY_GIT_TOKEN", raising=False)
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: str(rig.origin),
    )

    def push(tree: Path, todo_id: int, sha: str, expected_tip: str) -> str:
        _rg(rig.b, "push", "--force", "origin", f"{other_tip}:{ref}")
        return land_slice._push_branch(
            tree, run_slice.slice_branch(todo_id), sha, expected_tip=expected_tip
        )

    monkeypatch.setattr(_slice_park, "_push", push)
    record = _slice_park.save_work(tree, TD, "stale save")
    assert record is not None
    assert "lease refused" in record["error"]
    assert rig.origin_ref(ref) == other_tip
    assert _rg(tree, "cat-file", "-t", record["head"]) == "commit"


def test_park_save_keeps_snapshot_local_when_origin_cannot_be_read(rig: _Rig) -> None:
    tree, _ = _tree_with_work(rig)
    _rg(tree, "remote", "set-url", "origin", str(rig.origin / "missing"))
    record = _slice_park.save_work(tree, TD, "failed attempt")
    assert record is not None
    assert record["push"] == "withheld: cannot verify origin's saved tip"
    assert rig.origin_ref(_slice_park.saved_ref(TD)) is None
    assert _rg(tree, "cat-file", "-t", record["head"]) == "commit"


def _tree_with_work(rig: _Rig) -> tuple[Path, str]:
    """Host A's slice tree: one committed file and one uncommitted edit
    plus an untracked file. Returns ``(tree, committed tip)``."""
    tree = run_slice._ensure_worktree(rig.a, TD)
    tip = _commit(tree, "built.txt", "done part")
    (tree / "first.txt").write_text("edited, not committed", encoding="utf-8")
    (tree / "new.txt").write_text("untracked", encoding="utf-8")
    return tree, tip


class _Ctx:
    def __init__(self) -> None:
        self.meta: dict[str, Any] = {}
        self.events: list[str] = []
        self.store = None

    def set_meta(self, **kw: Any) -> None:
        self.meta.update(kw)

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append(f"{kind}: {text}")


def test_save_work_commits_the_dirty_tree_and_pushes(rig: _Rig) -> None:
    """The park's save: a ``wip: parked`` commit of the uncommitted tree on
    top of the branch, pushed to the hidden ref; the record names host,
    worktree and head. The worktree, its index and the local branch are
    untouched — an ordinary retry still drops the dead attempt's edits."""
    tree, tip = _tree_with_work(rig)
    before_status = _rg(tree, "status", "--porcelain")
    rec = _slice_park.save_work(tree, TD, "ladder spent")
    assert rec is not None and rec["wip"] is True
    assert rec["host"] == "host-a" and rec["worktree"] == str(tree)
    wip = rec["head"]
    assert _rg(tree, "log", "-1", "--format=%s", wip) == (
        f"wip: parked ladder spent (td{TD})"
    )
    assert _rg(tree, "rev-parse", f"{wip}^") == tip
    shown = _rg(tree, "show", "--stat", "--format=", wip)
    assert "first.txt" in shown and "new.txt" in shown
    assert rig.origin_ref(f"refs/factory/slice/{TD}") == wip
    # non-destructive: branch, index and working tree are as the session left them
    assert _rg(tree, "rev-parse", "HEAD") == tip
    assert _rg(tree, "status", "--porcelain") == before_status


def test_save_work_clean_tree_pushes_the_tip_and_skips_the_wip_commit(
    rig: _Rig,
) -> None:
    tree = run_slice._ensure_worktree(rig.a, TD)
    tip = _commit(tree, "built.txt")
    rec = _slice_park.save_work(tree, TD, "child failed")
    assert rec is not None and rec["wip"] is False and rec["head"] == tip
    assert rig.origin_ref(f"refs/factory/slice/{TD}") == tip


def test_save_work_with_nothing_to_save_pushes_nothing(rig: _Rig) -> None:
    tree = run_slice._ensure_worktree(rig.a, TD)
    assert _slice_park.save_work(tree, TD, "no work") is None
    assert rig.origin_ref(f"refs/factory/slice/{TD}") is None


def test_save_at_job_end_stamps_the_job_and_survives_a_failed_push(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree, _tip = _tree_with_work(rig)
    ctx = _Ctx()
    _slice_park.save_at_job_end(ctx, tree, TD, "exit 1")
    saved = ctx.meta[_slice_park.SAVED_KEY]
    assert saved["wip"] is True and saved["host"] == "host-a"
    assert any("park-save:" in e and saved["head"][:12] in e for e in ctx.events)

    # A failed push never raises and never loses the local record: the
    # failure is logged on the job.
    monkeypatch.setattr(
        _slice_park, "_push", lambda *_a: f"branch push of slice/{TD}: FAILED (down)"
    )
    comments: list[str] = []
    monkeypatch.setattr(
        run_slice, "_append_comment", lambda _s, _t, text: comments.append(text)
    )
    bad = _Ctx()
    _slice_park.save_at_job_end(bad, tree, TD, "exit 1")
    assert "FAILED" in bad.meta[_slice_park.SAVED_KEY]["error"]
    assert any(e.startswith("job_event: park-save FAILED") for e in bad.events)
    assert comments and "park-save FAILED" in comments[0]

    # A raise inside the save is swallowed too.
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("git exploded")

    monkeypatch.setattr(_slice_park, "save_work", boom)
    worse = _Ctx()
    _slice_park.save_at_job_end(worse, tree, TD, "exit 1")
    assert _slice_park.SAVED_KEY not in worse.meta
    assert any("git exploded" in e for e in worse.events)


def test_resume_on_another_host_cuts_from_the_saved_tip_not_main(rig: _Rig) -> None:
    tree, tip = _tree_with_work(rig)
    rec = _slice_park.save_work(tree, TD, "rescue ask")
    assert rec is not None
    notes: list[str] = []
    # A parked node's re-arm keeps the WIP snapshot.
    b_tree = run_slice._ensure_worktree(rig.b, TD, keep_wip=True, notes=notes)
    assert _rg(b_tree, "rev-parse", "HEAD") == rec["head"] != rig.main
    assert (b_tree / "built.txt").read_text(encoding="utf-8") == "done part"
    assert (b_tree / "new.txt").read_text(encoding="utf-8") == "untracked"
    assert notes == [f"resumed from {rec['head']}"]
    assert _rg(b_tree, "rev-parse", "--abbrev-ref", "HEAD") == f"slice/{TD}"


def test_an_ordinary_retry_resumes_from_the_last_real_commit(rig: _Rig) -> None:
    """Without ``keep_wip`` (the node is not parked) the WIP snapshot on top
    of the pushed branch is not carried: committed work resumes, the dead
    attempt's uncommitted edits stay dropped (gr571311)."""
    tree, tip = _tree_with_work(rig)
    assert _slice_park.save_work(tree, TD, "exit 1") is not None
    notes: list[str] = []
    b_tree = run_slice._ensure_worktree(rig.b, TD, notes=notes)
    assert _rg(b_tree, "rev-parse", "HEAD") == tip
    assert not (b_tree / "new.txt").exists()
    assert notes == [f"resumed from {tip}"]


def test_parked_resume_without_a_saved_ref_refuses(rig: _Rig) -> None:
    with pytest.raises(RuntimeError, match="resume on the saving host"):
        run_slice._ensure_worktree(rig.b, TD, keep_wip=True)
    assert not (run_slice.worktrees_dir(rig.b) / run_slice.slice_name(TD)).exists()


def test_resume_on_the_same_host_keeps_the_wip_commit(rig: _Rig) -> None:
    tree, tip = _tree_with_work(rig)
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None
    notes: list[str] = []
    again = run_slice._ensure_worktree(
        rig.a, TD, keep_wip=True, saved_head=rec["head"], notes=notes
    )
    assert again == tree
    assert _rg(tree, "rev-parse", "HEAD") == rec["head"]
    assert _rg(tree, "log", "-1", "--format=%s").startswith("wip: parked ")
    assert (tree / "new.txt").read_text(encoding="utf-8") == "untracked"
    assert _rg(tree, "status", "--porcelain") == ""
    assert notes == [f"resumed from {rec['head']}"]


def test_same_host_resume_keeps_the_branch_when_the_push_failed(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Origin never got the snapshot: the object is in this host's own
    store, ``meta.parked.head`` finds it."""
    monkeypatch.setattr(_slice_park, "_push", lambda *_a: "FAILED (down)")
    tree, _tip = _tree_with_work(rig)
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None and rig.origin_ref(f"refs/factory/slice/{TD}") is None
    run_slice._ensure_worktree(rig.a, TD, keep_wip=True, saved_head=rec["head"])
    assert _rg(tree, "rev-parse", "HEAD") == rec["head"]


def test_same_host_ordinary_retry_still_drops_uncommitted_edits(rig: _Rig) -> None:
    tree, tip = _tree_with_work(rig)
    assert _slice_park.save_work(tree, TD, "exit 1") is not None
    run_slice._ensure_worktree(rig.a, TD)
    assert _rg(tree, "rev-parse", "HEAD") == tip  # committed work keeps
    assert _rg(tree, "status", "--porcelain") == ""  # dirty edits drop
    assert not (tree / "new.txt").exists()


def test_reused_tree_fast_forwards_to_origins_pushed_tip(rig: _Rig) -> None:
    """gr709688: host A's local ``slice/42`` holds the round-1 tip while
    a round on host B pushed a newer tip to ``refs/factory/slice/42``.
    The reuse fetches it and fast-forwards the branch — a tip origin
    moved past is never the rebuild's base again — while a branch with
    unpushed commits of its own is never moved."""
    tree = run_slice._ensure_worktree(rig.a, TD)
    first = _commit(tree, "built.txt", "round 1")
    assert _slice_park.save_work(tree, TD, "failed round") is not None
    assert rig.origin_ref(f"refs/factory/slice/{TD}") == first

    b_tree = run_slice._ensure_worktree(rig.b, TD)
    assert _rg(b_tree, "rev-parse", "HEAD") == first
    newer = _commit(b_tree, "built2.txt", "round 2")
    assert _slice_park.save_work(b_tree, TD, "host B round 2") is not None
    assert rig.origin_ref(f"refs/factory/slice/{TD}") == newer

    notes: list[str] = []
    again = run_slice._ensure_worktree(rig.a, TD, notes=notes)
    assert again == tree
    assert _rg(tree, "rev-parse", "HEAD") == newer
    assert notes == [f"resumed from {newer}"]

    # A diverged branch — commits origin never got — is never moved.
    _rg(tree, "reset", "-q", "--hard", first)
    diverged = _commit(tree, "local.txt", "unpushed")
    run_slice._ensure_worktree(rig.a, TD)
    assert _rg(tree, "rev-parse", "HEAD") == diverged


@pytest.mark.parametrize("dirty", [False, True])
def test_save_work_withholds_a_tip_behind_origins(rig: _Rig, dirty: bool) -> None:
    """gr709688: the same clobber the defer refusal stops, through the
    park-save — a stale tree's tip (or the WIP snapshot on it) does not
    descend from the tip another host pushed, so the save withholds it
    like a secret-scan hit: kept local, never an error."""
    tree = run_slice._ensure_worktree(rig.a, TD)
    _commit(tree, "built.txt", "round 1")
    assert _slice_park.save_work(tree, TD, "round 1") is not None
    b_tree = run_slice._ensure_worktree(rig.b, TD)
    newer = _commit(b_tree, "built2.txt", "round 2")
    assert _slice_park.save_work(b_tree, TD, "round 2") is not None
    assert rig.origin_ref(f"refs/factory/slice/{TD}") == newer

    if dirty:
        (tree / "new.txt").write_text("unsaved", encoding="utf-8")
    rec = _slice_park.save_work(tree, TD, "stale retry")
    assert rec is not None
    assert rec["push"].startswith("withheld: behind origin's pushed tip")
    assert "error" not in rec
    assert rig.origin_ref(f"refs/factory/slice/{TD}") == newer


def test_same_host_resume_never_resets_committed_work(rig: _Rig) -> None:
    """A parked node whose branch moved on past the saved snapshot keeps
    its own commits: the snapshot is applied only as a fast-forward."""
    tree, _tip = _tree_with_work(rig)
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None
    _rg(tree, "reset", "-q", "--hard", "HEAD")
    _rg(tree, "clean", "-fdq")
    own = _commit(tree, "own.txt")
    run_slice._ensure_worktree(rig.a, TD, keep_wip=True, saved_head=rec["head"])
    assert _rg(tree, "rev-parse", "HEAD") == own


def test_start_over_resumes_from_main_and_drops_the_saved_branch(rig: _Rig) -> None:
    tree, _tip = _tree_with_work(rig)
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None
    notes: list[str] = []
    fresh = run_slice._ensure_worktree(
        rig.a, TD, keep_wip=True, start_over=True, saved_head=rec["head"], notes=notes
    )
    assert _rg(fresh, "rev-parse", "HEAD") == rig.main
    assert not (fresh / "built.txt").exists() and not (fresh / "new.txt").exists()
    assert any("worktree" in n and "dropped" in n for n in notes)
    assert any("branch slice/42 dropped" in n for n in notes)
    assert not any(n.startswith("resumed from") for n in notes)
    # the tracking copy of the saved ref is gone too: another host's
    # launch cannot resume from it through this clone
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", _slice_park.tracking_ref(TD)],
            cwd=str(rig.a),
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_start_over_on_a_host_with_no_tree_ignores_the_saved_ref(rig: _Rig) -> None:
    tree, _tip = _tree_with_work(rig)
    assert _slice_park.save_work(tree, TD, "ask") is not None
    b_tree = run_slice._ensure_worktree(rig.b, TD, keep_wip=True, start_over=True)
    assert _rg(b_tree, "rev-parse", "HEAD") == rig.main


_LEAK = (
    "gh" + "p_" + ("aB3dE5fG7hJ9kL1mN3pQ" + "5rS7tU9vW1xY3zA5")
)  # token-shaped, not real


def test_a_secret_scan_hit_withholds_the_push_and_keeps_the_snapshot_local(
    rig: _Rig,
) -> None:
    """The repo is public and refs/factory/* fetchable: a snapshot whose
    diff against base trips the repo's secret scan is not pushed. It stays
    in the host's object store, so a same-host resume still works."""
    tree, _tip = _tree_with_work(rig)
    (tree / "notes.txt").write_text(f"token = {_LEAK}\n", encoding="utf-8")
    rec = _slice_park.save_work(tree, TD, "ladder spent")
    assert rec is not None and rec["wip"] is True
    assert rec["push"] == "withheld: secret-scan 1 hits"
    assert "error" not in rec  # a withheld push is a decision, not a failure
    assert rig.origin_ref(f"refs/factory/slice/{TD}") is None
    # the snapshot object is local and carries the work
    assert "notes.txt" in _rg(tree, "show", "--stat", "--format=", rec["head"])
    # same-host resume works from it; no other host can see it
    run_slice._ensure_worktree(rig.a, TD, keep_wip=True, saved_head=rec["head"])
    assert _rg(tree, "rev-parse", "HEAD") == rec["head"]
    # no other host can see it: td702498 refuses the parked resume there
    # rather than silently cutting a fresh build from origin/main
    with pytest.raises(RuntimeError, match="resume on the saving host"):
        run_slice._ensure_worktree(rig.b, TD, keep_wip=True, saved_head=rec["head"])
    assert not (run_slice.worktrees_dir(rig.b) / run_slice.slice_name(TD)).exists()


def test_a_hit_in_committed_work_withholds_too_and_the_job_event_says_so(
    rig: _Rig,
) -> None:
    tree = run_slice._ensure_worktree(rig.a, TD)
    _commit(tree, "cfg.txt", f"key: {_LEAK}\n")
    ctx = _Ctx()
    _slice_park.save_at_job_end(ctx, tree, TD, "exit 1")
    saved = ctx.meta[_slice_park.SAVED_KEY]
    assert saved["push"].startswith("withheld: secret-scan")
    assert any("WITHHELD (kept local only)" in e for e in ctx.events)
    assert rig.origin_ref(f"refs/factory/slice/{TD}") is None
    assert _LEAK not in "".join(ctx.events)  # never echo the finding


def test_a_clean_scan_pushes(rig: _Rig) -> None:
    tree, _tip = _tree_with_work(rig)
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None and rec["push"].endswith("on origin")
    assert rig.origin_ref(f"refs/factory/slice/{TD}") == rec["head"]


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        "server.pem",
        "api.key",
        "id_ed25519",
        "my_token.txt",
        "credentials.json",
    ],
)
def test_untracked_secret_named_files_never_ride_the_snapshot(
    rig: _Rig, name: str
) -> None:
    tree, _tip = _tree_with_work(rig)
    (tree / name).write_text("harmless-looking content\n", encoding="utf-8")
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None
    files = _rg(tree, "show", "--name-only", "--format=", rec["head"]).split()
    assert name not in files and "new.txt" in files
    assert (tree / name).exists()  # the worktree itself is untouched


@pytest.mark.parametrize("dirty", [False, True])
def test_deleted_credential_in_history_withholds_push(rig: _Rig, dirty: bool) -> None:
    tree = run_slice._ensure_worktree(rig.a, TD)
    _commit(tree, "leaked.txt", "ghp_" + "A" * 36)
    _rg(tree, "rm", "leaked.txt")
    _rg(tree, "commit", "-qm", "remove credential")
    if dirty:
        (tree / "work.txt").write_text("unfinished", encoding="utf-8")
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None and rec["push"].startswith("withheld:")
    assert rig.origin_ref(_slice_park.saved_ref(TD)) is None


@pytest.mark.parametrize("existing_tree", [False, True])
def test_unavailable_snapshot_refuses_without_cleaning(
    rig: _Rig, existing_tree: bool
) -> None:
    tree, _tip = _tree_with_work(rig)
    rec = _slice_park.save_work(tree, TD, "older snapshot")
    assert rec is not None
    if existing_tree:
        target = run_slice._ensure_worktree(rig.b, TD)
        (target / "precious.txt").write_text("unsaved", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unavailable"):
        run_slice._ensure_worktree(rig.b, TD, keep_wip=True, saved_head="f" * 40)
    if existing_tree:
        assert (target / "precious.txt").read_text(encoding="utf-8") == "unsaved"


def test_same_host_without_tree_recovers_unpushed_snapshot(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree, _tip = _tree_with_work(rig)
    monkeypatch.setattr(_slice_park, "_push", lambda *_a: "FAILED (down)")
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None
    _rg(rig.a, "worktree", "remove", "--force", str(tree))
    _rg(rig.a, "branch", "-D", run_slice.slice_branch(TD))
    restored = run_slice._ensure_worktree(
        rig.a, TD, keep_wip=True, saved_head=rec["head"]
    )
    assert _rg(restored, "rev-parse", "HEAD") == rec["head"]


@pytest.mark.parametrize("push_note", ["FAILED (down)", "withheld: secret-scan 1 hits"])
def test_cross_host_resume_refuses_unpublished_snapshot(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, push_note: str
) -> None:
    tree, _tip = _tree_with_work(rig)
    monkeypatch.setattr(_slice_park, "_push", lambda *_a: push_note)
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None
    with pytest.raises(RuntimeError, match="resume on the saving host"):
        run_slice._ensure_worktree(rig.b, TD, keep_wip=True, saved_head=rec["head"])
    assert not (run_slice.worktrees_dir(rig.b) / run_slice.slice_name(TD)).exists()


def test_scan_failure_withholds_push(rig: _Rig) -> None:
    tree = run_slice._ensure_worktree(rig.a, TD)
    tip = _commit(tree, "work.txt")
    assert _slice_park.secret_hits(tree, "missing-base", tip) > 0


def test_deleted_credential_on_merged_branch_withholds_push(rig: _Rig) -> None:
    tree = run_slice._ensure_worktree(rig.a, TD)
    _rg(tree, "checkout", "-qb", "side")
    _commit(tree, "leaked.txt", "ghp_" + "A" * 36)
    _rg(tree, "rm", "leaked.txt")
    _rg(tree, "commit", "-qm", "remove credential")
    _rg(tree, "checkout", run_slice.slice_branch(TD))
    _commit(tree, "work.txt")
    _rg(tree, "merge", "--no-ff", "-m", "merge side", "side")
    rec = _slice_park.save_work(tree, TD, "ask")
    assert rec is not None and rec["push"].startswith("withheld:")
    assert rig.origin_ref(_slice_park.saved_ref(TD)) is None
