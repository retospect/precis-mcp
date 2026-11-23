"""gr690404 lane A — no-DOI inbox binding and visible drop outcomes.

A manual PDF drop that prints no DOI used to mint an anonymous record
even when a held 0-block stub's title matched the body (the Yao/Iijima
Nature-letter cases): the stub kept reporting a bare ``no-pdf`` and the
owner re-dropped a file already held. Now the drop scores every held
stub's title against the leading body — a unique high score folds into
the stub, a near miss mints and leaves ``stub_possible_match`` rows on
both refs, and every drop aimed at a stub leaves a verdict on the stub.
A detected next-article head in the tail is journalled — naming the
held stub that owns it when one does — under a ``multi-article`` tag;
the extract is never cut.

``_pick_stub_bind`` and the title_body lexicon are pure; the rest is
end-to-end on a real Postgres (the ``store`` fixture) with
``extract_paper`` stubbed like ``tests/ingest/test_add.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from precis.ingest.add import (
    IngestResult,
    PdfInput,
    _pick_stub_bind,
    precis_add,
)
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite
from precis.store import Store
from precis.utils.title_body import body_lexicon, lexicon_score, title_body_score

# ---------------------------------------------------------------------------
# Pure: _pick_stub_bind + the shared body lexicon
# ---------------------------------------------------------------------------


class TestPickStubBind:
    def test_unique_high_score_binds(self) -> None:
        bind, rest = _pick_stub_bind([(10, "T", 0.8), (20, "U", 0.55)])
        assert bind == 10 and rest == [(20, "U", 0.55)]

    def test_ambiguous_pair_does_not_bind(self) -> None:
        # A runner-up inside the margin makes the match ambiguous.
        cands = [(10, "T", 0.7), (20, "U", 0.62)]
        bind, rest = _pick_stub_bind(cands)
        assert bind is None and rest == cands

    def test_clear_margin_binds_despite_second_hit(self) -> None:
        bind, _rest = _pick_stub_bind([(10, "T", 0.9), (20, "U", 0.65)])
        assert bind == 10

    def test_below_threshold_never_binds_but_notes(self) -> None:
        cands = [(10, "T", 0.5)]
        bind, rest = _pick_stub_bind(cands)
        assert bind is None and rest == cands

    def test_empty(self) -> None:
        assert _pick_stub_bind([]) == (None, [])


class TestBodyLexicon:
    def test_lexicon_score_matches_title_body_score(self) -> None:
        title = "Atomic structure of carbon nanotubes"
        body = (
            "Atomic structure of carbon nanotubes\n"
            "S. Iijima and T. Ichihashi\n"
            "The carbon nanotube shells reported here are helical. " * 8
        )
        direct = title_body_score(title, body)
        lex = body_lexicon(body)
        assert lex is not None
        via = lexicon_score(lex, title)
        assert direct is not None and via is not None
        assert via.score == direct.score
        assert via.matched == direct.matched

    def test_empty_body_is_none(self) -> None:
        assert body_lexicon("   ") is None
        assert body_lexicon("") is None


# ---------------------------------------------------------------------------
# DB-backed: a no-DOI drop binds to the held stub its title matches
# ---------------------------------------------------------------------------

_STUB_TITLE = "Atomic structure of carbon nanotubes"

#: Chunk 0 carries the paper's title head + byline like Marker leaves it;
#: later chunks are prose only so only a real article restart trips
#: ``_opens_article``. Every title term (atomic, structure, carbon,
#: nanotubes) occurs in the prose, so the stub's title scores ~1.0.
_HEAD = _STUB_TITLE + "\nS. Iijima and T. Ichihashi\n"
_PROSE = (
    "The carbon nanotube shells reported here are helical and their atomic "
    "structure is consistent with graphitic carbon layers rolled into "
    "cylinders; electron diffraction shows the nanotube walls clearly. "
)

_FOREIGN_TITLE = (
    "Cobalt-catalysed growth of carbon nanotubes with single-atomic-layer walls"
)

_FOREIGN_TAIL = (
    _FOREIGN_TITLE
    + "\nD. S. Bethune, C. H. Kiang & M. S. de Vries\n"
    + "Cobalt-catalysed growth produces carbon nanotubes whose walls are a "
    "single atomic layer; the cobalt catalyst drives the growth of the "
    "single-atomic-layer walls throughout the sample. " * 3
)


def _body_chunks(n_prose: int = 3) -> list[ChunkToWrite]:
    return [
        ChunkToWrite(
            ord=-1,
            chunk_kind="card_combined",
            text=_STUB_TITLE + "\nS. Iijima",
        ),
        ChunkToWrite(
            ord=0,
            chunk_kind="paragraph",
            text=_HEAD + _PROSE * 4,
            page_first=1,
            page_last=1,
        ),
        *[
            ChunkToWrite(
                ord=i,
                chunk_kind="paragraph",
                text=_PROSE * 4 + f"Section {i}.",
                page_first=1,
                page_last=1,
            )
            for i in range(1, n_prose)
        ],
    ]


def _paper(
    *,
    sha: str,
    chunks: list[ChunkToWrite] | None = None,
    doi: str | None = None,
) -> PaperToWrite:
    return PaperToWrite(
        title=_STUB_TITLE,
        authors=[{"name": "Iijima, S."}],
        year=1993,
        kind="paper",
        provider="local",
        set_by="system",
        paper_id="p" + sha[:7],
        cite_key_prefix="iijima93",
        doi=doi,
        pdf_sha256=sha,
        content_hash="h" + sha[:63],
        pdf_storage_path="/tmp/fake.pdf",
        pdf_page_count=1,
        pdf_size_bytes=100,
        chunks=chunks if chunks is not None else _body_chunks(),
    )


def _ingest(
    store: Store,
    tmp_path: Path,
    paper: PaperToWrite,
    name: str = "yao1999.pdf",
    *,
    fold_ref_id: int | None = None,
) -> IngestResult | None:
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-fake " + str(paper.pdf_sha256).encode())
    with patch("precis.ingest.pipeline.extract_paper", return_value=paper):
        return precis_add(PdfInput(pdf_path=pdf, fold_ref_id=fold_ref_id), store=store)


def _events(store: Store, ref_id: int) -> list[tuple[str, str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT source, event, payload FROM ref_events WHERE ref_id = %s "
            "ORDER BY ts",
            (ref_id,),
        ).fetchall()
    return [(str(s), str(e), dict(p or {})) for s, e, p in rows]


def _chunk_count(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _open_tags(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT tg.value FROM ref_tags rt JOIN tags tg USING (tag_id) "
            "WHERE rt.ref_id = %s AND tg.namespace = 'OPEN'",
            (ref_id,),
        ).fetchall()
    return {str(r[0]) for r in rows}


class TestNoDoiStubBind:
    def test_binds_to_unique_matching_stub(self, store: Store, tmp_path: Path) -> None:
        """The Yao/Iijima shape: a held stub whose title the body matches
        takes the PDF's body instead of a second anonymous record."""
        stub = store.insert_ref(kind="paper", slug="iijima93x", title=_STUB_TITLE)
        result = _ingest(store, tmp_path, _paper(sha="b" * 64))
        assert result is not None and result.ref_id == stub.id
        assert result.inserted is False
        assert _chunk_count(store, stub.id) == 3
        bound = [p for _s, e, p in _events(store, stub.id) if e == "stub_bound"]
        assert bound and bound[0]["via"] == "title-match"
        assert bound[0]["score"] >= 0.6

    @pytest.mark.parametrize("stub_authors", [[], [{"family": "Iijima"}]])
    def test_second_opening_after_references_stays_advisory(
        self, store: Store, tmp_path: Path, stub_authors: list[dict[str, str]]
    ) -> None:
        stub = store.insert_ref(
            kind="paper", slug="next93", title=_STUB_TITLE, authors=stub_authors
        )
        chunks = [
            ChunkToWrite(
                ord=0,
                chunk_kind="paragraph",
                text="Superfluid helium in confined channels\n"
                "S. Iijima and M. S. de Vries\n"
                "Helium flow exhibits quantized circulation.",
            ),
            ChunkToWrite(ord=1, chunk_kind="references", text="References"),
            ChunkToWrite(ord=2, chunk_kind="paragraph", text=_HEAD + _PROSE * 4),
        ]
        result = _ingest(store, tmp_path, _paper(sha="e" * 64, chunks=chunks))
        assert result is not None and result.inserted
        assert _chunk_count(store, stub.id) == 0
        status = store.paper_fetch_status([stub.id])[0]
        assert status["inbox_event"] == "stub_possible_match"
        assert status["inbox_payload"]["record_ref_id"] == result.ref_id

    def test_no_candidate_mints_without_notes(
        self, store: Store, tmp_path: Path
    ) -> None:
        store.insert_ref(
            kind="paper", slug="other93", title="Unrelated superfluid helium"
        )
        result = _ingest(store, tmp_path, _paper(sha="c" * 64))
        assert result is not None and result.inserted is True
        assert all(
            e != "stub_possible_match" for _s, e, _p in _events(store, result.ref_id)
        )

    def test_ambiguous_match_mints_and_notes_both_sides(
        self, store: Store, tmp_path: Path
    ) -> None:
        """Two stubs the same body satisfies equally: mint + a visible
        'possible match' note on the record and each stub."""
        stub_a = store.insert_ref(kind="paper", slug="iijima93a", title=_STUB_TITLE)
        stub_b = store.insert_ref(kind="paper", slug="iijima93b", title=_STUB_TITLE)
        result = _ingest(store, tmp_path, _paper(sha="d" * 64))
        assert result is not None and result.inserted is True
        assert result.ref_id not in (stub_a.id, stub_b.id)
        rec_notes = [
            p
            for _s, e, p in _events(store, result.ref_id)
            if e == "stub_possible_match"
        ]
        assert {p["stub_ref_id"] for p in rec_notes} == {stub_a.id, stub_b.id}
        for stub in (stub_a, stub_b):
            notes = [
                p for _s, e, p in _events(store, stub.id) if e == "stub_possible_match"
            ]
            assert notes and notes[0]["record_ref_id"] == result.ref_id
            assert "stub-possible-match" in _open_tags(store, stub.id)

    def test_cited_title_and_scattered_terms_stay_advisory(
        self, store: Store, tmp_path: Path
    ) -> None:
        stub = store.insert_ref(kind="paper", slug="iijima93w", title=_STUB_TITLE)
        chunks = [
            ChunkToWrite(ord=0, chunk_kind="paragraph", text=_PROSE * 3),
            ChunkToWrite(ord=1, chunk_kind="references", text=_STUB_TITLE),
        ]
        result = _ingest(store, tmp_path, _paper(sha="1" * 64, chunks=chunks))
        assert result is not None and result.inserted
        assert _chunk_count(store, stub.id) == 0
        assert (
            store.paper_fetch_status([stub.id])[0]["inbox_event"]
            == "stub_possible_match"
        )

    def test_missing_named_target_does_not_abort_mint(
        self, store: Store, tmp_path: Path
    ) -> None:
        result = _ingest(store, tmp_path, _paper(sha="2" * 64), name="pa2147483647.pdf")
        assert result is not None and result.inserted

    def test_sidecar_dedup_leaves_record_pointer(
        self, store: Store, tmp_path: Path
    ) -> None:
        original = _ingest(
            store, tmp_path, _paper(sha="3" * 64, doi="10.9999/duplicate")
        )
        assert original is not None
        target = store.insert_ref(kind="paper", slug="wanted93", title=_STUB_TITLE)
        for sha in ("3" * 64, "4" * 64):
            duplicate = _ingest(
                store,
                tmp_path,
                _paper(sha=sha, doi="10.9999/duplicate"),
                fold_ref_id=target.id,
            )
            assert duplicate is not None and duplicate.ref_id == original.ref_id
            status = store.paper_fetch_status([target.id])[0]
            assert status["inbox_event"] == "stub_duplicate"
            assert status["inbox_payload"]["record_ref_id"] == original.ref_id

    def test_doi_printed_skips_title_bind(self, store: Store, tmp_path: Path) -> None:
        """A drop that prints a DOI never title-binds — identifier dedup
        owns that path."""
        stub = store.insert_ref(kind="paper", slug="iijima93f", title=_STUB_TITLE)
        result = _ingest(store, tmp_path, _paper(sha="f" * 64, doi="10.9999/x"))
        assert result is not None and result.inserted is True
        assert result.ref_id != stub.id
        assert _chunk_count(store, stub.id) == 0

    def test_named_target_gets_outcome_when_invalid(
        self, store: Store, tmp_path: Path
    ) -> None:
        """``pa<id>.pdf`` naming a ref that already has a body: the drop
        mints elsewhere and the named ref's log says why (was: silence)."""
        bodied = store.insert_ref(kind="paper", slug="full93", title="Bodied")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', 'already a body')",
                (bodied.id,),
            )
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 1, '')",
                ("0" * 64, "0" * 64),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                ("0" * 64, bodied.id),
            )
            conn.commit()
        result = _ingest(
            store, tmp_path, _paper(sha="7" * 64), name=f"pa{bodied.id}.pdf"
        )
        assert result is not None and result.inserted is True
        refused = [
            p for _s, e, p in _events(store, bodied.id) if e == "stub_fold_refused"
        ]
        assert refused and "has-body" in refused[0]["reason"]


