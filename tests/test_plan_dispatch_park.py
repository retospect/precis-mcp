"""Park = save, resume anywhere: the tick half (``plan_dispatch``'s
``_step_park_stamp`` and the ``start over`` answer in ``_step_rearm``). The
git half is ``tests/test_slice_park.py``."""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.workers.job_types import plan_dispatch as pd
from tests.test_plan_dispatch import (
    _answer_ask,
    _comment_texts,
    _factory_on,  # noqa: F401  (autouse fixture of the sibling module)
    _failed_job,
    _meta,
    _one_task_plan,
)

_SAVED: dict[str, Any] = {
    "host": "build-host-1",
    "worktree": "/srv/slices/.claude/worktrees/slice-td7",
    "head": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "reason": "ladder spent",
    "at": "2026-11-16T10:00:00Z",
    "wip": True,
    "push": "branch push of slice/7: on origin",
}


def _ask_blocking(store: Store, task: int, root: int) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title="needs a human",
            meta={
                "ask": {
                    "kind": "ruling",
                    "options": ["retry", "start over", "drop"],
                    "blocks": f"td{task}",
                    "context": "parked",
                }
            },
            parent_id=root,
            conn=conn,
        )
        ask = int(ref.id)
        store.add_tag(
            ask,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(ask, Tag.open("ask-user"), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ask,
            [
                ChunkInsert(
                    ord=0,
                    text="The tick stopped dispatching the task.",
                    meta={"chunk_kind": TODO_BODY_KIND},
                )
            ],
            conn=conn,
        )
        store.add_link(
            src_ref_id=task, dst_ref_id=ask, relation="blocked-by", conn=conn
        )
    return ask


def _ask_body(store: Store, ask: int) -> str:
    return next(
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(ask)
        if c.chunk_kind == TODO_BODY_KIND
    )


def test_park_stamps_the_node_and_names_the_work_in_the_ask_body(
    store: Store,
) -> None:
    """Whatever opened the ask, the tick copies the failed job's saved-work
    record onto the node (``meta.parked``) and writes host, worktree and
    head into the ask's body — once."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    job = _failed_job(store, task, "boom", meta={"saved": _SAVED})
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert _meta(store, task)["parked"] == {
        "host": "build-host-1",
        "worktree": "/srv/slices/.claude/worktrees/slice-td7",
        "head": _SAVED["head"],
        "reason": "ladder spent",
        "at": "2026-11-16T10:00:00Z",
        "push": "branch push of slice/7: on origin",
        "job": job,
    }
    body = _ask_body(store, ask)
    assert "Parked work: host `build-host-1`" in body
    assert "/srv/slices/.claude/worktrees/slice-td7" in body
    assert _SAVED["head"][:12] in body and f"refs/factory/slice/{task}" in body
    assert "start over" in body
    pd.run_pass(store)  # idempotent: one note, one stamp comment
    assert _ask_body(store, ask).count("Parked work:") == 1
    assert sum("parked: work saved" in t for t in _comment_texts(store, task)) == 1


@pytest.mark.parametrize("how", ["rescue:builder", "paused"])
def test_park_by_tag_or_pause_stamps_the_node_too(store: Store, how: str) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "boom", meta={"saved": _SAVED})
    if how == "paused":
        store.add_tag(
            task, Tag.closed("STATUS", "paused"), set_by="system", replace_prefix=True
        )
    else:
        store.add_tag(task, Tag.open(how), set_by="system")
    pd.run_pass(store)
    assert _meta(store, task)["parked"]["head"] == _SAVED["head"]


def test_a_retried_build_is_not_parked_so_nothing_is_stamped(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "boom", meta={"saved": _SAVED})
    pd.run_pass(store)  # no ask, no tag: the tick just retries it
    assert "parked" not in _meta(store, task)


def test_answering_start_over_arms_the_drop_and_clears_the_park(
    store: Store,
) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "boom", meta={"saved": _SAVED})
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert _meta(store, task)["parked"]["head"] == _SAVED["head"]
    _answer_ask(store, ask, "Start Over please")
    pd.run_pass(store)
    meta = _meta(store, task)
    assert meta["start_over"] == ask and meta.get("parked") is None
    assert meta["rearmed_by_ask"] == ask


def test_any_other_answer_keeps_the_parked_branch_for_resume(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "boom", meta={"saved": _SAVED})
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    _answer_ask(store, ask, "retry")
    pd.run_pass(store)
    meta = _meta(store, task)
    assert not meta.get("start_over")
    assert meta["parked"]["head"] == _SAVED["head"]
    assert meta["rearmed_by_ask"] == ask


def test_resume_lifts_paused_but_not_a_waiting_for_hold(store: Store) -> None:
    """Reto 2026-11-16: answering ``resume`` lifts ``STATUS:paused``; a
    ``waiting-for:`` hold (waiting on files) stays as is."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "boom", meta={"saved": _SAVED})
    ask = _ask_blocking(store, task, mint.root_id)
    store.add_tag(
        task, Tag.closed("STATUS", "paused"), set_by="system", replace_prefix=True
    )
    store.add_tag(task, Tag.open("waiting-for:files"), set_by="system")
    pd.run_pass(store)
    _answer_ask(store, ask, "resume")
    pd.run_pass(store)
    status = next(t.value for t in store.tags_for(task) if t.prefix == "STATUS")
    assert status != "paused"
    assert "waiting-for:files" in {str(t.value) for t in store.tags_for(task)}


def test_a_withheld_snapshot_is_named_as_host_local_in_the_ask_body(
    store: Store,
) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    saved = {**_SAVED, "push": "withheld: secret-scan 2 hits"}
    _failed_job(store, task, "boom", meta={"saved": saved})
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    body = _ask_body(store, ask)
    assert "withheld by the secret scan" in body and "that host only" in body
    assert "pushed to" not in body
    assert "lifts STATUS:paused" in body and "waiting-for:*" in body
