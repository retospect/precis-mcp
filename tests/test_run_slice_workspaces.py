# ruff: noqa: F811
"""``run_slice`` against the real workspace layer (gr741695): real git in
``tmp_path``, a local bare repository playing GitHub, the host checkout a
clone of it, the real hub with its real ``pre-receive`` hook. Only the
session itself is scripted: the launch is recorded (``seams``), then the
test plays the builder with the very env the launch gave it, pushing from
the clone the submit cut.

The call order and arguments are ``tests/test_run_slice_park.py``'s (fake
workspace layer); these pin what the real layer does with them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from precis.workers.job_types import _slice_ws, run_slice
from tests.test_run_slice import (  # noqa: F401  (``seams`` is a fixture)
    _Ctx,
    _finish,
    _poll_ctx,
    _Store,
    seams,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only hub hook (sh stub) and flock"
)

#: The real functions, captured before the ``seams`` fixture fakes them.
_REAL = {
    name: getattr(_slice_ws, name)
    for name in (
        "attempt_dir",
        "hub_path",
        "ensure_hub",
        "sync_hub",
        "resolve_start",
        "import_legacy",
        "delete_builds",
        "cut",
        "finalize",
        "drop",
        "sweep",
    )
}
_REAL_AS_META = _slice_ws.Pushed.as_meta
_REAL_EXCLUDE = run_slice._ensure_info_exclude

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
#: Obviously fake, shaped like a GitHub token; split so this file does not
#: trip the repo's own secret scan.
FAKE_TOKEN = "".join(("gh", "p_", "a1B2c3D4e5F6", "g7H8i9J0k1L2", "m3N4o5P6q7R8"))


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> Any:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, **_GIT_ENV, **(env or {})},
    )


def _g(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return str(proc.stdout).strip()


def _commit(repo: Path, name: str, text: str = "x\n", msg: str | None = None) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _g(repo, "add", name)
    _g(repo, "commit", "-q", "-m", msg or f"feat(x): add {name} (td42)")
    return _g(repo, "rev-parse", "HEAD")


class _Rig:
    """A bare 'GitHub', the host checkout, the real hub, and the run_slice
    seams pointed at them."""

    def __init__(
        self,
        tmp_path: Path,
        seams: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.seams = seams
        self.monkeypatch = monkeypatch
        self.github = tmp_path / "github.git"
        _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.github))
        self.root = tmp_path / "root"
        _g(tmp_path, "clone", "-q", str(self.github), str(self.root))
        _g(self.root, "checkout", "-q", "-b", "main")
        # like the repository's own: the brief, the commit message and the
        # session's notes live under scratch/ and never count as work
        (self.root / ".gitignore").write_text("scratch/\n.venv/\n", encoding="utf-8")
        _g(self.root, "add", ".gitignore")
        self.main = _commit(self.root, "first.txt", "first\n", "chore: first")
        _g(self.root, "push", "-q", "origin", "main")
        monkeypatch.setenv("PRECIS_SLICE_ROOT", str(self.root))
        seams["origin_url"] = str(self.github)
        for name, fn in _REAL.items():
            monkeypatch.setattr(_slice_ws, name, fn)
        monkeypatch.setattr(_slice_ws.Pushed, "as_meta", _REAL_AS_META)
        monkeypatch.setattr(run_slice, "_git", seams["real_git"])
        monkeypatch.setattr(run_slice, "_ensure_info_exclude", _REAL_EXCLUDE)
        self.store = _Store()
        self.store.add_todo(42, "Build the widget", body="Do X.")

    @property
    def hub(self) -> Path:
        return _slice_ws.hub_path(self.root)

    def tree(self, job: int = 900) -> Path:
        return _slice_ws.attempt_dir(self.root, 42, job)

    def hub_ref(self, job: int = 900) -> str | None:
        return _slice_ws.hub_tip(self.hub, _slice_ws.build_ref(42, job))

    def submit(self, job: int = 900) -> tuple[_Ctx, Any]:
        self.seams["launch"].clear()
        ctx = _Ctx(self.store, {"todo_id": 42})
        ctx.ref_id = job
        return ctx, run_slice.submit(ctx, None)

    def launch_env(self) -> dict[str, str]:
        """The identity part of the env the launch gave the session."""
        [(_argv, kw)] = self.seams["launch"]
        return {k: kw["env"][k] for k in ("PRECIS_SLICE_ID", "PRECIS_SLICE_JOB")}

    session_env = launch_env

    def builder_push(self, tree: Path, ref: str, env: dict[str, str]) -> Any:
        return _git(tree, "push", "origin", f"+HEAD:{ref}", env=env)

    def poll(
        self,
        ctx: _Ctx,
        handle: Any,
        *,
        code: int = 0,
        result: str = "done.\ntests: ran",
        is_error: bool = False,
        **meta: Any,
    ) -> _Ctx:
        log = json.dumps({"is_error": is_error, "result": result}) + "\n"
        _finish(handle, code, log)
        pctx = _poll_ctx(self.store, ctx)
        pctx.ref_id = ctx.ref_id
        pctx.meta.update({**ctx.meta_set, **meta})
        assert run_slice.poll(pctx, handle) is True
        return pctx


@pytest.fixture
def rig(tmp_path: Path, seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> _Rig:
    for k, v in _GIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return _Rig(tmp_path, seams, monkeypatch)


def test_a_first_attempt_is_a_clone_at_the_base_with_the_session_env_and_brief(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    assert handle is not None and ctx.failure is None, ctx.failure
    tree = rig.tree()
    assert Path(handle["worktree"]) == tree and tree.name == "slice-td42-j900"
    assert _g(tree, "rev-parse", "HEAD") == rig.main
    assert _g(tree, "branch", "--show-current") == "slice/42"
    assert _g(tree, "rev-parse", "--git-dir") == ".git"  # a clone, not a worktree
    assert _g(tree, "remote", "get-url", "origin") == str(rig.hub)
    assert _g(tree, "rev-parse", "origin/main") == rig.main
    assert ctx.meta_set["start"] is None and ctx.meta_set["base"] == "main"
    # the env the session gets
    [(_argv, kw)] = rig.seams["launch"]
    assert kw["cwd"] == tree
    assert kw["env"]["PRECIS_SLICE_ID"] == "td42"
    assert kw["env"]["PRECIS_SLICE_JOB"] == "900"
    gate = Path(kw["env"]["PRECIS_GATE_SLOT_ROOT"])
    assert gate.is_absolute() and gate == rig.hub.parent  # the host's git common dir
    assert gate.name == ".git"
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "git push origin +HEAD:refs/factory/build/42/900" in brief
    assert "continues earlier work" not in brief
    assert "slice-td42-j900" in brief


def test_a_session_that_pushed_is_verified_landed_from_the_hub_and_dropped(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    head = _commit(tree, "widget.py", "VALUE = 1\n")
    pushed = rig.builder_push(tree, "refs/factory/build/42/900", rig.session_env())
    assert pushed.returncode == 0, pushed.stderr
    pctx = rig.poll(ctx, handle)
    assert pctx.status == "succeeded", pctx.failure
    push = pctx.meta_set["push"]
    assert push["state"] == "pushed" and push["by"] == "builder"
    assert push["sha"] == head and push["head"] == head
    assert rig.hub_ref() == head
    assert rig.store.refs[42].meta["build_ref"] == push
    # the land is for that ref and that head; nothing names a folder
    assert rig.seams["land_kw"] == [{"ref": "refs/factory/build/42/900", "head": head}]
    assert not tree.exists()
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "Push: pushed, by the builder" in summary


def test_a_session_that_exited_without_pushing_has_the_worker_push_and_the_folder_goes(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    head = _commit(tree, "widget.py", "VALUE = 1\n")
    rig.seams["ahead"] = "abc1234 feat(x): add widget.py (td42)\n"
    assert rig.hub_ref() is None
    pctx = rig.poll(ctx, handle)
    assert pctx.status == "succeeded", pctx.failure
    assert pctx.meta_set["push"]["by"] == "worker"
    assert rig.hub_ref() == head
    assert rig.seams["land_kw"] == [{"ref": "refs/factory/build/42/900", "head": head}]
    assert not tree.exists()


def test_a_session_killed_with_a_dirty_tree_pushes_a_snapshot_and_the_next_attempt_starts_from_it(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    real = _commit(tree, "widget.py", "VALUE = 1\n")
    (tree / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tree / "notes.txt").write_text("half done\n", encoding="utf-8")
    pctx = rig.poll(ctx, handle, code=137, result="", is_error=True)
    assert pctx.status == "failed"
    assert {"STATUS:open", "attempt:1"} <= rig.store.tag_strings(42)
    rec = rig.store.refs[42].meta["build_ref"]
    assert rec["state"] == "pushed" and rec["wip"] is True and rec["head"] == real
    snap = rec["sha"]
    assert snap != real and rig.hub_ref() == snap
    assert _g(
        rig.root, "--git-dir", str(rig.hub), "log", "-1", "--format=%s", snap
    ).startswith("wip: snapshot")
    assert not tree.exists()  # the hub holds it; the folder goes
    assert rig.seams["lands"] == []

    # the next attempt (a new job) starts at the snapshot and says so
    ctx2, handle2 = rig.submit(job=901)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    tree2 = rig.tree(901)
    assert _g(tree2, "rev-parse", "HEAD") == snap
    assert ctx2.meta_set["start"] == {
        "sha": snap,
        "ref": "refs/factory/build/42/900",
        "wip": True,
    }
    assert (tree2 / "notes.txt").read_text(encoding="utf-8") == "half done\n"
    brief = (tree2 / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "continues earlier work" in brief
    assert "git reset --soft HEAD~1" in brief
    # a snapshot is never routed to a land: a session was launched
    assert len(rig.seams["launch"]) == 1 and rig.seams["lands"] == []


def test_a_continuing_attempt_starts_at_the_pushed_commit_and_the_brief_says_so(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    head = _commit(tree, "widget.py", "VALUE = 1\n")
    assert (
        rig.builder_push(
            tree, "refs/factory/build/42/900", rig.session_env()
        ).returncode
        == 0
    )
    # the run fails its gates (no tests receipt) after pushing: the work is kept
    pctx = rig.poll(ctx, handle, result="done, no marker")
    assert pctx.status == "failed"
    rec = rig.store.refs[42].meta["build_ref"]
    assert rec["sha"] == head and rec["wip"] is False

    # complete pushed work goes straight to its land (no session) ...
    rig.seams["ahead"] = "abc1234 feat(x): add widget.py (td42)\n"
    rig.seams["cherry"] = "+ abc1234\n"
    run_slice_carried = run_slice._carried_on_base
    rig.monkeypatch.setattr(run_slice, "_carried_on_base", lambda *a, **k: None)
    routed_ctx, routed = rig.submit(job=901)
    assert routed is None
    assert routed_ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "branch-ahead"
    assert rig.seams["land_kw"][-1] == {
        "ref": "refs/factory/build/42/900",
        "head": head,
    }
    assert not rig.tree(901).exists() and rig.seams["launch"] == []
    rig.monkeypatch.setattr(run_slice, "_carried_on_base", run_slice_carried)

    # ... unless the lander's failure asks for a session: the clone starts
    # at the pushed commit and the brief says so
    run_slice._append_comment(
        rig.store, 42, "handoff: lander: land FAILED: ruff check . is red"
    )
    ctx2, handle2 = rig.submit(job=902)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    tree2 = rig.tree(902)
    assert _g(tree2, "rev-parse", "HEAD") == head
    assert ctx2.meta_set["start"]["sha"] == head
    brief = (tree2 / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "continues earlier work" in brief and head[:12] in brief
    assert "git reset --soft" not in brief


def _pushed_then_main_moved(rig: _Rig, text: str) -> str:
    """Job 900 pushes ``widget.py`` and fails its gates; GitHub's main then
    gains its own ``widget.py`` holding ``text`` (the same file when ``text``
    is set, else another file); the lander's handoff asks for a session."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    head = _commit(tree, "widget.py", "VALUE = 1\n")
    ref = "refs/factory/build/42/900"
    assert rig.builder_push(tree, ref, rig.session_env()).returncode == 0
    assert rig.poll(ctx, handle, result="done, no marker").status == "failed"
    _commit(rig.root, "widget.py" if text else "other.py", text or "OTHER = 1\n")
    _g(rig.root, "push", "-q", "origin", "main")
    run_slice._append_comment(
        rig.store, 42, "handoff: lander: land FAILED: ruff check . is red"
    )
    return head


