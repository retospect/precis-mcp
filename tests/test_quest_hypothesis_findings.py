"""A tick raises living findings through the independent-source gate."""

from __future__ import annotations

from types import SimpleNamespace

from precis.quest.tick import run_quest_tick
from precis.quest.weave_tick import QUEST_BODY_INQUIRY
from precis.store import Store
from tests.workers._helpers import seed_ref


def test_tick_mints_serving_finding_and_pointer(store: Store) -> None:
    quest_id = seed_ref(store, title="A striving", kind="quest")
    direction = seed_ref(store, title="Material stability", kind="todo")
    store.add_link(src_ref_id=direction, dst_ref_id=quest_id, relation="part-of")
    first = seed_ref(store, title="First computation", kind="pathway")
    second = seed_ref(store, title="Independent computation", kind="pathway")
    proposal = {
        "sentence": "Nanoindentation measures a modulus above 9 GPa in nanobud films.",
        "motivation": "Both calculations point to the same stiffening mechanism.",
        "testable_by": "Nanoindentation on a series of nanobud films.",
        "motivated_by": [f"pw{first}", f"pw{second}"],
        "direction": f"td{direction}",
    }
    rejected = {
        **proposal,
        "sentence": "Nanoindentation measures a modulus below 3 GPa in nanobud films.",
        "motivated_by": [f"pw{first}", f"pw{first}"],
    }

    def dispatch(_request: object) -> SimpleNamespace:
        return SimpleNamespace(
            data={"hypotheses": [proposal, rejected]},
            model="test-model",
            text="",
            error=None,
            cost_usd=0.01,
            paused=False,
        )

    for _ in range(2):
        result = run_quest_tick(
            store,
            quest_id,
            dispatch_fn=dispatch,
            compute=False,
            quest_body=QUEST_BODY_INQUIRY,
        )
        assert result.status == "succeeded"

    servers = store.links_for(quest_id, direction="in", relation="serves")
    findings = [edge.src_ref_id for edge in servers]
    assert len(findings) == 1
    fid = findings[0]
    ref = store.get_ref(kind="finding", id=fid)
    assert ref is not None and ref.title == proposal["sentence"]
    assert ref.meta and ref.meta["artifact_type"] == "hypothesis"
    assert ref.meta["proposed_payload"]["llm_models"] == ["test-model"]
    assert {
        edge.dst_ref_id
        for edge in store.links_for(fid, direction="out", relation="serves")
    } == {quest_id, direction}
    notes = [
        row
        for row in store.chunks.list_chunks_for_ref(quest_id)
        if row.meta and row.meta.get("entry_type") == "note"
    ]
    assert [row.text for row in notes].count(f"raised fi{fid}") == 1
    assert any(
        "hypothesis 2 skipped" in row.text and "distinct source paper" in row.text
        for row in notes
    )
