"""Read-only memory garden views and detector statistics on the real ledger."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.cli.garden import _stats
from precis.dispatch import Hub
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import (
    emit_proposal,
    record_rejection,
    resolve_proposal,
)
from precis.graph.garden.reviewer import REVIEWER_ACTOR
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert, Tag

MEMORY_DETECTORS = (
    "dangling_mention",
    "anchor_rot",
    "thread_shape",
    "review_due",
    "title_shape",
    "negative_undated",
    "source_drift",
    "entity_claim_stale",
    "restated_claim",
)


def _memory(store: Store, title: str, section: str | None = None) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0, text=f"Body for {title}", meta={"chunk_kind": "memory_body"}
            )
        ],
    )
    if section is not None:
        store.add_tag(ref.id, Tag.open(f"section:{section}"), set_by="system")
    return int(ref.id)


def _proposal(
    store: Store,
    ref_id: int,
    detector: str = "anchor_rot",
    *,
    action: str = "rewrite",
    evidence: str = "missing scripts/old-command",
) -> int:
    review_id = emit_proposal(
        store,
        GardenCandidate(
            detector=detector,
            action=action,
            target_kind="ref",
            target_id=ref_id,
            title=f"Repair {detector}",
            evidence={"observed": evidence},
        ),
        rationale="Fixture evidence supports this repair",
    )
    assert review_id is not None
    return review_id


def _age(store: Store, review_id: int, days: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE reviews SET at = %s WHERE review_id = %s",
            (datetime.now(UTC) - timedelta(days=days), review_id),
        )


def test_smells_lists_open_and_decided_with_evidence_without_writes(hub: Hub) -> None:
    store = hub.live_store
    target = _memory(store, "Repairable memory", "reference")
    open_id = _proposal(store, target, evidence="missing scripts/first-command")
    decided_id = _proposal(
        store,
        target,
        "dangling_mention",
        evidence="retired mention me999999",
    )
    decision = resolve_proposal(store, decided_id, actor="fixture", verdict="approved")
    assert decision is not None
    before = store.reviews_for("ref", target)
    before_ref = store.fetch_refs_by_ids([target])[target]
    before_tags = store.tags_for(target)

    response = MemoryHandler(hub=hub).get(id=target, view="smells")

    assert response.result_count == 2
    for value in (
        "anchor_rot",
        "dangling_mention",
        "rewrite",
        "proposed",
        "approved",
        "missing scripts/first-command",
        "retired mention me999999",
        str(open_id),
        str(decided_id),
        decision.at.astimezone(UTC).strftime("%Y-%m-%dT"),
    ):
        assert value in response.body
    assert "Z" in response.body
    assert store.reviews_for("ref", target) == before
    assert store.fetch_refs_by_ids([target])[target] == before_ref
    assert store.tags_for(target) == before_tags


def test_smells_empty_node_names_a_next_step(hub: Hub) -> None:
    target = _memory(hub.live_store, "No garden proposals")
    response = MemoryHandler(hub=hub).get(id=target, view="smells")
    assert response.result_count == 0
    assert "no" in response.body.lower() and "proposal" in response.body.lower()
    assert "next:" in response.body.lower()
    assert str(target) in response.body


def test_smells_batch_renders_each_memory(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.live_store
    first = _memory(store, "First batched memory")
    second = _memory(store, "Second batched memory")
    _proposal(store, first, "anchor_rot", evidence="first-batch-evidence")
    _proposal(store, second, "dangling_mention", evidence="second-batch-evidence")

    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": [first, second], "view": "smells"}
    )

    assert not is_error, body
    for value in (
        f"me{first}",
        f"me{second}",
        "anchor_rot",
        "dangling_mention",
        "first-batch-evidence",
        "second-batch-evidence",
    ):
        assert value in body
    assert body.index("first-batch-evidence") < body.index("second-batch-evidence")


def test_smells_keeps_old_open_and_recent_decisions_only(hub: Hub) -> None:
    store = hub.live_store
    target = _memory(store, "Proposal history")
    old_open = _proposal(store, target, evidence="old-open-evidence")
    old_decided = _proposal(
        store, target, "thread_shape", action="retire", evidence="old-decision-evidence"
    )
    decision = resolve_proposal(store, old_decided, actor="fixture", verdict="rejected")
    assert decision is not None
    _age(store, old_open, 60)
    _age(store, old_decided, 60)
    _age(store, decision.review_id, 45)

    response = MemoryHandler(hub=hub).get(id=target, view="smells")

    assert response.result_count == 1
    assert "old-open-evidence" in response.body
    assert "old-decision-evidence" not in response.body


def test_smells_pairs_decision_with_its_proposal_after_key_reopens(hub: Hub) -> None:
    store = hub.live_store
    target = _memory(store, "Reopened repair")
    first = _proposal(store, target, evidence="first-round-evidence")
    assert (
        resolve_proposal(store, first, actor="fixture", verdict="approved") is not None
    )
    second = _proposal(store, target, evidence="second-round-evidence")
    assert first != second

    response = MemoryHandler(hub=hub).get(id=target, view="smells")

    assert response.result_count == 2
    assert "first-round-evidence" in response.body
    assert "second-round-evidence" in response.body
    assert "approved" in response.body and "proposed" in response.body


def test_smells_ignores_proposer_rejections_and_unrelated_reviews(hub: Hub) -> None:
    store = hub.live_store
    target = _memory(store, "No accepted proposal")
    record_rejection(
        store,
        GardenCandidate(
            detector="anchor_rot", action="rewrite", target_kind="ref", target_id=target
        ),
        rationale="No broken anchor",
        prompt_version="fixture",
    )
    for note in (
        "not JSON",
        '{"garden": invalid}',
        '["garden"]',
        '{"garden": []}',
        '{"garden": null}',
        json.dumps({"garden_review": {"proposal": 123}}),
    ):
        store.record_target_review(
            "ref", target, actor="fixture", verdict="approved", note=note
        )

    response = MemoryHandler(hub=hub).get(id=target, view="smells")

    assert response.result_count == 0
    assert "no" in response.body.lower() and "proposal" in response.body.lower()


def test_smells_keeps_proposals_from_an_unknown_detector(hub: Hub) -> None:
    target = _memory(hub.live_store, "Future detector")
    _proposal(hub.live_store, target, "future_detector", evidence="future-evidence")

    response = MemoryHandler(hub=hub).get(id=target, view="smells")

    assert response.result_count == 1
    assert "future_detector" in response.body
    assert "future-evidence" in response.body


def test_hub_rollup_groups_by_detector_and_type_through_args(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers import _memory_smells

    store = runtime_with_store.live_store
    root = _memory(store, "Memory hub", "index")
    child = _memory(store, "Reference child", "reference")
    nested = _memory(store, "Nested gotcha", "gotchas")
    outside = _memory(store, "Outside this hub", "workflow")
    store.add_link(src_ref_id=child, dst_ref_id=root, relation="part-of")
    store.add_link(src_ref_id=nested, dst_ref_id=child, relation="part-of")
    _proposal(store, child)
    decided = _proposal(store, child, "dangling_mention")
    assert (
        resolve_proposal(store, decided, actor="fixture", verdict="approved")
        is not None
    )
    _proposal(store, nested)
    _proposal(store, outside, "title_shape", action="qualify")
    rendered: list[dict[str, Any]] = []
    render = _memory_smells.render_agent_table

    def capture(rows: Any, **kwargs: Any) -> str:
        rendered.extend(rows)
        return render(rows, **kwargs)

    monkeypatch.setattr(_memory_smells, "render_agent_table", capture)
    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "view": "smells", "under": f"me{root}"}
    )

    assert not is_error, body
    counts = {
        (row["detector"], row["type"]): (row["pending"], row["decided"], row["nodes"])
        for row in rendered
    }
    assert counts == {
        ("anchor_rot", "section:reference"): (1, 0, 1),
        ("anchor_rot", "section:gotchas"): (1, 0, 1),
        ("dangling_mention", "section:reference"): (0, 1, 1),
    }
    assert "title_shape" not in body


def test_stats_prints_zero_for_every_memory_detector(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    _stats(argparse.Namespace(), store)
    lines = capsys.readouterr().out.splitlines()
    for detector in MEMORY_DETECTORS:
        row = next(line for line in lines if line.startswith(f"{detector}:"))
        assert "pending=0" in row
        assert "sampled=0" in row
        assert "approval=—" in row


def test_rollup_respects_tag_exclusions_retirement_and_empty_scope(hub: Hub) -> None:
    store = hub.live_store
    handler = MemoryHandler(hub=hub)
    included = _memory(store, "Included reference", "reference")
    excluded = _memory(store, "Excluded reference", "reference")
    other_type = _memory(store, "Other type", "gotchas")
    retired = _memory(store, "Retired reference", "reference")
    other_space = _memory(store, "Research reference", "reference")
    for target in (included, excluded, other_type, retired):
        store.add_tag(target, Tag.closed("SPACE", "repo-dev"), set_by="system")
        _proposal(store, target)
    store.add_tag(other_space, Tag.closed("SPACE", "research"), set_by="system")
    _proposal(store, other_space)
    store.retire_ref(retired)

    response = handler.search(
        view="smells",
        tags=["section:reference", "SPACE:repo-dev"],
        exclude_ref_ids=[excluded],
    )

    assert response.result_count == 1
    assert "section:reference" in response.body
    assert "section:gotchas" not in response.body
    empty = handler.search(view="smells", include_ref_ids=[])
    assert empty.result_count == 0
    assert "no" in empty.body.lower() and "proposal" in empty.body.lower()
    assert "next:" in empty.body.lower()


def test_stats_counts_pending_and_opus_agreement_without_counting_decisions(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    pending = _proposal(store, _memory(store, "Pending repair"))
    target = _memory(store, "Decided repair")
    decided = _proposal(store, target)
    assert (
        resolve_proposal(store, decided, actor="fixture", verdict="approved")
        is not None
    )
    record_rejection(
        store,
        GardenCandidate(
            detector="anchor_rot",
            action="rewrite",
            target_kind="ref",
            target_id=_memory(store, "Proposer rejected repair"),
        ),
        rationale="No proposal warranted",
        prompt_version="fixture",
    )
    for proposal, verdict in ((pending, "approve"), (decided, "reject")):
        store.record_target_review(
            "ref",
            target,
            actor=REVIEWER_ACTOR,
            verdict="approved" if verdict == "approve" else "rejected",
            note=json.dumps(
                {
                    "garden_review": {
                        "proposal": proposal,
                        "detector": "anchor_rot",
                        "verdict": verdict,
                    }
                }
            ),
        )

    _stats(argparse.Namespace(), store)

    row = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("anchor_rot:")
    )
    assert "pending=1" in row
    assert "sampled=2" in row
    assert "approval=50%" in row
