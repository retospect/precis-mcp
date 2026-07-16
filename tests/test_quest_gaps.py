"""Tests for quest gaps + health — slice 3 of the quest layer.

Covers the read-time, mechanical primitives in :mod:`precis.quest.gaps` (the
exploration queue + momentum + the alignment floor) and their surfacing in the
handler's ``view='tree'`` rollup, the per-quest ``view='gaps'``, and the
corpus-wide ``id='/gaps'`` dashboard. Runs against real PG (the ``store``
fixture) so the ``serves`` walk + tag/ref_events SQL is exercised end to end.
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.handlers.todo import TodoHandler
from precis.quest.gaps import quest_alignment, quest_gaps, quest_momentum


def _handler(store: Any) -> QuestHandler:
    return QuestHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


def _gap_kinds(store: Any, qid: int) -> list[str]:
    return [g.kind for g in quest_gaps(store, qid)]


# ── gaps ──────────────────────────────────────────────────────────────


class TestGaps:
    def test_thin_support_when_no_servers(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A lonely striving nothing serves"))
        assert "thin-support" in _gap_kinds(store, qid)

    def test_no_literature_when_servers_but_no_paper(self, store: Any) -> None:
        from tests.conftest import id_of

        th = TodoHandler(hub=Hub(store=store))
        h = _handler(store)
        qid = _created_id(h.put(text="Work under way, no papers yet"))
        for i in range(2):
            t = id_of(th.put(text=f"work item {i}").body)
            store.add_link(src_ref_id=t, dst_ref_id=qid, relation="serves")
        kinds = _gap_kinds(store, qid)
        assert "no-literature" in kinds
        assert "thin-support" not in kinds  # 2 servers clears the thin flag

    def test_paper_server_clears_no_literature(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        h = _handler(store)
        qid = _created_id(h.put(text="A grounded striving"))
        for title in ("paper A", "paper B"):
            p = seed_ref(store, title=title)
            store.add_link(src_ref_id=p, dst_ref_id=qid, relation="serves")
        assert "no-literature" not in _gap_kinds(store, qid)

    def test_low_mastery_served_concept(self, store: Any) -> None:
        from precis.handlers.concept import ConceptHandler

        ch = ConceptHandler(hub=Hub(store=store))

        def _cid(resp: Any) -> int:
            m = re.search(r"\bcn(\d+)\b", resp.body)
            assert m is not None
            return int(m.group(1))

        h = _handler(store)
        qid = _created_id(h.put(text="Needs a hard idea understood"))
        c = _cid(ch.put(text="proton-coupled electron transfer — a hard concept"))
        store.add_link(src_ref_id=c, dst_ref_id=qid, relation="serves")
        low = [g for g in quest_gaps(store, qid) if g.kind == "low-mastery"]
        assert low, "a freshly-minted (mastery 0.0) served concept is a gap"
        assert low[0].handle == f"cn{c}"

    def test_open_hypothesis_then_answered(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A tested striving"))
        h.put(id=qid, text="maybe Fe–N₄ sites work", entry="hypothesis")
        assert "open-hypothesis" in _gap_kinds(store, qid)
        # a later result / dead-end closes it
        h.put(id=qid, text="barrier too high — no", entry="dead-end")
        assert "open-hypothesis" not in _gap_kinds(store, qid)


# ── momentum ──────────────────────────────────────────────────────────


class TestMomentum:
    def test_quiet_when_empty(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="Brand new striving"))
        assert quest_momentum(store, qid).label == "quiet"

    def test_active_after_recent_logbook(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A busy striving"))
        for i in range(3):
            h.put(id=qid, text=f"observation {i}", entry="observation")
        m = quest_momentum(store, qid)
        assert m.recent_entries == 3
        assert m.label == "active"

    def test_open_and_blocked_todo_servers(self, store: Any) -> None:
        from precis.store import Tag
        from tests.conftest import id_of

        th = TodoHandler(hub=Hub(store=store))
        h = _handler(store)
        qid = _created_id(h.put(text="A striving with work in flight"))
        t_open = id_of(th.put(text="open work").body)
        t_blocked = id_of(th.put(text="blocked work").body)
        for t in (t_open, t_blocked):
            store.add_link(src_ref_id=t, dst_ref_id=qid, relation="serves")
        # bubble a child-failure onto the blocked todo (the same open tag the
        # job failure-bubble writes).
        store.add_tag(t_blocked, Tag.open("child-failed:999"), set_by="system")
        m = quest_momentum(store, qid)
        assert m.open_todo_servers == 2  # neither is done
        assert m.blocked_todo_servers == 1


# ── alignment floor ───────────────────────────────────────────────────


class TestAlignment:
    def test_cosine_pure(self) -> None:
        from precis.quest.gaps import _cosine

        assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
        assert abs(_cosine([1.0, 1.0], [1.0, 0.0]) - (1 / 2**0.5)) < 1e-9
        assert _cosine([], [1.0]) == 0.0  # degenerate

    def test_floor_is_noop_without_embeddings(self, store: Any) -> None:
        # No embedder runs in the test env → cards carry no vector → the
        # alignment floor checks nothing and flags nothing (best-effort).
        from tests.workers._helpers import seed_ref

        h = _handler(store)
        qid = _created_id(h.put(text="A striving"))
        p = seed_ref(store, title="a server with no embedding")
        store.add_link(src_ref_id=p, dst_ref_id=qid, relation="serves")
        flags, checked = quest_alignment(store, qid)
        assert checked == 0 and flags == []


# ── surfacing in the handler views ────────────────────────────────────


class TestGapViews:
    def test_tree_shows_health_and_gaps(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A NO→NH₃ catalyst"))
        body = h.get(id=qid, view="tree").body
        assert "health" in body and "momentum" in body
        # a lonely quest surfaces its thin-support gap in the tree rollup
        assert "gaps" in body and "thin-support" in body

    def test_view_gaps_focuses_one_quest(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        h = _handler(store)
        qid = _created_id(h.put(text="A well-supported striving"))
        for title in ("paper A", "paper B"):
            store.add_link(
                src_ref_id=seed_ref(store, title=title),
                dst_ref_id=qid,
                relation="serves",
            )
        body = h.get(id=qid, view="gaps").body
        assert body.startswith("# gaps")
        assert "no gaps" in body  # 2 papers → well-supported

    def test_gaps_dashboard_lists_active_quests(self, store: Any) -> None:
        h = _handler(store)
        _created_id(h.put(text="Striving alpha"))
        body = h.get(id="/gaps").body
        assert "exploration queue" in body
        assert "Striving alpha" in body
