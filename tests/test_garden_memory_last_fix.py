"""Undo protects every removal route without owning unrelated successor links."""

from __future__ import annotations

import argparse

import pytest
from psycopg.types.json import Jsonb

from precis.cli.garden import _undo
from precis.errors import BadInput
from precis.graph.garden import apply as doors
from precis.graph.garden import candidates, consumer, reviewer
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import emit_proposal, key_block, pending_proposals
from precis.store import Store
from precis.store.revision_context import revision_context
from tests.test_garden_memory_apply import _apply, _body, _links, _memory, _reviews
from tests.test_garden_memory_consumer_prerequisites import _never_review
from tests.test_garden_memory_proposal_prerequisites import _inferred_retirement
from tests.test_garden_memory_retirement_undo import (
    _graph,
    _propose,
    _record,
    _successor,
    _thread,
)


def _duplicate(store: Store, loser: int, winner: int) -> GardenCandidate:
    return next(
        c
        for c in candidates.duplicate_candidates(store, kinds=("memory",))
        if c.target_id == loser and c.other_id == winner
    )


@pytest.mark.parametrize("action", ["retire", "supersede"])
def test_undone_removal_blocks_duplicate_merge_until_content_changes(
    store: Store, action: str
) -> None:
    winner = _thread(store)
    successor = _successor(store) if action == "supersede" else None
    target = _thread(store, successor=successor)
    prop = _propose(store, target, action)
    _apply(store, prop)
    doors.undo_retirement(
        store, _record(store, prop)["retirement_id"], actor="operator"
    )

    duplicate = _duplicate(store, target, winner)
    assert key_block(store, "ref", target, duplicate.key) == "rejected"
    assert emit_proposal(store, duplicate, rationale="Duplicate body") is None
    # Being a survivor does not remove the restored node.
    later = _thread(store)
    assert emit_proposal(
        store, _duplicate(store, later, target), rationale="Keep restored"
    )

    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        for ref_id in (target, winner):
            store.chunks.replace_body_chunk(
                ref_id, "A revised campaign claim.", chunk_kind="memory_body"
            )
    duplicate = _duplicate(store, target, winner)
    assert key_block(store, "ref", target, duplicate.key) is None
    assert emit_proposal(store, duplicate, rationale="Review changed claim") is not None


