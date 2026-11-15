"""``get(view='dependants')`` (gr626866) — the rendered rely-side page.

Covers the handler seam: the view on numeric-ref kinds (memory +
finding exercise the shared dispatch; todo/gripe inherit it) and on
paper, plus the footer contract — the exact continue call when the
walk is truncated, the bare ``complete`` word only when exhausted, and
a BadInput naming the first-page call for a bad cursor.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.handlers.paper import PaperHandler
from precis.store import Store


def _mem(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _paper(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _mk_ref(store: Store, kind: str, title: str) -> int:
    return store.insert_ref(kind=kind, slug=None, title=title).id


def _mk_paper(store: Store, slug: str, title: str = "test paper") -> int:
    return store.insert_ref(kind="paper", slug=slug, title=title).id


class TestNumericRefView:
    def test_renders_rows_and_complete_footer(self, hub: Hub, store: Store) -> None:
        target = _mk_ref(store, "memory", "the relied-upon note")
        dep = _mk_ref(store, "finding", "the relying claim")
        store.add_link(src_ref_id=dep, dst_ref_id=target, relation="cites")
        resp = _mem(hub).get(id=target, view="dependants")
        assert f"# memory {target} — dependants" in resp.body
        assert "cited-by" in resp.body
        assert f"fi{dep}" in resp.body
        assert "shown 1 of 1" in resp.body
        assert "complete" in resp.body
        assert "next:" not in resp.body

    def test_truncated_footer_names_exact_next_call(
        self, hub: Hub, store: Store
    ) -> None:
        target = _mk_ref(store, "memory", "crowded note")
        for i in range(5):
            dep = _mk_ref(store, "todo", f"citing todo {i}")
            store.add_link(src_ref_id=dep, dst_ref_id=target, relation="cites")
        resp = _mem(hub).get(id=target, view="dependants", limit=3)
        assert "shown 3 of 5" in resp.body
        assert "INCOMPLETE" in resp.body
        assert (
            f"get(kind='memory', id='{target}', view='dependants', "
            f"args={{'depth': 2, 'limit': 3, 'cursor': 'dep:3'}})" in resp.body
        )
        # page 2 completes
        resp2 = _mem(hub).get(id=target, view="dependants", limit=3, cursor="dep:3")
        assert "shown 2 of 5" in resp2.body
        assert "complete" in resp2.body
        assert "next:" not in resp2.body

    def test_bad_cursor_names_first_page_call(self, hub: Hub, store: Store) -> None:
        target = _mk_ref(store, "memory", "cursor note")
        with pytest.raises(BadInput) as ei:
            _mem(hub).get(id=target, view="dependants", cursor="bogus")
        nxt = ei.value.next
        nxt_s = nxt if isinstance(nxt, str) else " ".join(nxt or [])
        assert "view='dependants'" in nxt_s
        assert "cursor" not in nxt_s

    def test_out_of_range_cursor_refused(self, hub: Hub, store: Store) -> None:
        """A cursor past the seen count is BadInput through the view too
        — an empty "complete" page would read as no dependants (review
        td681243)."""
        target = _mk_ref(store, "memory", "ranged cursor note")
        dep = _mk_ref(store, "finding", "the one dependant")
        store.add_link(src_ref_id=dep, dst_ref_id=target, relation="cites")
        with pytest.raises(BadInput, match="cursor"):
            _mem(hub).get(id=target, view="dependants", cursor="dep:1")
        with pytest.raises(BadInput, match="cursor"):
            _mem(hub).get(id=target, view="dependants", cursor="dep:999999")

    def test_depth_arg_bound(self, hub: Hub, store: Store) -> None:
        target = _mk_ref(store, "memory", "depth note")
        mid = _mk_ref(store, "finding", "middle claim")
        far = store.insert_ref(kind="draft", slug="fardraftdep", title="far draft").id
        # target corroborates the claim → the claim relies on target.
        store.add_link(src_ref_id=target, dst_ref_id=mid, relation="corroborates")
        # the draft cites the claim → relies on it → depth-2 dependant.
        store.add_link(src_ref_id=far, dst_ref_id=mid, relation="cites")
        resp1 = _mem(hub).get(id=target, view="dependants", depth=1)
        assert "shown 1 of 1" in resp1.body
        assert f"fi{mid}" in resp1.body
        assert f"dr{far}" not in resp1.body
        resp2 = _mem(hub).get(id=target, view="dependants", depth=2)
        assert f"dr{far}" in resp2.body

    def test_walk_args_refused_on_other_views(self, hub: Hub, store: Store) -> None:
        target = _mk_ref(store, "memory", "scope note")
        with pytest.raises(BadInput, match="dependants"):
            _mem(hub).get(id=target, view="links", depth=2)
        with pytest.raises(BadInput, match="dependants"):
            _mem(hub).get(id=target, cursor="dep:0")


class TestPaperView:
    def test_paper_dependants_view(self, hub: Hub, store: Store) -> None:
        paper_id = _mk_paper(store, "paperview2026")
        hub_ref = _mk_ref(store, "finding", "claim leaning on the paper")
        store.add_link(src_ref_id=paper_id, dst_ref_id=hub_ref, relation="corroborates")
        resp = _paper(hub).get(id="paperview2026", view="dependants")
        assert f"fi{hub_ref}" in resp.body
        assert "corroborates" in resp.body
        assert "shown 1 of 1" in resp.body
        assert "complete" in resp.body

    def test_paper_next_call_echoes_slug(self, hub: Hub, store: Store) -> None:
        paper_id = _mk_paper(store, "paperview2026b")
        for i in range(4):
            dep = _mk_ref(store, "memory", f"citing note {i}")
            store.add_link(src_ref_id=dep, dst_ref_id=paper_id, relation="cites")
        resp = _paper(hub).get(
            id="paperview2026b", view="dependants", args={"limit": 2}
        )
        assert "shown 2 of 4" in resp.body
        assert "id='paperview2026b'" in resp.body
        assert "'cursor': 'dep:2'" in resp.body
