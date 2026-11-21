"""Park = save, resume where the work is: the tick half (``plan_dispatch``'s
``_step_park_stamp`` and the ``start over`` answer in ``_step_rearm``). The
work is the task's ``meta.build_ref`` (in its host's hub) or, when the push
was withheld or failed, the newest ended job's ``meta.push`` (only in the
attempt folder)."""

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

_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_SHA = "b2c3d4e5f60718293a4b5c6d7e8f90123456789a"


def _build_ref(task: int, **over: Any) -> dict[str, Any]:
    """``meta.build_ref`` as the worker stamps it after a push."""
    rec: dict[str, Any] = {
        "state": "pushed",
        "ref": f"refs/factory/build/{task}/12",
        "sha": _HEAD,
        "head": _HEAD,
        "wip": False,
        "by": "builder",
        "note": "",
        "host": "build-host-1",
        "at": "2026-11-16T10:00:00Z",
    }
    return rec | over


def _push(state: str, **over: Any) -> dict[str, Any]:
    """A job's ``meta.push`` for an attempt whose work the hub does not hold."""
    rec: dict[str, Any] = {
        "state": state,
        "ref": "refs/factory/build/7/12",
        "sha": None,
        "head": _HEAD,
        "wip": False,
        "by": "worker",
        "note": "secret-scan: 2 hits in a.py",
        "host": "build-host-2",
        "at": "2026-11-16T10:05:00Z",
    }
    return rec | over


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


def test_park_stamps_the_node_and_names_the_hub_work_in_the_ask_body(
    store: Store,
) -> None:
    """Whatever opened the ask, the tick copies the task's ``build_ref``
    onto the node (``meta.parked``) and writes host, ref and head into the
    ask's body — once."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    ref = _build_ref(task)
    store.stamp_ref_meta(task, {"build_ref": ref})
    _failed_job(store, task, "boom")
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert _meta(store, task)["parked"] == {
        "host": "build-host-1",
        "ref": ref["ref"],
        "sha": _HEAD,
        "head": _HEAD,
        "wip": False,
        "state": "pushed",
        "at": "2026-11-16T10:00:00Z",
        "job": 12,
    }
    body = _ask_body(store, ask)
    assert body.count("Parked work: host `build-host-1`") == 1
    assert f"ref `{ref['ref']}` in that host's hub" in body
    assert f"head `{_HEAD[:12]}`" in body
    assert "wip:" not in body
    assert "continue from that commit on that host" in body
    assert "answer `start over`" in body
    assert "worktree" not in body
    pd.run_pass(store)  # idempotent: one note, one stamp comment
    assert _ask_body(store, ask).count("Parked work:") == 1
    parked_comments = [
        t for t in _comment_texts(store, task) if t.startswith("parked:")
    ]
    assert len(parked_comments) == 1
    assert "build-host-1" in parked_comments[0] and ref["ref"] in parked_comments[0]
    assert _HEAD[:12] in parked_comments[0] and "worktree" not in parked_comments[0]


def test_a_wip_snapshot_on_top_is_named_in_the_ask_body(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task, sha=_SHA, wip=True)})
    _failed_job(store, task, "boom")
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    body = _ask_body(store, ask)
    assert "the top commit is a `wip:` snapshot" in body
    assert f"head `{_HEAD[:12]}`" in body
    assert _meta(store, task)["parked"]["sha"] == _SHA


def test_a_moved_head_rewrites_the_stamp_but_not_the_ask_note(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task)})
    _failed_job(store, task, "boom")
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    store.stamp_ref_meta(
        task, {"build_ref": _build_ref(task, sha=_SHA, head=_SHA, ref="refs/x/9")}
    )
    pd.run_pass(store)
    assert _meta(store, task)["parked"]["head"] == _SHA
    assert _ask_body(store, ask).count("Parked work:") == 1
    assert sum(t.startswith("parked:") for t in _comment_texts(store, task)) == 2


@pytest.mark.parametrize("state", ["withheld", "failed"])
def test_work_only_in_a_folder_is_named_with_its_host_folder_and_why(
    store: Store, state: str
) -> None:
    """No ``build_ref``: the newest ended job's ``meta.push`` says the
    work sits only in ``slice-td<todo>-j<job>`` on ``push.host``."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    job = _failed_job(store, task, "boom", meta={"push": _push(state)})
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert _meta(store, task)["parked"] == {
        "host": "build-host-2",
        "ref": "refs/factory/build/7/12",
        "sha": None,
        "head": _HEAD,
        "wip": False,
        "state": state,
        "at": "2026-11-16T10:05:00Z",
        "job": job,
    }
    body = _ask_body(store, ask)
    assert "Parked work: host `build-host-2`" in body
    assert f"folder `slice-td{task}-j{job}`" in body
    assert "only in that folder on that host, not in its hub" in body
    assert f"{state}: secret-scan: 2 hits in a.py" in body
    assert "start over" in body
    parked_comment = next(
        t for t in _comment_texts(store, task) if t.startswith("parked:")
    )
    assert (
        f"slice-td{task}-j{job}" in parked_comment and "build-host-2" in parked_comment
    )


def test_a_build_ref_wins_over_a_folder_only_job(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task)})
    _failed_job(store, task, "boom", meta={"push": _push("withheld")})
    _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert _meta(store, task)["parked"]["state"] == "pushed"


