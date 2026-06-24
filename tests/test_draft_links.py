"""Draft autolinker — materialise the superset of references a draft's
chunks carry as ``related-to`` graph edges (ADR 0033 §8).

Mirrors the note autolinker: ``kind:ref`` mentions, ``¶`` cross-refs, and
``§`` citations resolve to live links; removing a reference drops its
link; intra-draft ``¶`` refs are a within-document concern, not edges.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.utils import handle_registry


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="Proj").id


def _auto_links(hub: Hub, slug: str) -> set[tuple[int, int | None]]:
    ref = hub.store.get_ref(kind="draft", id=slug)
    return {
        (link.dst_ref_id, link.dst_pos)
        for link in hub.store.links_for(ref.id, direction="out", relation="related-to")
        if (link.meta or {}).get("auto") == "mention"
    }


def test_kind_ref_mention_materialises_link(draft: DraftHandler, hub: Hub) -> None:
    target = hub.store.insert_ref(kind="memory", slug=None, title="cited note").id
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = hub.store.reading_order(hub.store.get_ref(kind="draft", id="nt").id)[
        0
    ].handle

    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in memory:{target}, the effect holds",
        at={"after": "¶" + title_h},
    )
    assert (target, None) in _auto_links(hub, "nt")


def test_universal_handle_ref_materialises_link(draft: DraftHandler, hub: Hub) -> None:
    """The simple rule: a ``[[<handle>]]`` is a ref to *something*. A bare
    ``[[me<id>]]`` universal handle resolves via the one decoder and
    materialises a related-to edge — no `kind:`/sigil needed."""
    target = hub.store.insert_ref(kind="memory", slug=None, title="cited note").id
    me_handle = handle_registry.format_handle("memory", target)  # e.g. me42
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = hub.store.reading_order(hub.store.get_ref(kind="draft", id="nt").id)[0].dc
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [[{me_handle}]], the effect holds",
        at={"after": title_h},
    )
    assert (target, None) in _auto_links(hub, "nt")


def test_editing_out_a_mention_drops_its_link(draft: DraftHandler, hub: Hub) -> None:
    a = hub.store.insert_ref(kind="memory", slug=None, title="A").id
    b = hub.store.insert_ref(kind="memory", slug=None, title="B").id
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.store.get_ref(kind="draft", id="nt")
    title_h = hub.store.reading_order(ref.id)[0].handle

    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"see memory:{a} and memory:{b}",
        at={"after": "¶" + title_h},
    )
    para_h = hub.store.reading_order(ref.id)[1].handle
    assert {(a, None), (b, None)} <= _auto_links(hub, "nt")

    # drop the reference to B → its link disappears, A survives
    draft.edit(id=f"¶{para_h}", text=f"see only memory:{a} now")
    links = _auto_links(hub, "nt")
    assert (a, None) in links and (b, None) not in links


def test_xref_to_another_draft_links_at_chunk_level(
    draft: DraftHandler, hub: Hub
) -> None:
    # a second draft whose title chunk we cross-reference by handle
    other_proj = _proj(hub)
    draft.put(id="other", title="Other doc", project=other_proj)
    other_ref = hub.store.get_ref(kind="draft", id="other")
    other_title = hub.store.reading_order(other_ref.id)[0]

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.store.get_ref(kind="draft", id="nt")
    title_h = hub.store.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"compare [the other doc](¶{other_title.handle})",
        at={"after": "¶" + title_h},
    )
    # chunk-level link to the other draft's title chunk (its ord)
    with hub.store.pool.connection() as conn:
        ord_ = conn.execute(
            "SELECT ord FROM chunks WHERE handle = %s", (other_title.handle,)
        ).fetchone()[0]
    assert (other_ref.id, ord_) in _auto_links(hub, "nt")


def test_intra_draft_xref_is_not_an_edge(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.store.get_ref(kind="draft", id="nt")
    title_h = hub.store.reading_order(ref.id)[0].handle
    # a paragraph referencing the draft's OWN title chunk
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"see [the title](¶{title_h})",
        at={"after": "¶" + title_h},
    )
    # no self-referential edge — intra-draft xrefs are document-internal
    assert _auto_links(hub, "nt") == set()
