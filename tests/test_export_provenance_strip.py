"""Provenance-only record handles are stripped with their debris (both exporters)."""

from __future__ import annotations

from typing import Any

import docx as docx_lib

from precis.export import docx as dx
from precis.export import latex
from precis.export._provenance_strip import strip_provenance_handles

_DOSSIER = (
    "Two directions stay permanently ruled out: grounding the finding-hub "
    "provenance mechanism in prior art ([ql4283070]/[ql4279704]), and surfacing "
    "dr173020's/catpath's structure via internal graph search by phrasing "
    "variation ([ql4291187], [ql4297241] → [ql4292314], [ql4302832], closed at "
    "[ql4302833]/[ql4302834])."
)
_DOSSIER_OUT = (
    "Two directions stay permanently ruled out: grounding the finding-hub "
    "provenance mechanism in prior art, and surfacing dr173020's/catpath's "
    "structure via internal graph search by phrasing variation."
)


def _title(h: str) -> str:
    return f"Title of {h}"


def _strip(text: str) -> str:
    return strip_provenance_handles(text, title_for=_title)


def test_dossier_sentence() -> None:
    assert _strip(_DOSSIER) == _DOSSIER_OUT


def test_double_space_and_possessive() -> None:
    assert (
        _strip("let the October draft [dr173020] define the format")
        == "let the October draft define the format"
    )
    assert _strip("Surface [dr173020]'s claim") == "Surface Title of dr173020's claim"


def test_long_parenthetical_keeps_words() -> None:
    assert (
        _strip("done (see [me5], and the other notes here) now")
        == "done (see, and the other notes here) now"
    )


def test_citations_and_pins_untouched() -> None:
    for t in (
        "x [pa5] y",
        "x [fi191163>pc307979] y",
        "x [dc41] y",
        "x [st12] y",
        "x [dr173020>pc1] y",
        "x [dr173020~3] y",
        "x [shown](dr173020) y",
        "plain (text) here",
    ):
        assert _strip(t) == t


class _Store:
    def fetch_refs_by_ids(self, ids: list[int]) -> dict[int, Any]:
        return {}


def _docx_text(text: str) -> str:
    ctx = dx._Ctx(
        store=_Store(),
        known_handles=set(),
        doi_links=False,
        library_links=False,
        library_label="x",
        library_search_url="x",
    )
    para = docx_lib.Document().add_paragraph()
    dx._render_inline(text, ctx, para)
    return para.text


def test_docx_render_drops_gaps() -> None:
    out = _docx_text(_DOSSIER)
    for bad in ("(/)", "( →", "(closed at)", "()"):
        assert bad not in out
    assert out.startswith("Two directions stay permanently ruled out")
    assert "in prior art, and surfacing" in out
    assert out.endswith("phrasing variation.")


def test_latex_render_drops_gaps() -> None:
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=_Store())
    out = latex._render_inline(_DOSSIER, ctx)
    for bad in ("(/)", "( ", "closed at", "()"):
        assert bad not in out
    assert "in prior art, and surfacing" in out
    assert out.endswith("phrasing variation.")


def test_parenthetical_keeps_real_content() -> None:
    assert _strip("(see [pa12], [dr5])") == "(see [pa12])"
    assert _strip("(Smith et al. [dr1]) said") == "(Smith et al.) said"
    assert _strip("(n = 5 [td1])") == "(n = 5)"
    assert _strip("x (closed at [ql1]/[ql2]) y") == "x y"


def test_tidy_is_local() -> None:
    assert _strip("a  b ; [dr1] x  y") == "a  b ; x  y"
    assert _strip("a, [dr1], b") == "a, b"


def test_code_and_math_untouched() -> None:
    assert _strip("code `a  b ;` [dr1] x") == "code `a  b ;` x"
    assert _strip("$a  :  b$ [dr1]") == "$a  :  b$"
    assert _strip("`[dr1]` and $[dr1]$") == "`[dr1]` and $[dr1]$"


def test_possessive_title_is_neutralised() -> None:
    out = strip_provenance_handles("see [dr1]'s", title_for=lambda _h: "T$x$ *b* [pa1]")
    assert out == "see Tx b (pa1)'s"
