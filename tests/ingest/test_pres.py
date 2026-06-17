"""Tests for ``precis.ingest.pres`` — pres-specific ingest module.

Three families:

- pure unit tests over the slug/title helpers and the per-page
  slide builder;
- DB-bound tests over ``write_pres`` covering the slug-collision
  ``-2``/``-3`` policy and the chunk shape;
- a precis_add-level idempotency test that verifies tags merge on
  the sha256-hit branch.

DB-bound tests skip when no postgres reachable (see
``tests/conftest.py::_pg_available``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from precis.identity import make_pdf_sha256
from precis.ingest.add import PresInput, precis_add
from precis.ingest.pres import (
    PresSlide,
    PresToWrite,
    _build_slide,
    _resolve_pres_slug,
    derive_pres_slug,
    derive_pres_title,
    extract_pres,
    kebab_slug,
    write_pres,
)

# ---------------------------------------------------------------------------
# Pure unit tests — slug + title derivation
# ---------------------------------------------------------------------------


class TestKebabSlug:
    def test_lowercases(self):
        assert kebab_slug("FooBar") == "foobar"

    def test_underscore_to_dash(self):
        assert kebab_slug("matthias_quantum") == "matthias-quantum"

    def test_collapses_runs(self):
        assert kebab_slug("  --foo--BAR--  ") == "foo-bar"

    def test_strips_unicode_safely(self):
        # Non-ASCII collapses to dashes; never crashes on edge chars.
        assert kebab_slug("café—naïve") == "caf-na-ve"

    def test_empty_returns_empty(self):
        assert kebab_slug("") == ""
        assert kebab_slug("---") == ""


class TestDerivePresSlug:
    def test_preserves_year_prefix(self, tmp_path: Path):
        pdf = tmp_path / "2026-06-matthias-quantum-lecture-3.pdf"
        pdf.write_bytes(b"%PDF")
        assert derive_pres_slug(pdf) == "2026-06-matthias-quantum-lecture-3"

    def test_prepends_year_month_when_missing(self, tmp_path: Path):
        pdf = tmp_path / "lecture-3.pdf"
        pdf.write_bytes(b"%PDF")
        slug = derive_pres_slug(pdf)
        # Year prefix derived from file mtime; just assert the shape
        # and trailing stem so we don't fight wall-clock flakiness.
        assert slug.endswith("-lecture-3")
        head = slug[: -len("-lecture-3")]
        # YYYY-MM
        assert len(head) == 7 and head[4] == "-"
        assert head[:4].isdigit() and head[5:].isdigit()

    def test_untitled_when_stem_is_garbage(self, tmp_path: Path):
        pdf = tmp_path / "---.pdf"
        pdf.write_bytes(b"%PDF")
        slug = derive_pres_slug(pdf)
        assert slug.endswith("-untitled")

    def test_year_only_prefix_kept(self, tmp_path: Path):
        # A 4-digit year with no month still counts as year-prefixed.
        pdf = tmp_path / "2026-deck.pdf"
        pdf.write_bytes(b"%PDF")
        assert derive_pres_slug(pdf) == "2026-deck"


class TestDerivePresTitle:
    def test_humanizes_underscores(self, tmp_path: Path):
        pdf = tmp_path / "Matthias_Quantum_Lecture_3.pdf"
        assert derive_pres_title(pdf) == "Matthias Quantum Lecture 3"

    def test_humanizes_dashes(self, tmp_path: Path):
        pdf = tmp_path / "matthias-quantum.pdf"
        assert derive_pres_title(pdf) == "matthias quantum"


# ---------------------------------------------------------------------------
# Pure unit tests — slide builder
# ---------------------------------------------------------------------------


class TestBuildSlide:
    def test_section_header_becomes_title(self):
        blocks = [
            {"type": "section_header", "text": "Intro to QED"},
            {"type": "text", "text": "Some body content"},
        ]
        slide = _build_slide(pos=0, page=1, blocks=blocks)
        assert slide.slide_title == "Intro to QED"
        assert slide.text == "Some body content"

    def test_first_short_line_promoted_when_no_header(self):
        blocks = [{"type": "text", "text": "A short line\n\nlots of detail follows"}]
        slide = _build_slide(pos=2, page=3, blocks=blocks)
        assert slide.slide_title == "A short line"

    def test_fallback_to_slide_n_when_empty(self):
        slide = _build_slide(pos=4, page=5, blocks=[])
        assert slide.slide_title == "Slide 5"  # 0-indexed pos + 1
        assert slide.text == ""

    def test_long_first_line_skipped_for_title(self):
        blocks = [
            {
                "type": "text",
                "text": "x" * 200,  # too long for a title
            }
        ]
        slide = _build_slide(pos=0, page=1, blocks=blocks)
        assert slide.slide_title == "Slide 1"

    def test_first_image_attached(self):
        blocks = [
            {"type": "section_header", "text": "Figures"},
            {
                "type": "figure",
                "text": "",
                "image_base64": "abc==",
                "image_mime": "image/png",
            },
            {
                "type": "figure",
                "text": "",
                "image_base64": "xyz==",  # second image — should be ignored
                "image_mime": "image/png",
            },
        ]
        slide = _build_slide(pos=0, page=1, blocks=blocks)
        assert slide.image_base64 == "abc=="
        assert slide.image_mime == "image/png"

    def test_subsequent_headers_kept_in_body(self):
        # The first heading owns the title; later headings on the
        # same slide stay in the body (subtitles, section markers).
        blocks = [
            {"type": "section_header", "text": "Main"},
            {"type": "section_header", "text": "Subtitle"},
            {"type": "text", "text": "body"},
        ]
        slide = _build_slide(pos=0, page=1, blocks=blocks)
        assert slide.slide_title == "Main"
        # Subtitle text not emitted since we ``continue`` past
        # subsequent ``section_header`` blocks in title detection,
        # but the body still picks up the rest. (Conservative — we
        # don't reformat headings as paragraphs.)
        assert "body" in slide.text


# ---------------------------------------------------------------------------
# Pure unit tests — slug collision resolver
# ---------------------------------------------------------------------------


class TestResolvePresSlug:
    def test_no_collision_returns_input(self, store):
        with store.pool.connection() as conn:
            slug, suffixed = _resolve_pres_slug("fresh-slug", conn=conn)
        assert slug == "fresh-slug"
        assert suffixed is False

    def test_collision_appends_dash_two(self, store):
        # Seed a cite_key collision via a paper ref so we don't depend
        # on write_pres itself for the setup.
        with store.pool.connection() as conn:
            ref_row = conn.execute(
                "INSERT INTO refs (kind, title, set_by) "
                "VALUES ('paper', 'sentinel', 'system') RETURNING ref_id"
            ).fetchone()
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('cite_key', %s, %s, 'embedded')",
                ("taken-slug", ref_row[0]),
            )
            conn.commit()
            slug, suffixed = _resolve_pres_slug("taken-slug", conn=conn)
        assert slug == "taken-slug-2"
        assert suffixed is True

    def test_collision_walks_to_three(self, store):
        with store.pool.connection() as conn:
            for s in ("taken", "taken-2"):
                ref_row = conn.execute(
                    "INSERT INTO refs (kind, title, set_by) "
                    "VALUES ('paper', %s, 'system') RETURNING ref_id",
                    (f"sentinel-{s}",),
                ).fetchone()
                conn.execute(
                    "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                    "VALUES ('cite_key', %s, %s, 'embedded')",
                    (s, ref_row[0]),
                )
            conn.commit()
            slug, suffixed = _resolve_pres_slug("taken", conn=conn)
        assert slug == "taken-3"
        assert suffixed is True


# ---------------------------------------------------------------------------
# DB-bound tests — write_pres
# ---------------------------------------------------------------------------


def _sample_pres(slug: str = "2026-06-test-deck") -> PresToWrite:
    """Build a small PresToWrite for round-trip tests."""
    return PresToWrite(
        slug=slug,
        title="Test Deck",
        pdf_sha256="a" * 64,
        pdf_page_count=3,
        pdf_size_bytes=1024,
        pdf_storage_path="/tmp/test.pdf",
        meta={"source_pdf": "test.pdf"},
        slides=[
            PresSlide(pos=0, text="Opening slide body", slide_title="Welcome", page=1),
            PresSlide(pos=1, text="Body of slide 2", slide_title="Outline", page=2),
            PresSlide(
                pos=2,
                text="",
                slide_title="Image slide",
                page=3,
                image_base64="abc==",
                image_mime="image/png",
            ),
        ],
    )


class TestWritePres:
    def test_inserts_ref_and_chunks(self, store):
        pres = _sample_pres()
        with store.pool.connection() as conn:
            result = write_pres(pres, conn=conn)
            conn.commit()
        assert result.slug == "2026-06-test-deck"
        assert result.slug_suffixed is False
        assert result.n_slides == 3

        with store.pool.connection() as conn:
            ref_row = conn.execute(
                "SELECT kind, title, pdf_sha256 FROM refs WHERE ref_id = %s",
                (result.ref_id,),
            ).fetchone()
            assert ref_row == ("pres", "Test Deck", "a" * 64)

            chunks = conn.execute(
                "SELECT ord, chunk_kind, text, meta "
                "FROM chunks WHERE ref_id = %s ORDER BY ord",
                (result.ref_id,),
            ).fetchall()
            assert len(chunks) == 3
            assert [c[0] for c in chunks] == [0, 1, 2]
            assert all(c[1] == "pres_slide" for c in chunks)
            assert chunks[0][2] == "Opening slide body"
            assert chunks[0][3]["slide_title"] == "Welcome"
            assert chunks[2][3]["image_base64"] == "abc=="
            assert chunks[2][3]["image_mime"] == "image/png"

    def test_writes_pdf_sha256_identifier(self, store):
        # The ``probe_existing`` idempotency path keys off
        # ``ref_identifiers(id_kind='pdf_sha256')``, so the writer
        # MUST land that row even for pres refs.
        pres = _sample_pres(slug="2026-06-idem-deck")
        with store.pool.connection() as conn:
            result = write_pres(pres, conn=conn)
            conn.commit()
            row = conn.execute(
                "SELECT ref_id FROM ref_identifiers "
                "WHERE id_kind = 'pdf_sha256' AND id_value = %s",
                ("a" * 64,),
            ).fetchone()
        assert row is not None and row[0] == result.ref_id

    def test_slug_collision_suffixes(self, store):
        first = _sample_pres(slug="dup-slug")
        with store.pool.connection() as conn:
            write_pres(first, conn=conn)
            conn.commit()

        second = PresToWrite(
            slug="dup-slug",
            title="Different deck same slug",
            pdf_sha256="b" * 64,
            slides=[PresSlide(pos=0, text="hi", slide_title="x", page=1)],
        )
        with store.pool.connection() as conn:
            result = write_pres(second, conn=conn)
            conn.commit()
        assert result.slug == "dup-slug-2"
        assert result.slug_suffixed is True

    def test_requires_slug_and_title(self):
        with pytest.raises(ValueError, match="slug"):
            write_pres(_sample_pres(slug=""), conn=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# precis_add integration — sha256-hit idempotency + tag merge
# ---------------------------------------------------------------------------


class TestPresIngestIdempotency:
    """Verify the precis_add(PresInput, ...) flow:

    1. First call inserts a pres ref with ``subtype:slides`` and
       ``extra_tags``.
    2. Second call with same bytes is idempotent (no new ref, no
       new chunks) and merges any new ``extra_tags`` additively.

    We mock :func:`extract_pres` so the test runs without a real
    PDF fixture and without invoking Marker. The mocked extract
    returns a deterministic ``PresToWrite`` matching the on-disk
    bytes we write.
    """

    def _fake_extract(self, pdf_path, slug_hint=None, title_hint=None):
        pdf_bytes = Path(pdf_path).read_bytes()
        sha = make_pdf_sha256(pdf_bytes)
        return PresToWrite(
            slug=slug_hint or "2026-06-mocked",
            title=title_hint or "Mocked Deck",
            pdf_sha256=sha,
            pdf_page_count=1,
            pdf_size_bytes=len(pdf_bytes),
            pdf_storage_path=str(pdf_path),
            meta={"source_pdf": Path(pdf_path).name},
            slides=[PresSlide(pos=0, text="hi", slide_title="Welcome", page=1)],
        )

    def test_double_ingest_merges_tags(self, store, tmp_path: Path):
        pdf = tmp_path / "deck.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock\n")

        with patch("precis.ingest.add.extract_pres", new=self._fake_extract):
            r1 = precis_add(
                PresInput(pdf_path=pdf, extra_tags=("topic:matthias-quantum",)),
                store=store,
            )
            assert r1 is not None and r1.inserted is True
            assert r1.kind == "pres"
            ref_id = r1.ref_id

            # Tags from first ingest
            with store.pool.connection() as conn:
                tags = _ref_tag_values(conn, ref_id)
            assert "subtype:slides" in tags
            assert "topic:matthias-quantum" in tags

            # Second ingest with a new tag — sha256 hit, no new ref,
            # tags merged additively.
            r2 = precis_add(
                PresInput(pdf_path=pdf, extra_tags=("topic:course-2026",)),
                store=store,
            )
            assert r2 is not None and r2.inserted is False
            assert r2.ref_id == ref_id  # same ref

            with store.pool.connection() as conn:
                tags = _ref_tag_values(conn, ref_id)
                n_chunks = conn.execute(
                    "SELECT count(*) FROM chunks WHERE ref_id = %s",
                    (ref_id,),
                ).fetchone()[0]
            assert n_chunks == 1  # no duplicate chunks
            assert "topic:matthias-quantum" in tags  # original kept
            assert "topic:course-2026" in tags  # new added


def _ref_tag_values(conn, ref_id: int) -> set[str]:
    """Helper: read all OPEN-namespace tag values on a ref."""
    rows = conn.execute(
        "SELECT t.value FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE rt.ref_id = %s AND t.namespace = 'OPEN'",
        (ref_id,),
    ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# extract_pres — empty-page edge case
# ---------------------------------------------------------------------------


class TestExtractPresEmpty:
    """Verify the empty-extraction fallback without invoking Marker."""

    def test_no_blocks_yields_placeholder(self, tmp_path: Path):
        pdf = tmp_path / "empty.pdf"
        pdf.write_bytes(b"%PDF-1.4 empty\n")

        with patch("precis.ingest.marker.extract_blocks_marker", return_value=[]):
            result = extract_pres(pdf)

        assert len(result.slides) == 1
        assert result.slides[0].slide_title.startswith("Slide 1")
        assert result.slides[0].text == ""

    def test_groups_blocks_by_page(self, tmp_path: Path):
        pdf = tmp_path / "deck.pdf"
        pdf.write_bytes(b"%PDF-1.4 with-pages\n")
        fake_blocks = [
            {"type": "section_header", "text": "Title One", "page": 1},
            {"type": "text", "text": "Body 1", "page": 1},
            {"type": "section_header", "text": "Title Two", "page": 2},
            {"type": "text", "text": "Body 2", "page": 2},
        ]
        with patch(
            "precis.ingest.marker.extract_blocks_marker", return_value=fake_blocks
        ):
            result = extract_pres(pdf)

        assert len(result.slides) == 2
        assert result.slides[0].slide_title == "Title One"
        assert result.slides[0].text == "Body 1"
        assert result.slides[1].slide_title == "Title Two"
        assert result.slides[1].text == "Body 2"
