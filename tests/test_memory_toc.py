"""``view='toc'`` — the generated memory TOCs (gr631890).

``search(kind='memory', tags=[...], view='toc')`` renders a tag's
*forest*: live nodes carrying the tag whose own ``part-of`` parents do
not (the tips of the trees), one line each with a keyword/hook
descriptor and a member count. ``tags=['spec:*']`` prefix-matches the
slug, so one call lists every spec root wherever it hangs — the
retired write-time audit (gr631564) is replaced by this listing.

``get(kind='memory', id=N, view='toc')`` renders the node's part-of
subtree as an indented tree: children order by ``meta.order``, then
the child's first handle-mention position in the parent body, then
title; ``depth=`` collapses below a level with subtree counts and
``offset=`` pages the DFS rows.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from tests.conftest import id_of


def _put(
    handler: MemoryHandler,
    title: str,
    *,
    tags: tuple[str, ...] = (),
    meta: dict | None = None,
    text: str | None = None,
    link: int | None = None,
) -> int:
    return id_of(
        handler.put(
            text=text or f"{title} body.",
            title=title,
            tags=list(tags) or None,
            meta=meta,
            link=f"memory:{link}" if link else None,
            rel="part-of" if link else None,
        ).body
    )


@pytest.fixture
def forest(store: Store, hub: Hub) -> dict[str, int]:
    """Two spec roots under different parents plus a third for paging.

    ``spec:routing`` hangs under a programme node, ``spec:other`` and
    ``spec:alpha`` under a topical hub — the tag forest's roots are the
    tagged nodes whose parents don't carry the tag, wherever they sit.
    """
    h = MemoryHandler(hub=hub)
    repo = _put(h, "Repo root")
    prog = _put(h, "Programme", link=repo)
    topical = _put(h, "Topical hub", link=repo)
    spec_r = _put(
        h,
        "Spec: routing",
        tags=("spec:routing", "section:index"),
        meta={"hook": "how routes resolve"},
        link=prog,
    )
    spec_o = _put(h, "Spec: other", tags=("spec:other", "section:index"), link=topical)
    spec_a = _put(h, "Spec: alpha", tags=("spec:alpha", "section:index"), link=topical)
    s1 = _put(h, "Route table", tags=("spec:routing",), meta={"order": 1}, link=spec_r)
    s2 = _put(h, "Resolver", tags=("spec:routing",), meta={"order": 2}, link=spec_r)
    note = _put(h, "Route note", link=s1)
    _put(h, "Other section", tags=("spec:other",), link=spec_o)
    return {
        "repo": repo,
        "prog": prog,
        "topical": topical,
        "spec_r": spec_r,
        "spec_o": spec_o,
        "spec_a": spec_a,
        "s1": s1,
        "s2": s2,
        "note": note,
    }


# ---------------------------------------------------------------------------
# search(view='toc') — the tag forest
# ---------------------------------------------------------------------------


def test_tag_toc_lists_every_spec_root_wherever_it_hangs(
    hub: Hub, forest: dict[str, int]
) -> None:
    """The gr631890 acceptance: one call lists every spec root across
    the graph — ``spec:routing`` under its programme node, the others
    under a topical hub — with a descriptor and member count each."""
    out = MemoryHandler(hub=hub).search(tags=["spec:*"], view="toc").body
    assert f"Spec: routing (me{forest['spec_r']})" in out
    assert f"Spec: other (me{forest['spec_o']})" in out
    assert f"Spec: alpha (me{forest['spec_a']})" in out
    # Sections carry the tag too but their part-of parent does — not
    # roots, and the untagged note never matches at all.
    assert "Route table" not in out
    assert "Other section" not in out
    # Member counts are the live subtree sizes view='members' reports.
    assert "· 3 members" in out  # spec:routing → s1, s2, note
    assert "· 1 member" in out  # spec:other → its section
    assert "· 0 members" in out  # spec:alpha is childless
    # The hook is the first-class descriptor while extraction is queued.
    assert "— how routes resolve" in out


def test_tag_toc_prefix_glob_vs_exact_tag(hub: Hub, forest: dict[str, int]) -> None:
    h = MemoryHandler(hub=hub)
    exact = h.search(tags=["spec:routing"], view="toc").body
    assert "Spec: routing" in exact
    assert "Spec: other" not in exact
    # A bare `*` glob over a different axis never matches spec tags.
    none = h.search(tags=["spec:bogus"], view="toc").body
    assert "start a tree" in none


def test_tag_toc_negated_entry_filters(hub: Hub, forest: dict[str, int]) -> None:
    out = MemoryHandler(hub=hub).search(tags=["spec:*", "!spec:other"], view="toc").body
    assert "Spec: routing" in out
    assert "Spec: alpha" in out
    assert "Spec: other" not in out


def test_tag_toc_scope_narrows_the_forest(hub: Hub, forest: dict[str, int]) -> None:
    """``args={'under': …}`` resolves to ``include_ref_ids`` upstream of
    the view — here the scope keeps one subtree's tagged nodes only."""
    h = MemoryHandler(hub=hub)
    out = h.search(
        tags=["spec:*"],
        view="toc",
        include_ref_ids=[forest["topical"], forest["spec_o"], forest["spec_a"]],
    ).body
    assert "Spec: other" in out
    assert "Spec: alpha" in out
    assert "Spec: routing" not in out


