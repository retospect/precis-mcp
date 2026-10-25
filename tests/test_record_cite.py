"""Whole-record cite (gr514384): the ``record_type`` vocabulary, the
``put(record_type=)`` declare door, the ``cites`` edge stamp, and the
lint/hygiene exemption — all through the one live predicate in
``precis.utils.record_cite`` (typed software/dataset AND zero body
chunks). DB-backed (``store``/``hub`` fixtures); the registry side is
offline-tested in ``test_doi_registry.py``.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.handlers.paper import PaperHandler
from precis.store.store import Store
from precis.utils.record_cite import (
    is_record_cite,
    record_cite_handles,
    record_type_of,
)
from tests.workers._helpers import seed_chunk, seed_ref


@pytest.fixture
def papers(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _type(store: Store, ref_id: int, rt: str) -> None:
    store.update_paper_fields(ref_id, meta_patch={"record_type": rt}, source="mcp-edit")


def _proj(store: Store) -> int:
    return store.insert_ref(kind="todo", slug=None, title="Proj").id


def _order(store: Store, slug: str) -> list:
    ref = store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return store.drafts.reading_order(ref.id)


def _add_chunk(draft: DraftHandler, store: Store, slug: str, text: str):
    title_handle = _order(store, slug)[0].handle
    return draft.put(
        id=slug,
        chunk_kind="paragraph",
        text=text,
        at={"after": "¶" + title_handle},
    )


# ── predicate truth table ─────────────────────────────────────────────


def test_record_type_of_vocabulary() -> None:
    assert record_type_of({"record_type": "software"}) == "software"
    assert record_type_of({"record_type": " Dataset "}) == "dataset"
    # Out-of-vocabulary / absent / malformed all read back untyped.
    assert record_type_of({"record_type": "record"}) == ""
    assert record_type_of({"record_type": "article"}) == ""
    assert record_type_of({"record_type": 7}) == ""
    assert record_type_of({}) == ""
    assert record_type_of(None) == ""


def test_is_record_cite_live_predicate(store: Store) -> None:
    """Typed + zero body chunks → record; untyped, or a record that has
    since gained a body, → not (the predicate is live state, never a
    cached flag)."""
    rid = seed_ref(store, title="Zenodo release", kind="paper")
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert not is_record_cite(store, ref)  # untyped

    _type(store, rid, "software")
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert is_record_cite(store, ref)  # typed, no passages

    seed_chunk(store, ref_id=rid, text="A README body landed.")
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert not is_record_cite(store, ref)  # gained a body — back to paper rules


def test_record_cite_handles_batched(store: Store) -> None:
    rec = seed_ref(store, title="SW record", kind="paper")
    plain = seed_ref(store, title="plain stub", kind="paper")
    chunked = seed_ref(store, title="typed but bodied", kind="paper")
    _type(store, rec, "dataset")
    _type(store, chunked, "software")
    seed_chunk(store, ref_id=chunked, text="passage")

    got = record_cite_handles(
        store, [f"pa{rec}", f"pa{plain}", f"pa{chunked}", "fi1", "pc2", "pa999999"]
    )
    assert got == {f"pa{rec}"}
    # Non-paper handles and nothing-to-check input stay cheap no-ops.
    assert record_cite_handles(store, ["fi1", "dc3"]) == set()
    assert record_cite_handles(store, []) == set()


# ── the declare door ──────────────────────────────────────────────────


def test_put_record_type_declares_chunkless(papers: PaperHandler, store: Store) -> None:
    rid = seed_ref(store, title="precis-mcp release", kind="paper")
    r = papers.put(id=f"pa{rid}", record_type="software")
    assert "record_type='software'" in r.body
    assert f"[pa{rid}]" in r.body
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert record_type_of(ref.meta) == "software"


def test_put_record_type_idempotent(papers: PaperHandler, store: Store) -> None:
    rid = seed_ref(store, title="SW", kind="paper")
    papers.put(id=f"pa{rid}", record_type="software")
    r = papers.put(id=f"pa{rid}", record_type="software")
    assert "already typed" in r.body


def test_put_record_type_corrects_wrong_value(
    papers: PaperHandler, store: Store
) -> None:
    rid = seed_ref(store, title="mistyped", kind="paper")
    _type(store, rid, "dataset")
    papers.put(id=f"pa{rid}", record_type="software")
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert record_type_of(ref.meta) == "software"


def test_put_record_type_refuses_out_of_vocab(
    papers: PaperHandler, store: Store
) -> None:
    rid = seed_ref(store, title="SW", kind="paper")
    with pytest.raises(BadInput, match="software.*dataset"):
        papers.put(id=f"pa{rid}", record_type="record")


def test_put_record_type_refuses_paper_with_passages(
    papers: PaperHandler, store: Store
) -> None:
    """The guard that keeps papers on the passage rules: a body chunk
    means a ``[pc<id>]`` exists to drill to, so ``record_type=`` refuses
    and names the finding-hub route."""
    rid = seed_ref(store, title="real paper", kind="paper")
    seed_chunk(store, ref_id=rid, text="passage one")
    with pytest.raises(BadInput, match=r"\[fi<id>\]"):
        papers.put(id=f"pa{rid}", record_type="software")


def test_put_record_type_refuses_mint_combos(
    papers: PaperHandler, store: Store
) -> None:
    rid = seed_ref(store, title="SW", kind="paper")
    with pytest.raises(BadInput, match="does not combine"):
        papers.put(id=f"pa{rid}", record_type="software", doi="10.1/x")


def test_put_record_type_refuses_supporters(papers: PaperHandler, store: Store) -> None:
    rid = seed_ref(store, title="SW", kind="paper")
    with pytest.raises(BadInput, match="supporters"):
        papers.put(
            id=f"pa{rid}",
            record_type="software",
            supporters=[{"paper": rid}],
        )


def test_put_record_type_requires_id(papers: PaperHandler) -> None:
    with pytest.raises(BadInput, match="id="):
        papers.put(record_type="software")


def test_search_record_type_scope_and_refusal(
    papers: PaperHandler, store: Store
) -> None:
    """``args={'record_type': 'software'}`` restricts to typed refs —
    SQL-level via the include channel; an out-of-vocabulary value is
    refused."""
    rec = seed_ref(store, title="SW record", kind="paper")
    seed_ref(store, title="plain", kind="paper")
    _type(store, rec, "software")
    assert papers._resolve_record_type_scope("software") == {rec}
    assert papers._resolve_record_type_scope("dataset") == set()
    with pytest.raises(BadInput, match="software.*dataset"):
        papers.search(q="precis", record_type="record")


# ── the cites edge stamp ──────────────────────────────────────────────


def test_sync_stamps_record_edge(draft: DraftHandler, store: Store) -> None:
    rid = seed_ref(store, title="precis-mcp", kind="paper")
    _type(store, rid, "dataset")
    draft.put(id="nt", title="T", project=_proj(store))
    _add_chunk(draft, store, "nt", f"We used the tool [pa{rid}].")

    dref = store.get_ref(kind="draft", id="nt")
    assert dref is not None
    cites = [
        l
        for l in store.links_for(dref.id, direction="out", relation="cites")
        if l.dst_ref_id == rid
    ]
    assert len(cites) == 1
    assert cites[0].meta.get("cite_mode") == "record"
    assert cites[0].meta.get("record_type") == "dataset"
    # The inverse walk: the record's cited-by list contains this draft.
    inbound = store.links_for(rid, direction="in", relation="cites")
    assert any(l.src_ref_id == dref.id for l in inbound)


def test_record_edge_stamp_idempotent(draft: DraftHandler, store: Store) -> None:
    rid = seed_ref(store, title="precis-mcp", kind="paper")
    _type(store, rid, "software")
    draft.put(id="nt", title="T", project=_proj(store))
    _add_chunk(draft, store, "nt", f"We used it [pa{rid}].")

    dref = store.get_ref(kind="draft", id="nt")
    assert dref is not None
    draft.sync_draft_links(dref.id)
    draft.sync_draft_links(dref.id)
    cites = [
        l
        for l in store.links_for(dref.id, direction="out", relation="cites")
        if l.dst_ref_id == rid
    ]
    assert len(cites) == 1
    assert cites[0].meta.get("cite_mode") == "record"


def test_edge_to_bodied_paper_gets_no_record_stamp(
    draft: DraftHandler, store: Store
) -> None:
    """A typed paper that carries passages is no longer a record — the
    edge stays a plain ``cites`` (stamp absent, predicate live)."""
    rid = seed_ref(store, title="typed paper", kind="paper")
    _type(store, rid, "software")
    seed_chunk(store, ref_id=rid, text="a passage")
    draft.put(id="nt", title="T", project=_proj(store))
    _add_chunk(draft, store, "nt", f"See [pa{rid}].")

    dref = store.get_ref(kind="draft", id="nt")
    assert dref is not None
    cites = [
        l
        for l in store.links_for(dref.id, direction="out", relation="cites")
        if l.dst_ref_id == rid
    ]
    assert len(cites) == 1
    assert "cite_mode" not in (cites[0].meta or {})


# ── lint hint + hygiene exemption, and the paper regression pin ───────


def test_whole_paper_hint_skips_record(draft: DraftHandler, store: Store) -> None:
    rid = seed_ref(store, title="precis-mcp", kind="paper")
    _type(store, rid, "software")
    draft.put(id="nt", title="T", project=_proj(store))
    r = _add_chunk(draft, store, "nt", f"We used the tool [pa{rid}].")
    assert "whole-paper" not in r.body


def test_whole_paper_hint_still_fires_on_plain_paper(
    draft: DraftHandler, store: Store
) -> None:
    """Regression pin (gr514384): an ordinary paper — or any paper without
    ``record_type`` — keeps the whole-paper warning exactly as today."""
    rid = seed_ref(store, title="ordinary paper", kind="paper")
    draft.put(id="nt", title="T", project=_proj(store))
    r = _add_chunk(draft, store, "nt", f"See the classic work [pa{rid}].")
    assert "whole-paper citation" in r.body
    assert f"[pa{rid}]" in r.body


def test_hygiene_skips_record_but_lists_plain(
    draft: DraftHandler, store: Store
) -> None:
    rec = seed_ref(store, title="precis-mcp", kind="paper")
    plain = seed_ref(store, title="ordinary paper", kind="paper")
    _type(store, rec, "software")
    draft.put(id="nt", title="T", project=_proj(store))
    _add_chunk(
        draft,
        store,
        "nt",
        f"We used [pa{rec}] and follow [pa{plain}].",
    )
    out = draft.get(id="nt", view="hygiene").body
    assert "whole-paper" in out  # the plain paper still lands the line
    assert f"[pa{plain}]" in out
    assert f"[pa{rec}]" not in out
