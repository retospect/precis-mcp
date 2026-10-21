"""Provenance-only record handles are stripped with their debris (both exporters)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import docx as docx_lib

from precis.export import docx as dx
from precis.export import latex
from precis.export._provenance_strip import (
    record_title,
    short_title,
    strip_provenance_handles,
)

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


def test_stranded_glue_word_drops_with_the_handle() -> None:
    """A dropped handle at a clause end takes the preposition/conjunction
    it stranded — "… under [td5]." prints "… .", not "… under."
    (gr493181, dossier 459659's "sits in January 2027 under.")."""
    assert _strip("sits in January 2027 under [td1].") == "sits in January 2027."
    assert (
        _strip("November holds catpath under [td1]; December next")
        == "November holds catpath; December next"
    )
    # the "(see [pa12] and [dr1])" leftover, gone with the stranded "and"
    assert _strip("(see [pa12] and [dr1]) ends") == "(see [pa12]) ends"
    assert _strip("x on [dr1] and [dr2].") == "x."  # a glued chain collapses
    # mid-clause the word stays; a word not touching the mark stays too
    assert _strip("kept in [td1] review.") == "kept in review."
    assert _strip("under review [td1].") == "under review."


def test_record_title_splices_the_short_title() -> None:
    """A bare handle renders the record's short title — a todo's
    ``refs.title`` tail is notes, not a title (gr493181: a bare
    ``td459589`` printed the todo's whole long title)."""
    title = (
        "Paper of the month, November 2026: post the catpath pathway-engine "
        "and trust-demo paper as a preprint. Reto moved it from January."
    )
    store = SimpleNamespace(
        fetch_refs_by_ids=lambda ids: {
            i: SimpleNamespace(title=title, meta={}) for i in ids
        }
    )
    out = record_title("td459589", store)
    assert out == (
        "Paper of the month, November 2026: post the catpath pathway-engine "
        "and trust-demo paper as a preprint"
    )
    assert "Reto moved" not in out


def test_short_title_first_sentence_and_clip() -> None:
    assert short_title("A short title") == "A short title"
    assert short_title("Head line.\nnotes below") == "Head line"
    assert short_title("First sentence. Then notes.") == "First sentence"
    long = "x " * 200
    out = short_title(long)
    assert len(out) <= 121 and out.endswith("…")


def test_short_title_skips_abbreviation_periods() -> None:
    """'Dr. Smith' / 'et al.' / 'e.g.' / 'J. R.' are not sentence ends —
    cutting there would splice a fragment ('Fix Dr') into prose."""
    assert (
        short_title("Fix Dr. Smith's model later. Notes here.")
        == "Fix Dr. Smith's model later"
    )
    assert short_title("Tuning e.g. the rate. Then notes.") == "Tuning e.g. the rate"
    assert short_title("J. R. Tolkien's maps. A note.") == "J. R. Tolkien's maps"
    assert (
        short_title("X by Chen et al. 2024 revisited. Notes.")
        == "X by Chen et al. 2024 revisited"
    )


def test_glue_words_stay_without_a_dropped_handle() -> None:
    """'under'/'and'/'see' in plain prose with no dropped handle are
    untouched — the word-drop only ever fires next to a removal mark."""
    for s in (
        "the cat sits under the mat and sleeps; see under the hood.",
        "it sits in January 2027 under review and over budget.",
        "see the appendix for details.",
    ):
        assert _strip(s) == s


def test_handle_selector_forms_normalise() -> None:
    """``[h~n]`` / ``(h~n)`` / bare ``h~n`` degrade to the record handle —
    a chunk selector can't resolve across a record boundary in an export
    (gr493181 — ``[dr173020~3]`` printed literally)."""
    assert _strip("see [dr173020~3] here") == "see here"
    assert _strip("bare dr173020~3 here") == "bare dr173020 here"
    assert _strip("x [pa4423~1] y") == "x [pa4423] y"
    assert _strip("[sel](dr173020~2) end") == "[sel](dr173020) end"
    # paper chunk cites and kind:slug selectors are not handle forms
    for keep in (
        "cite [§smith20~3] stays",
        "keep paper:lee22~3 cite",
        "math $x~dr5$ code `dr1~2`",
    ):
        assert _strip(keep) == keep


def test_handle_list_explodes() -> None:
    """``[pa442, pc30728]`` — authored shorthand for a cite group — becomes
    adjacent handles so each dispatches (cites merge; provenance drops)
    instead of printing ``[pa442, paper]``-style mangled text (gr493181)."""
    assert _strip("x [pa442, pc30728] y") == "x [pa442][pc30728] y"
    assert _strip("x [pa1 and pa2] y") == "x [pa1][pa2] y"
    assert _strip("x [dr5, ql9] y") == "x y"
    assert _strip("x [pa442, dr5] y") == "x [pa442] y"
    # not all elements handles → authored prose, untouched
    assert _strip("x [see fig. 2, or dr5] y") == "x [see fig. 2, or dr5] y"


def test_authoring_links_unwrap_or_drop() -> None:
    """``[[…]]``: a handle payload rejoins the grammar (``[[dc5]]`` → its
    cross-ref, ``[[pa5]]`` → the cite, ``[[dr5]]`` → provenance drop); a
    known ``kind:`` payload drops; anything else is markup, not body text
    — all with seam repair (gr493181)."""
    assert _strip("x [[dr5]] y") == "x y"
    assert _strip("x [[dc5]] y") == "x [dc5] y"
    assert _strip("x [[pa5]] y") == "x [pa5] y"
    assert _strip("x [[draft:2]] y") == "x y"
    assert _strip("x [[paper:lee22]] y") == "x paper:lee22 y"
    assert _strip("x [[weird words]] y") == "x y"


def test_kind_ref_drops_with_seam_repair() -> None:
    """A bare ``kind:ref`` for an unciteable known kind (``todo:5``,
    ``memory:6184~2``, ``[todo:5]``) is dropped — the same set the backend
    reference dispatch dropped before, now with the seam repaired. Citeable
    and unknown kinds stay literal (``paper:``, ``doi:``, ``nursery:``)."""
    assert _strip("see todo:5 here") == "see here"
    assert _strip("see memory:6184~2 here") == "see here"
    assert _strip("see [todo:5] here") == "see here"
    for keep in (
        "cite paper:lee22 here",
        "doi doi:10.1/x here",
        "cluster nursery:orphan here",
        "code `todo:5` and math $job:1$",
    ):
        assert _strip(keep) == keep


def test_display_link_surface_survives_a_dropped_target() -> None:
    """``[text](<record>)``: the record handle is provenance but the authored
    display text is body prose (gr493181 — it used to vanish with the link)."""
    out = _docx_text("read [the dossier](dr173020) next")
    assert out == "read the dossier next"
    out = _docx_text("read [the intro](dc1) next")
    assert out == "read the intro next"
