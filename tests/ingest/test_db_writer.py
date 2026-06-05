"""Tests for ``precis.ingest.db_writer`` — the v2 INSERT cascade.

Two test families:

- pure unit tests (no DB) covering the suffix-progression logic in
  ``_next_cite_key``;
- integration tests against the ``store`` fixture (ephemeral
  Postgres with the migration applied) covering ``probe_existing``,
  ``resolve_cite_key``, and ``write_paper``.

The DB-bound tests skip automatically when the precis-dev container
isn't reachable (see ``tests/conftest.py::_pg_available``).
"""

from __future__ import annotations

import pytest

from precis.ingest.db_writer import (
    ChunkToWrite,
    PaperToWrite,
    _next_cite_key,
    probe_existing,
    resolve_cite_key,
    write_paper,
)

# ---------------------------------------------------------------------------
# _next_cite_key — pure unit tests
# ---------------------------------------------------------------------------


class TestNextCiteKey:
    def test_no_collision_returns_prefix(self):
        assert _next_cite_key("miller23", set()) == "miller23"

    def test_first_collision_appends_a(self):
        assert _next_cite_key("miller23", {"miller23"}) == "miller23a"

    def test_second_collision_appends_b(self):
        taken = {"miller23", "miller23a"}
        assert _next_cite_key("miller23", taken) == "miller23b"

    def test_fills_gaps_in_order(self):
        # 'b' is taken but 'a' is free, so 'a' is the next pick.
        taken = {"miller23", "miller23b"}
        assert _next_cite_key("miller23", taken) == "miller23a"

    def test_progresses_past_z(self):
        taken = {"miller23"}
        for c in "abcdefghijklmnopqrstuvwxyz":
            taken.add(f"miller23{c}")
        # All single-letter suffixes taken; next is 'aa'.
        assert _next_cite_key("miller23", taken) == "miller23aa"

    def test_unrelated_prefixes_dont_count(self):
        # 'jones23a' shares no prefix with 'miller23'; should not affect.
        assert _next_cite_key("miller23", {"jones23a"}) == "miller23"


# ---------------------------------------------------------------------------
# probe_existing — DB-backed
# ---------------------------------------------------------------------------


class TestProbeExisting:
    def test_empty_db_returns_none(self, store):
        with store.pool.connection() as conn:
            assert probe_existing(doi="10.1038/x", conn=conn) is None

    def test_no_identifiers_returns_none(self, store):
        with store.pool.connection() as conn:
            assert probe_existing(conn=conn) is None

    def test_finds_by_doi(self, store):
        ref_id = _seed_ref(
            store,
            identifiers={"doi": "10.1038/test1"},
        )
        with store.pool.connection() as conn:
            hit = probe_existing(doi="10.1038/test1", conn=conn)
        assert hit == ref_id

    def test_finds_by_arxiv(self, store):
        ref_id = _seed_ref(
            store,
            identifiers={"arxiv": "2401.12345"},
        )
        with store.pool.connection() as conn:
            hit = probe_existing(arxiv_id="2401.12345", conn=conn)
        assert hit == ref_id

    def test_finds_by_pdf_sha256(self, store):
        sha = "a" * 64
        ref_id = _seed_ref(
            store,
            identifiers={"pdf_sha256": sha},
        )
        with store.pool.connection() as conn:
            hit = probe_existing(pdf_sha256=sha, conn=conn)
        assert hit == ref_id

    def test_first_match_wins_when_multiple(self, store):
        # Same paper, two identifiers: probing with both must return
        # the same ref_id.
        ref_id = _seed_ref(
            store,
            identifiers={
                "doi": "10.1038/multi",
                "arxiv": "2403.99999",
            },
        )
        with store.pool.connection() as conn:
            hit = probe_existing(
                doi="10.1038/multi",
                arxiv_id="2403.99999",
                conn=conn,
            )
        assert hit == ref_id


# ---------------------------------------------------------------------------
# resolve_cite_key — DB-backed
# ---------------------------------------------------------------------------