def test_tag_toc_counts_members_past_non_memory_intermediates(
    store: Store, hub: Hub
) -> None:
    """The forest member count is the ``view='members'`` subtree size:
    the recursive walk transmits non-memory intermediates without
    counting them, so a memory leaf under a ``todo`` bridge counts —
    matching the node TOC, which re-attaches the leaf to its nearest
    live memory ancestor (td633593 review of td633592)."""
    h = MemoryHandler(hub=hub)
    root = _put(h, "Spec: bridged", tags=("spec:bridged",))
    bridge = store.insert_ref(kind="todo", slug=None, title="bridge", meta={})
    leaf = _put(h, "Bridged leaf")
    store.add_link(src_ref_id=bridge.id, dst_ref_id=root, relation="part-of")
    store.add_link(src_ref_id=leaf, dst_ref_id=bridge.id, relation="part-of")
    out = h.search(tags=["spec:bridged"], view="toc").body
    assert "· 1 member" in out
    node = h.get(id=root, view="toc").body
    assert "· 1 in subtree" in node


def test_tag_toc_pages_by_page_size(hub: Hub, forest: dict[str, int]) -> None:
    h = MemoryHandler(hub=hub)
    first = h.search(tags=["spec:*"], view="toc", page_size=2).body
    assert "Spec: alpha" in first
    assert "Spec: other" in first
    assert "Spec: routing" not in first
    assert "… 1 more — next: search(kind='memory'" in first
    second = h.search(tags=["spec:*"], view="toc", page_size=2, page=2).body
    assert "Spec: routing" in second
    assert "Spec: alpha" not in second


def test_tag_toc_requires_a_positive_tag(hub: Hub) -> None:
    h = MemoryHandler(hub=hub)
    with pytest.raises(BadInput, match="positive tags= entry"):
        h.search(view="toc")
    with pytest.raises(BadInput, match="positive tags= entry"):
        h.search(tags=["!spec:other"], view="toc")


def test_tag_toc_q_filters_roots(hub: Hub, forest: dict[str, int]) -> None:
    """gr702042: ``q=`` with view='toc' now filters the forest instead of
    BadInput. The filter is root-level — a root stays only when its own
    chunks match; member counts stay full subtree sizes."""
    h = MemoryHandler(hub=hub)
    out = h.search(q="routing", tags=["spec:*"], view="toc").body
    assert "Spec: routing (me" in out
    assert "Spec: other" not in out
    assert "Spec: alpha" not in out
    # The count is the untouched subtree size (s1, s2, note).
    assert "· 3 members" in out
    # The header and empty page name the q filter.
    assert "matching 'routing'" in out
    none = h.search(q="zzz-not-a-topic", tags=["spec:*"], view="toc").body
    assert "no memory entries tagged ['spec:*'] match 'zzz-not-a-topic'" in none


def test_tag_toc_q_does_not_let_members_qualify_a_root(
    hub: Hub, forest: dict[str, int]
) -> None:
    """``q=`` matches the root's own chunks, not its subtree's: 'table'
    lives only in the Route table section under Spec: routing, so no
    root qualifies."""
    out = MemoryHandler(hub=hub).search(q="table", tags=["spec:*"], view="toc").body
    assert "Spec: routing" not in out
    assert "match 'table'" in out


def test_tag_toc_q_echoes_in_the_next_call(hub: Hub, forest: dict[str, int]) -> None:
    first = (
        MemoryHandler(hub=hub)
        .search(q="spec", tags=["spec:*"], view="toc", page_size=1)
        .body
    )
    assert "'q': 'spec'" in first


