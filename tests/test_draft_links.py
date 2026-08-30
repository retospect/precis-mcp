"""Draft autolinker — materialise the superset of references a draft's
chunks carry as ``related-to`` graph edges.

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
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _auto_links(hub: Hub, slug: str) -> set[tuple[int, int | None]]:
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return {
        (link.dst_ref_id, link.dst_ord)
        for link in hub.live_store.links_for(
            ref.id, direction="out", relation="related-to"
        )
        if (link.meta or {}).get("auto") == "mention"
    }


def test_kind_ref_mention_materialises_link(draft: DraftHandler, hub: Hub) -> None:
    target = hub.live_store.insert_ref(kind="memory", slug=None, title="cited note").id
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle

    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in memory:{target}, the effect holds",
        at={"after": "¶" + title_h},
    )
    assert (target, None) in _auto_links(hub, "nt")


def test_universal_handle_ref_materialises_link(draft: DraftHandler, hub: Hub) -> None:
    """The simple rule: a ``[<handle>]`` is a ref to *something*. A bare
    ``[me<id>]`` universal handle resolves via the one decoder and
    materialises a related-to edge — no `kind:`/sigil needed."""
    target = hub.live_store.insert_ref(kind="memory", slug=None, title="cited note").id
    me_handle = handle_registry.format_handle("memory", target)  # e.g. me42
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].dc
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [{me_handle}], the effect holds",
        at={"after": title_h},
    )
    assert (target, None) in _auto_links(hub, "nt")


def test_editing_out_a_mention_drops_its_link(draft: DraftHandler, hub: Hub) -> None:
    a = hub.live_store.insert_ref(kind="memory", slug=None, title="A").id
    b = hub.live_store.insert_ref(kind="memory", slug=None, title="B").id
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle

    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"see memory:{a} and memory:{b}",
        at={"after": "¶" + title_h},
    )
    para_h = hub.live_store.drafts.reading_order(ref.id)[1].handle
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
    other_ref = hub.live_store.get_ref(kind="draft", id="other")
    assert other_ref is not None
    other_title = hub.live_store.drafts.reading_order(other_ref.id)[0]

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"compare [the other doc](¶{other_title.handle})",
        at={"after": "¶" + title_h},
    )
    # chunk-level link to the other draft's title chunk (its ord)
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ord FROM chunks WHERE handle = %s", (other_title.handle,)
        ).fetchone()
    assert row is not None
    ord_ = row[0]
    assert (other_ref.id, ord_) in _auto_links(hub, "nt")


def _cite_links(hub: Hub, slug: str) -> set[tuple[int, int | None]]:
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return {
        (link.dst_ref_id, link.dst_ord)
        for link in hub.live_store.links_for(ref.id, direction="out", relation="cites")
        if (link.meta or {}).get("auto") == "mention"
    }


def test_paper_chunk_ref_is_a_cites_edge_not_related_to(
    draft: DraftHandler, hub: Hub
) -> None:
    """A reference to a paper chunk by handle (``[pc<id>]``) is a
    CITATION — it materialises a ``cites`` edge, not ``related-to``. A
    memory reference in the same draft stays ``related-to`` (citations
    are to the literature; links are to our own notes)."""
    from precis.store.types import ChunkInsert

    paper = hub.live_store.insert_ref(kind="paper", slug="miller23", title="Paper")
    hub.live_store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text="We measured 12% FE.", meta={})]
    )
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
            (paper.id,),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
    pc = handle_registry.format_handle("paper", chunk_id, chunk=True)  # pc<id>
    mem = hub.live_store.insert_ref(kind="memory", slug=None, title="note").id
    me = handle_registry.format_handle("memory", mem)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"the effect holds [{pc}], as we noted [{me}]",
        at={"after": "¶" + title_h},
    )

    cited = {dst for dst, _pos in _cite_links(hub, "nt")}
    related = {dst for dst, _pos in _auto_links(hub, "nt")}
    assert paper.id in cited  # paper chunk → cites
    assert mem in related  # memory → related-to
    assert paper.id not in related  # the paper is NOT a provenance link


def test_cites_edge_grounds_at_source_draft_chunk(
    draft: DraftHandler, hub: Hub
) -> None:
    """A ``[pc<id>]`` citation records WHICH draft chunk cites (the edge's
    ``src`` is that ``dc<id>`` chunk), not just the draft as a whole — so a
    reader / the citation tree can resolve back to the originating
    paragraph. Before grounding, every draft cite landed ref-level
    (``src_pos is None``)."""
    from precis.store.types import ChunkInsert

    paper = hub.live_store.insert_ref(kind="paper", slug="wu2022a", title="Paper")
    hub.live_store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text="Rotaxane nanomachines.", meta={})]
    )
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
            (paper.id,),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
    pc = handle_registry.format_handle("paper", chunk_id, chunk=True)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"the effect holds [{pc}]",
        at={"after": "¶" + title_h},
    )

    # the citing paragraph is the source chunk of the cites edge
    para = hub.live_store.drafts.reading_order(ref.id)[1]
    para_ord = hub.live_store.drafts.chunk_ord_map(ref.id)[para.chunk_id]
    cites = [
        link
        for link in hub.live_store.links_for(ref.id, direction="out", relation="cites")
        if (link.meta or {}).get("auto") == "mention"
    ]
    assert len(cites) == 1
    assert cites[0].src_ord == para_ord  # grounded at the paragraph…
    assert cites[0].src_chunk_id == para.chunk_id  # …not ref-level (None)


def test_same_ref_cited_from_two_chunks_is_two_edges(
    draft: DraftHandler, hub: Hub
) -> None:
    """Two different paragraphs each citing the same paper chunk yield two
    distinct chunk-grounded edges (one per citing paragraph), not one
    collapsed ref-level edge — each passage keeps its own provenance."""
    from precis.store.types import ChunkInsert

    paper = hub.live_store.insert_ref(kind="paper", slug="miller23", title="Paper")
    hub.live_store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text="We measured 12% FE.", meta={})]
    )
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
            (paper.id,),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
    pc = handle_registry.format_handle("paper", chunk_id, chunk=True)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"first mention [{pc}]",
        at={"after": "¶" + title_h},
    )
    para1_h = hub.live_store.drafts.reading_order(ref.id)[1].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"second mention [{pc}]",
        at={"after": "¶" + para1_h},
    )

    cites = [
        link
        for link in hub.live_store.links_for(ref.id, direction="out", relation="cites")
        if (link.meta or {}).get("auto") == "mention" and link.dst_ref_id == paper.id
    ]
    assert len(cites) == 2  # one edge per citing paragraph
    assert len({link.src_chunk_id for link in cites}) == 2  # distinct sources


def test_intra_draft_xref_is_not_an_edge(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    # a paragraph referencing the draft's OWN title chunk
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"see [the title](¶{title_h})",
        at={"after": "¶" + title_h},
    )
    # no self-referential edge — intra-draft xrefs are document-internal
    assert _auto_links(hub, "nt") == set()
