"""``view='refs'`` / ``view='si'`` / ``view='sentences'`` — gr725288.

The call-errors cadence fingerprinted ``get(kind='paper')`` dying on
``unknown view 'refs'`` (52 calls in 24 h, one client version) with
``view='sentences'`` and ``view='si'`` in the same arg samples. The
server now serves all three: the paper's own reference list, its
supplementary-information records, and a sentence-split chunk read.

DB-backed: real ``refs``/``chunks``/``paper_bib_entries``/``links``
rows, read back through ``PaperHandler.get`` and the runtime dispatch
path, mirroring ``tests/test_paper_tables_view.py``.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput, NotFound
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry


def _handler(store: Store) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))


def _seed_paper(store: Store, *, slug: str) -> int:
    return store.insert_ref(kind="paper", slug=slug, title=slug).id


def _insert_bib_entry(
    store: Store,
    paper_id: int,
    *,
    marker: int,
    raw_text: str,
    doi: str | None = None,
    held_ref_id: int | None = None,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO paper_bib_entries (ref_id, marker, raw_text, doi, "
            "held_ref_id, parse_version) VALUES (%s, %s, %s, %s, %s, 1)",
            (paper_id, marker, raw_text, doi, held_ref_id),
        )


# --- view='refs' -------------------------------------------------------------


def test_refs_view_renders_parsed_bib_entries(store: Store) -> None:
    paper = _seed_paper(store, slug="refsbib07")
    held = _seed_paper(store, slug="refsheld07")
    _insert_bib_entry(
        store,
        paper,
        marker=3,
        raw_text="Z. Ali, A. Braun, ChemCatChem 2020, 12, 360.",
        doi="10.1002/cctc.202000111",
        held_ref_id=held,
    )
    _insert_bib_entry(
        store,
        paper,
        marker=7,
        raw_text="Q. Unmatched, Some Journal 2019, 4, 1.",
    )
    pa = handle_registry.format_handle("paper", paper)
    held_pa = handle_registry.format_handle("paper", held)

    resp = _handler(store).get(id=pa, view="refs")

    assert resp.result_count == 2
    out = resp.body
    assert out.startswith(f"# {pa} — refs: 2 bibliography entries")
    assert "[3]" in out and "[7]" in out
    # Resolved identity columns ride along: DOI + held-ref handle.
    assert "10.1002/cctc.202000111" in out
    assert held_pa in out
    assert "ChemCatChem 2020" in out
    # The view points back at the INBOUND list so the two never blur.
    assert "view='bibliography'" in out


def test_refs_view_references_alias_matches_refs(store: Store) -> None:
    paper = _seed_paper(store, slug="refsalias07")
    _insert_bib_entry(store, paper, marker=1, raw_text="A. Nother, J. X 2021.")
    pa = handle_registry.format_handle("paper", paper)
    h = _handler(store)

    via_alias = h.get(id=pa, view="references").body
    via_canonical = h.get(id=pa, view="refs").body
    assert via_alias == via_canonical


def test_refs_view_falls_back_to_references_chunks(store: Store) -> None:
    paper = store.insert_ref(
        kind="paper",
        slug="refsraw07",
        title="Raw references",
        meta={"bib_parse_version": 1},
    ).id
    text = "- [1] A. Nother, J. X 2021, 3, 12.\n- [2] B. Second, J. Y 2022."
    (block,) = store.chunks.insert_chunks(
        paper,
        [ChunkInsert(ord=0, text=text, meta={"chunk_kind": "references"})],
    )
    pa = handle_registry.format_handle("paper", paper)

    resp = _handler(store).get(id=pa, view="refs")

    assert resp.result_count == 1
    out = resp.body
    assert out.startswith("# refsraw07 — raw references (1 chunk)")
    assert f"pc{block.id}" in out
    assert "- [2] B. Second, J. Y 2022." in out


def test_refs_view_empty_points_at_s2_graph(store: Store) -> None:
    paper = _seed_paper(store, slug="refsempty07")
    store.chunks.insert_chunks(
        paper, [ChunkInsert(ord=0, text="prose only, no bibliography")]
    )
    pa = handle_registry.format_handle("paper", paper)

    resp = _handler(store).get(id=pa, view="refs")

    assert resp.result_count == 0
    assert f"no reference list on file for {pa}" in resp.body
    assert "refs:<doi>" in resp.body


def test_refs_view_reaches_through_runtime_dispatch(
    runtime_with_store: PrecisRuntime,
) -> None:
    store = runtime_with_store.store
    assert store is not None
    paper = _seed_paper(store, slug="refsdisp07")
    _insert_bib_entry(store, paper, marker=1, raw_text="A. Nother, J. X 2021.")
    pa = handle_registry.format_handle("paper", paper)

    out = runtime_with_store.dispatch(
        "get", {"kind": "paper", "id": pa, "view": "refs"}
    )
    assert "[1]" in out
    assert "[error:" not in out


def test_refs_view_path_form(store: Store) -> None:
    """``slug/refs`` serves the same view as ``view='refs'`` — the help
    advertises ``slug/<view>`` as equivalent, so the path form resolves
    (gr725288)."""
    paper = _seed_paper(store, slug="refspath07")
    _insert_bib_entry(store, paper, marker=1, raw_text="A. Nother, J. X 2021.")
    h = _handler(store)

    via_path = h.get(id="refspath07/refs").body
    via_kwarg = h.get(id="refspath07", view="refs").body
    assert via_path == via_kwarg
    assert "[1]" in via_path
    # The kwarg alias carries over to the path form.
    assert h.get(id="refspath07/references").body == via_kwarg


# --- view='si' ---------------------------------------------------------------


def _link_si(store: Store, *, si_id: int, parent_id: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET pdf_role = 'supplement' WHERE ref_id = %s", (si_id,)
        )
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, "
            "set_by, meta) VALUES (%s, %s, 'part-of', 'agent', "
            '\'{"role": "supplement"}\'::jsonb)',
            (si_id, parent_id),
        )


def test_si_view_lists_children_on_parent(store: Store) -> None:
    parent = _seed_paper(store, slug="siparent07")
    si = _seed_paper(store, slug="sichild07")
    _link_si(store, si_id=si, parent_id=parent)
    parent_pa = handle_registry.format_handle("paper", parent)
    si_pa = handle_registry.format_handle("paper", si)

    resp = _handler(store).get(id=parent_pa, view="si")

    assert resp.result_count == 1
    out = resp.body
    assert si_pa in out
    assert "cite an SI record as its parent paper" in out


def test_si_view_names_parent_on_si_ref(store: Store) -> None:
    parent = _seed_paper(store, slug="sipart07")
    si = _seed_paper(store, slug="sirec07")
    _link_si(store, si_id=si, parent_id=parent)
    parent_pa = handle_registry.format_handle("paper", parent)
    si_pa = handle_registry.format_handle("paper", si)

    resp = _handler(store).get(id=si_pa, view="si")
    assert resp.result_count == 1
    out = resp.body
    assert f"parent: {parent_pa}" in out
    assert f"cite as {parent_pa}, never as sirec07" in out


def test_si_view_none_points_at_fetch_si(store: Store) -> None:
    paper = _seed_paper(store, slug="sinone07")
    pa = handle_registry.format_handle("paper", paper)

    resp = _handler(store).get(id=pa, view="si")

    assert resp.result_count == 0
    out = resp.body
    assert "none ingested" in out
    assert "mode='fetch-si'" in out


def test_si_view_path_form(store: Store) -> None:
    parent = _seed_paper(store, slug="sipath07")
    si = _seed_paper(store, slug="sipathchild07")
    _link_si(store, si_id=si, parent_id=parent)
    si_pa = handle_registry.format_handle("paper", si)
    h = _handler(store)

    out = h.get(id="sipath07/si").body
    assert si_pa in out
    assert out == h.get(id="sipath07", view="si").body


# --- view='sentences' --------------------------------------------------------


def _seed_prose_paper(store: Store, *, slug: str) -> tuple[int, list[int]]:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    blocks = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=0, text="The rate doubled under load. The yield held."),
            ChunkInsert(ord=1, text="A second block follows here. It has two parts."),
        ],
    )
    return ref.id, [b.id for b in blocks]


def test_sentences_view_splits_a_single_chunk(store: Store) -> None:
    _seed_prose_paper(store, slug="sentsingle07")

    resp = _handler(store).get(id="sentsingle07~0", view="sentences")

    assert resp.result_count == 2
    out = resp.body
    assert out.startswith("# sentsingle07~0..0 — sentences")
    assert "1. The rate doubled under load." in out
    assert "2. The yield held." in out


def test_sentences_view_splits_a_range(store: Store) -> None:
    _paper, chunk_ids = _seed_prose_paper(store, slug="sentrange07")

    resp = _handler(store).get(id="sentrange07~0..1", view="sentences")

    assert resp.result_count == 4
    out = resp.body
    assert out.startswith("# sentrange07~0..1 — sentences")
    for cid in chunk_ids:
        assert f"pc{cid}" in out
    assert "1. The rate doubled under load." in out
    assert "2. It has two parts." in out


def test_sentences_view_refuses_ref_level_with_selector_form(
    store: Store,
) -> None:
    paper, _chunk_ids = _seed_prose_paper(store, slug="sentreflevel07")
    pa = handle_registry.format_handle("paper", paper)

    with pytest.raises(BadInput, match="chunk range"):
        _handler(store).get(id=pa, view="sentences")


def test_sentences_view_empty_range_raises_not_found(store: Store) -> None:
    _seed_prose_paper(store, slug="sentmiss07")

    with pytest.raises(NotFound):
        _handler(store).get(id="sentmiss07~5..6", view="sentences")


def test_sentences_view_preserves_stored_text_verbatim(store: Store) -> None:
    """The view is advertised for precise quoting — a numbered sentence
    must grep back to the stored chunk bytes, so no inline-tag strip,
    entity decode or whitespace collapse on the way out (gr725288)."""
    ref = store.insert_ref(kind="paper", slug="sentraw07", title="sentraw07")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="The <i>in&nbsp;vitro</i>  yield doubled. It held.",
            )
        ],
    )

    out = _handler(store).get(id="sentraw07~0", view="sentences").body

    assert "The <i>in&nbsp;vitro</i>  yield doubled." in out
    assert "2. It held." in out


def test_sentences_view_path_form(store: Store) -> None:
    """``slug~N/sentences`` serves the same read as ``~N`` +
    ``view='sentences'`` (gr725288)."""
    _seed_prose_paper(store, slug="sentspath07")
    h = _handler(store)

    via_path = h.get(id="sentspath07~0/sentences").body
    via_kwarg = h.get(id="sentspath07~0", view="sentences").body
    assert via_path == via_kwarg
    assert "1. The rate doubled under load." in via_path


def test_sentences_view_reaches_through_runtime_dispatch(
    runtime_with_store: PrecisRuntime,
) -> None:
    store = runtime_with_store.store
    assert store is not None
    _seed_prose_paper(store, slug="sentdisp07")

    out = runtime_with_store.dispatch(
        "get",
        {"kind": "paper", "id": "sentdisp07~0", "view": "sentences"},
    )
    assert "1. The rate doubled under load." in out
    assert "[error:" not in out


@pytest.mark.parametrize("ordinal", [0, -1])
def test_sentences_view_pc_handle_reaches_through_runtime_dispatch(
    runtime_with_store: PrecisRuntime, ordinal: int
) -> None:
    """gr725288's reported call shape — ``id='pc<id>'`` +
    ``view='sentences'``. Dispatch rewrites the chunk handle to
    ``<slug>~<ord>``, then chunk_spec + view='sentences' routes to the
    sentence renderer."""
    store = runtime_with_store.store
    assert store is not None
    paper = _seed_paper(store, slug="sentpc07")
    (block,) = store.chunks.insert_chunks(
        paper,
        [
            ChunkInsert(
                ord=ordinal,
                text="The rate doubled under load. The yield held.",
                meta={"chunk_kind": "card_abstract" if ordinal < 0 else "paragraph"},
            )
        ],
    )

    out = runtime_with_store.dispatch(
        "get",
        {"kind": "paper", "id": f"pc{block.id}", "view": "sentences"},
    )
    assert "1. The rate doubled under load." in out
    assert "2. The yield held." in out
    assert "[error:" not in out


@pytest.mark.parametrize("suspected", [False, True])
def test_sentences_view_preserves_glyph_warning(store: Store, suspected: bool) -> None:
    ref = store.insert_ref(
        kind="paper",
        slug="sentglyph07",
        title="Glyph warning",
        meta={"glyph_health": {"suspected": suspected}},
    )
    clean = "The measurement was repeated."
    damaged = "The temperature was 2,000 8C."
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=clean), ChunkInsert(ord=1, text=damaged)]
    )

    out = _handler(store).get(id="sentglyph07~0..1", view="sentences").body

    warning = "GLYPH-DAMAGED SOURCE"
    assert out.count(warning) == 1
    if suspected:
        assert out.index(warning) < out.index(clean)
    else:
        assert out.index(clean) < out.index(warning)
    assert out.index(warning) < out.index(damaged)


def test_new_views_are_advertised(store: Store) -> None:
    accepted = _handler(store).accepted_views()
    assert "refs" in accepted
    assert "si" in accepted
    assert "sentences" in accepted