@pytest.mark.parametrize("state", ["pushed", "empty"])
def test_a_job_whose_work_the_hub_holds_or_that_made_none_parks_nothing(
    store: Store, state: str
) -> None:
    """Without a ``build_ref``, only ``withheld``/``failed`` push records
    name parkable work."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "boom", meta={"push": _push(state)})
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert "parked" not in _meta(store, task)
    assert "Parked work:" not in _ask_body(store, ask)


@pytest.mark.parametrize("how", ["rescue:builder", "paused"])
def test_park_by_tag_or_pause_stamps_the_node_too(store: Store, how: str) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task)})
    _failed_job(store, task, "boom")
    if how == "paused":
        store.add_tag(
            task, Tag.closed("STATUS", "paused"), set_by="system", replace_prefix=True
        )
    else:
        store.add_tag(task, Tag.open(how), set_by="system")
    pd.run_pass(store)
    assert _meta(store, task)["parked"]["head"] == _HEAD


def test_a_retried_build_is_not_parked_so_nothing_is_stamped(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task)})
    _failed_job(store, task, "boom")
    pd.run_pass(store)  # no ask, no tag: the tick just retries it
    assert "parked" not in _meta(store, task)


def test_answering_start_over_arms_the_drop_and_keeps_the_pin_record(
    store: Store,
) -> None:
    """``start over`` clears ``meta.parked`` and stamps ``meta.start_over``;
    ``meta.build_ref`` stays — the launch needs its host to delete the
    task's refs in that hub, and clears the record itself."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task)})
    _failed_job(store, task, "boom")
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert _meta(store, task)["parked"]["head"] == _HEAD
    _answer_ask(store, ask, "Start Over please")
    pd.run_pass(store)
    meta = _meta(store, task)
    assert meta["start_over"] == ask and meta.get("parked") is None
    assert meta["rearmed_by_ask"] == ask
    assert meta["build_ref"]["host"] == "build-host-1"


def test_any_other_answer_keeps_the_parked_work_for_resume(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task)})
    _failed_job(store, task, "boom")
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    _answer_ask(store, ask, "retry")
    pd.run_pass(store)
    meta = _meta(store, task)
    assert not meta.get("start_over")
    assert meta["parked"]["head"] == _HEAD
    assert meta["rearmed_by_ask"] == ask


def test_resume_lifts_paused_but_not_a_waiting_for_hold(store: Store) -> None:
    """Reto 2026-11-16: answering ``resume`` lifts ``STATUS:paused``; a
    ``waiting-for:`` hold (waiting on files) stays as is."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task)})
    _failed_job(store, task, "boom")
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
    body = _ask_body(store, ask)
    assert "lifts STATUS:paused" in body and "waiting-for:*" in body


def test_newer_folder_only_work_is_named_beside_the_hub_commit(
    store: Store,
) -> None:
    """Item 8: ``build_ref`` names an older attempt than a job whose push
    was withheld: the ask names the hub commit a re-arm continues from AND
    the folder that holds newer work the hub does not."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(
        task, {"build_ref": _build_ref(task, ref=f"refs/factory/build/{task}/1")}
    )
    job = _failed_job(store, task, "boom", meta={"push": _push("withheld")})
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    body = _ask_body(store, ask)
    assert "ref `refs/factory/build/" in body and "continue from that commit" in body
    assert "Newer work, not in the hub: host `build-host-2`" in body
    assert f"folder `slice-td{task}-j{job}`" in body
    assert "withheld: secret-scan: 2 hits in a.py" in body
    assert "lost" in body
    parked = _meta(store, task)["parked"]
    assert parked["state"] == "pushed" and parked["folder"]["job"] == job
    comment = next(t for t in _comment_texts(store, task) if t.startswith("parked:"))
    assert f"slice-td{task}-j{job}" in comment and "build-host-1" in comment
    pd.run_pass(store)  # once per ask
    assert _ask_body(store, ask).count("Newer work, not in the hub:") == 1


def test_newer_folder_only_work_that_appears_later_still_reaches_the_ask(
    store: Store,
) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(
        task, {"build_ref": _build_ref(task, ref=f"refs/factory/build/{task}/1")}
    )
    _failed_job(store, task, "boom")
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert "Newer work" not in _ask_body(store, ask)
    _failed_job(store, task, "boom", meta={"push": _push("failed")})
    pd.run_pass(store)
    assert "Newer work, not in the hub:" in _ask_body(store, ask)
    assert _ask_body(store, ask).count("Parked work:") == 1


def test_older_folder_only_work_is_not_named(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(
        task, {"build_ref": _build_ref(task, ref=f"refs/factory/build/{task}/999999")}
    )
    _failed_job(store, task, "boom", meta={"push": _push("withheld")})
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert "Newer work" not in _ask_body(store, ask)
    assert "folder" not in _meta(store, task)["parked"]


def test_a_cancelled_attempts_folder_is_named_as_newer_work(store: Store) -> None:
    """Round 4: a stopped attempt is the usual way a folder holds work the
    hub lacks — its ``meta.push`` is stamped on a cancelled job."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(
        task, {"build_ref": _build_ref(task, ref=f"refs/factory/build/{task}/1")}
    )
    job = _failed_job(store, task, "stopped", meta={"push": _push("withheld")})

    store.add_tag(
        job, Tag.closed("STATUS", "cancelled"), set_by="system", replace_prefix=True
    )
    ask = _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    assert _meta(store, task)["parked"]["folder"]["job"] == job
    assert "Newer work, not in the hub:" in _ask_body(store, ask)
    assert f"folder `slice-td{task}-j{job}`" in _ask_body(store, ask)
