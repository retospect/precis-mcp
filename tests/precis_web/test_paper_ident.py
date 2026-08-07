"""Unit tests for the shared paper identity header (paper_ident + macro)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from precis_web.deps import templates
from precis_web.paper_ident import (
    PAPER_IDENT_KINDS,
    PaperHead,
    paper_abstract,
    paper_head,
    paper_head_from_facts,
)


def _ref(**kw):
    """A minimal duck-typed ``refs`` row (paper_head reads via getattr)."""
    kw.setdefault("id", 42)
    kw.setdefault("meta", {})
    kw.setdefault("authors", None)
    kw.setdefault("year", None)
    kw.setdefault("title", "")
    kw.setdefault("slug", None)
    return SimpleNamespace(**kw)


def test_paper_head_pulls_year_title_journal_authors() -> None:
    head = paper_head(
        _ref(
            id=7,
            title="A Study of Widgets",
            year=2021,
            meta={"journal": "Nature"},
            authors=[{"name": "Ada Lovelace"}, {"name": "Alan Turing"}],
            slug="lovelace2021",
        ),
        held=True,
        handle="pa7",
    )
    assert head.ref_id == 7
    assert head.title == "A Study of Widgets"
    assert head.year == 2021
    assert head.journal == "Nature"
    assert head.first_author == "Ada Lovelace"
    assert head.last_author == "Alan Turing"
    assert head.multi_author is True
    assert head.cite_key == "lovelace2021"
    assert head.handle == "pa7"
    assert head.held is True


def test_paper_head_single_author_is_not_multi() -> None:
    head = paper_head(_ref(title="Solo", authors=[{"name": "Grace Hopper"}]), held=True)
    assert head.first_author == "Grace Hopper"
    assert head.last_author == "Grace Hopper"
    assert head.multi_author is False


def test_paper_head_clips_long_journal() -> None:
    head = paper_head(
        _ref(
            title="X",
            meta={"journal": "Journal of the American Chemical Society"},
        ),
        held=True,
    )
    assert len(head.journal) <= 24
    assert head.journal.endswith("…")


def test_paper_head_tolerates_missing_and_garbage_meta() -> None:
    # meta not a dict, no authors, no title → no crash, sane blanks.
    head = paper_head(_ref(title="", meta=None, authors=None), held=False)
    assert head.title == "(untitled)"
    assert head.journal == ""
    assert head.first_author == ""
    assert head.multi_author is False
    assert head.held is False


def test_paper_abstract_strips_tags_and_clamps() -> None:
    ref = _ref(meta={"abstract": "<jats:p>Hello   <b>world</b></jats:p>"})
    assert paper_abstract(ref) == "Hello world"
    assert paper_abstract(_ref(meta={"abstract": "abcdefgh"}), max_chars=4) == "abcd…"
    assert paper_abstract(_ref(meta={})) == ""
    assert paper_abstract(_ref(meta=None)) == ""


def test_paper_head_from_facts_degrades_cleanly() -> None:
    head = paper_head_from_facts(ref_id=3, title="Bare\nfacts", year=1999)
    assert isinstance(head, PaperHead)
    assert head.ref_id == 3
    assert head.title == "Bare"  # first line only
    assert head.year == 1999
    assert head.journal == ""
    assert head.first_author == ""
    assert head.held is True


def test_as_dict_is_json_safe_and_carries_multi_author() -> None:
    head = paper_head(
        _ref(title="T", authors=[{"name": "A"}, {"name": "B"}]), held=True
    )
    d = head.as_dict()
    # tojson on the References panel would break on a dataclass — the dict
    # must round-trip through json.
    assert json.loads(json.dumps(d))["multi_author"] is True
    assert d["title"] == "T"


def test_paper_ident_kinds_are_the_paper_family() -> None:
    assert frozenset({"paper", "cfp", "patent"}) == PAPER_IDENT_KINDS


def _render(head: PaperHead, **kw: object) -> str:
    tmpl = templates.env.get_template("_paper_head.html.j2")
    # the macro is a dynamic attribute on the compiled module — invisible to
    # mypy's TemplateModule type, so reach it through Any.
    module: Any = tmpl.module
    return str(module.paper_head(head, **kw))


def test_macro_two_line_held_is_sky() -> None:
    head = paper_head(
        _ref(
            title="Widgets",
            year=2020,
            meta={"journal": "Nature"},
            authors=[{"name": "Ada"}, {"name": "Alan"}],
        ),
        held=True,
    )
    html = _render(head)
    assert "2020" in html
    assert "Widgets" in html
    assert "Nature" in html
    assert "Ada" in html and "Alan" in html and "…" in html  # first … last
    assert "text-sky-700" in html
    assert "text-amber-700" not in html


def test_macro_not_held_is_amber() -> None:
    head = paper_head(_ref(title="Stub", year=2019), held=False)
    html = _render(head)
    assert "text-amber-700" in html
    assert "text-sky-700" not in html


def test_macro_compact_is_single_span() -> None:
    head = paper_head(_ref(title="Compact", year=2018), held=True)
    html = _render(head, compact=True)
    assert "Compact" in html
    assert "2018" in html
    # compact mode is one inline <span>, not the two-line <div>.
    assert "<div" not in html
