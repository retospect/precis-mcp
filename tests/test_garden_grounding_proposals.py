"""Grounding repair proposals (gr634486 slice 4): proposing and
approving stay distinct — a rollup of ``supported`` is withheld when
the audit actor certified the evidence itself, and repair proposals go
through the existing garden ledger with no apply door, so a missing
door leaves them pending for a human or reviewer.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.graph.garden import grounding as g
from precis.graph.garden.proposals import (
    MEMORY_APPLY_DETECTORS,
    pending_proposals,
)
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.utils.handle_registry import try_format

QUOTE = "The catalyst doubles the yield"


def _paper(store: Store, slug: str) -> tuple[int, str]:
    """A paper with one body chunk; returns (paper_id, chunk handle)."""
    paper = store.insert_ref(kind="paper", slug=slug, title="Source paper")
    (chunk,) = store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text="The catalyst doubles the yield in vitro.")]
    )
    handle = try_format("paper", int(chunk.id), chunk=True)
    assert handle is not None
    return int(paper.id), handle


def _memory(store: Store, body: str) -> int:
    memory = store.insert_ref(
        kind="memory", slug=None, title="Grounding proposals fixture"
    )
    store.chunks.insert_chunks(
        memory.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
    )
    store.add_tag(memory.id, Tag.open("section:reference"), set_by="system")
    return int(memory.id)


def _memory_citing_paper(store: Store) -> tuple[int, int, str]:
    """A memory whose body cites a real paper chunk handle."""
    import uuid

    paper_id, handle = _paper(store, f"grounding-proposal-{uuid.uuid4().hex[:12]}")
    memory_id = _memory(store, f"{QUOTE} ({handle}).")
    return memory_id, paper_id, handle


def _classify(handle: str) -> g.Call:
    def call(prompt: str) -> tuple[str, str | None]:
        assert handle in prompt
        return (
            json.dumps(
                {
                    "claims": [
                        {
                            "i": 0,
                            "quote": QUOTE,
                            "kind": "empirical",
                            "status": "supported",
                            "evidence": [handle],
                        }
                    ]
                }
            ),
            None,
        )

    return call


def _classify_unresolved_premise() -> g.Call:
    def call(_prompt: str) -> tuple[str, str | None]:
        return (
            json.dumps(
                {
                    "claims": [
                        {
                            "i": 0,
                            "quote": QUOTE,
                            "kind": "empirical",
                            "status": "unresolved",
                            "premises": ["me999"],
                            "evidence": [],
                        }
                    ]
                }
            ),
            None,
        )

    return call


def _raw(**kw: Any) -> g.RawSupport:
    base: dict[str, Any] = {
        "supported": True,
        "source_fidelity": "faithful",
        "evidential_strength": "established",
        "study_type": "experiment",
        "quantities_match": True,
        "conditions_match": True,
    }
    base.update(kw)
    return g.RawSupport(**base)


def _audit(store: Store, memory_id: int, call: g.Call, raw: g.RawSupport) -> None:
    (candidate,) = [
        c for c in g.grounding_audit_candidates(store) if c.target_id == memory_id
    ]
    assert (
        g.audit_candidate(store, candidate, call=call, verify_fn=lambda _c, _p: raw)
        is not None
    )


def _rollup_of(store: Store, memory_id: int) -> str:
    record = g.latest_audit(store, memory_id)
    assert record is not None
    (claim,) = [c for c in record.claims if c.kind == "empirical"]
    return claim.rollup


def test_proposer_cannot_certify_own_new_evidence(store: Store) -> None:
    plain_id, _plain_paper, plain_handle = _memory_citing_paper(store)
    self_id, self_paper, self_handle = _memory_citing_paper(store)
    # The audit actor already certified the self memory's evidence —
    # the proposer supplying its own proof.
    store.record_target_review(
        "ref",
        self_paper,
        actor=g.AUDIT_ACTOR,
        verdict="proposed",
        note="garden-grounding evidence certification",
    )
    _audit(store, plain_id, _classify(plain_handle), _raw())
    _audit(store, self_id, _classify(self_handle), _raw())
    assert _rollup_of(store, plain_id) == "supported"
    assert _rollup_of(store, self_id) == "partial"


def test_missing_apply_door_leaves_proposal_pending(store: Store) -> None:
    assert "grounding_audit" not in MEMORY_APPLY_DETECTORS
    memory_id, _paper_id, handle = _memory_citing_paper(store)
    _audit(
        store,
        memory_id,
        _classify(handle),
        _raw(supported=False, reason="passage does not contain the claim"),
    )
    proposals = pending_proposals(store)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.detector == "grounding_audit"
    assert proposal.action == "rewrite"
    assert proposal.target_id == memory_id
    # The audit row is pinned and the proposal's dedup key is pending —
    # re-running the window neither re-audits nor re-proposes. The current
    # memory stays listed only as the baseline starvation-guard slot
    # (gr634490): listing writes nothing and the pending proposal dedups.
    remaining = g.grounding_audit_candidates(store)
    assert [c.target_id for c in remaining] == [memory_id]
    assert remaining[0].payload["baseline"] is True
    assert len(pending_proposals(store)) == 1


def test_unresolvable_premise_proposes_ask(store: Store) -> None:
    memory_id = _memory(store, f"{QUOTE} (me999).")
    _audit(store, memory_id, _classify_unresolved_premise(), _raw())
    proposals = pending_proposals(store)
    assert len(proposals) == 1
    assert proposals[0].action == "ask"
    assert proposals[0].detector == "grounding_audit"
    record = g.latest_audit(store, memory_id)
    assert record is not None
    assert record.coverage["rollup"]["unresolved"] == 1


def test_supported_claim_proposes_nothing(store: Store) -> None:
    memory_id, _paper_id, handle = _memory_citing_paper(store)
    _audit(store, memory_id, _classify(handle), _raw())
    assert pending_proposals(store) == []
    assert _rollup_of(store, memory_id) == "supported"


def test_overreach_proposes_qualify(store: Store) -> None:
    memory_id, _paper_id, handle = _memory_citing_paper(store)
    _audit(store, memory_id, _classify(handle), _raw(overreach=True))
    proposals = pending_proposals(store)
    assert len(proposals) == 1
    assert proposals[0].action == "qualify"
    assert _rollup_of(store, memory_id) == "partial"


def test_garden_ledger_actor_certification_is_self_certified(store: Store) -> None:
    # Repair proposals carry actor 'graph-garden'; certification by that
    # actor must cap the rollup exactly like the audit actor's own.
    from precis.graph.garden.proposals import GARDEN_ACTOR

    plain_id, _plain_paper, plain_handle = _memory_citing_paper(store)
    self_id, self_paper, self_handle = _memory_citing_paper(store)
    store.record_target_review(
        "ref",
        self_paper,
        actor=GARDEN_ACTOR,
        verdict="proposed",
        note='{"garden": {"key": "evidence-supply"}}',
    )
    _audit(store, plain_id, _classify(plain_handle), _raw())
    _audit(store, self_id, _classify(self_handle), _raw())
    assert _rollup_of(store, plain_id) == "supported"
    assert _rollup_of(store, self_id) == "partial"


def _two_claim_classify(handle: str) -> g.Call:
    """Two empirical claims, one per sentence, both citing ``handle``."""
    second = "The catalyst halves the cost"

    def call(prompt: str) -> tuple[str, str | None]:
        assert handle in prompt
        return (
            json.dumps(
                {
                    "claims": [
                        {
                            "i": 0,
                            "quote": QUOTE,
                            "kind": "empirical",
                            "status": "supported",
                            "evidence": [handle],
                        },
                        {
                            "i": 1,
                            "quote": second,
                            "kind": "empirical",
                            "status": "supported",
                            "evidence": [handle],
                        },
                    ]
                }
            ),
            None,
        )

    return call


def _memory_citing_paper_two_claims(store: Store) -> tuple[int, int, str]:
    import uuid

    paper_id, handle = _paper(store, f"grounding-proposal-{uuid.uuid4().hex[:12]}")
    memory_id = _memory(store, f"{QUOTE} ({handle}). The catalyst halves the cost.")
    return memory_id, paper_id, handle


def test_repairs_for_different_spans_do_not_collide(store: Store) -> None:
    memory_id, _paper_id, handle = _memory_citing_paper_two_claims(store)
    _audit(
        store,
        memory_id,
        _two_claim_classify(handle),
        _raw(supported=False, reason="passage does not contain the claim"),
    )
    proposals = pending_proposals(store)
    assert len(proposals) == 2
    assert all(p.action == "rewrite" for p in proposals)
    assert len({p.key for p in proposals}) == 2
    spans = sorted(tuple(p.payload["span"]) for p in proposals)
    assert spans[0] != spans[1]
    assert all(p.payload["audit_sha"] for p in proposals)


def test_audit_candidate_repair_limit_caps_emitted(store: Store) -> None:
    memory_id, _paper_id, handle = _memory_citing_paper_two_claims(store)
    (candidate,) = [
        c for c in g.grounding_audit_candidates(store) if c.target_id == memory_id
    ]
    result = g.audit_candidate(
        store,
        candidate,
        call=_two_claim_classify(handle),
        verify_fn=lambda _c, _p: _raw(
            supported=False, reason="passage does not contain the claim"
        ),
        repair_limit=1,
    )
    assert result is not None
    _review_id, repairs = result
    assert repairs == 1
    assert len(pending_proposals(store)) == 1


def test_repairs_spend_the_shared_tick_budget(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.graph.garden.runner import run_garden_tick

    memory_id, _paper_id, handle = _memory_citing_paper_two_claims(store)
    monkeypatch.setattr(g, "_routed_call", lambda _ref_id: _two_claim_classify(handle))
    monkeypatch.setattr(
        g,
        "_reviewer_support",
        lambda _c, _p: _raw(
            supported=False, reason="passage does not contain the claim"
        ),
    )
    tick = run_garden_tick(store, passes=("memory-grounding",), max_proposals=2)
    # The audit row plus one capped repair: three rows were possible,
    # the budget stopped the tick at two.
    assert tick.proposals == 2
    assert tick.proposals_by_detector == {"grounding_audit": 2}
    assert len(pending_proposals(store)) == 1
    assert g.latest_audit(store, memory_id) is not None
