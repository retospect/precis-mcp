"""``search(args={'under': '<handle>', 'depth': N})`` — hits restricted to the
descendants of a ref on the ``part-of`` tree."""

from __future__ import annotations

from typing import Any

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


def test_under_scopes_the_queryless_tag_listing(tree: _Tree) -> None:
    """gr568264: under= + tags= without q= on the default view announced
    the scope but listed the whole corpus."""
    out = tree.rt.dispatch(
        "search",
        {"kind": "memory", "tags": ["section:gotchas"], "under": _h(tree.hub)},
    )
    assert "Pump detail" in out
    assert "Direct gotcha" in out
    assert "Stray gotcha" not in out


def test_under_scopes_the_queryless_tag_listing_with_empty_q_via_args(
    tree: _Tree,
) -> None:
    """gr621795's verbatim repro: q='' + tags= + args={'under': …} through
    the slim door. The empty-string q is the queryless listing shape; the
    banner, the header count and the membership must all agree on the
    subtree ∩ tags set — never '5 of 1377' under a scoped banner."""
    out = tree.rt.dispatch(
        "search",
        {
            "kind": "memory",
            "q": "",
            "tags": ["section:gotchas"],
            "page_size": 5,
            "__extras__": {"under": _h(tree.hub)},
        },
    )
    assert f"_(under={_h(tree.hub)}: restricted to 3 descendants on part-of)_" in out
    # header count is the filtered total, not the tagged corpus size
    assert "# 2 memory entries tagged ['section:gotchas']" in out
    assert "Pump detail" in out
    assert "Direct gotcha" in out
    # the stray matches the tag but sits outside the subtree
    assert "Stray gotcha" not in out


def test_under_scopes_the_ranked_tagged_search(tree: _Tree) -> None:
    """gr621795: q= + tags= + under= on the ranked route — the resolved
    under set must intersect the tagged results, not only render in the
    banner. The stray matches both the tag and q from outside the subtree."""
    out = tree.rt.dispatch(
        "search",
        {
            "kind": "memory",
            "q": "zebra",
            "tags": ["section:gotchas"],
            "under": _h(tree.hub),
        },
    )
    assert f"_(under={_h(tree.hub)}: restricted to 3 descendants on part-of)_" in out
    assert "Pump detail" in out
    assert "Direct gotcha" in out
    assert "Stray gotcha" not in out


def test_under_queryless_tags_empty_intersection_lists_nothing(tree: _Tree) -> None:
    """gr621795: tags= + under= whose intersection is empty must render
    zero hits — never fall back to the unscoped tagged listing."""
    out = tree.rt.dispatch(
        "search",
        {"kind": "memory", "tags": ["section:gotchas"], "under": _h(tree.detail)},
    )
    assert "no memory entries tagged ['section:gotchas']" in out
    assert "Pump detail" not in out
    assert "Direct gotcha" not in out
    assert "Stray gotcha" not in out


def _tool_text(out: Any) -> str:
    """A verb/slim result: str, or a CallToolResult whose text we want."""
    content = getattr(out, "content", None)
    return content[0].text if content else str(out)


def test_bare_under_default_view_lists_the_subtree(tree: _Tree) -> None:
    """gr623799: search(kind='memory', under=<handle>) with no q=/tags=/
    view= must list the subtree, not BadInput 'search requires q='."""
    out = tree.rt.dispatch("search", {"kind": "memory", "under": _h(tree.hub)})
    assert "search requires q=" not in out
    assert "Pump detail" in out
    assert "Stray gotcha" not in out


