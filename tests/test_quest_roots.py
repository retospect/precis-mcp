"""Tests for ``search(kind='quest', view='roots')`` — the corpus-wide
read gr634363 asked for: list the top-level quests (a *root* is a live
quest serving no other live quest) with the ``serves`` DAG indented
beneath them, in one call. Same render on ``get(id='/roots')``; the
``args={'roots': true}`` flag is the same view spelled as a kwarg.

Runs against real PG (the ``store`` fixture) so it exercises the
``serves`` link map end to end.
"""

from __future__ import annotations

import re
from typing import Any

import pytest


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    return QuestHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


def _seed_ladder(store: Any) -> dict[str, int]:
    """root_a ← child ← grandchild on the serves DAG; root_b stands alone."""
    h = _handler(store)
    root_a = _created_id(h.put(text="Root striving A"))
    child = _created_id(h.put(text="Sub-quest serving A"))
    grandchild = _created_id(h.put(text="Rung serving the sub-quest"))
    root_b = _created_id(h.put(text="Standalone striving B"))
    store.add_link(src_ref_id=child, dst_ref_id=root_a, relation="serves")
    store.add_link(src_ref_id=grandchild, dst_ref_id=child, relation="serves")
    return {
        "root_a": root_a,
        "child": child,
        "grandchild": grandchild,
        "root_b": root_b,
    }


def _line_of(body: str, qid: int) -> str:
    return next(ln for ln in body.splitlines() if f"qu{qid}" in ln)


