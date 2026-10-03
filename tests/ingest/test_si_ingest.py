"""SI attachments, ingest side: a ``role='supplement'`` sidecar mints the PDF
as its OWN ref linked to the parent, and the SI cites as the parent."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

from precis.ingest.add import PdfInput, precis_add
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite
from precis.ingest.fetch_sidecar import read_sidecar, write_sidecar
from precis.store.si_links import (
    SI_INVERSE_RELATION,
    SI_RELATION,
    supplement_children,
    supplement_parent,
)

PARENT_DOI = "10.1021/acscatal.3c01963"


def _seed_parent(store) -> int:
    ref = store.insert_ref(
        kind="paper",
        slug="smith2023cat",
        title="NO reduction on Pd(111)",
        authors=[{"name": "Smith, Jane"}],
        year=2023,
        meta={"journal": "ACS Catal."},
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'doi', %s, 'manual')",
            (ref.id, PARENT_DOI),
        )
        conn.commit()
    return ref.id


def _drop_pdf(tmp_path: Path, name: str, payload: bytes) -> tuple[Path, str]:
    pdf = tmp_path / name
    pdf.write_bytes(payload)
    return pdf, hashlib.sha256(payload).hexdigest()


def _si_paper_as_extracted(sha: str) -> PaperToWrite:
    """What Marker + the metadata cascade yield for an SI PDF: it prints the
    PARENT's DOI and title, which is exactly the trap."""
    return PaperToWrite(
        title="NO reduction on Pd(111)",
        authors=[{"name": "Smith, Jane"}],
        year=2023,
        kind="paper",
        provider="crossref",
        paper_id=f"doi:{PARENT_DOI}",
        pub_id="abcdef",
        cite_key_prefix="smith23",
        pdf_sha256=sha,
        content_hash="c" * 64,
        pdf_role="main",
        pdf_storage_path="/tmp/si.pdf",
        pdf_page_count=2,
        pdf_size_bytes=10,
        doi=PARENT_DOI,
        meta={"abstract": "the parent's abstract", "journal": "wrong"},
        chunks=[
            ChunkToWrite(ord=-1, chunk_kind="card_combined", text="stale card"),
            ChunkToWrite(
                ord=0, chunk_kind="paragraph", text="Table S1 lists NH3 yields."
            ),
        ],
    )


def _ingest(store, pdf: Path, sha: str, parent_id: int, **info: str):
    with patch(
        "precis.ingest.pipeline.extract_paper",
        return_value=_si_paper_as_extracted(sha),
    ):
        return precis_add(
            PdfInput(
                pdf_path=pdf,
                supplement_of=parent_id,
                supplement_info=info or None,
            ),
            store=store,
        )


def test_sidecar_role_roundtrip(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    write_sidecar(
        pdf,
        ref_id=7,
        identifiers={"cite_key": "smith2023cat"},
        source="fetcher:si",
        role="supplement",
        si={"source": "figshare", "url": "https://x/f.pdf", "component_doi": ""},
    )
    sc = read_sidecar(pdf)
    assert sc is not None
    assert sc.role == "supplement"
    assert sc.ref_id == 7
    assert sc.si == {"source": "figshare", "url": "https://x/f.pdf"}


def test_sidecar_without_role_is_unchanged(tmp_path: Path) -> None:
    pdf = tmp_path / "y.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    write_sidecar(pdf, ref_id=3, identifiers={}, source="fetcher:unpaywall")
    sc = read_sidecar(pdf)
    assert sc is not None and sc.role is None and sc.si is None


def test_supplement_mints_own_linked_ref(store, tmp_path: Path) -> None:
    parent_id = _seed_parent(store)
    pdf, sha = _drop_pdf(tmp_path, "si1.pdf", b"%PDF-1.4 si one")
    res = _ingest(
        store,
        pdf,
        sha,
        parent_id,
        source="figshare",
        url="https://ndownloader.figshare.com/files/1",
        component_doi=f"{PARENT_DOI}.s001",
    )
    assert res is not None
    assert res.inserted is True
    assert res.ref_id != parent_id

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT pdf_role, title, year, meta FROM refs WHERE ref_id = %s",
            (res.ref_id,),
        ).fetchone()
        idents = dict(
            conn.execute(
                "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id = %s",
                (res.ref_id,),
            ).fetchall()
        )
        parent_doi_rows = conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind='doi' AND id_value=%s",
            (PARENT_DOI,),
        ).fetchall()
        link = conn.execute(
            "SELECT src_ref_id, dst_ref_id, meta->>'role' FROM links "
            "WHERE relation = %s",
            (SI_RELATION,),
        ).fetchall()
        parent_children = supplement_children(conn, parent_id)
        si_parent = supplement_parent(conn, res.ref_id)
        parent_chunks = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s", (parent_id,)
        ).fetchone()
        si_cards = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord < 0", (res.ref_id,)
        ).fetchall()

    assert row is not None
    assert row[0] == "supplement"
    assert row[1] == "Supporting Information: NO reduction on Pd(111)"
    assert row[2] == 2023
    assert row[3]["si_parent"]["ref_id"] == parent_id
    assert row[3]["si_parent"]["source"] == "figshare"
    assert "abstract" not in row[3]
    assert row[3]["journal"] == "ACS Catal."
    # no parent identifiers on the SI ref; only its own component DOI
    assert idents["doi"] == f"{PARENT_DOI}.s001"
    assert idents["paper_id"].startswith("sha256:")
    assert "pub_id" not in idents
    # the parent's DOI still belongs to the parent alone
    assert [r[0] for r in parent_doi_rows] == [parent_id]
    assert link == [(res.ref_id, parent_id, "supplement")]
    assert [c[0] for c in parent_children] == [res.ref_id]
    assert si_parent == (parent_id, "smith2023cat")
    assert SI_INVERSE_RELATION == "contains"
    # parent untouched: no chunks folded into it
    assert parent_chunks is not None and parent_chunks[0] == 0
    # cards rebuilt from the minted title, not the stale extracted card
    assert si_cards and all("stale card" not in r[0] for r in si_cards)
    assert any("Supporting Information" in r[0] for r in si_cards)


