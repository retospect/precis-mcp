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


class TestEdit:
    def test_edit_replace_rewrites_founding_text_keeps_logbook_and_links(
        self, store: Any
    ) -> None:
        """gripe 169979: edit(mode='replace') rewrites the founding striving
        statement in place — without touching logbook entries or
        serves/served-by links."""
        from tests.workers._helpers import seed_ref

        h = _handler(store)
        qid = _created_id(h.put(text="A NO->NH3 catalyst, first draft wording"))
        h.put(id=qid, text="Try Fe-N4 single-atom sites", entry="hypothesis")
        proj = seed_ref(store, title="Screen Fe-N4 candidates")
        store.add_link(src_ref_id=proj, dst_ref_id=qid, relation="serves")

        resp = h.edit(id=qid, mode="replace", text="A NO->NH3 catalyst, polished")
        assert "replaced" in resp.body

        ref = store.get_ref(kind="quest", id=qid)
        assert ref.title.startswith("A NO->NH3 catalyst, polished")
        assert "first draft wording" not in ref.title

        # logbook entry survives untouched
        body = h.get(id=qid).body
        assert "hypothesis" in body
        assert "Try Fe-N4 single-atom sites" in body

        # serves link survives untouched
        served = store.links_for(qid, direction="in", relation="serves")
        assert any(ln.src_ref_id == proj for ln in served)

        # the rewritten statement re-embeds into the card_combined chunk
        with store.pool.connection() as conn:
            card = conn.execute(
                "select text from chunks where ref_id=%s and ord=-1", (qid,)
            ).fetchone()
        assert card is not None and "polished" in card[0]

    def test_edit_requires_replace_mode_and_text(self, store: Any) -> None:
        from precis.errors import BadInput

        h = _handler(store)
        qid = _created_id(h.put(text="Some striving"))
        try:
            h.edit(id=qid, mode="append", text="x")
        except BadInput:
            pass
        else:  # pragma: no cover - guard
            raise AssertionError("edit should reject mode!='replace'")
        try:
            h.edit(id=qid, mode="replace")
        except BadInput:
            pass
        else:  # pragma: no cover - guard
            raise AssertionError("edit(mode='replace') should require text=")


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

    def test_draft_links_serves_via_public_link_path(self, store: Any) -> None:
        """gripe 161912: a draft can be wired as a quest's server through
        the public ``link()`` verb (not a direct store INSERT), and the
        tree rollup — which walks ``links_for(direction='in',
        relation='serves')`` — sees it."""
        from precis.dispatch import Hub
        from precis.handlers.draft import DraftHandler

        h = _handler(store)
        qid = _created_id(h.put(text="A NO→NH₃ catalyst"))

        proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
        draft = DraftHandler(hub=Hub(store=store))
        draft.put(id="serving-doc", title="Serving Doc", project=proj)

        resp = draft.link(id="serving-doc", target=f"quest:{qid}", rel="serves")
        assert "link" in resp.body

        tree = h.get(id=qid, view="tree").body
        assert "draft (1) serving" in tree
        assert "Serving Doc" in tree

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
