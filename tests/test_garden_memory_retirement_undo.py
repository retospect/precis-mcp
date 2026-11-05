"""Retirement undo preserves graph routes and refuses unsafe restoration."""

from __future__ import annotations

import argparse
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.cli.garden import _undo
from precis.errors import BadInput
from precis.graph.garden import apply as doors
from precis.graph.garden import candidates as cand
from precis.graph.garden.proposals import emit_proposal, key_block, pending_proposals
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.store.types import Tag
from tests.test_garden_memory_apply import (
    MemoryProposal,
    _apply,
    _body,
    _links,
    _memory,
    _reviews,
)


def _thread(store: Store, *, successor: int | None = None) -> int:
    target = _memory(store, "Dated snapshot: the campaign started.\n")
    store.add_tag(target, Tag.open("section:threads"), set_by="system")
    if successor is not None:
        with revision_context(trust_origin="generated", trust_actor_type="agent"):
            store.add_link(src_ref_id=target, dst_ref_id=successor, relation="part-of")
    return target


def _successor(store: Store) -> int:
    target = _memory(store, "Left: finish campaign\nEarlier: campaign started.")
    store.add_tag(target, Tag.open("section:threads"), set_by="system")
    return target


def _propose(store: Store, target: int, action: str) -> MemoryProposal:
    candidate = next(
        c for c in cand.thread_shape_candidates(store) if c.target_id == target
    )
    assert candidate.action == action
    review_id = emit_proposal(
        store, candidate, rationale="Checked every retained claim and route."
    )
    assert review_id is not None
    return MemoryProposal(target, review_id, _body(store, target), candidate)


def _record(store: Store, prop: MemoryProposal) -> dict[str, Any]:
    ref = store.get_ref(kind="memory", id=prop.target, include_deleted=True)
    assert ref is not None and ref.retired_at is not None
    record = ref.meta["garden_retirement"]
    assert record["retirement_id"] == (
        f"garden-{prop.candidate.action}:{prop.target}:{prop.review_id}"
    )
    return dict(record)


def _graph(store: Store, sources: list[int]) -> dict[int, list[tuple[Any, ...]]]:
    return {target: _links(store, target) for target in sources}


@pytest.mark.parametrize("action", ["retire", "supersede"])
def test_retirement_undo_restores_chunk_routes_and_sticks(
    store: Store, action: str
) -> None:
    successor = _successor(store) if action == "supersede" else None
    target = _thread(store, successor=successor)
    reader = _memory(store, "A reader of the retained claim.")
    source = _memory(store, "Evidence for the retained claim.")
    with revision_context(trust_origin="extracted", trust_actor_type="agent"):
        store.add_link(
            src_ref_id=target,
            src_pos=0,
            dst_ref_id=source,
            dst_pos=0,
            relation="cites",
            set_by="agent",
            meta={"why": "claim evidence", "route": ["source", "claim"]},
        )
        store.add_link(
            src_ref_id=reader,
            src_pos=0,
            dst_ref_id=target,
            dst_pos=0,
            relation="cites",
            set_by="user",
            meta={"why": "incoming evidence route"},
        )
    prop = _propose(store, target, action)
    sources = [target, reader, source, *([successor] if successor else [])]
    before = _graph(store, sources)
    before_ref = store.get_ref(kind="memory", id=target)
    assert before_ref is not None

    _apply(store, prop)

    record = _record(store, prop)
    assert _links(store, target) == []
    assert not any(p.review_id == prop.review_id for p in pending_proposals(store))
    result = doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert result["links_restored"] >= 2
    assert _graph(store, sources) == before
    assert _body(store, target).encode("utf-8") == prop.body.encode("utf-8")
    restored = store.get_ref(kind="memory", id=target)
    assert restored is not None and restored.retired_at is None
    assert restored.title == before_ref.title
    assert "garden_retirement" not in restored.meta
    assert "superseded_by" not in restored.meta
    assert key_block(store, "ref", target, prop.candidate.key) == "rejected"
    assert emit_proposal(store, prop.candidate, rationale="Try the undone key") is None


