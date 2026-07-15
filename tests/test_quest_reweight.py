"""Tests for quest reweighting — slice 2 of the quest layer.

Covers the shared field primitive (:mod:`precis.quest.reweight`) and the three
sinks it feeds: rotation (the doable view), acquisition (the OA fetch backlog),
and reading (the meditation concept selection). Also the quest handler's
``PRIO:`` → ``prio`` column sync that makes a quest's striving weight canonical.

Runs against real PG (the ``store`` fixture) so the SQL reweighting (unnest CTE
join in the doable query; the correlated LATERAL in the fetch backlog) is
exercised end to end.
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.handlers.todo import TodoHandler
from precis.quest import reweight


def _q(store: Any) -> QuestHandler:
    return QuestHandler(hub=Hub(store=store))


def _qid(resp: Any) -> int:
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


def _mk_quest(store: Any, text: str, *, prio_tag: str | None = None) -> int:
    h = _q(store)
    qid = _qid(h.put(text=text))
    if prio_tag:
        h.tag(id=qid, add=[prio_tag])
    return qid


# ── the field primitive ──────────────────────────────────────────────


class TestBaseWeight:
    def test_inverts_and_normalises_prio(self) -> None:
        assert reweight.base_weight(1) == 1.0  # hottest
        assert reweight.base_weight(5) == 0.6  # default
        assert reweight.base_weight(10) == 0.1  # coolest
        assert reweight.base_weight(None) == 0.6  # unset → neutral default


class TestActiveQuestWeights:
    def test_only_active_quests_contribute(self, store: Any) -> None:
        h = _q(store)
        hot = _mk_quest(store, "Hot striving", prio_tag="PRIO:urgent")
        cold = _qid(h.put(text="Set-aside striving"))
        h.tag(id=cold, add=["STATUS:dormant"])
        w = reweight.active_quest_weights(store)
        assert w.get(hot) == 1.0
        assert cold not in w  # dormant exerts no pull

    def test_priority_flows_down_the_quest_ladder(self, store: Any) -> None:
        # grand (urgent, 1.0) ← sub (low base 0.3). The sub inherits
        # max(0.3, 1.0 × DECAY) = 0.5 — priority flows down the ladder.
        grand = _mk_quest(store, "Grand striving", prio_tag="PRIO:urgent")
        sub = _mk_quest(store, "Sub striving", prio_tag="PRIO:low")
        store.add_link(src_ref_id=sub, dst_ref_id=grand, relation="serves")
        w = reweight.active_quest_weights(store)
        assert w[grand] == 1.0
        assert w[sub] == 1.0 * reweight.STRIVING_DECAY  # 0.5 > its own 0.3

    def test_empty_when_nothing_active(self, store: Any) -> None:
        assert reweight.active_quest_weights(store) == {}


class TestServedStrivingWeight:
    def test_work_inherits_the_quest_it_serves_max_on_overlap(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        hot = _mk_quest(store, "Hot", prio_tag="PRIO:urgent")  # 1.0
        warm = _mk_quest(store, "Warm", prio_tag="PRIO:normal")  # 0.6
        w1 = seed_ref(store, title="serves hot only")
        w2 = seed_ref(store, title="serves both")
        w3 = seed_ref(store, title="serves nothing")
        store.add_link(src_ref_id=w1, dst_ref_id=hot, relation="serves")
        store.add_link(src_ref_id=w2, dst_ref_id=hot, relation="serves")
        store.add_link(src_ref_id=w2, dst_ref_id=warm, relation="serves")
        got = reweight.served_striving_weight(store, [w1, w2, w3])
        assert got[w1] == 1.0
        assert got[w2] == 1.0  # max(1.0, 0.6)
        assert got[w3] == 0.0

    def test_serving_a_dormant_quest_is_zero(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        h = _q(store)
        dq = _qid(h.put(text="Dormant"))
        h.tag(id=dq, add=["STATUS:dormant"])
        w = seed_ref(store, title="serves a dormant quest")
        store.add_link(src_ref_id=w, dst_ref_id=dq, relation="serves")
        assert reweight.served_striving_weight(store, [w])[w] == 0.0


class TestServerWeightsForActiveQuests:
    def test_keyed_by_server_with_kind_filter(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        q = _mk_quest(store, "Striving", prio_tag="PRIO:urgent")
        paper = seed_ref(store, title="a paper")  # seed_ref makes kind='paper'
        store.add_link(src_ref_id=paper, dst_ref_id=q, relation="serves")
        all_servers = reweight.server_weights_for_active_quests(store)
        assert all_servers.get(paper) == 1.0
        papers_only = reweight.server_weights_for_active_quests(
            store, server_kind="paper"
        )
        assert papers_only.get(paper) == 1.0
        todos_only = reweight.server_weights_for_active_quests(
            store, server_kind="todo"
        )
        assert paper not in todos_only


# ── quest handler: PRIO: → the canonical prio column ─────────────────


class TestQuestPrioColumn:
    def test_prio_tag_on_create_sets_column(self, store: Any) -> None:
        h = _q(store)
        qid = _qid(h.put(text="Urgent striving", tags=["PRIO:urgent"]))
        assert store.get_ref(kind="quest", id=qid).prio == 1

    def test_prio_tag_via_tag_sets_and_clears_column(self, store: Any) -> None:
        h = _q(store)
        qid = _qid(h.put(text="A striving"))
        h.tag(id=qid, add=["PRIO:high"])
        assert store.get_ref(kind="quest", id=qid).prio == 3
        h.tag(id=qid, remove=["PRIO:high"])
        assert store.get_ref(kind="quest", id=qid).prio is None


# ── sink 1: rotation (the doable view) ───────────────────────────────


class TestRotationReweight:
    def test_quest_serving_strategic_surfaces_first(self, store: Any) -> None:
        from tests.conftest import id_of

        th = TodoHandler(hub=Hub(store=store))
        q = _mk_quest(store, "Hot quest", prio_tag="PRIO:urgent")
        # Strategic B created FIRST (lower ref_id) and serves nothing — without
        # reweighting its leaf wins the ref_id tiebreak.
        root_b = id_of(th.put(text="Strategic B.", tags=["level:strategic"]).body)
        leaf_b = id_of(th.put(text="Leaf B work.", parent_id=root_b).body)
        # Strategic A created SECOND but serves the hot quest.
        root_a = id_of(th.put(text="Strategic A.", tags=["level:strategic"]).body)
        leaf_a = id_of(th.put(text="Leaf A work.", parent_id=root_a).body)
        store.add_link(src_ref_id=root_a, dst_ref_id=q, relation="serves")

        body = th.search(view="doable").body
        # A's leaf (served) now precedes B's leaf despite the higher ref_id.
        assert body.index(f"td{leaf_a}") < body.index(f"td{leaf_b}")

    def test_no_op_without_active_quests(self, store: Any) -> None:
        from tests.conftest import id_of

        th = TodoHandler(hub=Hub(store=store))
        root_a = id_of(th.put(text="Strategic A.", tags=["level:strategic"]).body)
        leaf_a = id_of(th.put(text="Leaf A.", parent_id=root_a).body)
        root_b = id_of(th.put(text="Strategic B.", tags=["level:strategic"]).body)
        leaf_b = id_of(th.put(text="Leaf B.", parent_id=root_b).body)
        body = th.search(view="doable").body
        # No quests: plain ref_id order (A before B).
        assert body.index(f"td{leaf_a}") < body.index(f"td{leaf_b}")


# ── sink 2: acquisition (the OA fetch backlog) ───────────────────────


class TestAcquisitionReweight:
    def test_quest_serving_stub_jumps_the_fetch_queue(self, store: Any) -> None:
        from precis.workers.fetch_oa import claim_stubs_to_fetch
        from tests.workers.test_fetch_oa import _seed_paper_stub

        q = _mk_quest(store, "Hot quest", prio_tag="PRIO:urgent")
        # Served stub seeded FIRST (older ref_id) — newest-first ordering would
        # otherwise sink it behind the newer stub.
        served = _seed_paper_stub(store, cite_key="served2024", doi="10.1/served")
        newer = _seed_paper_stub(store, cite_key="newer2024", doi="10.2/newer")
        store.add_link(src_ref_id=served, dst_ref_id=q, relation="serves")
        with store.pool.connection() as conn:
            stubs = claim_stubs_to_fetch(conn, limit=10)
            conn.commit()
        order = [s.ref_id for s in stubs]
        assert order.index(served) < order.index(newer)


# ── sink 3: reading (the meditation concept selection) ───────────────


class TestReadingBias:
    def test_bias_pulls_quest_serving_concept_to_front(self, store: Any) -> None:
        from precis.handlers.concept import ConceptHandler
        from precis.reading.meditation import _load

        ch = ConceptHandler(hub=Hub(store=store))

        def _cid(resp: Any) -> int:
            m = re.search(r"\bcn(\d+)\b", resp.body)
            assert m is not None
            return int(m.group(1))

        q = _mk_quest(store, "Hot quest", prio_tag="PRIO:urgent")
        # Three concepts; only the last-created serves the quest. Without bias
        # the recency order (DESC) would still lead with it, so make it the
        # OLDEST to isolate the bias effect.
        served = _cid(ch.put(text="served concept — one that serves the quest"))
        _cid(ch.put(text="filler one — no quest"))
        _cid(ch.put(text="filler two — no quest"))
        store.add_link(src_ref_id=served, dst_ref_id=q, relation="serves")

        biased, _adj = _load(store, None, 10, bias_active_quests=True)
        assert biased[0][0] == served  # quest-serving concept leads
        plain, _adj2 = _load(store, None, 10)
        assert plain[0][0] != served  # unbiased leads with the newest instead
