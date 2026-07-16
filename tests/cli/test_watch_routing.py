"""Pure tests for :func:`precis.cli.watch.route_pdf`.

The routing function is path-only — no DB, no Marker — so we can
exhaustively cover the matrix of (kind dir, tagging dir nesting,
sentinel collisions) without a fixture PDF.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.cli.watch import (
    _infer_markup_fmt,
    _is_ingestable,
    _is_markup,
    _is_pdf,
    route_pdf,
)
from precis.ingest.fetch_sidecar import FetchSidecar


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """A throwaway watch dir; files don't need to exist for routing."""
    root = tmp_path / "inbox"
    root.mkdir()
    return root


def _drop(inbox: Path, *parts: str) -> Path:
    """Materialize a fake PDF at ``inbox/parts...`` and return its path."""
    target = inbox.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"%PDF-1.4\n")
    return target


class TestKindSelection:
    def test_papers_dir_routes_to_paper(self, inbox: Path):
        pdf = _drop(inbox, "papers", "foo.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "paper"
        assert r.extra_tags == ()

    def test_books_dir_routes_to_paper_with_book_sentinels(self, inbox: Path):
        pdf = _drop(inbox, "books", "cohen-tannoudji.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "paper"
        assert set(r.extra_tags) == {"subtype:book", "topic:book"}

    def test_cfp_dir_routes_to_cfp(self, inbox: Path):
        pdf = _drop(inbox, "cfp", "nsf-25-501.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "cfp"
        assert r.extra_tags == ()

    def test_cfp_dir_with_tagging(self, inbox: Path):
        pdf = _drop(inbox, "cfp", "tagging", "grants-2026", "call.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "cfp"
        assert set(r.extra_tags) == {"topic:grants-2026"}

    def test_datasheets_dir_routes_to_datasheet(self, inbox: Path):
        pdf = _drop(inbox, "datasheets", "esp32-c3.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "datasheet"
        assert r.extra_tags == ()

    def test_datasheets_dir_with_tagging(self, inbox: Path):
        pdf = _drop(inbox, "datasheets", "tagging", "esp32", "ds.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "datasheet"
        assert set(r.extra_tags) == {"topic:esp32"}

    def test_presentations_dir_routes_to_pres(self, inbox: Path):
        pdf = _drop(inbox, "presentations", "lecture-3.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "pres"
        assert r.extra_tags == ()

    def test_flat_inbox_falls_back_to_paper(self, inbox: Path):
        # Back-compat: files already sitting flat in inbox/ at deploy
        # keep ingesting as paper.
        pdf = _drop(inbox, "old-style.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "paper"
        assert r.extra_tags == ()

    def test_unknown_first_dir_falls_back_to_paper(self, inbox: Path):
        # An operator-created subdir that isn't part of the kind
        # vocabulary still ingests as paper — no silent rejection.
        pdf = _drop(inbox, "misc", "foo.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "paper"
        assert r.extra_tags == ()


class TestTaggingSegment:
    def test_single_tagging_slug_under_papers(self, inbox: Path):
        pdf = _drop(inbox, "papers", "tagging", "matthias-quantum", "foo.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "paper"
        assert r.extra_tags == ("topic:matthias-quantum",)

    def test_underscore_normalized_to_kebab(self, inbox: Path):
        pdf = _drop(inbox, "papers", "tagging", "matthias_quantum", "foo.pdf")
        r = route_pdf(pdf, inbox)
        assert r.extra_tags == ("topic:matthias-quantum",)

    def test_nested_tagging_components_stack(self, inbox: Path):
        pdf = _drop(
            inbox,
            "presentations",
            "tagging",
            "matthias-quantum",
            "lecture-3",
            "deck.pdf",
        )
        r = route_pdf(pdf, inbox)
        assert r.kind == "pres"
        assert r.extra_tags == ("topic:matthias-quantum", "topic:lecture-3")

    def test_books_plus_tagging_compose(self, inbox: Path):
        pdf = _drop(
            inbox,
            "books",
            "tagging",
            "matthias-quantum",
            "qm-textbook.pdf",
        )
        r = route_pdf(pdf, inbox)
        assert r.kind == "paper"
        # Order matters: book sentinels come before user topic tags.
        assert r.extra_tags == (
            "subtype:book",
            "topic:book",
            "topic:matthias-quantum",
        )

    def test_tagging_without_kind_dir_still_works(self, inbox: Path):
        # Flat-inbox fallback still picks up tagging/ tokens — useful
        # for operators staging mixed batches without a kind decision.
        pdf = _drop(inbox, "tagging", "matthias-quantum", "foo.pdf")
        r = route_pdf(pdf, inbox)
        assert r.kind == "paper"
        assert r.extra_tags == ("topic:matthias-quantum",)

    def test_empty_tagging_components_dropped(self, inbox: Path):
        # A trailing/extra path separator that resolves to empty parts
        # shouldn't yield a ``topic:`` tag.
        pdf = _drop(inbox, "papers", "tagging", "valid-slug", "foo.pdf")
        r = route_pdf(pdf, inbox)
        assert r.extra_tags == ("topic:valid-slug",)


class TestEdgeCases:
    def test_pdf_outside_watch_dir_falls_back(self, tmp_path: Path):
        watch_dir = tmp_path / "inbox"
        watch_dir.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        pdf = other / "wandered.pdf"
        pdf.write_bytes(b"%PDF")
        r = route_pdf(pdf, watch_dir)
        # Defensive: paper kind, no tags. Should not raise.
        assert r.kind == "paper"
        assert r.extra_tags == ()

    def test_pres_with_tagging_no_book_sentinels(self, inbox: Path):
        # Sanity: book sentinels live only on the books/ branch.
        pdf = _drop(inbox, "presentations", "tagging", "course-2026", "deck.pdf")
        r = route_pdf(pdf, inbox)
        assert "subtype:book" not in r.extra_tags
        assert "topic:book" not in r.extra_tags
        assert "topic:course-2026" in r.extra_tags


class TestMarkupRecognition:
    def test_is_markup_extensions(self):
        assert _is_markup(Path("a.xml"))
        assert _is_markup(Path("a.tex"))
        assert _is_markup(Path("a.ltx"))
        assert _is_markup(Path("a.html"))
        assert _is_markup(Path("a.tar.gz"))
        assert _is_markup(Path("a.tgz"))
        assert not _is_markup(Path("a.pdf"))
        # A sidecar (even one named for a markup trigger) is never markup.
        assert not _is_markup(Path("a.xml.precis-fetch.json"))

    def test_is_ingestable_matrix(self):
        assert _is_ingestable(Path("a.pdf"))
        assert _is_ingestable(Path("a.xml"))
        assert _is_ingestable(Path("a.tar.gz"))
        assert not _is_ingestable(Path("a.txt"))
        assert not _is_ingestable(Path("a.pdf.precis-fetch.json"))
        assert not _is_ingestable(Path("a.xml.precis-fetch.json"))

    def test_is_pdf_still_pdf_only(self):
        assert _is_pdf(Path("a.pdf"))
        assert not _is_pdf(Path("a.xml"))

    def test_infer_fmt_from_sidecar_is_authoritative(self):
        # An .xml whose sidecar says elsevier_xml → elsevier_xml, not jats.
        sc = FetchSidecar(
            ref_id=1,
            identifiers={"doi": "10.1/x"},
            source="fetcher:elsevier",
            source_format="elsevier_xml",
        )
        assert _infer_markup_fmt(Path("a.xml"), sc) == "elsevier_xml"

    def test_infer_fmt_from_extension_fallback(self):
        assert _infer_markup_fmt(Path("a.xml"), None) == "jats"
        assert _infer_markup_fmt(Path("a.tex"), None) == "latex"
        assert _infer_markup_fmt(Path("a.tar.gz"), None) == "latex"
        assert _infer_markup_fmt(Path("a.html"), None) == "arxiv_html"

    def test_infer_fmt_pdf_sidecar_falls_through_to_extension(self):
        # A sidecar marked 'pdf' on a markup file → ignore, use extension.
        sc = FetchSidecar(
            ref_id=1,
            identifiers={},
            source="fetcher:x",
            source_format="pdf",
        )
        assert _infer_markup_fmt(Path("a.tex"), sc) == "latex"

    def test_infer_fmt_unknown_extension_returns_none(self):
        assert _infer_markup_fmt(Path("a.bin"), None) is None
