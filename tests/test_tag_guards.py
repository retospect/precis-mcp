"""Tag-verb guards: job lifecycle, finding STATUS:established, todo
atomicity, closed-prefix replace ack, dispatch per-verb kwarg tolerance."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.handlers.job import JobHandler
from precis.handlers.todo import TodoHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


@pytest.fixture
def todos(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _job(store: Store, status: str) -> int:
    ref = store.insert_ref(
        kind="job", slug=None, title="guard-job", meta={"job_type": "sweep"}
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return ref.id


def _status(store: Store, ref_id: int) -> set[str]:
    return {
        str(t)
        for t in store.tags_for(ref_id)
        if t.namespace == "closed" and t.prefix == "STATUS"
    }


# ── (a) job lifecycle ───────────────────────────────────────────────


def test_queued_cancel_requested_becomes_cancelled_with_event(
    jobs: JobHandler, store: Store
) -> None:
    jid = _job(store, "queued")
    jobs.tag(id=jid, add=["STATUS:cancel_requested"])
    assert _status(store, jid) == {"STATUS:cancelled"}
    assert store.events_for(jid, event="cancelled")


def test_running_cancel_requested_stays_cancel_requested(
    jobs: JobHandler, store: Store
) -> None:
    jid = _job(store, "running")
    jobs.tag(id=jid, add=["STATUS:cancel_requested"])
    assert _status(store, jid) == {"STATUS:cancel_requested"}
    assert not store.events_for(jid, event="cancelled")


@pytest.mark.parametrize(
    ("current", "target"), [("running", "succeeded"), ("succeeded", "queued")]
)
def test_worker_owned_transitions_refused(
    jobs: JobHandler, store: Store, current: str, target: str
) -> None:
    jid = _job(store, current)
    with pytest.raises(BadInput) as ei:
        jobs.tag(id=jid, add=[f"STATUS:{target}"])
    assert ei.value.next
    assert _status(store, jid) == {f"STATUS:{current}"}


def test_cancel_requested_to_cancelled_allowed(jobs: JobHandler, store: Store) -> None:
    jid = _job(store, "cancel_requested")
    jobs.tag(id=jid, add=["STATUS:cancelled"])
    assert _status(store, jid) == {"STATUS:cancelled"}


# ── (b) finding ─────────────────────────────────────────────────────


def _finding(store: Store) -> int:
    return store.insert_ref(kind="finding", slug=None, title="f", meta={}).id


def test_finding_hand_set_established_refused(hub: Hub, store: Store) -> None:
    h = FindingHandler(hub=hub)
    fid = _finding(store)
    with pytest.raises(BadInput) as ei:
        h.tag(id=fid, add=["STATUS:established"])
    assert "chase worker" in str(ei.value)
    assert "STATUS:established" not in {str(t) for t in store.tags_for(fid)}


def test_finding_put_create_established_refused(hub: Hub, store: Store) -> None:
    """gr496191: the create-time ``tags=`` shortcut must not bypass the A3
    guard — ``put(tags=['STATUS:established'])`` would mint the grounded-
    cohort marker the ``tag`` verb refuses to write."""
    h = FindingHandler(hub=hub)
    with pytest.raises(BadInput) as ei:
        h.put(title="t", body="b", cited_in="x", tags=["STATUS:established"])
    assert "chase worker" in str(ei.value)


def test_finding_put_create_other_status_tag_allowed(hub: Hub, store: Store) -> None:
    """The guard refuses only ``established`` — a caller-pinned lifecycle
    tag (e.g. the ungrounded ``STATUS:tracing``) still lands on create."""
    from precis.store.types import ChunkInsert

    paper = store.insert_ref(kind="paper", slug="miller23a", title="p", meta={})
    store.chunks.insert_chunks(paper.id, [ChunkInsert(ord=0, text="body", meta={})])
    h = FindingHandler(hub=hub)
    resp = h.put(title="t", body="b", cited_in="miller23a", tags=["STATUS:acquiring"])
    m = re.search(r"id=(\d+)", resp.body)
    assert m is not None
    assert "STATUS:acquiring" in {str(t) for t in store.tags_for(int(m.group(1)))}


def test_finding_tracing_still_ok(hub: Hub, store: Store) -> None:
    h = FindingHandler(hub=hub)
    fid = _finding(store)
    h.tag(id=fid, add=["STATUS:tracing"])
    assert "STATUS:tracing" in {str(t) for t in store.tags_for(fid)}


# ── (c) todo atomicity ──────────────────────────────────────────────


def test_todo_tag_bad_add_leaves_prio_unchanged(
    todos: TodoHandler, store: Store
) -> None:
    tid = todos.put(text="atomic", prio=3).ref_id
    assert tid is not None
    ref = store.get_ref(kind="todo", id=tid)
    assert ref is not None
    before = ref.prio
    with pytest.raises(BadInput):
        todos.tag(id=tid, prio=1, add=["STATUS:bogus"])
    after = store.get_ref(kind="todo", id=tid)
    assert after is not None
    assert after.prio == before


# ── (d) replaced ack ────────────────────────────────────────────────


def test_closed_prefix_retag_ack_names_replaced_value(todos: TodoHandler) -> None:
    tid = todos.put(text="ack").ref_id
    assert tid is not None
    resp = todos.tag(id=tid, add=["STATUS:doing"])
    assert "(replaced STATUS:open -> STATUS:doing)" in resp.body


# ── (e) dispatch kwargs ─────────────────────────────────────────────


def _todo_id(rt: PrecisRuntime) -> int:
    out = rt.dispatch("put", {"kind": "todo", "text": "kw"})
    m = re.search(r"id=(\d+)", out)
    assert m, out
    return int(m.group(1))


@pytest.mark.parametrize("verb", ["put", "delete", "tag"])
def test_typed_page_size_refused_with_next(
    runtime_with_store: PrecisRuntime, verb: str
) -> None:
    tid = _todo_id(runtime_with_store)
    args: dict = {"kind": "todo", "id": tid, "page_size": 5}
    if verb == "put":
        args["text"] = "x"
    if verb == "tag":
        args["add"] = ["x"]
    out = runtime_with_store.dispatch(verb, args)
    assert "[error:BadInput]" in out, out
    assert "page_size" in out
    assert "drop it" in out


def test_typed_page_size_refused_on_get_without_paging(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch(
        "get", {"kind": "random", "view": "slug", "page_size": 5}
    )
    # RandomHandler.get is tolerant (opted out of the gate): never an error.
    assert "[error:" not in out, out
    tid = _todo_id(runtime_with_store)
    out = runtime_with_store.dispatch(
        "get", {"kind": "citation", "id": tid, "page_size": 5}
    )
    assert "page_size" in out


def test_wrapper_defaults_still_pass_edit_link_search(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    tid = _todo_id(rt)
    out = rt.dispatch("search", {"kind": "todo", "q": "kw", "page": 1})
    assert "[error:" not in out, out
    out = rt.dispatch(
        "edit",
        {"kind": "todo", "id": tid, "text": "kw2", "mode": "replace", "verdict": None},
    )
    assert "does not accept" not in out, out
    out = rt.dispatch(
        "link", {"kind": "todo", "id": tid, "target": f"td{tid}", "mode": "add"}
    )
    assert "does not accept" not in out, out