class TestResolveCiteKey:
    def test_empty_db_returns_prefix(self, store):
        with store.pool.connection() as conn:
            assert resolve_cite_key("smith24", conn=conn) == "smith24"

    def test_collision_appends_a(self, store):
        _seed_ref(store, identifiers={"cite_key": "smith24"})
        with store.pool.connection() as conn:
            assert resolve_cite_key("smith24", conn=conn) == "smith24a"

    def test_two_collisions_appends_b(self, store):
        _seed_ref(store, identifiers={"cite_key": "smith24"})
        _seed_ref(store, identifiers={"cite_key": "smith24a"})
        with store.pool.connection() as conn:
            assert resolve_cite_key("smith24", conn=conn) == "smith24b"

    def test_empty_prefix_raises(self, store):
        with store.pool.connection() as conn, pytest.raises(ValueError):
            resolve_cite_key("", conn=conn)


# ---------------------------------------------------------------------------
# write_paper — DB-backed integration
# ---------------------------------------------------------------------------


class TestWritePaper:
    def test_metadata_only_minimal(self, store):
        """A DOI-only ingest writes refs + ref_identifiers but no
        pdfs row and no chunks. Used by ``precis add --doi``."""
        paper = PaperToWrite(
            title="Quantum Error Correction in Practice",
            authors=[{"name": "Smith, John"}],
            year=2024,
            paper_id="z7q2k4m5",
            pub_id="doi:10.1038/test",
            cite_key_prefix="smith24",
            doi="10.1038/test",
            provider="crossref",
        )
        with store.pool.connection() as conn:
            result = write_paper(paper, conn=conn)
            conn.commit()

        assert result.cite_key == "smith24"
        assert result.chunks_written == 0
        assert result.identifiers_written == {
            "paper_id": "z7q2k4m5",
            "cite_key": "smith24",
            "pub_id": "doi:10.1038/test",
            "doi": "10.1038/test",
        }

        with store.pool.connection() as conn:
            ref_row = conn.execute(
                "SELECT title, year, provider, kind, set_by, pdf_sha256 "
                "FROM refs WHERE ref_id = %s",
                (result.ref_id,),
            ).fetchone()
        assert ref_row == (
            "Quantum Error Correction in Practice",
            2024,
            "crossref",
            "paper",
            "system",
            None,
        )

    def test_pdf_with_chunks(self, store):
        """A PDF ingest writes pdfs + refs + ref_identifiers + chunks
        and stitches them by ref_id."""
        sha = "b" * 64
        paper = PaperToWrite(
            title="Surface Codes for Fault-Tolerant Computing",
            authors=[{"name": "Jones, Alice"}],
            year=2023,
            paper_id="m9p3n6q1",
            pub_id="doi:10.1103/test",
            cite_key_prefix="jones23",
            doi="10.1103/test",
            pdf_sha256=sha,
            content_hash="c" * 64,
            pdf_storage_path="/corpus/j/jones23.pdf",
            pdf_page_count=12,
            pdf_size_bytes=1_500_000,
            pdf_pages_first=1,
            pdf_pages_last=12,
            pdf_role="main",
            provider="s2",
            chunks=[
                ChunkToWrite(
                    ord=-1,
                    chunk_kind="card_combined",
                    text="Surface Codes for Fault-Tolerant Computing\nJones, Alice (2023)",
                ),
                ChunkToWrite(
                    ord=0,
                    chunk_kind="paragraph",
                    text="Quantum error correction is essential…",
                    section_path=["1", "Introduction"],
                    page_first=1,
                    page_last=1,
                ),
                ChunkToWrite(
                    ord=1,
                    chunk_kind="paragraph",
                    text="The surface code achieves a threshold of …",
                    section_path=["1", "Introduction"],
                    page_first=1,
                    page_last=2,
                ),
            ],
        )
        with store.pool.connection() as conn:
            result = write_paper(paper, conn=conn)
            conn.commit()

        assert result.chunks_written == 3
        assert result.identifiers_written["pdf_sha256"] == sha

        with store.pool.connection() as conn:
            pdf_row = conn.execute(
                "SELECT page_count, size_bytes, storage_path "
                "FROM pdfs WHERE pdf_sha256 = %s",
                (sha,),
            ).fetchone()
            chunk_count = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s",
                (result.ref_id,),
            ).fetchone()
            card_count = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord < 0",
                (result.ref_id,),
            ).fetchone()
        assert pdf_row == (12, 1_500_000, "/corpus/j/jones23.pdf")
        assert chunk_count is not None and chunk_count[0] == 3
        assert card_count is not None and card_count[0] == 1

    def test_pdf_dedup_on_conflict(self, store):
        """Inserting two refs that point at the same pdf_sha256 must
        not duplicate the pdfs row (ON CONFLICT DO NOTHING)."""
        sha = "d" * 64
        paper1 = _make_paper(
            paper_id="aa11bb22",
            cite_key_prefix="kim23",
            doi="10.1/aa",
            pdf_sha256=sha,
        )
        paper2 = _make_paper(
            paper_id="cc33dd44",
            cite_key_prefix="kim23a",
            doi="10.1/bb",
            pdf_sha256=sha,
        )
        with store.pool.connection() as conn:
            write_paper(paper1, conn=conn)
            write_paper(paper2, conn=conn)
            conn.commit()

        with store.pool.connection() as conn:
            pdf_count = conn.execute(
                "SELECT count(*) FROM pdfs WHERE pdf_sha256 = %s",
                (sha,),
            ).fetchone()
        assert pdf_count is not None and pdf_count[0] == 1

    def test_cite_key_collision_progresses(self, store):
        """Two distinct papers with the same prefix get suffix
        progression (smith24 → smith24a)."""
        paper1 = _make_paper(
            paper_id="ee55ff66",
            cite_key_prefix="smith24",
            doi="10.1/p1",
        )
        paper2 = _make_paper(
            paper_id="gg77hh88",
            cite_key_prefix="smith24",
            doi="10.1/p2",
        )
        with store.pool.connection() as conn:
            r1 = write_paper(paper1, conn=conn)
            r2 = write_paper(paper2, conn=conn)
            conn.commit()
        assert r1.cite_key == "smith24"
        assert r2.cite_key == "smith24a"

    def test_missing_paper_id_raises(self, store):
        bad = PaperToWrite(
            title="x",
            authors=[],
            year=None,
            paper_id="",  # invalid
            cite_key_prefix="x24",
        )
        with store.pool.connection() as conn, pytest.raises(ValueError):
            write_paper(bad, conn=conn)

    def test_missing_cite_key_prefix_raises(self, store):
        bad = PaperToWrite(
            title="x",
            authors=[],
            year=None,
            paper_id="aabbccdd",
            cite_key_prefix="",  # invalid
        )
        with store.pool.connection() as conn, pytest.raises(ValueError):
            write_paper(bad, conn=conn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_ref(store, *, identifiers: dict[str, str]) -> int:
    """Insert a minimal ref + the given identifiers; return ref_id."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO refs (kind, set_by, title) "
            "VALUES ('paper', 'system', 'seed') "
            "RETURNING ref_id"
        ).fetchone()
        assert row is not None
        ref_id = row[0]
        for kind, value in identifiers.items():
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id) "
                "VALUES (%s, %s, %s)",
                (kind, value, ref_id),
            )
        conn.commit()
        return ref_id


def _make_paper(
    *,
    paper_id: str,
    cite_key_prefix: str,
    doi: str | None = None,
    pdf_sha256: str | None = None,
) -> PaperToWrite:
    return PaperToWrite(
        title="placeholder",
        authors=[{"name": "Placeholder, P."}],
        year=2024,
        paper_id=paper_id,
        pub_id=f"doi:{doi}" if doi else None,
        cite_key_prefix=cite_key_prefix,
        doi=doi,
        pdf_sha256=pdf_sha256,
        content_hash=pdf_sha256,
        pdf_storage_path="/tmp/x.pdf" if pdf_sha256 else None,
        pdf_page_count=1 if pdf_sha256 else None,
        pdf_size_bytes=100 if pdf_sha256 else None,
    )
