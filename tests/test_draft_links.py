"""Draft autolinker — materialise the superset of references a draft's
chunks carry as ``related-to`` graph edges.

Mirrors the note autolinker: ``kind:ref`` mentions, ``¶`` cross-refs, and
``§`` citations resolve to live links; removing a reference drops its
link; intra-draft ``¶`` refs are a within-document concern, not edges.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers import _draft_lint
from precis.handlers.draft import DraftHandler
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub, refine_claim_sentence
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


# ── cite-pins-hub-version (docs/backlog/cite-pins-hub-version.md) ─────────


def _mint_hub(hub: Hub, sentence: str) -> int:
    return mint_hub(hub.live_store, CanonicalClaim(sentence=sentence, scope={}))


def test_finding_cite_stamps_the_hubs_current_pub_id(
    draft: DraftHandler, hub: Hub
) -> None:
    """A ``[fi<id>]`` cite to a claim hub records the hub's CURRENT pub_id
    on the ``cites`` edge — the version pin drift detection compares
    against later. The ``auto`` key that other code reads survives."""
    hub_id = _mint_hub(hub, "Graphene has a tensile strength of 130 GPa.")
    pub_id = hub.live_store.current_pub_ids([hub_id])[hub_id]
    fi = handle_registry.format_handle("finding", hub_id)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [{fi}], the effect holds",
        at={"after": "¶" + title_h},
    )

    cites = [
        link
        for link in hub.live_store.links_for(ref.id, direction="out", relation="cites")
        if link.dst_ref_id == hub_id
    ]
    assert len(cites) == 1
    assert cites[0].meta.get("cited_pub_id") == pub_id
    assert cites[0].meta.get("auto") == "mention"


def test_non_finding_cite_gets_no_pub_id_pin(draft: DraftHandler, hub: Hub) -> None:
    """A paper/patent cite is out of scope — its edge meta is unchanged
    (no ``cited_pub_id`` key), exactly as before this feature."""
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
        text=f"the effect holds [{pc}]",
        at={"after": "¶" + title_h},
    )

    cites = [
        link
        for link in hub.live_store.links_for(ref.id, direction="out", relation="cites")
        if link.dst_ref_id == paper.id
    ]
    assert len(cites) == 1
    assert cites[0].meta == {"auto": "mention"}


def test_reword_drifts_the_cite_and_old_pub_id_still_resolves(
    draft: DraftHandler, hub: Hub
) -> None:
    hub_id = _mint_hub(hub, "Graphene has a tensile strength of 130 GPa.")
    old_pub_id = hub.live_store.current_pub_ids([hub_id])[hub_id]
    fi = handle_registry.format_handle("finding", hub_id)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [{fi}], the effect holds",
        at={"after": "¶" + title_h},
    )

    result = refine_claim_sentence(
        hub.live_store,
        hub_id,
        "Nanoindentation measurements show graphene has a tensile strength of 130 GPa.",
    )
    new_pub_id = result["pub_id"]
    assert new_pub_id != old_pub_id

    drifted, unstamped = _draft_lint.find_drifted_cites(hub.live_store, "nt")
    assert unstamped == 0
    assert len(drifted) == 1
    row = drifted[0]
    assert row.hub_ref_id == hub_id
    assert row.stamped_pub_id == old_pub_id
    assert row.current_pub_id == new_pub_id
    assert row.current_title == result["new_title"]

    # The old pub_id keeps resolving as an alias throughout — this item
    # adds visibility, it must never break a cite (decisions log +
    # acceptance criteria).
    assert (
        hub.live_store.find_ref_by_identifier("pub_id", old_pub_id, kind="finding")
        == hub_id
    )


def test_rewriting_the_citing_chunk_clears_drift(draft: DraftHandler, hub: Hub) -> None:
    hub_id = _mint_hub(hub, "Graphene has a tensile strength of 130 GPa.")
    fi = handle_registry.format_handle("finding", hub_id)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [{fi}], the effect holds",
        at={"after": "¶" + title_h},
    )
    para_h = hub.live_store.drafts.reading_order(ref.id)[1].handle

    refine_claim_sentence(
        hub.live_store,
        hub_id,
        "Nanoindentation measurements show graphene has a tensile strength of 130 GPa.",
    )
    drifted, _unstamped = _draft_lint.find_drifted_cites(hub.live_store, "nt")
    assert len(drifted) == 1

    # A rewrite of the citing chunk re-stamps it against the hub's
    # current version, with no hub-side change.
    draft.edit(id=f"¶{para_h}", text=f"as reported in [{fi}], the effect still holds")
    drifted, _unstamped = _draft_lint.find_drifted_cites(hub.live_store, "nt")
    assert drifted == []


def test_unstamped_cite_is_not_reported_as_drift(draft: DraftHandler, hub: Hub) -> None:
    """A ``cites`` edge with no ``cited_pub_id`` (every edge written
    before this feature existed) is UNKNOWN, not drifted — counted
    separately."""
    hub_id = _mint_hub(hub, "Graphene has a tensile strength of 130 GPa.")

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    # Hand-write a legacy cites edge, bypassing sync_draft_links — the
    # shape every edge had before this stamp existed.
    hub.live_store.add_link(
        src_ref_id=ref.id,
        dst_ref_id=hub_id,
        relation="cites",
        src_pos=0,
        set_by="agent",
        meta={"auto": "mention"},
    )

    drifted, unstamped = _draft_lint.find_drifted_cites(hub.live_store, "nt")
    assert drifted == []
    assert unstamped == 1


def test_finding_cite_stamps_the_hubs_title_alongside_pub_id(
    draft: DraftHandler, hub: Hub
) -> None:
    """The version pin carries the hub's TITLE (``cited_title``) alongside
    its ``cited_pub_id`` — the pub_id is a one-way content hash with no
    reverse lookup to the sentence, so the drift report needs the title
    pinned separately to quote 'what it used to say' (task 1)."""
    sentence = "Graphene has a tensile strength of 130 GPa."
    hub_id = _mint_hub(hub, sentence)
    fi = handle_registry.format_handle("finding", hub_id)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [{fi}], the effect holds",
        at={"after": "¶" + title_h},
    )

    cites = [
        link
        for link in hub.live_store.links_for(ref.id, direction="out", relation="cites")
        if link.dst_ref_id == hub_id
    ]
    assert len(cites) == 1
    assert cites[0].meta.get("cited_title") == sentence


def test_title_pin_advances_only_when_pub_id_advances(
    draft: DraftHandler, hub: Hub
) -> None:
    """``cited_title`` rides the SAME scoping gate as ``cited_pub_id``: a
    reword alone (no write to the citing chunk) advances neither; a write
    to an unrelated chunk advances neither; only a rewrite of the CITING
    chunk advances both together. If the pair could go inconsistent, the
    'was/now' message would lie about what the old sentence said."""
    old_sentence = "Graphene has a tensile strength of 130 GPa."
    hub_id = _mint_hub(hub, old_sentence)
    fi = handle_registry.format_handle("finding", hub_id)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [{fi}], the effect holds",
        at={"after": "¶" + title_h},
    )
    para_h = hub.live_store.drafts.reading_order(ref.id)[1].handle

    def cite_meta() -> dict:
        cites = [
            link
            for link in hub.live_store.links_for(
                ref.id, direction="out", relation="cites"
            )
            if link.dst_ref_id == hub_id
        ]
        assert len(cites) == 1
        return cites[0].meta

    result = refine_claim_sentence(
        hub.live_store,
        hub_id,
        "Nanoindentation measurements show graphene has a tensile strength of 130 GPa.",
    )

    # Reword alone (no write to the citing chunk) advances neither half.
    assert cite_meta().get("cited_title") == old_sentence
    assert cite_meta().get("cited_pub_id") != result["pub_id"]

    # A write to a DIFFERENT chunk must not advance either half either.
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="an unrelated paragraph citing nothing at all",
        at={"after": "¶" + para_h},
    )
    assert cite_meta().get("cited_title") == old_sentence
    assert cite_meta().get("cited_pub_id") != result["pub_id"]

    # Rewriting the CITING chunk re-stamps both halves together.
    draft.edit(id=f"¶{para_h}", text=f"as reported in [{fi}], the effect still holds")
    assert cite_meta().get("cited_title") == result["new_title"]
    assert cite_meta().get("cited_pub_id") == result["pub_id"]


def test_drift_carries_the_stamped_title_for_the_was_now_message(
    draft: DraftHandler, hub: Hub
) -> None:
    """``DriftedCite.stamped_title`` recovers the exact sentence the
    citing prose was written against — the acceptance criterion this item
    exists for (a flag that can't say what the claim used to say costs the
    reader a second pass)."""
    old_sentence = "Graphene has a tensile strength of 130 GPa."
    hub_id = _mint_hub(hub, old_sentence)
    fi = handle_registry.format_handle("finding", hub_id)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [{fi}], the effect holds",
        at={"after": "¶" + title_h},
    )

    result = refine_claim_sentence(
        hub.live_store,
        hub_id,
        "Nanoindentation measurements show graphene has a tensile strength of 130 GPa.",
    )

    drifted, _unstamped = _draft_lint.find_drifted_cites(hub.live_store, "nt")
    assert len(drifted) == 1
    assert drifted[0].stamped_title == old_sentence
    assert drifted[0].current_title == result["new_title"]


def test_drift_stamped_title_none_for_a_legacy_pub_id_only_edge(
    draft: DraftHandler, hub: Hub
) -> None:
    """An edge stamped before the title pin existed carries a
    ``cited_pub_id`` but no ``cited_title`` — ``stamped_title`` must
    degrade to ``None``, not crash and not claim the old title was
    empty."""
    hub_id = _mint_hub(hub, "Graphene has a tensile strength of 130 GPa.")
    old_pub_id = hub.live_store.current_pub_ids([hub_id])[hub_id]

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    # Hand-write a legacy cites edge carrying the OLD-shape stamp: a
    # pub_id but no title — the shape every edge had before task 1.
    hub.live_store.add_link(
        src_ref_id=ref.id,
        dst_ref_id=hub_id,
        relation="cites",
        src_pos=0,
        set_by="agent",
        meta={"auto": "mention", "cited_pub_id": old_pub_id},
    )

    refine_claim_sentence(
        hub.live_store,
        hub_id,
        "Nanoindentation measurements show graphene has a tensile strength of 130 GPa.",
    )

    drifted, unstamped = _draft_lint.find_drifted_cites(hub.live_store, "nt")
    assert unstamped == 0
    assert len(drifted) == 1
    assert drifted[0].stamped_title is None
    assert drifted[0].stamped_pub_id == old_pub_id


def test_unrelated_chunk_write_does_not_clear_drift(draft: DraftHandler, hub: Hub) -> None:
    """ADVERSARIAL: drift means *this passage's prose* was written against
    an older hub sentence. A write to a DIFFERENT chunk must not advance
    this chunk's stamp — doing so silently erases the drift signal while
    the stale paraphrase stays on the page."""
    hub_id = _mint_hub(hub, "Graphene has a tensile strength of 130 GPa.")
    fi = handle_registry.format_handle("finding", hub_id)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [{fi}], the effect holds",
        at={"after": "\u00b6" + title_h},
    )
    cite_para = hub.live_store.drafts.reading_order(ref.id)[1].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="an unrelated paragraph citing nothing at all",
        at={"after": "\u00b6" + cite_para},
    )
    other_para = hub.live_store.drafts.reading_order(ref.id)[2].handle

    refine_claim_sentence(
        hub.live_store,
        hub_id,
        "Nanoindentation measurements show graphene has a tensile strength of 130 GPa.",
    )
    drifted, _ = _draft_lint.find_drifted_cites(hub.live_store, "nt")
    assert len(drifted) == 1, "reword should drift the cite"

    # The citing paragraph is untouched; only an unrelated one is edited.
    draft.edit(id=f"\u00b6{other_para}", text="an unrelated paragraph, now reworded")

    drifted, _ = _draft_lint.find_drifted_cites(hub.live_store, "nt")
    assert len(drifted) == 1, (
        "drift was silently cleared by a write to an unrelated chunk — "
        "the stale paraphrase is still on the page"
    )
