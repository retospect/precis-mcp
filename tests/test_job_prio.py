"""``put(kind='job', prio=…, origin=…)`` and ``tag(kind='job', prio=…)``
(gr549202).

An agent had no way to queue background batch work at low priority:
``put(kind='job')`` minted every row at the context-derived class (a
chat agent's ``agent``) with ``refs.prio`` NULL, so gardener's
``survey_extract`` fan-out ran beside interactive calls. ``prio=``
writes the ``refs.prio`` claim-order column (1..10, lower = hotter);
``origin=`` writes ``meta.origin``, the scheduling class the claim
ranks *before* prio. ``tag(prio=)`` reprioritises a live job and
refuses a terminal one.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.workers._mint import mint_background_job
from precis.workers.executors._common import claim_executor_jobs


def _parent(store: Store) -> int:
    return int(store.insert_ref(kind="todo", slug=None, title="owner", meta={}).id)


def _put_job(hub: Hub, store: Store, **kw: Any) -> int:
    resp = JobHandler(hub=hub).put(
        job_type="graph_garden", parent_id=_parent(store), **kw
    )
    m = re.search(r"job id=(\d+)", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _claim_order(store: Store) -> list[int]:
    """The ref_ids ``claim_executor_jobs`` would lock, in order."""
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(conn, executor="job_inproc", limit=50, node="h1")
        conn.rollback()
    return [int(r[0]) for r in rows]


def test_put_stamps_prio_and_origin(hub: Hub, store: Store) -> None:
    job_id = _put_job(hub, store, prio=8, origin="background")
    ref = store.get_ref(kind="job", id=job_id)
    assert ref is not None
    assert ref.prio == 8
    assert dict(ref.meta or {})["origin"] == "background"


def test_background_put_claims_behind_a_later_agent_put(hub: Hub, store: Store) -> None:
    """The gripe's pin: a batch job explicitly demoted to the background
    class is claimed AFTER agent-class work queued later — before the
    door existed the agent context stamped it 'agent' alongside the
    interactive calls it competes with."""
    with revision_context(actor="agent"):
        background = _put_job(hub, store, origin="background", prio=8)
        agent = _put_job(hub, store)  # derives class 'agent'
    ref = store.get_ref(kind="job", id=agent)
    assert ref is not None
    assert dict(ref.meta or {})["origin"] == "agent"
    order = _claim_order(store)
    assert order.index(agent) < order.index(background)


def test_put_prio_range_validated(hub: Hub, store: Store) -> None:
    for prio in (0, 11):
        with pytest.raises(BadInput, match="out of range"):
            _put_job(hub, store, prio=prio)


def test_put_prio_type_validated(hub: Hub, store: Store) -> None:
    with pytest.raises(BadInput, match="must be an int 1..10"):
        _put_job(hub, store, prio="high")


def test_put_origin_vocabulary_validated(hub: Hub, store: Store) -> None:
    with pytest.raises(BadInput, match="origin must be one of"):
        _put_job(hub, store, origin="cron")


def test_put_origin_human_refused_for_an_agent(hub: Hub, store: Store) -> None:
    """An agent cannot mint into the interactive-human lane — the class
    derivation rule still owns 'human' (authenticated web writes only)."""
    with revision_context(actor="agent"):
        with pytest.raises(BadInput, match="origin='human'"):
            _put_job(hub, store, origin="human")


def test_put_origin_conflicting_job_parent_refused(hub: Hub, store: Store) -> None:
    """A job parent fixes the child's class (2026-10-23 ruling) — a
    conflicting ``origin=`` is refused rather than silently dropped; the
    matching value is accepted."""
    with store.tx() as conn:
        parent = mint_background_job(
            store,
            conn=conn,
            job_type="plan_tick",
            executor="coordinator",
            title="p",
            origin="recurring",
        )
    handler = JobHandler(hub=hub)
    with pytest.raises(BadInput, match="conflicts with the parent job's class"):
        handler.put(
            job_type="graph_garden",
            parent_id=int(parent.id),
            origin="background",
        )
    resp = handler.put(
        job_type="graph_garden", parent_id=int(parent.id), origin="recurring"
    )
    assert re.search(r"job id=\d+", resp.body)


def test_put_retry_rejects_prio_and_origin(hub: Hub, store: Store) -> None:
    """mode='retry' re-mints via the parent todo's own class — stamping
    knobs on it would be silently ignored, so they're refused."""
    job_id = _put_job(hub, store)
    with pytest.raises(BadInput, match="does not mint a row"):
        JobHandler(hub=hub).put(id=job_id, mode="retry", prio=3)


def test_tag_prio_reorders_a_queued_job(hub: Hub, store: Store) -> None:
    """``tag(kind='job', id=…, prio=N)`` rewrites the claim-order column:
    the reprioritised row claims ahead of the older same-class row."""
    first = _put_job(hub, store)
    second = _put_job(hub, store)
    resp = JobHandler(hub=hub).tag(id=second, prio=2)
    assert "prio=2" in resp.body
    order = _claim_order(store)
    assert order.index(second) < order.index(first)


def test_tag_prio_range_validated(hub: Hub, store: Store) -> None:
    job_id = _put_job(hub, store)
    with pytest.raises(BadInput, match="out of range"):
        JobHandler(hub=hub).tag(id=job_id, prio=42)


def test_tag_prio_refused_on_a_terminal_job(hub: Hub, store: Store) -> None:
    """A terminal job never claims again — the prio write would be a
    silent no-op, so it refuses instead of landing."""
    job_id = _put_job(hub, store)
    JobHandler(hub=hub).tag(id=job_id, add=["STATUS:failed"])
    with pytest.raises(BadInput, match="never claims again"):
        JobHandler(hub=hub).tag(id=job_id, prio=1)


def test_dispatch_boundary_accepts_put_and_tag_prio(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The gripe's exact shape — the verb-level door, not the handler
    call: ``prio``/``origin`` used to die at the strictness gate as
    undeclared kwargs."""
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "job",
            "job_type": "graph_garden",
            "parent_id": _parent(store),
            "origin": "background",
            "prio": 9,
        },
    )
    assert "[error:" not in out, out
    m = re.search(r"job id=(\d+)", out)
    assert m is not None, out
    job_id = int(m.group(1))
    tag_out = runtime_with_store.dispatch(
        "tag", {"kind": "job", "id": job_id, "prio": 4}
    )
    assert "[error:" not in tag_out, tag_out
    ref = store.get_ref(kind="job", id=job_id)
    assert ref is not None and ref.prio == 4