@pytest.mark.parametrize("action", ["retire", "supersede"])
@pytest.mark.parametrize("enabled", ["0", "1"])
def test_undone_removal_closes_already_pending_duplicate_merge(
    store: Store, monkeypatch: pytest.MonkeyPatch, action: str, enabled: str
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", enabled)
    winner = _thread(store)
    successor = _successor(store) if action == "supersede" else None
    target = _thread(store, successor=successor)
    duplicate = _duplicate(store, target, winner)
    merge = emit_proposal(store, duplicate, rationale="Duplicate body")
    assert merge is not None
    prop = _propose(store, target, action)
    _apply(store, prop)
    doors.undo_retirement(
        store, _record(store, prop)["retirement_id"], actor="operator"
    )
    pending = pending_proposals(store)
    assert [p.review_id for p in pending] == [merge]
    assert pending[0].current
    before = _graph(store, [target, winner])

    report = consumer.consume_proposals(store, review=_never_review)

    assert (report.rejected, report.attempts, report.applied) == (1, 0, 0)
    assert pending_proposals(store) == []
    assert _graph(store, [target, winner]) == before
    assert _body(store, target) == prop.body
    assert store.get_ref(kind="memory", id=target) is not None
    assert key_block(store, "ref", target, duplicate.key) == "rejected"


@pytest.mark.parametrize("change", ["new_link", "chunk_link_cascade"])
def test_undo_leaves_unrelated_successor_link_changes_alone(
    store: Store, change: str
) -> None:
    successor = _successor(store)
    target = _thread(store, successor=successor)
    neighbor = _memory(store, "Unrelated neighbor evidence.")
    link_id = None
    if change == "chunk_link_cascade":
        with revision_context(trust_origin="generated", trust_actor_type="agent"):
            link_id = store.add_link(
                src_ref_id=neighbor, src_pos=0, dst_ref_id=successor, relation="cites"
            ).id
    prop = _propose(store, target, "supersede")
    before_target = _links(store, target)
    _apply(store, prop)
    record = _record(store, prop)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        if change == "new_link":
            link_id = store.add_link(
                src_ref_id=successor, dst_ref_id=neighbor, relation="cites"
            ).id
        else:
            store.chunks.replace_body_chunk(
                neighbor, "Corrected neighbor evidence.", chunk_kind="memory_body"
            )
            assert not any(l[0] == link_id for l in _links(store, neighbor))
    untouched = _graph(store, [successor, neighbor])
    affected = {
        l["link_id"] for l in record["applied_links"] if l not in record["links"]
    }

    doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert _links(store, target) == before_target
    assert _graph(store, [successor, neighbor]) == {
        ref_id: [l for l in links if l[0] not in affected]
        for ref_id, links in untouched.items()
    }
    if change == "new_link":
        assert any(l[0] == link_id for l in _links(store, successor))
    else:
        assert _body(store, neighbor) == "Corrected neighbor evidence."
        assert not any(l[0] == link_id for l in _links(store, neighbor))


@pytest.mark.parametrize("change", ["migrated", "added", "collision"])
def test_undo_refuses_changes_to_rows_it_would_rewrite_or_delete(
    store: Store, change: str
) -> None:
    successor = _successor(store)
    target = _thread(store, successor=successor)
    source = _memory(store, "Preserved source.")
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.add_link(src_ref_id=target, dst_ref_id=source, relation="cites")
    prop = _propose(store, target, "supersede")
    _apply(store, prop)
    record = _record(store, prop)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        if change == "collision":
            store.add_link(src_ref_id=target, dst_ref_id=source, relation="cites")
        else:
            relation = "cites" if change == "migrated" else "supersedes"
            with store.pool.connection() as conn:
                row = conn.execute(
                    "UPDATE links SET meta = meta || %s WHERE src_ref_id = %s "
                    "AND relation = %s RETURNING link_id",
                    (Jsonb({"why": "New review"}), successor, relation),
                ).fetchone()
            assert row is not None
    before = (_graph(store, [target, successor, source]), _reviews(store, target))

    with pytest.raises(BadInput, match="undo is unsafe"):
        doors.undo_retirement(store, record["retirement_id"], actor="operator")

    assert (
        _graph(store, [target, successor, source]),
        _reviews(store, target),
    ) == before
    assert _record(store, prop) == record


@pytest.mark.parametrize("issue", ["drift", "md_successor"])
@pytest.mark.parametrize("enabled", ["0", "1"])
def test_sampled_review_prerequisites_only_apply_with_flag_on(
    store: Store, monkeypatch: pytest.MonkeyPatch, issue: str, enabled: str
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", enabled)
    proposal, successor = _inferred_retirement(store)
    if issue == "md_successor":
        proposal.payload["successor"] = "md:main/docs/source.md"
    else:
        with revision_context(trust_origin="generated", trust_actor_type="agent"):
            store.chunks.replace_body_chunk(
                successor, "Changed successor evidence.", chunk_kind="memory_body"
            )
    calls = []
    expected = reviewer.ReviewerVerdict(
        verdict="approve", why="Model verdict", model="fixture"
    )

    def review(_store: Store, prop: object) -> reviewer.ReviewerVerdict:
        calls.append(prop)
        return expected

    verdict = reviewer._call_verdict(review, store, proposal)

    if enabled == "0":
        assert calls == [proposal]
        assert verdict is expected
    else:
        assert calls == []
        assert verdict.verdict == "reject"
        assert verdict.model is None
        assert (
            "shown and pinned" in verdict.why
            if issue == "md_successor"
            else "changed" in verdict.why
        )


def test_cli_retirement_undo_reports_strict_legacy_restamp(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _thread(store)
    source = _memory(store, "Legacy evidence.")
    with revision_context(trust_origin="", trust_actor_type="agent"):
        store.add_link(src_ref_id=target, dst_ref_id=source, relation="cites")
    prop = _propose(store, target, "retire")
    _apply(store, prop)
    record = _record(store, prop)
    store.activate_trust_strict(lock_timeout="5s")
    try:
        _undo(
            argparse.Namespace(apply_id=record["retirement_id"], by="operator"), store
        )
        assert "1 link(s) restamped" in capsys.readouterr().out
    finally:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE trust_write_state SET mode = 'compat', strict_at = NULL"
            )