def test_tag_toc_rejects_unapplied_facets(hub: Hub) -> None:
    h = MemoryHandler(hub=hub)
    with pytest.raises(BadInput, match="sort= is not applied"):
        h.search(tags=["spec:*"], view="toc", sort="recent")


# ---------------------------------------------------------------------------
# get(view='toc') — the node's part-of tree
# ---------------------------------------------------------------------------


def test_node_toc_renders_the_indented_tree(hub: Hub, forest: dict[str, int]) -> None:
    out = MemoryHandler(hub=hub).get(id=forest["spec_r"], view="toc").body
    lines = out.splitlines()
    assert lines[0].startswith(f"# memory me{forest['spec_r']} — TOC · 3 in subtree")
    assert lines[1].startswith(f"- Spec: routing (me{forest['spec_r']})")
    # meta.order puts Route table (1) before Resolver (2); the note
    # nests under Route table at depth 2.
    assert lines[2].startswith(f"  - Route table (me{forest['s1']})")
    assert lines[3].startswith(f"    - Route note (me{forest['note']})")
    assert lines[4].startswith(f"  - Resolver (me{forest['s2']})")


def test_node_toc_depth_collapses_with_counts(hub: Hub, forest: dict[str, int]) -> None:
    out = MemoryHandler(hub=hub).get(id=forest["spec_r"], view="toc", depth=1).body
    assert "depth ≤ 1" in out
    assert f"  - Route table (me{forest['s1']})" in out
    assert "· +1 below" in out  # the note under Route table
    assert "Route note" not in out
    assert f"  - Resolver (me{forest['s2']})" in out


def test_node_toc_offset_pages_the_dfs_rows(hub: Hub, forest: dict[str, int]) -> None:
    out = MemoryHandler(hub=hub).get(id=forest["spec_r"], view="toc", offset=2).body
    assert "· 3 in subtree" in out
    assert "Route table" not in out
    assert "Route note" not in out
    assert f"  - Resolver (me{forest['s2']})" in out


def test_node_toc_reading_order_from_body_mentions(store: Store, hub: Hub) -> None:
    """No ``meta.order`` and no hand-maintained rank: the parent body's
    handle-mention order is the reading order (gr631890's middle
    fallback — a 'Sections in reading order' list just works)."""
    h = MemoryHandler(hub=hub)
    beta = _put(h, "Beta section")
    alpha = _put(h, "Alpha section")
    parent = _put(h, "Parent", text=f"Outline: me{beta} first, then me{alpha}.")
    h.link(id=beta, target=f"memory:{parent}", rel="part-of")
    h.link(id=alpha, target=f"memory:{parent}", rel="part-of")
    out = h.get(id=parent, view="toc").body
    assert out.index("Beta section") < out.index("Alpha section")


def test_node_toc_meta_order_outranks_a_mention_at_offset_zero(
    store: Store, hub: Hub
) -> None:
    """``meta.order`` is its own precedence tier: a child ranked 1
    precedes an unranked sibling even when the sibling's handle sits
    at body offset 0 — order values and character offsets are
    different units, not one number line (td633593 review of
    td633592)."""
    h = MemoryHandler(hub=hub)
    first = _put(h, "Mentioned first")
    ranked = _put(h, "Ranked one", meta={"order": 1})
    parent = _put(h, "Parent", text=f"me{first} leads, me{ranked} trails.")
    h.link(id=first, target=f"memory:{parent}", rel="part-of")
    h.link(id=ranked, target=f"memory:{parent}", rel="part-of")
    out = h.get(id=parent, view="toc").body
    assert out.index("Ranked one") < out.index("Mentioned first")


def test_node_toc_orders_by_title_without_rank_or_mention(
    store: Store, hub: Hub
) -> None:
    h = MemoryHandler(hub=hub)
    parent = _put(h, "Parent")
    z = _put(h, "Zebra", link=parent)
    a = _put(h, "Apple", link=parent)
    out = h.get(id=parent, view="toc").body
    assert out.index("Apple") < out.index("Zebra")


