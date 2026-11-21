"""The lander's own clone (gr741695), against real git and the real
workspace layer (``_slice_ws``): a local bare repository plays GitHub, the
host's main checkout is a clone of it, the hub is the real
``precis-hub.git``. Ship, docker and the drift probe are stood in (the
``test_land_slice`` seams); everything the land does to git, the hub and the
folder is real. No database, no network.

``tests/test_land_slice.py`` holds the same flows against a recording fake of
the workspace layer (the order of its calls, the failure shapes); these tests
hold that the pieces fit: the clone is cut at the right commit, ship runs in
it with ``origin`` where it expects it, and what the land leaves behind is
nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from precis.workers.job_types import _slice_ws as ws
from precis.workers.job_types import land_slice
from tests.test_land_slice import GREEN_LOG, SUBJECT, _Ctx, _Store

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only hub hook (sh stub) and flock"
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
TODO = 42
JOB = 901
BUILD_JOB = 7
REF = ws.build_ref(TODO, BUILD_JOB)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, **_GIT_ENV},
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def _g(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _g(repo, "rev-parse", "HEAD")


class _Rig:
    """A bare 'GitHub', the host's main checkout, the hub and a builder's
    own clone (gone by the time a land runs, but its commits are in the hub)."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        #: what the stood-in launcher was asked to start, filled by ``rig``
        self.launches: list[tuple[list[str], dict[str, Any]]] = []
        self.github = tmp_path / "github.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.github))
        self.root = tmp_path / "root"
        _git(tmp_path, "clone", "-q", str(self.github), str(self.root))
        _git(self.root, "checkout", "-q", "-b", "main")
        self.main = _commit(self.root, "base.txt", "base\n", "chore: base")
        _git(self.root, "push", "-q", "origin", "main")
        self.hub = ws.ensure_hub(self.root)
        assert ws.sync_hub(self.root, url=str(self.github), base="main") == self.main
        # the builder's clone: slice/42 on top of main, one commit
        self.builder = tmp_path / "builder"
        _git(tmp_path, "clone", "-q", str(self.github), str(self.builder))
        _git(self.builder, "checkout", "-q", "-b", f"slice/{TODO}")
        self.head = _commit(self.builder, "feature.txt", "feature\n", SUBJECT)

    def push_attempt(self, sha: str, *, job: int = BUILD_JOB) -> str:
        """What the builder (or the worker's finalize) leaves in the hub: the
        attempt's ref. A fetch into the bare hub — no hook, as the worker's
        own writes."""
        ref = ws.build_ref(TODO, job)
        _git(
            self.hub,
            "fetch",
            "-q",
            "--no-tags",
            str(self.builder),
            f"+{sha}:{ref}",
        )
        assert ws.hub_tip(self.hub, ref) == sha
        return ref

    def snapshot_on_top(self) -> str:
        """A ``wip:`` snapshot commit on top of ``head``, as ``finalize`` makes."""
        (self.builder / "leftover.txt").write_text("unfinished\n", encoding="utf-8")
        _git(self.builder, "add", "leftover.txt")
        _git(
            self.builder,
            "commit",
            "-q",
            "-m",
            f"wip: snapshot session exited (td{TODO})",
        )
        return _g(self.builder, "rev-parse", "HEAD")

    def advance_github(self, name: str, text: str) -> str:
        sha = _commit(self.root, name, text, f"chore: {name}")
        _git(self.root, "push", "-q", "origin", "main")
        return sha

    def tree(self, job: int = JOB) -> Path:
        return ws.attempt_dir(self.root, TODO, job, ws.ROLE_LAND)

    def github_ref(self, ref: str) -> str | None:
        out = _git(self.github, "rev-parse", "--verify", "--quiet", ref, check=False)
        return out.stdout.strip() or None


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    for k, v in _GIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path / "ops"))
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("PRECIS_SHIP_BIN", raising=False)
    monkeypatch.delenv("PRECIS_FACTORY_GIT_TOKEN", raising=False)
    the_rig = _Rig(tmp_path)
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(the_rig.root))
    # the repository's GitHub url is the bare repository's path
    monkeypatch.setattr(land_slice, "_github_url", lambda root: str(the_rig.github))
    # ship, docker, the drift probe and the selector are stood in
    launches: list[tuple[list[str], dict[str, Any]]] = []

    def fake_launch(argv: list[str], **kw: Any) -> int:
        launches.append((argv, kw))
        Path(kw["log"]).touch()  # the real launcher opens the ship's log
        return 5150 + len(launches)

    monkeypatch.setattr(land_slice, "_launch", fake_launch)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: False)
    monkeypatch.setattr(land_slice, "_sweep_stacks", lambda *, keep=None: [])
    monkeypatch.setattr(land_slice, "_evict_stack", lambda project, *, volumes: True)
    monkeypatch.setattr(
        land_slice,
        "_drift_preflight",
        lambda tree, **kw: {"ran": False, "rc": None, "age": "", "diag": ""},
    )
    monkeypatch.setattr(land_slice, "spot_selection", lambda tree: False)
    monkeypatch.setattr(land_slice, "_pending_gates", lambda store, plan, todo: [])
    monkeypatch.setattr(
        "precis.workers.slice_stacks.evict_project", lambda project, *, volumes: True
    )
    the_rig.launches = launches
    return the_rig


