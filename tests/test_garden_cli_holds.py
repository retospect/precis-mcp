"""Held garden proposals remain visible and apply errors can be retried."""

from __future__ import annotations

import argparse
import json

import pytest

from precis.cli import garden
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.consumer import _hold_for_human
from precis.graph.garden.proposals import (
    GardenProposal,
    emit_proposal,
    key_block,
    pending_proposals,
)
from precis.store import Store
from precis.store.types import ChunkInsert


def _proposal(store: Store) -> GardenProposal:
    ref = store.insert_ref(kind="memory", slug=None, title="Memory CLI fixture")
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
    return next(p for p in pending_proposals(store) if p.review_id == review_id)


@pytest.mark.parametrize("reason", ["human", "apply_error"])
def test_proposals_show_memory_hold_reason(
    store: Store, capsys: pytest.CaptureFixture[str], reason: str
) -> None:
    prop = _proposal(store)
    _hold_for_human(
        store, prop, reason=reason, why="Detailed hold explanation", identity=None
    )
    # Other review actors and malformed notes must not override the hold.
    store.record_target_review(
        "ref",
        prop.target_id,
        actor="garden-consumer",
        verdict="proposed",
        note='not JSON "garden_review"',
    )
    store.record_target_review(
        "ref",
        prop.target_id,
        actor="garden-reviewer",
        verdict="proposed",
        note=json.dumps(
            {
                "garden_review": {
                    "proposal": prop.review_id,
                    "held": "wrong actor",
                    "why": "irrelevant",
                }
            }
        ),
    )
    garden._proposals(argparse.Namespace(action="rewrite", limit=200), store)

    output = capsys.readouterr().out
    assert f"review {prop.review_id}: rewrite ref:{prop.target_id}" in output
    assert f"held: {reason} — Detailed hold explanation" in output
    if reason == "apply_error":
        assert "fix the error, then reject to retry" in output
        assert "human protection or unresolved evidence" not in output
    assert "wrong actor" not in output
    assert [p.review_id for p in pending_proposals(store)] == [prop.review_id]


@pytest.mark.parametrize("reason", [None, "human", "apply_error"])
def test_reject_only_reopens_apply_error_holds(
    store: Store, reason: str | None
) -> None:
    prop = _proposal(store)
    if reason:
        _hold_for_human(store, prop, reason=reason, why="fixture", identity=None)

    garden._reject(
        argparse.Namespace(review_id=str(prop.review_id), by="operator"), store
    )

    assert pending_proposals(store) == []
    assert key_block(store, "ref", prop.target_id, prop.key) == (
        None if reason == "apply_error" else "rejected"
    )
    rejection = store.reviews_for("ref", prop.target_id)[0]
    assert rejection.note is not None
    decision = json.loads(rejection.note)["garden"]
    assert bool(decision.get("retryable")) is (reason == "apply_error")
    if reason == "apply_error":
        assert decision["reason"] == "apply_error"


@pytest.mark.parametrize("action", ["rewrite", "supersede", "retire"])
def test_proposals_accept_memory_action_filters(action: str) -> None:
    parser = argparse.ArgumentParser()
    garden.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["garden", "proposals", "--action", action])
    assert args.action == action