def test_node_toc_non_memory_intermediate_reattaches(store: Store, hub: Hub) -> None:
    """A live non-memory node between memories is skipped: the leaf
    re-attaches to its nearest live memory ancestor and the footer
    counts the unlisted member (a retired intermediate is different —
    its edges die with it, orphaning the child exactly like
    view='members')."""
    h = MemoryHandler(hub=hub)
    root = _put(h, "Root")
    bridge = store.insert_ref(kind="todo", slug=None, title="bridge", meta={})
    leaf = _put(h, "Surviving leaf")
    store.add_link(src_ref_id=bridge.id, dst_ref_id=root, relation="part-of")
    store.add_link(src_ref_id=leaf, dst_ref_id=bridge.id, relation="part-of")
    out = h.get(id=root, view="toc").body
    assert f"  - Surviving leaf (me{leaf})" in out
    assert "bridge" not in out
    assert "1 linked non-memory member not listed" in out


def test_node_toc_rejects_bad_args(hub: Hub, forest: dict[str, int]) -> None:
    h = MemoryHandler(hub=hub)
    with pytest.raises(BadInput, match="depth= must be an integer"):
        h.get(id=forest["spec_r"], view="toc", depth="x")
    with pytest.raises(BadInput, match="offset= must be an integer"):
        h.get(id=forest["spec_r"], view="toc", offset=-1)


def test_node_toc_past_the_end_names_the_total(
    hub: Hub, forest: dict[str, int]
) -> None:
    out = MemoryHandler(hub=hub).get(id=forest["spec_r"], view="toc", offset=9).body
    assert "no members past offset 9 — 3 total" in out


# ---------------------------------------------------------------------------
# get(id='/toc') — the kind-level TOC (gr568237)
# ---------------------------------------------------------------------------


def test_kind_toc_lists_every_root(hub: Hub, forest: dict[str, int]) -> None:
    """``id='/toc'`` is the whole-graph forest: the one parentless node
    heads the listing and carries the subtree's member count."""
    out = MemoryHandler(hub=hub).get(id="/toc").body
    assert "# memory TOC — 1 root" in out
    assert f"- Repo root (me{forest['repo']})" in out
    assert "· 9 members" in out


def test_kind_toc_bare_view_is_the_same_door(hub: Hub, forest: dict[str, int]) -> None:
    """A bare ``get(view='toc')`` (no id) reads as ``id='/toc'`` — the
    spelling agents reaching for "a toc for memories" (gr568237) type."""
    out = MemoryHandler(hub=hub).get(view="toc").body
    assert "# memory TOC — 1 root" in out
    assert f"- Repo root (me{forest['repo']})" in out


def test_kind_toc_ignores_tagged_parents(hub: Hub, forest: dict[str, int]) -> None:
    """Roots are structural, not tag-defined: the untagged repo root
    is the only root even though tagged spec nodes hang under it."""
    out = MemoryHandler(hub=hub).get(id="/toc").body
    assert "Spec: routing" not in out
    assert "Spec: other" not in out


def test_kind_toc_pages_by_page_size(hub: Hub) -> None:
    h = MemoryHandler(hub=hub)
    _put(h, "Zeta root")
    _put(h, "Alpha root")
    _put(h, "Mid root")
    first = h.get(id="/toc", page_size=2).body
    assert "Alpha root" in first
    assert "Mid root" in first
    assert "Zeta root" not in first
    assert "… 1 more — next: get(kind='memory', id='/toc'" in first
    second = h.get(id="/toc", page_size=2, page=2).body
    assert "Zeta root" in second
    assert "Alpha root" not in second


def test_kind_toc_rejects_sort(hub: Hub) -> None:
    h = MemoryHandler(hub=hub)
    _put(h, "Solo root")
    with pytest.raises(BadInput, match="sort= is not applied"):
        h.get(id="/toc", sort="recent")


def test_kind_toc_refuses_view_arg_on_the_list_id(hub: Hub) -> None:
    """``id='/toc'`` + ``view='toc'`` — the list selectors live in id=;
    the refusal names the working spelling."""
    h = MemoryHandler(hub=hub)
    _put(h, "Solo root")
    with pytest.raises(Unsupported, match="takes no view"):
        h.get(id="/toc", view="toc")


def test_kind_toc_bare_view_refuses_node_args(hub: Hub) -> None:
    """``depth=``/``offset=`` belong to a node's own subtree toc, not
    the paged kind forest — say which call they do bind."""
    h = MemoryHandler(hub=hub)
    _put(h, "Solo root")
    with pytest.raises(BadInput, match="depth=/offset="):
        h.get(view="toc", depth=1)
    with pytest.raises(BadInput, match="depth=/offset="):
        h.get(view="toc", offset=2)
