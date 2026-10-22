"""``get(kind='quest', id=Q, view=<eye rung>)`` — the fisheye ladder on a quest
(``docs/backlog/fisheye-everywhere.md`` AC 2). Mirrors
``tests/test_finding.py::TestFisheyeExtentLadder`` and
``tests/test_eye_render.py``'s quest ring test, through the handler door."""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput, Unsupported


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    return QuestHandler(hub=Hub(store=store))


def _quest(store: Any, title: str) -> Any:
    return store.insert_ref(kind="quest", slug=None, title=title)


class TestQuestFisheyeLadder:
    def test_fisheye_1hop_groups_serves_both_ways_under_roadmap(
        self, store: Any
    ) -> None:
        parent = _quest(store, "Grow the mesh")
        child = _quest(store, "Ship the ring")
        grandchild = _quest(store, "Pin the groups")
        store.add_link(src_ref_id=child.id, dst_ref_id=parent.id, relation="serves")
        store.add_link(src_ref_id=grandchild.id, dst_ref_id=child.id, relation="serves")

        out = _handler(store).get(id=child.id, view="fisheye+1hop").body

        assert "— linked (1 hop) —" in out
        assert "Roadmap:" in out
        assert f"  serves: qu{parent.id} — Grow the mesh" in out
        assert f"  served-by: qu{grandchild.id} — Pin the groups" in out

    def test_bare_fisheye_has_no_neighbourhood(self, store: Any) -> None:
        parent = _quest(store, "Grow the mesh")
        child = _quest(store, "Ship the ring")
        store.add_link(src_ref_id=child.id, dst_ref_id=parent.id, relation="serves")

        out = _handler(store).get(id=child.id, view="fisheye").body

        assert "Ship the ring" in out
        assert "— linked (1 hop) —" not in out

    def test_supporting_findings_and_serving_papers_are_grouped_and_capped(
        self, store: Any
    ) -> None:
        """Legacy shape: findings linked to a quest by ``supports`` and
        hundreds of papers/structures (``serves``). ``add_link`` now refuses
        a ``supports`` edge on a quest endpoint (findings join by ``serves``,
        see the next test), but legacy rows render until migrated — seeded
        by raw INSERT. Such findings land under ``Notes & links`` as
        ``supported-by``; papers and structures land under ``Roadmap`` as
        ``served-by``. Each (group, label) block caps at 8 with an explicit
        overflow line."""
        q = _quest(store, "Catalyse NO to NH3")
        for i in range(10):
            f = store.insert_ref(kind="finding", slug=None, title=f"Finding {i}")
            with store.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
                    "VALUES (%s, %s, 'supports', 'agent')",
                    (f.id, q.id),
                )
        for i in range(9):
            p = store.insert_ref(kind="paper", slug=f"serve{i}x", title=f"Paper {i}")
            store.add_link(src_ref_id=p.id, dst_ref_id=q.id, relation="serves")
        s = store.insert_ref(kind="structure", slug="fen4site", title="Fe-N4 site")
        store.add_link(src_ref_id=s.id, dst_ref_id=q.id, relation="serves")

        out = _handler(store).get(id=q.id, view="fisheye+1hop").body

        assert "Notes & links:" in out
        assert out.count("supported-by: fi") == 8
        assert "    … +2 more" in out
        # 9 papers + 1 structure share the one `served-by` block: 8 shown
        assert out.count("served-by: ") == 8
        assert "    … +2 more" in out.split("Notes & links:")[0]

    def test_serving_findings_list_before_serving_papers(self, store: Any) -> None:
        """Findings join a quest by ``serves`` (finding -> quest), so they
        share the ``Roadmap`` ``served-by`` block with the serving papers. A
        quest with hundreds of older papers would hide every finding behind
        the per-label cap; findings rank after sub-quests, before papers."""
        q = _quest(store, "Catalyse NO to NH3")
        for i in range(9):
            p = store.insert_ref(kind="paper", slug=f"older{i}x", title=f"Paper {i}")
            store.add_link(src_ref_id=p.id, dst_ref_id=q.id, relation="serves")
        findings = []
        for i in range(3):
            f = store.insert_ref(kind="finding", slug=None, title=f"Finding {i}")
            store.add_link(src_ref_id=f.id, dst_ref_id=q.id, relation="serves")
            findings.append(f)
        sub = _quest(store, "Sub-quest")
        store.add_link(src_ref_id=sub.id, dst_ref_id=q.id, relation="serves")

        out = _handler(store).get(id=q.id, view="fisheye+1hop").body
        served = [ln for ln in out.splitlines() if ln.startswith("  served-by: ")]

        assert len(served) == 8
        assert served[0].startswith(f"  served-by: qu{sub.id} ")
        assert {ln.split()[1] for ln in served[1:4]} == {f"fi{f.id}" for f in findings}
        assert all(ln.split()[1].startswith("pa") for ln in served[4:])
        assert "supported-by:" not in out

    def test_2hop_and_recall_suffix_are_accepted(self, store: Any) -> None:
        parent = _quest(store, "Grow the mesh")
        child = _quest(store, "Ship the ring")
        store.add_link(src_ref_id=child.id, dst_ref_id=parent.id, relation="serves")
        h = _handler(store)

        assert "Roadmap:" in h.get(id=child.id, view="fisheye+2hop").body
        # recall needs the embedder-backed similarity leg; the dispatch still
        # reaches the eye (no Unsupported), whatever the leg returns.
        h.get(id=child.id, view="fisheye+1hop+recall")

    def test_q_on_a_rung_below_2hop_is_bad_input(self, store: Any) -> None:
        child = _quest(store, "Ship the ring")
        with pytest.raises(BadInput):
            _handler(store).get(id=child.id, view="fisheye+1hop", q="paper:cites")

    def test_unknown_view_lists_fisheye_among_options(self, store: Any) -> None:
        q = _quest(store, "Grow the mesh")
        with pytest.raises(Unsupported) as exc:
            _handler(store).get(id=q.id, view="nonsense")
        options = exc.value.options or []
        assert "fisheye+1hop" in options
        assert "fisheye" in options
        assert "tree" in options
