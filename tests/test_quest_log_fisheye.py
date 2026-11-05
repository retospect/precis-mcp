"""Candidate eyes expose linked log entries without inferring their sections."""

from __future__ import annotations

from typing import Any

import pytest

from precis.store.types import ChunkInsert
from precis.utils.eye_render import render_eye


def _entry(
    store: Any,
    quest_id: int,
    structure_id: int,
    ord_: int,
    text: str,
    meta: dict[str, Any],
) -> int:
    chunks = store.chunks.insert_chunks(
        quest_id,
        [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": "quest_log", **meta})],
    )
    store.add_link(
        src_ref_id=quest_id,
        src_pos=ord_,
        dst_ref_id=structure_id,
        relation="cites",
    )
    return int(chunks[0].id)


@pytest.mark.parametrize("hops", [1, 2])
def test_log_entries_group_by_stored_section_and_type(store: Any, hops: int) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="Candidate search")
    structure = store.insert_ref(kind="structure", slug="candidate", title="Candidate")
    entries: dict[int, int] = {}
    for ord_, section in enumerate(("why", "plan", "results", "analysis", "lineage")):
        entries[ord_] = _entry(
            store,
            quest.id,
            structure.id,
            ord_,
            f"Stored {section} reasoning",
            {"section": section, "entry_type": "note"},
        )
    _entry(
        store, quest.id, structure.id, 5, "A measured outcome", {"entry_type": "result"}
    )
    entries[6] = _entry(
        store, quest.id, structure.id, 6, "why plan analysis are prose only", {}
    )
    entries[7] = _entry(
        store, quest.id, structure.id, 7, "Another reason", {"section": "why"}
    )

    out = render_eye(store, f"st{structure.id}", f"fisheye+{hops}hop")

    assert "Notes & links:" in out
    for section in ("plan", "results", "analysis", "lineage", "result"):
        assert f"  quest log — {section}:" in out
    assert "  quest log — why (2):" in out
    assert f"ql{entries[0]} ~0 — Stored why reasoning" in out
    assert f"ql{entries[7]} ~7 — Another reason" in out
    assert f"  quest log: ql{entries[6]} ~6 — why plan analysis are prose only" in out
    assert "quest log — note" not in out
    assert "  cited-by:" not in out


def test_quest_log_group_uses_existing_cap_and_expansion(store: Any) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="Candidate search")
    structure = store.insert_ref(kind="structure", slug="candidate", title="Candidate")
    entries: list[int] = []
    for ord_ in range(10):
        entries.append(
            _entry(
                store,
                quest.id,
                structure.id,
                ord_,
                f"Reason {ord_}",
                {"section": "why"},
            )
        )

    out = render_eye(store, f"st{structure.id}", "fisheye+1hop")
    assert "  quest log — why (10):" in out
    assert "… +2 more" in out
    assert "q='quest log — why'" in out
    assert f"ql{entries[9]}" not in out

    expanded = render_eye(
        store, f"st{structure.id}", "fisheye+1hop", q="quest log — why"
    )
    assert f"ql{entries[9]} ~9 — Reason 9" in expanded


def test_structure_without_linked_log_entries_renders_as_before(store: Any) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="Candidate search")
    structure = store.insert_ref(kind="structure", slug="candidate", title="Candidate")
    store.add_link(src_ref_id=structure.id, dst_ref_id=quest.id, relation="serves")
    before = render_eye(store, f"st{structure.id}", "fisheye+1hop")

    store.chunks.insert_chunks(
        quest.id,
        [
            ChunkInsert(
                ord=0,
                text="Unlinked reasoning",
                meta={"chunk_kind": "quest_log", "section": "why"},
            )
        ],
    )

    after = render_eye(store, f"st{structure.id}", "fisheye+1hop")
    assert after == before
    assert f"serves: qu{quest.id} — Candidate search" in after
    assert "quest log" not in after


def test_log_group_respects_ring_kind_filter(store: Any) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="Candidate search")
    structure = store.insert_ref(kind="structure", slug="candidate", title="Candidate")
    _entry(store, quest.id, structure.id, 0, "A reason", {"section": "why"})

    out = render_eye(
        store, f"st{structure.id}", "fisheye+1hop", ring={"kinds": "paper"}
    )

    assert "quest log" not in out
    assert "A reason" not in out


def test_other_target_kinds_keep_ordinary_citation_rows(store: Any) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="Candidate search")
    paper = store.insert_ref(kind="paper", slug="candidate-evidence", title="Evidence")
    _entry(store, quest.id, paper.id, 0, "A reason", {"section": "why"})

    out = render_eye(store, f"pa{paper.id}", "fisheye+1hop")

    assert "quest log" not in out
    assert f"cited-by: qu{quest.id} — Candidate search" in out
