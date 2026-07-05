"""PresentationHandler attribution editing + citation export.

Covers the ``edit`` verb (writes attribution into ``meta``), the
``view='bibtex'`` / ``'ris'`` render it feeds, and the two pure helpers
(``_normalize_pres_authors`` / ``_format_pres_citation``). The shared
get/search/put machinery is exercised by the ingest tests; here we only
assert the attribution surface added for citing slides.

DB-bound tests use the ``store`` fixture and skip when no postgres is
reachable (see ``tests/conftest.py``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.presentation import (
    PresentationHandler,
    _format_pres_citation,
    _normalize_pres_authors,
)

# ---------------------------------------------------------------------------
# Pure unit — author normalisation
# ---------------------------------------------------------------------------


class TestNormalizeAuthors:
    def test_newline_separated(self) -> None:
        assert _normalize_pres_authors("Payne, M. C.\nClark, S. J.") == [
            "Payne, M. C.",
            "Clark, S. J.",
        ]

    def test_semicolon_separated(self) -> None:
        assert _normalize_pres_authors("A; B ;C") == ["A", "B", "C"]

    def test_comma_within_name_not_split(self) -> None:
        # BibTeX "Family, Given" must stay whole — we never split on comma.
        assert _normalize_pres_authors("Payne, M. C.") == ["Payne, M. C."]

    def test_list_of_dicts_and_strings(self) -> None:
        assert _normalize_pres_authors(
            [{"family": "Payne", "given": "M. C."}, {"name": "S. Clark"}, "Z"]
        ) == ["M. C. Payne", "S. Clark", "Z"]

    def test_none_and_blank(self) -> None:
        assert _normalize_pres_authors(None) == []
        assert _normalize_pres_authors("\n ; \n") == []


# ---------------------------------------------------------------------------
# Pure unit — citation formatter
# ---------------------------------------------------------------------------


def _ref(**meta: object) -> SimpleNamespace:
    return SimpleNamespace(
        slug=meta.pop("slug", "deck1"),  # type: ignore[arg-type]
        title=meta.pop("title", "Lecture 1"),  # type: ignore[arg-type]
        meta=dict(meta),
    )


class TestFormatCitation:
    def test_bibtex_misc_default(self) -> None:
        out = _format_pres_citation(
            _ref(authors=["Payne, M. C."], venue="CASTEP Workshop", date="2001-12-06"),
            style="bibtex",
        )
        assert out.startswith("@misc{deck1,")
        assert "title = {Lecture 1}," in out
        assert "author = {Payne, M. C.}," in out
        assert "year = {2001}," in out
        assert "howpublished = {CASTEP Workshop}," in out

    def test_multiple_authors_joined_with_and(self) -> None:
        out = _format_pres_citation(
            _ref(authors=["Payne, M. C.", "Clark, S. J."]), style="bibtex"
        )
        assert "author = {Payne, M. C. and Clark, S. J.}," in out

    def test_bibtex_type_override(self) -> None:
        out = _format_pres_citation(_ref(bibtex_type="unpublished"), style="bibtex")
        assert out.startswith("@unpublished{deck1,")

    def test_latex_escape_in_title(self) -> None:
        out = _format_pres_citation(_ref(title="Bonds & Bands"), style="bibtex")
        assert r"Bonds \& Bands" in out

    def test_empty_attribution_still_valid_stub(self) -> None:
        out = _format_pres_citation(_ref(), style="bibtex")
        assert out.startswith("@misc{deck1,")
        assert "title = {Lecture 1}," in out

    def test_ris(self) -> None:
        out = _format_pres_citation(
            _ref(authors=["X"], date="2001", venue="V"), style="ris"
        )
        assert "TY  - SLIDE" in out
        assert "AU  - X" in out
        assert "PY  - 2001" in out
        assert "ER  - " in out


# ---------------------------------------------------------------------------
# Spec surface
# ---------------------------------------------------------------------------


def test_spec_supports_edit_and_lists_citation_views(hub: Hub) -> None:
    assert PresentationHandler.spec.supports_edit is True
    views = PresentationHandler(hub=hub).accepted_views()
    assert "bibtex" in views and "ris" in views


# ---------------------------------------------------------------------------
# DB-bound — edit round-trip
# ---------------------------------------------------------------------------


def _mk_deck(handler: PresentationHandler, slug: str = "2001-nuts-and-bolts") -> str:
    handler.put(
        id=slug,
        text="Timescales and lengthscales.",
        pos=0,
        subtype="slides",
        title="lecture01",
    )
    return slug


class TestEditRoundTrip:
    def test_edit_persists_and_feeds_bibtex(self, store) -> None:
        h = PresentationHandler(hub=Hub(store=store))
        slug = _mk_deck(h)

        resp = h.edit(
            id=slug,
            title="Nuts and Bolts — Lecture 1",
            authors="Payne, M. C.\nClark, S. J.",
            venue="CASTEP Workshop, Durham",
            date="2001-12-06",
        )
        assert "updated" in resp.body

        # Overview surfaces the authors.
        overview = h.get(id=slug)
        assert "Payne, M. C." in overview.body

        # BibTeX view renders from the persisted meta.
        bib = h.get(id=slug, view="bibtex").body
        assert bib.startswith("@misc{")
        assert "author = {Payne, M. C. and Clark, S. J.}," in bib
        assert "year = {2001}," in bib
        assert "Nuts and Bolts" in bib

    def test_edit_leaves_slides_untouched(self, store) -> None:
        h = PresentationHandler(hub=Hub(store=store))
        slug = _mk_deck(h)
        h.edit(id=slug, venue="Somewhere")
        # The single ingested slide body is unchanged (metadata-only edit).
        assert "Timescales and lengthscales." in h.get(id=f"{slug}/full").body

    def test_edit_requires_at_least_one_field(self, store) -> None:
        h = PresentationHandler(hub=Hub(store=store))
        slug = _mk_deck(h)
        with pytest.raises(BadInput):
            h.edit(id=slug)
