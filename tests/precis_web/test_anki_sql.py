"""Real-PG regression tests for the ``/anki`` route's raw SQL
(``precis_web.routes.anki``) — the route-level ``test_anki.py`` runs against
a fake store that never parses SQL, so ``_counts`` / ``_forge_groups`` /
``_concept_cards`` are exercised here against the live ``store`` fixture.
"""

from __future__ import annotations

import uuid
from typing import Any

from precis.reading.flashcards import author_card
from precis.reading.promote import create_concept
from precis.users import hash_password
from precis_web.routes.anki import _concept_cards, _counts, _forge_groups


def _user(store: Any, login: str = "reto") -> str:
    try:
        store.create_web_user(
            login=login, abbrev=login[:2], password=hash_password("pw")
        )
    except Exception:
        pass  # fixture user may already exist from another test
    return login


def _forge_card(store: Any, concept_id: int, owner: str) -> int:
    card_id = author_card(
        store,
        text=f"The answer is {{{{c1::{uuid.uuid4().hex[:6]}}}}}.",
        concept_id=concept_id,
        deck="Precis::reading",
    )
    store.set_ref_owner(card_id, owner)
    return card_id


def test_counts_split_cards_and_concepts(store: Any) -> None:
    owner = _user(store)
    before = _counts(store, owner)
    cid = create_concept(store, name=f"cnt-{uuid.uuid4().hex[:8]}")
    card_id = _forge_card(store, cid, owner)
    store.update_ref(
        card_id,
        meta_patch={"anki_stats": {"lapses_total": 9, "ease_min": 1.5}},
    )
    after = _counts(store, owner)
    assert after["cards"] == before["cards"] + 1
    assert after["forge"] == before["forge"] + 1
    assert after["leeches"] == before["leeches"] + 1
    assert after["active"] >= before["active"] + 1


def test_forge_groups_group_cards_by_concept(store: Any) -> None:
    owner = _user(store)
    cid = create_concept(store, name=f"grp-{uuid.uuid4().hex[:8]}")
    c1 = _forge_card(store, cid, owner)
    c2 = _forge_card(store, cid, owner)
    groups = _forge_groups(store, owner)
    g = next(g for g in groups if g["concept_id"] == cid)
    assert {c["id"] for c in g["cards"]} == {c1, c2}
    assert g["name"].startswith("grp-")

    # Another owner's card on the same concept must not leak in.
    other = _user(store, login="alice")
    _forge_card(store, cid, other)
    g2 = next(g for g in _forge_groups(store, owner) if g["concept_id"] == cid)
    assert {c["id"] for c in g2["cards"]} == {c1, c2}


def test_concept_cards_scopes_to_owner(store: Any) -> None:
    owner = _user(store)
    other = _user(store, login="alice")
    cid = create_concept(store, name=f"own-{uuid.uuid4().hex[:8]}")
    mine = _forge_card(store, cid, owner)
    theirs = _forge_card(store, cid, other)
    assert _concept_cards(store, cid, owner) == [mine]
    assert theirs not in _concept_cards(store, cid, owner)
