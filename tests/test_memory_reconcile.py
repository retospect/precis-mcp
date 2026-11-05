"""Shared memory reconciliation reaches interactive and garden judging paths.

Model routes are captured, never invoked. Store fixtures use only the isolated
test database, including the rejection-cache check through the actual runner.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from precis.graph.garden import consumer, proposer, reviewer, runner
from precis.graph.garden.candidates import DetectorRun, GardenCandidate
from precis.graph.garden.proposals import GardenProposal, record_rejection
from precis.graph.garden.proposer import Verdict
from precis.ingest.skill_template import DocResolver, IncludeError, Includer
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.utils import memory_reconcile
from precis.utils.llm import router
from precis.utils.llm.router import LlmRequest, LlmResult, Tier


def _memory(store: Store, title: str, body: str) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title, meta={})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    return int(ref.id)


def _proposal(candidate: GardenCandidate) -> GardenProposal:
    return GardenProposal(
        review_id=1,
        target_kind=candidate.target_kind,
        target_id=candidate.target_id,
        action=candidate.action,
        key=candidate.key,
        detector=candidate.detector,
        title=candidate.title,
        payload=candidate.payload,
        evidence=candidate.evidence,
        rationale="Compare the records' own statements.",
        model="test",
        at=datetime.now(UTC),
        current=True,
    )


def test_make_memory_include_and_garden_loader_share_exact_policy() -> None:
    skills = resources.files("precis.data").joinpath("skills")
    source = skills.joinpath("precis-memory-help.md").read_text(encoding="utf-8")
    make_memory = skills.joinpath("precis-make-memory.md").read_text(encoding="utf-8")
    resolver = DocResolver(docs={"precis-memory-help": source})
    expected = resolver("precis-memory-help", "memory-reconciliation-policy")
    expanded = Includer(resolvers={"doc": resolver}).expand(make_memory)

    assert memory_reconcile.memory_reconciliation_policy() == expected
    assert expanded.count(expected) == 1
    assert "{{include" not in expanded


def test_policy_change_changes_memory_rejection_cache_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = memory_reconcile.memory_policy_version()
    policy = memory_reconcile.memory_reconciliation_policy()
    monkeypatch.setattr(
        memory_reconcile, "memory_reconciliation_policy", lambda: policy + "\nChanged."
    )
    assert memory_reconcile.memory_policy_version() != original


def test_missing_packaged_policy_section_refuses_instead_of_omitting_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "precis-memory-help.md").write_text(
        "# Memory\n\n## Other rules\nNothing to reconcile here.\n", encoding="utf-8"
    )
    memory_reconcile.memory_reconciliation_policy.cache_clear()
    monkeypatch.setattr(memory_reconcile.resources, "files", lambda _package: tmp_path)
    try:
        with pytest.raises(IncludeError, match="memory-reconciliation-policy"):
            memory_reconcile.memory_reconciliation_policy()
    finally:
        memory_reconcile.memory_reconciliation_policy.cache_clear()


@pytest.mark.parametrize("stage", ["proposer", "consumer", "review", "fable"])
@pytest.mark.parametrize("kind_field", ["evidence", "payload"])
def test_memory_duplicate_judges_receive_policy_and_stored_bodies(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    kind_field: str,
) -> None:
    winner_body = "Jane Doe's dog Juniper was red in the supplied observation."
    loser_body = "Bill Smith's dog Cedar was brown in the supplied observation."
    winner = _memory(store, "First pet observation", winner_body)
    loser = _memory(store, "Second pet observation", loser_body)
    evidence: dict[str, Any] = {"kind": "memory"} if kind_field == "evidence" else {}
    payload: dict[str, Any] = {"winner": winner, "loser": loser}
    if kind_field == "payload":
        payload["kind"] = "memory"
    candidate = GardenCandidate(
        detector="dup_pair",
        action="merge",
        target_kind="ref",
        target_id=loser,
        other_id=winner,
        evidence=evidence,
        payload=payload,
    )
    captured: list[LlmRequest] = []

    def capture(request: LlmRequest) -> LlmResult:
        captured.append(request)
        return LlmResult(
            text=json.dumps(
                {
                    "accept": False,
                    "rationale": "Different owners and dogs.",
                    "verdict": "reject",
                    "reason": "distinct_terms",
                    "why": "Different owners and dogs.",
                }
            ),
            cost_usd=None,
            turns_used=None,
            model="test",
            tier=Tier.MEDIUM,
        )

    monkeypatch.setattr(router, "route", capture)
    proposal = _proposal(candidate)
    result: proposer.Verdict | consumer.ConsumerVerdict | reviewer.ReviewerVerdict
    if stage == "proposer":
        result = proposer.propose_llm(store, candidate)
        expected_source = proposer.OP_SOURCE
    elif stage == "consumer":
        result = consumer.consume_llm(store, proposal)
        expected_source = consumer.OP_SOURCE_CONSUME
    elif stage == "review":
        result = reviewer.review_llm(store, proposal)
        expected_source = reviewer.OP_SOURCE_REVIEW
    else:
        result = reviewer.fable_llm(store, proposal)
        expected_source = reviewer.OP_SOURCE_REVIEW_FABLE

    assert result.error is None
    (request,) = captured
    assert request.source == expected_source
    assert request.prompt.count(memory_reconcile.memory_reconciliation_policy()) == 1
    assert winner_body in request.prompt
    assert loser_body in request.prompt


def test_unrelated_proposer_head_keeps_legacy_cache_contract() -> None:
    candidate = GardenCandidate(
        detector="dup_pair",
        action="merge",
        target_kind="ref",
        target_id=2,
        other_id=1,
        evidence={"kind": "taxon"},
        payload={"kind": "taxon"},
    )
    head = proposer._prompt_for(candidate).split("\nCandidate:\n")[0]
    assert hashlib.sha256(head.encode("utf-8")).hexdigest() == (
        "6fc3dcbf619dde09b8c4625b0b2512f6d10a145fb84c93cda46d25413241c1ee"
    )
    assert memory_reconcile.memory_reconciliation_policy() not in head
    proposal = _proposal(candidate)
    assert memory_reconcile.memory_reconciliation_policy() not in consumer._prompt_for(
        proposal, {}
    )
    assert memory_reconcile.memory_reconciliation_policy() not in reviewer._prompt_for(
        proposal
    )


@pytest.mark.parametrize(
    ("detector", "action"),
    [
        ("dangling_mention", "rewrite"),
        ("thread_shape", "retire"),
        ("anchor_rot", "rewrite"),
    ],
)
def test_memory_maintenance_judges_share_policy(detector: str, action: str) -> None:
    candidate = GardenCandidate(
        detector=detector, action=action, target_kind="ref", target_id=1
    )
    proposal = _proposal(candidate)
    policy = memory_reconcile.memory_reconciliation_policy()
    assert proposer._prompt_for(candidate).count(policy) == 1
    assert consumer._prompt_for(proposal, {}).count(policy) == 1
    assert reviewer._prompt_for(proposal).count(policy) == 1


@pytest.mark.parametrize("detector", proposer.MEMORY_ACTIONS)
def test_all_memory_actions_receive_policy_without_kind(detector: str) -> None:
    assert memory_reconcile.uses_memory_policy(detector, {}, {})
    candidate = GardenCandidate(
        detector=detector,
        action=proposer.MEMORY_ACTIONS[detector][0],
        target_kind="ref",
        target_id=1,
        evidence={},
        payload={"evidence_sha": "test"},
    )
    proposal = _proposal(candidate)
    policy = memory_reconcile.memory_reconciliation_policy()
    assert proposer._prompt_for(candidate).count(policy) == 1
    assert consumer._prompt_for(proposal, {}).count(policy) == 1
    assert reviewer._prompt_for(proposal).count(policy) == 1


def test_non_memory_detector_without_memory_kind_does_not_use_policy() -> None:
    assert not memory_reconcile.uses_memory_policy(
        "dup_pair", {"kind": "taxon"}, {"kind": "taxon"}
    )


def test_memory_policy_update_reopens_memory_duplicates_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_id = _memory(store, "Memory duplicate", "The exact repeated claim.")
    taxon_id = int(
        store.insert_ref(
            kind="taxon", slug=None, title="Category duplicate", meta={}
        ).id
    )
    candidates = [
        GardenCandidate(
            detector="dup_pair",
            action="merge",
            target_kind="ref",
            target_id=ref_id,
            evidence={"kind": kind},
            payload={"kind": kind},
        )
        for kind, ref_id in (("memory", memory_id), ("taxon", taxon_id))
    ]
    for candidate in candidates:
        record_rejection(
            store,
            candidate,
            rationale="Unchanged contents are insufficient.",
            prompt_version=runner.MEMORY_PROMPT_VERSION
            if candidate.target_id == memory_id
            else runner.PROMPT_VERSION,
        )
    monkeypatch.setattr(
        runner,
        "run_detectors",
        lambda *_a, **_kw: DetectorRun(candidates=candidates),
    )
    asked: list[int] = []

    def reject(_store: Store, candidate: GardenCandidate) -> Verdict:
        asked.append(candidate.target_id)
        return Verdict(accept=False, rationale="Different underlying claims.")

    initial = runner.run_garden_tick(store, propose=reject)
    assert initial.skipped_rejected == 2
    assert asked == []

    monkeypatch.setattr(runner, "MEMORY_PROMPT_VERSION", "memory-policy-test-change")
    changed = runner.run_garden_tick(store, propose=reject)
    assert (changed.skipped_rejected, changed.rejected) == (1, 1)
    assert asked == [memory_id]
    latest = store.reviews_for("ref", memory_id)[0]
    assert latest.note is not None
    assert json.loads(latest.note)["garden"]["prompt"] == "memory-policy-test-change"

    repeated = runner.run_garden_tick(store, propose=reject)
    assert repeated.skipped_rejected == 2
    assert asked == [memory_id]
