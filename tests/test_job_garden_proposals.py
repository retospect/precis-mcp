"""The ``/garden/proposals`` view and ``mode='garden-reject'`` door (gr681311).

The pending-proposal read mirrors ``precis garden proposals``; the reject
mode mirrors ``precis garden reject`` — one append-only terminal
``reviews`` row, nothing else.
"""

from __future__ import annotations

from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import (
    emit_proposal,
    pending_proposals,
    resolve_proposal,
)
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert
from tests._skill_roots import BUILTIN_SKILLS_DIR


def _proposal(store: Store, *, action: str = "rewrite") -> tuple[int, int]:
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
        action=action,
        target_kind="ref",
        target_id=ref.id,
        title="Repair a stale anchor",
        evidence={},
        payload={},
    )
    review_id = emit_proposal(store, candidate, rationale="fixture")
    assert review_id is not None
    return review_id, ref.id


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


def test_proposals_view_filters_stale_qualify_beyond_small_scan_window(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    review_id, ref_id = _proposal(store, action="qualify")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = title || ' x' WHERE ref_id = %s", (ref_id,)
        )
    other_reviews = [_proposal(store)[0] for _ in range(4)]

    request = {"kind": "job", "id": "/garden/proposals"}
    unfiltered = runtime_with_store.dispatch("get", request)
    assert f"review {review_id}: qualify" in unfiltered
    filtered = runtime_with_store.dispatch(
        "get", {**request, "__extras__": {"action": "qualify", "limit": 1}}
    )
    assert f"review {review_id}: qualify" in filtered
    assert "stale: target changed" in filtered
    assert all(f"review {other}:" not in filtered for other in other_reviews)


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