def test_supersede_undo_preserves_collisions_and_prior_supersedes_trail(
    store: Store,
) -> None:
    successor = _successor(store)
    target = _thread(store, successor=successor)
    source = _memory(store, "A source shared by both versions.")
    reader = _memory(store, "A reader of both versions.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        for ref_id in (target, successor):
            store.add_link(
                src_ref_id=ref_id,
                dst_ref_id=source,
                relation="cites",
                meta={"original": ref_id},
            )
            store.add_link(
                src_ref_id=reader,
                dst_ref_id=ref_id,
                relation="related-to",
                meta={"original": ref_id},
            )
        trail = store.add_link(
            src_ref_id=successor,
            dst_ref_id=target,
            relation="supersedes",
            set_by="user",
            meta={"why": "already reviewed replacement"},
        )
    prop = _propose(store, target, "supersede")
    sources = [target, successor, source, reader]
    before = _graph(store, sources)

    _apply(store, prop)
    record = _record(store, prop)
    assert len([link for link in _links(store, successor) if link[3] == source]) == 1

    doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert _graph(store, sources) == before
    assert any(link[0] == trail.id for link in _links(store, successor))


@pytest.mark.parametrize("inferred_from", ["metadata", "supersedes"])
def test_retire_undo_restores_edges_moved_to_inferred_successor(
    store: Store, inferred_from: str
) -> None:
    successor = _successor(store)
    target = _thread(store)
    source = _memory(store, "Evidence retained by the replacement.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.add_link(src_ref_id=target, dst_ref_id=source, relation="cites")
        if inferred_from == "metadata":
            store.stamp_ref_meta(target, {"superseded_by": f"me{successor}"})
        else:
            store.add_link(
                src_ref_id=successor, dst_ref_id=target, relation="supersedes"
            )
    prop = _propose(store, target, "retire")
    before = _graph(store, [target, successor, source])
    before_ref = store.get_ref(kind="memory", id=target)
    assert before_ref is not None

    _apply(store, prop)
    record = _record(store, prop)
    assert any(link[3] == source for link in _links(store, successor))

    doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert _graph(store, [target, successor, source]) == before
    restored = store.get_ref(kind="memory", id=target)
    assert restored is not None and restored.retired_at is None
    assert restored.meta.get("superseded_by") == before_ref.meta.get("superseded_by")


@pytest.mark.parametrize(
    "change", ["successor_body", "endpoint_body", "endpoint_retired", "moved_link"]
)
def test_retirement_undo_refuses_changed_dependencies_without_partial_restore(
    store: Store, change: str
) -> None:
    successor = _successor(store)
    target = _thread(store, successor=successor)
    source = _memory(store, "Evidence at application time.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.add_link(
            src_ref_id=target,
            dst_ref_id=source,
            dst_pos=0,
            relation="cites",
            meta={"why": "retained source"},
        )
    prop = _propose(store, target, "supersede")
    _apply(store, prop)
    record = _record(store, prop)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        if change in {"successor_body", "endpoint_body"}:
            store.chunks.replace_body_chunk(
                successor if change == "successor_body" else source,
                "The evidence changed after application.",
                chunk_kind="memory_body",
            )
        elif change == "endpoint_retired":
            store.retire_ref(source)
        else:
            with store.pool.connection() as conn:
                conn.execute(
                    "UPDATE links SET meta = meta || %s "
                    "WHERE src_ref_id = %s AND dst_ref_id = %s AND relation = 'cites'",
                    (Jsonb({"why": "later reviewed route"}), successor, source),
                )
    before = _graph(store, [target, successor, source])
    before_reviews = _reviews(store, target)

    with pytest.raises(BadInput) as exc:
        doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert exc.value.next
    assert _graph(store, [target, successor, source]) == before
    assert _reviews(store, target) == before_reviews
    assert _record(store, prop) == record


def test_retire_undo_refuses_missing_original_chunk(store: Store) -> None:
    target = _thread(store)
    source = _memory(store, "Original external body.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.add_link(
            src_ref_id=target, dst_ref_id=source, dst_pos=0, relation="cites"
        )
    prop = _propose(store, target, "retire")
    _apply(store, prop)
    record = _record(store, prop)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.chunks.replace_body_chunk(
            source, "Original external body.", chunk_kind="memory_body"
        )
    before_reviews = _reviews(store, target)

    with pytest.raises(BadInput) as exc:
        doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert exc.value.next
    assert _links(store, target) == []
    assert _reviews(store, target) == before_reviews
    assert _record(store, prop) == record


@pytest.mark.parametrize("action", ["retire", "supersede"])
def test_retirement_snapshot_and_decision_roll_back_on_delete_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    successor = _successor(store) if action == "supersede" else None
    target = _thread(store, successor=successor)
    source = _memory(store, "Retained source.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.add_link(src_ref_id=target, dst_ref_id=source, relation="cites")
    prop = _propose(store, target, action)
    sources = [target, source, *([successor] if successor else [])]
    before = _graph(store, sources)
    before_reviews = _reviews(store, target)
    delete = MemoryHandler._delete

    def fail_after_delete(self: MemoryHandler, *args: Any, **kwargs: Any) -> Any:
        delete(self, *args, **kwargs)
        raise RuntimeError("fixture retirement failure")

    monkeypatch.setattr(MemoryHandler, "_delete", fail_after_delete)
    with pytest.raises(RuntimeError, match="fixture retirement failure"):
        _apply(store, prop)

    assert _graph(store, sources) == before
    assert _reviews(store, target) == before_reviews
    ref = store.get_ref(kind="memory", id=target)
    assert ref is not None and ref.retired_at is None
    assert "garden_retirement" not in ref.meta
    assert _body(store, target) == prop.body


@pytest.mark.parametrize("action", ["retire", "supersede"])
def test_cli_dispatches_retirement_undo(
    store: Store, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    successor = _successor(store) if action == "supersede" else None
    target = _thread(store, successor=successor)
    prop = _propose(store, target, action)
    _apply(store, prop)
    record = _record(store, prop)

    _undo(argparse.Namespace(apply_id=record["retirement_id"], by="operator"), store)

    assert record["retirement_id"] in capsys.readouterr().out
    ref = store.get_ref(kind="memory", id=target)
    assert ref is not None and ref.retired_at is None
    assert key_block(store, "ref", target, prop.candidate.key) == "rejected"


@pytest.mark.parametrize("action", ["retire", "supersede"])
def test_apply_refuses_ancestor_drift_as_retryable_stale(
    store: Store, action: str
) -> None:
    successor = _successor(store)
    target = _thread(store, successor=successor if action == "supersede" else None)
    prop = _propose(store, target, action)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        if action == "retire":
            store.add_link(src_ref_id=target, dst_ref_id=successor, relation="part-of")
        else:
            assert (
                store.remove_link(
                    src_ref_id=target, dst_ref_id=successor, relation="part-of"
                )
                == 1
            )
    assert next(
        p for p in pending_proposals(store) if p.review_id == prop.review_id
    ).current
    fresh = next(
        c for c in cand.thread_shape_candidates(store) if c.target_id == target
    )
    assert fresh.action != action
    assert fresh.evidence != prop.candidate.evidence
    before_graph = _graph(store, [target, successor])
    before_reviews = _reviews(store, target)

    with pytest.raises(doors.MergeRefused) as exc:
        _apply(store, prop)

    assert exc.value.reason == "stale"
    assert _graph(store, [target, successor]) == before_graph
    assert _reviews(store, target) == before_reviews
    assert _body(store, target) == prop.body
    assert store.get_ref(kind="memory", id=target) is not None
    assert store.get_ref(kind="memory", id=successor) is not None


def test_retire_refuses_cycle_through_inferred_successor(store: Store) -> None:
    target = _thread(store)
    parent = _memory(store, "An intermediate parent without a current thread.")
    successor = _memory(store, "The replacement has no Left line.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.add_link(src_ref_id=target, dst_ref_id=parent, relation="part-of")
        store.add_link(src_ref_id=parent, dst_ref_id=successor, relation="part-of")
        store.stamp_ref_meta(target, {"superseded_by": f"me{successor}"})
    prop = _propose(store, target, "retire")
    before_graph = _graph(store, [target, parent, successor])
    before_reviews = _reviews(store, target)

    with pytest.raises(doors.MergeRefused, match="cycle") as exc:
        doors.apply_retire(store, prop.review_id, actor="operator")

    assert exc.value.reason == "apply_refused"
    assert _graph(store, [target, parent, successor]) == before_graph
    assert _reviews(store, target) == before_reviews
    assert _body(store, target) == prop.body
    target_ref = store.get_ref(kind="memory", id=target)
    assert target_ref is not None and target_ref.retired_at is None
    assert "garden_retirement" not in target_ref.meta
