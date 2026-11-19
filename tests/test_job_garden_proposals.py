"""The ``/garden/proposals`` view and ``mode='garden-reject'`` door (gr681311).

The pending-proposal read mirrors ``precis garden proposals``; the reject
mode mirrors ``precis garden reject`` — one append-only terminal
``reviews`` row, nothing else.
"""

from __future__ import annotations

import pytest

from precis.graph.garden import consumer
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import (
    GardenProposal,
    emit_proposal,
    pending_proposals,
    resolve_proposal,
)
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.utils import handle_registry
from tests._skill_roots import BUILTIN_SKILLS_DIR


def _proposal(store: Store) -> tuple[int, int]:
    """Seed one pending proposal; returns ``(review_id, ref_id)``."""
    ref = store.insert_ref(kind="memory", slug=None, title="Memory fixture")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0, text="A retained claim.", meta={"chunk_kind": "memory_body"}
            )
        ],
    )
    candidate = GardenCandidate(
        detector="anchor_rot",
        action="rewrite",
        target_kind="ref",
        target_id=ref.id,
        title="Repair a stale anchor",
        evidence={},
        payload={},
    )
    review_id = emit_proposal(store, candidate, rationale="fixture")
    assert review_id is not None
    return review_id, ref.id


def _memory(store: Store, title: str) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="A dated claim.", meta={"chunk_kind": "memory_body"})],
    )
    return int(ref.id)


def _retire_proposal(store: Store, target: int, *, key: str | None = None) -> int:
    candidate = GardenCandidate(
        detector="thread_shape",
        action="retire",
        target_kind="ref",
        target_id=target,
        title="Retire a dated memory",
        evidence={},
        payload={},
    )
    review_id = emit_proposal(store, candidate, rationale="fixture", key=key)
    assert review_id is not None
    return review_id