def test_same_sha_twice_is_one_ref(store, tmp_path: Path) -> None:
    parent_id = _seed_parent(store)
    pdf, sha = _drop_pdf(tmp_path, "si2.pdf", b"%PDF-1.4 si two")
    first = _ingest(store, pdf, sha, parent_id)
    second = _ingest(store, pdf, sha, parent_id)
    assert first is not None and second is not None
    assert second.ref_id == first.ref_id
    with store.pool.connection() as conn:
        n_refs = conn.execute(
            "SELECT count(*) FROM refs WHERE pdf_role = 'supplement'"
        ).fetchone()
        n_links = conn.execute(
            "SELECT count(*) FROM links WHERE relation = %s", (SI_RELATION,)
        ).fetchone()
    assert n_refs is not None and n_refs[0] == 1
    assert n_links is not None and n_links[0] == 1


def test_second_si_gets_numbered_title(store, tmp_path: Path) -> None:
    parent_id = _seed_parent(store)
    pdf1, sha1 = _drop_pdf(tmp_path, "a.pdf", b"%PDF-1.4 a")
    pdf2, sha2 = _drop_pdf(tmp_path, "b.pdf", b"%PDF-1.4 b")
    r1 = _ingest(store, pdf1, sha1, parent_id)
    # second file: distinct content hash so it is not the same-body dedup
    with patch(
        "precis.ingest.pipeline.extract_paper",
        return_value=_si_paper_as_extracted(sha2).__class__(
            **{
                **_si_paper_as_extracted(sha2).__dict__,
                "content_hash": "d" * 64,
            }
        ),
    ):
        r2 = precis_add(PdfInput(pdf_path=pdf2, supplement_of=parent_id), store=store)
    assert r1 is not None and r2 is not None and r1.ref_id != r2.ref_id
    with store.pool.connection() as conn:
        titles = [
            r[0]
            for r in conn.execute(
                "SELECT title FROM refs WHERE pdf_role='supplement' ORDER BY ref_id"
            ).fetchall()
        ]
    assert titles == [
        "Supporting Information: NO reduction on Pd(111)",
        "Supporting Information: NO reduction on Pd(111) (2)",
    ]


def test_citation_of_si_resolves_to_parent(store, tmp_path: Path) -> None:
    parent_id = _seed_parent(store)
    pdf, sha = _drop_pdf(tmp_path, "si3.pdf", b"%PDF-1.4 si three")
    res = _ingest(store, pdf, sha, parent_id)
    assert res is not None
    with store.pool.connection() as conn:
        chunk_id = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (res.ref_id,)
        ).fetchone()
    assert chunk_id is not None

    ref_h = store.resolve_handle(f"pa{res.ref_id}")
    chunk_h = store.resolve_handle(f"pc{chunk_id[0]}")
    parent_h = store.resolve_handle(f"pa{parent_id}")
    for h in (ref_h, chunk_h):
        assert h is not None
        assert h.cite_public_id == "smith2023cat"
        # address-producing callers still reach the SI ref itself
        assert h.ref_id == res.ref_id
        assert h.public_id != "smith2023cat"
    assert parent_h is not None and parent_h.cite_public_id is None

    # the exporters' shared resolver returns the parent's key
    from precis.export import latex

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=store)
    assert latex._handle_cite_key(f"pc{chunk_id[0]}", ctx) == "smith2023cat"


def test_overview_names_parent_and_lists_children(store, tmp_path: Path) -> None:
    from precis.dispatch import Hub
    from precis.embedder import MockEmbedder
    from precis.handlers.paper import PaperHandler

    parent_id = _seed_parent(store)
    pdf, sha = _drop_pdf(tmp_path, "si4.pdf", b"%PDF-1.4 si four")
    res = _ingest(store, pdf, sha, parent_id)
    assert res is not None
    handler = PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
    # parent-side listing is gated on the paper having been SI-requested
    handler.put(id="smith2023cat", mode="fetch-si")
    si_ref = store.fetch_refs_by_ids([res.ref_id])[res.ref_id]
    parent_ref = store.fetch_refs_by_ids([parent_id])[parent_id]
    assert (
        "supplementary information of smith2023cat — cite as smith2023cat"
        in handler._render_overview(si_ref).body
    )
    assert (
        f"supplementary information: {si_ref.slug}"
        in handler._render_overview(parent_ref).body
    )


def test_search_row_for_si_chunk_says_cite_as_parent(store, tmp_path: Path) -> None:
    from precis.handlers._paper_search import (
        BlockSearchResult,
        PaperSearchResultRenderer,
    )

    parent_id = _seed_parent(store)
    pdf, sha = _drop_pdf(tmp_path, "si5.pdf", b"%PDF-1.4 si five")
    res = _ingest(store, pdf, sha, parent_id)
    assert res is not None
    si_ref = store.fetch_refs_by_ids([res.ref_id])[res.ref_id]
    block = store.chunks.get_chunk(res.ref_id, pos=0)
    assert block is not None
    out = PaperSearchResultRenderer(kind="paper").render(
        BlockSearchResult(
            kind="paper",
            q="NH3 yields",
            page=1,
            page_size=10,
            scope=None,
            hits=[(block, si_ref, 0.5)],
            year_notice="",
            broad=False,
            broad_has_more=False,
        )
    )
    assert "SI of smith2023cat — cite as smith2023cat" in out.body
