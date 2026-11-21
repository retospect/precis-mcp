"""Attempt workspaces (``_slice_ws``): real git in ``tmp_path``, no database, no
network. A local bare repository plays GitHub; the main checkout is a clone of
it; the hub is the real ``precis-hub.git`` with its real ``pre-receive`` hook,
so a session's push here goes through the same fence it does on a host."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from precis.workers import slice_stacks
from precis.workers.job_types import _slice_park
from precis.workers.job_types import _slice_ws as ws

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only hub hook (sh stub) and flock"
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
TD = 5
JOB = 9
#: Obviously fake, but shaped like a GitHub token: split so this file does not
#: itself trip the repo's secret scan.
FAKE_TOKEN = "".join(("gh", "p_", "a1B2c3D4e5F6", "g7H8i9J0k1L2", "m3N4o5P6q7R8"))


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True):
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, **_GIT_ENV, **(env or {})},
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def _g(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _commit(repo: Path, name: str, text: str = "x\n", msg: str | None = None) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg or f"add {name}")
    return _g(repo, "rev-parse", "HEAD")


def _session_push(
    tree: Path,
    refspec: str,
    *,
    todo: int = TD,
    job: int = JOB,
    extra: tuple[str, ...] = (),
):
    """``git push origin <refspec>`` as the builder session of ``todo``/``job``."""
    return _git(
        tree,
        "push",
        *extra,
        "origin",
        refspec,
        env={ws.ENV_TODO: str(todo), ws.ENV_JOB: str(job)},
        check=False,
    )


def _hook_lines(stderr: str) -> list[str]:
    return [
        ln.removeprefix("remote: ").rstrip()
        for ln in stderr.splitlines()
        if ln.startswith("remote: ")
    ]


class _Rig:
    """A bare 'GitHub', the host's main checkout (a clone of it) and the hub."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.github = tmp_path / "github.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.github))
        self.root = tmp_path / "root"
        _git(tmp_path, "clone", "-q", str(self.github), str(self.root))
        _git(self.root, "checkout", "-q", "-b", "main")
        self.main = _commit(self.root, "first.txt", "first\n")
        _git(self.root, "push", "-q", "origin", "main")
        self.hub = ws.ensure_hub(self.root)
        tip = ws.sync_hub(self.root, url=str(self.github), base="main")
        assert tip == self.main

    def advance_github(self, name: str, text: str = "more\n") -> str:
        """A new commit on GitHub's main (through the main checkout)."""
        sha = _commit(self.root, name, text)
        _git(self.root, "push", "-q", "origin", "main")
        return sha

    def tree(self, todo: int = TD, job: int = JOB, role: str = ws.ROLE_BUILD) -> Path:
        return ws.attempt_dir(self.root, todo, job, role)

    def cut(self, todo: int = TD, job: int = JOB, **kw: Any) -> Path:
        tree = self.tree(todo, job)
        ws.cut(self.hub, tree, base="main", branch=f"slice/{todo}", **kw)
        return tree

    def hub_ref(self, ref: str) -> str | None:
        return ws.hub_tip(self.hub, ref)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    for k, v in _GIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return _Rig(tmp_path)


# ── names ──────────────────────────────────────────────────────────


def test_ref_and_folder_names_round_trip(tmp_path: Path) -> None:
    """A name the engine writes must parse back to the same ids. If this
    fails, a sweep or resolve_start misreads which task a ref or folder is."""
    assert ws.build_ref(5, 9) == "refs/factory/build/5/9"
    assert ws.parse_build_ref("refs/factory/build/5/9") == (5, 9)
    assert ws.legacy_ref(5) == "refs/factory/slice/5"
    for bad in ("refs/factory/build/5", "refs/factory/build/5/x", "refs/heads/main"):
        assert ws.parse_build_ref(bad) is None
    build = ws.attempt_dir(tmp_path, 5, 9)
    land = ws.attempt_dir(tmp_path, 5, 9, ws.ROLE_LAND)
    assert build.name == "slice-td5-j9"
    assert land.name == "slice-td5-land-j9"
    assert build.parent == tmp_path / ".claude" / "worktrees"
    assert ws.parse_attempt_dir(build) == (5, 9, ws.ROLE_BUILD)
    assert ws.parse_attempt_dir(land) == (5, 9, ws.ROLE_LAND)
    for odd in ("slice-td5", "slice-main-td5", "slice-td5-j9.cut-12", "other"):
        assert ws.parse_attempt_dir(tmp_path / odd) is None


def test_constants_match_the_hook_and_the_ref_namespace() -> None:
    """The hook prints its own marker and ``finalize`` keys on this module's
    copy; if they drift, a refusal is never recognised. REF_NS is kept local
    to avoid importing the plan engine, and must equal the real one."""
    from precis.plans import templates
    from precis.utils import hub_hook

    assert ws.HOOK_REFUSED == hub_hook.REFUSED
    assert ws.HOOK_SECRET == hub_hook.SECRET
    assert ws.ENV_TODO == hub_hook.ENV_TODO
    assert ws.ENV_JOB == hub_hook.ENV_JOB
    assert ws.ENV_WORKER == hub_hook.ENV_WORKER
    assert ws.REF_NS == templates.SLICE_REF_NS


# ── the hub ────────────────────────────────────────────────────────


def test_ensure_hub_is_idempotent_and_configured(tmp_path: Path) -> None:
    """A second call must change nothing and report nothing. If the config
    keys are missing the hub is garbage-collected under a running clone or
    loses its reflog."""
    root = tmp_path / "root"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _commit(root, "a.txt")
    notes: list[str] = []
    hub = ws.ensure_hub(root, notes=notes)
    assert hub == root / ".git" / "precis-hub.git" == ws.hub_path(root)
    assert notes
    assert _g(hub, "rev-parse", "--is-bare-repository") == "true"
    assert _g(hub, "for-each-ref") == ""  # no refs until sync_hub writes one
    assert _g(hub, "config", "gc.auto") == "0"
    assert _g(hub, "config", "receive.autoGc") == "false"
    assert _g(hub, "config", "maintenance.auto") == "false"
    assert _g(hub, "config", "core.logAllRefUpdates") == "always"
    assert _g(hub, "config", "--file", str(hub / "config"), "core.hooksPath") == str(
        hub / "hooks"
    )
    hook = hub / "hooks" / "pre-receive"
    text = hook.read_text(encoding="utf-8")
    assert os.access(hook, os.X_OK)
    assert sys.executable in text and "precis.utils.hub_hook" in text
    again: list[str] = []
    assert ws.ensure_hub(root, notes=again) == hub
    assert again == []
    assert hook.read_text(encoding="utf-8") == text


def test_ensure_hub_links_the_checkouts_objects(tmp_path: Path) -> None:
    """The hub must hold the checkout's history at no copy cost. If objects
    are copied, not linked, every host pays the repository's size again."""
    root = tmp_path / "root"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    sha = _commit(root, "a.txt")
    hub = ws.ensure_hub(root)
    rel = Path("objects") / sha[:2] / sha[2:]
    assert os.stat(root / ".git" / rel).st_ino == os.stat(hub / rel).st_ino
    assert _git(hub, "cat-file", "-e", sha, check=False).returncode == 0


def test_ensure_hub_rewrites_a_stale_hook(rig: _Rig) -> None:
    """The hook must come from the deployed code on every call. A hook left by
    an older worker (a changed interpreter path, say) that is not rewritten
    would run old rules."""
    hook = rig.hub / "hooks" / "pre-receive"
    good = hook.read_text(encoding="utf-8")
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    notes: list[str] = []
    ws.ensure_hub(rig.root, notes=notes)
    assert hook.read_text(encoding="utf-8") == good
    assert any("pre-receive" in n for n in notes)
    hook.chmod(0o644)
    ws.ensure_hub(rig.root)
    assert os.access(hook, os.X_OK)


def test_ensure_hub_refuses_a_directory_that_is_not_a_checkout(tmp_path: Path) -> None:
    """The caller defers on WorkspaceError; any other exception would crash the
    launch."""
    with pytest.raises(ws.WorkspaceError, match="next:"):
        ws.ensure_hub(tmp_path / "nowhere")


def test_sync_hub_moves_main_and_keeps_the_old_tip_when_the_remote_is_gone(
    rig: _Rig, tmp_path: Path
) -> None:
    """The hub's base must follow GitHub, and an unreadable GitHub must never
    clear it. A stale base only costs a rebase; losing it would fail every
    launch."""
    new = rig.advance_github("two.txt")
    assert ws.sync_hub(rig.root, url=str(rig.github), base="main") == new
    assert rig.hub_ref("refs/heads/main") == new
    assert ws.sync_hub(rig.root, url=str(tmp_path / "gone.git"), base="main") is None
    assert rig.hub_ref("refs/heads/main") == new


def test_sync_hub_private_path_uses_an_ops_repository(rig: _Rig) -> None:
    """A private repository is fetched in a fresh ops repository and moved in
    without a credential. If that path breaks, private repos never sync."""
    new = rig.advance_github("two.txt")
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    tip = ws.sync_hub(rig.root, url=str(rig.github), base="main", private=True, env=env)
    assert tip == new
    assert rig.hub_ref("refs/heads/main") == new