def test_retired_targets_are_marked_stale_pending_gr714718(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    # Redirected tombstone — retired with a successor, stamped as a raw
    # ref id like the supersede door records it.
    redirected = _memory(store, "Redirected memory")
    successor = _memory(store, "Survivor memory")
    rid_redirected = _retire_proposal(store, redirected)
    store.stamp_ref_meta(redirected, {"superseded_by": successor})
    store.retire_ref(redirected)

    # Successorless tombstone, with a second distinct pending row on the
    # same retired target (another detector's question, distinct key).
    plain = _memory(store, "Plain memory")
    rid_plain = _retire_proposal(store, plain)
    rid_second = _retire_proposal(store, plain, key=f"retire:ref:{plain}:grab_bag")
    store.retire_ref(plain)

    rows = {p.review_id: p for p in pending_proposals(store, limit=50)}
    redirected_row = rows[rid_redirected]
    assert redirected_row.target_retired
    assert not redirected_row.current
    assert redirected_row.retired_successor == handle_registry.format_handle(
        "memory", successor
    )
    for rid in (rid_plain, rid_second):
        assert rows[rid].target_retired
        assert not rows[rid].current
        assert rows[rid].retired_successor is None

    body = runtime_with_store.dispatch(
        "get", {"kind": "job", "id": "/garden/proposals"}
    )
    assert f"review {rid_redirected}" in body
    assert (
        f"[stale: target retired → {handle_registry.format_handle('memory', successor)}]"
        in body
    )
    assert f"review {rid_plain}" in body
    assert f"review {rid_second}" in body
    assert body.count("[stale: target retired (no successor)]") == 2
    assert "[stale: target changed]" not in body


def test_consumer_closes_retired_target_memory_proposal_gr714718(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    target = _memory(store, "Memory retired elsewhere")
    successor = _memory(store, "Successor memory")
    review_id = _retire_proposal(store, target)
    # The audited delete door stamps a handle string.
    store.stamp_ref_meta(
        target, {"superseded_by": handle_registry.format_handle("memory", successor)}
    )
    store.retire_ref(target)

    def unexpected(
        _store: Store, _proposal: GardenProposal
    ) -> consumer.ConsumerVerdict:
        pytest.fail("a retired target must not buy a model review")
        raise AssertionError("unreachable")  # pragma: no cover

    report = consumer.consume_proposals(store, review=unexpected)
    assert report.rejected == 1
    assert pending_proposals(store) == []

    # Both decisions survive in the audit history: the tombstone on the
    # ref, and the terminal row recording why the proposal closed.
    tomb = store.fetch_refs_by_ids([target], include_deleted=True)[target]
    assert tomb.retired_at is not None
    terminal = [
        rev
        for rev in store.reviews_for("ref", target)
        if rev.verdict == "rejected" and rev.actor == consumer.CONSUMER_ACTOR
    ]
    assert len(terminal) == 1
    assert f'"resolves": {review_id}' in (terminal[0].note or "")
    assert '"reason": "target_gone"' in (terminal[0].note or "")
    assert "already retired elsewhere" in (terminal[0].note or "")
    assert handle_registry.format_handle("memory", successor) in (
        terminal[0].note or ""
    )


def test_proposals_view_lists_pending_and_flags_stale(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    review_id, ref_id = _proposal(store)
    body = runtime_with_store.dispatch(
        "get", {"kind": "job", "id": "/garden/proposals"}
    )
    assert f"review {review_id}" in body
    assert "Repair a stale anchor" in body
    assert "[stale: target changed]" not in body
    filtered = runtime_with_store.dispatch(
        "get",
        {
            "kind": "job",
            "id": "/garden/proposals",
            "__extras__": {"action": "merge"},
        },
    )
    assert "no pending garden proposals" in filtered
    filtered = runtime_with_store.dispatch(
        "get",
        {
            "kind": "job",
            "id": "/garden/proposals",
            "__extras__": {"action": "rewrite"},
        },
    )
    assert f"review {review_id}" in filtered
    # An edit to the target makes the proposal stale in the view.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = title || ' x' WHERE ref_id = %s", (ref_id,)
        )
    body = runtime_with_store.dispatch(
        "get", {"kind": "job", "id": "/garden/proposals"}
    )
    assert f"review {review_id}" in body
    assert "stale: target changed" in body
    # A decided proposal leaves the queue.
    resolved = resolve_proposal(store, review_id, actor="agent", verdict="rejected")
    assert resolved is not None
    body = runtime_with_store.dispatch(
        "get", {"kind": "job", "id": "/garden/proposals"}
    )
    assert "no pending garden proposals" in body


def test_garden_view_reports_dark_without_service_row(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM service_config WHERE service = 'graph_garden'")
    body = runtime_with_store.dispatch("get", {"kind": "job", "id": "/garden"})
    assert "gardener: dark" in body
    assert "cooling:" in body
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('test-host', 'graph_garden', 1) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = 1"
        )
    body = runtime_with_store.dispatch("get", {"kind": "job", "id": "/garden"})
    assert "gardener: on" in body


def test_garden_reject_closes_proposal_once(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    review_id, _ = _proposal(store)
    with store.pool.connection() as conn:
        before = conn.execute("SELECT count(*) FROM reviews").fetchone()
    assert before is not None

    body = runtime_with_store.dispatch(
        "put",
        {
            "kind": "job",
            "mode": "garden-reject",
            "__extras__": {"review_id": review_id, "reason": "not a dupe"},
        },
    )
    assert f"rejected proposal {review_id}" in body
    assert pending_proposals(store) == []
    with store.pool.connection() as conn:
        after = conn.execute("SELECT count(*) FROM reviews").fetchone()
    assert after is not None and after[0] == before[0] + 1

    again = runtime_with_store.dispatch(
        "put",
        {
            "kind": "job",
            "mode": "garden-reject",
            "__extras__": {"review_id": review_id, "reason": "again"},
        },
    )
    assert "[error:NotFound]" in again
    missing_reason = runtime_with_store.dispatch(
        "put",
        {
            "kind": "job",
            "mode": "garden-reject",
            "__extras__": {"review_id": review_id},
        },
    )
    assert "[error:BadInput]" in missing_reason
    unknown = runtime_with_store.dispatch(
        "put",
        {
            "kind": "job",
            "mode": "garden-reject",
            "__extras__": {"review_id": 999999999, "reason": "none such"},
        },
    )
    assert "[error:NotFound]" in unknown


def test_gardener_skill_names_proposals_view() -> None:
    text = (BUILTIN_SKILLS_DIR / "precis-gardener-help.md").read_text(encoding="utf-8")
    assert "id='/garden/proposals'" in text
    assert "mode='garden-reject'" in text