class TestForeignTailNote:
    def test_tail_matching_held_stub_is_kept_and_journalled(
        self, store: Store, tmp_path: Path
    ) -> None:
        """pa477143's shape: the bound body's tail is the NEXT letter's
        head, and that letter's stub is held — the extract stays whole,
        both refs get a journal row, the record is tagged multi-article."""
        stub = store.insert_ref(kind="paper", slug="iijima93t", title=_STUB_TITLE)
        neighbour = store.insert_ref(
            kind="paper", slug="bethune93", title=_FOREIGN_TITLE
        )
        chunks = _body_chunks() + [
            ChunkToWrite(
                ord=3,
                chunk_kind="paragraph",
                text=_FOREIGN_TAIL,
                page_first=2,
                page_last=2,
            ),
        ]
        # Named for the stub (stem fold): the tail would otherwise make the
        # neighbour's title an equal title-match, an ambiguity by design.
        result = _ingest(
            store,
            tmp_path,
            _paper(sha="8" * 64, chunks=chunks),
            name=f"pa{stub.id}.pdf",
        )
        assert result is not None and result.ref_id == stub.id
        # The foreign suffix stays in the stored body — the boundary is
        # journalled, never cut (the standing tag-only ruling).
        with store.pool.connection() as conn:
            ords = [
                r[0]
                for r in conn.execute(
                    "SELECT ord FROM chunks WHERE ref_id = %s AND ord >= 0 "
                    "AND retired_at IS NULL ORDER BY ord",
                    (stub.id,),
                ).fetchall()
            ]
        assert ords == [0, 1, 2, 3]
        noted = [p for _s, e, p in _events(store, stub.id) if e == "foreign_tail"]
        assert noted and noted[0]["stub_ref_id"] == neighbour.id
        assert noted[0]["tail_ords"] == [3]
        stub_noted = [
            p for _s, e, p in _events(store, neighbour.id) if e == "stub_foreign_tail"
        ]
        assert stub_noted and stub_noted[0]["record_ref_id"] == stub.id
        assert "multi-article" in _open_tags(store, stub.id)

    def test_tail_without_held_stub_gets_suspected_boundary(
        self, store: Store, tmp_path: Path
    ) -> None:
        """A next-letter head whose letter has no held stub: the extract
        stays whole, the suspected boundary is journalled with its tail
        title, and the record is tagged multi-article."""
        stub = store.insert_ref(kind="paper", slug="iijima93u", title=_STUB_TITLE)
        chunks = _body_chunks() + [
            ChunkToWrite(
                ord=3,
                chunk_kind="paragraph",
                text=_FOREIGN_TAIL,
                page_first=2,
                page_last=2,
            ),
        ]
        result = _ingest(
            store,
            tmp_path,
            _paper(sha="9" * 64, chunks=chunks),
            name=f"pa{stub.id}.pdf",
        )
        assert result is not None and result.ref_id == stub.id
        assert _chunk_count(store, stub.id) == 4
        noted = [p for _s, e, p in _events(store, stub.id) if e == "foreign_tail"]
        assert noted and noted[0]["tail_ords"] == [3]
        assert "stub_ref_id" not in noted[0]
        assert "multi-article" in _open_tags(store, stub.id)

    def test_own_title_tail_is_not_flagged(self, store: Store, tmp_path: Path) -> None:
        """A tail chunk that only restates the record's own title head —
        no restart signature — leaves the body alone entirely."""
        echo = _STUB_TITLE + "\nS. Iijima and T. Ichihashi\n" + _PROSE * 3
        chunks = _body_chunks() + [
            ChunkToWrite(
                ord=3, chunk_kind="paragraph", text=echo, page_first=2, page_last=2
            ),
        ]
        result = _ingest(store, tmp_path, _paper(sha="a" * 64, chunks=chunks))
        assert result is not None
        assert _chunk_count(store, result.ref_id) == 4
        assert all(
            e not in ("foreign_tail",) for _s, e, _p in _events(store, result.ref_id)
        )