class TestRootsForest:
    def test_roots_render_the_dag_from_its_roots(self, store: Any) -> None:
        ids = _seed_ladder(store)
        body = _handler(store).search(view="roots").body
        assert body.startswith("# 2 quest roots · 4 live quests")
        lines = body.splitlines()
        # Roots render at depth 0 with lifecycle; servers indent beneath.
        assert _line_of(body, ids["root_a"]).startswith("◆ ")
        assert _line_of(body, ids["root_b"]).startswith("◆ ")
        assert "[active]" in _line_of(body, ids["root_a"])
        assert _line_of(body, ids["child"]).startswith("  ◆ ")
        assert _line_of(body, ids["grandchild"]).startswith("    ◆ ")
        # …and the indentation really is "under": the ladder order holds.
        order = [
            lines.index(_line_of(body, ids[k]))
            for k in ("root_a", "child", "grandchild")
        ]
        assert order == sorted(order)

    def test_serving_only_a_retired_quest_still_counts_as_root(
        self, store: Any
    ) -> None:
        h = _handler(store)
        parent = _created_id(h.put(text="Soon-retired striving"))
        child = _created_id(h.put(text="Orphaned sub-quest"))
        store.add_link(src_ref_id=child, dst_ref_id=parent, relation="serves")
        store.retire_ref(parent)
        body = h.search(view="roots").body
        # The dead parent is gone from the DAG entirely; its child is a root.
        assert f"qu{parent}" not in body
        assert _line_of(body, child).startswith("◆ ")

    def test_diamond_child_renders_under_both_parents(self, store: Any) -> None:
        h = _handler(store)
        p1 = _created_id(h.put(text="Parent one"))
        p2 = _created_id(h.put(text="Parent two"))
        shared = _created_id(h.put(text="Shared sub-quest"))
        store.add_link(src_ref_id=shared, dst_ref_id=p1, relation="serves")
        store.add_link(src_ref_id=shared, dst_ref_id=p2, relation="serves")
        body = h.search(view="roots").body
        assert body.startswith("# 2 quest roots · 3 live quests")
        rows = [ln for ln in body.splitlines() if f"qu{shared}" in ln]
        assert len(rows) == 2
        assert all(ln.startswith("  ◆ ") for ln in rows)

    def test_deep_acyclic_chain_is_not_reported_as_a_cycle(self, store: Any) -> None:
        """A rung past the depth cap renders as a "…" row and stays
        marked rendered — the orphan pass must not promote it to a
        fallback root or claim a serves cycle that does not exist."""
        h = _handler(store)
        top = _created_id(h.put(text="Top striving"))
        rungs = [top]
        for i in range(4):
            rung = _created_id(h.put(text=f"Rung {i}"))
            store.add_link(src_ref_id=rung, dst_ref_id=rungs[-1], relation="serves")
            rungs.append(rung)
        body = h.search(view="roots").body
        assert "unreachable from any root" not in body
        assert body.startswith("# 1 quest root · 5 live quests")
        rows = [ln for ln in body.splitlines() if f"qu{rungs[-1]}" in ln]
        assert len(rows) == 1
        # The capped row sits one level below its depth-3 parent.
        assert rows[0].startswith("        ◆ ")
        assert rows[0].rstrip().endswith("…")

    def test_forest_pages_past_the_fetch_cap(self, store: Any) -> None:
        """The fetch pages by id_min: a child whose parent lands on an
        earlier page still renders under it, and nothing is dropped. A
        capped list_refs stands in for a corpus past 500 live quests."""
        from unittest import mock

        h = _handler(store)
        real_list = store.list_refs

        def capped_list(*args: Any, **kwargs: Any) -> Any:
            kwargs["limit"] = min(int(kwargs.get("limit", 500)), 2)
            return real_list(*args, **kwargs)

        parent = _created_id(h.put(text="Paged parent"))
        child = _created_id(h.put(text="Paged child"))
        store.add_link(src_ref_id=child, dst_ref_id=parent, relation="serves")
        with mock.patch.object(store, "list_refs", capped_list):
            body = h.search(view="roots").body
        assert body.startswith("# 1 quest root · 2 live quests")
        assert _line_of(body, child).startswith("  ◆ ")

    def test_cycle_with_no_root_stays_visible(self, store: Any) -> None:
        """A serves-cycle reachable from no root is promoted to fallback
        roots rather than vanishing (same rule as the web index)."""
        h = _handler(store)
        a = _created_id(h.put(text="Cyclic striving A"))
        b = _created_id(h.put(text="Cyclic striving B"))
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="serves")
        store.add_link(src_ref_id=b, dst_ref_id=a, relation="serves")
        body = h.search(view="roots").body
        assert "unreachable from any root" in body
        assert _line_of(body, a).startswith("◆ ")
        assert _line_of(body, b).startswith("  ◆ ")

    def test_no_live_quests(self, store: Any) -> None:
        body = _handler(store).search(view="roots").body
        assert "no live quests" in body

    def test_path_view_renders_the_same_forest(self, store: Any) -> None:
        _seed_ladder(store)
        body = _handler(store).get(id="/roots").body
        assert body.startswith("# 2 quest roots · 4 live quests")

    def test_get_roots_view_without_id_redirects_to_search(self, store: Any) -> None:
        from precis.errors import BadInput

        with pytest.raises(BadInput, match="search view"):
            _handler(store).get(view="roots")

    def test_roots_flag_is_the_same_view(self, store: Any) -> None:
        ids = _seed_ladder(store)
        body = _handler(store).search(roots=True).body
        assert body.startswith("# 2 quest roots · 4 live quests")
        assert _line_of(body, ids["child"]).startswith("  ◆ ")

    def test_roots_flag_and_view_must_agree(self, store: Any) -> None:
        from precis.errors import BadInput

        with pytest.raises(BadInput, match="disagree"):
            _handler(store).search(view="list", roots=True)

    def test_narrowing_args_refused_loudly(self, store: Any) -> None:
        """The forest is a fixed shape: q=/tags=/sort= refuse rather than
        silently no-op (the silent-drop version of this call is the gripe)."""
        from precis.errors import BadInput

        _seed_ladder(store)
        h = _handler(store)
        with pytest.raises(BadInput, match="not applied by view='roots'"):
            h.search(view="roots", q="striving")
        with pytest.raises(BadInput, match="not applied by view='roots'"):
            h.search(view="roots", sort="prio")
        with pytest.raises(BadInput, match="not applied by view='roots'"):
            h.search(view="roots", page=2)
        with pytest.raises(BadInput, match="not applied by view='roots'"):
            h.search(view="roots", tags=["STATUS:active"])

    def test_roots_view_with_a_scope_refused(self, store: Any) -> None:
        from precis.errors import BadInput

        ids = _seed_ladder(store)
        with pytest.raises(BadInput, match="scoped"):
            _handler(store).search(view="roots", q="x", scope=f"qu{ids['root_a']}")

    def test_unknown_search_view_names_roots(self, store: Any) -> None:
        """The misspelling a caller guesses at ('tree' lives on get) points
        at the corpus read that does exist."""
        from precis.errors import BadInput

        with pytest.raises(BadInput, match="unknown view 'tree'") as exc:
            _handler(store).search(view="tree")
        assert "roots" in str(exc.value.next) or "roots" in str(exc.value.options)
