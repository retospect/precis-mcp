"""A queued job says where it waits and why (gr495292).

``op='route'`` jobs sat ``STATUS:queued`` for minutes while only other jobs
were running, and ``get(kind='job')`` showed neither a position nor a
reason. The view now renders ``queue:`` lines for ``STATUS:queued`` rows:
position in the executor's claim order, what runs ahead, and the claim
gates visible from the row (suspension, node pin).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.job import JobHandler
from precis.store import Store
from precis.store.types import Tag


def _job(
    store: Store,
    status: str,
    *,
    job_type: str = "pcb_place",
    executor: str = "job_inproc",
    prio: int | None = None,
    params: dict[str, str] | None = None,
    origin: str | None = None,
) -> int:
    meta: dict[str, object] = {"job_type": job_type, "executor": executor}
    if params:
        meta["params"] = params
    if origin is not None:
        meta["origin"] = origin
    ref = store.insert_ref(kind="job", slug=None, title=job_type, meta=meta)
    if prio is not None:
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET prio = %s WHERE ref_id = %s", (prio, ref.id))
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent")
    return ref.id


def _body(store: Store, job_id: int) -> str:
    return JobHandler(hub=Hub(store=store, embedder=None)).get(id=job_id).body


def test_queued_job_shows_position_and_what_runs_ahead(store: Store) -> None:
    _job(store, "running", job_type="materialize")
    first = _job(store, "queued")
    second = _job(store, "queued")
    _job(store, "queued", executor="ssh_node")
    body = _body(store, second)
    assert (
        "queue: position 2 of 2 queued for executor 'job_inproc' — class "
        "background (1 ahead" in body
    )
    assert "claim order is class, then priority, then time queued" in body
    assert "queue: running now on 'job_inproc': 1 (materialize x1)" in body
    assert "queue: position 1 of 2" in _body(store, first)


def test_queue_shows_origin_class_and_per_class_ahead(store: Store) -> None:
    # A background band already queued; a fresh agent (interactive) job
    # behind it outranks it, so the agent row is position 1 and the
    # background rows report behind it (slice 6, D6).
    _job(store, "queued", origin="background")
    _job(store, "queued", origin="background")
    agent = _job(store, "queued", origin="agent")
    abody = _body(store, agent)
    assert "— class agent (0 ahead" in abody
    # The last-ranked background row is behind the agent row + its sibling.
    tail = _job(store, "queued", origin="background")
    tbody = _body(store, tail)
    assert "— class background (3 ahead" in tbody
    assert "queue: ahead by class: agent x1, background x2" in tbody


def test_higher_priority_job_jumps_the_position(store: Store) -> None:
    _job(store, "queued")
    urgent = _job(store, "queued", prio=1)
    assert "queue: position 1 of 2" in _body(store, urgent)


def test_idle_executor_is_called_out(store: Store) -> None:
    job = _job(store, "queued")
    assert "queue: nothing running on 'job_inproc'" in _body(store, job)


def test_node_pin_is_a_reason(store: Store) -> None:
    job = _job(store, "queued", params={"target_node": "box-a"})
    assert "queue: pinned to node 'box-a'" in _body(store, job)


def test_suspended_type_is_held_not_ranked(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SUSPENDED_JOB_TYPES", "pcb_place")
    body = _body(store, _job(store, "queued"))
    assert "queue: held — job_type 'pcb_place'" in body
    assert "queue: position" not in body


def test_terminal_job_has_no_queue_lines(store: Store) -> None:
    assert "queue:" not in _body(store, _job(store, "succeeded"))
