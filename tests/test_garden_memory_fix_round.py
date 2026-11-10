"""Retirement restoration and bounded review regressions from the fix round."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.errors import QueryCanceled

from precis.graph.garden import apply as doors
from precis.graph.garden import candidates, consumer, reviewer
from precis.graph.garden.proposals import (
    GardenProposal,
    emit_proposal,
    key_block,
    pending_proposals,
)
from precis.store import Store
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.store.revision_context import revision_context
from tests.test_garden_memory_apply import (
    _apply,
    _body,
    _links,
    _memory,
    _proposal,
    _reviews,
)
from tests.test_garden_memory_consumer_prerequisites import _never_review
from tests.test_garden_memory_retirement_undo import (
    _graph,
    _propose,
    _record,
    _successor,
    _thread,
)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("action", ["retire", "supersede"])
def test_retirement_undo_restores_legacy_link_fields(
    store: Store, action: str, strict: bool
) -> None:
    successor = _successor(store) if action == "supersede" else None
    target = _thread(store, successor=successor)
    source = _memory(store, "Evidence attached before trust stamping existed.")
    with revision_context(trust_origin="", trust_actor_type="agent"):
        link = store.add_link(
            src_ref_id=target,
            src_pos=0,
            dst_ref_id=source,
            dst_pos=0,
            relation="cites",
            set_by="user",
            meta={"why": "Original source", "route": ["source", "claim"]},
        )
    before = next(row for row in _links(store, target) if row[0] == link.id)
    assert before[8:11] == ("unknown", "unknown", 0)
    prop = _propose(store, target, action)
    if strict:
        store.activate_trust_strict(lock_timeout="5s")
    try:
        _apply(store, prop)
        record = _record(store, prop)
        result = doors.undo_retirement(store, record["retirement_id"], actor="operator")
        after = next(row for row in _links(store, target) if row[0] == link.id)
        assert after[:10] == before[:10]
        assert after[11:] == before[11:]
        assert after[10] == int(strict)
        assert result["links_restamped"] == int(strict)
        assert _body(store, target) == prop.body
    finally:
        if strict:
            with store.pool.connection() as conn:
                conn.execute(
                    "UPDATE trust_write_state SET mode = 'compat', strict_at = NULL"
                )


@pytest.mark.parametrize("action", ["retire", "supersede"])
def test_retirement_undo_ignores_chunk_read_statistics(
    store: Store, action: str
) -> None:
    successor = _successor(store) if action == "supersede" else None
    target = _thread(store, successor=successor)
    source = _memory(store, "The cited evidence has been read since retirement.")
    with revision_context(trust_origin="extracted", trust_actor_type="agent"):
        store.add_link(
            src_ref_id=target,
            src_pos=0,
            dst_ref_id=source,
            dst_pos=0,
            relation="cites",
        )
    prop = _propose(store, target, action)
    before = _links(store, target)
    _apply(store, prop)
    record = _record(store, prop)

    assert store.chunks.bump_salience_for_ref(source) == 1
    doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert _links(store, target) == before
    assert _body(store, target) == prop.body
    with store.pool.connection() as conn:
        accesses = conn.execute(
            "SELECT accesses FROM chunks WHERE ref_id = %s AND ord = 0", (source,)
        ).fetchone()
    assert accesses is not None and accesses[0] > 0


@pytest.mark.parametrize("route", ["competing_supersedes", "retired_metadata"])
def test_unresolved_retirement_closes_once_across_three_ticks(
    store: Store, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    target = _thread(store)
    first = _memory(store, "First preservation route.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        if route == "competing_supersedes":
            second = _memory(store, "Competing preservation route.")
            for successor in (first, second):
                store.add_link(
                    src_ref_id=successor, dst_ref_id=target, relation="supersedes"
                )
        else:
            store.retire_ref(first)
            store.stamp_ref_meta(target, {"superseded_by": f"me{first}"})
    prop = _propose(store, target, "retire")

    def retirement_reviews() -> list[tuple[Any, ...]]:
        return [
            row
            for row in _reviews(store, target)
            if json.loads(row[3]).get("garden", {}).get("key") == prop.candidate.key
        ]

    reports = []
    for tick in range(3):
        fresh = next(
            c
            for c in candidates.thread_shape_candidates(store)
            if c.target_id == target
        )
        assert emit_proposal(store, fresh, rationale="The route is unchanged") is None
        reports.append(consumer.consume_proposals(store, review=_never_review))
        assert pending_proposals(store) == []
        assert key_block(store, "ref", target, prop.candidate.key) == "rejected"
        reviews = retirement_reviews()
        assert len(reviews) == 2
        assert [row[2] for row in reviews] == ["proposed", "rejected"]
        assert reports[tick].attempts == 0

    assert sum(report.rejected for report in reports) == 1
    assert [row[2] for row in retirement_reviews()] == ["proposed", "rejected"]
    assert store.get_ref(kind="memory", id=target) is not None


def test_supersede_undo_allows_edit_to_unrelated_successor_neighbor(
    store: Store,
) -> None:
    successor = _successor(store)
    target = _thread(store, successor=successor)
    neighbor = _memory(store, "An unrelated claim cited only by the successor.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.add_link(src_ref_id=successor, dst_ref_id=neighbor, relation="cites")
    prop = _propose(store, target, "supersede")
    before = _graph(store, [target, successor, neighbor])
    _apply(store, prop)
    record = _record(store, prop)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.chunks.replace_body_chunk(
            neighbor,
            "The unrelated claim has been corrected.",
            chunk_kind="memory_body",
        )

    doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert _graph(store, [target, successor, neighbor]) == before
    assert _body(store, neighbor) == "The unrelated claim has been corrected."
    assert _body(store, target) == prop.body


def test_supersede_undo_keeps_unchanged_successor_measure_anchor(store: Store) -> None:
    successor = _successor(store)
    target = _thread(store, successor=successor)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        temperature = store.insert_ref(
            kind="taxon",
            slug=None,
            title="recorded temperature",
            meta={
                "name": "recorded temperature",
                "norm_name": "recorded temperature",
                "slug": "recorded-temperature",
                "definition": "Temperature recorded by the source.",
                "aliases": [],
                "status": "proposed",
                "canonical_unit": "K",
            },
        )
        with store.pool.connection() as conn:
            chunk = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0",
                (successor,),
            ).fetchone()
        assert chunk is not None
        run = store.insert_measure(
            MeasureSpec(
                measurand_ref_id=temperature.id,
                subject_ref_id=successor,
                literal="300",
                reported_unit="K",
                tier="asserted",
                source_ref_id=successor,
                anchor=MeasureAnchor(successor, int(chunk[0]), "sentence", "s1"),
            ),
            actor="reader",
        )
    (before_measure,) = [
        row for row in store.measures_for(successor) if row["id"] == run.output_id
    ]
    assert before_measure["primary_link_id"] is not None
    with store.pool.connection() as conn:
        anchored_link = conn.execute(
            "SELECT to_jsonb(l) FROM links l WHERE link_id = %s",
            (before_measure["primary_link_id"],),
        ).fetchone()
        assert anchored_link is not None
        with pytest.raises(doors.MergeRefused, match="anchor measures") as exc:
            doors._guard_memory_link_measures(conn, [anchored_link[0]])
        assert exc.value.next
    prop = _propose(store, target, "supersede")
    before = _graph(store, [target, successor])
    _apply(store, prop)
    record = _record(store, prop)

    doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert _graph(store, [target, successor]) == before
    (after_measure,) = [
        row for row in store.measures_for(successor) if row["id"] == run.output_id
    ]
    assert after_measure["primary_link_id"] == before_measure["primary_link_id"]
    assert after_measure["anchor_lost"] is False


def test_restore_link_missing_chunk_refusal_has_next_step(store: Store) -> None:
    target = _memory(store, "Original source.")
    destination = _memory(store, "Original destination.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        link = store.add_link(
            src_ref_id=target, dst_ref_id=destination, relation="cites"
        )
    before = _links(store, target)
    with store.atomic() as conn:
        original = conn.execute(
            "SELECT to_jsonb(l) FROM links l WHERE link_id = %s", (link.id,)
        ).fetchone()
        assert original is not None
        missing = conn.execute(
            "SELECT COALESCE(MAX(chunk_id), 0) + 1 FROM chunks"
        ).fetchone()
        assert missing is not None
        snapshot = {**original[0], "src_chunk_id": int(missing[0])}
        with pytest.raises(doors.MergeRefused, match="original chunk") as exc:
            doors._restore_memory_link(store, conn, snapshot, actor="operator")
        assert exc.value.next
    assert _links(store, target) == before


def test_sampled_retirement_drift_failure_is_a_proposal_error(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    target = _thread(store)
    prop = _propose(store, target, "retire")
    pending = next(p for p in pending_proposals(store) if p.review_id == prop.review_id)

    def fail_drift(*_args: Any, **_kwargs: Any) -> None:
        raise QueryCanceled("Timed out resolving the retirement route")

    def unexpected(
        _store: Store, _proposal: GardenProposal
    ) -> reviewer.ReviewerVerdict:
        pytest.fail("A failed prerequisite read must not buy a review")

    monkeypatch.setattr(reviewer, "memory_retirement_drift", fail_drift)

    verdict = reviewer._call_verdict(unexpected, store, pending)

    assert verdict.verdict == ""
    assert verdict.error == "Timed out resolving the retirement route"
    assert pending_proposals(store) == [pending]


@pytest.mark.parametrize("action", ["supersede", "retire"])
def test_retirement_undo_blocks_other_removal_routes_until_target_changes(
    store: Store, action: str
) -> None:
    successor = _successor(store)
    target = _thread(store, successor=successor)
    alternative = _successor(store)
    prop = _propose(store, target, "supersede")
    _apply(store, prop)
    record = _record(store, prop)
    doors.undo_retirement(store, record["retirement_id"], actor="operator")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        assert (
            store.remove_link(
                src_ref_id=target, dst_ref_id=successor, relation="part-of"
            )
            == 1
        )
        if action == "supersede":
            store.add_link(
                src_ref_id=target, dst_ref_id=alternative, relation="part-of"
            )
    changed_route = next(
        c for c in candidates.thread_shape_candidates(store) if c.target_id == target
    )
    assert changed_route.action == action
    assert changed_route.key != prop.candidate.key
    assert key_block(store, "ref", target, changed_route.key) == "rejected"
    assert emit_proposal(store, changed_route, rationale="Try another route") is None

    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.chunks.replace_body_chunk(
            target,
            prop.body + "The author changed this claim.\n",
            chunk_kind="memory_body",
        )

    assert key_block(store, "ref", target, changed_route.key) is None
    changed_route = next(
        c for c in candidates.thread_shape_candidates(store) if c.target_id == target
    )
    assert (
        emit_proposal(store, changed_route, rationale="Review changed claim")
        is not None
    )


def test_retirement_undo_blocks_already_pending_alternate_action(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    target = _thread(store)
    retirement = _propose(store, target, "retire")
    successor = _successor(store)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.add_link(src_ref_id=target, dst_ref_id=successor, relation="part-of")
    supersede = _propose(store, target, "supersede")
    _apply(store, supersede)
    record = _record(store, supersede)
    doors.undo_retirement(store, record["retirement_id"], actor="operator")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        assert (
            store.remove_link(
                src_ref_id=target, dst_ref_id=successor, relation="part-of"
            )
            == 1
        )
    pending = pending_proposals(store)
    assert [p.review_id for p in pending] == [retirement.review_id]
    assert pending[0].current
    fresh = next(
        c for c in candidates.thread_shape_candidates(store) if c.target_id == target
    )
    assert fresh.action == "retire"
    assert fresh.key == retirement.candidate.key
    before = (_graph(store, [target, successor]), _reviews(store, target))

    with pytest.raises(doors.MergeRefused):
        doors.apply_retire(store, retirement.review_id, actor="operator")

    assert (_graph(store, [target, successor]), _reviews(store, target)) == before
    report = consumer.consume_proposals(store, review=_never_review)
    assert (report.rejected, report.attempts, report.applied) == (1, 0, 0)
    assert pending_proposals(store) == []
    assert key_block(store, "ref", target, retirement.candidate.key) == "rejected"
    assert _graph(store, [target, successor]) == before[0]
    assert _body(store, target) == retirement.body


def test_unresolved_hold_names_age_in_hours(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")

    def unresolved(payload: dict[str, Any]) -> None:
        for op in payload["ops"]:
            op["text"] = None

    prop = _proposal(store, change_payload=unresolved)
    old = replace(
        pending_proposals(store)[0],
        at=datetime.now(UTC) - consumer.MAX_UNRESOLVED_AGE - timedelta(seconds=1),
    )
    report = consumer.ConsumeReport()

    consumer.review_pending(store, [old], report, review=_never_review, limit=1)

    assert (report.held, report.attempts) == (1, 0)
    hold = json.loads(_reviews(store, prop.target)[-1][3])["garden_review"]
    assert "after 24 hours" in hold["why"]
