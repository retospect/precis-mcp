"""Brace-aware cloze scanning + the write-door `}}` spacing (anki cards)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.anki.cloze import (
    acronym_hint_leaks,
    cloze_hints,
    has_cloze,
    space_adjacent_braces,
    strip_cloze,
)
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.anki import AnkiHandler

ACK = "inside cloze answers so Anki does not cut the answer"


def _h(store):
    return AnkiHandler(hub=Hub(store=store))


def _ref_id(body: str) -> int:
    m = re.search(r"id=(\d+)", body)
    assert m is not None
    return int(m.group(1))


def _put(store, text):
    resp = _h(store).put(text=text)
    ref_id = _ref_id(resp.body)
    ref = store.get_ref(kind="anki", id=ref_id)
    with store.pool.connection() as conn:
        card = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1", (ref_id,)
        ).fetchone()[0]
    return resp.body, ref.meta["fields"]["Text"], card


@pytest.mark.parametrize(
    ("raw", "stored", "stripped"),
    [
        (
            r"{{c1::\sqrt{x^{2}}::latex}}",
            r"{{c1::\sqrt{x^{2} }::latex}}",
            r"\sqrt{x^{2} }",
        ),
        (r"{{c1::\frac{a}{b}}}", r"{{c1::\frac{a}{b} }}", r"\frac{a}{b} "),
    ],
)
def test_put_spaces_and_acks(store, raw, stored, stripped) -> None:
    body, text, card = _put(store, raw)
    assert text == stored
    assert card.strip() == stripped.strip()
    assert ACK in body
    assert "spaced 1 `}}`" in body


def test_hint_still_dropped() -> None:
    assert strip_cloze("{{c1::Paris::capital}}") == "Paris"


def test_hint_splits_at_first_separator_like_anki() -> None:
    assert strip_cloze("{{c1::a::b::c}}") == "a"


def test_acronym_hint_initials_split_on_hyphens_and_ignore_case() -> None:
    assert acronym_hint_leaks(
        "{{c1::RMSD::root-mean-square deviation}} and "
        "{{c2::rmsf::Root-Mean-Square Fluctuation}}"
    ) == ["c1", "c2"]
    assert not acronym_hint_leaks(
        "RMSD ({{c1::root-mean-square deviation::expansion}}) "
        r"and {{c2::\sqrt{x^{2}}::latex}}"
    )


def test_put_warns_about_acronym_hint_without_rewriting_card(store: Any) -> None:
    raw = "{{c1::RMSD::root-mean-square deviation}} measures structural change."
    body, text, _ = _put(store, raw + "\n---\n{{c2::ATP::adenosine tri phosphate}}")
    assert "Warning (c1): the hint's initials spell the answer" in body
    assert "Warning (c2)" not in body
    assert text == raw


def test_unbalanced_brace_answer_closes_at_first_pair() -> None:
    text = "A block opens with {{c1::{}}."
    assert has_cloze(text)
    assert space_adjacent_braces(text) == (text, 0)
    assert strip_cloze(text) == "A block opens with {."


def test_multiple_clozes_in_one_sentence(store) -> None:
    raw = r"{{c1::\sqrt{x^{2}}}} and {{c2::\frac{a}{b}}} and {{c3::plain}}."
    body, text, _ = _put(store, raw)
    assert text == r"{{c1::\sqrt{x^{2} } }} and {{c2::\frac{a}{b} }} and {{c3::plain}}."
    assert "spaced 3 `}}`" in body
    assert strip_cloze(text) == r"\sqrt{x^{2} }  and \frac{a}{b}  and plain."


def test_nested_cloze_unchanged(store) -> None:
    raw = "{{c1::a {{c2::b}} c}}"
    body, text, card = _put(store, raw)
    assert text == raw
    assert ACK not in body
    assert card == "a b c"


def test_already_spaced_unchanged_no_ack(store) -> None:
    raw = r"{{c1::\sqrt{x^{2} }::latex}}"
    body, text, _ = _put(store, raw)
    assert text == raw
    assert ACK not in body


def test_idempotent() -> None:
    once, n = space_adjacent_braces(r"{{c1::\sqrt{x^{2}}::latex}}")
    assert n == 1
    assert space_adjacent_braces(once) == (once, 0)


def test_outside_cloze_untouched() -> None:
    raw = r"$x^{2}}$ {{c1::a}}"
    assert space_adjacent_braces(raw) == (raw, 0)


def test_unterminated_left_alone() -> None:
    raw = r"ok {{c1::a}} then {{c2::\frac{a}{b}"
    out, n = space_adjacent_braces(raw)
    assert out == raw and n == 0
    assert not has_cloze("{{c1::open")


def test_back_extra_not_touched(store) -> None:
    body, text, _ = _put(store, "{{c1::\\frac{a}{b}}}\n---\nnote {x}}")
    assert text == r"{{c1::\frac{a}{b} }}"
    meta = store.get_ref(kind="anki", id=_ref_id(body)).meta
    assert meta["fields"]["Back Extra"] == "note {x}}"


def test_no_cloze_still_bad_input(store) -> None:
    with pytest.raises(BadInput):
        _h(store).put(text=r"\sqrt{x^{2}} has no cloze")


# ── cloze_hints (put-time lint source) ───────────────────────────────


def test_cloze_hints_reports_hint_and_position() -> None:
    text = "The {{c1::heart::organ}} pumps {{c2::blood}}."
    assert cloze_hints(text) == [
        (text.index("{{c1"), "organ"),
        (text.index("{{c2"), ""),
    ]


def test_cloze_hints_latex_answer_with_braces() -> None:
    text = r"{{c1::\sqrt{x^{2}}::latex}} is the root"
    [(start, hint)] = cloze_hints(text)
    assert hint == "latex"
    assert text[start:].startswith("{{c1::")


def test_cloze_hints_unterminated_left_alone() -> None:
    assert cloze_hints("ok {{c1::a}} then {{c2::open") == [(3, "")]


# ── put-time hint/lead-in lint (gr743086) ────────────────────────────

WARN = "cut the lead-in scaffolding"


def test_put_warns_when_lead_in_repeats_hint(store) -> None:
    body, _text, _card = _put(
        store,
        "The term {{c1::available::term}} means ready for use.",
    )
    assert WARN in body
    assert "'term'" in body


def test_put_warns_on_synonym_lead_in(store) -> None:
    body, _text, _card = _put(store, "The character {{c1::字::kanji}}.")
    assert WARN in body


def test_put_silent_on_clean_card(store) -> None:
    body, _text, _card = _put(
        store,
        "{{c1::Available::term}} means capable of being {{c2::obtained::action}}.",
    )
    assert WARN not in body


def test_put_silent_when_earlier_cloze_hint_sits_in_lead_window(store) -> None:
    # The first cloze's *hint* token must not count as prose for the second.
    body, _text, _card = _put(
        store,
        "{{c1::Mitochondrion::term}} makes {{c2::ATP::molecule}}.",
    )
    assert WARN not in body