def test_a_sync_that_loses_the_ref_update_returns_the_tip_the_other_wrote(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two launches may sync at once, with no lock between them: git lets one
    ref update through and fails the other. The loser must answer the tip
    the winner wrote, not None, or a land beside a launch would probe a
    stale main."""
    new = rig.advance_github("two.txt")
    real = ws._hub_git

    def losing(hub: Path, args: list[str]) -> Any:
        if args[:1] == ["fetch"]:
            # the other sync lands first; this one's ref update is refused
            real(hub, args)
            return subprocess.CompletedProcess(args, 1, "", "cannot lock ref")
        return real(hub, args)

    monkeypatch.setattr(ws, "_hub_git", losing)
    assert ws.sync_hub(rig.root, url=str(rig.github), base="main") == new
    assert rig.hub_ref("refs/heads/main") == new


def test_syncs_side_by_side_leave_the_hub_at_the_remote_tip(rig: _Rig) -> None:
    """Real git, four syncs at once: the hub must end on GitHub's tip and no
    sync may report a wrong one. (One may answer None when git's ref lock
    timed out under load; that is the stale-base answer, never a wrong
    tip.)"""
    from concurrent.futures import ThreadPoolExecutor

    new = rig.advance_github("two.txt")
    with ThreadPoolExecutor(4) as pool:
        tips = list(
            pool.map(
                lambda _: ws.sync_hub(rig.root, url=str(rig.github), base="main"),
                range(4),
            )
        )
    assert new in tips and set(tips) <= {new, None}
    assert rig.hub_ref("refs/heads/main") == new


def test_hub_tip_and_builds(rig: _Rig) -> None:
    """hub_builds must list exactly one task's attempts: a prefix match would
    hand td5 the work of td55."""
    for ref, sha in (
        (ws.build_ref(5, 3), rig.main),
        (ws.build_ref(5, 11), rig.main),
        (ws.build_ref(55, 1), rig.main),
    ):
        _git(rig.hub, "update-ref", ref, sha)
    assert ws.hub_builds(rig.hub, 5) == {3: rig.main, 11: rig.main}
    assert ws.hub_builds(rig.hub, 7) == {}
    assert ws.hub_tip(rig.hub, "refs/heads/main") == rig.main
    assert ws.hub_tip(rig.hub, "refs/heads/none") is None
    with pytest.raises(ws.HubUnreadable, match="next:"):
        ws.hub_builds(rig.tmp / "no-hub.git", 5)


def test_import_legacy_brings_the_old_slice_ref_across(
    rig: _Rig, tmp_path: Path
) -> None:
    """Work pushed before the switch must be continued, not lost. The second
    call must not need GitHub: the hub already has it."""
    _git(rig.root, "checkout", "-q", "-b", "slice/5")
    sha = _commit(rig.root, "old.txt", "old\n")
    _git(rig.root, "push", "-q", str(rig.github), f"{sha}:{ws.legacy_ref(5)}")
    _git(rig.root, "checkout", "-q", "main")
    assert ws.import_legacy(rig.hub, str(rig.github), 5) == sha
    assert rig.hub_ref(ws.build_ref(5, 0)) == sha
    assert ws.import_legacy(rig.hub, str(tmp_path / "gone.git"), 5) == sha
    assert ws.import_legacy(rig.hub, str(rig.github), 6) is None


def test_delete_builds_removes_one_tasks_refs(rig: _Rig) -> None:
    """After a land the task's attempt refs must go, and only its own."""
    _git(rig.hub, "update-ref", ws.build_ref(5, 3), rig.main)
    _git(rig.hub, "update-ref", ws.build_ref(5, 4), rig.main)
    _git(rig.hub, "update-ref", ws.build_ref(6, 1), rig.main)
    note = ws.delete_builds(rig.hub, 5)
    assert "2" in note
    assert ws.hub_builds(rig.hub, 5) == {}
    assert ws.hub_builds(rig.hub, 6) == {1: rig.main}
    assert "no attempt refs" in ws.delete_builds(rig.hub, 5)
    assert "not deleted" in ws.delete_builds(rig.tmp / "no-hub.git", 5)


def _loose(objects: Path) -> set[str]:
    """The ids of the loose objects in an object store."""
    return {
        d.name + f.name
        for d in objects.iterdir()
        if d.is_dir() and len(d.name) == 2
        for f in d.iterdir()
    }


def _packs(objects: Path) -> set[str]:
    return {p.name for p in (objects / "pack").glob("*.pack")}


def _age(path: Path, seconds: float = 25 * 3600) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def _connected(repo: Path) -> bool:
    """Every object the repository's refs reach is present."""
    return _git(repo, "fsck", "--connectivity-only", check=False).returncode == 0


def test_maintain_hub_runs_at_most_once_a_day(rig: _Rig) -> None:
    """The tidy-up must not run on every sweep (the sweep runs every half
    minute), and its interval is what keeps a file it deletes replaced for a
    day first."""
    first = ws.maintain_hub(rig.hub)
    assert first and "FAILED" not in first
    assert ws.maintain_hub(rig.hub) is None
    _age(rig.hub / ws._MAINTAINED)
    assert ws.maintain_hub(rig.hub) is not None
    assert ws.maintain_hub(rig.tmp / "no-hub.git") is None


def test_maintain_hub_deletes_only_what_the_run_before_it_replaced(rig: _Rig) -> None:
    """The tidy-up runs beside clones, pushes and fetches with no lock. That
    is safe only because no run deletes a file it has itself just replaced:
    the first run packs the loose objects and leaves them, the next removes
    them. If one run did both, a clone copying the hub's files in that moment
    could end up without an object it needs."""
    tree = rig.cut()
    work = _commit(tree, "work.txt", "work\n")
    assert _session_push(tree, f"HEAD:{ws.build_ref(TD, JOB)}").returncode == 0
    objects = rig.hub / "objects"
    loose, packs = _loose(objects), _packs(objects)
    assert work in loose
    first = ws.maintain_hub(rig.hub)
    assert first and "FAILED" not in first
    assert _loose(objects) == loose
    assert len(_packs(objects) - packs) == 1
    between = ws.attempt_dir(rig.root, TD, JOB + 1)
    ws.cut(
        rig.hub,
        between,
        base="main",
        branch="slice/5",
        start=ws.resolve_start(rig.hub, TD),
    )
    _age(rig.hub / ws._MAINTAINED)
    second = ws.maintain_hub(rig.hub)
    assert second and "FAILED" not in second
    assert _loose(objects) == set()
    assert _connected(rig.hub) and _connected(between) and _connected(tree)
    assert (between / "work.txt").read_text(encoding="utf-8") == "work\n"
    after = ws.attempt_dir(rig.root, TD, JOB + 2)
    ws.cut(
        rig.hub,
        after,
        base="main",
        branch="slice/5",
        start=ws.resolve_start(rig.hub, TD),
    )
    assert _g(after, "rev-parse", "HEAD") == work and _connected(after)


def test_a_clone_reads_its_packs_without_the_hubs_pack_index(rig: _Rig) -> None:
    """The hub's tidy-up keeps a multi-pack index. A clone copies files, and a
    copied index can name a pack the copy missed, hiding objects the clone
    does hold. The clone therefore drops its copy; the hub keeps its own."""
    assert "FAILED" not in (ws.maintain_hub(rig.hub) or "")
    index = Path("objects") / "pack" / "multi-pack-index"
    assert (rig.hub / index).is_file()
    tree = rig.cut()
    assert not (tree / ".git" / index).exists()
    assert (rig.hub / index).is_file()
    assert _connected(tree) and (tree / "first.txt").exists()


def test_maintain_hub_clears_gits_lock_of_a_killed_run(rig: _Rig) -> None:
    """git skips this command without a word while its own lock file exists,
    and a killed run leaves the file behind. If an old one were not removed
    the hub would never be tidied again and would only grow. A young one
    belongs to a run that is under way and is left alone."""
    objects = rig.hub / "objects"
    own = objects / "maintenance.lock"
    packs = _packs(objects)
    own.touch()
    note = ws.maintain_hub(rig.hub)
    assert note and "skipped" in note
    assert own.exists() and _packs(objects) == packs
    _age(own, 3600)
    _age(rig.hub / ws._MAINTAINED)
    note = ws.maintain_hub(rig.hub)
    assert note and "FAILED" not in note and "skipped" not in note
    assert not own.exists() and len(_packs(objects) - packs) == 1


def test_maintain_hub_asks_for_one_task_per_call_and_no_repack_without_a_pack(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git 2.39 fails the two tasks in one call on a hub whose own store has
    no pack yet ("no pack files to index"), and the tidy-up of a new hub then
    reported FAILED once a day. Each task is its own call, in order, and the
    repack is not asked for while there is no pack to combine."""
    asked: list[list[str]] = []
    real = ws._mirror._run

    def run(argv: list[str], **kw: Any) -> Any:
        tasks = [arg for arg in argv if arg.startswith("--task=")]
        if tasks:
            asked.append(tasks)
        return real(argv, **kw)

    monkeypatch.setattr(ws._mirror, "_run", run)
    empty = rig.tmp / "empty-hub.git"
    _git(rig.tmp, "init", "--quiet", "--bare", str(empty))
    note = ws.maintain_hub(empty)
    assert note and "FAILED" not in note
    assert asked == [["--task=loose-objects"]]
    del asked[:]
    tree = rig.cut()
    _commit(tree, "work.txt", "work\n")
    assert _session_push(tree, f"HEAD:{ws.build_ref(TD, JOB)}").returncode == 0
    note = ws.maintain_hub(rig.hub)
    assert note and "FAILED" not in note
    assert asked == [["--task=loose-objects"], ["--task=incremental-repack"]]


def test_maintain_hub_removes_the_ops_repository_of_a_call_that_died(
    rig: _Rig,
) -> None:
    """A sync or a push killed mid-way leaves its ops repository under the
    hub. Nothing else removes it; one still in use is young and must stay."""
    ops = rig.hub / "ops"
    dead, live = ops / "ops-dead", ops / "ops-live"
    for path in (dead, live):
        path.mkdir(parents=True)
    _age(dead, 2 * ws.CUT_TMP_MAX_AGE_S)
    ws.maintain_hub(rig.hub)
    assert not dead.exists() and live.exists()


def test_nothing_takes_a_lock_in_the_hub(rig: _Rig) -> None:
    """A sync, a cut, a session's push, a finalize and the tidy-up leave no
    lock file of ours in the hub. A leftover lock file is how a killed job
    used to block the next one."""
    rig.advance_github("two.txt")
    ws.sync_hub(rig.root, url=str(rig.github), base="main")
    tree = rig.cut()
    _commit(tree, "work.txt")
    pushed = ws.finalize(
        tree, TD, JOB, hub=rig.hub, base="main", start_sha=None, reason="test"
    )
    assert pushed.state == ws.PUSHED
    ws.maintain_hub(rig.hub)
    names = {p.name for p in rig.hub.iterdir()}
    assert not {n for n in names if n.endswith((".lock", ".json"))}
    assert not hasattr(ws, "_flock")


# ── resolve_start ──────────────────────────────────────────────────


def test_resolve_start_none_without_work(rig: _Rig) -> None:
    """A task with no pushed work starts at the base tip (None)."""
    assert ws.resolve_start(rig.hub, TD) is None


def test_resolve_start_takes_the_newest_attempt_by_job_id(rig: _Rig) -> None:
    """The next attempt continues from the newest one. Ordering by job id (not
    by string) matters: j10 is newer than j9."""
    _git(rig.hub, "update-ref", ws.build_ref(TD, 9), rig.main)
    tree = rig.cut(job=2)
    new = _commit(tree, "b.txt", msg="wip: snapshot killed (td5)")
    _git(
        tree,
        "push",
        "-q",
        "origin",
        f"{new}:{ws.build_ref(TD, 10)}",
        env={ws.ENV_TODO: "5", ws.ENV_JOB: "10"},
    )
    start = ws.resolve_start(rig.hub, TD)
    assert start == ws.Start(new, ws.build_ref(TD, 10), True)


def test_resolve_start_checks_the_recorded_commit(rig: _Rig) -> None:
    """A ref that no longer holds the commit the worker verified was moved by
    something else: refuse. A match returns it, and the recorded wip flag
    wins over the subject."""
    ref = ws.build_ref(TD, 9)
    _git(rig.hub, "update-ref", ref, rig.main)
    ok = ws.resolve_start(
        rig.hub, TD, recorded={"ref": ref, "sha": rig.main, "wip": True}
    )
    assert ok == ws.Start(rig.main, ref, True)
    with pytest.raises(ws.RefMoved, match="next:"):
        ws.resolve_start(rig.hub, TD, recorded={"ref": ref, "sha": "1" * 40})
    _git(rig.hub, "update-ref", "-d", ref)
    with pytest.raises(ws.RefMoved):
        ws.resolve_start(rig.hub, TD, recorded={"ref": ref, "sha": rig.main})


def test_resolve_start_ignores_attempts_below_the_floor(rig: _Rig) -> None:
    """After a land or a start-over the task's floor is a job id: an attempt
    ref of an earlier job is stale even when it was never deleted, and a
    record that names one is ignored rather than refused."""
    old, new = ws.build_ref(TD, 9), ws.build_ref(TD, 12)
    _git(rig.hub, "update-ref", old, rig.main)
    assert ws.resolve_start(rig.hub, TD, floor=10) is None
    assert (
        ws.resolve_start(rig.hub, TD, recorded={"ref": old, "sha": rig.main}, floor=10)
        is None
    )
    assert ws.resolve_start(rig.hub, TD, floor=9) == ws.Start(rig.main, old, False)
    _git(rig.hub, "update-ref", new, rig.main)
    assert ws.resolve_start(rig.hub, TD, floor=10) == ws.Start(rig.main, new, False)


def test_resolve_start_prefers_a_later_attempt_over_the_record(rig: _Rig) -> None:
    """Job 12 pushed but the worker never recorded it (a crash, a push it
    could not add to): the next attempt continues from job 12, not from the
    record of job 9, and the stale record is not checked."""
    old, new = ws.build_ref(TD, 9), ws.build_ref(TD, 12)
    _git(rig.hub, "update-ref", old, rig.main)
    tree = rig.cut(job=12)
    later = _commit(tree, "c.txt")
    _git(rig.hub, "update-ref", new, rig.main)
    _git(
        tree,
        "push",
        "-q",
        "origin",
        f"+{later}:{new}",
        env={ws.ENV_TODO: "td5", ws.ENV_JOB: "12"},
    )
    got = ws.resolve_start(rig.hub, TD, recorded={"ref": old, "sha": "1" * 40})
    assert got == ws.Start(later, new, False)


def test_resolve_start_unreadable_hub_defers(rig: _Rig) -> None:
    """An unreadable hub must raise HubUnreadable: starting from the base
    because pushed work could not be seen would discard it."""
    with pytest.raises(ws.HubUnreadable):
        ws.resolve_start(rig.tmp / "no-hub.git", TD)


# ── cut ────────────────────────────────────────────────────────────


def test_cut_is_a_fresh_clone_at_the_base_tip(rig: _Rig) -> None:
    """The attempt's folder must be its own repository on the named branch at
    the hub's base tip, with the factory's identity and no other config."""
    notes: list[str] = []
    tree = rig.tree()
    sha = ws.cut(rig.hub, tree, base="main", branch="slice/5", notes=notes)
    assert sha == rig.main and notes
    assert _g(tree, "rev-parse", "--abbrev-ref", "HEAD") == "slice/5"
    assert (
        _g(tree, "rev-parse", "HEAD")
        == _g(tree, "rev-parse", "origin/main")
        == rig.main
    )
    assert _g(tree, "remote", "get-url", "origin") == str(rig.hub)
    assert _g(tree, "config", "user.name") == _slice_park.IDENT_NAME
    assert _g(tree, "config", "user.email") == _slice_park.IDENT_EMAIL
    local = _g(tree, "config", "--local", "--list")
    assert not any(ln.startswith("branch.") for ln in local.splitlines())
    assert (tree / "first.txt").read_text(encoding="utf-8") == "first\n"
    assert not list(tree.parent.glob("*.cut-*"))


def test_cut_hard_links_objects_from_the_hub(rig: _Rig) -> None:
    """A clone must cost no copy of the history. If the objects are copied,
    every attempt pays the repository's size in disk and time."""
    tree = rig.cut()
    rel = Path("objects") / rig.main[:2] / rig.main[2:]
    assert os.stat(rig.hub / rel).st_ino == os.stat(tree / ".git" / rel).st_ino


def test_cut_refuses_an_existing_folder(rig: _Rig) -> None:
    """No job may enter a folder another job made. If cut reused one, a
    successor would inherit a predecessor's state and locks."""
    tree = rig.tree()
    tree.mkdir(parents=True)
    (tree / "mine.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ws.WorkspaceError, match="next:"):
        ws.cut(rig.hub, tree, base="main", branch="slice/5")
    assert (tree / "mine.txt").read_text(encoding="utf-8") == "keep"


def test_a_failed_cut_leaves_no_folder(rig: _Rig) -> None:
    """A cut that fails must leave nothing a sweep could take for an attempt
    (and nothing the next cut trips over)."""
    tree = rig.tree()
    with pytest.raises(ws.WorkspaceError, match="no refs/heads/nope"):
        ws.cut(rig.hub, tree, base="nope", branch="slice/5")
    assert not tree.exists()
    assert not list(tree.parent.glob("*.cut-*"))


def test_cut_continues_from_an_attempt_ref(rig: _Rig) -> None:
    """A retry must start from the earlier attempt's pushed commit, not the
    base: this is how work survives an attempt."""
    first = rig.cut(job=2)
    done = _commit(first, "work.txt", "work\n")
    pushed = _session_push(first, "HEAD:" + ws.build_ref(TD, 2), job=2)
    assert pushed.returncode == 0, pushed.stderr
    start = ws.resolve_start(rig.hub, TD)
    assert start == ws.Start(done, ws.build_ref(TD, 2), False)
    second = rig.tree(job=3)
    assert ws.cut(rig.hub, second, base="main", branch="slice/5", start=start) == done
    assert _g(second, "rev-parse", "HEAD") == done
    assert _g(second, "rev-parse", "--abbrev-ref", "HEAD") == "slice/5"
    assert (second / "work.txt").read_text(encoding="utf-8") == "work\n"


def test_cut_refuses_a_ref_that_holds_another_commit(rig: _Rig) -> None:
    """If the ref moved between resolve_start and cut, the attempt would build
    on a commit nobody verified: RefMoved, and no folder."""
    ref = ws.build_ref(TD, 2)
    _git(rig.hub, "update-ref", ref, rig.main)
    tree = rig.tree(job=3)
    with pytest.raises(ws.RefMoved):
        ws.cut(
            rig.hub, tree, base="main", branch="slice/5", start=ws.Start("2" * 40, ref)
        )
    assert not tree.exists() and not list(tree.parent.glob("*.cut-*"))


def test_cut_seeds_the_environment_from_the_named_checkout(
    rig: _Rig, tmp_path: Path
) -> None:
    """The clone's .venv comes from the named checkout's own seed script, and a
    missing source is a note, not an error."""
    src = tmp_path / "seedsrc"
    (src / ".venv").mkdir(parents=True)
    (src / "scripts").mkdir()
    script = src / "scripts" / "seed-worktree-venv"
    script.write_text('#!/bin/sh\necho "$1 $2" > "$1/seeded.txt"\n', encoding="utf-8")
    script.chmod(0o755)
    notes: list[str] = []
    tree = rig.tree()
    ws.cut(rig.hub, tree, base="main", branch="slice/5", seed_from=src, notes=notes)
    assert (tree / "seeded.txt").read_text(encoding="utf-8").split() == [
        str(tree),
        str(src),
    ]
    assert any("seeded" in n for n in notes)
    other = rig.tree(job=10)
    notes = []
    ws.cut(
        rig.hub,
        other,
        base="main",
        branch="slice/5",
        seed_from=tmp_path / "none",
        notes=notes,
    )
    assert any("no .venv" in n for n in notes)


def test_a_lock_in_one_clone_blocks_no_other_work(rig: _Rig) -> None:
    """The point of a clone per job: a git process killed in A leaves its
    index.lock in A's own .git. B must commit, and finalize on A must clear it
    and still push A's work."""
    a = rig.cut(job=1)
    b = rig.cut(job=2)
    work = _commit(a, "a.txt")
    (a / ".git" / "index.lock").write_text("", encoding="utf-8")
    assert (
        _git(a, "commit", "--allow-empty", "-q", "-m", "x", check=False).returncode != 0
    )
    _commit(b, "b.txt")
    got = ws.finalize(
        a, TD, 1, hub=rig.hub, base="main", start_sha=None, reason="killed"
    )
    assert got.state == ws.PUSHED and got.sha == work and got.by == "worker"
    assert not (a / ".git" / "index.lock").exists()


# ── the fence: a session's push through the hub's hook ─────────────


def test_session_may_push_its_own_ref_and_replace_it(rig: _Rig) -> None:
    """The builder pushes its attempt ref and re-pushes after an amend or
    rebase with a lease. If either is refused, the builder cannot save work."""
    tree = rig.cut()
    ref = ws.build_ref(TD, JOB)
    first = _commit(tree, "a.txt")
    assert _session_push(tree, f"HEAD:{ref}").returncode == 0
    assert rig.hub_ref(ref) == first
    _git(tree, "commit", "--amend", "-q", "-m", "amended")
    second = _g(tree, "rev-parse", "HEAD")
    assert second != first
    out = _session_push(
        tree, f"HEAD:{ref}", extra=(f"--force-with-lease={ref}:{first}",)
    )
    assert out.returncode == 0, out.stderr
    assert rig.hub_ref(ref) == second


@pytest.mark.parametrize(
    "target",
    [
        "refs/factory/build/6/9",  # another task
        "refs/factory/build/5/10",  # another job of the same task
        "refs/heads/main",
        "refs/heads/slice/5",
        "refs/tags/v1",
    ],
)
def test_session_refused_for_any_other_ref(rig: _Rig, target: str) -> None:
    """A session holds one ref. If it could write another task's attempt ref or
    a branch, a builder mistake would overwrite someone's work or the base."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    before = rig.hub_ref(target)
    out = _session_push(tree, f"HEAD:{target}")
    assert out.returncode != 0
    lines = _hook_lines(out.stderr)
    assert lines[0] == f"{ws.HOOK_REFUSED} (ref rule)"
    assert "refs/factory/build/5/9" in out.stderr
    assert rig.hub_ref(target) == before


def test_session_deletion_is_refused(rig: _Rig) -> None:
    """Deleting an attempt ref by push would destroy the task's work; only the
    worker deletes, with update-ref."""
    tree = rig.cut()
    ref = ws.build_ref(TD, JOB)
    _commit(tree, "a.txt")
    assert _session_push(tree, f"HEAD:{ref}").returncode == 0
    out = _session_push(tree, f":{ref}")
    assert out.returncode != 0
    assert _hook_lines(out.stderr)[0] == f"{ws.HOOK_REFUSED} (deletion)"
    assert rig.hub_ref(ref) is not None


def test_secret_in_a_commit_is_refused_then_accepted_after_an_amend(rig: _Rig) -> None:
    """A credential must not reach the hub. The refusal names the file, line
    and pattern but never the secret, tells the builder how to fix it, and the
    amended commit is then accepted: this is the builder's whole fix loop."""
    tree = rig.cut()
    ref = ws.build_ref(TD, JOB)
    _commit(tree, "settings.txt", f'token = "{FAKE_TOKEN}"\n', msg="add settings")
    out = _session_push(tree, f"HEAD:{ref}")
    assert out.returncode != 0
    lines = _hook_lines(out.stderr)
    assert lines[0] == f"{ws.HOOK_REFUSED} ({ws.HOOK_SECRET})"
    assert any(ln.strip().startswith("settings.txt:1: github token") for ln in lines)
    assert (
        "remove it from every commit you push (amend or rebase), then push again"
        in out.stderr
    )
    assert FAKE_TOKEN not in out.stderr and FAKE_TOKEN[:12] not in out.stderr
    assert rig.hub_ref(ref) is None
    (tree / "settings.txt").write_text(
        'token = "from the environment"\n', encoding="utf-8"
    )
    _git(tree, "commit", "-aq", "--amend", "-m", "add settings")
    again = _session_push(tree, f"HEAD:{ref}")
    assert again.returncode == 0, again.stderr
    assert rig.hub_ref(ref) == _g(tree, "rev-parse", "HEAD")


def test_a_secret_removed_by_a_later_commit_is_still_refused(rig: _Rig) -> None:
    """The scan covers every commit pushed, not the net diff: a credential
    committed and then deleted is still public history once pushed."""
    tree = rig.cut()
    _commit(tree, "s.txt", f"k = {FAKE_TOKEN}\n")
    _commit(tree, "s.txt", "k = none\n", msg="remove it")
    out = _session_push(tree, "HEAD:" + ws.build_ref(TD, JOB))
    assert out.returncode != 0 and ws.HOOK_SECRET in out.stderr


WORKER = {ws.ENV_WORKER: "1"}


def test_a_push_without_an_identity_is_refused(rig: _Rig) -> None:
    """A builder shell that lost the session's environment must not write the
    hub (and must be told the worker saves its work): neither identity, no
    push."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    out = _git(tree, "push", "origin", "HEAD:" + ws.build_ref(TD, JOB), check=False)
    assert out.returncode != 0
    lines = _hook_lines(out.stderr)
    assert lines[0] == f"{ws.HOOK_REFUSED} (no identity)"
    assert "worker saves" in out.stderr and ws.ENV_TODO in out.stderr
    assert rig.hub_ref(ws.build_ref(TD, JOB)) is None


def test_the_worker_may_push_attempt_refs_and_nothing_else(rig: _Rig) -> None:
    """The worker marks its pushes with PRECIS_HUB_WORKER and may write attempt
    refs only: a branch, a tag or a deletion is refused (the base arrives by
    a hub-side fetch, never a push)."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    ok = _git(
        tree, "push", "origin", "HEAD:" + ws.build_ref(7, 1), env=WORKER, check=False
    )
    assert ok.returncode == 0, ok.stderr
    for target in ("refs/heads/other", "refs/tags/v1", "refs/heads/main"):
        bad = _git(tree, "push", "origin", f"HEAD:{target}", env=WORKER, check=False)
        assert bad.returncode != 0
        assert _hook_lines(bad.stderr)[0] == f"{ws.HOOK_REFUSED} (ref rule)"
    gone = _git(
        tree, "push", "origin", ":" + ws.build_ref(7, 1), env=WORKER, check=False
    )
    assert gone.returncode != 0 and "(deletion)" in gone.stderr
    assert rig.hub_ref("refs/heads/other") is None


def test_every_accepted_push_is_scanned_including_the_workers(rig: _Rig) -> None:
    """There is no unscanned way in: the worker's push to an attempt ref goes
    through the same secret scan as a session's."""
    tree = rig.cut()
    _commit(tree, "s.txt", f"k = {FAKE_TOKEN}\n")
    out = _git(
        tree, "push", "origin", "HEAD:" + ws.build_ref(7, 1), env=WORKER, check=False
    )
    assert out.returncode != 0
    assert _hook_lines(out.stderr)[0] == f"{ws.HOOK_REFUSED} ({ws.HOOK_SECRET})"
    assert FAKE_TOKEN not in out.stderr


def test_a_command_line_hooks_path_does_not_bypass_the_hook(rig: _Rig) -> None:
    """git drops command-line config for a local transport, so a pusher's
    ``-c core.hooksPath=/dev/null`` still meets the hub's hook. (The engine's
    own trusted git calls pass that option; the hub is protected regardless.)"""
    tree = rig.cut()
    _commit(tree, "a.txt")
    out = _git(
        tree,
        "-c",
        "core.hooksPath=/dev/null",
        "push",
        "origin",
        "HEAD:refs/heads/x",
        check=False,
    )
    assert out.returncode != 0 and ws.HOOK_REFUSED in out.stderr
    assert rig.hub_ref("refs/heads/x") is None


def test_a_global_hooks_path_does_not_bypass_the_hook(
    rig: _Rig, tmp_path: Path
) -> None:
    """core.hooksPath in the hub's own config wins over a global setting, so a
    pusher's ~/.gitconfig cannot turn the fence off."""
    (tmp_path / "home" / ".gitconfig").write_text(
        "[core]\n\thooksPath = /nonexistent\n", encoding="utf-8"
    )
    tree = rig.cut()
    _commit(tree, "a.txt")
    out = _git(tree, "push", "origin", "HEAD:refs/heads/x", check=False)
    assert out.returncode != 0 and ws.HOOK_REFUSED in out.stderr


# ── finalize ───────────────────────────────────────────────────────


def _parents(rig: _Rig, ref: str) -> list[str]:
    """The parents of the commit ``ref`` holds in the hub, first parent first."""
    return _g(rig.hub, "rev-list", "--parents", "-n", "1", ref).split()[1:]


def _final(rig: _Rig, tree: Path | None = None, **kw: Any) -> ws.Pushed:
    args: dict[str, Any] = {
        "hub": rig.hub,
        "base": "main",
        "start_sha": None,
        "reason": "session exited",
    }
    args.update(kw)
    return ws.finalize(tree or rig.tree(), TD, JOB, **args)


def test_finalize_builder_already_pushed(rig: _Rig) -> None:
    """When the builder pushed its head the worker must not push again, and
    must say so: the record shows who saved the work."""
    tree = rig.cut()
    head = _commit(tree, "a.txt")
    assert _session_push(tree, "HEAD:" + ws.build_ref(TD, JOB)).returncode == 0
    got = _final(rig)
    assert (got.state, got.by, got.sha, got.head, got.wip) == (
        ws.PUSHED,
        "builder",
        head,
        head,
        False,
    )
    assert got.safe and got.ref == ws.build_ref(TD, JOB)


def test_finalize_pushes_clean_unpushed_commits(rig: _Rig) -> None:
    """A builder that exited without pushing must not strand its commits in the
    folder."""
    tree = rig.cut()
    head = _commit(tree, "a.txt")
    got = _final(rig)
    assert (got.state, got.by, got.sha, got.wip) == (ws.PUSHED, "worker", head, False)
    assert rig.hub_ref(ws.build_ref(TD, JOB)) == head


def test_finalize_snapshots_a_dirty_tree_without_touching_the_folder(rig: _Rig) -> None:
    """Uncommitted work is pushed as a ``wip: snapshot`` commit built outside
    the folder's own index, so the folder (kept for a look) is exactly as the
    builder left it."""
    tree = rig.cut()
    head = _commit(tree, "a.txt")
    (tree / "a.txt").write_text("edited\n", encoding="utf-8")
    (tree / "new.txt").write_text("new\n", encoding="utf-8")
    got = _final(rig, reason="wall timeout")
    assert got.state == ws.PUSHED and got.wip and got.head == head and got.sha != head
    ref = ws.build_ref(TD, JOB)
    assert (
        _g(rig.hub, "log", "-1", "--format=%s", ref)
        == "wip: snapshot wall timeout (td5)"
    )
    assert _g(rig.hub, "show", f"{ref}:a.txt") == "edited"
    assert _g(rig.hub, "show", f"{ref}:new.txt") == "new"
    assert _g(tree, "rev-parse", "HEAD") == head
    assert _g(tree, "diff", "--cached", "--name-only") == ""
    assert "?? new.txt" in _g(tree, "status", "--porcelain")
    assert (tree / "a.txt").read_text(encoding="utf-8") == "edited\n"


@pytest.mark.parametrize("layout", ["on the line", "beside a branch", "off the line"])
def test_a_repeat_finalize_of_an_unchanged_dirty_folder_gives_the_same_commit(
    rig: _Rig, layout: str
) -> None:
    """A finalize leaves the folder's index alone, so a second one sees the
    same uncommitted work. It must give the commit the first one gave and
    push nothing: its own snapshot differs only in date and subject, and
    stacking it made every later check of such a folder look like newer work
    (one more commit on the hub ref per sweep for a folder that cannot be
    removed)."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    kw: dict[str, Any] = {}
    if layout != "on the line":
        _git(tree, "checkout", "-q", "-b", "side")
        _commit(tree, "side.txt", "side\n")
        kw = {"branch": "slice/5"}
    if layout == "beside a branch":
        _git(tree, "checkout", "-q", "slice/5")
    (tree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    first = _final(rig, reason="session exited", **kw)
    assert first.state == ws.PUSHED and first.wip and first.sha == rig.hub_ref(REF)
    held = _g(rig.hub, "rev-list", "--count", REF)
    for reason in ("run ended", "relaunch"):
        again = _final(rig, reason=reason, **kw)
        assert (again.state, again.sha, again.head) == (
            ws.PUSHED,
            first.sha,
            first.head,
        )
        assert rig.hub_ref(REF) == first.sha
        assert _g(rig.hub, "rev-list", "--count", REF) == held
    # work added after the first finalize is still new work, on top of it
    (tree / "dirty.txt").write_text("more\n", encoding="utf-8")
    later = _final(rig, reason="run ended", **kw)
    assert later.state == ws.PUSHED and later.sha not in (None, first.sha)
    assert (
        _git(
            rig.hub, "merge-base", "--is-ancestor", str(first.sha), str(later.sha)
        ).returncode
        == 0
    )
    holds_it = [
        sha
        for sha in _g(rig.hub, "rev-list", REF).split()
        if _git(rig.hub, "show", f"{sha}:dirty.txt", check=False).stdout == "more\n"
    ]
    assert holds_it


def test_finalize_nothing_new_is_empty(rig: _Rig) -> None:
    """A build that changed nothing is EMPTY (safe to delete), both against the
    base and against a start commit it continued from."""
    tree = rig.cut(job=1)
    assert (
        ws.finalize(
            tree, TD, 1, hub=rig.hub, base="main", start_sha=None, reason="x"
        ).state
        == ws.EMPTY
    )
    assert (
        ws.finalize(
            tree, TD, 1, hub=rig.hub, base="main", start_sha=rig.main, reason="x"
        ).state
        == ws.EMPTY
    )
    got = ws.finalize(tree, TD, 1, hub=rig.hub, base="main", start_sha=None, reason="x")
    assert got.safe and got.sha is None and rig.hub_ref(ws.build_ref(TD, 1)) is None
    # Ahead of the base but equal to its start: still nothing new.
    head = _commit(tree, "a.txt")
    assert (
        ws.finalize(
            tree, TD, 1, hub=rig.hub, base="main", start_sha=head, reason="x"
        ).state
        == ws.EMPTY
    )
    assert (
        ws.finalize(
            tree, TD, 1, hub=rig.hub, base="main", start_sha=None, reason="x"
        ).state
        == ws.PUSHED
    )


def test_finalize_of_a_clone_reset_to_the_base_is_empty(rig: _Rig) -> None:
    """A session that started from an earlier attempt's work and reset its
    clone to the base has nothing of its own. Pushing the base tip as this
    attempt would record it as the task's newest work and bury the attempt
    it started from, so nothing is pushed and the earlier ref stays the
    newest."""
    first = rig.cut(job=1)
    work = _commit(first, "a.txt")
    assert ws.finalize(
        first, TD, 1, hub=rig.hub, base="main", start_sha=rig.main, reason="x"
    ).safe
    start = ws.resolve_start(rig.hub, TD)
    assert start is not None and start.sha == work
    tree = rig.cut(job=2, start=start)
    _git(tree, "reset", "-q", "--hard", "origin/main")
    got = ws.finalize(
        tree,
        TD,
        2,
        hub=rig.hub,
        base="main",
        start_sha=work,
        reason="x",
        branch=f"slice/{TD}",
    )
    assert got.state == ws.EMPTY and got.sha is None
    assert rig.hub_ref(ws.build_ref(TD, 2)) is None
    again = ws.resolve_start(rig.hub, TD)
    assert again is not None and again.sha == work


def test_resolve_start_reads_a_builders_own_snapshot_as_unfinished(rig: _Rig) -> None:
    """A builder that could not finish pushes its own ``wip: snapshot``
    commit; the worker records it with wip false (it made no snapshot). The
    next start must still read as unfinished, or it would go to the land."""
    tree = rig.cut(job=3)
    sha = _commit(tree, "half.txt", msg="wip: snapshot — tests still red")
    ref = ws.build_ref(TD, 3)
    _git(
        tree,
        "push",
        "-q",
        "origin",
        f"+{sha}:{ref}",
        env={ws.ENV_TODO: "td5", ws.ENV_JOB: "3"},
    )
    got = ws.resolve_start(rig.hub, TD, recorded={"ref": ref, "sha": sha, "wip": False})
    assert got == ws.Start(sha, ref, True)


def test_finalize_aborts_a_rebase_killed_with_conflicts(rig: _Rig) -> None:
    """A session killed mid-rebase leaves HEAD detached on a half-built stack
    and a stale lock. The line's tip is what the builder committed; the
    half-rebased state (conflict markers and all) is captured first and kept as
    a parent, so nothing the abort discards is lost."""
    tree = rig.cut()
    mine = _commit(tree, "first.txt", "builder\n", msg="builder change")
    rig.advance_github("first.txt", "upstream\n")
    assert ws.sync_hub(rig.root, url=str(rig.github), base="main")
    _git(tree, "fetch", "-q", "origin")
    assert _git(tree, "rebase", "origin/main", check=False).returncode != 0
    gitdir = tree / ".git"
    assert (gitdir / "rebase-merge").exists()
    (gitdir / "index.lock").write_text("", encoding="utf-8")
    got = _final(rig, branch="slice/5")
    ref = ws.build_ref(TD, JOB)
    assert got.state == ws.PUSHED and got.head == mine
    assert _parents(rig, ref)[0] == mine and "in-flight rebase" in got.note
    assert _g(rig.hub, "rev-parse", f"{ref}^{{tree}}") == _g(
        rig.hub, "rev-parse", f"{mine}^{{tree}}"
    )
    assert not (gitdir / "rebase-merge").exists()
    assert _g(tree, "status", "--porcelain") == ""
    assert _g(tree, "rev-parse", "--abbrev-ref", "HEAD") == "slice/5"


def test_finalize_withholds_a_credential(rig: _Rig) -> None:
    """Work holding a credential must not enter the hub, and the folder, which
    now holds the only copy, must stay. The note shows where, not what."""
    tree = rig.cut()
    head = _commit(tree, "settings.txt", f'token = "{FAKE_TOKEN}"\n')
    got = _final(rig)
    assert got.state == ws.WITHHELD and not got.safe and got.sha is None
    assert got.head == head
    assert "settings.txt:1: github token" in got.note
    assert FAKE_TOKEN not in got.note
    assert rig.hub_ref(ws.build_ref(TD, JOB)) is None
    assert _g(tree, "rev-parse", "HEAD") == head
    assert (tree / "settings.txt").exists()


def test_finalize_withholds_a_credential_in_uncommitted_work(rig: _Rig) -> None:
    """The snapshot of a dirty tree is scanned like any commit."""
    tree = rig.cut()
    (tree / "notes.txt").write_text(f"{FAKE_TOKEN}\n", encoding="utf-8")
    got = _final(rig)
    assert got.state == ws.WITHHELD and got.wip
    assert rig.hub_ref(ws.build_ref(TD, JOB)) is None
    assert (tree / "notes.txt").exists()


def test_finalize_after_an_amend_keeps_the_commit_the_ref_held(rig: _Rig) -> None:
    """The ref's old tip is not an ancestor of an amended head. It is not
    dropped from the ref: the leased push lands a commit that has the amended
    head as first parent and the old tip as a further parent."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    first = _final(rig)
    assert first.state == ws.PUSHED
    _git(tree, "commit", "--amend", "-q", "-m", "amended")
    head = _g(tree, "rev-parse", "HEAD")
    got = _final(rig)
    assert got.state == ws.PUSHED and got.by == "worker" and got.head == head
    assert _parents(rig, REF) == [head, first.sha]


def test_finalize_missing_hub_fails_and_never_raises(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finalize runs in the worker at the end of a job: whatever is wrong it
    must answer with a record, never an exception."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    got = _final(rig, hub=rig.tmp / "no-hub.git")
    assert got.state == ws.FAILED and not got.safe and "next:" in got.note
    gone = ws.finalize(
        rig.tmp / "no-folder",
        TD,
        JOB,
        hub=rig.hub,
        base="main",
        start_sha=None,
        reason="x",
    )
    assert gone.state == ws.FAILED
    odd = rig.tmp / "not-a-repo"
    odd.mkdir()
    assert (
        ws.finalize(
            odd, TD, JOB, hub=rig.hub, base="main", start_sha=None, reason="x"
        ).state
        == ws.FAILED
    )

    def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("git exploded")

    monkeypatch.setattr(_slice_park, "_snapshot_commit", boom)
    err = _final(rig)
    assert err.state == ws.FAILED and "git exploded" in err.note


def test_push_commit_puts_a_worker_made_commit_in_the_hub(rig: _Rig) -> None:
    """A mechanical job (pin_bump) commits in a tree of its own, a linked
    worktree of the host checkout, and hands the commit to the lander: the
    commit must reach the hub as that job's attempt ref, through the hook."""
    tree = rig.tmp / "pin-tree"
    _git(rig.root, "worktree", "add", "-q", "-b", "slice/5", str(tree), "main")
    sha = _commit(tree, "pin.txt", "v2\n")
    got = ws.push_commit(tree, TD, JOB, sha, hub=rig.hub)
    assert got.state == ws.PUSHED and got.safe and got.by == "worker"
    assert got.sha == sha and got.head == sha and not got.wip
    assert got.ref == ws.build_ref(TD, JOB)
    assert rig.hub_ref(got.ref) == sha
    again = ws.push_commit(tree, TD, JOB, sha, hub=rig.hub)
    assert again.state == ws.PUSHED and again.note == "the hub already held it"
    # a credential is refused like any push, and nothing raises
    bad = _commit(tree, "settings.txt", f'token = "{FAKE_TOKEN}"\n')
    held = ws.push_commit(tree, TD, JOB, bad, hub=rig.hub)
    assert held.state == ws.WITHHELD and rig.hub_ref(got.ref) == sha
    assert ws.push_commit(tree, TD, JOB, "f" * 40, hub=rig.hub).state == ws.FAILED
    assert ws.push_commit(tree, TD, JOB, "nonsense", hub=rig.hub).state == ws.FAILED
    assert (
        ws.push_commit(tree, TD, JOB, sha, hub=rig.tmp / "no-hub.git").state
        == ws.FAILED
    )


def test_pushed_record(rig: _Rig) -> None:
    """The record the worker stores on the task and the job carries every key
    the next launch reads."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    meta = _final(rig).as_meta()
    assert set(meta) == {
        "state",
        "ref",
        "sha",
        "head",
        "wip",
        "by",
        "note",
        "host",
        "at",
    }
    assert meta["state"] == ws.PUSHED and meta["ref"] == ws.build_ref(TD, JOB)


# ── drop and sweep ─────────────────────────────────────────────────


@pytest.fixture
def evicted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    calls: list[tuple[str, bool]] = []

    def fake(project: str, *, volumes: bool) -> bool:
        calls.append((project, volumes))
        return True

    monkeypatch.setattr(slice_stacks, "evict_project", fake)
    return calls


def test_drop_removes_the_folder_and_brings_its_stack_down(
    rig: _Rig, evicted: list[tuple[str, bool]]
) -> None:
    """A folder's Compose project holds volumes and a network slot; deleting
    the directory alone leaks them. The project is named after the folder."""
    tree = rig.cut()
    assert ws.drop(tree) is True
    assert not tree.exists()
    assert evicted == [("precis-test-slice-td5-j9", True)]
    assert ws.drop(tree) is True  # already gone


def test_drop_reports_a_folder_it_could_not_take_away(
    rig: _Rig, evicted: list[tuple[str, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """drop answers False, with a note, when the attempt folder is still there
    under its own name; the caller then reports it kept."""
    tree = rig.cut()

    def busy(src: Any, dst: Any) -> None:
        raise OSError("busy")

    monkeypatch.setattr(ws.os, "rename", busy)
    notes: list[str] = []
    assert ws.drop(tree, notes=notes) is False
    assert tree.is_dir() and any("busy" in n for n in notes)


def _delete_half_then_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``shutil.rmtree`` remove one tracked file and then fail, as it does
    on a file another account owns."""

    def half(path: Any, **kw: Any) -> None:
        (Path(path) / "first.txt").unlink()
        raise OSError("permission denied")

    monkeypatch.setattr(ws.shutil, "rmtree", half)


def test_a_delete_that_stops_half_way_is_never_saved_as_a_snapshot(
    rig: _Rig, evicted: list[tuple[str, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finalize records missing files as deletions. If a half-deleted folder
    kept its attempt name, the next sweep would push a snapshot in which the
    builder 'deleted' those files, on top of the good work, and the task's
    next attempt would start from it. A folder being deleted therefore stops
    being an attempt folder first."""
    tree = rig.cut()
    work = _commit(tree, "work.txt", "work\n")
    pushed = ws.finalize(
        tree, TD, JOB, hub=rig.hub, base="main", start_sha=None, reason="test"
    )
    assert pushed.state == ws.PUSHED and pushed.sha == work
    with monkeypatch.context() as patch:
        _delete_half_then_fail(patch)
        notes: list[str] = []
        assert ws.drop(tree, notes=notes) is True
    assert not tree.exists()
    [left] = [p for p in tree.parent.iterdir() if ".drop-" in p.name]
    assert not (left / "first.txt").exists() and (left / "work.txt").exists()
    assert ws.parse_attempt_dir(left) is None
    judged: list[str] = []

    def settle(folder: Path, todo: int, job: int, role: str) -> ws.Pushed | None:
        judged.append(folder.name)
        return ws.finalize(
            folder, todo, job, hub=rig.hub, base="main", start_sha=None, reason="x"
        )

    monkeypatch.setattr(ws, "maintain_hub", lambda hub: None)
    assert ws.sweep(rig.root, settle) == []  # young: maybe still being deleted
    assert judged == [] and rig.hub_ref(ws.build_ref(TD, JOB)) == work
    _age(left, 2 * ws.CUT_TMP_MAX_AGE_S)
    notes = ws.sweep(rig.root, settle)
    assert judged == [] and not left.exists()
    assert notes == [f"removed the leftover of a dropped folder: {left.name}"]
    assert rig.hub_ref(ws.build_ref(TD, JOB)) == work


def test_a_leftover_that_cannot_be_removed_is_reported_in_the_same_words_each_sweep(
    rig: _Rig, evicted: list[tuple[str, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the sweep cannot delete either needs a person. The note is the
    alert's text, and the alert is raised once only while the text stays the
    same, so it must not carry the error of the day."""
    tree = rig.cut()
    _delete_half_then_fail(monkeypatch)
    assert ws.drop(tree) is True
    [left] = [p for p in tree.parent.iterdir() if ".drop-" in p.name]
    _age(left, 2 * ws.CUT_TMP_MAX_AGE_S)
    monkeypatch.setattr(ws, "maintain_hub", lambda hub: None)

    def fail(path: Any, **kw: Any) -> None:
        raise OSError(f"permission denied at {time.time()}")

    monkeypatch.setattr(ws.shutil, "rmtree", fail)
    first = ws.sweep(rig.root, lambda *a: None)
    second = ws.sweep(rig.root, lambda *a: None)
    assert first == second and len(first) == 1
    assert first[0].startswith(f"kept {left.name}: ") and "by hand" in first[0]
    assert left.exists()


def test_a_sweep_stops_starting_folders_when_its_time_is_spent(
    rig: _Rig, evicted: list[tuple[str, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep runs under the lock that also guards every running job's
    poll on the host. One that saved a long row of folders in one go would
    hold up deadline kills and, past the lock's own limit, let a second
    worker sweep beside it. It pauses, and later sweeps reach the rest."""
    trees = [rig.cut(job=j) for j in (1, 2, 3)]
    maintained: list[Path] = []
    monkeypatch.setattr(ws, "maintain_hub", lambda hub: maintained.append(hub))

    def slow(tree: Path, todo: int, job: int, role: str) -> ws.Pushed | None:
        time.sleep(0.05)
        return ws.Pushed(ws.PUSHED, ws.build_ref(todo, job), sha="a" * 40)

    notes = ws.sweep(rig.root, slow, budget_s=0.01)
    assert [t.exists() for t in trees] == [False, True, True]
    assert any("paused" in n and "2 of 3" in n for n in notes)
    assert maintained == []  # the hub's tidy-up waits for a sweep that got through
    ws.sweep(rig.root, slow, budget_s=0.01)
    assert [t.exists() for t in trees] == [False, False, True]
    notes = ws.sweep(rig.root, slow, budget_s=0.01)
    assert not any(t.exists() for t in trees)
    assert not any("paused" in n for n in notes) and maintained == [rig.hub]


def test_sweep_drops_safe_folders_and_keeps_the_rest(
    rig: _Rig, evicted: list[tuple[str, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The janitor deletes only what settle proves is in the hub. Everything
    else stays with a note beginning 'kept' (the caller's alert), a running
    job's folder and the ``keep`` folder are not touched, and a settle that
    raises costs one folder, not the sweep."""
    safe = rig.cut(job=1)
    held = rig.cut(job=2)
    running = rig.cut(job=3)
    keeper = rig.cut(job=4)
    boom = rig.cut(job=5)
    land = ws.attempt_dir(rig.root, TD, 6, ws.ROLE_LAND)
    land.mkdir()
    old_tmp = ws.attempt_dir(rig.root, TD, 7).with_name("slice-td5-j7.cut-4242")
    old_tmp.mkdir()
    long_ago = time.time() - 2 * 3600
    os.utime(old_tmp, (long_ago, long_ago))
    new_tmp = old_tmp.with_name("slice-td5-j8.cut-4243")
    new_tmp.mkdir()
    (rig.root / ".claude" / "worktrees" / "unrelated").mkdir()
    seen: list[tuple[int, int, str]] = []

    def settle(tree: Path, todo: int, job: int, role: str) -> ws.Pushed | None:
        seen.append((todo, job, role))
        if job == 5:
            raise RuntimeError("settle exploded")
        if job == 1:
            return ws.Pushed(ws.PUSHED, ws.build_ref(todo, job), sha="a" * 40)
        if job == 2:
            return ws.Pushed(ws.WITHHELD, ws.build_ref(todo, job), note="secret")
        if job == 6:
            return ws.Pushed(ws.EMPTY, ws.build_ref(todo, job))
        return None

    maintained: list[Path] = []

    def fake_maintain(hub: Path) -> str | None:
        maintained.append(hub)
        return None

    monkeypatch.setattr(ws, "maintain_hub", fake_maintain)
    notes = ws.sweep(rig.root, settle, keep=keeper)
    assert not safe.exists() and not land.exists() and not old_tmp.exists()
    assert held.exists() and running.exists() and keeper.exists() and boom.exists()
    assert new_tmp.exists()
    assert (4, 4) not in {(t, j) for t, j, _ in seen} and (
        TD,
        4,
        ws.ROLE_BUILD,
    ) not in seen
    assert (TD, 6, ws.ROLE_LAND) in seen
    kept = [n for n in notes if n.startswith("kept")]
    assert any("slice-td5-j2" in n and "secret" in n for n in kept)
    assert any("slice-td5-j5" in n for n in kept)
    assert len(kept) == 2
    assert maintained == [rig.hub]
    assert sorted(p for p, _ in evicted) == [
        "precis-test-slice-td5-j1",
        "precis-test-slice-td5-land-j6",
    ]


def test_sweep_of_a_host_with_no_folders_does_not_raise(tmp_path: Path) -> None:
    """The janitor runs on every tick, including on a host that has never cut
    anything."""
    assert ws.sweep(tmp_path / "no-root", lambda *a: None) == []


def test_sweep_settles_and_drops_one_folder_after_another_and_makes_no_lock_file(
    rig: _Rig, evicted: list[tuple[str, bool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is one writer of a folder at a time (the executor's watch pass),
    so the sweep takes no lock: it judges and drops each folder in turn, and
    nothing is ever created beside a folder."""
    first = rig.cut(job=1)
    second = rig.cut(job=2)
    monkeypatch.setattr(ws, "maintain_hub", lambda hub: None)
    order: list[tuple[str, int]] = []

    def settle(tree: Path, todo: int, job: int, role: str) -> ws.Pushed | None:
        order.append(("settle", job))
        return ws.Pushed(ws.PUSHED, ws.build_ref(todo, job), sha="a" * 40)

    real_drop = ws.drop

    def recording_drop(tree: Path, *, notes: list[str] | None = None) -> bool:
        order.append(("drop", int(tree.name.rsplit("j", 1)[1])))
        return real_drop(tree, notes=notes)

    monkeypatch.setattr(ws, "drop", recording_drop)
    notes = ws.sweep(rig.root, settle)
    assert order == [("settle", 1), ("drop", 1), ("settle", 2), ("drop", 2)]
    assert not first.exists() and not second.exists()
    assert [n for n in notes if n.startswith("dropped")] != []
    assert not hasattr(ws, "folder_lock") and not hasattr(ws, "lock_path")
    assert [p for p in first.parent.iterdir() if p.name.endswith(".lock")] == []


@pytest.mark.parametrize("ceiling", [True, False])
def test_finalize_of_a_folder_without_its_own_git_leaves_the_host_alone(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, ceiling: bool
) -> None:
    """A folder that lost its .git sits inside the host checkout, so git
    there answers for the host's repository (gr742505). Finalize must fail
    and touch nothing of the host: not its locks, not its branch. Two fences:
    the factory's git stops at the folder's parent, and finalize refuses a
    git dir that is not the folder's own should git walk up all the same."""
    tree = rig.cut()
    (tree / "work.txt").write_text("work\n", encoding="utf-8")
    shutil.rmtree(tree / ".git")
    if not ceiling:
        rs = ws._rs()

        def walking_up(args: list[str], cwd: Path) -> Any:
            return subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                env=rs._git_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=60,
            )

        monkeypatch.setattr(rs, "_git", walking_up)
    host_lock = rig.root / ".git" / "index.lock"
    host_lock.write_text("", encoding="utf-8")
    head = _g(rig.root, "rev-parse", "HEAD")
    try:
        got = _final(rig, tree)
        assert got.state == ws.FAILED
        assert (
            "is not a git folder" if ceiling else "no repository of its own"
        ) in got.note
        assert host_lock.exists()
    finally:
        host_lock.unlink(missing_ok=True)
    assert _g(rig.root, "rev-parse", "HEAD") == head
    assert _g(rig.root, "status", "--porcelain", "--untracked-files=no") == ""
    assert rig.hub_ref(REF) is None


# ── finalize keeps every tip only the folder holds ─────────────────

REF = ws.build_ref(TD, JOB)


def test_finalize_keeps_commits_on_another_local_branch(rig: _Rig) -> None:
    """Work on a side branch is reachable from nothing the hub would get, and
    the folder is then dropped. It must ride as a parent of the commit pushed,
    with the line's own tip as first parent and the line's tree."""
    tree = rig.cut()
    c1 = _commit(tree, "a.txt")
    _git(tree, "checkout", "-q", "-b", "side")
    s1 = _commit(tree, "side.txt", "side\n")
    _git(tree, "checkout", "-q", "slice/5")
    got = _final(rig, branch="slice/5")
    assert got.state == ws.PUSHED and got.by == "worker" and got.wip
    assert got.head == c1 and got.sha == rig.hub_ref(REF) != c1
    assert _parents(rig, REF) == [c1, s1]
    assert _g(rig.hub, "rev-parse", f"{REF}^{{tree}}") == _g(
        rig.hub, "rev-parse", f"{c1}^{{tree}}"
    )
    assert "kept 1 other tip(s): branch side" in got.note
    assert (
        _g(rig.hub, "log", "-1", "--format=%s", REF)
        == "wip: snapshot session exited (td5), keeps 1 other tip(s)"
    )
    assert _g(tree, "rev-parse", "slice/5") == c1  # the folder's branches are untouched


@pytest.mark.parametrize("entries", [1, 2])
def test_finalize_keeps_every_stash_entry(rig: _Rig, entries: int) -> None:
    """A stash is invisible to HEAD and to a branch listing; every entry (not
    only the newest) must reach the hub."""
    tree = rig.cut()
    c1 = _commit(tree, "a.txt")
    for i in range(entries):
        (tree / "a.txt").write_text(f"stashed {i}\n", encoding="utf-8")
        _git(tree, "stash")
    assert len(_g(tree, "stash", "list").splitlines()) == entries
    got = _final(rig, branch="slice/5")
    assert got.state == ws.PUSHED and got.head == c1
    parents = _parents(rig, REF)
    assert parents[0] == c1 and len(parents) == 1 + entries
    held = {_g(rig.hub, "show", f"{p}:a.txt") for p in parents[1:]}
    assert held == {f"stashed {i}" for i in range(entries)}
    assert ("stash" if entries == 1 else f"stash x{entries}") in got.note


def test_finalize_never_moves_the_hub_ref_back(rig: _Rig) -> None:
    """The builder pushed its tip and then left HEAD detached on an older
    commit. Naming the line keeps the hub ref where it is; letting the line be
    the detached HEAD must still never drop the commits the ref held: the
    new tip descends from them."""
    tree = rig.cut()
    c1 = _commit(tree, "a.txt")
    c2 = _commit(tree, "b.txt")
    assert _session_push(tree, f"HEAD:{REF}").returncode == 0
    _git(tree, "checkout", "-q", "--detach", "HEAD~1")
    named = _final(rig, branch="slice/5")
    assert (named.state, named.by, named.sha, named.head) == (
        ws.PUSHED,
        "builder",
        c2,
        c2,
    )
    assert rig.hub_ref(REF) == c2
    unnamed = _final(rig)
    assert unnamed.state == ws.PUSHED and unnamed.head == c1
    tip = rig.hub_ref(REF)
    assert tip is not None and tip != c2
    assert _git(rig.hub, "merge-base", "--is-ancestor", c2, tip).returncode == 0
    assert _parents(rig, REF)[0] == c1


def test_finalize_ends_a_bisect_and_pushes_the_line(rig: _Rig) -> None:
    """A bisect leaves HEAD detached mid-history and state files; finalize ends
    it (so the folder is at rest) and the line's tip is what is pushed."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    _commit(tree, "b.txt")
    c3 = _commit(tree, "c.txt")
    _git(tree, "bisect", "start")
    _git(tree, "bisect", "bad", "HEAD")
    _git(tree, "bisect", "good", "HEAD~2")
    assert (tree / ".git" / "BISECT_START").exists()
    got = _final(rig, branch="slice/5")
    assert got.state == ws.PUSHED and got.head == c3 and got.sha == c3
    assert not (tree / ".git" / "BISECT_START").exists()
    assert _g(tree, "symbolic-ref", "HEAD") == "refs/heads/slice/5"


def test_finalize_keeps_the_state_of_an_interactive_rebase_stopped_at_edit(
    rig: _Rig,
) -> None:
    """``rebase --abort`` throws away a commit made at a stop. It is captured
    first and kept as a parent; the line's tip is the pre-rebase one."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    c2 = _commit(tree, "b.txt")
    stop = _git(
        tree,
        "rebase",
        "-i",
        "origin/main",
        env={"GIT_SEQUENCE_EDITOR": "sed -i.bak '1s/^pick/edit/'"},
        check=False,
    )
    assert (tree / ".git" / "rebase-merge").exists(), stop.stderr
    made = _commit(tree, "stop.txt", "made at the stop\n")
    got = _final(rig, branch="slice/5")
    assert got.state == ws.PUSHED and got.head == c2
    assert _parents(rig, REF) == [c2, made]
    assert "in-flight rebase" in got.note
    assert _g(rig.hub, "show", f"{made}:stop.txt") == "made at the stop"
    assert not (tree / ".git" / "rebase-merge").exists()
    assert _g(tree, "for-each-ref", "refs/factory-keep")  # the folder still names it


def test_finalize_keeps_a_conflicted_rebase_with_one_conflict_resolved(
    rig: _Rig,
) -> None:
    """A resolved-and-staged conflict exists only in the working tree and the
    index; the abort would discard it. The captured state holds both files:
    the resolution and the file still in conflict."""
    rig.advance_github("f1.txt", "base1\n")
    rig.advance_github("f2.txt", "base2\n")
    assert ws.sync_hub(rig.root, url=str(rig.github), base="main")
    tree = rig.cut()
    (tree / "f1.txt").write_text("mine1\n", encoding="utf-8")
    (tree / "f2.txt").write_text("mine2\n", encoding="utf-8")
    _git(tree, "commit", "-qam", "mine")
    mine = _g(tree, "rev-parse", "HEAD")
    rig.advance_github("f1.txt", "up1\n")
    rig.advance_github("f2.txt", "up2\n")
    assert ws.sync_hub(rig.root, url=str(rig.github), base="main")
    _git(tree, "fetch", "-q", "origin")
    assert _git(tree, "rebase", "origin/main", check=False).returncode != 0
    (tree / "f1.txt").write_text("resolved1\n", encoding="utf-8")
    _git(tree, "add", "f1.txt")
    got = _final(rig, branch="slice/5")
    assert got.state == ws.PUSHED and got.head == mine
    parents = _parents(rig, REF)
    assert parents[0] == mine and len(parents) == 2
    assert _g(rig.hub, "show", f"{parents[1]}:f1.txt") == "resolved1"
    assert "<<<<<<<" in _g(rig.hub, "show", f"{parents[1]}:f2.txt")
    assert _g(tree, "status", "--porcelain") == ""


def test_finalize_keeps_what_the_hub_ref_held_when_the_line_was_reset(
    rig: _Rig,
) -> None:
    """A builder that pushed and then reset the line behind its push must not
    take the pushed commits out of the hub ref: the new tip keeps them as a
    parent. A second finalize finds the hub already holding it all and stacks
    nothing."""
    tree = rig.cut()
    c1 = _commit(tree, "a.txt")
    c2 = _commit(tree, "b.txt")
    assert _session_push(tree, f"HEAD:{REF}").returncode == 0
    _git(tree, "reset", "-q", "--hard", "HEAD~1")
    got = _final(rig, branch="slice/5")
    assert got.state == ws.PUSHED and got.head == c1 and got.wip
    assert _parents(rig, REF) == [c1, c2]
    assert _g(rig.hub, "rev-parse", f"{REF}^{{tree}}") == _g(
        rig.hub, "rev-parse", f"{c1}^{{tree}}"
    )
    assert "the hub ref's earlier tip" in got.note
    first_tip = rig.hub_ref(REF)
    again = _final(rig, branch="slice/5")
    assert again.state == ws.PUSHED and again.by == "builder" and again.sha == first_tip
    assert rig.hub_ref(REF) == first_tip


def test_finalize_keeps_uncommitted_work_on_a_head_that_is_not_the_line(
    rig: _Rig,
) -> None:
    """HEAD on another branch with a dirty tree: the dirty tree is neither the
    line's nor reachable from it, so it is kept as its own tip."""
    tree = rig.cut()
    c1 = _commit(tree, "a.txt")
    _git(tree, "checkout", "-q", "-b", "side")
    _commit(tree, "side.txt", "side\n")
    (tree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    got = _final(rig, branch="slice/5")
    assert got.state == ws.PUSHED and got.head == c1
    parents = _parents(rig, REF)
    assert parents[0] == c1 and len(parents) == 3
    assert _g(rig.hub, "show", f"{parents[1]}:dirty.txt") == "dirty"
    assert "uncommitted work on side" in got.note and "branch side" in got.note
    assert _g(tree, "rev-parse", "--abbrev-ref", "HEAD") == "side"  # as left


def test_finalize_does_not_keep_tips_the_head_already_holds(rig: _Rig) -> None:
    """A branch or tag at an ancestor of the line holds nothing extra, so it
    must not turn a clean push into a stacked one (or cost an EMPTY)."""
    tree = rig.cut()
    c1 = _commit(tree, "a.txt")
    _git(tree, "branch", "old")
    _git(tree, "tag", "t1")
    got = _final(rig, branch="slice/5")
    assert (got.state, got.sha, got.wip) == (ws.PUSHED, c1, False)
    assert "kept" not in got.note
    empty = rig.cut(job=2)
    _git(empty, "branch", "spare")  # at origin/main: reachable from a remote ref
    assert (
        ws.finalize(
            empty, TD, 2, hub=rig.hub, base="main", start_sha=None, reason="x"
        ).state
        == ws.EMPTY
    )


def test_finalize_fails_rather_than_guess(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When what the folder holds cannot be established (an unknown line, a
    failing ``git stash list``) the answer is FAILED, so the folder stays."""
    tree = rig.cut()
    _commit(tree, "a.txt")
    assert _final(rig, branch="no-such-branch").state == ws.FAILED
    real = ws._git

    def broken(args: list[str], cwd: Path) -> Any:
        if args[:2] == ["stash", "list"]:
            return subprocess.CompletedProcess(args, 1, "", "stash exploded")
        return real(args, cwd)

    monkeypatch.setattr(ws, "_git", broken)
    got = _final(rig, branch="slice/5")
    assert got.state == ws.FAILED and not got.safe and "stash" in got.note
    assert rig.hub_ref(REF) is None


def test_finalize_names_untracked_files_it_left_out(rig: _Rig) -> None:
    """Files named like secrets never ride a snapshot. The note names them (not
    their content) so a dropped folder does not silently take them along,
    whether or not anything else was pushed."""
    tree = rig.cut()
    (tree / ".env.local").write_text("word = " + "hunter2" * 2 + "\n", encoding="utf-8")
    (tree / "tok.pem").write_text("pem\n", encoding="utf-8")
    empty = _final(rig)
    assert empty.state == ws.EMPTY
    assert ".env.local" in empty.note and "tok.pem" in empty.note
    assert "hunter2" not in empty.note
    (tree / "a.txt").write_text("real work\n", encoding="utf-8")
    pushed = _final(rig)
    assert pushed.state == ws.PUSHED and ".env.local" in pushed.note
    assert _g(rig.hub, "ls-tree", "--name-only", REF).split() == ["a.txt", "first.txt"]


# ── the worker never writes a branch or an import through the hook ──


def test_the_private_sync_and_the_legacy_import_do_not_push(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the hook tightened (no identity, no push) the base still syncs and
    a legacy ref still imports: both reach the hub by a hub-side fetch, never
    a push."""
    seen: list[list[str]] = []
    real = ws._mirror._run

    def spy(args: list[str], **kw: Any) -> Any:
        seen.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(ws._mirror, "_run", spy)
    new = rig.advance_github("two.txt")
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    assert (
        ws.sync_hub(rig.root, url=str(rig.github), base="main", private=True, env=env)
        == new
    )
    _git(rig.root, "checkout", "-q", "-b", "slice/5")
    old = _commit(rig.root, "old.txt")
    _git(rig.root, "push", "-q", str(rig.github), f"{old}:{ws.legacy_ref(5)}")
    _git(rig.root, "checkout", "-q", "main")
    assert ws.import_legacy(rig.hub, str(rig.github), 5, env=env) == old
    assert rig.hub_ref("refs/heads/main") == new
    assert rig.hub_ref(ws.build_ref(5, 0)) == old
    assert seen and not any("push" in argv for argv in seen)


def test_import_legacy_tells_no_work_from_a_failed_import(
    rig: _Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None means GitHub holds no legacy ref (start from the base). When it
    holds one and the import fails, or GitHub cannot be read, the caller must
    defer: HubUnreadable, never None."""
    assert ws.import_legacy(rig.hub, str(rig.github), 6) is None
    _git(rig.root, "push", "-q", str(rig.github), f"{rig.main}:{ws.legacy_ref(5)}")
    with pytest.raises(ws.HubUnreadable, match="next:"):
        ws.import_legacy(rig.hub, str(tmp_path / "gone.git"), 5)
    real = ws._mirror._run

    def failing(args: list[str], **kw: Any) -> Any:
        if "fetch" in args and "refs/heads/fetched" in " ".join(args):
            return subprocess.CompletedProcess(args, 1, "", "network is down")
        return real(args, **kw)

    monkeypatch.setattr(ws._mirror, "_run", failing)
    with pytest.raises(ws.HubUnreadable, match="network is down"):
        ws.import_legacy(rig.hub, str(rig.github), 5)
    assert rig.hub_ref(ws.build_ref(5, 0)) is None


# ── failures that must stay refusals ───────────────────────────────


def test_a_cut_that_cannot_put_its_folder_in_place_is_a_refusal(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch defers on WorkspaceError and nothing else; an OS error from
    the last step must not crash it, and must leave no half-made folder."""

    def no_rename(src: Any, dst: Any) -> None:
        raise OSError("directory not empty")

    tree = rig.tree()
    monkeypatch.setattr(ws.os, "rename", no_rename)
    with pytest.raises(ws.WorkspaceError, match="next:"):
        ws.cut(rig.hub, tree, base="main", branch="slice/5")
    assert not tree.exists() and not list(tree.parent.glob("*.cut-*"))


def test_sync_hub_never_raises(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hub that cannot be made or an OS error is None (a stale base is
    allowed), not an exception: a launch must not die of a failed sync."""

    def broken_sync(*a: Any, **k: Any) -> str:
        raise OSError("disk on fire")

    monkeypatch.setattr(ws, "_sync_once", broken_sync)
    assert ws.sync_hub(rig.root, url=str(rig.github), base="main") is None

    def broken_hub(*a: Any, **k: Any) -> Path:
        raise ws.WorkspaceError("no hub; next: x")

    monkeypatch.setattr(ws, "ensure_hub", broken_hub)
    assert ws.sync_hub(rig.root, url=str(rig.github), base="main") is None


# ── temporaries ────────────────────────────────────────────────────


def test_temporary_names_are_unique_per_call() -> None:
    """Two threads of one worker share a pid; a pid-only suffix would collide."""
    assert len({ws._uniq() for _ in range(300)}) == 300


def test_two_threads_ensure_the_same_new_hub(tmp_path: Path) -> None:
    """Concurrent launches on a fresh host: both get a working hub, and no
    half-built temporary is left behind."""
    from concurrent.futures import ThreadPoolExecutor

    root = tmp_path / "root"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _commit(root, "a.txt")
    with ThreadPoolExecutor(4) as pool:
        hubs = list(pool.map(lambda _: ws.ensure_hub(root), range(4)))
    assert len(set(hubs)) == 1
    assert (hubs[0] / "hooks" / "pre-receive").is_file()
    assert not list(hubs[0].parent.glob("*.cut-*"))
    assert not list((hubs[0] / "hooks").glob(".pre-receive.*"))
