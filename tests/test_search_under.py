"""``search(args={'under': '<handle>', 'depth': N})`` — hits restricted to the
descendants of a ref on the ``part-of`` tree."""

from __future__ import annotations

import pytest

from precis.cli.memory import SPACE_TAG, _created_id
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils import handle_registry

HUB = "section:index"


@pytest.fixture
def runtime(runtime_with_store: PrecisRuntime) -> PrecisRuntime:
    return runtime_with_store


def _h(i: int) -> str:
    h = handle_registry.try_format("memory", i)
    assert h is not None
    return h


class _Tree:
    """hub -> {summary -> {detail}, direct_gotcha}; stray outside the hub."""

    def __init__(self, runtime: PrecisRuntime, store: Store) -> None:
        handler = MemoryHandler(hub=runtime.hub)

        def note(title: str, body: str, *tags: str) -> int:
            return _created_id(
                handler.put(text=body, title=title, tags=[SPACE_TAG, *tags])
            )

        self.hub = note("Pump hub", "pumps", HUB)
        self.summary = note("Pump summary", "overview zebra", "section:threads")
        self.detail = note("Pump detail", "impeller zebra", "section:gotchas")
        self.direct = note("Direct gotcha", "seal zebra", "section:gotchas")
        self.stray = note("Stray gotcha", "elsewhere zebra", "section:gotchas")
        for child, parent in (
            (self.summary, self.hub),
            (self.detail, self.summary),
            (self.direct, self.hub),
        ):
            store.add_link(src_ref_id=child, dst_ref_id=parent, relation="part-of")
        self.store = store
        self.rt = runtime

    def index(self, **args: object) -> list[str]:
        out = self.rt.dispatch(
            "search",
            {"kind": "memory", "view": "index", **args},
        )
        return [ln.split(" (")[0] for ln in out.splitlines() if ln.startswith("- ")]


@pytest.fixture
def tree(runtime: PrecisRuntime, store: Store) -> _Tree:
    return _Tree(runtime, store)


def test_depth_one_is_direct_members_unlimited_reaches_the_detail(
    tree: _Tree,
) -> None:
    under = {"under": _h(tree.hub)}
    assert set(tree.index(**under)) == {
        "- Pump summary",
        "- Pump detail",
        "- Direct gotcha",
    }
    assert set(tree.index(**under, depth=1)) == {"- Pump summary", "- Direct gotcha"}
    assert tree.index(under=_h(tree.summary)) == ["- Pump detail"]


def test_under_combines_with_tags_and_via_extras(tree: _Tree) -> None:
    assert set(tree.index(under=_h(tree.hub), tags=[SPACE_TAG, "section:gotchas"])) == {
        "- Pump detail",
        "- Direct gotcha",
    }
    # the slim MCP door delivers args={...} as __extras__
    got = tree.index(__extras__={"under": _h(tree.hub), "depth": 1})
    assert set(got) == {"- Pump summary", "- Direct gotcha"}


def test_under_with_a_query_restricts_ranked_hits(tree: _Tree) -> None:
    out = tree.rt.dispatch(
        "search", {"kind": "memory", "q": "zebra", "under": _h(tree.hub)}
    )
    assert "Pump detail" in out and "Direct gotcha" in out
    assert "Stray gotcha" not in out
    only_direct = tree.rt.dispatch(
        "search",
        {"kind": "memory", "q": "zebra", "under": _h(tree.hub), "depth": 1},
    )
    assert "Direct gotcha" in only_direct and "Pump detail" not in only_direct


def test_under_without_q_lists_by_recency(tree: _Tree) -> None:
    with tree.store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '9 days' WHERE ref_id = %s",
            (tree.summary,),
        )
    assert tree.index(under=_h(tree.hub))[-1] == "- Pump summary"


def test_unknown_handle_and_bad_depth_are_named(tree: _Tree) -> None:
    out = tree.rt.dispatch("search", {"kind": "memory", "q": "x", "under": "me999999"})
    assert "under='me999999' resolves to no live ref" in out
    out = tree.rt.dispatch(
        "search",
        {"kind": "memory", "q": "x", "under": _h(tree.hub), "depth": 0},
    )
    assert "depth= must be a positive integer" in out
    out = tree.rt.dispatch("search", {"kind": "memory", "q": "x", "depth": 2})
    assert "depth= needs under=" in out


def test_empty_subtree_yields_zero_hits_not_no_filter(tree: _Tree) -> None:
    # tree.detail has no part-of children: `under` must return nothing, never
    # silently fall back to an unfiltered search
    assert tree.index(under=_h(tree.detail)) == []
    out = tree.rt.dispatch(
        "search", {"kind": "memory", "q": "zebra", "under": _h(tree.detail)}
    )
    assert "Pump detail" not in out and "Direct gotcha" not in out


def test_cross_kind_search_leaves_todo_and_taxon_own_under_alone(
    tree: _Tree,
) -> None:
    args = {"q": "zebra", "under": "td1", "depth": 2}
    assert tree.rt._resolve_under(args) is None
    assert args["under"] == "td1" and args["depth"] == 2
    for kind in ("todo", "taxon"):
        a = {"kind": kind, "under": "x", "depth": 1}
        assert tree.rt._resolve_under(a) is None and a["under"] == "x"
