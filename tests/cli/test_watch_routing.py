"""Pure tests for :func:`precis.cli.watch.route_pdf`.

The routing function is path-only — no DB, no Marker — so we can
exhaustively cover the matrix of (kind dir, tagging dir nesting,
sentinel collisions) without a fixture PDF.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.cli.watch import route_pdf


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
