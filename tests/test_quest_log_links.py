"""Forward quest log links and candidate mint provenance (gr553318).

Real store tests exercise chunk endpoints, admission and transaction recovery.
No compute jobs or model calls are needed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest.logbook import (
    StructureLinkReceipt,
    append_entry,
    link_entry_structures,
)
from precis.quest.tick import _TickRun
from precis.store.revision_context import revision_context

_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}


def _quest(store: Any) -> Any:
    return store.insert_ref(kind="quest", slug=None, title="Find a catalyst")


def _structure(store: Any, slug: str = "log-candidate") -> Any:
    return store.insert_ref(kind="structure", slug=slug, title=slug)


def _logs(store: Any, qid: int) -> list[Any]:
    return [
        chunk
        for chunk in store.chunks.list_chunks_for_ref(qid)
        if chunk.chunk_kind == "quest_log"
    ]


def _cites(store: Any, qid: int) -> list[Any]:
    return store.links_for(qid, direction="out", relation="cites")


def test_append_links_from_the_log_chunk_not_the_quest(store: Any) -> None:
    quest = _quest(store)
    candidate = _structure(store)

    number = append_entry(
        store,
        quest.id,
        text=f"Consider [st{candidate.id}] for its stable site.",
        entry_type="hypothesis",
        by="agent",
    )

    chunk = _logs(store, quest.id)[0]
    links = _cites(store, quest.id)
    assert len(links) == 1
    assert links[0].src_ref_id == quest.id
    assert links[0].src_chunk_id == chunk.id
    assert links[0].src_ord == number - 1 == chunk.ord
    assert links[0].dst_ref_id == candidate.id
    assert links[0].dst_chunk_id is None


@pytest.mark.parametrize(
    ("trust_run", "leading_zeros"),
    [(None, ""), ("test-run", "000")],
    ids=["entry-run-fallback", "named-run-exact-quote"],
)
def test_generated_append_supplies_log_evidence_to_edge_gate(
    store: Any, trust_run: str | None, leading_zeros: str
) -> None:
    quest = _quest(store)
    candidate = _structure(store)
    mention = f"st{leading_zeros}{candidate.id}"
    receipt = StructureLinkReceipt()

    with revision_context(
        trust_origin="generated", model="test-model", trust_run=trust_run
    ):
        append_entry(
            store,
            quest.id,
            text=f"Consider [{mention}] for its stable site.",
            entry_type="hypothesis",
            by="agent",
            link_receipt=receipt,
        )

    chunk = _logs(store, quest.id)[0]
    links = _cites(store, quest.id)
    assert len(links) == 1
    link = links[0]
    assert link.src_chunk_id == chunk.id
    assert link.dst_ref_id == candidate.id
    assert link.meta["evidence"] == [f"ql{chunk.id}"]
    assert link.meta["quote"] == mention
    assert link.meta["quote"] in chunk.text
    assert link.meta["model"] == "test-model"
    assert link.meta["prompt_version"] == "quest-log-structures-v1"
    assert link.meta["run_id"] == (trust_run or f"ql{chunk.id}")
    assert link.meta["why"].strip()
    assert (receipt.linked, receipt.skipped, receipt.failed) == (1, 0, 0)


def test_superseded_structure_and_successor_share_one_log_edge(store: Any) -> None:
    quest = _quest(store)
    retired = _structure(store, "retired-log-candidate")
    successor = _structure(store, "successor-log-candidate")
    store.stamp_ref_meta(retired.id, {"superseded_by": successor.id})
    store.retire_ref(retired.id)
    text = f"Revisit [st{retired.id}] as st{successor.id}."
    receipt = StructureLinkReceipt()

    with revision_context(trust_origin="generated", model="test-model"):
        number = append_entry(
            store,
            quest.id,
            text=text,
            entry_type="note",
            by="agent",
            link_receipt=receipt,
        )
        original_ids = [link.id for link in _cites(store, quest.id)]
        retry = link_entry_structures(store, quest.id, number - 1, text)

    links = _cites(store, quest.id)
    assert len(links) == 1
    assert [link.id for link in links] == original_ids
    assert links[0].dst_ref_id == successor.id
    assert links[0].src_chunk_id == _logs(store, quest.id)[0].id
    assert links[0].meta["quote"] == f"st{retired.id}"
    assert (receipt.linked, receipt.skipped, receipt.failed) == (1, 0, 0)
    assert (retry.linked, retry.skipped, retry.failed) == (1, 0, 0)
    assert len(_logs(store, quest.id)) == 1


def test_superseded_handle_resolving_to_another_kind_is_skipped(store: Any) -> None:
    quest = _quest(store)
    retired = _structure(store)
    converted = store.insert_ref(
        kind="todo",
        slug=None,
        title="Converted candidate",
        meta={"converted_from_kind": "structure"},
    )
    store.stamp_ref_meta(retired.id, {"superseded_by": converted.id})
    store.retire_ref(retired.id)
    resolved = store.resolve_handle(f"st{retired.id}")
    assert resolved is not None
    assert resolved.ref_id == converted.id
    assert resolved.kind == "todo"
    receipt = StructureLinkReceipt()
    text = f"Revisit st{retired.id}."

    append_entry(
        store,
        quest.id,
        text=text,
        entry_type="note",
        by="agent",
        link_receipt=receipt,
    )

    assert _logs(store, quest.id)[0].text == text
    assert _cites(store, quest.id) == []
    assert (receipt.linked, receipt.skipped, receipt.failed) == (0, 1, 0)


def test_repeated_mentions_and_link_retry_do_not_duplicate_edges(store: Any) -> None:
    quest = _quest(store)
    candidate = _structure(store)
    text = f"st{candidate.id} then [st{candidate.id}] again."
    number = append_entry(store, quest.id, text=text, entry_type="note", by="human")
    original_ids = [link.id for link in _cites(store, quest.id)]

    link_entry_structures(store, quest.id, number - 1, text)

    assert len(original_ids) == 1
    assert [link.id for link in _cites(store, quest.id)] == original_ids
    assert len(_logs(store, quest.id)) == 1


def test_two_named_structures_get_two_chunk_edges(store: Any) -> None:
    quest = _quest(store)
    first = _structure(store, "first-candidate")
    second = _structure(store, "second-candidate")
    receipt = StructureLinkReceipt()

    append_entry(
        store,
        quest.id,
        text=f"Compare st{first.id} and [st{second.id}].",
        entry_type="observation",
        by="agent",
        link_receipt=receipt,
    )

    links = _cites(store, quest.id)
    assert len(links) == 2
    assert {link.dst_ref_id for link in links} == {first.id, second.id}
    assert {link.src_chunk_id for link in links} == {_logs(store, quest.id)[0].id}
    assert (receipt.linked, receipt.skipped, receipt.failed) == (2, 0, 0)


def test_unresolved_handles_are_counted_in_handler_receipt(store: Any) -> None:
    quest = _quest(store)
    retired = _structure(store)
    store.retire_ref(retired.id)
    # The quest's id exists but cannot resolve as a structure.
    text = f"st0, st{retired.id}, st{quest.id}; st0 again."

    response = QuestHandler(hub=Hub(store=store)).put(id=quest.id, text=text)

    assert _logs(store, quest.id)[0].text == text
    assert _cites(store, quest.id) == []
    assert "0 linked, 3 skipped (unresolved), 0 failed" in response.body
    assert f"qu{quest.id}~0" in response.body


def test_oversized_handles_are_skipped_and_leading_zeros_deduplicate(
    store: Any,
) -> None:
    quest = _quest(store)
    candidate = _structure(store)
    huge_digits = "9" * 4301
    text = f"st{huge_digits}, st{2**63}, st{candidate.id} and [st000{candidate.id}]."

    response = QuestHandler(hub=Hub(store=store)).put(id=quest.id, text=text)

    entries = _logs(store, quest.id)
    assert len(entries) == 1
    assert entries[0].text == text
    links = _cites(store, quest.id)
    assert len(links) == 1
    assert links[0].dst_ref_id == candidate.id
    assert links[0].src_chunk_id == entries[0].id
    assert "1 linked, 2 skipped (unresolved), 0 failed" in response.body


def test_sql_link_failure_preserves_append_and_other_links_in_atomic_scope(
    store: Any, monkeypatch: Any
) -> None:
    quest = _quest(store)
    failing = _structure(store, "failing-candidate")
    healthy = _structure(store, "healthy-candidate")
    original_add_link = store.add_link

    def fail_one_link(**kwargs: Any) -> Any:
        if kwargs["dst_ref_id"] == failing.id:
            kwargs["conn"].execute("SELECT 1 / 0")
        return original_add_link(**kwargs)

    monkeypatch.setattr(store, "add_link", fail_one_link)
    with store.atomic():
        response = QuestHandler(hub=Hub(store=store)).put(
            id=quest.id,
            text=f"Compare st{failing.id} with st{healthy.id}.",
        )

    assert len(_logs(store, quest.id)) == 1
    assert [link.dst_ref_id for link in _cites(store, quest.id)] == [healthy.id]
    assert "1 linked, 0 skipped (unresolved), 1 failed" in response.body


def test_new_candidate_names_its_proposal_once_across_retry(store: Any) -> None:
    quest = _quest(store)
    rationale = "The exposed iron site may bind the intermediate."
    proposal_number = append_entry(
        store,
        quest.id,
        text=f"candidate: Fe site — {rationale}",
        entry_type="hypothesis",
        by="agent",
    )
    proposal = {
        "name": "Fe site",
        "structure": _SPEC,
        "rationale": rationale,
        "_proposal_entry": proposal_number,
    }

    sid, was_duplicate, _note = compute_mod._ensure_candidate_detail(
        store, quest.id, proposal
    )
    again, repeated, _note = compute_mod._ensure_candidate_detail(
        store, quest.id, proposal
    )

    assert sid is not None and not was_duplicate
    assert again == sid and repeated
    minted = [
        chunk
        for chunk in _logs(store, quest.id)
        if chunk.meta.get("candidate_id") == sid
        and chunk.meta.get("section") == "lineage"
    ]
    assert len(minted) == 1
    entry = minted[0]
    proposal_handle = f"qu{quest.id}~{proposal_number - 1}"
    assert entry.meta["proposal_entry"] == proposal_handle
    assert f"st{sid}" in entry.text
    assert proposal_handle in entry.text
    assert rationale in entry.text
    assert any(
        link.src_chunk_id == entry.id and link.dst_ref_id == sid
        for link in _cites(store, quest.id)
    )


def test_direct_mint_records_a_proposal_when_no_tick_entry_exists(store: Any) -> None:
    quest = _quest(store)
    rationale = "A low coordination site is a useful starting point."

    sid, _duplicate, _note = compute_mod._ensure_candidate_detail(
        store,
        quest.id,
        {"name": "Fe site", "structure": _SPEC, "rationale": rationale},
    )

    assert sid is not None
    entries = _logs(store, quest.id)
    minted = [entry for entry in entries if entry.meta.get("candidate_id") == sid]
    assert len(minted) == 1
    proposal_handle = minted[0].meta["proposal_entry"]
    proposal = next(
        entry for entry in entries if f"qu{quest.id}~{entry.ord}" == proposal_handle
    )
    assert proposal.meta["entry_type"] == "hypothesis"
    assert rationale in proposal.text
    assert rationale in minted[0].text


def test_proposal_entry_handoff_survives_tick_checkpoint(
    store: Any, monkeypatch: Any
) -> None:
    quest = _quest(store)
    payload = {
        "proposals": [
            {"structure": _SPEC},
            {"name": "Fe site", "structure": _SPEC, "rationale": "Test the site."},
        ]
    }
    run = _TickRun(
        store=store,
        quest_id=quest.id,
        tier=None,
        dispatch_fn=None,
        by="agent",
        compute=True,
        review=False,
        search_fn=None,
        job_ref_id=None,
        embedder=None,
        st={},
        stash={"payload": payload},
    )
    run._stage_apply()
    proposal_chunk = next(
        entry for entry in _logs(store, quest.id) if entry.text.startswith("candidate:")
    )
    run.st = json.loads(json.dumps(run.st))
    run.stash = json.loads(json.dumps(run.stash))
    seen: list[dict[str, Any]] = []

    def record_proposals(
        _store: Any, _quest_id: int, proposals: list[dict[str, Any]], **kwargs: Any
    ) -> Any:
        seen.extend(proposals)
        return compute_mod.ComputeStep(
            candidates_created=1,
            sims_dispatched=1,
            results_harvested=0,
            ruled_out=0,
            notes=[],
        )

    monkeypatch.setenv("PRECIS_QUEST_MAX_PROPOSALS", "3")
    monkeypatch.setattr(compute_mod, "run_compute_step", record_proposals)
    monkeypatch.setattr(compute_mod, "dispatch_search", lambda *a, **kw: None)
    monkeypatch.setattr(compute_mod, "apply_reruns", lambda *a, **kw: 0)

    run._stage_compute()

    named = next(proposal for proposal in seen if proposal.get("name") == "Fe site")
    assert named["_proposal_entry"] == proposal_chunk.ord + 1
    stored_proposals: Any = payload["proposals"]
    assert "_proposal_entry" not in stored_proposals[1]


def _apply_run(
    store: Any, quest_id: int, payload: dict[str, Any], *, review: bool = False
) -> _TickRun:
    return _TickRun(
        store=store,
        quest_id=quest_id,
        tier=None,
        dispatch_fn=None,
        by="agent",
        compute=False,
        review=review,
        search_fn=None,
        job_ref_id=None,
        embedder=None,
        st={"is_review": review},
        stash={"payload": payload},
    )


def test_tick_outcome_reports_skipped_structure_after_checkpoint(store: Any) -> None:
    """td565602: a landed entry's skipped handle reaches the tick receipt."""
    quest = _quest(store)
    text = "Consider st0 as a possible candidate."
    run = _apply_run(store, quest.id, {"logbook": [{"text": text}]})

    run._stage_apply()
    assert [entry.text for entry in _logs(store, quest.id)] == [text]
    run.st = json.loads(json.dumps(run.st))
    run.stash = json.loads(json.dumps(run.stash))
    outcome = run._stage_finish()

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.logbook_added == 1
    assert "structure links: 0 linked, 1 skipped (unresolved), 0 failed" in outcome.note
    assert _cites(store, quest.id) == []