def _store(**meta: Any) -> _Store:
    store = _Store()
    store.add_todo(TODO, tags=("claimed-by:lane-claude-sub",), meta=meta)
    return store


def _params(**extra: Any) -> dict[str, Any]:
    return {"todo_id": TODO, "branch": f"slice/{TODO}", "lane": "claude-sub", **extra}


def _submit(store: _Store, params: dict[str, Any]) -> tuple[_Ctx, Any]:
    ctx = _Ctx(store, params)
    ctx.ref_id = JOB
    return ctx, land_slice.submit(ctx, None)


def _finish(handle: dict[str, Any], exit_code: int, log: str) -> None:
    Path(handle["log"]).write_text(log, encoding="utf-8")
    Path(handle["exit_file"]).write_text(f"{exit_code}\n", encoding="utf-8")


def _poll(store: _Store, params: dict[str, Any], handle: dict[str, Any]) -> _Ctx:
    ctx = _Ctx(store, params)
    ctx.ref_id = JOB
    assert land_slice.poll(ctx, handle) is True
    return ctx


# ── a land of a pushed attempt ─────────────────────────────────────


def test_a_land_cuts_at_head_runs_ship_in_the_clone_and_leaves_nothing(
    rig: _Rig,
) -> None:
    """The whole flow: the clone is cut at ``head`` on ``slice/42`` with
    ``origin/main`` and GitHub as its origin, ship launches in it, and after
    the green land the folder, the hub's attempt refs and the task's
    ``build_ref`` are gone. The builder's own folder is not involved."""
    ref = rig.push_attempt(rig.head)
    store = _store(build_ref={"state": "pushed", "ref": REF, "sha": rig.head})
    params = _params(ref=ref, head=rig.head)

    ctx, handle = _submit(store, params)

    assert ctx.failure is None and handle is not None
    tree = rig.tree()
    assert handle["worktree"] == str(tree) and tree.name == "slice-td42-land-j901"
    # the clone: its own git dir, the branch at head, origin as ship expects it
    assert (tree / ".git").is_dir()
    assert _g(tree, "rev-parse", "HEAD") == rig.head
    assert _g(tree, "rev-parse", "--abbrev-ref", "HEAD") == "slice/42"
    assert _g(tree, "remote", "get-url", "origin") == str(rig.github)
    assert _g(tree, "rev-parse", "refs/remotes/origin/main") == rig.main
    assert (tree / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    # ship launched in it, on its own scripts
    [(argv, kw)] = rig.launches
    assert kw["cwd"] == tree and argv[0] == str(tree / "scripts" / "ship")
    assert argv[1] == "--quick" and argv[2] == SUBJECT
    # the land lock is the todo's, in the host checkout's git common dir,
    # and names this folder
    lock = rig.root / ".git" / land_slice.LAND_LOCK_TODO_FILE.format(todo=TODO)
    assert f"tree={tree}" in lock.read_text(encoding="utf-8")
    assert not (tree / ".git" / land_slice.LAND_LOCK_FILE).exists()
    assert land_slice._worktree_gitdir(tree) == (tree / ".git").resolve()

    # ship exits green: the land record, then nothing left behind
    _finish(handle, 0, GREEN_LOG)
    pctx = _poll(store, params, handle)

    assert pctx.status == "succeeded" and pctx.failure is None
    assert not tree.exists()
    assert ws.hub_builds(rig.hub, TODO) == {}
    assert store.refs[TODO].meta["build_ref"] is None
    assert any(c.startswith("landed:") for c in store.comments(TODO))
    assert "STATUS:done" in store.tag_strings(TODO)
    # the host checkout was never touched: no worktree, no branch
    assert "slice/42" not in _g(rig.root, "branch", "--list")
    assert _g(rig.root, "worktree", "list").count("\n") == 0


def test_head_below_a_wip_snapshot_is_what_gets_landed(rig: _Rig) -> None:
    """The ref's tip is the worker's ``wip:`` snapshot; ``head`` is the
    builder's last real commit below it. The land checks out ``head``: the
    snapshot's files are not in the clone."""
    snapshot = rig.snapshot_on_top()
    ref = rig.push_attempt(snapshot)
    assert ws.hub_tip(rig.hub, ref) == snapshot != rig.head
    store = _store()

    ctx, handle = _submit(store, _params(ref=ref, head=rig.head))

    assert ctx.failure is None and handle is not None
    tree = rig.tree()
    assert _g(tree, "rev-parse", "HEAD") == rig.head
    assert _g(tree, "rev-parse", "slice/42") == rig.head
    assert not (tree / "leftover.txt").exists()
    # (the repo's .gitignore hides scratch/ in a real checkout; this rig has none)
    assert _g(tree, "status", "--porcelain", "--untracked-files=no") == ""
    # the snapshot is still reachable in the clone (origin's attempt ref)
    assert (
        _git(tree, "cat-file", "-e", f"{snapshot}^{{commit}}", check=False).returncode
        == 0
    )


def test_a_ref_that_no_longer_holds_head_lands_nothing(rig: _Rig) -> None:
    """A rebuild replaced the ref: ``head`` is not its tip nor an ancestor of
    it. The land fails attempt-free; no clone is left, nothing reached
    GitHub, no attempt is charged."""
    other = _commit(rig.builder, "other.txt", "other\n", "feat(y): other (td42)")
    _git(rig.builder, "reset", "-q", "--hard", rig.main)
    replaced = _commit(rig.builder, "replaced.txt", "replaced\n", "feat(z): z (td42)")
    ref = rig.push_attempt(replaced)
    # ``head`` exists in the hub (it came with the replaced attempt's objects)
    _git(
        rig.hub,
        "fetch",
        "-q",
        "--no-tags",
        str(rig.builder),
        f"+{other}:refs/x/other",
        check=False,
    )
    store = _store()

    ctx, handle = _submit(store, _params(ref=ref, head=rig.head))

    assert handle is None and ctx.status == "failed"
    assert "no longer holds" in (ctx.failure or "") or "does not have" in (
        ctx.failure or ""
    )
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert "attempt:1" not in store.tag_strings(TODO)
    assert not rig.tree().exists()
    assert rig.launches == []
    assert rig.github_ref("refs/heads/main") == rig.main
    assert not any(c.startswith("landed:") for c in store.comments(TODO))
    # the hub still holds the work
    assert ws.hub_tip(rig.hub, ref) == replaced


def test_a_head_that_is_not_below_the_tip_is_refused(rig: _Rig) -> None:
    """``head`` is a commit the hub has but the ref's tip does not descend
    from (a sibling attempt's work): refused, nothing cut."""
    sibling = _commit(rig.builder, "sib.txt", "sib\n", "feat(s): s (td42)")
    _git(rig.builder, "reset", "-q", "--hard", rig.main)
    tip = _commit(rig.builder, "tip.txt", "tip\n", "feat(t): t (td42)")
    ref = rig.push_attempt(tip)
    _git(
        rig.hub,
        "fetch",
        "-q",
        "--no-tags",
        str(rig.builder),
        f"+{sibling}:refs/factory/build/42/3",
    )
    store = _store()

    ctx, handle = _submit(store, _params(ref=ref, head=sibling))

    assert handle is None and f"it is at {tip[:12]}" in (ctx.failure or "")
    assert not rig.tree().exists()


# ── a land minted before the switch ────────────────────────────────


def test_a_legacy_land_imports_the_branch_github_holds_and_deletes_it(
    rig: _Rig,
) -> None:
    """No ``ref`` on the params: the slice GitHub holds under
    ``refs/factory/slice/42`` is imported into the hub as attempt ref 0, the
    land cuts from it, and after the land GitHub's legacy ref is deleted
    along with the hub's."""
    _git(rig.builder, "push", "-q", str(rig.github), f"+HEAD:refs/factory/slice/{TODO}")
    assert rig.github_ref(f"refs/factory/slice/{TODO}") == rig.head
    store = _store()
    params = _params(head=rig.head, worktree="/gone/slice-td42")

    ctx, handle = _submit(store, params)

    assert ctx.failure is None and handle is not None
    assert ws.hub_tip(rig.hub, ws.build_ref(TODO, 0)) == rig.head
    tree = rig.tree()
    assert _g(tree, "rev-parse", "HEAD") == rig.head
    assert handle["worktree"] == str(tree)  # params.worktree was ignored

    _finish(handle, 0, GREEN_LOG)
    pctx = _poll(store, params, handle)

    assert pctx.status == "succeeded"
    assert rig.github_ref(f"refs/factory/slice/{TODO}") is None
    assert ws.hub_builds(rig.hub, TODO) == {}
    assert not tree.exists()


def test_a_legacy_land_takes_head_from_the_pending_land_then_the_tip(
    rig: _Rig,
) -> None:
    second = _commit(rig.builder, "second.txt", "2\n", "feat(x): second (td42)")
    _git(rig.builder, "push", "-q", str(rig.github), f"+HEAD:refs/factory/slice/{TODO}")
    store = _store(pending_land={"head": rig.head})  # below the imported tip

    ctx, handle = _submit(store, _params())

    assert ctx.failure is None and handle is not None
    assert _g(rig.tree(), "rev-parse", "HEAD") == rig.head
    assert second != rig.head


def test_a_legacy_land_with_no_work_anywhere_fails_attempt_free(rig: _Rig) -> None:
    ctx, handle = _submit(_store(), _params(head=rig.head))

    assert handle is None and "names no hub ref" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert not rig.tree().exists()


# ── a land red on a conflict ───────────────────────────────────────

REBASE_CONFLICT_LOG = (
    "quick ship (--quick)\n"
    "\x1b[31m✖ rebase conflict onto origin/main — resolve in /w, then 'git add "
    "-A && GIT_EDITOR=true git rebase --continue' and re-run scripts/ship (or "
    "'git rebase --abort' to back out).\x1b[0m\n"
    "SHIP_STATUS: conflict\n"
)


def test_a_rebase_conflict_stamps_the_task_from_the_clone_then_drops_it(
    rig: _Rig,
) -> None:
    """Ship stops mid-rebase in the clone; the conflicted path is read from
    it before the folder is dropped, and the handoff and the attempt are as
    they always were."""
    # main moves under the builder's file; the builder's commit adds the
    # same path, so the rebase conflicts on it
    other_main = rig.advance_github("feature.txt", "someone else's feature\n")
    ref = rig.push_attempt(rig.head)
    store = _store()
    params = _params(ref=ref, head=rig.head)
    _ctx, handle = _submit(store, params)
    tree = rig.tree()
    # ship's work, in the clone: fetch the moved main, rebase, stop on it
    _git(tree, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    rebase = _git(tree, "rebase", "origin/main", check=False)
    assert rebase.returncode != 0
    _finish(handle, 1, REBASE_CONFLICT_LOG)

    pctx = _poll(store, params, handle)

    assert pctx.status == "failed" and "attempt:1" in store.tag_strings(TODO)
    assert store.comments(TODO)[-1].startswith(
        "handoff: lander: land FAILED on slice/42"
    )
    conflict = store.refs[TODO].meta["conflict"]
    assert conflict["head"] == rig.head
    assert conflict["base_tip"] == other_main
    assert conflict["files"] == ["feature.txt"]
    assert conflict["at"].endswith("Z")
    assert conflict["job"] == JOB
    assert not tree.exists()  # dropped: the hub holds the work
    assert ws.hub_tip(rig.hub, ref) == rig.head
    assert not any(c.startswith("landed:") for c in store.comments(TODO))


def test_an_ordinary_red_drops_the_clone_and_keeps_the_hubs_refs(rig: _Rig) -> None:
    ref = rig.push_attempt(rig.head)
    store = _store(build_ref={"state": "pushed", "ref": ref})
    params = _params(ref=ref, head=rig.head)
    _ctx, handle = _submit(store, params)
    _finish(handle, 1, "x\n\x1b[31m✖ ruff: 1 error\x1b[0m\n")

    pctx = _poll(store, params, handle)

    assert pctx.status == "failed"
    assert not rig.tree().exists()
    assert ws.hub_tip(rig.hub, ref) == rig.head
    assert store.refs[TODO].meta["build_ref"]["ref"] == ref
    assert "conflict" not in store.refs[TODO].meta


# ── a job resumed in its own folder ────────────────────────────────


def test_a_resumed_land_job_finds_its_own_folder(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same job submitted again (an outage relaunch) names the same
    folder: it is there, it is the job's own, and nothing is cut again."""
    ref = rig.push_attempt(rig.head)
    store = _store()
    params = _params(ref=ref, head=rig.head)
    _ctx, _handle = _submit(store, params)
    tree = rig.tree()
    marker = tree / "scratch" / "marker"
    marker.write_text("first run\n", encoding="utf-8")
    cuts: list[Path] = []
    real_cut = ws.cut

    def counting_cut(hub: Path, t: Path, **kw: Any) -> str:
        cuts.append(t)
        return real_cut(hub, t, **kw)

    monkeypatch.setattr(ws, "cut", counting_cut)

    ctx, handle = _submit(store, params)

    assert ctx.failure is None and handle is not None
    assert cuts == []
    assert marker.read_text(encoding="utf-8") == "first run\n"
    assert handle["worktree"] == str(tree)
    assert len(rig.launches) == 2


def test_a_new_land_job_of_the_same_todo_cuts_a_folder_of_its_own(rig: _Rig) -> None:
    """Another job (a re-mint after an attempt-free failure) never enters the
    first job's folder, even while it is still on disk."""
    ref = rig.push_attempt(rig.head)
    store = _store()
    params = _params(ref=ref, head=rig.head)
    _submit(store, params)
    first = rig.tree()

    ctx = _Ctx(store, params)
    ctx.ref_id = JOB + 1
    handle = land_slice.submit(ctx, None)

    second = rig.tree(JOB + 1)
    assert ctx.failure is None and handle["worktree"] == str(second)
    assert first.is_dir() and second.is_dir() and first != second
    assert _g(second, "rev-parse", "HEAD") == rig.head


# ── what ship expects of the clone's origin ────────────────────────


def test_ships_git_calls_work_in_the_clone_through_the_mirror_redirect(
    rig: _Rig,
) -> None:
    """``scripts/ship`` fetches ``origin main``, ``origin prod`` and
    ``origin +release/r<N>`` and pushes ``origin NEW:refs/heads/main``
    under a lease (scripts/ship), expecting ``origin/main`` to resolve
    already and the others once fetched. In the clone ``origin`` is
    GitHub's url, and the mirror's url rewrite
    (``mirror_ship_env``) turns every one of those contacts into the
    lander's local mirror: the same calls, run here with real git against a
    mirror holding main, prod and a release branch, fetch and push there
    and leave the clone with all three tracking refs."""
    from precis.workers import factory_git_mirror as mirror_mod

    prod = rig.main
    _git(rig.root, "push", "-q", "origin", "main:prod")
    _git(rig.root, "push", "-q", "origin", "main:refs/heads/release/r7")
    mirror = rig.tmp / "mirror.git"
    _git(rig.tmp, "init", "-q", "--bare", "-b", "main", str(mirror))
    _git(rig.github, "push", "-q", str(mirror), "main", "prod", "release/r7")
    ref = rig.push_attempt(rig.head)
    _ctx, handle = _submit(_store(), _params(ref=ref, head=rig.head))
    tree = rig.tree()
    assert handle is not None
    # before ship runs: the base's tracking ref the lander and ship read
    assert _g(tree, "rev-parse", "refs/remotes/origin/main") == rig.main
    assert _g(tree, "config", "--get", "remote.origin.fetch") == (
        "+refs/heads/*:refs/remotes/origin/*"
    )

    env = mirror_mod.mirror_ship_env(
        mirror, {**os.environ, **_GIT_ENV}, str(rig.github)
    )

    def ship_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(tree),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    for args in (
        ("fetch", "-q", "origin", "main"),
        ("fetch", "-q", "origin", "prod:refs/remotes/origin/prod"),
        (
            "fetch",
            "-q",
            "origin",
            "+refs/heads/release/r7:refs/remotes/origin/release/r7",
        ),
    ):
        done = ship_git(*args)
        assert done.returncode == 0, (args, done.stderr)
    assert _g(tree, "rev-parse", "origin/main") == rig.main
    assert _g(tree, "rev-parse", "origin/prod") == prod
    assert _g(tree, "rev-parse", "origin/release/r7") == rig.main
    # ship's CAS push of the squash lands in the MIRROR, not on GitHub
    new = _commit(tree, "squash.txt", "s\n", SUBJECT)
    pushed = ship_git(
        "push",
        f"--force-with-lease=main:{rig.main}",
        "origin",
        f"{new}:refs/heads/main",
    )
    assert pushed.returncode == 0, pushed.stderr
    assert _g(mirror, "rev-parse", "refs/heads/main") == new
    assert rig.github_ref("refs/heads/main") == rig.main


# ── round two: unfinished folders, the host's own branch, floors, locks ──


def _lock_file(rig: _Rig) -> Path:
    return rig.root / ".git" / land_slice.LAND_LOCK_TODO_FILE.format(todo=TODO)


def test_a_half_cut_folder_is_dropped_and_cut_again(rig: _Rig) -> None:
    """The worker died after ``cut`` renamed the clone into place and before
    the branch was put on ``head`` and ``origin`` set: the folder stands on
    the ref's ``wip:`` tip with ``origin`` the hub. The resubmitted job has
    no launch on record, so the folder is dropped and cut again."""
    snapshot = rig.snapshot_on_top()
    ref = rig.push_attempt(snapshot)
    tree = rig.tree()
    # exactly what a real cut leaves when the worker dies right after it
    ws.cut(
        rig.hub,
        tree,
        base="main",
        branch="slice/42",
        start=ws.Start(sha=snapshot, ref=ref),
    )
    assert _g(tree, "rev-parse", "HEAD") == snapshot
    assert _g(tree, "remote", "get-url", "origin") == str(rig.hub)
    (tree / "stale-marker").write_text("x\n", encoding="utf-8")

    ctx, handle = _submit(_store(), _params(ref=ref, head=rig.head))

    assert ctx.failure is None and handle is not None
    assert not (tree / "stale-marker").exists()  # a fresh clone
    assert _g(tree, "rev-parse", "HEAD") == rig.head
    assert _g(tree, "remote", "get-url", "origin") == str(rig.github)
    assert not (tree / "leftover.txt").exists()
    assert any("left unfinished" in t for _k, t in ctx.chunks)


def test_a_launched_folder_is_not_cut_again(rig: _Rig) -> None:
    """With a launch on record the folder is the job's, as before."""
    ref = rig.push_attempt(rig.head)
    params = _params(ref=ref, head=rig.head)
    store = _store()
    _submit(store, params)  # cuts and launches (the log is the evidence)
    tree = rig.tree()
    (tree / "keep").write_text("k\n", encoding="utf-8")

    ctx, handle = _submit(store, params)

    assert ctx.failure is None and handle is not None
    assert (tree / "keep").exists()


def _host_branch(rig: _Rig, sha: str) -> None:
    """The old engine's builder worktree shared the host checkout's git dir:
    its ``slice/42`` branch is in the checkout."""
    _git(rig.root, "fetch", "-q", str(rig.builder), f"+{sha}:refs/heads/slice/{TODO}")


def test_a_legacy_land_imports_the_host_checkouts_slice_branch(rig: _Rig) -> None:
    """No ``ref``, nothing on GitHub: the branch the old engine left in the
    host checkout holds ``head`` and is brought into the hub (a fetch from
    the checkout's path), without GitHub."""
    _host_branch(rig, rig.head)
    assert rig.github_ref(f"refs/factory/slice/{TODO}") is None

    ctx, handle = _submit(_store(), _params(head=rig.head))

    assert ctx.failure is None and handle is not None
    assert ws.hub_tip(rig.hub, ws.build_ref(TODO, 0)) == rig.head
    assert _g(rig.tree(), "rev-parse", "HEAD") == rig.head
    assert ctx.meta_set[land_slice.LAND_START_KEY] == {
        "head": rig.head,
        "ref": ws.build_ref(TODO, 0),
    }


def test_a_host_branch_below_head_is_not_used_and_github_is(rig: _Rig) -> None:
    """The checkout's ``slice/42`` does not hold ``head`` (it is behind):
    skipped, and GitHub's legacy ref is imported as before."""
    _host_branch(rig, rig.main)  # behind head
    _git(rig.builder, "push", "-q", str(rig.github), f"+HEAD:refs/factory/slice/{TODO}")

    ctx, handle = _submit(_store(), _params(head=rig.head))

    assert ctx.failure is None and handle is not None
    assert ws.hub_tip(rig.hub, ws.build_ref(TODO, 0)) == rig.head


def test_a_stale_import_in_the_hub_is_deleted_and_imported_again(rig: _Rig) -> None:
    """The hub already has ``build/42/0`` from an earlier land, but it does
    not hold this land's ``head``: it is deleted and the import runs again
    instead of failing every later land."""
    older = _commit(rig.builder, "older.txt", "o\n", "feat(o): older (td42)")
    _git(rig.builder, "reset", "-q", "--hard", rig.main)
    rig.head = _commit(rig.builder, "feature2.txt", "f2\n", SUBJECT)
    stale_ref = ws.build_ref(TODO, 0)
    _git(rig.hub, "fetch", "-q", "--no-tags", str(rig.builder), f"+{older}:{stale_ref}")
    assert ws.hub_tip(rig.hub, stale_ref) == older
    _host_branch(rig, rig.head)

    ctx, handle = _submit(_store(), _params(head=rig.head))

    assert ctx.failure is None and handle is not None
    assert ws.hub_tip(rig.hub, stale_ref) == rig.head
    assert _g(rig.tree(), "rev-parse", "HEAD") == rig.head


def test_a_legacy_land_with_the_work_nowhere_names_both_places(rig: _Rig) -> None:
    ctx, handle = _submit(_store(), _params(head=rig.head))
    assert handle is None
    assert "host checkout" in (ctx.failure or "") and "GitHub" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"


def test_a_failed_github_import_reads_as_the_workspace_layers_own_message(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``import_legacy`` raising (GitHub holds the ref, the import failed)
    is a ``WorkspaceError``: the land fails attempt-free with its message."""

    def refuse(hub: Path, url: str, todo: int, **kw: Any) -> str | None:
        raise ws.HubUnreadable(
            "GitHub holds refs/factory/slice/42 but it could not be fetched; next: retry"
        )

    monkeypatch.setattr(ws, "import_legacy", refuse)
    ctx, handle = _submit(_store(), _params(head=rig.head))
    assert handle is None and "could not be fetched" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert not rig.tree().exists()


def test_the_floor_and_the_lock_after_a_land_with_a_ref_left_behind(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The land stamps ``build_floor`` with its own job id and clears
    ``build_ref`` even when the hub's ref could not be deleted; the per-todo
    lock file is gone with the land."""
    ref = rig.push_attempt(rig.head)
    monkeypatch.setattr(ws, "delete_builds", lambda hub, todo: "FAILED for j7: busy")
    store = _store(build_ref={"ref": ref})
    params = _params(ref=ref, head=rig.head)
    _ctx, handle = _submit(store, params)
    assert _lock_file(rig).exists()
    _finish(handle, 0, GREEN_LOG)

    pctx = _poll(store, params, handle)

    assert pctx.status == "succeeded"
    assert ws.hub_tip(rig.hub, ref) == rig.head  # the ref is still there ...
    meta = store.refs[TODO].meta
    assert meta["build_floor"] == JOB > BUILD_JOB  # ... and harmless
    assert meta["build_ref"] is None
    assert not _lock_file(rig).exists()


def test_the_lock_file_stays_under_a_live_holder_then_goes(rig: _Rig) -> None:
    import fcntl

    ref = rig.push_attempt(rig.head)
    store = _store()
    params = _params(ref=ref, head=rig.head)
    _ctx, handle = _submit(store, params)
    _finish(handle, 1, "x\n\x1b[31m✖ ruff: 1 error\x1b[0m\n")
    # an orphan of the exited ship still holds the lock: file and folder stay
    fd = os.open(_lock_file(rig), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        first = _poll(store, params, handle)
        assert first.status == "failed"
        assert _lock_file(rig).exists() and rig.tree().is_dir()
    finally:
        os.close(fd)
    land_slice._unlink_land_lock(rig.tree(), TODO)
    assert not _lock_file(rig).exists()


def test_a_legacy_conflict_stamp_carries_the_ref_the_cut_resolved(rig: _Rig) -> None:
    """A legacy land has no ``params.ref`` and ship's squash can move the
    clone's branch; the stamp's ``head`` and ``ref`` are the cut's."""
    _host_branch(rig, rig.head)
    other_main = rig.advance_github("feature.txt", "someone else's feature\n")
    store = _store()
    params = _params(head=rig.head)
    ctx, handle = _submit(store, params)
    tree = rig.tree()
    _git(tree, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    assert _git(tree, "rebase", "origin/main", check=False).returncode != 0
    _finish(handle, 1, REBASE_CONFLICT_LOG)
    pctx = _Ctx(store, params)
    pctx.ref_id = JOB
    # what the framework hands poll: the job's meta, with the cut's record
    pctx.meta[land_slice.LAND_START_KEY] = ctx.meta_set[land_slice.LAND_START_KEY]

    assert land_slice.poll(pctx, handle) is True

    conflict = store.refs[TODO].meta["conflict"]
    assert conflict["head"] == rig.head and conflict["ref"] == ws.build_ref(TODO, 0)
    assert conflict["job"] == JOB
    assert conflict["base_tip"] == other_main and conflict["files"] == ["feature.txt"]


# ── round three: wip tips, and a folder that lost its .git ─────────


def test_head_below_a_real_wip_tip_still_lands_head(rig: _Rig) -> None:
    snapshot = rig.snapshot_on_top()
    ref = rig.push_attempt(snapshot)
    ctx, handle = _submit(_store(), _params(ref=ref, head=rig.head))
    assert ctx.failure is None and handle is not None
    assert _g(rig.tree(), "rev-parse", "HEAD") == rig.head


def test_no_head_and_a_real_wip_tip_is_refused(rig: _Rig) -> None:
    snapshot = rig.snapshot_on_top()
    ref = rig.push_attempt(snapshot)
    store = _store()
    ctx, handle = _submit(store, _params(ref=ref))
    assert handle is None
    assert "unfinished snapshot; next: let a build finish it" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert "attempt:1" not in store.tag_strings(TODO)
    assert not rig.tree().exists() and rig.launches == []


def test_no_head_and_a_real_finished_tip_lands_the_tip(rig: _Rig) -> None:
    ref = rig.push_attempt(rig.head)
    ctx, handle = _submit(_store(), _params(ref=ref))
    assert ctx.failure is None and handle is not None
    assert _g(rig.tree(), "rev-parse", "HEAD") == rig.head


def _host_state(rig: _Rig) -> dict[str, str]:
    return {
        "head": _g(rig.root, "rev-parse", "HEAD"),
        "branch": _g(rig.root, "symbolic-ref", "HEAD"),
        "status": _g(rig.root, "status", "--porcelain"),
        "index": (rig.root / ".git" / "index").read_bytes().hex(),
    }


def test_a_land_folder_without_its_own_git_dir_never_resolves_to_the_host(
    rig: _Rig,
) -> None:
    """gr742505: the land clone sits under ``<host checkout>/.claude/
    worktrees``. If its ``.git`` is gone, git run in it must fail — not walk
    up and act on the host checkout (reset, checkout, clean, lock removal)."""
    folder = rig.tree()
    folder.mkdir(parents=True)
    (folder / "scratch").mkdir()
    (folder / "file.txt").write_text("x\n", encoding="utf-8")
    before = _host_state(rig)

    # without the ceiling git finds the host checkout (the hazard)
    bare = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(folder),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={k: v for k, v in os.environ.items() if k != "GIT_CEILING_DIRECTORIES"},
        check=False,
    )
    assert bare.returncode == 0

    # the lander's helper fails, and so does everything it launches
    assert land_slice._git(["rev-parse", "--git-dir"], folder).returncode != 0
    assert land_slice._git(["reset", "--hard"], folder).returncode != 0
    assert land_slice._git(["clean", "-fdx"], folder).returncode != 0
    ship_like = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(folder),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=land_slice.land_env(),
        check=False,
    )
    assert ship_like.returncode != 0
    # a deeper cwd inside the folder is no way out either
    (folder / "scratch" / "deep").mkdir()
    assert (
        land_slice._git(
            ["rev-parse", "--git-dir"], folder / "scratch" / "deep"
        ).returncode
        != 0
    )

    assert _host_state(rig) == before
    assert (folder / "file.txt").exists()  # the clean never ran
    assert not list((rig.root / ".git").glob("*.lock"))


def test_the_ceiling_leaves_the_host_a_clone_and_a_worktree_alone(rig: _Rig) -> None:
    """Only walking UP into the worktrees directory is stopped: the host
    checkout as cwd, a clone with its own ``.git`` directory (also from a
    subdirectory) and a linked worktree with a ``.git`` file (the
    second-repository lander's shape) all work as before."""
    assert land_slice._git(["rev-parse", "--git-dir"], rig.root).returncode == 0
    ref = rig.push_attempt(rig.head)
    _submit(_store(), _params(ref=ref, head=rig.head))
    clone = rig.tree()
    assert land_slice._git(["rev-parse", "--git-dir"], clone).stdout.strip() == ".git"
    (clone / "sub").mkdir()
    got = land_slice._git(["rev-parse", "--show-toplevel"], clone / "sub")
    assert Path(got.stdout.strip()).resolve() == clone.resolve()
    worktree = rig.root / ".claude" / "worktrees" / "slice-pr-7"
    add = land_slice._git(
        ["worktree", "add", "--detach", str(worktree), "main"], rig.root
    )
    assert add.returncode == 0, add.stderr
    assert (worktree / ".git").is_file()
    assert land_slice._git(["rev-parse", "--git-dir"], worktree).returncode == 0
    assert land_slice._git(["status", "--porcelain"], worktree).returncode == 0
    # and a worktree that lost its .git file fails too
    (worktree / ".git").unlink()
    assert land_slice._git(["rev-parse", "--git-dir"], worktree).returncode != 0


def test_land_env_names_the_worktrees_directory(rig: _Rig) -> None:
    """The allowlist env carries the ceiling too, so ship and everything
    under it inherit it."""
    entries = land_slice.land_env()["GIT_CEILING_DIRECTORIES"].split(os.pathsep)
    assert str(rig.root / ".claude" / "worktrees") in entries
