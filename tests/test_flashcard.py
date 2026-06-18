"""Contract tests for :class:`precis.handlers.flashcard.FlashcardHandler`.

The behaviour under test is that put-create emits a ``card_combined``
chunk (ord=-1) holding the knowledge statement, so the embed +
chunk_keywords workers index it. Before ``emits_card`` was turned on,
the statement lived only in ``refs.title`` — lexically searchable, but
never embedded or keyword-extracted.
"""

from __future__ import annotations

import re

from precis.dispatch import Hub
from precis.handlers.flashcard import FlashcardHandler


def _make_handler(store):
    return FlashcardHandler(hub=Hub(store=store))


class TestKnowledgeCard:
    def test_put_emits_card_combined(self, store) -> None:
        h = _make_handler(store)
        statement = (
            "The Krebs cycle (citric acid cycle) oxidises acetyl-CoA to CO2, "
            "transferring electrons to NAD+ and FAD across eight enzymatic steps."
        )
        resp = h.put(text=statement)
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

        with store.pool.connection() as conn:
            card = conn.execute(
                "SELECT chunk_kind, text FROM chunks WHERE ref_id = %s AND ord = -1",
                (ref_id,),
            ).fetchone()
        assert card is not None, "expected a card_combined chunk at ord=-1"
        assert card[0] == "card_combined"
        # The full knowledge statement lands in the card verbatim so the
        # embed worker vectorises the whole thing.
        assert card[1] == statement

    def test_card_text_matches_title(self, store) -> None:
        """The card mirrors refs.title — both hold the knowledge statement,
        so lexical (title) and semantic (card) search agree."""
        h = _make_handler(store)
        resp = h.put(text="Avogadro's number is 6.022e23 per mole.")
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

        ref = store.get_ref(kind="flashcard", id=ref_id)
        assert ref is not None
        with store.pool.connection() as conn:
            card_text = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1",
                (ref_id,),
            ).fetchone()[0]
        assert card_text == ref.title