def test_bare_under_through_full_verb(
    tree: _Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing client shape (gr623799): codex-mcp-client called the
    search verb with kind=memory, page_size=100, under=<handle> and no
    q=/tags=/link=. The full verb schema declares under=, so it reaches
    dispatch; a strict-schema client's undeclared page= is dropped by
    FastMCP before this layer."""
    from precis.tools import core as tools_core

    monkeypatch.setattr(tools_core, "_runtime", tree.rt)
    out = tools_core.search(kind="memory", page_size=100, under=_h(tree.hub))
    text = _tool_text(out)
    assert "search requires q=" not in text
    assert "Pump detail" in text
    assert "Stray gotcha" not in text


def test_bare_under_through_slim_door(
    tree: _Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr623799 through the advertised slim schema, both spellings: under=
    is not a core param, so the slim door either receives it top-level
    (filed into args= with a deprecation note, then promoted back — the
    memory handler declares it) or nested inside args= already. Both must
    list the subtree, not raise 'search requires q='."""
    from precis.tools import core as tools_core
    from precis.tools.mcp_slim import make_slim_verb

    monkeypatch.setattr(tools_core, "_runtime", tree.rt)
    slim = make_slim_verb("search", tools_core.search)

    top = slim(kind="memory", page_size=100, under=_h(tree.hub))
    top_text = _tool_text(top)
    assert "search requires q=" not in top_text
    assert "Pump detail" in top_text
    assert "Stray gotcha" not in top_text
    assert "top-level under is deprecated" in top_text

    nested = slim(kind="memory", args={"under": _h(tree.hub), "page_size": 100})
    nested_text = _tool_text(nested)
    assert "search requires q=" not in nested_text
    assert "Pump detail" in nested_text
    assert "Stray gotcha" not in nested_text
    assert "deprecated" not in nested_text


def test_under_scopes_the_queryless_link_listing(tree: _Tree) -> None:
    """The link= listing honours the same resolved scope."""
    tree.store.add_link(
        src_ref_id=tree.stray, dst_ref_id=tree.detail, relation="see-also"
    )
    out_all = tree.rt.dispatch("search", {"kind": "memory", "link": _h(tree.detail)})
    assert "Stray gotcha" in out_all
    out = tree.rt.dispatch(
        "search",
        {"kind": "memory", "link": _h(tree.detail), "under": _h(tree.hub)},
    )
    # The summary is linked to the detail (part-of) and in scope; the
    # stray's see-also edge stays out — the scope is real, not announced.
    assert "Pump summary" in out
    assert "Stray gotcha" not in out


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


def _bullets(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln.startswith("- ")]


def test_index_listing_pages_with_a_next_line(tree: _Tree) -> None:
    """gr538490: ``view='index'`` listings page via ``args={'page': N}`` —
    each page ends in a ``next:`` call re-rendering the caller's own
    ``under``/``page_size`` spelling."""
    p1 = tree.rt.dispatch(
        "search",
        {
            "kind": "memory",
            "view": "index",
            "page_size": 2,
            "__extras__": {"under": _h(tree.hub)},
        },
    )
    assert len(_bullets(p1)) == 2
    assert "next: search(kind='memory'" in p1
    assert f"'under': '{_h(tree.hub)}'" in p1
    assert "'page': 2" in p1 and "'page_size': 2" in p1
    p2 = tree.rt.dispatch(
        "search",
        {
            "kind": "memory",
            "view": "index",
            "page_size": 2,
            "__extras__": {"under": _h(tree.hub), "page": 2},
        },
    )
    assert len(_bullets(p2)) == 1
    assert "more — next:" not in p2
    assert not set(_bullets(p1)) & set(_bullets(p2))
    # under= is forwarded for the hint echo; the include filter still
    # holds — the stray outside the hub never appears
    assert "Stray gotcha" not in p1 + p2


def test_index_listing_pages_are_byte_bounded_too(
    tree: _Tree, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page closes at ``page_size`` rows OR the context cap, whichever
    first — byte-boundary pages mean no member ever falls between the
    fitted cut and the next count-page (the gripe's skipped-rows bug)."""
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "700")
    handler = MemoryHandler(hub=tree.rt.hub)
    extra = [
        _created_id(
            handler.put(text=f"extra {i} body", title=f"Extra {i}", tags=[SPACE_TAG])
        )
        for i in range(8)
    ]
    for m in extra:
        store.add_link(src_ref_id=m, dst_ref_id=tree.hub, relation="part-of")
    seen: list[str] = []
    page = 1
    while True:
        out = tree.rt.dispatch(
            "search",
            {
                "kind": "memory",
                "view": "index",
                "page_size": 50,
                "__extras__": {"under": _h(tree.hub), "page": page},
            },
        )
        rows = _bullets(out)
        assert rows and not (set(seen) & set(rows))
        seen += rows
        if "'page':" not in out:
            break
        page += 1
        assert page <= 10
    assert len(seen) == 11  # 3 tree members + 8 extras


def test_index_listing_page_beyond_end_says_so(tree: _Tree) -> None:
    out = tree.rt.dispatch(
        "search",
        {
            "kind": "memory",
            "view": "index",
            "__extras__": {"under": _h(tree.hub), "page": 9},
        },
    )
    assert "no memory entries on page 9" in out


def test_cross_kind_search_leaves_todo_and_taxon_own_under_alone(
    tree: _Tree,
) -> None:
    args = {"q": "zebra", "under": "td1", "depth": 2}
    assert tree.rt._resolve_under(args) == (None, None)
    assert args["under"] == "td1" and args["depth"] == 2
    for kind in ("todo", "taxon"):
        a = {"kind": kind, "under": "x", "depth": 1}
        assert tree.rt._resolve_under(a) == (None, None) and a["under"] == "x"
