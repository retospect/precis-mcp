"""Memory successor evidence and undo deduplication prerequisites."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.graph.garden import apply as doors
from precis.graph.garden import candidates, consumer, reviewer
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import (
    GardenProposal,
    emit_proposal,
    key_block,
    memory_retirement_drift,
    memory_successor_refusal,
    pending_proposals,
    proposal_ref_ids,
    record_rejection,
)
from precis.graph.garden.proposer import ref_excerpts
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.store.types import Tag
from tests.test_garden_memory_apply import _memory, _proposal


@pytest.mark.parametrize("location", ["payload", "mention"])
@pytest.mark.parametrize(
    "successor", ["md:main/docs/source.md", "python:main::pkg.m.helper"]
)
def test_md_successor_refusal_names_missing_content_pin(
    location: str, successor: str
) -> None:
    payload: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    if location == "payload":
        payload["successor"] = successor
    else:
        evidence["mentions"] = [{"successor": successor}]
    reason = memory_successor_refusal(payload, evidence)
    assert reason is not None
    assert "shown and pinned" in reason
    assert "live ref successor" in reason
    assert memory_successor_refusal({"successor": "me123"}, {}) is None


def test_sampled_md_successor_refusal_does_not_buy_review(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = GardenProposal(
        review_id=1,
        target_kind="ref",
        target_id=1,
        action="rewrite",
        key="rewrite:ref:1:0",
        detector="dangling_mention",
        title="Former source",
        payload={},
        evidence={"mentions": [{"successor": "md:main/docs/source.md"}]},
        rationale="Source moved",
        model=None,
        at=datetime.now(UTC),
        current=True,
    )

    def unexpected(
        _store: Store, _proposal: GardenProposal
    ) -> reviewer.ReviewerVerdict:
        pytest.fail("an unpinned document successor must not buy a model review")

    verdict = reviewer._call_verdict(unexpected, store, prop)
    assert verdict.verdict == "reject"
    assert "md: successors are unsupported" in verdict.why


def test_md_successor_refused_before_consumer_review_and_direct_apply(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _proposal(
        store,
        change_evidence=lambda evidence: evidence["mentions"][0].update(
            successor="md:main/docs/source.md"
        ),
    )
    with pytest.raises(doors.MergeRefused, match="md: successors are unsupported"):
        doors.apply_rewrite(store, prop.review_id, actor="fixture")

    def unexpected(
        _store: Store, _proposal: GardenProposal
    ) -> consumer.ConsumerVerdict:
        pytest.fail("an unpinned document successor must not buy a model review")

    report = consumer.consume_proposals(store, review=unexpected)
    assert report.attempts == 0
    assert report.rejected == 1
    assert key_block(store, "ref", prop.target, prop.candidate.key) == "rejected"


def _anchor_proposal(store: Store) -> GardenCandidate:
    target = _memory(store, "Route: docs/former.md")
    with store.pool.connection() as conn:
        row = conn.execute("SELECT precis_target_sha('ref', %s)", (target,)).fetchone()
        assert row is not None
        sha = row[0]
    return GardenCandidate(
        detector="anchor_rot",
        action="rewrite",
        target_kind="ref",
        target_id=target,
        evidence={"content_sha": sha, "main_sha": "old-main"},
        payload={
            "base_sha": sha,
            "main_sha": "old-main",
            "ops": [{"find": "docs/former.md", "text": "docs/current.md"}],
        },
    )


def test_anchor_undo_stays_rejected_across_main_commits(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _anchor_proposal(store)
    monkeypatch.setattr(
        candidates, "anchor_rot_candidates", lambda *_a, **_kw: [candidate]
    )
    proposal = emit_proposal(store, candidate, rationale="Source moved")
    assert proposal is not None
    applied = doors.apply_rewrite(store, proposal, actor="fixture")
    doors.undo_rewrite(store, applied.rewrite_id, actor="fixture")
    changed = replace(candidate, payload={**candidate.payload, "main_sha": "new-main"})
    assert changed.key != candidate.key
    assert key_block(store, "ref", candidate.target_id, changed.key) == "rejected"
    assert emit_proposal(store, changed, rationale="Main moved") is None

    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.chunks.replace_body_chunk(
            candidate.target_id,
            "New route: docs/former.md",
            chunk_kind="memory_body",
            source="fixture",
            reason="The author changed the claim",
        )
    assert key_block(store, "ref", candidate.target_id, changed.key) is None


def test_anchor_model_rejection_can_reopen_on_main_change(store: Store) -> None:
    candidate = _anchor_proposal(store)
    record_rejection(
        store, candidate, rationale="Historical route", prompt_version="fixture"
    )
    changed = replace(candidate, payload={**candidate.payload, "main_sha": "new-main"})
    assert key_block(store, "ref", candidate.target_id, changed.key) is None


def _inferred_retirement(store: Store) -> tuple[GardenProposal, int]:
    target = _memory(store, "Earlier campaign summary.")
    successor = _memory(store, "Preserved campaign result.")
    store.add_tag(target, Tag.open("section:threads"), set_by="system")
    store.add_link(src_ref_id=successor, dst_ref_id=target, relation="supersedes")
    candidate = next(
        c for c in candidates.thread_shape_candidates(store) if c.target_id == target
    )
    assert candidate.action == "retire"
    proposal = emit_proposal(store, candidate, rationale="Results preserved")
    assert proposal is not None
    pending = next(p for p in pending_proposals(store) if p.review_id == proposal)
    return pending, successor


def test_inferred_retirement_successor_is_shown_and_pinned(store: Store) -> None:
    proposal, successor = _inferred_retirement(store)
    assert proposal.payload["delete_successor"] == f"me{successor}"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT precis_target_sha('ref', %s)", (successor,)
        ).fetchone()
        assert row is not None
        sha = row[0]
    assert proposal.payload["delete_successor_sha"] == sha
    assert successor in proposal_ref_ids(proposal)
    excerpts = ref_excerpts(store, proposal_ref_ids(proposal))
    assert "Preserved campaign result." in consumer._prompt_for(proposal, excerpts)
    assert "Preserved campaign result." in reviewer._prompt_for(proposal, excerpts)
    assert memory_retirement_drift(store, proposal) is None


def test_inferred_retirement_successor_edit_closes_retryably_before_review(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    proposal, successor = _inferred_retirement(store)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.chunks.replace_body_chunk(
            successor,
            "Different campaign result.",
            chunk_kind="memory_body",
            source="fixture",
            reason="The preserved result changed",
        )
    assert memory_retirement_drift(store, proposal) is not None
    with pytest.raises(doors.MergeRefused) as exc:
        doors.apply_retire(store, proposal.review_id, actor="fixture")
    assert exc.value.reason == "stale"

    def unexpected(
        _store: Store, _proposal: GardenProposal
    ) -> consumer.ConsumerVerdict:
        pytest.fail("changed inferred successor must close before paid review")

    report = consumer.consume_proposals(store, review=unexpected)
    assert report.attempts == 0
    assert report.rejected == 1
    assert key_block(store, "ref", proposal.target_id, proposal.key) is None


def test_retirement_no_successor_pin_detects_new_route(store: Store) -> None:
    original = _proposal(store, "retire")
    proposal = next(
        p for p in pending_proposals(store) if p.review_id == original.review_id
    )
    assert "delete_successor" in proposal.payload
    assert proposal.payload["delete_successor"] is None
    assert proposal.payload["delete_successor_sha"] is None
    successor = _memory(store, "New preservation route.")
    store.add_link(
        src_ref_id=successor, dst_ref_id=proposal.target_id, relation="supersedes"
    )
    assert memory_retirement_drift(store, proposal) is not None
