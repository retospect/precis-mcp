"""Contract tests for :class:`precis.handlers.anki.AnkiHandler`.

Slice 1 is the corpus half: put-create stores cloze markup in ``refs.title``,
the generic Anki note shape in ``ref.meta``, and emits a ``card_combined``
chunk built from the *markup-stripped* text so the embed + chunk_keywords
workers index the natural sentence (a query for "capital" matches a card that
hides ``{{c1::capital}}``). No AnkiWeb dependency yet.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.anki import AnkiHandler, _split_extra, _strip_cloze


def _make_handler(store):
    return AnkiHandler(hub=Hub(store=store))


class TestClozeHelpers:
    def test_strip_cloze_drops_markup_keeps_answer(self) -> None:
        assert (
            _strip_cloze("Paris is the {{c1::capital}} of France.")
            == "Paris is the capital of France."
        )

    def test_strip_cloze_drops_hint(self) -> None:
        assert _strip_cloze("The {{c1::heart::organ}} pumps blood.") == (
            "The heart pumps blood."
        )

    def test_strip_cloze_multiple_indices(self) -> None:
        assert _strip_cloze("The {{c1::heart}} pumps {{c2::blood}}.") == (
            "The heart pumps blood."
        )

    def test_split_extra_none(self) -> None:
        assert _split_extra("Just a {{c1::card}}.") == ("Just a {{c1::card}}.", "")

    def test_split_extra_present(self) -> None:
        body = "The {{c1::Krebs}} cycle.\n---\naka TCA cycle"
        assert _split_extra(body) == ("The {{c1::Krebs}} cycle.", "aka TCA cycle")


class TestAnkiCard:
    def test_put_emits_stripped_card_combined(self, store) -> None:
        h = _make_handler(store)
        resp = h.put(text="Paris is the {{c1::capital}} of France.")
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT chunk_kind, text FROM chunks WHERE ref_id = %s AND ord = -1",
                (ref_id,),
            ).fetchone()
        assert row is not None, "expected a card_combined chunk at ord=-1"
        assert row[0] == "card_combined"
        # The embedded/searchable card is the natural sentence — markup gone.
        assert row[1] == "Paris is the capital of France."

    def test_meta_carries_generic_note_shape(self, store) -> None:
        h = _make_handler(store)
        cloze = "The mitochondrion is the {{c1::powerhouse}} of the cell."
        resp = h.put(text=cloze)
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

        ref = store.get_ref(kind="anki", id=ref_id)
        assert ref is not None
        assert ref.title == cloze  # raw markup preserved in the body
        assert ref.meta["notetype"] == "Cloze"
        assert ref.meta["deck"] == "Precis"
        assert ref.meta["fields"]["Text"] == cloze
        assert "Back Extra" not in ref.meta["fields"]

    def test_back_extra_split_and_stored(self, store) -> None:
        h = _make_handler(store)
        body = "The {{c1::Krebs}} cycle occurs in the matrix.\n---\naka TCA cycle"
        resp = h.put(text=body)
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

        ref = store.get_ref(kind="anki", id=ref_id)
        assert ref.meta["fields"]["Text"] == (
            "The {{c1::Krebs}} cycle occurs in the matrix."
        )
        assert ref.meta["fields"]["Back Extra"] == "aka TCA cycle"

        with store.pool.connection() as conn:
            card_text = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1",
                (ref_id,),
            ).fetchone()[0]
        # Card text = stripped cloze + the Back Extra, both searchable.
        assert "Krebs cycle occurs in the matrix" in card_text
        assert "aka TCA cycle" in card_text

    def test_deck_tag_sets_subdeck(self, store) -> None:
        h = _make_handler(store)
        resp = h.put(
            text="Beijing is the {{c1::capital}} of China.", tags=["deck-chinese"]
        )
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))
        ref = store.get_ref(kind="anki", id=ref_id)
        assert ref.meta["deck"] == "Precis::chinese"

    def test_no_deck_tag_defaults_to_precis(self, store) -> None:
        h = _make_handler(store)
        resp = h.put(text="A {{c1::plain}} card.")
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))
        assert store.get_ref(kind="anki", id=ref_id).meta["deck"] == "Precis"

    def test_non_cloze_body_rejected(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput):
            h.put(text="This sentence has no cloze deletion.")

    def test_rejected_card_writes_nothing(self, store) -> None:
        """A non-cloze put leaves no ghost ref behind (atomic-create contract)."""
        h = _make_handler(store)
        before = len(store.list_refs(kind="anki", limit=1000))
        with pytest.raises(BadInput):
            h.put(text="no cloze here")
        after = len(store.list_refs(kind="anki", limit=1000))
        assert after == before


class TestLeeches:
    """`get(kind='anki', id='/leeches')` surfaces bad-recall cards (high lapses
    or collapsed ease) from `meta.anki_stats` — the fix-cloze-or-study loop."""

    def _seed(self, store, title, stats):
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="anki",
                slug=None,
                title=title,
                meta={
                    "notetype": "Cloze",
                    "deck": "Precis",
                    "fields": {"Text": title},
                    "anki_stats": stats,
                },
                conn=conn,
            )
        return ref.id

    def test_leeches_lists_bad_recall_only(self, store) -> None:
        h = _make_handler(store)
        leech = self._seed(
            store,
            "hard {{c1::sesquipedalian}}",
            {"lapses_total": 6, "ease_min": 1.8, "reps_total": 20},
        )
        good = self._seed(
            store,
            "easy {{c1::cat}}",
            {"lapses_total": 0, "ease_min": 2.5, "reps_total": 3},
        )
        out = h.get(id="/leeches")
        assert f"ak{leech}" in out.body
        assert f"ak{good}" not in out.body
        assert "fix the cloze" in out.body