def test_tick_totals_include_logbook_proposals_directions_and_failed_links(
    store: Any, monkeypatch: Any
) -> None:
    quest = _quest(store)
    healthy = _structure(store, "tick-healthy")
    failing = _structure(store, "tick-failing")
    original_add_link = store.add_link

    def fail_one_link(**kwargs: Any) -> Any:
        if kwargs["dst_ref_id"] == failing.id:
            kwargs["conn"].execute("SELECT 1 / 0")
        return original_add_link(**kwargs)

    monkeypatch.setattr(store, "add_link", fail_one_link)
    run = _apply_run(
        store,
        quest.id,
        {
            "logbook": [
                {"text": f"Compare st{healthy.id} with st0."},
                {"text": f"Investigate st{failing.id}."},
            ],
            "proposals": [
                {"name": "New site", "rationale": f"Build on st{healthy.id}."}
            ],
            "directions": ["Investigate st0 next."],
        },
        review=True,
    )

    run._stage_apply()
    assert len(_logs(store, quest.id)) == 4
    run.st = json.loads(json.dumps(run.st))
    outcome = run._stage_finish()

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.logbook_added == 4
    assert "structure links: 2 linked, 2 skipped (unresolved), 1 failed" in outcome.note
    links = _cites(store, quest.id)
    assert len(links) == 2
    assert {link.dst_ref_id for link in links} == {healthy.id}
    assert len({link.src_chunk_id for link in links}) == 2
