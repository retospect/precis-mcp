"""Brace-aware cloze scanning + the write-door `}}` spacing (anki cards)."""

from __future__ import annotations

import re

import pytest

from precis.anki.cloze import has_cloze, space_adjacent_braces, strip_cloze
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
