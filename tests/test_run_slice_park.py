# ruff: noqa: F811
"""Start-over, continuation and the run's end at the ``run_slice`` seams
(gr741695): the workspace layer (``_slice_ws``) is faked by
``tests/test_run_slice.py``'s ``seams`` fixture, so these tests pin the
order and the arguments of the calls and what the job and the task record.
The real hub, clone and push are ``tests/test_run_slice_workspaces.py``.

What a failed or killed run leaves behind is the hub's, not the folder's:
the worker finalizes (a ``wip:`` snapshot of uncommitted work included)
and the next attempt starts from it; the earlier "park = save, resume
anywhere" machinery (``meta.saved``, ``meta.parked``, the park-save at a
failed end) is retired."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.plans import lanes
from precis.workers.job_types import _slice_ws, run_slice
from tests._lane_rows import use_lanes
from tests.test_run_slice import (  # noqa: F401  (``seams`` is a fixture)
    PUSHED_REF,
    _Ctx,
    _devin_lane_submit,
    _finish,
    _job_tree,
    _poll_ctx,
    _pushed,
    _Store,
    _submit,
    catpath_build,
    seams,
)

#: The real sweep, captured before the ``seams`` fixture fakes it.
_REAL_SWEEP = _slice_ws.sweep

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs shell script scripts/test"
)

SNAPSHOT = "e" * 40


def _names(seams: dict[str, Any]) -> list[str]:
    return [name for name, _ in seams["ws"]]


def _recorded(job: int = 70, sha: str = "a" * 40, **kw: Any) -> dict[str, Any]:
    """``meta.build_ref`` as the worker stamped it after job ``job``."""
    return _pushed(job=job, sha=sha, **kw).as_meta() | {"host": "testhost"}


# ── start point ────────────────────────────────────────────────────


def test_first_attempt_starts_at_the_base_and_looks_for_legacy_work(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    assert handle is not None
    assert _names(seams)[-3:] == ["resolve_start", "import_legacy", "cut"]
    assert dict(seams["ws"])["cut"]["start"] is None
    assert ctx.meta_set["start"] is None and ctx.meta_set["base"] == "main"


def test_a_later_attempt_continues_from_the_recorded_pushed_work(
    seams: dict[str, Any],
) -> None:
    recorded = _recorded()
    store = _Store()
    store.add_todo(42, "t", meta={"build_ref": recorded})
    start = _slice_ws.Start(sha="a" * 40, ref=PUSHED_REF)
    seams["start"] = start
    ctx, handle = _submit(store, seams)
    assert handle is not None
    detail = dict(seams["ws"])
    # the hub is asked for the recorded ref, never the folder of an
    # earlier attempt; a recorded start is no reason to look for legacy work
    assert detail["resolve_start"] == {
        "todo": 42,
        "recorded": recorded,
        "floor": None,
    }
    assert "import_legacy" not in detail
    assert detail["cut"]["start"] == start
    assert ctx.meta_set["start"] == {"sha": "a" * 40, "ref": PUSHED_REF, "wip": False}
    brief = (_job_tree(seams["ws"][0][1]) / "scratch" / "brief.md").read_text(
        encoding="utf-8"
    )
    assert "continues earlier work" in brief and PUSHED_REF in brief
    assert "git reset --soft" not in brief


def test_a_snapshot_start_is_continued_and_never_routed_to_a_land(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unfinished work (``wip:`` on top) goes to a session with the reset
    hint in its brief, not to the land or the pre-land review."""
    store = _Store()
    store.add_todo(42, "t", meta={"build_ref": _recorded(wip=True)})
    seams["start"] = _slice_ws.Start(sha=SNAPSHOT, ref=PUSHED_REF, wip=True)
    seams["ahead"] = "aaa snapshot\n"
    seams["cherry"] = "+ aaa\n"
    monkeypatch.setattr(run_slice, "_carried_on_base", lambda *a, **k: None)
    ctx, handle = _submit(store, seams)
    assert handle is not None and ctx.failure is None
    assert seams["lands"] == [] and len(seams["launch"]) == 1
    tree = _job_tree(seams["ws"][0][1])
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "`git reset --soft HEAD~1` keeps them as uncommitted changes" in brief
    assert ctx.meta_set["start"]["wip"] is True


