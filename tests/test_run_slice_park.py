# ruff: noqa: F811
"""Park = save, resume anywhere, at the ``run_slice`` seams: the failed end
of a run saves the work, and a re-armed parked node launches into its saved
branch (the git behaviour itself is ``tests/test_slice_park.py``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from precis.workers.job_types import _slice_park, run_slice
from tests.test_run_slice import (  # noqa: F401  (``seams`` is a fixture)
    _finish,
    _poll_ctx,
    _Store,
    _submit,
    seams,
)

_RECORD = {
    "host": "build-host-1",
    "worktree": "/tmp/wt",
    "head": "f" * 40,
    "reason": "x",
    "at": "2026-11-16T10:00:00Z",
    "wip": True,
    "push": "branch push of slice/42: on origin",
}


def test_a_failed_end_saves_the_work_before_the_tick_sees_it(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[Path, int, str, str]] = []

    def save(tree: Path, todo_id: int, reason: str, *, base: str, **_k: Any) -> Any:
        saved.append((tree, todo_id, reason, base))
        return dict(_RECORD)

    monkeypatch.setattr(_slice_park, "save_work", save)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Nothing to do."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    [(tree, todo_id, reason, base)] = saved
    assert (str(tree), todo_id, base) == (handle["worktree"], 42, "origin/main")
    assert "nothing to land" in reason
    assert pctx.meta_set["saved"] == _RECORD
    assert any(
        k == "job_event" and t.startswith("park-save: ffffffffffff")
        for k, t in pctx.chunks
    )


def test_a_blocked_commit_the_worker_cannot_finish_still_park_saves(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr702644: the worker's own commit of a blocked session tree can
    fail too — the dirty tree still park-saves like every failed run, so
    a parked resume keeps the wip instead of relaunching empty."""
    saved: list[tuple[Path, int, str, str]] = []

    def save(tree: Path, todo_id: int, reason: str, *, base: str, **_k: Any) -> Any:
        saved.append((tree, todo_id, reason, base))
        return dict(_RECORD)

    monkeypatch.setattr(_slice_park, "save_work", save)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["porcelain"] = " M a.py\n"
    seams["staged"] = True
    seams["commit_rc"] = 1
    _finish(
        handle,
        0,
        json.dumps(
            {
                "is_error": False,
                "result": "Commit blocked by read-only `.git`.\ntests: ran",
            }
        ),
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    # The blocked report reached the worker commit first (fallback subject).
    assert ["add", "-A"] in [a for a, _cwd in seams["git"]]
    assert any(a[:1] == ["commit"] for a, _cwd in seams["git"])
    [(tree, todo_id, reason, base)] = saved
    assert (str(tree), todo_id, base) == (handle["worktree"], 42, "origin/main")
    assert "nothing to land" in reason
    assert pctx.meta_set[run_slice.NO_COMMIT_KEY] is True
    assert pctx.meta_set["saved"] == _RECORD


def test_a_clean_end_that_lands_saves_nothing(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[int] = []

    def save_nothing(*_a: Any, **_k: Any) -> None:
        called.append(1)

    monkeypatch.setattr(_slice_park, "save_work", save_nothing)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix: the thing\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "done. tests: ran"}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and called == []


def test_a_failed_save_never_masks_the_failure_and_is_logged_on_the_node(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("origin unreachable")

    monkeypatch.setattr(_slice_park, "save_work", boom)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Nothing to do."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.failure is not None and "nothing to land" in pctx.failure
    assert "saved" not in pctx.meta_set
    assert any(
        k == "job_event" and t.startswith("park-save FAILED") for k, t in pctx.chunks
    )
    assert any("park-save FAILED" in c for c in store.comments(42))
    assert {"STATUS:open", "attempt:1"} <= store.tag_strings(42)


def test_a_vanished_worktree_has_nothing_to_save(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[int] = []

    def save_nothing(*_a: Any, **_k: Any) -> None:
        called.append(1)

    monkeypatch.setattr(_slice_park, "save_work", save_nothing)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    pctx = _poll_ctx(store, ctx)
    import shutil

    shutil.rmtree(handle["worktree"])
    assert run_slice.poll(pctx, handle) is True and called == []


def _capture_ensure(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    real = run_slice._ensure_worktree

    def spy(root: Path, todo_id: int, *a: Any, **kw: Any) -> Path:
        # ``store``, ``job_id`` and ``notes`` are plumbing, not park semantics.
        seen.append(
            {k: v for k, v in kw.items() if k not in ("notes", "store", "job_id")}
        )
        kw["notes"].append("resumed from " + "e" * 40)
        return real(root, todo_id, *a, **kw)

    monkeypatch.setattr(run_slice, "_ensure_worktree", spy)
    return seen


def test_submit_resumes_a_parked_node_from_its_saved_branch(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_ensure(monkeypatch)
    store = _Store()
    store.add_todo(42, "t", meta={"parked": {**_RECORD, "job": 5}})
    ctx, handle = _submit(store, seams)
    assert handle is not None
    assert seen == [{"keep_wip": True, "start_over": False, "saved_head": "f" * 40}]
    assert (
        "job_event",
        f"run_slice: td42 resumed from {'e' * 40}",
    ) in ctx.chunks
    assert store.refs[42].meta["parked"]["head"] == "f" * 40  # still parked


def test_submit_of_an_unparked_node_keeps_the_plain_retry_behaviour(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_ensure(monkeypatch)
    store = _Store()
    store.add_todo(42, "t")
    _ctx, handle = _submit(store, seams)
    assert handle is not None
    assert seen == [{"keep_wip": False, "start_over": False, "saved_head": None}]


def test_submit_after_a_start_over_answer_drops_the_branch_once(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_ensure(monkeypatch)
    store = _Store()
    store.add_todo(42, "t", meta={"start_over": 77, "parked": None})
    _ctx, handle = _submit(store, seams)
    assert handle is not None
    assert seen == [{"keep_wip": False, "start_over": True, "saved_head": None}]
    meta = store.refs[42].meta
    assert meta["start_over"] is None and meta["parked"] is None  # consumed
