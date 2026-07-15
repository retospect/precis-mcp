"""Tests for the `quest` kind — the striving above the work (quest layer,
slice 1, docs/proposals/quest-layer.md).

Covers: create (emits the embeddable card + STATUS:active), the append-only
logbook (entry types + deed/tote accounting), the `serves` relation + its
read-time inverse, and the `view='tree'` rollup (servers grouped by kind,
sub-quest recursion, deed ledger). Runs against real PG (the ``store`` fixture)
so it exercises migration 0064's seeds + the tag-axis enforcement.
"""

from __future__ import annotations

import re
from typing import Any


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    return QuestHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


class TestQuestCreate:
    def test_put_creates_striving_with_card_and_active_status(self, store: Any) -> None:
        h = _handler(store)
        resp = h.put(text="A NO→NH₃ catalyst with no external energy")
        qid = _created_id(resp)
        ref = store.get_ref(kind="quest", id=qid)
        assert ref.title.startswith("A NO→NH₃")
        # born striving
        tags = [str(t) for t in store.tags_for(qid)]
        assert "STATUS:active" in tags
        # embeddable card_combined emitted (ord=-1) = the quest vector
        with store.pool.connection() as conn:
            card = conn.execute(
                "select text from chunks where ref_id=%s and ord=-1", (qid,)
            ).fetchone()
        assert card is not None and "catalyst" in card[0]

    def test_lifecycle_status_is_accepted_but_done_is_not(self, store: Any) -> None:
        from precis.errors import BadInput

        h = _handler(store)
        qid = _created_id(h.put(text="Heal the environment"))
        # dormant/abandoned are legal transitions
        h.tag(id=qid, add=["STATUS:dormant"])
        assert "STATUS:dormant" in [str(t) for t in store.tags_for(qid)]
        # a quest never completes — `done` is not on its axis
        try:
            h.tag(id=qid, add=["STATUS:done"])
        except BadInput:
            pass
        else:  # pragma: no cover - guard
            raise AssertionError("STATUS:done should be rejected on a quest")


class TestLogbook:
    def test_append_entry_and_render_ledger(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A solid NO→NH₃ catalyst"))
        h.put(id=qid, text="Try Fe–N₄ single-atom sites", entry="hypothesis")
        h.put(id=qid, text="Barrier too high on the second PCET", entry="dead-end")
        h.put(id=qid, text="Found a viable dual-metal site", entry="milestone")
        body = h.get(id=qid).body
        assert "logbook" in body
        assert "hypothesis" in body and "dead-end" in body and "milestone" in body
        # a milestone is a deed
        assert "1 deed" in body

    def test_cost_entries_sum_into_the_tote(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="Bio/o-chem NO→NH₃ route"))
        h.put(id=qid, text="relax batch A", entry="result", cost=1.5)
        h.put(id=qid, text="relax batch B", entry="result", cost=2.0)
        body = h.get(id=qid, view="tree").body
        assert "tote 3.5" in body

    def test_bad_entry_type_rejected(self, store: Any) -> None:
        from precis.errors import BadInput

        h = _handler(store)
        qid = _created_id(h.put(text="Some striving"))
        try:
            h.put(id=qid, text="x", entry="not-a-type")
        except BadInput as e:
            assert "milestone" in str(e.next)
        else:  # pragma: no cover - guard
            raise AssertionError("unknown entry type should be rejected")

    def test_append_rejects_link_and_tags(self, store: Any) -> None:
        from precis.errors import BadInput

        h = _handler(store)
        qid = _created_id(h.put(text="Some striving"))
        for kwargs in ({"tags": ["x"]}, {"link": "paper:1", "rel": "serves"}):
            try:
                h.put(id=qid, text="entry", **kwargs)
            except BadInput:
                pass
            else:  # pragma: no cover - guard
                raise AssertionError(f"append should reject {kwargs}")


class TestServesAndTree:
    def test_serves_edge_surfaces_in_tree(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        h = _handler(store)
        qid = _created_id(h.put(text="A NO→NH₃ catalyst"))
        # a paper (plain ref) put in the quest's service
        proj = seed_ref(store, title="Screen Fe–N₄ candidates")
        store.add_link(src_ref_id=proj, dst_ref_id=qid, relation="serves")

        tree = h.get(id=qid, view="tree").body
        assert "paper (1) serving" in tree
        assert "Screen Fe–N₄" in tree
        # inverse resolves: the quest is served-by the project
        served = store.links_for(qid, direction="out", relation="served-by")
        assert any(proj in (ln.src_ref_id, ln.dst_ref_id) for ln in served)

    def test_sub_quest_recurses_under_grand_quest(self, store: Any) -> None:
        h = _handler(store)
        grand = _created_id(h.put(text="Heal the environment"))
        sub = _created_id(h.put(text="A NO→NH₃ catalyst"))
        # sub-quest serves the grand striving (a DAG of strivings)
        store.add_link(src_ref_id=sub, dst_ref_id=grand, relation="serves")

        tree = h.get(id=grand, view="tree").body
        # the grand quest renders, and the sub-quest nests beneath it
        assert "Heal the environment" in tree
        assert "A NO→NH₃ catalyst" in tree
        # sub-quest is not miscounted as an ordinary 'quest' server group
        assert "quest (1) serving" not in tree


class TestListViews:
    def test_active_list_view(self, store: Any) -> None:
        h = _handler(store)
        _created_id(h.put(text="Striving one"))
        body = h.get(id="/active").body
        assert "Striving one" in body