def test_a_continuing_attempt_is_rebased_onto_the_base_that_moved(rig: _Rig) -> None:
    """Work pushed on an older main must be on today's main before the session
    builds on it: otherwise it is built, checked and reviewed against code the
    land will not meet."""
    head = _pushed_then_main_moved(rig, "")
    ctx2, handle2 = rig.submit(job=902)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    tree2 = rig.tree(902)
    now = _g(tree2, "rev-parse", "HEAD")
    assert now != head
    assert _g(tree2, "rev-parse", "HEAD~1") == _g(tree2, "rev-parse", "origin/main")
    assert (tree2 / "other.py").exists() and (tree2 / "widget.py").exists()
    start = ctx2.meta_set["start"]
    assert (start["sha"], start["rebased"], start["conflicts"]) == (head, now, [])
    brief = (tree2 / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "The worker rebased it onto the current `origin/main`" in brief
    assert head[:12] in brief and now[:12] in brief
    assert any("rebased the earlier work" in t for k, t in ctx2.chunks)


def test_a_rebased_attempt_that_added_nothing_is_pushed_and_landed_as_rebased(
    rig: _Rig,
) -> None:
    """The rebased commits are this attempt's work: a session that adds
    nothing still leaves them pushed under its own ref, and the land is for
    that ref and head, not for the old attempt's."""
    old = _pushed_then_main_moved(rig, "")
    ctx2, handle2 = rig.submit(job=902)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    tree2 = rig.tree(902)
    rebased = _g(tree2, "rev-parse", "HEAD")
    assert rebased != old
    rig.seams["ahead"] = "abc1234 feat(x): add widget.py (td42)\n"
    rig.seams["land_kw"].clear()
    pctx = rig.poll(ctx2, handle2)
    assert pctx.status == "succeeded", pctx.failure
    push = pctx.meta_set["push"]
    assert (push["state"], push["by"]) == ("pushed", "worker")
    assert push["sha"] == rebased
    assert rig.hub_ref(902) == rebased
    assert rig.hub_ref(900) == old
    assert rig.store.refs[42].meta["build_ref"] == push
    assert rig.seams["land_kw"] == [
        {"ref": "refs/factory/build/42/902", "head": rebased}
    ]
    assert not tree2.exists()


def test_a_rebased_attempt_that_was_killed_leaves_the_rebased_commit_in_the_hub(
    rig: _Rig,
) -> None:
    """A kill after the worker's rebase must not lose it or fall back to the
    old attempt: the hub holds the rebased commit under this job's ref."""
    old = _pushed_then_main_moved(rig, "")
    ctx2, handle2 = rig.submit(job=902)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    tree2 = rig.tree(902)
    rebased = _g(tree2, "rev-parse", "HEAD")
    assert rebased != old and _g(tree2, "status", "--porcelain") == ""
    rig.seams["land_kw"].clear()
    pctx = rig.poll(ctx2, handle2, code=137, result="", is_error=True)
    assert pctx.status == "failed"
    rec = rig.store.refs[42].meta["build_ref"]
    assert rec["state"] == "pushed" and rec["sha"] == rebased
    assert rec["ref"] == "refs/factory/build/42/902"
    assert rig.hub_ref(902) == rebased
    assert not tree2.exists()
    assert rig.seams["land_kw"] == []


def test_a_continuing_attempt_that_conflicts_with_the_base_is_told_first(
    rig: _Rig,
) -> None:
    """A conflict with main is cheapest to resolve before more work is piled
    on it: the clone stays at the pushed commit and the brief names the paths."""
    head = _pushed_then_main_moved(rig, "VALUE = 'from main'\n")
    ctx2, handle2 = rig.submit(job=902)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    tree2 = rig.tree(902)
    assert _g(tree2, "rev-parse", "HEAD") == head
    assert _g(tree2, "status", "--porcelain") == ""
    start = ctx2.meta_set["start"]
    assert (start["sha"], start["rebased"], start["conflicts"]) == (
        head,
        None,
        ["widget.py"],
    )
    brief = (tree2 / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "git stopped on conflicts in `widget.py`" in brief
    assert "Do that rebase first, before any other change" in brief


def test_a_withheld_push_keeps_the_folder_and_names_host_and_folder(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    _commit(tree, "creds.py", f'TOKEN = "{FAKE_TOKEN}"\n')
    rig.seams["ahead"] = "abc1234 feat(x): add creds.py (td42)\n"
    pctx = rig.poll(ctx, handle)
    push = pctx.meta_set["push"]
    assert push["state"] == "withheld"
    assert rig.hub_ref() is None and "build_ref" not in rig.store.refs[42].meta
    assert tree.is_dir()  # the work exists only here
    # the outcome is what a run with no pushed work is: no land, a failed attempt
    assert pctx.status == "failed" and rig.seams["lands"] == []
    kept = [c for c in rig.store.comments(42) if "was kept" in c]
    assert len(kept) == 1 and str(tree) in kept[0] and push["host"] in kept[0]
    assert any(k == "job_event" and "was kept" in t for k, t in pctx.chunks)


def test_an_outage_relaunch_in_the_same_job_finalizes_drops_and_cuts_again(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    _commit(tree, "widget.py", "VALUE = 1\n")
    (tree / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")
    rig.seams["alive"] = False
    # the same job runs submit again, as _relaunch_when_due does
    ctx2 = _Ctx(rig.store, {"todo_id": 42}, start=None, base="main")
    ctx2.ref_id = 900
    handle2 = run_slice.submit(ctx2, run_slice.Relaunch({"own_folder": True}))
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    push = rig.store.refs[42].meta["build_ref"]  # the settle recorded it
    assert ctx2.meta_set["push"] is None  # the new session's launch cleared it
    assert push["state"] == "pushed" and push["wip"] is True
    assert rig.hub_ref() == push["sha"]
    assert _g(tree, "rev-parse", "HEAD") == push["sha"]  # the new clone, same name
    assert (tree / "widget.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert ctx2.meta_set["start"]["wip"] is True


def test_start_over_deletes_the_attempts_and_starts_at_the_base(rig: _Rig) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    _commit(tree, "widget.py", "VALUE = 1\n")
    rig.poll(ctx, handle, code=1, result="boom", is_error=True)
    assert rig.hub_ref() is not None
    rig.store.refs[42].meta["start_over"] = 77
    ctx2, handle2 = rig.submit(job=901)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    assert rig.hub_ref() is None  # the attempt ref is gone
    assert _g(rig.tree(901), "rev-parse", "HEAD") == rig.main
    assert ctx2.meta_set["start"] is None
    meta = rig.store.refs[42].meta
    assert meta["build_ref"] is None and meta["start_over"] is None


@pytest.mark.parametrize("legacy_tip", ["absent", "older", "saved"])
def test_legacy_park_requires_its_saved_head_or_start_over(
    rig: _Rig, legacy_tip: str
) -> None:
    _g(rig.root, "checkout", "-qb", "slice/42")
    saved = _commit(rig.root, "widget.py", msg="wip: parked awaiting answer (td42)")
    _g(rig.root, "checkout", "-q", "main")
    if legacy_tip != "absent":
        tip = saved if legacy_tip == "saved" else rig.main
        _g(rig.root, "push", "-q", "origin", f"{tip}:{_slice_ws.legacy_ref(42)}")
    meta = rig.store.refs[42].meta
    meta["parked"] = {"head": saved, "host": "maple", "worktree": "slice-td42"}

    ctx, handle = rig.submit()
    if legacy_tip == "saved":
        assert handle is not None, ctx.failure
        assert _g(rig.tree(), "rev-parse", "HEAD") == saved
        return

    assert handle is None
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    assert saved in str(ctx.failure) and "maple" in str(ctx.failure)
    assert not rig.seams["launch"] and not rig.tree().exists()
    assert "attempt:1" not in rig.store.tag_strings(42)
    assert meta["parked"]["head"] == saved

    meta["start_over"] = 77
    restarted, handle = rig.submit(job=901)
    assert handle is not None, restarted.failure
    assert _g(rig.tree(901), "rev-parse", "HEAD") == rig.main
    assert rig.store.refs[42].meta["parked"] is None


def test_a_recorded_ref_that_moved_ends_the_job_attempt_free(rig: _Rig) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    _commit(tree, "widget.py", "VALUE = 1\n")
    rig.poll(ctx, handle, code=1, result="boom", is_error=True)
    ref = _slice_ws.build_ref(42, 900)
    _g(rig.hub.parent, "--git-dir", str(rig.hub), "update-ref", ref, rig.main)
    ctx2, handle2 = rig.submit(job=901)
    assert handle2 is None
    assert ctx2.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    assert "next:" in str(ctx2.failure)
    assert not rig.tree(901).exists()
    # the lease went back unburned; the attempt count stays at the one failure
    assert "attempt:1" in rig.store.tag_strings(42)
    assert "attempt:2" not in rig.store.tag_strings(42)


def test_the_launch_env_is_what_the_hook_reads(rig: _Rig) -> None:
    """The builder pushes with exactly the env the launch gave it
    (``PRECIS_SLICE_ID=td<N>``). If this fails, every builder push is
    refused ("must both be numbers")."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    _commit(tree, "widget.py", "VALUE = 1\n")
    pushed = rig.builder_push(tree, "refs/factory/build/42/900", rig.launch_env())
    assert pushed.returncode == 0, pushed.stderr


def test_the_hubs_hook_takes_only_the_sessions_own_ref(rig: _Rig) -> None:
    """The env the launch gives the session is exactly what the fence reads."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    _commit(tree, "widget.py", "VALUE = 1\n")
    env = rig.session_env()
    other = rig.builder_push(tree, "refs/factory/build/42/777", env)
    assert other.returncode != 0 and "precis-hub: refused" in other.stderr
    mine = rig.builder_push(tree, "refs/factory/build/42/900", env)
    assert mine.returncode == 0, mine.stderr
    rig.poll(ctx, handle)


def test_a_reviewer_reads_the_reviewed_attempt_from_the_hub_in_its_own_clone(
    rig: _Rig,
) -> None:
    """The held land's ref is fetched into the reviewer's clone as
    ``origin/slice/<target>``; the head the reviewer judges is
    ``pending_land.head``."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    head = _commit(tree, "widget.py", "VALUE = 1\n")
    assert (
        rig.builder_push(
            tree, "refs/factory/build/42/900", rig.session_env()
        ).returncode
        == 0
    )
    rig.poll(ctx, handle)
    rig.store.refs[42].meta["pending_land"] = {
        "todo_id": 42,
        "branch": "slice/42",
        "ref": "refs/factory/build/42/900",
        "head": head,
    }
    rig.store.add_todo(
        55,
        "review td42",
        meta={"generate": {"role": "reviewer", "from": "td42"}},
    )
    reviewer = _Ctx(rig.store, {"todo_id": 55})
    reviewer.ref_id = 950
    rig.seams["launch"].clear()
    handle2 = run_slice.submit(reviewer, None)
    assert handle2 is not None and reviewer.failure is None, reviewer.failure
    rtree = _slice_ws.attempt_dir(rig.root, 55, 950)
    assert _g(rtree, "rev-parse", "origin/slice/42") == head
    brief = (rtree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert f"The branch head is `{head}`" in brief
    assert "git diff origin/main...origin/slice/42" in brief
    assert "STALE CHECKOUT" not in brief


def test_a_resolution_review_has_both_heads_and_a_working_range_diff(
    rig: _Rig,
) -> None:
    """The reviewed head (job 900, before the base moved) and the resolution
    (job 901, rebased onto the moved base by the worker at the cut) are both
    in the reviewer's clone; the range-diff the brief names runs and marks
    the commit changed."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    first = _commit(tree, "widget.py", "VALUE = 1\n")
    assert (
        rig.builder_push(tree, "refs/factory/build/42/900", rig.launch_env()).returncode
        == 0
    )
    rig.poll(ctx, handle)
    # the base moves under it
    _commit(rig.root, "other.py", "OTHER = 1\n", "feat(y): other (td43)")
    _g(rig.root, "push", "-q", "origin", "main")
    # job 901 is the resolution: the worker rebases the pushed work onto the
    # moved base at the cut; the session's own rebase finds nothing to do
    run_slice._append_comment(
        rig.store, 42, "handoff: lander: land FAILED: conflicts with origin/main"
    )
    ctx2, handle2 = rig.submit(job=901)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    tree2 = rig.tree(901)
    assert _g(tree2, "rev-parse", "HEAD") != first
    assert _g(tree2, "rev-parse", "HEAD~1") == _g(tree2, "rev-parse", "origin/main")
    before = _g(tree2, "rev-parse", "HEAD")
    assert _git(tree2, "rebase", "origin/main").returncode == 0
    resolved = _g(tree2, "rev-parse", "HEAD")
    assert resolved == before != first
    assert (
        rig.builder_push(
            tree2, "refs/factory/build/42/901", rig.launch_env()
        ).returncode
        == 0
    )
    rig.poll(ctx2, handle2)
    # the reviewer of the resolution
    rig.store.refs[42].meta["pending_land"] = {
        "todo_id": 42,
        "branch": "slice/42",
        "ref": "refs/factory/build/42/901",
        "head": resolved,
    }
    rig.store.add_todo(
        55, "review td42", meta={"generate": {"role": "reviewer", "from": "td42"}}
    )
    reviewer = _Ctx(
        rig.store,
        {
            "todo_id": 55,
            "resolution": {"of": first, "ref": "refs/factory/build/42/900"},
        },
    )
    reviewer.ref_id = 950
    rig.seams["launch"].clear()
    handle3 = run_slice.submit(reviewer, None)
    assert handle3 is not None and reviewer.failure is None, reviewer.failure
    rtree = _slice_ws.attempt_dir(rig.root, 55, 950)
    assert _g(rtree, "rev-parse", "origin/slice/42") == resolved
    assert _g(rtree, "cat-file", "-t", first) == "commit"
    brief = (rtree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert f"git range-diff origin/main {first} origin/slice/42" in brief
    shown = _g(rtree, "range-diff", "origin/main", first, "origin/slice/42")
    assert "feat(x): add widget.py" in shown
    # a hub that lost the reviewed head refuses the review, attempt-free
    _g(
        rig.hub.parent,
        "--git-dir",
        str(rig.hub),
        "update-ref",
        "-d",
        "refs/factory/build/42/900",
    )
    other = _Ctx(
        rig.store,
        {
            "todo_id": 55,
            "resolution": {"of": first, "ref": "refs/factory/build/42/900"},
        },
    )
    other.ref_id = 951
    shutil.rmtree(rtree)  # the sweep dropped the first reviewer's folder
    assert run_slice.submit(other, None) is None
    assert other.meta_set[run_slice.ATTEMPT_FREE_KEY] == run_slice.REVIEW_WAIT_KEY
    assert "next:" in str(other.failure)


def test_a_builders_own_wip_snapshot_is_continued_never_landed_or_held(
    rig: _Rig,
) -> None:
    """A builder that could not finish pushes ``wip: snapshot — <why>`` and
    exits clean. No land and no review hold is minted for it; the next
    submit launches a session (not the no-session route) whose brief says
    where it starts and how to keep or drop the snapshot's edits."""
    rig.seams["preland"] = 77  # a live pre-land review would otherwise hold it
    ctx, handle = rig.submit()
    tree = rig.tree()
    snap = _commit(
        tree, "widget.py", "VALUE = 1\n", "wip: snapshot — ran out of turns (td42)"
    )
    assert (
        rig.builder_push(tree, "refs/factory/build/42/900", rig.launch_env()).returncode
        == 0
    )
    rig.seams["ahead"] = "abc1234 wip: snapshot — ran out of turns (td42)\n"
    pctx = rig.poll(ctx, handle)
    assert pctx.status == "failed"
    assert rig.seams["lands"] == [] and "pending_land" not in rig.store.refs[42].meta
    assert "preland_review" not in pctx.meta_set
    # the next attempt continues from it, in a session
    rig.seams["cherry"] = "+ abc1234\n"
    ctx2, handle2 = rig.submit(job=901)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    assert rig.seams["lands"] == []
    assert ctx2.meta_set["start"] == {
        "sha": snap,
        "ref": "refs/factory/build/42/900",
        "wip": True,
    }
    brief = (rig.tree(901) / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "continues earlier work" in brief
    assert "`git reset --soft HEAD~1` keeps them as uncommitted changes" in brief
    assert "`git reset --hard HEAD~1` discards them" in brief


def test_a_session_that_resets_to_the_base_and_validates_moves_nothing(
    rig: _Rig,
) -> None:
    """Under a live pre-land review the rebuild resets its clone to the base
    and answers ``validated:``. ``finalize`` says ``empty``; ``build_ref``
    and the held land keep the reviewed work, never the base tip."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    reviewed = _commit(tree, "widget.py", "VALUE = 1\n")
    assert (
        rig.builder_push(tree, "refs/factory/build/42/900", rig.launch_env()).returncode
        == 0
    )
    rig.poll(ctx, handle, result="done, no marker")  # fails its gate: work kept
    recorded = dict(rig.store.refs[42].meta["build_ref"])
    assert recorded["head"] == reviewed
    # the rebuild: a steer asking for a session, then a reset and a verdict
    run_slice._append_comment(
        rig.store, 42, "handoff: lander: land FAILED: ruff check . is red"
    )
    rig.seams["preland"] = 77
    ctx2, handle2 = rig.submit(job=901)
    assert handle2 is not None and ctx2.failure is None, ctx2.failure
    tree2 = rig.tree(901)
    assert _g(tree2, "rev-parse", "HEAD") == reviewed
    _g(tree2, "reset", "--hard", "origin/main")
    rig.seams["ahead"] = ""
    pctx = rig.poll(
        ctx2,
        handle2,
        result="validated: the branch already holds the review fix\ntests: ran",
    )
    assert pctx.status == "succeeded", pctx.failure
    assert pctx.meta_set["push"]["state"] == "empty"
    meta = rig.store.refs[42].meta
    assert meta["build_ref"] == recorded  # not moved to the base tip
    assert meta["pending_land"]["head"] == reviewed
    assert meta["pending_land"]["ref"] == "refs/factory/build/42/900"
    assert rig.main not in (meta["pending_land"]["head"], meta["build_ref"]["sha"])
    assert not rig.tree(901).exists()  # empty is safe: the folder goes


def test_a_clones_own_info_exclude_takes_the_profiles_lines(rig: _Rig) -> None:
    """The excludes used to be written into the host checkout's common dir,
    which worktrees shared; a clone has its own ``.git/info/exclude``."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    run_slice._ensure_info_exclude(tree, ("generated/", "notes.local"))
    (tree / "generated").mkdir()
    (tree / "generated" / "x.txt").write_text("x\n", encoding="utf-8")
    (tree / "notes.local").write_text("n\n", encoding="utf-8")
    assert _g(tree, "status", "--porcelain") == ""
    assert "generated/" in (tree / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    )
    # not the host checkout's
    assert "generated/" not in (rig.root / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    )


def test_a_folder_that_lost_its_dot_git_never_acts_on_the_host_checkout(
    rig: _Rig,
) -> None:
    """gr742505: a folder under ``<host>/.claude/worktrees`` with no ``.git``
    makes every ``git`` run there act on the host checkout above it.
    ``run_slice._git`` and the session's env set ``GIT_CEILING_DIRECTORIES``."""
    folder = rig.tree()
    folder.mkdir(parents=True)
    (folder / "stray.txt").write_text("x\n", encoding="utf-8")
    # the hazard: plain git in the folder finds the host's repository
    assert _git(folder, "rev-parse", "--git-dir").returncode == 0
    # run_slice's own git: not a repository
    got = run_slice._git(["rev-parse", "--git-dir"], folder)
    assert got.returncode != 0 and "not a git repository" in got.stderr
    # the session's env: the same
    env = run_slice.slice_env(
        42, "low", worktrees_root=run_slice.worktrees_dir(rig.root)
    )
    assert env["GIT_CEILING_DIRECTORIES"] == str(run_slice.worktrees_dir(rig.root))
    for args in (["status"], ["add", "-A"], ["commit", "-qm", "x"]):
        proc = subprocess.run(
            ["git", *args],
            cwd=str(folder),
            env={**env, **_GIT_ENV},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert proc.returncode != 0 and "not a git repository" in proc.stderr, args
    # the host checkout is untouched
    # (nothing staged or committed: the only entry is the untracked folder
    # of attempts itself)
    assert _g(rig.root, "status", "--porcelain") == "?? .claude/"
    assert _g(rig.root, "rev-parse", "HEAD") == rig.main
    # a repository root and its subdirectories are not given a ceiling
    assert run_slice._git(["rev-parse", "--git-dir"], rig.root).returncode == 0
    sub = rig.root / "sub"
    sub.mkdir()
    assert run_slice._git(["rev-parse", "--git-dir"], sub).returncode == 0


def test_the_launch_env_carries_the_ceiling(rig: _Rig) -> None:
    ctx, handle = rig.submit()
    [(_argv, kw)] = rig.seams["launch"]
    assert kw["env"]["GIT_CEILING_DIRECTORIES"] == str(
        run_slice.worktrees_dir(rig.root)
    )


def test_a_second_repositorys_attempt_gets_its_excludes_through_open_attempt(
    rig: _Rig,
) -> None:
    """The real ``_open_attempt`` path: the clone is cut, then the profile's
    exclude lines are written into the clone's own ``.git/info/exclude``."""
    from dataclasses import replace

    from precis.plans import lanes

    profile = replace(
        lanes.REPO_PROFILES["catpath"],
        remote=str(rig.github),
        private=False,
        exclude=("generated/", "notes.local"),
    )
    ctx = _Ctx(rig.store, {"todo_id": 42})
    ctx.ref_id = 900
    notes: list[str] = []
    opened = run_slice._open_attempt(
        ctx, rig.store, 42, rig.root, "main", profile, notes
    )
    tree = opened.tree
    (tree / "generated").mkdir()
    (tree / "generated" / "x.txt").write_text("x\n", encoding="utf-8")
    (tree / "notes.local").write_text("n\n", encoding="utf-8")
    (tree / "scratch").mkdir(exist_ok=True)
    assert _g(tree, "status", "--porcelain") == ""
    text = (tree / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "generated/" in text and "notes.local" in text


# ── one sequential place settles a folder: the poll or the sweep (round 6) ──


def _tidy(rig: _Rig, status: str, handle: Any, *, job: int = 900, **meta: Any) -> None:
    """The next watch pass's ``tidy`` over a terminal job's folder."""
    from tests.test_run_slice import _Ref

    rig.store.refs.setdefault(job, _Ref(job, "job", {}, None))
    rig.seams["alive"] = False
    row = (
        status,
        {
            "params": {"todo_id": 42},
            "compute_handle": dict(handle),
            "base": "main",
            "finished_at": "2020-01-01T00:00:00+00:00",
            # what the sweep stamped on the job earlier is what it reads now,
            # as in the database
            **rig.store.refs[job].meta,
            **meta,
        },
    )
    if isinstance(rig.seams["job_row"], dict):
        rig.seams["job_row"][job] = row
    else:
        rig.seams["job_row"] = row
    rig.monkeypatch.setattr(run_slice, "_tidy_last", None)
    run_slice.tidy(rig.store, None)


def _release(rig: _Rig, handle: Any, ctx: _Ctx) -> _Ctx:
    rctx = _Ctx(rig.store, {"todo_id": 42}, lane="claude-sub")
    rctx.ref_id = ctx.ref_id
    run_slice.release(rctx, handle)
    return rctx


def _no_lock_files(rig: _Rig) -> bool:
    folders = run_slice.worktrees_dir(rig.root)
    return not [e for e in folders.iterdir() if e.name.endswith(".lock")]


def test_a_job_ended_without_a_poll_is_saved_by_the_next_tidy_and_release_writes_nothing(
    rig: _Rig,
) -> None:
    """A cancel or a deadline kill: no poll ever runs. ``release`` touches
    nothing; the sweep saves the uncommitted work as a snapshot, stamps the
    record and the task's ``build_ref``, drops the folder, and the next
    attempt starts from the snapshot."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    real = _commit(tree, "widget.py", "VALUE = 1\n")
    (tree / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tree / "notes.txt").write_text("half done\n", encoding="utf-8")
    before = _folder_state(tree)
    rctx = _release(rig, handle, ctx)
    assert _folder_state(tree) == before and rig.hub_ref() is None
    assert "push" not in rctx.meta_set and "build_ref" not in rig.store.refs[42].meta
    _tidy(rig, "cancelled", handle)
    record = rig.store.refs[900].meta["push"]
    assert record["state"] == "pushed" and record["wip"] is True
    assert record["head"] == real and rig.hub_ref() == record["sha"]
    assert not tree.exists()
    assert rig.store.refs[42].meta["build_ref"]["sha"] == record["sha"]
    assert _no_lock_files(rig)
    ctx2, handle2 = rig.submit(job=901)
    assert handle2 is not None and "not_before" not in handle2
    assert ctx2.meta_set["start"]["sha"] == record["sha"]
    assert (rig.tree(901) / "widget.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_a_hard_kill_mid_commit_is_saved_by_the_next_tidy(rig: _Rig) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    (tree / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    _g(tree, "add", "widget.py")
    (tree / ".git" / "index.lock").write_text("", encoding="utf-8")  # killed git
    _release(rig, handle, ctx)
    assert (tree / ".git" / "index.lock").exists()  # release did not clear it
    _tidy(rig, "failed", handle)
    record = rig.store.refs[900].meta["push"]
    assert record["state"] == "pushed" and record["wip"] is True
    assert _g(
        rig.root, "--git-dir", str(rig.hub), "show", f"{record['sha']}:widget.py"
    ) == ("VALUE = 1")
    assert not tree.exists() and _no_lock_files(rig)


def test_a_withheld_secret_is_kept_and_reported_once_across_sweeps(rig: _Rig) -> None:
    """The job row the sweep reads carries what the sweep stamped on the job,
    as the database does: the first sweep judges the folder, the next two take
    its record. One finalize, one stamp, one comment, one alert."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    _commit(tree, "creds.py", f'TOKEN = "{FAKE_TOKEN}"\n')
    _release(rig, handle, ctx)
    finalizes: list[str] = []
    real_finalize = _slice_ws.finalize

    def counting(*args: Any, **kw: Any) -> Any:
        finalizes.append(kw.get("reason", ""))
        return real_finalize(*args, **kw)

    rig.monkeypatch.setattr(_slice_ws, "finalize", counting)
    stamps: list[int] = []
    real_stamp = rig.store.stamp_ref_meta

    def stamp(ref_id: int, updates: dict[str, Any], **kw: Any) -> None:
        stamps.append(ref_id)
        real_stamp(ref_id, updates, **kw)

    rig.monkeypatch.setattr(rig.store, "stamp_ref_meta", stamp, raising=False)
    for _ in range(3):  # three watch passes
        _tidy(rig, "failed", handle)
    assert finalizes == ["run ended"] and stamps == [900]
    assert rig.store.refs[900].meta["push"]["state"] == "withheld"
    assert tree.is_dir() and rig.hub_ref() is None
    assert "build_ref" not in rig.store.refs[42].meta
    kept = [c for c in rig.store.comments(42) if "was kept" in c]
    assert len(kept) == 1 and str(tree) in kept[0]
    raised = [d for k, d in rig.seams["alerts"] if k == "raise"]
    assert raised and {d["fingerprint"] for d in raised} == {tree.name}
    assert _no_lock_files(rig)


def test_a_poll_that_finds_its_job_already_terminal_writes_nothing(rig: _Rig) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    _work_to_commit(tree)
    before = _folder_state(tree)
    rig.seams["job_row"] = ("cancelled", {})
    pctx, done = _poll_once(rig, ctx, handle)
    assert done is True and pctx.status is None
    assert _folder_state(tree) == before and rig.hub_ref() is None
    assert rig.seams["lands"] == [] and "push" not in pctx.meta_set


def test_the_poll_adopts_a_push_of_its_own_launch_a_crash_left_behind(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    head = _commit(tree, "widget.py", "VALUE = 1\n")
    pushed = rig.builder_push(tree, "refs/factory/build/42/900", rig.session_env())
    assert pushed.returncode == 0, pushed.stderr
    record = {
        "state": "pushed",
        "ref": "refs/factory/build/42/900",
        "sha": head,
        "head": head,
        "by": "builder",
        "launch": run_slice._launch_key(handle),
    }
    # the poll recorded this and died before the land
    rig.seams["job_row"] = ("running", {"push": record})
    rig.seams["ahead"] = "abc1234 feat(x): add widget.py (td42)\n"
    pctx, done = _poll_once(rig, ctx, handle)
    assert done is True and pctx.status == "succeeded", pctx.failure
    assert rig.seams["land_kw"] == [{"ref": "refs/factory/build/42/900", "head": head}]
    assert not tree.exists() and _no_lock_files(rig)


def test_a_launch_waits_for_the_sweep_to_save_the_earlier_attempt_then_continues_from_it(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    (tree / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    _release(rig, handle, ctx)  # the job ended with unsaved work
    rig.seams["alive"] = False
    # per job: the ended one (no record yet) and the job being launched
    rig.seams["job_row"] = {
        900: (
            "failed",
            {
                "params": {"todo_id": 42},
                "compute_handle": dict(handle),
                "base": "main",
            },
        ),
        901: ("running", {"params": {"todo_id": 42}}),
    }
    ctx2, handle2 = rig.submit(job=901)
    assert "not_before" in handle2 and handle2["folder_wait"] == tree.name
    assert not rig.tree(901).exists()
    assert (tree / "widget.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"  # untouched
    # a retry before the sweep: still parked
    handle2["not_before"] = "2000-01-01T00:00:00Z"
    pctx = _poll_ctx(rig.store, ctx2)
    pctx.ref_id = 901
    assert run_slice.poll(pctx, handle2) is False and "not_before" in handle2
    assert tree.is_dir()
    # the sweep saves the work; the retry then launches from it
    _tidy(rig, "failed", handle)
    record = rig.store.refs[900].meta["push"]
    handle2["not_before"] = "2000-01-01T00:00:00Z"
    pctx2 = _poll_ctx(rig.store, ctx2)
    pctx2.ref_id = 901
    assert run_slice.poll(pctx2, handle2) is False
    assert "not_before" not in handle2 and handle2["worktree"] == str(rig.tree(901))
    assert _g(rig.tree(901), "rev-parse", "HEAD") == record["sha"]
    assert _no_lock_files(rig)


def _work_to_commit(tree: Path) -> None:
    """Uncommitted work and the message the worker commits it with."""
    (tree / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tree / "scratch").mkdir(exist_ok=True)
    (tree / "scratch" / "commit-msg.txt").write_text(
        "feat(x): add widget.py (td42)\n", encoding="utf-8"
    )


def _folder_state(tree: Path) -> tuple[str, str, str]:
    """HEAD, the index and the worktree's status."""
    return (
        _g(tree, "rev-parse", "HEAD"),
        _g(tree, "diff", "--cached"),
        _g(tree, "status", "--porcelain"),
    )


def _poll_once(rig: _Rig, ctx: _Ctx, handle: Any) -> tuple[_Ctx, bool]:
    _finish(handle, 0, json.dumps({"is_error": False, "result": "done.\ntests: ran"}))
    pctx = _poll_ctx(rig.store, ctx)
    pctx.ref_id = ctx.ref_id
    pctx.meta.update(ctx.meta_set)
    return pctx, run_slice.poll(pctx, handle)


# ── round 8: a stored record is no licence to delete ─────────────────────────


def _record_for(handle: Any, head: str, **extra: Any) -> dict[str, Any]:
    return {
        "state": "pushed",
        "ref": "refs/factory/build/42/900",
        "sha": head,
        "head": head,
        "wip": False,
        "by": "builder",
        "launch": run_slice._launch_key(handle),
        **extra,
    }


def test_a_commit_made_after_the_stored_record_reaches_the_hub_before_the_sweep_drops(
    rig: _Rig,
) -> None:
    """The finding that a stored PUSHED record was trusted to delete: the
    session committed again after the record was made (the job ended while its
    handle was still the parked one, no pid, so the record read as settled)."""
    ctx, handle = rig.submit()
    tree = rig.tree()
    first = _commit(tree, "widget.py", "VALUE = 1\n")
    pushed = rig.builder_push(tree, "refs/factory/build/42/900", rig.session_env())
    assert pushed.returncode == 0, pushed.stderr
    record = _record_for(handle, first)
    later = _commit(tree, "more.py", "MORE = 1\n")  # never pushed
    assert rig.hub_ref() == first and later != first
    parked = {k: v for k, v in handle.items() if k not in ("pid", "pgid")}
    parked["own_folder"] = True
    _tidy(rig, "cancelled", parked, push=record)
    assert rig.hub_ref() == later  # in the hub before the folder is gone
    assert not tree.exists()
    stamped = rig.store.refs[900].meta["push"]
    assert stamped["sha"] == later
    assert rig.store.refs[42].meta["build_ref"]["sha"] == later


def test_a_commit_made_after_the_adopted_record_reaches_the_hub_before_the_poll_drops(
    rig: _Rig,
) -> None:
    ctx, handle = rig.submit()
    tree = rig.tree()
    first = _commit(tree, "widget.py", "VALUE = 1\n")
    pushed = rig.builder_push(tree, "refs/factory/build/42/900", rig.session_env())
    assert pushed.returncode == 0, pushed.stderr
    record = _record_for(handle, first)
    later = _commit(tree, "more.py", "MORE = 1\n")
    # the earlier pass of this poll stored the record and died before the land
    rig.seams["job_row"] = ("running", {"params": {"todo_id": 42}, "push": record})
    rig.seams["ahead"] = "abc1234 feat(x): add more.py (td42)\n"
    pctx, done = _poll_once(rig, ctx, handle)
    assert done is True and pctx.status == "succeeded", pctx.failure
    assert rig.hub_ref() == later and not tree.exists()
    assert rig.seams["land_kw"] == [{"ref": "refs/factory/build/42/900", "head": later}]
    assert pctx.meta_set["push"]["sha"] == later
    assert any("newer than the stored record" in t for k, t in pctx.chunks)