def test_start_over_deletes_the_builds_and_starts_at_the_base(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(
        42,
        "t",
        meta={"start_over": 77, "parked": None, "build_ref": _recorded()},
    )
    seams["start"] = _slice_ws.Start(sha="a" * 40, ref=PUSHED_REF)  # must not be read
    ctx, handle = _submit(store, seams)
    assert handle is not None and ctx.failure is None
    names = _names(seams)
    assert names.index("delete_builds") < names.index("cut")
    assert "resolve_start" not in names and "import_legacy" not in names
    assert dict(seams["ws"])["cut"]["start"] is None
    meta = store.refs[42].meta
    assert meta["build_ref"] is None  # cleared
    assert meta["start_over"] is None and meta["parked"] is None  # consumed


@pytest.mark.parametrize(
    "exc",
    [
        _slice_ws.HubUnreadable("the hub cannot be read; next: retry"),
        _slice_ws.RefMoved("the recorded ref moved; next: ask"),
    ],
    ids=["hub-unreadable", "ref-moved"],
)
def test_an_unreadable_hub_or_a_moved_ref_ends_the_job_attempt_free(
    seams: dict[str, Any], exc: Exception
) -> None:
    """Never a fallback to the base: the task's pushed work stays where it
    is and a later launch continues from it."""
    store = _Store()
    store.add_todo(42, "t", meta={"build_ref": _recorded()})
    seams["resolve_exc"] = exc
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert str(exc) in str(ctx.failure)
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == run_slice.WORKSPACE_KEY
    assert run_slice.WORKSPACE_KEY == "workspace"
    assert "cut" not in _names(seams) and seams["launch"] == []
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags and not any(t.startswith("attempt:") for t in tags)
    assert not any(t.startswith("claimed-by:") for t in tags)


def test_a_clone_that_cannot_be_cut_is_attempt_free_and_leaves_no_folder(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    seams["cut_exc"] = _slice_ws.WorkspaceError("git clone failed; next: look")
    ctx, handle = _submit(store, seams)
    assert handle is None
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    assert "attempt:1" not in store.tag_strings(42)
    assert seams["launch"] == []


def test_legacy_work_pushed_before_the_switch_is_imported_once(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    seams["legacy_tip"] = "9" * 40
    start = _slice_ws.Start(sha="9" * 40, ref="refs/factory/build/42/0")
    seams["legacy_start"] = start  # readable only once imported
    ctx, handle = _submit(store, seams)
    assert handle is not None
    names = _names(seams)
    assert names.count("resolve_start") == 2  # again, after the import
    assert dict(seams["ws"])["import_legacy"]["todo"] == 42
    assert dict(seams["ws"])["cut"]["start"] == start


# ── the same job runs submit again (an outage relaunch) ────────────


def test_a_relaunch_in_the_same_job_finalizes_drops_and_cuts_again(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """The folder of this very job is finalized with reason ``relaunch``
    (``meta.push`` and the task's ``meta.build_ref`` stamped), dropped, and
    the new clone starts from what the first session pushed."""
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    store = _Store()
    store.add_todo(42, "t")
    pushed = _pushed(sha=SNAPSHOT, head="b" * 40, wip=True, by="worker")
    seams["pushed_by_reason"]["relaunch"] = pushed
    # what resolve_start answers once the stamp is there
    seams["start"] = _slice_ws.Start(sha=SNAPSHOT, ref=pushed.ref, wip=True)
    ctx = _Ctx(
        store,
        {"todo_id": 42},
        start={"sha": "c" * 40, "ref": PUSHED_REF, "wip": False},
    )
    handle = run_slice.submit(ctx, run_slice.Relaunch({"own_folder": True}))
    assert handle is not None
    names = _names(seams)
    assert names.index("finalize") < names.index("drop") < names.index("cut")
    fin = dict(seams["ws"])["finalize"]
    assert fin["reason"] == "relaunch" and fin["start_sha"] == "c" * 40
    assert fin["job"] == 900 and fin["base"] == "main"
    # the new session's launch cleared the stored push: it judged the
    # earlier session's folder (gr741695), the task's record keeps the work
    assert ctx.meta_set["push"] is None
    assert store.refs[42].meta["build_ref"]["sha"] == SNAPSHOT
    assert dict(seams["ws"])["resolve_start"]["recorded"]["sha"] == SNAPSHOT
    assert dict(seams["ws"])["cut"]["start"].sha == SNAPSHOT
    assert ctx.meta_set["start"]["wip"] is True


def test_a_relaunch_whose_folder_cannot_be_dropped_fails_attempt_free(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    store = _Store()
    store.add_todo(42, "t")
    seams["pushed_by_reason"]["relaunch"] = _pushed(_slice_ws.WITHHELD, note="secret")
    ctx = _Ctx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, run_slice.Relaunch({"own_folder": True})) is None
    assert "cut" not in _names(seams) and "drop" not in _names(seams)
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    assert tree.exists()


# ── the run's end ──────────────────────────────────────────────────


def _poll_with(
    store: _Store,
    seams: dict[str, Any],
    ctx: _Ctx,
    handle: Any,
    *,
    code: int = 0,
    result: str = "done.\ntests: ran",
    is_error: bool = False,
) -> _Ctx:
    log = json.dumps({"is_error": is_error, "result": result}) + "\n"
    _finish(handle, code, log)
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    return pctx


def test_a_session_that_pushed_mints_a_land_with_ref_and_head_and_the_folder_goes(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    seams["pushed"] = _pushed(sha="d" * 40, by="builder")
    pctx = _poll_with(store, seams, ctx, handle)
    assert pctx.status == "succeeded"
    assert seams["land_kw"] == [{"ref": "refs/factory/build/42/900", "head": "d" * 40}]
    push = pctx.meta_set["push"]
    assert push["state"] == "pushed" and push["by"] == "builder"
    assert store.refs[42].meta["build_ref"] == push
    # finalize before anything reads the branch, the drop last
    names = _names(seams)
    assert names.index("finalize") < names.index("drop")
    fin = [d for n, d in seams["ws"] if n == "finalize"][-1]
    assert fin["reason"] == "session exited" and fin["start_sha"] is None
    assert fin["base"] == "main" and fin["tree"] == Path(handle["worktree"])
    assert not Path(handle["worktree"]).exists()
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "Push: pushed, by the builder" in summary


def test_a_session_that_exited_without_pushing_has_the_worker_push_for_it(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    seams["pushed"] = _pushed(sha="d" * 40, by="worker")
    pctx = _poll_with(store, seams, ctx, handle)
    assert pctx.status == "succeeded"
    assert pctx.meta_set["push"]["by"] == "worker"
    assert seams["land_kw"] == [{"ref": "refs/factory/build/42/900", "head": "d" * 40}]
    assert ("drop", Path(handle["worktree"])) in seams["ws"]


def test_a_session_killed_with_a_dirty_tree_fails_and_the_next_attempt_starts_from_the_snapshot(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["porcelain"] = " M a.py\n"
    seams["pushed"] = _pushed(sha=SNAPSHOT, head="b" * 40, wip=True, by="worker")
    pctx = _poll_with(store, seams, ctx, handle, code=137, result="", is_error=True)
    assert pctx.status == "failed"
    assert {"STATUS:open", "attempt:1"} <= store.tag_strings(42)
    assert seams["lands"] == []
    fin = [d for n, d in seams["ws"] if n == "finalize"][-1]
    assert fin["reason"].startswith("session failed")
    rec = store.refs[42].meta["build_ref"]
    assert rec["sha"] == SNAPSHOT and rec["wip"] is True
    assert ("drop", Path(handle["worktree"])) in seams["ws"]

    # the next attempt: resolve_start reads that record and the clone
    # starts at the snapshot, which its brief says
    seams["ws"].clear()
    seams["start"] = _slice_ws.Start(sha=SNAPSHOT, ref=rec["ref"], wip=True)
    ctx2 = _Ctx(store, {"todo_id": 42})
    ctx2.ref_id = 901
    assert run_slice.submit(ctx2, None) is not None
    assert dict(seams["ws"])["resolve_start"]["recorded"] == rec
    assert dict(seams["ws"])["cut"]["start"].sha == SNAPSHOT
    brief = (
        _job_tree(Path(handle["worktree"]).parents[2], 42, 901) / "scratch" / "brief.md"
    ).read_text(encoding="utf-8")
    assert "git reset --hard HEAD~1" in brief


def test_a_withheld_push_keeps_the_folder_and_says_where(
    seams: dict[str, Any],
) -> None:
    """The folder stays, the task's comment names host and folder, the job
    event says so; the outcome is the one it would have been."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["pushed"] = _pushed(_slice_ws.WITHHELD, note="secret-scan: 1 hit")
    seams["pushed"] = _slice_ws.Pushed(
        state=_slice_ws.WITHHELD, ref="refs/factory/build/42/900", note="secret-scan"
    )
    pctx = _poll_with(store, seams, ctx, handle, code=1, result="boom", is_error=True)
    tree = Path(handle["worktree"])
    assert pctx.status == "failed"  # exactly what a failed run is
    assert {"STATUS:open", "attempt:1"} <= store.tag_strings(42)
    assert "drop" not in _names(seams) and tree.is_dir()
    kept = [c for c in store.comments(42) if "was kept" in c]
    assert len(kept) == 1 and str(tree) in kept[0] and _slice_park_node() in kept[0]
    assert any(k == "job_event" and "was kept" in t for k, t in pctx.chunks)
    assert "build_ref" not in store.refs[42].meta  # nothing the hub holds
    assert pctx.meta_set["push"]["state"] == "withheld"


def test_an_empty_run_that_continued_complete_pushed_work_lands_that_work(
    seams: dict[str, Any],
) -> None:
    """The session added nothing (finalize: ``empty``): the land is for the
    pushed attempt it started from."""
    store = _Store()
    store.add_todo(42, "t", meta={"build_ref": _recorded()})
    seams["start"] = _slice_ws.Start(sha="a" * 40, ref=PUSHED_REF)
    seams["ahead"] = "aaa fix: the earlier work\n"
    seams["cherry"] = ""
    ctx, handle = _submit(store, seams)
    assert handle is not None
    seams["pushed"] = _pushed(_slice_ws.EMPTY)
    pctx = _poll_with(store, seams, ctx, handle)
    assert pctx.status == "succeeded"
    assert seams["land_kw"] == [{"ref": PUSHED_REF, "head": "a" * 40}]
    assert ("drop", Path(handle["worktree"])) in seams["ws"]
    [fin] = [d for n, d in seams["ws"] if n == "finalize"]
    assert fin["start_sha"] == "a" * 40


def test_a_clean_run_the_hub_does_not_hold_lands_nothing(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    seams["pushed"] = _slice_ws.Pushed(
        state=_slice_ws.FAILED, ref="refs/factory/build/42/900", note="push refused"
    )
    pctx = _poll_with(store, seams, ctx, handle)
    assert pctx.status == "failed" and seams["lands"] == []
    assert "hub holds no pushed work" in str(pctx.failure)
    assert Path(handle["worktree"]).is_dir()


# ── stops (gr741695) ───────────────────────────────────────────────


def _release_ctx(store: _Store, **meta: Any) -> _Ctx:
    ctx = _Ctx(store, {"todo_id": 42}, lane="claude-sub", **meta)
    return ctx


def test_release_touches_no_folder_and_the_next_tidy_saves_the_work(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``release`` also runs from the executor's claim step, outside the watch
    pass: it writes nothing in a folder, the hub or the job's push record.
    The sweep (``tidy``) then finalizes the folder — a ``wip:`` snapshot of
    the uncommitted work — stamps the record and the task's ``build_ref``,
    and drops it."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    seams["alive"] = False  # the group is gone
    seams["pushed"] = _pushed(sha=SNAPSHOT, head="b" * 40, wip=True, by="worker")
    start = {"sha": "c" * 40, "ref": PUSHED_REF, "wip": False}
    rctx = _release_ctx(store, start=start, base="release/r9")
    run_slice.release(rctx, handle)
    assert {"finalize", "drop"}.isdisjoint(_names(seams)) and tree.is_dir()
    assert "push" not in rctx.meta_set
    assert "build_ref" not in store.refs[42].meta
    assert not any("was kept" in c for c in store.comments(42))
    # the next sweep: the job is terminal, nothing answers
    old = "2020-01-01T00:00:00+00:00"
    _settle_row(
        monkeypatch,
        "cancelled",
        {
            "compute_handle": dict(handle),
            "start": start,
            "base": "release/r9",
            "finished_at": old,
        },
    )
    stamped = _record_stamps(store, monkeypatch)
    monkeypatch.setattr(_slice_ws, "sweep", _REAL_SWEEP)
    run_slice.tidy(store, None)
    fin = [d for n, d in seams["ws"] if n == "finalize"][-1]
    assert fin["tree"] == tree and fin["reason"] == "run ended"
    assert fin["start_sha"] == "c" * 40 and fin["base"] == "release/r9"
    assert ("drop", tree) in seams["ws"] and not tree.exists()
    assert [rid for rid, _ in stamped] == [900, 42]
    assert stamped[0][1]["push"]["wip"] is True
    assert stamped[1][1]["build_ref"]["sha"] == SNAPSHOT


def test_release_leaves_a_live_group_and_a_judged_folder_alone(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    kept = {
        **_pushed(_slice_ws.WITHHELD).as_meta(),
        "launch": run_slice._launch_key(handle),
    }
    for alive, push in ((True, None), (False, kept)):
        seams["alive"] = alive
        kw = {} if push is None else {"push": push}
        run_slice.release(_release_ctx(store, **kw), handle)
        assert {"finalize", "drop"}.isdisjoint(_names(seams))
        assert Path(handle["worktree"]).is_dir()
    assert not any("was kept" in c for c in store.comments(42))


def _settle_row(
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
    meta: dict[str, Any] | None,
) -> None:
    """The job row the sweep reads: a job of td42 unless ``meta`` says
    otherwise (the sweep judges a folder only by a row of its own task)."""
    row = {"params": {"todo_id": 42}, **(meta or {})}
    monkeypatch.setattr(
        run_slice,
        "_job_row",
        lambda store, job_id: None if status is None else (status, row),
    )


def _settle(
    store: Any, tree: Path, role: str = "build", job: int = 70
) -> _slice_ws.Pushed | None:
    return run_slice._settle(store, tree.parent, tree, 42, job, role)


def test_the_host_sweep_leaves_a_running_job_or_an_unreadable_row_alone(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42, 70)
    _settle_row(monkeypatch, "running", {})
    assert _settle(_Store(), tree) is None
    _settle_row(monkeypatch, None, None)
    assert _settle(_Store(), tree) is None
    assert "finalize" not in _names(seams)


def test_the_host_sweep_kills_a_group_once_the_job_has_been_terminal_for_30s(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import signal
    from datetime import UTC, datetime, timedelta

    tree = _job_tree(tmp_path, 42, 70)
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    handle = {"pid": 4242, "pgid": 4242, "boot_id": "boot", "start_time": 55}
    fresh = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    _settle_row(monkeypatch, "failed", {"compute_handle": handle, "finished_at": fresh})
    assert _settle(_Store(), tree) is None
    assert seams["signals"] == []  # too soon
    old = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    _settle_row(monkeypatch, "failed", {"compute_handle": handle, "finished_at": old})
    assert _settle(_Store(), tree) is None  # still not judged this pass
    assert seams["signals"] == [(4242, signal.SIGKILL)]
    assert "finalize" not in _names(seams)


@pytest.mark.parametrize(
    ("handle", "unreadable", "readable"),
    [
        (
            {"pid": 4242, "pgid": 4242, "boot_id": "boot", "start_time": 55},
            {"identity": None},
            {"identity": ("boot", 55)},
        ),
        (
            {"pid": 4242, "pgid": 4242, "ps_identity": "x"},
            {"ps_identity": None},
            {"ps_identity": "x"},
        ),
    ],
    ids=["linux", "darwin"],
)
def test_the_host_sweep_sends_no_signal_in_a_pass_that_cannot_identify_the_leader(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handle: dict[str, Any],
    unreadable: dict[str, Any],
    readable: dict[str, Any],
) -> None:
    """The leader's pid answers but its identity cannot be read (``ps`` timed
    out): the folder is held and nothing is signalled, because the number may
    be another process's by now. The pass that reads the identity signals."""
    import signal
    from datetime import UTC, datetime, timedelta

    tree = _job_tree(tmp_path, 42, 70)
    seams["alive"] = True
    seams.update(unreadable)
    old = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    _settle_row(monkeypatch, "failed", {"compute_handle": handle, "finished_at": old})
    assert _settle(_Store(), tree) is None
    assert seams["signals"] == [] and "finalize" not in _names(seams)
    seams.update(readable)
    assert _settle(_Store(), tree) is None
    assert seams["signals"] == [(4242, signal.SIGKILL)]
    assert "finalize" not in _names(seams)


@pytest.mark.parametrize("params", [{"todo_id": 7}, {}, None])
def test_the_sweep_does_not_judge_a_folder_by_another_tasks_job_row(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    params: dict[str, Any] | None,
) -> None:
    """A folder is judged only by a job row of its own task. A worker reading
    a different database than the one the folder was cut under finds some
    other job under the same id; going by that row it would kill a process
    group and delete a folder that are not its own."""
    from datetime import UTC, datetime, timedelta

    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    handle = {"pid": 4242, "pgid": 4242, "boot_id": "boot", "start_time": 55}
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    store = _Store()
    store.add_todo(42, "t")
    for role in ("build", "land"):
        _settle_row(
            monkeypatch,
            "failed",
            {"params": params, "compute_handle": handle, "finished_at": old},
        )
        assert _settle(store, tree, role=role) is None
    assert seams["signals"] == [] and "finalize" not in _names(seams)
    assert tree.is_dir() and store.comments(42) == []


def test_the_host_sweep_judges_a_land_folder_empty_once_its_job_ended(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _slice_ws.attempt_dir(tmp_path, 42, 70, "land")
    _settle_row(monkeypatch, "succeeded", {"compute_handle": {"pid": 1, "pgid": 1}})
    seams["alive"] = False
    got = _settle(_Store(), tree, role="land")
    assert got is not None and got.state == _slice_ws.EMPTY and got.safe
    assert got.ref == "refs/factory/build/42/70"
    assert "finalize" not in _names(seams)


_HANDLE = {"pid": 4242, "pgid": 4242, "started_at": "2026-11-20T00:00:00Z"}


def test_the_host_sweep_returns_the_stored_push_when_the_poll_made_one(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    seams["alive"] = False
    fresh = _pushed(job=70, sha=SNAPSHOT, by="builder")  # what a finalize finds
    seams["pushed"] = fresh
    stored = _pushed(job=70, sha=SNAPSHOT, wip=True, by="worker").as_meta()
    stored["launch"] = run_slice._launch_key(_HANDLE)
    _settle_row(monkeypatch, "failed", {"push": stored, "compute_handle": _HANDLE})
    got = _settle(_Store(), tree)
    # confirmed by a finalize (a stored safe record is no licence to delete),
    # and the stored record stands as it is: its ``by`` and ``wip`` are kept
    assert _finalizes(seams) == ["run ended"]
    assert got is not None and got.state == "pushed" and got.sha == SNAPSHOT
    assert got.wip is True and got.by == "worker"
    # a withheld record is returned as stored: no finalize, nothing to confirm
    seams["ws"].clear()
    stored_bad = _pushed(_slice_ws.WITHHELD, job=70).as_meta()
    stored_bad["launch"] = run_slice._launch_key(_HANDLE)
    _settle_row(monkeypatch, "failed", {"push": stored_bad, "compute_handle": _HANDLE})
    got = _settle(_Store(), tree)
    assert got is not None and not got.safe and "finalize" not in _names(seams)


def test_the_host_sweep_finalizes_a_dead_job_that_never_got_a_poll(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    seams["alive"] = False
    seams["pushed"] = _pushed(job=70, sha=SNAPSHOT, wip=True, by="worker")
    _settle_row(
        monkeypatch,
        "timed-out",
        {
            "compute_handle": {"pid": 1, "pgid": 1},
            "start": {"sha": "c" * 40, "ref": PUSHED_REF, "wip": False},
            "base": "main",
        },
    )
    stamped: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(
        store,
        "stamp_ref_meta",
        lambda rid, upd, **kw: stamped.append((rid, upd)),
        raising=False,
    )
    got = _settle(store, tree)
    assert got is not None and got.state == "pushed"
    fin = [d for n, d in seams["ws"] if n == "finalize"][-1]
    assert fin["reason"] == "run ended" and fin["start_sha"] == "c" * 40
    assert fin["job"] == 70 and fin["base"] == "main"
    assert [rid for rid, _ in stamped] == [70, 42]
    assert stamped[0][1]["push"]["sha"] == SNAPSHOT
    assert stamped[1][1]["build_ref"]["sha"] == SNAPSHOT


@pytest.fixture(autouse=True)
def _fresh_tidy_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_slice, "_tidy_last", None)
    monkeypatch.setattr(run_slice, "_tidy_turn", 0)


def _builds_in(*roots: Path) -> None:
    """Mark each checkout as one this host builds in: its folders directory
    exists (``tidy`` passes over a checkout with neither that nor a hub)."""
    for root in roots:
        run_slice.worktrees_dir(root).mkdir(parents=True, exist_ok=True)


def _record_stamps(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[int, dict[str, Any]]]:
    stamped: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(
        store,
        "stamp_ref_meta",
        lambda rid, upd, **kw: stamped.append((rid, upd)),
        raising=False,
    )
    return stamped


def test_submit_does_not_sweep_and_never_touches_another_tasks_folder(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launch cuts its own clone and nothing else: another task's ended
    folder (its job has no push record) is left exactly as it is — the host
    sweep settles it."""
    other = _job_tree(tmp_path, 7, 3)
    other.mkdir(parents=True)
    (other / "work.txt").write_text("unsaved\n", encoding="utf-8")
    _settle_row(monkeypatch, "failed", {"compute_handle": dict(_HANDLE)})
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    assert handle is not None and ctx.failure is None
    assert "sweep" not in _names(seams)
    assert {"finalize", "drop"}.isdisjoint(_names(seams))
    assert (other / "work.txt").read_text(encoding="utf-8") == "unsaved\n"


def test_tidy_sweeps_every_checkout_this_host_builds_in_and_reports_what_it_keeps(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    second = tmp_path / "second"
    monkeypatch.setenv(run_slice.SLICE_REPOS_ENV, json.dumps({"catpath": str(second)}))
    _builds_in(tmp_path, second)
    seams["sweep_notes"] = ["kept slice-td7-j3: withheld: secret-scan"]
    store = _Store()
    store.add_todo(7, "t")
    run_slice.tidy(store, "node-a")
    sweeps = [d for n, d in seams["ws"] if n == "sweep"]
    assert [d["root"] for d in sweeps] == [tmp_path, second]
    assert all(callable(d["settle"]) and d["keep"] is None for d in sweeps)
    # the task's comment and the alert, once per folder and state
    # the fake sweep reports the same note for both checkouts: one comment each
    kept = [c for c in store.comments(7) if "was kept" in c]
    assert len(kept) == 2
    assert all("the hub does not hold its work (withheld" in c for c in kept)
    for root in (tmp_path, second):
        folder = run_slice.worktrees_dir(root) / "slice-td7-j3"
        assert sum(str(folder) in c for c in kept) == 1
    assert {a["fingerprint"] for a in _alerts(seams, "raise")} == {"slice-td7-j3"}
    [stale, _stale2] = _alerts(seams, "stale")
    assert stale["source"] == f"run_slice:kept:{_slice_park_node()}"


def test_tidy_passes_over_a_checkout_that_never_built(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every worker that runs the executor calls the hook, also on a host
    that builds nothing. There it must cost no sweep and no database call."""
    second = tmp_path / "second"
    monkeypatch.setenv(run_slice.SLICE_REPOS_ENV, json.dumps({"catpath": str(second)}))
    run_slice.tidy(_Store(), None)
    assert "sweep" not in [n for n, _ in seams["ws"]]
    assert _alerts(seams, "stale") == [] and _alerts(seams, "raise") == []


def test_tidy_runs_at_most_once_in_thirty_seconds(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _builds_in(tmp_path)
    store = _Store()
    run_slice.tidy(store, None)
    run_slice.tidy(store, None)
    assert [n for n, _ in seams["ws"]].count("sweep") == 1
    monkeypatch.setattr(
        run_slice, "_tidy_last", run_slice.time.monotonic() - run_slice.TIDY_EVERY_S - 1
    )
    run_slice.tidy(store, None)
    assert [n for n, _ in seams["ws"]].count("sweep") == 2


def test_tidy_survives_a_failing_sweep(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    second = tmp_path / "second"
    monkeypatch.setenv(run_slice.SLICE_REPOS_ENV, json.dumps({"catpath": str(second)}))
    _builds_in(tmp_path, second)
    calls: list[Path] = []

    def boom(root: Path, settle: Any, **kw: Any) -> list[str]:
        calls.append(root)
        raise RuntimeError("disk gone")

    monkeypatch.setattr(_slice_ws, "sweep", boom)
    run_slice.tidy(_Store(), None)
    assert calls == [tmp_path, second]


# ── floor, legacy import (gr741695 round 2) ────────────────────────


def test_the_floor_goes_to_resolve_start_and_blocks_the_legacy_import(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t", meta={"build_floor": 880})
    ctx, handle = _submit(store, seams)
    assert handle is not None
    detail = dict(seams["ws"])
    assert detail["resolve_start"]["floor"] == 880
    # a legacy ref of a task that has a floor is stale: no import attempted
    assert "import_legacy" not in detail


def test_a_start_over_stamps_the_floor_with_this_job_after_delete_builds(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(
        42, "t", meta={"start_over": 77, "build_ref": _recorded(), "build_floor": 5}
    )
    ctx, handle = _submit(store, seams)
    assert handle is not None
    names = _names(seams)
    assert names.index("delete_builds") < names.index("cut")
    meta = store.refs[42].meta
    assert meta["build_ref"] is None
    assert meta["build_floor"] == 900  # this job's id
    assert "resolve_start" not in names


def test_a_failed_legacy_import_ends_the_job_attempt_free(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``import_legacy`` raises ``HubUnreadable`` when GitHub holds the legacy
    ref and the import fails: never a start from the base."""
    store = _Store()
    store.add_todo(42, "t")

    def boom(hub: Path, url: str, todo: int, *, env: Any = None) -> str | None:
        raise _slice_ws.HubUnreadable(
            "the legacy ref could not be imported; next: retry"
        )

    monkeypatch.setattr(_slice_ws, "import_legacy", boom)
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    assert "legacy ref could not be imported" in str(ctx.failure)
    assert "cut" not in _names(seams) and seams["launch"] == []
    assert "attempt:1" not in store.tag_strings(42)


def test_there_is_exactly_one_finalize_call_site() -> None:
    """The edit that adds ``branch=`` is one line: every path (poll, stop,
    relaunch, host sweep) goes through ``_finalize_folder``."""
    import inspect

    src = inspect.getsource(run_slice)
    assert src.count("_slice_ws.finalize(") == 1
    assert "return _slice_ws.finalize(" in inspect.getsource(run_slice._finalize_folder)


# ── conflict-only attempt ──────────────────────────────────────────

_CONFLICT = {
    "head": "a" * 40,
    "ref": PUSHED_REF,
    "base_tip": "b" * 40,
    "files": ["src/precis/x.py", "tests/test_x.py"],
    "try": 1,
}


def _conflict_brief(**over: Any) -> str:
    kw: dict[str, Any] = dict(
        todo_id=42,
        title="Build the widget",
        body="THE-TASK-BODY",
        latest=None,
        lane="claude-sub",
        model="m",
        effort="high",
        job_id=77,
        start={"sha": "a" * 40, "ref": PUSHED_REF, "wip": False},
        conflict=dict(_CONFLICT),
    )
    kw.update(over)
    return run_slice.render_brief(**kw)


def test_params_schema_admits_the_conflict_and_resolution_objects() -> None:
    props = run_slice.PARAMS_SCHEMA["properties"]
    assert props["conflict"] == {"type": "object"}
    assert props["resolution"] == {"type": "object"}
    assert run_slice.PARAMS_SCHEMA["additionalProperties"] is False


def test_the_conflict_brief_is_the_rebase_brief_not_the_build_brief() -> None:
    brief = _conflict_brief()
    assert "## Conflict-only attempt (try 1 of 3)" in brief
    assert "failed to land because `origin/main` moved" in brief
    assert "`git fetch origin main`, then `git rebase origin/main`" in brief
    assert "- `src/precis/x.py`" in brief and "- `tests/test_x.py`" in brief
    assert (
        "A plain rebase: keep the commits as they are. No squash, no reordering"
        in brief
    )
    assert "keep the base's side" in brief
    assert "git push origin +HEAD:refs/factory/build/42/77" in brief
    assert "git ls-remote origin refs/factory/build/42/77" in brief
    assert "`wip: snapshot — <why>`" in brief
    assert "continues earlier work" in brief  # it starts at the branch tip
    # the checks are the land's, numbered; the tests for the touched files
    for step in run_slice._LAND_LINT_STEPS:
        assert f"`{step}`" in brief
    assert "scripts/test <paths>" in brief
    # not the task's build brief
    assert "THE-TASK-BODY" not in brief and "## Brief" not in brief
    assert "Commit on `slice/42`" not in brief
    assert "earlier tries" not in brief
    assert "tests: ran" in brief and "`handoff:` comment" in brief
    assert "never run `scripts/round`" in brief
    assert "Do not push" not in brief


@pytest.mark.parametrize(
    ("n", "phrase"),
    [
        (2, "This is try 2: the earlier try at this conflict failed"),
        (3, "This is try 3: the 2 earlier tries at this conflict failed"),
    ],
)
def test_the_conflict_brief_names_the_try(n: int, phrase: str) -> None:
    brief = _conflict_brief(conflict={**_CONFLICT, "try": n})
    assert f"(try {n} of 3)" in brief and phrase in brief


def test_the_conflict_brief_of_a_second_repository_and_a_lane_that_cannot_push() -> (
    None
):
    kw: dict[str, Any] = dict(
        todo_id=42,
        title="t",
        body="b",
        latest=None,
        lane="l",
        model="m",
        effort="high",
        job_id=77,
        conflict=dict(_CONFLICT),
        start={"sha": "a" * 40, "ref": PUSHED_REF, "wip": False},
    )
    cat = run_slice.render_brief(**kw, profile=lanes.REPO_PROFILES["catpath"])
    assert "Conflict-only attempt" in cat and "`catpath` repository" in cat
    assert "git push origin +HEAD:refs/factory/build/42/77" in cat
    assert "scripts/test" not in cat and "uv run ruff check src tests" in cat
    codex = _conflict_brief(adapter="codex_exec", mcp=False)
    assert "the worker pushes it after you exit" in codex
    assert "git push" not in codex


def test_a_conflict_job_always_runs_a_session_and_keeps_its_brief(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complete pushed work with no steer routes straight to its land — not
    when the job is a conflict-only attempt."""
    store = _Store()
    store.add_todo(42, "t", body="Do X.", meta={"build_ref": _recorded()})
    seams["start"] = _slice_ws.Start(sha="a" * 40, ref=PUSHED_REF)
    seams["ahead"] = "aaa fix: the work\n"
    seams["cherry"] = "+ aaa\n"
    monkeypatch.setattr(run_slice, "_carried_on_base", lambda *a, **k: None)
    ctx, handle = _submit(store, seams, conflict=dict(_CONFLICT))
    assert handle is not None and ctx.failure is None
    assert seams["lands"] == [] and len(seams["launch"]) == 1
    tree = _job_tree(seams["ws"][0][1])
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "Conflict-only attempt (try 1 of 3)" in brief
    # the same job without the param is routed to its land (the sweep has
    # dropped the first launch's folder)
    shutil.rmtree(tree)
    seams["ws"].clear()
    ctx2, handle2 = _submit(store, seams)
    assert handle2 is None
    assert ctx2.meta_set[run_slice.ATTEMPT_FREE_KEY] == "branch-ahead"


def test_a_conflict_attempt_carries_push_in_its_job_meta(
    seams: dict[str, Any],
) -> None:
    """The tick judges the try by ``push.state == "pushed"``."""
    store = _Store()
    store.add_todo(42, "t", body="Do X.", meta={"build_ref": _recorded()})
    seams["start"] = _slice_ws.Start(sha="a" * 40, ref=PUSHED_REF)
    ctx, handle = _submit(store, seams, conflict=dict(_CONFLICT))
    seams["ahead"] = "bbb fix: the work, rebased\n"
    seams["pushed"] = _pushed(sha="d" * 40, by="builder")
    pctx = _poll_with(store, seams, ctx, handle)
    assert pctx.meta_set["push"]["state"] == "pushed"


# ── resolution-only review ─────────────────────────────────────────

_OF = "7" * 40
_RES_REF = "refs/factory/build/7/70"


def _resolution_store() -> _Store:
    store = _Store()
    store.add_todo(
        7,
        "Build it",
        body="Build.",
        meta={
            "pending_land": {
                "todo_id": 7,
                "branch": "slice/7",
                "ref": "refs/factory/build/7/71",
                "head": "9" * 40,
            }
        },
    )
    return store


def test_a_resolution_review_reads_the_range_diff_of_the_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_git(args: list[str], cwd: Path) -> Any:
        calls.append(args)
        if args[:2] == ["rev-parse", "--verify"]:
            return _git_proc("9" * 40 + "\n")
        return _git_proc("")

    monkeypatch.setattr(run_slice, "_git", fake_git)
    target = run_slice._review_target(
        _resolution_store(),
        {"generate": {"role": "reviewer", "from": "td7"}},
        tree=tmp_path,
        resolution={"of": _OF, "ref": _RES_REF},
    )
    assert target is not None
    # the reviewed head is made present: fetched beside the resolution's own
    # tracking ref, then read back
    fetches = [a[-1] for a in calls if a[:1] == ["fetch"]]
    assert f"+{_RES_REF}:refs/remotes/origin/resolution-of/slice/7" in fetches
    assert ["cat-file", "-e", f"{_OF}^{{commit}}"] in calls
    assert f"git range-diff origin/main {_OF} origin/slice/7" in target
    assert "git diff origin/main...origin/slice/7" not in target
    assert "commits marked `=` are unchanged and already reviewed" in target
    assert "rewrote more than the conflict needed is a FIX" in target
    # the verdict contract is unchanged
    assert "verified head <sha>" in target and "`anchors:`" in target


def test_a_resolution_review_refuses_attempt_free_when_the_hub_lost_the_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_git(args: list[str], cwd: Path) -> Any:
        if args[:1] == ["fetch"] and "resolution-of" in args[-1]:
            return subprocess.CompletedProcess(
                [], 128, stdout="", stderr="fatal: couldn't find remote ref\n"
            )
        if args[:2] == ["rev-parse", "--verify"]:
            return _git_proc("9" * 40 + "\n")
        return _git_proc("")

    monkeypatch.setattr(run_slice, "_git", fake_git)
    with pytest.raises(run_slice._ReviewRefusal, match="next:"):
        run_slice._review_target(
            _resolution_store(),
            {"generate": {"role": "reviewer", "from": "td7"}},
            tree=tmp_path,
            resolution={"of": _OF, "ref": _RES_REF},
        )


def test_the_reviewer_job_submit_passes_its_resolution_and_a_refusal_is_attempt_free(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _resolution_store()
    store.add_todo(
        55,
        "review td7",
        meta={"generate": {"role": "reviewer", "from": "td7"}},
    )
    seen: list[Any] = []

    def target(store: Any, meta: Any, **kw: Any) -> str:
        seen.append(kw["resolution"])
        raise run_slice._ReviewRefusal("the hub no longer holds it; next: retry")

    monkeypatch.setattr(run_slice, "_review_target", target)
    ctx = _Ctx(
        store,
        {"todo_id": 55, "resolution": {"of": _OF, "ref": _RES_REF}},
    )
    assert run_slice.submit(ctx, None) is None
    assert seen == [{"of": _OF, "ref": _RES_REF}]
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == run_slice.REVIEW_WAIT_KEY
    assert "attempt:1" not in store.tag_strings(55)


def _git_proc(out: str, rc: int = 0) -> Any:
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


# ── kept folders alert ─────────────────────────────────────────────


def _alerts(seams: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [d for k, d in seams["alerts"] if k == kind]


def test_a_withheld_push_raises_one_alert_naming_host_and_folder_in_its_body(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["pushed"] = _slice_ws.Pushed(
        state=_slice_ws.WITHHELD, ref="refs/factory/build/42/900", note="secret-scan"
    )
    _poll_with(store, seams, ctx, handle, code=1, result="boom", is_error=True)
    [raised] = _alerts(seams, "raise")
    tree = Path(handle["worktree"])
    assert raised["source"] == f"run_slice:kept:{_slice_park_node()}"
    assert raised["fingerprint"] == tree.name  # one alert per folder
    assert str(tree) in raised["detail"] and _slice_park_node() in raised["detail"]
    assert raised["severity"] == "warn"
    from precis import alert_ids

    assert alert_ids.rule_id(raised["source"], raised["fingerprint"]) is not None


def test_a_failed_finalize_raises_the_alert_and_a_dropped_folder_does_not(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    _poll_with(store, seams, ctx, handle)  # pushed, dropped
    assert _alerts(seams, "raise") == []
    seams["ws"].clear()
    ctx2, handle2 = _submit(store, seams)
    seams["pushed"] = _slice_ws.Pushed(
        state=_slice_ws.FAILED, ref="refs/factory/build/42/900", note="push refused"
    )
    _poll_with(store, seams, ctx2, handle2, code=1, result="boom", is_error=True)
    assert len(_alerts(seams, "raise")) == 1


def _slice_park_node() -> str:
    from precis.workers.job_types import _slice_park

    return _slice_park.node_name()


# ── review fixes (gr741695 round 3) ────────────────────────────────


def test_a_stored_push_of_an_earlier_launch_is_not_trusted_by_the_sweep(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outage relaunch settles the first session's folder and stamps its
    push; the second session's folder, killed later, must be finalized by
    the sweep — not read as "the poll already judged it"."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    seams["alive"] = False
    seams["pushed"] = _pushed(sha=SNAPSHOT, head="b" * 40, wip=True, by="worker")
    _record_stamps(store, monkeypatch)
    stale = {
        **_pushed(_slice_ws.EMPTY).as_meta(),
        "launch": "999@2026-11-19T00:00:00Z",  # the first session's launch
    }
    _settle_row(monkeypatch, "failed", {"push": stale, "compute_handle": dict(handle)})
    assert run_slice._settle(store, tmp_path_of(handle), tree, 42, 900, "build")
    assert "finalize" in _names(seams)
    # a push with no launch key (a relaunch settle's) is never trusted either
    seams["ws"].clear()
    nokey = _pushed(_slice_ws.EMPTY).as_meta()
    _settle_row(monkeypatch, "failed", {"push": nokey, "compute_handle": dict(handle)})
    assert run_slice._settle(store, tmp_path_of(handle), tree, 42, 900, "build")
    assert "finalize" in _names(seams)


def tmp_path_of(handle: dict[str, Any]) -> Path:
    """The checkout root of a handle's folder (``<root>/.claude/worktrees/x``)."""
    return Path(handle["worktree"]).parents[2]


def test_the_host_sweep_finalizes_when_the_stored_push_is_of_an_earlier_launch(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    seams["alive"] = False
    seams["pushed"] = _pushed(job=70, sha=SNAPSHOT, wip=True, by="worker")
    stale = {**_pushed(_slice_ws.EMPTY, job=70).as_meta(), "launch": "1@old"}
    _settle_row(monkeypatch, "failed", {"push": stale, "compute_handle": dict(_HANDLE)})
    monkeypatch.setattr(store, "stamp_ref_meta", lambda *a, **k: None, raising=False)
    got = _settle(store, tree)
    assert got is not None and got.sha == SNAPSHOT
    assert "finalize" in _names(seams)


def test_a_recycled_pid_is_not_the_session(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = {"pid": 4242, "pgid": 4242, "boot_id": "boot", "start_time": 55}
    seams["alive"] = True
    seams["group_alive"] = False
    monkeypatch.setattr(run_slice, "_process_identity", lambda pid: ("boot", 56))
    assert run_slice._session_alive(handle) is False
    monkeypatch.setattr(run_slice, "_process_identity", lambda pid: ("boot", 55))
    assert run_slice._session_alive(handle) is True
    monkeypatch.setattr(run_slice, "_process_identity", lambda pid: ("other", 55))
    assert run_slice._session_alive(handle) is False


def test_the_host_sweep_never_signals_a_recycled_pid_and_does_not_keep_the_folder(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    store = _Store()
    store.add_todo(42, "t")
    seams["alive"] = True  # the pid answers — it is someone else's now
    seams["group_alive"] = False
    seams["pushed"] = _pushed(job=70, sha=SNAPSHOT, by="worker")
    monkeypatch.setattr(run_slice, "_process_identity", lambda pid: ("boot", 99))
    monkeypatch.setattr(store, "stamp_ref_meta", lambda *a, **k: None, raising=False)
    _settle_row(
        monkeypatch,
        "failed",
        {
            "compute_handle": {
                "pid": 4242,
                "pgid": 4242,
                "boot_id": "boot",
                "start_time": 55,
                "started_at": "2026-11-20T00:00:00Z",
            },
            "finished_at": "2020-01-01T00:00:00+00:00",
        },
    )
    got = _settle(store, tree)
    assert got is not None  # judged, so the sweep can drop it
    assert seams["signals"] == []
    assert "finalize" in _names(seams)


def _worker_commit_inputs(tree: Path) -> None:
    """What makes the poll commit for a session: a message and staged work."""
    (tree / "scratch").mkdir(exist_ok=True)
    (tree / "scratch" / "commit-msg.txt").write_text(
        "fix(x): y (td42)\n", encoding="utf-8"
    )


def _git_writes(seams: dict[str, Any]) -> list[list[str]]:
    return [a for a, _cwd in seams["git"] if a[:1] in (["add"], ["commit"])]


def test_the_polls_finalize_record_and_drop_run_in_that_order_and_create_no_lock_file(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    _worker_commit_inputs(tree)
    seams["staged"] = True
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    seams["alive"] = False
    pctx = _poll_with(store, seams, ctx, handle)
    assert _git_writes(seams) != []
    names = _names(seams)
    assert names.index("finalize") < names.index("drop")
    assert pctx.meta_set["push"]["state"] == "pushed"
    assert list(tree.parent.glob("*.lock")) == []  # nothing beside the folder


def test_the_poll_writes_nothing_for_a_job_that_is_already_terminal(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    _worker_commit_inputs(tree)
    seams["staged"] = True
    seams["alive"] = False
    seams["job_row"] = ("cancelled", {})
    _finish(handle, 0, json.dumps({"is_error": False, "result": "done.\ntests: ran"}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert _git_writes(seams) == [] and "finalize" not in _names(seams)
    assert seams["lands"] == [] and pctx.status is None and tree.is_dir()


def test_the_poll_adopts_the_record_its_own_earlier_pass_wrote(
    seams: dict[str, Any],
) -> None:
    """A crash between the record and the land: the next pass finds the
    record of its own launch (the sweep only settles terminal jobs, so nobody
    else wrote it), judges nothing again and mints the land with that
    record's head."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    seams["alive"] = False
    mine = {
        **_pushed(sha="d" * 40, by="worker").as_meta(),
        "launch": run_slice._launch_key(handle),
    }
    seams["job_row"] = ("running", {"push": mine})
    pctx = _poll_with(store, seams, ctx, handle)
    # the pass drops the folder, so it confirms the record first: the same sha
    # leaves the stored record as it is (nothing re-stamped)
    assert _finalizes(seams) == ["session exited"]
    assert "push" not in pctx.meta_set
    assert seams["land_kw"] == [{"ref": "refs/factory/build/42/900", "head": "d" * 40}]
    assert pctx.status == "succeeded"


def test_a_withheld_relaunch_is_reported_once_across_three_sweeps(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The relaunch could not save the folder and ended attempt-free: its own
    record is on the job (no launch key — the handle is the parked one, with
    no pid) and the folder stays. The sweep takes that record as it is: no new
    snapshot, no new push, no new stamp, one comment, however often it runs."""
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    _builds_in(tmp_path)
    store = _Store()
    store.add_todo(42, "t")
    # Each settle would build its own snapshot, and the hook's refusal names
    # that commit: the notes differ between settles.
    seams["pushed"] = _slice_ws.Pushed(
        state=_slice_ws.WITHHELD,
        ref="refs/factory/build/42/900",
        note="secret-scan: commit bbbbbbbbbbbb holds it",
    )
    seams["pushed_by_reason"]["relaunch"] = _slice_ws.Pushed(
        state=_slice_ws.WITHHELD,
        ref="refs/factory/build/42/900",
        note="secret-scan: commit aaaaaaaaaaaa holds it",
    )
    parked = {
        "worktree": str(tree),
        "own_folder": True,
        "not_before": "2000-01-01T00:00:00Z",
        "outage": "quota",
        "outage_parks": 1,
    }
    ctx = _Ctx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, run_slice.Relaunch(dict(parked))) is None
    rows = _Rows(monkeypatch, store)
    rows.set(900, "failed", compute_handle=dict(parked), push=ctx.meta_set["push"])
    seams["alive"] = False
    for _ in range(3):
        _sweep(monkeypatch, store)
    assert _finalizes(seams) == ["relaunch"]  # the relaunch's own, no other
    assert rows.stamps == []  # the sweep wrote nothing
    [kept] = [c for c in store.comments(42) if "was kept" in c]
    assert "commit aaaaaaaaaaaa" in kept  # the first words stand
    assert {a["fingerprint"] for a in _alerts(seams, "raise")} == {tree.name}
    assert tree.is_dir() and "drop" not in _names(seams)


def test_a_kept_folder_is_commented_once_per_state_not_per_wording(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42)
    store = _Store()
    store.add_todo(42, "t")
    host = _slice_park_node()

    def report(folder: Path, state: str, note: str) -> None:
        why, lead = run_slice._unsaved({"state": state, "note": note})
        run_slice._report_kept(None, 42, folder, why, host, store=store, state=lead)

    def comments() -> int:
        return len([c for c in store.comments(42) if "was kept" in c])

    report(tree, "withheld", "secret-scan: commit aaaa")
    report(tree, "withheld", "secret-scan: commit bbbb")  # same folder and state
    assert comments() == 1
    report(tree, "failed", "push failed")  # another state is news
    assert comments() == 2
    report(tree, "failed", "push failed again")
    assert comments() == 2
    report(_job_tree(tmp_path, 42, 901), "withheld", "x")  # another folder
    assert comments() == 3


# ── signals need an identity (gr741695 round 4) ────────────────────

_NO_IDENTITY = {"pid": 4242, "pgid": 4242, "started_at": "2026-11-20T00:00:00Z"}


def test_a_handle_with_no_identity_is_never_signalled(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ps`` failed at submit: the legacy rule admits a live pid as the
    session, and a TERM or KILL could land on another build's group. The
    host sweep waits (and, long after, says so); the poll waits too."""
    from datetime import UTC, datetime, timedelta

    assert run_slice._may_signal(_NO_IDENTITY) is False
    assert run_slice._may_signal({**_NO_IDENTITY, "boot_id": "b", "start_time": 1})
    assert not run_slice._may_signal(
        {"pid": 4242, "pgid": 4242, "ps_identity": "x", "recovered_exit": True}
    )
    store = _Store()
    store.add_todo(42, "t")
    tree = _job_tree(tmp_path, 42, 70)
    seams["alive"] = True
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    _settle_row(
        monkeypatch,
        "failed",
        {"compute_handle": dict(_NO_IDENTITY), "finished_at": old},
    )
    assert run_slice._settle(store, tmp_path, tree, 42, 70, "build") is None
    assert seams["signals"] == [] and "finalize" not in _names(seams)
    assert [c for c in store.comments(42) if "was kept" in c] == []  # too soon


def test_a_group_that_will_not_go_is_reported_kept_once_across_sweeps(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import UTC, datetime, timedelta

    store = _Store()
    store.add_todo(42, "t")
    tree = _job_tree(tmp_path, 42, 70)
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    handle = {"pid": 4242, "pgid": 4242, "boot_id": "boot", "start_time": 55}
    long_ago = (
        datetime.now(UTC) - timedelta(seconds=run_slice.SETTLE_STUCK_AFTER_S + 60)
    ).isoformat()
    _settle_row(
        monkeypatch, "failed", {"compute_handle": handle, "finished_at": long_ago}
    )
    for _ in range(3):  # three sweeps
        assert run_slice._settle(store, tmp_path, tree, 42, 70, "build") is None
    kept = [c for c in store.comments(42) if "was kept" in c]
    assert len(kept) == 1 and "still answers after the job ended" in kept[0]
    assert {a["fingerprint"] for a in _alerts(seams, "raise")} == {tree.name}
    # a handle with no identity says so
    seams["alive"] = True
    _settle_row(
        monkeypatch,
        "failed",
        {"compute_handle": dict(_NO_IDENTITY), "finished_at": long_ago},
    )
    tree2 = _job_tree(tmp_path, 42, 71)
    assert run_slice._settle(store, tmp_path, tree2, 42, 71, "build") is None
    assert any("no recorded identity" in c for c in store.comments(42))


def test_the_poll_waits_for_a_group_it_cannot_identify_and_signals_nothing(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.add_todo(42, "Build")
    monkeypatch.setattr(run_slice.sys, "platform", "linux")  # no ps identity
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert (
        handle is not None and "boot_id" not in handle and "ps_identity" not in handle
    )
    seams["alive"] = False
    seams["group_alive"] = True
    _finish(handle, 1, "tail\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    assert seams["signals"] == [] and "finalize" not in _names(seams)


# ── the alert's recovery command, the old pre-land text ────────────


def test_the_kept_folder_alert_names_the_command_the_hub_admits(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42, 900)
    run_slice._raise_kept_alert(_Store(), tree, "the hub does not hold its work")
    [raised] = _alerts(seams, "raise")
    detail = raised["detail"]
    # a plain push: a ``+`` could drop commits the hub ref already holds
    assert (
        f"{_slice_ws.ENV_WORKER}=1 git push origin HEAD:refs/factory/build/42/900"
        in detail
    )
    assert "+HEAD" not in detail
    # ... and what a refusal as not a fast-forward means
    assert "not a fast-forward" in detail
    assert "other work under that ref" in detail
    assert "leave the folder as it is and file a gripe" in detail
    assert "from inside the folder" in detail
    assert f"{_slice_ws.HOOK_REFUSED} ({_slice_ws.HOOK_SECRET})" in detail
    assert "leave every commit first" in detail


def test_the_preland_paragraphs_discount_the_old_engines_fetch_instruction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_git(args: list[str], cwd: Path) -> Any:
        if args[:2] == ["rev-parse", "--verify"]:
            return _git_proc("9" * 40 + "\n")
        return _git_proc("")

    monkeypatch.setattr(run_slice, "_git", fake_git)
    meta = {"generate": {"role": "reviewer", "from": "td7"}}
    for resolution in (None, {"of": _OF, "ref": _RES_REF}):
        target = run_slice._review_target(
            _resolution_store(), meta, tree=tmp_path, resolution=resolution
        )
        assert target is not None
        assert "names `refs/factory/slice/` is from the old engine" in target
        assert "this clone already holds `origin/slice/7`" in target


def test_a_relaunch_whose_folder_cannot_be_saved_reports_it_before_it_raises(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    store = _Store()
    store.add_todo(42, "t")
    seams["pushed_by_reason"]["relaunch"] = _pushed(
        _slice_ws.WITHHELD, note="secret-scan: 1 hit"
    )
    ctx = _Ctx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, run_slice.Relaunch({"own_folder": True})) is None
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    # the collected notes are job events ...
    events = [t for k, t in ctx.chunks if k == "job_event"]
    assert any(
        "relaunch: folder slice-td42-j900 finalized as withheld" in t for t in events
    )
    assert any("was kept" in t for t in events)
    # ... the task's comment names host and folder ... and the alert is raised
    [kept] = [c for c in store.comments(42) if "was kept" in c]
    assert str(tree) in kept and _slice_park_node() in kept
    [raised] = _alerts(seams, "raise")
    assert raised["fingerprint"] == tree.name and str(tree) in raised["detail"]


def test_a_second_repositorys_clone_gets_the_profiles_excludes(
    seams: dict[str, Any], catpath_build: dict[str, Any]
) -> None:
    store = _Store()
    store.add_todo(42, "Widen the grid", body="Do X.", tags=("repo:catpath",))
    ctx, handle = _submit(store, seams, repo="catpath")
    assert handle is not None and ctx.failure is None, ctx.failure
    tree = _job_tree(catpath_build["root"], 42)
    assert seams["excludes"] == [(tree, catpath_build["profile"].exclude)]
    # the default repository's clone is cut with the repository's own ignores
    seams["excludes"].clear()
    store2 = _Store()
    store2.add_todo(43, "t")
    ctx2 = _Ctx(store2, {"todo_id": 43})
    ctx2.ref_id = 901
    assert run_slice.submit(ctx2, None) is not None
    assert seams["excludes"] == []


def test_finish_steps_end_on_a_clean_tree(seams: dict[str, Any]) -> None:
    """The checks run fixers; no step used to say to commit what they
    changed, so a session following the steps pushed HEAD with a dirty tree
    that finalize then snapshotted as unfinished work."""
    need = "`git status --short` must print nothing"
    amend = "`git commit --amend --no-edit`"
    brief = run_slice.render_brief(
        todo_id=42,
        title="t",
        body="b",
        latest=None,
        lane="l",
        model="m",
        effort="high",
        job_id=77,
    )
    assert need in brief and amend in brief
    assert brief.index(need) < brief.index("git push origin +HEAD")
    assert "(`ruff check --fix`, `ruff format`)" in brief
    for adapter in ("devin_tree", "kimi_tree"):
        other = run_slice.render_brief(
            todo_id=42,
            title="t",
            body="b",
            latest=None,
            lane="l",
            model="m",
            effort="high",
            job_id=77,
            adapter=adapter,
            mcp=False,
        )
        assert need in other and amend in other
    cat = run_slice.render_brief(
        todo_id=42,
        title="t",
        body="b",
        latest=None,
        lane="l",
        model="m",
        effort="high",
        job_id=77,
        profile=lanes.REPO_PROFILES["catpath"],
    )
    assert need in cat and amend in cat and "ruff" not in cat.split("## When done")[1]
    conflict = _conflict_brief()
    assert need in conflict and amend in conflict
    assert conflict.index(need) < conflict.index("git push origin +HEAD")
    codex = run_slice.render_brief(
        todo_id=42,
        title="t",
        body="b",
        latest=None,
        lane="l",
        model="m",
        effort="high",
        job_id=77,
        adapter="codex_exec",
        mcp=False,
    )
    assert need not in codex  # its worker commits and pushes


# ── budget stops and recovered exits need an identity (round 5) ────


def _over_budget(
    store: _Store,
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_Ctx, dict[str, Any]]:
    """A running devin session over its allowance, launched with no
    recorded identity (``ps`` failed; Linux without a start time)."""
    from tests.test_run_slice import _atif

    monkeypatch.setattr(run_slice.sys, "platform", "linux")
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert handle is not None
    assert "boot_id" not in handle and "ps_identity" not in handle
    _atif(Path(handle["atif"]), final={"total_cost_usd": 6.25, "total_steps": 3})
    return ctx, handle


def _over_budget_poll(store: _Store, ctx: _Ctx, handle: Any) -> _Ctx:
    pctx = _poll_ctx(store, ctx)
    pctx.meta["params"]["budget_usd"] = 5.0
    assert run_slice.poll(pctx, handle) is False
    return pctx


def _unsignalled_events(pctx: _Ctx) -> list[str]:
    return [
        t for k, t in pctx.chunks if k == "job_event" and "cannot be signalled" in t
    ]


def test_the_budget_watch_signals_nothing_without_an_identity_and_says_so_once(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _over_budget(store, seams, monkeypatch)
    pctx = _over_budget_poll(store, ctx, handle)
    assert seams["signals"] == [] and "budget_kill" not in handle
    [event] = _unsignalled_events(pctx)
    assert "budget is exceeded" in event
    assert "the launch recorded no identity" in event
    assert "deadline will stop it" in event
    assert handle["budget_unsignalled"] is True  # persisted with the handle
    # every later pass, and a restarted worker reading the handle back, stays quiet
    for _ in range(2):
        again = _over_budget_poll(store, ctx, dict(handle))
        assert _unsignalled_events(again) == [] and seams["signals"] == []


def test_the_budget_watchs_sigkill_needs_an_identity_too(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _over_budget(store, seams, monkeypatch)
    # a TERM an earlier worker sent, long past its grace
    handle["budget_kill"] = {"sig": "term", "at": "2000-01-01T00:00:00Z"}
    pctx = _over_budget_poll(store, ctx, handle)
    assert seams["signals"] == []
    assert handle["budget_kill"]["sig"] == "term"
    assert len(_unsignalled_events(pctx)) == 1


def test_the_budget_watch_still_signals_a_launch_with_an_identity(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _over_budget(store, seams, monkeypatch)
    handle.update(boot_id="boot", start_time=55)
    seams["identity"] = ("boot", 55)
    pctx = _over_budget_poll(store, ctx, handle)
    assert seams["signals"] == [(handle["pgid"], run_slice.signal.SIGTERM)]
    assert _unsignalled_events(pctx) == []


def test_the_budget_watch_waits_out_a_pass_that_cannot_identify_the_leader(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over budget, the leader answers, its identity cannot be read this
    pass: no signal and no stop record, so the next pass decides again."""
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _over_budget(store, seams, monkeypatch)
    handle.update(boot_id="boot", start_time=55)
    seams["identity"] = None
    pctx = _over_budget_poll(store, ctx, handle)
    assert seams["signals"] == [] and "budget_kill" not in handle
    assert _unsignalled_events(pctx) == []
    seams["identity"] = ("boot", 55)
    _over_budget_poll(store, ctx, handle)
    assert seams["signals"] == [(handle["pgid"], run_slice.signal.SIGTERM)]


_FINISHED_OK = json.dumps({"is_error": False, "result": "done.\ntests: ran"})


def _recovered(
    store: _Store, seams: dict[str, Any]
) -> tuple[_Ctx, dict[str, Any], Path]:
    """A session that exited (its exit file is there) whose worker restarted:
    the recovered handle has an exit and no leader to identify."""
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    handle["recovered_exit"] = True
    seams["alive"] = False
    _finish(handle, 0, _FINISHED_OK)
    return ctx, handle, tree


def test_a_recovered_exit_whose_group_still_answers_is_waited_out(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The common path after a worker restart: a backgrounded child may
    still write in the folder, so nothing is finalized or dropped and nothing
    is signalled (the group cannot be told from another that reused the
    number); the job's deadline stops it."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle, tree = _recovered(store, seams)
    seams["group_alive"] = True
    assert run_slice._session_alive(handle) is True
    assert run_slice._may_signal(handle) is False
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    assert seams["signals"] == [] and tree.is_dir()
    assert not {"finalize", "drop"} & set(_names(seams))
    [event] = [t for k, t in pctx.chunks if k == "job_event" and "still answers" in t]
    assert f"process group {handle['pgid']}" in event
    assert "no identity to signal it with" in event
    assert "the job's deadline stops it" in event
    # once per handle
    pctx2 = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx2, handle) is False
    assert not [t for k, t in pctx2.chunks if "still answers" in t]
    # release keeps the folder ...
    run_slice.release(_release_ctx(store), handle)
    assert tree.is_dir() and not {"finalize", "drop"} & set(_names(seams))
    # ... and so does the host sweep, long after the job ended
    old = "2020-01-01T00:00:00+00:00"
    _settle_row(
        monkeypatch, "failed", {"compute_handle": dict(handle), "finished_at": old}
    )
    assert run_slice._settle(store, tmp_path, tree, 42, 900, "build") is None
    assert seams["signals"] == [] and tree.is_dir()
    assert not {"finalize", "drop"} & set(_names(seams))


def test_a_recovered_exit_whose_group_is_gone_finalizes_as_before(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle, tree = _recovered(store, seams)
    seams["group_alive"] = False
    assert run_slice._session_alive(handle) is False
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    names = _names(seams)
    assert names.index("finalize") < names.index("drop")
    assert seams["signals"] == []


# ── launch ordering: a task's earlier folder is settled first (round 6) ──────


class _Rows:
    """The job rows the worker reads, live: what the sweep stamps on a job is
    what the next read returns. ``gone``: no such job; ``unreadable``: the
    database cannot be read."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
        self.rows: dict[int, tuple[str, dict[str, Any]]] = {}
        self.gone: set[int] = set()
        self.unreadable = False
        self.stamps: list[tuple[int, dict[str, Any]]] = []
        real_stamp = store.stamp_ref_meta

        def read(_store: Any, job_id: int) -> Any:
            if self.unreadable:
                return None
            if job_id in self.gone:
                return (run_slice.ROW_ABSENT, {})
            return self.rows.get(job_id)

        def stamp(ref_id: int, updates: dict[str, Any], **kw: Any) -> None:
            self.stamps.append((ref_id, updates))
            if ref_id in self.rows:
                self.rows[ref_id][1].update(updates)
            else:
                real_stamp(ref_id, updates, **kw)

        monkeypatch.setattr(run_slice, "_job_row", read)
        monkeypatch.setattr(store, "stamp_ref_meta", stamp, raising=False)

    def set(self, job: int, status: str, **meta: Any) -> None:
        self.rows[job] = (status, {"params": {"todo_id": 42}, **meta})

    def job_stamps(self, job: int) -> list[dict[str, Any]]:
        return [u for r, u in self.stamps if r == job]


def _sweep(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """One watch pass's ``tidy``, over the real sweep."""
    monkeypatch.setattr(_slice_ws, "sweep", _REAL_SWEEP)
    monkeypatch.setattr(run_slice, "_tidy_last", None)
    run_slice.tidy(store, None)


def _finalizes(seams: dict[str, Any]) -> list[str]:
    return [d["reason"] for n, d in seams["ws"] if n == "finalize"]


def _earlier_folder(tmp_path: Path, job: int = 70) -> Path:
    folder = _job_tree(tmp_path, 42, job)
    folder.mkdir(parents=True)
    (folder / "work.txt").write_text("unsaved\n", encoding="utf-8")
    _builds_in(tmp_path)
    return folder


def _stored_record(
    job: int = 70, state: str = "pushed", **extra: Any
) -> dict[str, Any]:
    return {**_pushed(state, job=job).as_meta(), **extra}


def test_a_launch_waits_while_the_tasks_earlier_folder_is_unsettled(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = _earlier_folder(tmp_path)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.set(70, "failed", compute_handle=dict(_HANDLE))  # no record
    ctx, handle = _submit(store, seams)
    # parked through the not_before handle, attempt-free: nothing cut, nothing
    # launched, nothing claimed, and nothing of the earlier folder touched
    assert ctx.failure is None and ctx.status is None
    assert set(handle) >= {"not_before", "folder_wait_since", "folder_wait"}
    assert handle["folder_wait"] == folder.name
    assert "cut" not in _names(seams) and seams["launch"] == []
    assert not any(t.startswith("claimed-by:") for t in store.tag_strings(42))
    assert (folder / "work.txt").read_text(encoding="utf-8") == "unsaved\n"
    assert {"finalize", "drop", "sweep"}.isdisjoint(_names(seams))
    events = [t for k, t in ctx.chunks if k == "job_event" and folder.name in t]
    assert len(events) == 1
    # the poll runs the launch again once due: still unsettled, still parked,
    # and no second event
    rows.set(900, "running")
    handle["not_before"] = "2000-01-01T00:00:00Z"
    since = handle["folder_wait_since"]
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    assert "not_before" in handle and handle["folder_wait_since"] == since
    assert handle["not_before"] > "2000-01-01T00:00:00Z"
    assert not [t for k, t in pctx.chunks if k == "job_event"]
    assert pctx.meta_set["compute_handle"] == handle
    assert seams["launch"] == []


def test_a_parked_launch_runs_once_the_sweep_has_settled_the_earlier_folder(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = _earlier_folder(tmp_path)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.set(70, "failed", compute_handle=dict(_HANDLE))
    ctx, handle = _submit(store, seams)
    assert "not_before" in handle
    # the sweep saved the work: the job's row has its record
    rows.set(
        70,
        "failed",
        compute_handle=dict(_HANDLE),
        push=_stored_record(launch=run_slice._launch_key(_HANDLE)),
    )
    rows.set(900, "running")
    handle["not_before"] = "2000-01-01T00:00:00Z"
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    assert "not_before" not in handle and handle["pid"] == 4242
    assert "cut" in _names(seams) and len(seams["launch"]) == 1
    assert pctx.meta_set["compute_handle"] == handle
    assert folder.is_dir()  # the launch settles nothing: the sweep drops it


def test_a_launch_goes_at_once_when_the_earlier_folder_was_kept_unsaved(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A withheld record is a settled folder (kept and reported); waiting for
    it to go would wait forever. A failed one is too, once its retries are
    spent."""
    folder = _earlier_folder(tmp_path)
    for state, tries in ((_slice_ws.WITHHELD, 0), (_slice_ws.FAILED, 4)):
        store = _Store()
        store.add_todo(42, "t")
        rows = _Rows(monkeypatch, store)
        rows.set(
            70,
            "failed",
            compute_handle=dict(_HANDLE),
            push=_stored_record(
                state=state, launch=run_slice._launch_key(_HANDLE), tries=tries
            ),
        )
        shutil.rmtree(_job_tree(tmp_path, 42, 900), ignore_errors=True)
        ctx, handle = _submit(store, seams)
        assert handle is not None and "not_before" not in handle, state
        assert handle["worktree"] == str(_job_tree(tmp_path, 42, 900))
    assert (folder / "work.txt").exists()


def test_a_launch_ends_attempt_free_when_the_earlier_folder_stays_unsettled(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import time

    folder = _earlier_folder(tmp_path)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.set(70, "failed", compute_handle=dict(_HANDLE))
    rows.set(900, "running")
    ctx = _Ctx(store, {"todo_id": 42})
    parked = {
        "not_before": "2000-01-01T00:00:00Z",
        "folder_wait_since": time.time() - run_slice.EARLIER_FOLDER_WAIT_S - 5,
        "folder_wait": folder.name,
    }
    assert run_slice.poll(ctx, parked) is True
    assert ctx.status == "failed"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    failure = ctx.failure or ""
    assert "look at it by hand" in failure and folder.name in failure
    assert "no attempt burned" in failure
    # the kept alert and the task's comment, the folder as it was
    [raised] = _alerts(seams, "raise")
    assert raised["fingerprint"] == folder.name
    assert any("was kept" in c and folder.name in c for c in store.comments(42))
    assert (folder / "work.txt").exists()
    assert "cut" not in _names(seams) and seams["launch"] == []
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))


def test_a_launch_ignores_its_own_jobs_folder_and_other_tasks_folders(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = _job_tree(tmp_path, 7, 3)
    other.mkdir(parents=True)
    land = _slice_ws.attempt_dir(tmp_path, 42, 71, "land")
    land.mkdir(parents=True)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.set(3, "failed")  # unsettled, were they considered
    rows.set(71, "failed")
    ctx, handle = _submit(store, seams)
    assert handle is not None and "not_before" not in handle


def test_no_lock_file_is_ever_created_beside_an_attempt_folder(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    _poll_with(store, seams, ctx, handle)
    ctx2, handle2 = _submit(store, seams)
    seams["alive"] = False
    run_slice.release(_release_ctx(store), handle2)
    rows = _Rows(monkeypatch, store)
    rows.set(900, "failed", compute_handle=dict(handle2))
    _sweep(monkeypatch, store)
    assert not hasattr(_slice_ws, "folder_lock") and not hasattr(_slice_ws, "lock_path")
    assert (
        list(tree.parent.glob(".*lock")) == []
        and list(tree.parent.glob("*.lock")) == []
    )


# ── round 7: what the fourth review found ──────────────────────────────────


def test_a_kept_folder_whose_handle_has_no_pid_is_finalized_once_not_every_sweep(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An outage park strips the pid, so no launch key can be made: the stored
    record is trusted anyway. Before, every sweep made a new snapshot, a push,
    a stamp and an alert rewrite."""
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    _builds_in(tmp_path)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.set(70, "failed", compute_handle={"worktree": str(tree), "own_folder": True})
    seams["alive"] = False
    seams["pushed"] = _slice_ws.Pushed(
        state=_slice_ws.WITHHELD, ref="refs/factory/build/42/70", note="secret-scan"
    )
    for _ in range(3):
        _sweep(monkeypatch, store)
    assert _finalizes(seams) == ["run ended"]
    assert len(rows.job_stamps(70)) == 1
    assert len([c for c in store.comments(42) if "was kept" in c]) == 1
    assert tree.is_dir() and "drop" not in _names(seams)


def test_a_relaunch_that_fails_leaves_no_stored_push_for_a_session_it_never_ran(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The relaunch's own record is cleared as soon as the clone is cut, so a
    launch that then fails (or crashes) does not leave it as the new session's."""
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    store = _Store()
    store.add_todo(42, "t")
    seams["pushed_by_reason"]["relaunch"] = _pushed(sha=SNAPSHOT, wip=True, by="worker")
    monkeypatch.setattr(
        run_slice,
        "_launch",
        lambda *a, **kw: (_ for _ in ()).throw(run_slice._HostRefusal("host blocked")),
    )
    ctx = _Ctx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, run_slice.Relaunch({"own_folder": True})) is None
    assert "finalize" in _names(seams) and "cut" in _names(seams)
    assert ctx.meta_set["push"] is None


@pytest.mark.parametrize("path", ["carried", "ahead"])
def test_a_routed_launch_drops_its_clone_before_the_job_ends(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
) -> None:
    """A terminal job whose folder has no push record is what the host sweep
    judges: the clone is gone by the time the job is terminal."""
    tree = _job_tree(tmp_path, 42, 900)
    seen: list[bool] = []

    class _SpyCtx(_Ctx):
        def record_failure(self, reason: str, **kw: Any) -> None:
            seen.append(tree.exists())
            super().record_failure(reason, **kw)

    store = _Store()
    store.add_todo(42, "t", body="Do X.", meta={"build_ref": _recorded()})
    seams["start"] = _slice_ws.Start(sha="a" * 40, ref=PUSHED_REF)
    seams["ahead"] = "aaa fix: the work\n"
    seams["cherry"] = "+ aaa\n"
    carried = {"sha": "c" * 40} if path == "carried" else None
    monkeypatch.setattr(run_slice, "_carried_on_base", lambda *a, **k: carried)
    ctx = _SpyCtx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, None) is None
    want = "already-on-main" if path == "carried" else "branch-ahead"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == want
    assert seen == [False]  # the clone was already gone when the job ended


@pytest.mark.parametrize(
    "parked",
    [
        {"folder_wait": "slice-td42-j70", "folder_wait_since": 1.0},
        {},
    ],
    ids=["folder-wait", "bare"],
)
def test_only_an_outage_park_may_finalize_and_drop_the_jobs_own_folder(
    seams: dict[str, Any], tmp_path: Path, parked: dict[str, Any]
) -> None:
    """A first launch parked for an earlier folder comes back as a Relaunch
    but is still a first launch: a folder of its own job fails it
    attempt-free, untouched."""
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    (tree / "work.txt").write_text("unsaved\n", encoding="utf-8")
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, run_slice.Relaunch(dict(parked))) is None
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    assert "already there" in (ctx.failure or "")
    assert {"finalize", "drop", "cut"}.isdisjoint(_names(seams))
    assert (tree / "work.txt").read_text(encoding="utf-8") == "unsaved\n"


def test_a_wait_for_an_earlier_folder_stays_a_first_launch_across_retries(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    earlier = _earlier_folder(tmp_path)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.set(70, "failed", compute_handle=dict(_HANDLE))
    ctx, handle = _submit(store, seams)
    assert "own_folder" not in handle
    ctx2 = _Ctx(store, {"todo_id": 42})
    again = run_slice.submit(ctx2, run_slice.Relaunch(dict(handle)))
    assert again["folder_wait_since"] == handle["folder_wait_since"]
    assert "own_folder" not in again
    assert earlier.is_dir()
    # an outage relaunch that has to wait stays one
    again = run_slice.submit(ctx2, run_slice.Relaunch({**handle, "own_folder": True}))
    assert again["own_folder"] is True


def _live_run(
    tmp_path: Path,
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tree: Path,
    *,
    todo: int = 42,
) -> dict[str, Any]:
    """A session of the job that owns ``tree`` that is running: the run dir a
    launch leaves (``baseline``, ``handle``, ``pid``, in the real format) and an
    identity that matches. Nothing of the recovery is stubbed."""
    _write_handle_record(tmp_path / "ops" / f"td{todo}" / ("a" * 32), tree)
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    return {"worktree": str(tree), "pid": 4242}


def test_a_relaunch_never_finalizes_or_drops_under_a_live_session(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    (tree / "work.txt").write_text("unsaved\n", encoding="utf-8")
    _live_run(tmp_path, seams, monkeypatch, tree)
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, run_slice.Relaunch({"own_folder": True})) is None
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    assert "still running" in (ctx.failure or "")
    assert {"finalize", "drop", "cut"}.isdisjoint(_names(seams))
    assert (tree / "work.txt").exists() and seams["launch"] == []


def test_the_poll_adopts_the_session_a_crashed_relaunch_left_running(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The worker died between the launch and the save of the new handle: the
    stored handle is still the parked one. The next poll finds the session the
    way a restart does and takes it over; it does not relaunch."""
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    live = _live_run(tmp_path, seams, monkeypatch, tree)
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42}, lane="claude-sub")
    parked = {
        "worktree": str(tree),
        "own_folder": True,
        "not_before": "2000-01-01T00:00:00Z",
        "outage_parks": 1,
    }
    assert run_slice.poll(ctx, parked) is False
    assert parked["pid"] == live["pid"] and "not_before" not in parked
    assert parked["outage_parks"] == 1
    assert ctx.meta_set["compute_handle"] == parked
    assert {"finalize", "drop", "cut"}.isdisjoint(_names(seams))
    assert seams["launch"] == [] and tree.is_dir()
    assert any("adopted the live session" in t for k, t in ctx.chunks)


def test_the_sweep_leaves_a_folder_with_a_live_session_alone_under_a_parked_handle(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The job was cancelled while its handle was still the parked one."""
    import signal
    from datetime import UTC, datetime, timedelta

    tree = _job_tree(tmp_path, 42, 900)
    tree.mkdir(parents=True)
    _builds_in(tmp_path)
    _live_run(tmp_path, seams, monkeypatch, tree)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    parked = {
        "worktree": str(tree),
        "own_folder": True,
        "not_before": "2030-01-01T00:00:00Z",
    }
    soon = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    rows.set(900, "cancelled", compute_handle=parked, finished_at=soon)
    _sweep(monkeypatch, store)
    assert seams["signals"] == [] and "finalize" not in _names(seams)
    # 45 seconds on: the sweep kills the group (it can: the launch had an identity)
    old = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    rows.set(900, "cancelled", compute_handle=parked, finished_at=old)
    _sweep(monkeypatch, store)
    assert seams["signals"] == [(4242, signal.SIGKILL)]
    assert "finalize" not in _names(seams) and tree.is_dir()
    # a quarter hour on and still there: the stuck report
    long_ago = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    rows.set(900, "cancelled", compute_handle=parked, finished_at=long_ago)
    _sweep(monkeypatch, store)
    assert any("still answers after the job ended" in c for c in store.comments(42))
    assert "finalize" not in _names(seams) and "drop" not in _names(seams)


def test_an_outage_park_saves_the_sessions_work_first_and_the_relaunch_does_not_adopt_it(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_run_slice import (
        _QUOTA_RESULT,
        _codex_and_sub,
        _pool_failures,
    )

    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    codex, sub = _codex_and_sub()
    _pool_failures(monkeypatch)
    use_lanes(monkeypatch, codex, sub)
    seams["alive"] = False
    seams["pushed"] = _pushed(sha=SNAPSHOT, head="b" * 40, wip=True, by="worker")
    _finish(handle, 1, json.dumps(_QUOTA_RESULT) + "\n")
    key = run_slice._launch_key(handle)
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    # saved BEFORE the park: record on the job, build_ref on the task, folder kept
    assert _finalizes(seams) == ["session stopped"]
    record = pctx.meta_set["push"]
    assert record["launch"] == key and record["sha"] == SNAPSHOT
    assert store.refs[42].meta["build_ref"]["sha"] == SNAPSHOT
    assert tree.is_dir() and "pid" not in handle and handle["own_folder"] is True
    # the parked handle has no launch key; the record still reads as settled
    meta = {"compute_handle": dict(handle), "push": record, "params": {"todo_id": 42}}
    assert run_slice._push_settled(meta) is True
    # the relaunch does not adopt the old record: its launch is another
    rows = _Rows(monkeypatch, store)
    rows.set(900, "running", push=record)
    rctx = _poll_ctx(store, ctx)
    rctx.meta["exclude_vendor"] = "anthropic"
    assert run_slice.poll(rctx, handle) is False
    assert handle["pid"] == 4242 and "not_before" not in handle
    assert rctx.meta_set["push"] is None  # the relaunch cleared the old record
    # a later pass of the new launch (another pid) does not adopt the old one
    assert run_slice._poll_look(rctx, {**handle, "pid": 5555})[1] is None


def test_a_job_cancelled_while_parked_has_its_folder_dropped_by_the_sweep_once(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    _builds_in(tmp_path)
    seams["alive"] = False
    seams["pushed"] = _pushed(sha=SNAPSHOT, head="b" * 40, by="worker")
    record = {**_pushed(sha=SNAPSHOT, head="b" * 40, by="worker").as_meta()}
    handle.pop("pid"), handle.pop("pgid")
    rows = _Rows(monkeypatch, store)
    rows.set(
        900, "cancelled", compute_handle={**handle, "own_folder": True}, push=record
    )
    _sweep(monkeypatch, store)
    # the park saved it; the sweep confirms before it deletes, and the same sha
    # leaves the record alone
    assert _finalizes(seams) == ["run ended"] and rows.stamps == []
    assert ("drop", tree) in seams["ws"] and not tree.exists()


def test_an_absent_or_foreign_job_row_raises_one_alert_and_no_comment(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    _builds_in(tmp_path)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.gone.add(70)
    for _ in range(2):
        _sweep(monkeypatch, store)
    raised = _alerts(seams, "raise")
    assert {a["fingerprint"] for a in raised} == {tree.name}
    assert len({a["detail"] for a in raised}) == 1  # the same words every sweep
    detail = raised[0]["detail"]
    assert "no job of td42 in this worker's database owns this folder" in detail
    assert "it was cut under another database, or its job row is gone" in detail
    assert "look at it by hand" in detail and "git push" not in detail
    assert store.comments(42) == []
    assert {"finalize", "drop"}.isdisjoint(_names(seams)) and tree.is_dir()
    # another task's row is the same
    seams["alerts"].clear()
    rows.gone.clear()
    rows.rows[70] = ("failed", {"params": {"todo_id": 7}})
    _sweep(monkeypatch, store)
    assert [a["fingerprint"] for a in _alerts(seams, "raise")] == [tree.name]
    assert store.comments(42) == [] and store.comments(7) == []


def test_an_unreadable_job_row_stays_silent_and_is_tried_again(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    _builds_in(tmp_path)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.unreadable = True
    _sweep(monkeypatch, store)
    assert _alerts(seams, "raise") == [] and "finalize" not in _names(seams)
    rows.unreadable = False
    rows.set(70, "failed", compute_handle=dict(_HANDLE))
    seams["alive"] = False
    _sweep(monkeypatch, store)
    assert "finalize" in _names(seams)


class _Cursor:
    def __init__(self, row: Any) -> None:
        self.row = row

    def fetchone(self) -> Any:
        return self.row


class _SqlStore:
    """Just enough of ``store.pool.connection()`` for ``_job_row``."""

    def __init__(self, row: Any = None, error: Exception | None = None) -> None:
        self.row, self.error = row, error

    @property
    def pool(self) -> Any:
        outer = self

        class _Pool:
            def connection(self) -> Any:
                import contextlib

                @contextlib.contextmanager
                def cm() -> Any:
                    if outer.error is not None:
                        raise outer.error
                    yield SimpleNamespace(execute=lambda *a, **k: _Cursor(outer.row))

                return cm()

        return _Pool()


def test_the_job_row_tells_unreadable_from_absent_and_carries_the_status_time() -> None:
    from datetime import UTC, datetime

    assert run_slice._job_row(_SqlStore(error=RuntimeError("db down")), 70) is None
    assert run_slice._job_row(_SqlStore(row=None), 70) == (run_slice.ROW_ABSENT, {})
    when = datetime(2026, 11, 20, 1, 2, 3, tzinfo=UTC)
    got = run_slice._job_row(_SqlStore(row=("failed", {"a": 1}, when)), 70)
    assert got == ("failed", {"a": 1, "_status_at": when.isoformat()})
    got = run_slice._job_row(_SqlStore(row=(None, {}, None)), 70)
    assert got == (None, {})


def test_a_terminal_job_with_no_finished_at_is_judged_by_when_its_status_was_set(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import signal
    from datetime import UTC, datetime, timedelta

    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    handle = {"pid": 4242, "pgid": 4242, "boot_id": "boot", "start_time": 55}
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    set_at = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    rows.set(70, "failed", compute_handle=handle, _status_at=set_at)
    assert _settle(store, tree) is None
    assert seams["signals"] == [(4242, signal.SIGKILL)]
    set_at = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    rows.set(70, "failed", compute_handle=handle, _status_at=set_at)
    assert _settle(store, tree) is None
    assert any("still answers after the job ended" in c for c in store.comments(42))


@pytest.mark.parametrize(
    ("state", "tries", "age_s", "again"),
    [
        ("failed", 0, 10, False),
        ("failed", 0, 31, True),
        ("failed", 1, 59, False),
        ("failed", 1, 61, True),
        ("failed", 2, 119, False),
        ("failed", 2, 121, True),
        ("failed", 3, 239, False),
        ("failed", 3, 241, True),
        ("failed", 4, 100_000, False),
        ("withheld", 0, 100_000, False),
    ],
)
def test_a_failed_push_is_tried_again_after_30_60_120_and_240_seconds_a_withheld_one_never(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
    tries: int,
    age_s: int,
    again: bool,
) -> None:
    from datetime import UTC, datetime, timedelta

    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    seams["alive"] = False
    seams["pushed"] = _pushed(job=70, sha=SNAPSHOT, by="worker")
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    at = (datetime.now(UTC) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stored = {
        **_pushed(state, job=70).as_meta(),
        "launch": run_slice._launch_key(_HANDLE),
        "at": at,
    }
    if tries:
        stored["tries"] = tries
    rows.set(70, "failed", compute_handle=dict(_HANDLE), push=stored)
    got = _settle(store, tree)
    assert got is not None
    assert (["run ended"] if again else []) == _finalizes(seams)
    if again:
        assert got.state == "pushed"
        assert rows.job_stamps(70)[0]["push"]["tries"] == tries + 1
        assert store.refs[42].meta["build_ref"]["sha"] == SNAPSHOT
    else:
        assert got.state == state and rows.stamps == []


def test_a_retry_that_fails_again_counts_and_the_launch_waits_for_the_retries(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = _earlier_folder(tmp_path)
    seams["alive"] = False
    seams["pushed"] = _pushed(_slice_ws.FAILED, job=70, note="push timed out")
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    first = {
        **_pushed(_slice_ws.FAILED, job=70).as_meta(),
        "at": "2000-01-01T00:00:00Z",
    }
    rows.set(70, "failed", compute_handle={"worktree": str(folder)}, push=first)
    for n in (1, 2, 3, 4):
        # the folder is unsettled while retries are left
        assert run_slice._unsettled_earlier_folder(store, tmp_path, 42, 900) == folder
        rows.rows[70][1]["push"]["at"] = "2000-01-01T00:00:00Z"
        _sweep(monkeypatch, store)
        assert rows.rows[70][1]["push"]["tries"] == n
        assert rows.rows[70][1]["push"]["state"] == "failed"
    assert run_slice._unsettled_earlier_folder(store, tmp_path, 42, 900) is None
    rows.rows[70][1]["push"]["at"] = "2000-01-01T00:00:00Z"
    _sweep(monkeypatch, store)  # final: no fifth try
    assert len(_finalizes(seams)) == 4


def test_a_good_run_whose_folder_the_poll_dropped_before_it_ended_the_job_is_finished(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll dropped the folder and the worker died before ``set_status``:
    the next pass finds no folder, but the stored record of this launch and the
    ``meta.outcome`` its arms stamped say what happened."""
    for outcome, ok in (("completed", True), ("error", False)):
        store = _Store()
        store.add_todo(42, "t")
        ctx, handle = _submit(store, seams)
        tree = Path(handle["worktree"])
        shutil.rmtree(tree)
        seams["alive"] = False
        rows = _Rows(monkeypatch, store)
        rows.set(
            900,
            "running",
            push=_stored_record(
                job=900, launch=run_slice._launch_key(handle), sha="d" * 40
            ),
            outcome={"outcome": outcome},
        )
        _finish(handle, 0, json.dumps({"is_error": False, "result": "done"}))
        pctx = _poll_ctx(store, ctx)
        assert run_slice.poll(pctx, handle) is True
        assert pctx.status == ("succeeded" if ok else "failed"), outcome
        assert "vanished" not in str(pctx.failure)
        assert seams["lands"] == [] and "finalize" not in _names(seams)
        assert ("attempt:1" in store.tag_strings(42)) is (not ok)
        assert any("finished after a restart" in t for k, t in pctx.chunks)
        shutil.rmtree(tree, ignore_errors=True)


def test_a_vanished_tree_with_no_record_is_still_judged_vanished(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    shutil.rmtree(handle["worktree"])
    seams["alive"] = False
    rows = _Rows(monkeypatch, store)
    rows.set(900, "running")
    _finish(handle, 0, json.dumps({"is_error": False, "result": "done"}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed" and "vanished" in (pctx.failure or "")


@pytest.mark.parametrize(
    ("current_job", "floor", "stamped"),
    [
        (None, None, True),
        (60, None, True),
        (70, None, True),
        (80, None, False),
        (None, 75, False),
    ],
)
def test_a_late_settled_folder_never_replaces_a_newer_attempts_build_ref(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    current_job: int | None,
    floor: int | None,
    stamped: bool,
) -> None:
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    seams["alive"] = False
    seams["pushed"] = _pushed(job=70, sha=SNAPSHOT, by="worker")
    meta: dict[str, Any] = {}
    if current_job is not None:
        meta["build_ref"] = _recorded(job=current_job, sha="f" * 40)
    if floor is not None:
        meta["build_floor"] = floor
    store = _Store()
    store.add_todo(42, "t", meta=meta)
    rows = _Rows(monkeypatch, store)
    rows.set(70, "failed", compute_handle=dict(_HANDLE))
    assert _settle(store, tree) is not None
    assert len(rows.job_stamps(70)) == 1  # the job always keeps its record
    got = store.refs[42].meta.get("build_ref")
    if stamped:
        assert got is not None and got["sha"] == SNAPSHOT
    else:
        assert got == meta.get("build_ref")


def test_release_of_a_parked_first_launch_leaves_the_claim_to_the_job_that_holds_it(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(
        42, "t", status="doing", tags=("claimed-by:lane-claude-sub",)
    )  # another job's claim
    before = store.tag_strings(42)
    parked = {"not_before": "2030-01-01T00:00:00Z", "folder_wait": "slice-td42-j70"}
    run_slice.release(_Ctx(store, {"todo_id": 42}), parked)
    assert store.tag_strings(42) == before
    # an outage park has a lane: its release gives the claim back, as before
    run_slice.release(
        _Ctx(store, {"todo_id": 42}, lane="claude-sub"),
        {"not_before": "2030-01-01T00:00:00Z", "own_folder": True},
    )
    assert "claimed-by:lane-claude-sub" not in store.tag_strings(42)


def test_the_poll_of_a_job_that_is_already_terminal_writes_nothing_at_all(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the outage park, not the usage stamp, not a signal to the group, not
    even a relaunch: everything of a terminal job is the host sweep's."""
    from tests.test_run_slice import _QUOTA_RESULT, _codex_and_sub, _pool_failures

    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    codex, sub = _codex_and_sub()
    pools = _pool_failures(monkeypatch)
    use_lanes(monkeypatch, codex, sub)
    seams["alive"] = False
    rows = _Rows(monkeypatch, store)
    rows.set(900, "cancelled")
    tags = store.tag_strings(42)
    _finish(handle, 1, json.dumps(_QUOTA_RESULT) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.meta_set == {} and pctx.chunks == []
    assert store.tag_strings(42) == tags and pools == {}
    assert "pid" in handle and "not_before" not in handle
    # a session that still answers is not signalled either
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    handle.update(boot_id="boot", start_time=55)
    pctx2 = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx2, handle) is True
    assert seams["signals"] == [] and pctx2.chunks == []
    # and a parked launch is not relaunched
    launches = len(seams["launch"])
    assert run_slice.poll(_poll_ctx(store, ctx), {"not_before": "2000-01-01T00:00:00Z"})
    assert len(seams["launch"]) == launches


def test_a_pushed_folder_that_could_not_be_removed_is_not_reported_as_unsaved(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    store = _Store()
    store.add_todo(42, "t")
    seams["pushed_by_reason"]["relaunch"] = _pushed(sha=SNAPSHOT, wip=True, by="worker")
    seams["drop_ok"] = False
    ctx = _Ctx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, run_slice.Relaunch({"own_folder": True})) is None
    [kept] = [c for c in store.comments(42) if "was kept" in c]
    assert "it could not be removed" in kept
    assert "does not hold its work" not in kept
    assert "could not be removed" in (ctx.failure or "")
    assert "still holds work the hub does not" not in (ctx.failure or "")


def test_a_folder_that_cannot_be_removed_is_commented_once_whatever_the_details(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(7, "t")
    for detail in ("compose project x not down", "could not remove: busy"):
        run_slice._alert_kept_folders(
            store,
            tmp_path,
            [f"kept slice-td7-j3: could not be removed ({detail})"],
        )
    assert len([c for c in store.comments(7) if "was kept" in c]) == 1
    assert {a["fingerprint"] for a in _alerts(seams, "raise")} == {"slice-td7-j3"}


def test_a_leftover_is_alerted_with_the_step_to_remove_it_and_no_task_comment(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(7, "t")
    name = "slice-td7-j3.drop-123-abcd1234"
    note = (
        f"kept {name}: the leftover of a dropped folder could not be removed; "
        "it holds no work the hub lacks, remove it by hand"
    )
    for _ in range(2):
        run_slice._alert_kept_folders(store, tmp_path, [note])
    raised = _alerts(seams, "raise")
    assert {a["fingerprint"] for a in raised} == {name}
    detail = raised[0]["detail"]
    assert "remove it by hand" in detail and "another account's rights" in detail
    assert "git push" not in detail and "push" not in detail.lower().replace(
        "the push", ""
    )
    assert store.comments(7) == []


def test_tidy_shares_one_budget_and_rotates_which_checkout_goes_first(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    second = tmp_path / "second"
    monkeypatch.setenv(run_slice.SLICE_REPOS_ENV, json.dumps({"catpath": str(second)}))
    _builds_in(tmp_path, second)
    order: list[tuple[Path, float | None]] = []

    def sweep(root: Path, settle: Any, **kw: Any) -> list[str]:
        order.append((root, kw.get("budget_s")))
        return []

    monkeypatch.setattr(_slice_ws, "sweep", sweep)
    store = _Store()
    for _ in range(2):
        monkeypatch.setattr(run_slice, "_tidy_last", None)
        run_slice.tidy(store, None)
    assert [r for r, _b in order] == [tmp_path, second, second, tmp_path]
    assert all(b is not None and 0 < b <= run_slice.TIDY_BUDGET_S for _r, b in order)
    assert run_slice.TIDY_BUDGET_S == 60.0
    # the remaining time shrinks as the first root takes its share
    first, later = order[0][1], order[1][1]
    assert first is not None and later is not None and later <= first


def test_a_slow_checkout_cannot_starve_the_other(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import time

    second = tmp_path / "second"
    monkeypatch.setenv(run_slice.SLICE_REPOS_ENV, json.dumps({"catpath": str(second)}))
    _builds_in(tmp_path, second)
    monkeypatch.setattr(run_slice, "TIDY_BUDGET_S", 0.2)
    ran: list[Path] = []

    def sweep(root: Path, settle: Any, **kw: Any) -> list[str]:
        ran.append(root)
        time.sleep(0.25)  # the first one takes the whole budget
        return []

    monkeypatch.setattr(_slice_ws, "sweep", sweep)
    store = _Store()
    for _ in range(2):
        monkeypatch.setattr(run_slice, "_tidy_last", None)
        run_slice.tidy(store, None)
    assert ran == [tmp_path, second]  # one root per call, the other goes first next


def test_a_failing_report_for_one_checkout_does_not_skip_the_next(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    second = tmp_path / "second"
    monkeypatch.setenv(run_slice.SLICE_REPOS_ENV, json.dumps({"catpath": str(second)}))
    _builds_in(tmp_path, second)
    swept: list[Path] = []

    def sweep(root: Path, settle: Any, **kw: Any) -> list[str]:
        swept.append(root)
        return []

    def boom(store: Any, root: Path, notes: list[str]) -> None:
        raise RuntimeError("alerts down")

    monkeypatch.setattr(_slice_ws, "sweep", sweep)
    monkeypatch.setattr(run_slice, "_alert_kept_folders", boom)
    run_slice.tidy(_Store(), None)
    assert swept == [tmp_path, second]


# ── round 8: the review of round 7 ──────────────────────────────────────────


def _park_poll_ctx(
    store: _Store, seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[_Ctx, dict[str, Any], dict[str, Any]]:
    """A first launch, then an outage park by the poll: ``(ctx, handle,
    parked)`` with ``parked`` a copy of the parked handle, due now."""
    from tests.test_run_slice import _QUOTA_RESULT, _codex_and_sub, _pool_failures

    ctx, handle = _submit(store, seams)
    codex, sub = _codex_and_sub()
    _pool_failures(monkeypatch)
    use_lanes(monkeypatch, codex, sub)
    seams["alive"] = False
    _finish(handle, 1, json.dumps(_QUOTA_RESULT) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    parked = dict(handle)
    parked["not_before"] = "2000-01-01T00:00:00Z"
    return ctx, handle, parked


def test_a_launch_from_the_poll_has_a_run_dir_that_finds_its_session(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll's ctx has no ``submit_attempt``: its launches used to write no
    ``baseline``, ``pid`` or ``handle``, so nothing could find a session whose
    handle was never saved."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle, parked = _park_poll_ctx(store, seams, monkeypatch)
    tree = Path(handle["worktree"])
    seams["identity"] = ("boot", 55)  # the launch records it in the handle
    seams["alive"] = True
    rctx = _poll_ctx(store, ctx)
    rctx.meta["exclude_vendor"] = "anthropic"
    assert "submit_attempt" not in rctx.meta
    assert run_slice.poll(rctx, handle) is False
    attempt = Path(handle["exit_file"]).parent
    assert re.fullmatch(r"[0-9a-f]{32}", attempt.name)
    assert (attempt / "baseline").is_file() and (attempt / "handle").is_file()
    live = run_slice._live_session_for(42, tree)  # the real recovery, no stubs
    assert live is not None and live["pid"] == handle["pid"] == 4242
    # nobody else's folder is this session's
    assert run_slice._live_session_for(42, _job_tree(tree.parents[2], 42, 999)) is None


def test_a_relaunch_that_died_after_its_launch_is_adopted_not_launched_over(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker died between the launch and the save of the new handle: the
    stored handle is still the parked one. The next pass finds the session by
    its run dir and takes it over — no second session, no finalize, no drop,
    the stale record of the park cleared, the lease taken again."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle, parked = _park_poll_ctx(store, seams, monkeypatch)
    tree = Path(handle["worktree"])
    assert "claimed-by:lane-claude-sub" not in store.tag_strings(
        42
    )  # the park gave it back
    seams["identity"] = ("boot", 55)
    # the relaunch (from a poll-shaped ctx) launches ... (the old session is
    # dead; the new one runs from here on)
    rctx = _poll_ctx(store, ctx)
    rctx.meta["exclude_vendor"] = "anthropic"
    fresh = run_slice.submit(rctx, run_slice.Relaunch(dict(parked)))
    assert fresh is not None and len(seams["launch"]) == 2
    seams["alive"] = True
    (tree / "later.txt").write_text("the new session's work\n", encoding="utf-8")
    ws_before = len(seams["ws"])
    # ... and the worker dies before it saves ``fresh``: the next pass
    pctx = _poll_ctx(store, ctx)
    pctx.meta_set["push"] = {"state": "pushed", "stale": "the park's record"}
    assert run_slice.poll(pctx, parked) is False
    assert parked["pid"] == fresh["pid"] and "not_before" not in parked
    assert len(seams["launch"]) == 2  # not launched over
    assert {"finalize", "drop", "cut"}.isdisjoint(n for n, _ in seams["ws"][ws_before:])
    assert (tree / "later.txt").exists()
    assert pctx.meta_set["push"] is None
    assert pctx.meta_set["compute_handle"] == parked
    assert any("adopted the live session" in t for k, t in pctx.chunks)
    assert "claimed-by:lane-codex" in store.tag_strings(42)  # the lease, again


def test_a_first_launch_resumed_after_a_wait_is_adopted_by_ids_not_by_the_handle(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wait for an earlier folder drops ``worktree`` from the handle; the
    folder of a live session of this job is named by the task and job ids."""
    _earlier_folder(tmp_path)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.set(70, "failed", compute_handle=dict(_HANDLE))
    ctx, parked = _submit(store, seams)
    assert "worktree" not in parked and "not_before" in parked
    rows.set(
        70,
        "failed",
        compute_handle=dict(_HANDLE),
        push=_stored_record(launch=run_slice._launch_key(_HANDLE)),
    )
    rows.set(900, "running")
    seams["identity"] = ("boot", 55)
    seams["alive"] = True
    parked["not_before"] = "2000-01-01T00:00:00Z"
    stored_handle = dict(parked)  # what the database keeps if the save is lost
    rctx = _poll_ctx(store, ctx)
    assert run_slice.submit(rctx, run_slice.Relaunch(dict(parked))) is not None
    launches = len(seams["launch"])
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, stored_handle) is False
    assert stored_handle["pid"] == 4242 and "not_before" not in stored_handle
    assert len(seams["launch"]) == launches
    assert any("adopted the live session" in t for k, t in pctx.chunks)


def test_a_job_that_ended_while_its_handle_was_still_parked_keeps_its_folder(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancelled after a relaunch that was never saved: the sweep finds the
    session by its run dir, does not finalize or drop under it, and says so
    when it will not go."""
    import signal
    from datetime import UTC, datetime, timedelta

    store = _Store()
    store.add_todo(42, "t")
    ctx, handle, parked = _park_poll_ctx(store, seams, monkeypatch)
    tree = Path(handle["worktree"])
    seams["identity"] = ("boot", 55)
    rctx = _poll_ctx(store, ctx)
    rctx.meta["exclude_vendor"] = "anthropic"
    assert run_slice.submit(rctx, run_slice.Relaunch(dict(parked))) is not None
    seams["alive"] = True
    (tree / "later.txt").write_text("work\n", encoding="utf-8")
    _builds_in(tree.parents[2])
    rows = _Rows(monkeypatch, store)
    old = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    rows.set(900, "cancelled", compute_handle=dict(parked), finished_at=old)
    seams["ws"].clear()
    _sweep(monkeypatch, store)
    assert seams["signals"] == [(4242, signal.SIGKILL)]
    assert {"finalize", "drop"}.isdisjoint(_names(seams)) and tree.is_dir()
    long_ago = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    rows.set(900, "cancelled", compute_handle=dict(parked), finished_at=long_ago)
    _sweep(monkeypatch, store)
    assert any("still answers after the job ended" in c for c in store.comments(42))
    assert {"finalize", "drop"}.isdisjoint(_names(seams))
    stuck = [a for a in _alerts(seams, "raise") if a["fingerprint"] == tree.name]
    assert (
        stuck
        and "stop it" in stuck[-1]["detail"]
        and "git push" not in stuck[-1]["detail"]
    )


def test_the_bookkeeping_after_a_launch_never_loses_the_handle(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.add_todo(42, "t")

    def boom(*a: Any, **kw: Any) -> None:
        raise RuntimeError("database gone")

    monkeypatch.setattr(run_slice, "_resolve_lane_refusal", boom)
    monkeypatch.setattr(run_slice._run_features, "record", boom)

    class _Flaky(_Ctx):
        def append_chunk(self, kind: str, text: str) -> None:
            if "launched td42" in text:
                raise RuntimeError("chunk write failed")
            super().append_chunk(kind, text)

    ctx = _Flaky(store, {"todo_id": 42})
    handle = run_slice.submit(ctx, None)
    assert handle is not None and handle["pid"] == 4242 and ctx.failure is None
    assert len(seams["launch"]) == 1
    # and the process record itself cannot raise
    monkeypatch.setattr(run_slice, "_process_identity", boom)
    h: dict[str, Any] = {}
    run_slice._record_launched_process(h, 777, None)
    assert h["pid"] == 777 and h["pgid"] == 777


@pytest.mark.parametrize(
    ("handle", "seam", "alive", "group", "want"),
    [
        # Linux: the identity cannot be read now, the pid answers: alive
        (
            {"pid": 4242, "boot_id": "b", "start_time": 5},
            {"identity": None},
            True,
            False,
            True,
        ),
        # ... a positive mismatch is another process: not the session
        (
            {"pid": 4242, "boot_id": "b", "start_time": 5},
            {"identity": ("b", 6)},
            True,
            False,
            False,
        ),
        # ... unreadable and gone: dead
        (
            {"pid": 4242, "boot_id": "b", "start_time": 5},
            {"identity": None},
            False,
            False,
            False,
        ),
        # Darwin: ``ps`` failed or timed out
        ({"pid": 4242, "ps_identity": "x"}, {"ps_identity": None}, True, False, True),
        ({"pid": 4242, "ps_identity": "x"}, {"ps_identity": "y"}, True, False, False),
        ({"pid": 4242, "ps_identity": "x"}, {"ps_identity": None}, False, False, False),
        ({"pid": 4242, "ps_identity": "x"}, {"ps_identity": "x"}, True, False, True),
    ],
    ids=[
        "linux-unreadable",
        "linux-mismatch",
        "linux-unreadable-gone",
        "darwin-ps-failed",
        "darwin-mismatch",
        "darwin-ps-failed-gone",
        "darwin-match",
    ],
)
def test_a_session_whose_identity_cannot_be_read_is_alive_only_a_mismatch_is_not(
    seams: dict[str, Any],
    handle: dict[str, Any],
    seam: dict[str, Any],
    alive: bool,
    group: bool,
    want: bool,
) -> None:
    handle = {**handle, "pgid": 4242}
    seams.update(seam)
    seams["alive"] = alive
    seams["group_alive"] = group
    assert run_slice._session_alive(handle) is want


def test_a_folder_is_dropped_only_in_a_pass_that_finalized_it_a_newer_commit_is_saved(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stored safe record is not a licence to delete. The folder held work
    newer than the record: the fresh record is stamped on the job and on the
    task, with one job_event naming the sha that now holds it."""
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    _builds_in(tmp_path)
    seams["alive"] = False
    seams["pushed"] = _pushed(job=70, sha="b" * 40, by="worker")  # the newer work
    store = _Store()
    store.add_todo(42, "t")
    events: list[str] = []
    monkeypatch.setattr(
        run_slice, "_job_event", lambda s, job, text: events.append(text)
    )
    rows = _Rows(monkeypatch, store)
    older = _stored_record(job=70, sha="a" * 40, by="builder")
    rows.set(70, "failed", compute_handle={"worktree": str(tree)}, push=older)
    _sweep(monkeypatch, store)
    assert _finalizes(seams) == ["run ended"]
    assert rows.job_stamps(70)[0]["push"]["sha"] == "b" * 40
    assert store.refs[42].meta["build_ref"]["sha"] == "b" * 40
    assert len(events) == 1 and "newer than the stored record" in events[0]
    assert "b" * 12 in events[0]
    assert ("drop", tree) in seams["ws"]
    # a task that has a newer attempt's record keeps it: only the job is stamped
    tree2 = _job_tree(tmp_path, 42, 60)
    tree2.mkdir(parents=True)
    store2 = _Store()
    store2.add_todo(42, "t", meta={"build_ref": _recorded(job=80, sha="f" * 40)})
    rows2 = _Rows(monkeypatch, store2)
    rows2.set(
        60,
        "failed",
        compute_handle={"worktree": str(tree2)},
        push=_stored_record(job=60, sha="a" * 40),
    )
    seams["pushed"] = _pushed(job=60, sha="c" * 40, by="worker")
    _sweep(monkeypatch, store2)
    assert rows2.job_stamps(60)[0]["push"]["sha"] == "c" * 40
    assert store2.refs[42].meta["build_ref"]["sha"] == "f" * 40


def test_newer_work_that_cannot_be_pushed_is_stamped_failed_and_kept(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    _builds_in(tmp_path)
    seams["alive"] = False
    seams["pushed"] = _pushed(_slice_ws.FAILED, job=70, note="push timed out")
    store = _Store()
    store.add_todo(42, "t", meta={"build_ref": _recorded(job=70, sha="a" * 40)})
    rows = _Rows(monkeypatch, store)
    rows.set(
        70,
        "failed",
        compute_handle={"worktree": str(tree)},
        push=_stored_record(job=70, sha="a" * 40),
    )
    events: list[str] = []
    monkeypatch.setattr(
        run_slice, "_job_event", lambda s_, job, text: events.append(text)
    )
    _sweep(monkeypatch, store)
    assert rows.job_stamps(70)[0]["push"]["state"] == "failed"
    assert store.refs[42].meta["build_ref"]["sha"] == "a" * 40  # the saved one stays
    assert tree.is_dir() and "drop" not in _names(seams)
    assert any("was kept" in c for c in store.comments(42))
    # the words fit the state: not "newer work", a folder that could not be
    # checked, the note quoted, the stored sha named
    [event] = events
    assert "could not be checked against the hub (failed: push timed out)" in event
    assert "the stored record held " + "a" * 12 in event and "newer" not in event


def test_a_launch_does_not_wait_for_a_folder_the_sweep_will_never_settle(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An absent row, or a row of another task, is reported by the sweep and
    never settled: waiting for it would fail every launch of the task."""
    _earlier_folder(tmp_path)
    for setup in ("absent", "foreign"):
        store = _Store()
        store.add_todo(42, "t")
        rows = _Rows(monkeypatch, store)
        if setup == "absent":
            rows.gone.add(70)
        else:
            rows.rows[70] = ("failed", {"params": {"todo_id": 7}})
        shutil.rmtree(_job_tree(tmp_path, 42, 900), ignore_errors=True)
        ctx, handle = _submit(store, seams)
        assert handle is not None and "not_before" not in handle, setup
    # an unreadable row still counts as unsettled
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.unreadable = True
    shutil.rmtree(_job_tree(tmp_path, 42, 900), ignore_errors=True)
    ctx, handle = _submit(store, seams)
    assert "not_before" in handle


def _write_handle_record(
    attempt: Path,
    tree: Path,
    *,
    spelling: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """What a launch leaves in its run dir, in the real format."""
    attempt.mkdir(parents=True)
    named = spelling or str(tree)
    (attempt / "baseline").write_text(json.dumps({"worktree": named}), encoding="utf-8")
    (attempt / "handle").write_text(
        json.dumps(
            {
                "worktree": named,
                "pid": 4242,
                "pgid": 4242,
                "boot_id": "boot",
                "start_time": 55,
                "ps_identity": "Sat Oct 31 19:57:00 2026 slice-user",
                **(extra or {}),
            }
        ),
        encoding="utf-8",
    )
    (attempt / "pid").write_text("4242\n", encoding="utf-8")


def test_a_land_folder_with_a_running_ship_is_not_dropped_as_empty(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    land = _slice_ws.attempt_dir(tmp_path, 42, 70, "land")
    land.mkdir(parents=True)
    _write_handle_record(tmp_path / "ops" / "td42" / ("c" * 32), land)
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.set(70, "failed")  # no saved handle: the launch raised
    assert _settle(store, land, role="land") is None  # not EMPTY: a ship runs
    seams["alive"] = False  # ... and once it is gone the folder holds nothing
    got = _settle(store, land, role="land")
    assert got is not None and got.state == _slice_ws.EMPTY


def test_a_live_session_is_found_through_a_symlinked_checkout_path(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    real = tmp_path / "real"
    link = tmp_path / "link"
    link.symlink_to(real)
    folder = _slice_ws.attempt_dir(real, 42, 70)
    folder.mkdir(parents=True)
    spelled = str(_slice_ws.attempt_dir(link, 42, 70))  # the launch's spelling
    _write_handle_record(
        tmp_path / "ops" / "td42" / ("d" * 32), folder, spelling=spelled
    )
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    assert run_slice._live_session_for(42, folder) is not None
    assert run_slice._live_session_for(42, _slice_ws.attempt_dir(real, 42, 71)) is None


def test_the_host_roots_are_resolved_like_the_launch_resolves_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv(run_slice.SLICE_REPOS_ENV, json.dumps({"x": str(link)}))
    assert real.resolve() in run_slice._host_roots()
    assert link not in run_slice._host_roots()


def test_the_launch_wait_covers_the_retry_schedule_and_its_words_name_the_state(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import time

    spent = sum(run_slice.PUSH_RETRY_AFTER_S)
    per_try = run_slice.TIDY_EVERY_S + run_slice.PUSH_TRY_ALLOWANCE_S
    assert (
        spent + len(run_slice.PUSH_RETRY_AFTER_S) * per_try
        <= run_slice.EARLIER_FOLDER_WAIT_S
    )
    folder = _earlier_folder(tmp_path)
    cases = {
        "its job has no push record": ("failed", {}),
        "its job is still running": ("running", {}),
        "its push failed and 2 retries are left": (
            "failed",
            {
                "push": {
                    "state": "failed",
                    "tries": 2,
                    "launch": run_slice._launch_key(_HANDLE),
                }
            },
        ),
    }
    for words, (status, extra) in cases.items():
        store = _Store()
        store.add_todo(42, "t")
        rows = _Rows(monkeypatch, store)
        rows.set(70, status, compute_handle=dict(_HANDLE), **extra)
        rows.set(900, "running")
        ctx = _Ctx(store, {"todo_id": 42})
        parked = {
            "not_before": "2000-01-01T00:00:00Z",
            "folder_wait_since": time.time() - run_slice.EARLIER_FOLDER_WAIT_S - 5,
            "folder_wait": folder.name,
        }
        assert run_slice.poll(ctx, parked) is True
        assert words in (ctx.failure or ""), (words, ctx.failure)


def test_a_folder_gone_with_this_launchs_record_but_no_outcome_burns_no_attempt(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outcome stamp's exception is swallowed, the drop runs, the worker
    dies: judging that a vanished tree would burn an attempt although a land
    may have been minted."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    shutil.rmtree(handle["worktree"])
    seams["alive"] = False
    rows = _Rows(monkeypatch, store)
    rows.set(
        900,
        "running",
        push=_stored_record(job=900, launch=run_slice._launch_key(handle)),
    )
    _finish(handle, 0, json.dumps({"is_error": False, "result": "done"}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
    assert "left no outcome" in (pctx.failure or "") and "vanished" not in (
        pctx.failure or ""
    )
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))
    assert "STATUS:open" in store.tag_strings(42)


def test_release_of_a_parked_first_launch_with_an_old_lane_still_leaves_the_claim(
    seams: dict[str, Any],
) -> None:
    """A requeued job can carry an earlier run's ``meta.lane``: the early
    return keys on the parked handle, not on the lane."""
    store = _Store()
    store.add_todo(42, "t", status="doing", tags=("claimed-by:lane-claude-sub",))
    before = store.tag_strings(42)
    parked = {"not_before": "2030-01-01T00:00:00Z", "folder_wait": "slice-td42-j70"}
    run_slice.release(_Ctx(store, {"todo_id": 42}, lane="claude-sub"), parked)
    assert store.tag_strings(42) == before
    # the same handle of an outage relaunch that waits (own_folder) is released
    run_slice.release(
        _Ctx(store, {"todo_id": 42}, lane="claude-sub"), {**parked, "own_folder": True}
    )
    assert "claimed-by:lane-claude-sub" not in store.tag_strings(42)


def test_a_pushed_folder_that_could_not_be_removed_alerts_to_remove_it_not_to_push(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42)
    tree.mkdir(parents=True)
    store = _Store()
    store.add_todo(42, "t")
    seams["pushed_by_reason"]["relaunch"] = _pushed(sha=SNAPSHOT, wip=True, by="worker")
    seams["drop_ok"] = False
    ctx = _Ctx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, run_slice.Relaunch({"own_folder": True})) is None
    [raised] = _alerts(seams, "raise")
    detail = raised["detail"]
    assert "work is in the host's hub" in detail and "remove it by hand" in detail
    assert "git push" not in detail and "not (or not provably)" not in detail
    # the sweep's note about the same folder says the same
    seams["alerts"].clear()
    run_slice._alert_kept_folders(
        store, tmp_path, [f"kept {tree.name}: could not be removed (busy)"]
    )
    [again] = _alerts(seams, "raise")
    assert "remove it by hand" in again["detail"] and "git push" not in again["detail"]


def test_the_poll_of_a_terminal_job_still_stops_its_detached_acceptance_group(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Item 11: the wrapper stops the dead job's own test group (a child nobody
    else would stop); the words of the poll say exactly that."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    handle["acceptance_handle"] = {
        "pid": 5151,
        "pgid": 5151,
        "exit_file": str(tmp_path / "acceptance.exit"),  # never written: running
    }
    killed: list[dict[str, Any]] = []
    monkeypatch.setattr(run_slice, "_kill_acceptance", lambda h: killed.append(h))
    rows = _Rows(monkeypatch, store)
    rows.set(900, "cancelled")
    seams["alive"] = False
    assert run_slice.poll(_poll_ctx(store, ctx), handle) is True
    assert killed == [handle] and seams["signals"] == []


# ── round 9: the sixth review ─────────────────────────────────────────────────


def _unreadable(seams: dict[str, Any]) -> None:
    """Both platforms' identity reads fail (``ps`` timed out, ``/proc`` bad)."""
    seams["identity"] = None
    seams["ps_identity"] = None
    seams["alive"] = True


def test_the_run_dir_lookup_finds_a_session_whose_identity_cannot_be_read(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42, 900)
    tree.mkdir(parents=True)
    _write_handle_record(tmp_path / "ops" / "td42" / ("a" * 32), tree)
    _unreadable(seams)
    live = run_slice._live_session_for(42, tree)
    assert live is not None and live["pid"] == 4242
    # a mismatch is another process; a gone pid is nobody
    seams["ps_identity"] = "somebody else"
    seams["identity"] = ("boot", 99)
    assert run_slice._live_session_for(42, tree) is None
    _unreadable(seams)
    seams["alive"] = False
    assert run_slice._live_session_for(42, tree) is None


def test_an_unreadable_identity_holds_the_folder_at_the_relaunch_the_open_and_the_sweep(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ps`` times out: the recorded identity reads back as nothing while the
    pid answers. The lookup used to say "no session", and each caller then
    finalized and dropped the folder of a running one."""
    import signal
    from datetime import UTC, datetime, timedelta

    tree = _job_tree(tmp_path, 42, 900)
    tree.mkdir(parents=True)
    (tree / "work.txt").write_text("a running session's\n", encoding="utf-8")
    _builds_in(tmp_path)
    _write_handle_record(tmp_path / "ops" / "td42" / ("a" * 32), tree)
    _unreadable(seams)
    store = _Store()
    store.add_todo(42, "t")
    # the open (the relaunch's own-folder branch) refuses, folder untouched
    ctx = _Ctx(store, {"todo_id": 42})
    assert run_slice.submit(ctx, run_slice.Relaunch({"own_folder": True})) is None
    assert "still running" in (ctx.failure or "")
    assert {"finalize", "drop", "cut"}.isdisjoint(_names(seams))
    # the relaunch (the poll of a parked handle due now) adopts it
    parked = {
        "worktree": str(tree),
        "own_folder": True,
        "not_before": "2000-01-01T00:00:00Z",
    }
    pctx = _Ctx(store, {"todo_id": 42}, lane="claude-sub")
    assert run_slice.poll(pctx, parked) is False
    assert parked["pid"] == 4242 and seams["launch"] == []
    assert {"finalize", "drop", "cut"}.isdisjoint(_names(seams))
    # the sweep of a terminal job keeps it and sends no signal (nothing in the
    # pass says the number is still the session's)
    rows = _Rows(monkeypatch, store)
    old = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    rows.set(900, "cancelled", compute_handle={"worktree": str(tree)}, finished_at=old)
    _sweep(monkeypatch, store)
    assert {"finalize", "drop"}.isdisjoint(_names(seams)) and tree.is_dir()
    assert seams["signals"] == []
    # a later pass that reads the identity signals
    seams["ps_identity"] = "Sat Oct 31 19:57:00 2026 slice-user"
    _sweep(monkeypatch, store)
    assert seams["signals"] == [(4242, signal.SIGKILL)]


def test_a_confirm_that_cannot_reach_the_hub_does_not_cost_the_run_its_record(
    seams: dict[str, Any],
) -> None:
    """The adopted record is safe, the fresh finalize is not (a push timeout):
    the arms go on with the stored sha — the land is minted from it, no attempt
    burned — the folder is kept, and nothing is stamped: a not-safe record
    stamped here would be adopted by the next pass if this one died before it
    ended the job (the sweep's own confirm stamps it once the job is terminal).
    A second pass after a first that died before ``set_status`` mints the same
    land again (the idem key makes it one), burns no attempt and leaves the
    stored record."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    seams["alive"] = False
    seams["pushed"] = _pushed(_slice_ws.FAILED, note="push timed out")
    mine = {
        **_pushed(sha="d" * 40, by="worker").as_meta(),
        "launch": run_slice._launch_key(handle),
    }
    seams["job_row"] = ("running", {"push": mine})

    # the first pass dies after the land was minted, before the job ended
    class _Dies(_Ctx):
        def set_status(self, value: str) -> None:
            raise RuntimeError("worker killed")

    dying = _Dies(store, ctx.meta["params"], **ctx.meta_set)
    _finish(handle, 0, json.dumps({"is_error": False, "result": "done.\ntests: ran"}))
    with pytest.raises(RuntimeError, match="worker killed"):
        run_slice.poll(dying, handle)
    assert "push" not in dying.meta_set  # nothing stamped for the next pass
    assert len(seams["land_kw"]) == 1
    pctx = _poll_with(store, seams, ctx, handle)
    assert pctx.status == "succeeded" and "attempt:1" not in store.tag_strings(42)
    assert "push" not in pctx.meta_set
    assert seams["land_kw"] == 2 * [
        {"ref": "refs/factory/build/42/900", "head": "d" * 40}
    ]  # the same land, from the stored sha, both times
    assert tree.is_dir() and "drop" not in _names(seams)  # kept, not dropped
    [event] = [t for k, t in pctx.chunks if k == "job_event" and "checked" in t]
    assert "could not be checked against the hub (failed: push timed out)" in event
    assert "the stored record held " + "d" * 12 in event
    assert "newer" not in event
    # the kept report is the sweep's, once the job is terminal (it confirms,
    # stamps the not-safe record and starts the retry schedule)
    assert not any("was kept" in c for c in store.comments(42))


def test_a_confirm_that_finds_newer_work_says_it_is_newer(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    seams["alive"] = False
    seams["pushed"] = _pushed(sha="e" * 40, by="worker")
    mine = {
        **_pushed(sha="d" * 40, by="worker").as_meta(),
        "launch": run_slice._launch_key(handle),
    }
    seams["job_row"] = ("running", {"push": mine})
    pctx = _poll_with(store, seams, ctx, handle)
    assert seams["land_kw"] == [{"ref": "refs/factory/build/42/900", "head": "e" * 40}]
    [event] = [t for k, t in pctx.chunks if k == "job_event" and "newer" in t]
    assert "newer than the stored record, now held as " + "e" * 12 in event


def test_a_land_pending_for_review_keeps_the_tasks_status_when_the_run_ends_without_outcome(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.store.types import Tag

    for case in ("pending_land", "land_job", "neither"):
        meta = {"pending_land": {"todo_id": 42}} if case == "pending_land" else {}
        store = _Store()
        store.add_todo(42, "t", meta=meta)
        ctx, handle = _submit(store, seams)
        # the arms held the land for review (STATUS:done, claim dropped by
        # the hold), or minted it (the lease stays, STATUS:doing, until the
        # land, as the success arm leaves it), or did neither
        held = "done" if case == "pending_land" else "doing"
        store.add_tag(42, Tag.closed("STATUS", held), replace_prefix=True)
        shutil.rmtree(handle["worktree"])
        seams["alive"] = False
        rows = _Rows(monkeypatch, store)
        job_meta = {"land_job": 777} if case == "land_job" else {}
        rows.set(
            900,
            "running",
            push=_stored_record(job=900, launch=run_slice._launch_key(handle)),
            **job_meta,
        )
        _finish(handle, 0, json.dumps({"is_error": False, "result": "done"}))
        pctx = _poll_ctx(store, ctx)
        assert run_slice.poll(pctx, handle) is True, case
        assert pctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "workspace"
        tags = store.tag_strings(42)
        claimed = any(t.startswith("claimed-by:") for t in tags)
        if case == "neither":
            assert "STATUS:open" in tags and not claimed
            assert "todo back to open" in (pctx.failure or "")
        else:
            assert f"STATUS:{held}" in tags and "STATUS:open" not in tags, case
            assert "todo back to open" not in (pctx.failure or ""), case
        # a minted land keeps its lease; a held one has none
        assert claimed is (case == "land_job"), case


def test_the_sweep_looks_at_the_run_dir_when_the_row_handle_is_a_dead_one(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relaunch whose handle save failed leaves the row naming the old,
    dead handle (a pid and an identity); the new session runs in the folder.
    The same for a land's ship."""
    import signal
    from datetime import UTC, datetime, timedelta

    _builds_in(tmp_path)
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    dead = {"pid": 1111, "pgid": 1111, "boot_id": "boot", "start_time": 99}
    old = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    build = _job_tree(tmp_path, 42, 900)
    build.mkdir(parents=True)
    land = _slice_ws.attempt_dir(tmp_path, 42, 901, "land")
    land.mkdir(parents=True)
    _write_handle_record(tmp_path / "ops" / "td42" / ("a" * 32), build)
    _write_handle_record(tmp_path / "ops" / "td42" / ("b" * 32), land)
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    rows.set(900, "failed", compute_handle=dict(dead), finished_at=old)
    rows.set(901, "failed", compute_handle=dict(dead), finished_at=old)
    _sweep(monkeypatch, store)
    assert {"finalize", "drop"}.isdisjoint(_names(seams))
    assert build.is_dir() and land.is_dir()
    assert seams["signals"] == [(4242, signal.SIGKILL)] * 2  # each live session


def test_one_identity_read_decides_a_signal_and_a_race_between_reads_sends_none(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = {"pid": 4242, "pgid": 4242, "boot_id": "boot", "start_time": 55}
    cases = [
        (("boot", 55), True, "match", True),
        (None, True, "unreadable", False),
        (("boot", 99), True, "mismatch", False),  # re-led: the group is not ours
        (None, False, "gone", False),
    ]
    for identity, alive, state, ok in cases:
        seams["identity"] = identity
        seams["alive"] = alive
        seams["group_alive"] = False
        assert run_slice._leader_state(handle) == state
        assert run_slice._signal_target_ok(handle) is ok
    # a mismatch whose group is still led by survivors is ours
    seams["identity"], seams["alive"], seams["group_alive"] = ("boot", 99), True, True
    assert run_slice._signal_target_ok(handle) is True
    # the race: the liveness read times out (the folder is held), the signal's
    # own read finds another process leading the number's group: no signal
    reads = iter([None, ("boot", 99)])
    monkeypatch.setattr(run_slice, "_process_identity", lambda pid: next(reads))
    seams["alive"], seams["group_alive"] = True, False
    assert run_slice._session_alive(handle) is True
    assert run_slice._signal_target_ok(handle) is False


def test_the_acceptance_group_is_killed_only_where_one_read_says_it_is_ours(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    seams["alive"] = True
    with_identity = {"pid": 5151, "pgid": 5151, "boot_id": "boot", "start_time": 55}
    run_slice._kill_acceptance({"acceptance_handle": dict(with_identity)})
    assert seams["signals"] == []  # identity unreadable: nothing goes out
    seams["identity"] = ("boot", 55)
    run_slice._kill_acceptance({"acceptance_handle": dict(with_identity)})
    assert seams["signals"] == [(5151, signal.SIGKILL)]
    seams["signals"].clear()
    # a handle that recorded no identity is never signalled: a bare pid may be
    # another process's by now (the deadline path cannot end such a group; it
    # ends on its own, and the poll holds the folder meanwhile)
    seams["group_alive"] = True  # whoever now holds the number answers
    run_slice._kill_acceptance({"acceptance_handle": {"pid": 5152, "pgid": 5152}})
    assert seams["signals"] == []
    seams["group_alive"] = False

    # an error from the signal (another user's group) never escapes
    def refused(pgid: int, sig: int) -> None:
        raise PermissionError("another user's group")

    monkeypatch.setattr(run_slice, "_signal_group", refused)
    run_slice._kill_acceptance({"acceptance_handle": dict(with_identity)})


def test_a_terminal_jobs_poll_stops_its_acceptance_group_through_the_real_gate(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import signal

    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    handle["acceptance_handle"] = {
        "pid": 5151,
        "pgid": 5151,
        "boot_id": "boot",
        "start_time": 55,
        "exit_file": str(tmp_path / "acceptance.exit"),  # never written
    }
    rows = _Rows(monkeypatch, store)
    rows.set(900, "cancelled")
    seams["alive"] = False
    seams["identity"] = ("boot", 55)
    # the session itself is dead (alive False covers both); the acceptance
    # group answers by the real liveness rule
    seams["alive"] = True
    seams["leader_after_exit"] = True
    _finish(handle, 0, "")
    assert run_slice.poll(_poll_ctx(store, ctx), handle) is True
    assert (5151, signal.SIGKILL) in seams["signals"]
    seams["signals"].clear()
    seams["identity"] = None  # unreadable: the same poll sends nothing
    assert run_slice.poll(_poll_ctx(store, ctx), handle) is True
    assert seams["signals"] == []


def test_the_sweep_keeps_a_folder_under_a_running_acceptance_group(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import signal
    from datetime import UTC, datetime, timedelta

    tree = _job_tree(tmp_path, 42, 70)
    tree.mkdir(parents=True)
    _builds_in(tmp_path)
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    # the session's pid was reused (a mismatch: dead); its test group runs
    row_handle = {
        "pid": 1111,
        "pgid": 1111,
        "boot_id": "boot",
        "start_time": 99,
        # the shape ``_devin_acceptance_tests`` writes (see
        # ``test_the_acceptance_launch_records_the_identity_every_signal_is_gated_on``)
        "acceptance_handle": {
            "pid": 5151,
            "boot_id": "boot",
            "start_time": 55,
            "log": str(tmp_path / "acceptance.log"),
            "exit_file": str(tmp_path / "acceptance.exit"),
            "started": 0.0,
        },
    }
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    soon = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    rows.set(70, "failed", compute_handle=row_handle, finished_at=soon)
    _sweep(monkeypatch, store)
    assert seams["signals"] == [] and tree.is_dir()
    assert {"finalize", "drop"}.isdisjoint(_names(seams))
    old = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    rows.set(70, "failed", compute_handle=row_handle, finished_at=old)
    _sweep(monkeypatch, store)
    assert seams["signals"] == [(5151, signal.SIGKILL)]  # the 30 s rule, gated
    assert {"finalize", "drop"}.isdisjoint(_names(seams))
    long_ago = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    rows.set(70, "failed", compute_handle=row_handle, finished_at=long_ago)
    _sweep(monkeypatch, store)
    assert any("acceptance-test group still answers" in c for c in store.comments(42))
    stuck = [a for a in _alerts(seams, "raise") if a["fingerprint"] == tree.name]
    assert stuck and "stop it" in stuck[-1]["detail"]


def test_release_of_a_parked_first_launch_that_lost_its_new_handle_releases_the_claim(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A first launch parked for an earlier folder relaunched, claimed and
    launched, lost the save of the new handle, and the deadline ended the job:
    the session the run dir names holds the claim and the grant."""
    tree = _job_tree(tmp_path, 42, 900)
    tree.mkdir(parents=True)
    _write_handle_record(
        tmp_path / "ops" / "td42" / ("a" * 32),
        tree,
        extra={
            "slice_token_dir": str(tmp_path / "tokens"),
            "slice_token_name": "td42j900",
        },
    )
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    revoked: list[Any] = []
    monkeypatch.setattr(
        run_slice, "_revoke_slice_grant", lambda ctx, t, g, st: revoked.append(g)
    )
    store = _Store()
    store.add_todo(42, "t", status="doing", tags=("claimed-by:lane-claude-sub",))
    parked = {"not_before": "2030-01-01T00:00:00Z", "folder_wait": "slice-td42-j70"}
    run_slice.release(_Ctx(store, {"todo_id": 42}, lane="claude-sub"), parked)
    assert revoked and revoked[0]["name"] == "td42j900"
    assert "claimed-by:lane-claude-sub" not in store.tag_strings(42)
    # no live session of this job: nothing to give back, the claim is another's
    seams["alive"] = False
    store2 = _Store()
    store2.add_todo(42, "t", status="doing", tags=("claimed-by:lane-claude-sub",))
    before = store2.tag_strings(42)
    revoked.clear()
    run_slice.release(_Ctx(store2, {"todo_id": 42}, lane="claude-sub"), dict(parked))
    assert store2.tag_strings(42) == before and revoked == []


def test_a_bound_on_a_running_or_unreadable_job_does_not_say_push_it(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import time

    folder = _earlier_folder(tmp_path)
    cases = {
        "running": ("running", False, "find out why that job has not ended"),
        "unread": (None, True, "check this worker's database access"),
        "work": ("failed", False, "git push"),
    }
    for name, (status, unreadable, words) in cases.items():
        store = _Store()
        store.add_todo(42, "t")
        rows = _Rows(monkeypatch, store)
        rows.unreadable = unreadable
        if status is not None:
            rows.set(70, status, compute_handle=dict(_HANDLE))
        rows.rows[900] = ("running", {"params": {"todo_id": 42}})
        seams["alerts"].clear()
        ctx = _Ctx(store, {"todo_id": 42})
        parked = {
            "not_before": "2000-01-01T00:00:00Z",
            "folder_wait_since": time.time() - run_slice.EARLIER_FOLDER_WAIT_S - 5,
            "folder_wait": folder.name,
        }
        if unreadable:
            rows.rows[900] = ("running", {"params": {"todo_id": 42}})
            rows.unreadable = False  # the poll's own row is readable
            monkeypatch.setattr(
                run_slice,
                "_job_row",
                lambda s, j: (
                    None if j == 70 else ("running", {"params": {"todo_id": 42}})
                ),
            )
        assert run_slice.poll(ctx, parked) is True, name
        [raised] = _alerts(seams, "raise")
        assert words in raised["detail"], (name, raised["detail"])
        if name != "work":
            assert "git push" not in raised["detail"], name


def test_a_sweep_failure_note_gets_its_own_next_step(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(7, "t")
    run_slice._alert_kept_folders(
        store, tmp_path, ["kept slice-td7-j3: sweep failed (RuntimeError: boom)"]
    )
    [raised] = _alerts(seams, "raise")
    assert "sweep failed on it" in raised["detail"]
    assert "worker's log" in raised["detail"] and "git push" not in raised["detail"]


def test_the_sweep_judges_an_acceptance_group_by_its_identity_and_its_exit(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No identity: a bare pid may be any process later, so nothing is signalled
    and the folder is held only while the tests could still run (their own
    timeout bounds it). An exit file marks the leader's end, not the group's:
    with an identity the group alone decides — a lingering child holds the
    folder and is killed after the 30 s rule, a gone group holds nothing."""
    import signal
    import time
    from datetime import UTC, datetime, timedelta

    _builds_in(tmp_path)
    seams["alive"] = True
    seams["identity"] = ("boot", 55)
    old = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    exited = tmp_path / "acceptance.exit"
    exited.write_text("0\n", encoding="utf-8")
    ident = {"boot_id": "boot", "start_time": 55}
    cases = {
        # no identity, started long ago: nobody the sweep can name
        70: {"pid": 5151, "started": 0.0, "exit_file": str(tmp_path / "none.exit")},
        # no identity, started just now: held (bounded), never signalled
        72: {
            "pid": 5153,
            "started": time.time(),
            "exit_file": str(tmp_path / "none.exit"),
        },
        # identity, exit file, a child of the group lingers: held and killed
        71: {"pid": 5152, **ident, "exit_file": str(exited)},
        # identity, exit file, the group is gone: nothing to hold
        73: {"pid": 5154, **ident, "exit_file": str(exited)},
    }
    store = _Store()
    store.add_todo(42, "t")
    rows = _Rows(monkeypatch, store)
    for job, acceptance in cases.items():
        _job_tree(tmp_path, 42, job).mkdir(parents=True)
        dead = {"pid": 1111, "pgid": 1111, "boot_id": "boot", "start_time": 99}
        rows.set(
            job,
            "failed",
            compute_handle={**dead, "acceptance_handle": acceptance},
            finished_at=old,
        )
    # the group answers for pids 5152 only
    monkeypatch.setattr(run_slice, "_group_alive", lambda pgid: pgid == 5152)
    _sweep(monkeypatch, store)
    assert seams["signals"] == [(5152, signal.SIGKILL)]
    assert sorted(d["job"] for n, d in seams["ws"] if n == "finalize") == [70, 73]


def test_the_poll_does_not_drop_a_folder_while_its_acceptance_group_answers(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    seams["ahead"] = "abc1234 fix(x): y (td42)\n"
    seams["alive"] = False
    handle["acceptance_handle"] = {"pid": 5151, "boot_id": "boot", "start_time": 55}
    monkeypatch.setattr(
        run_slice, "_session_alive", lambda h: bool(h.get("start_time") == 55)
    )
    monkeypatch.setattr(run_slice, "_kill_acceptance", lambda h: None)
    _poll_with(store, seams, ctx, handle)
    assert "finalize" in _names(seams)  # the work is saved ...
    assert "drop" not in _names(seams) and tree.is_dir()  # ... the folder stays


def test_an_exit_file_beats_an_unreadable_identity_in_the_run_dir_lookup(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    tree = _job_tree(tmp_path, 42, 900)
    tree.mkdir(parents=True)
    exit_file = tmp_path / "exit"
    exit_file.write_text("0\n", encoding="utf-8")
    attempt = tmp_path / "ops" / "td42" / ("a" * 32)
    _write_handle_record(attempt, tree, extra={"exit_file": str(exit_file)})
    seams["identity"] = None
    seams["ps_identity"] = None
    seams["alive"] = True
    got = run_slice._recover_attempt(attempt)
    assert got is not None and got.get("recovered_exit") is True
    exit_file.unlink()
    got = run_slice._recover_attempt(attempt)
    assert got is not None and "recovered_exit" not in got  # no exit: the session


def test_the_confirm_words_fit_every_state() -> None:
    stored = {"sha": "a" * 40}
    safe = _pushed(sha="b" * 40)
    assert "now held as " + "b" * 12 in run_slice._confirm_words(stored, safe)
    empty = _slice_ws.Pushed(state=_slice_ws.EMPTY, ref="r")
    words = run_slice._confirm_words(stored, empty)
    assert "None" not in words and "holds nothing the hub lacks" in words
    bad = _slice_ws.Pushed(state=_slice_ws.FAILED, ref="r", note="timed out")
    assert "could not be checked" in run_slice._confirm_words(stored, bad)


def test_a_ps_that_failed_at_the_acceptance_launch_leaves_no_none_identity_key(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ps`` fails or times out at launch on macOS: a stored ``ps_identity:
    None`` sent ``_session_alive`` to the group check, false for a live leader,
    so running tests read as gone and the folder was dropped under them."""
    monkeypatch.setattr(run_slice.sys, "platform", "darwin")
    seams["ps_identity"] = None
    seams["identity"] = None
    handle: dict[str, Any] = {
        "exit_file": str(tmp_path / "ops" / "run.exit"),
        "acceptance_tests": ["tests/test_x.py"],
    }
    (tmp_path / "ops").mkdir()
    monkeypatch.setattr(run_slice, "_slice_test_exists", lambda tree, p: True)
    monkeypatch.setattr(run_slice, "_acceptance_env", lambda h: {})
    run_slice._devin_acceptance_tests(tmp_path, handle, _Ctx(_Store(), {"todo_id": 42}))
    test_handle = handle["acceptance_handle"]
    assert "ps_identity" not in test_handle
    seams["alive"] = True
    assert run_slice._session_alive(test_handle) is True  # the pid answers
