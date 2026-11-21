"""Real-PG regression tests for the ``/anki`` route's raw SQL
(``precis_web.routes.anki``) — the route-level ``test_anki.py`` runs against
a fake store that never parses SQL, so ``_counts`` / ``_card_groups`` /
the known/undo transaction are exercised here against the live ``store`` fixture.
"""

from __future__ import annotations

import uuid
from typing import Any

from precis.reading.flashcards import author_card
from precis.reading.known import known_undo_entries, mark_known, undo_known
from precis.reading.promote import create_concept
from precis.users import hash_password
from precis_web.routes.anki import _card_groups, _counts


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


def _agent_card(
    store: Any, concept_id: int, owner: str, *, source: str | None = None
) -> int:
    """A card the way ``AnkiHandler._create`` writes it (meta shape without
    ``authored_by``) — what an agent's MCP put leaves behind."""
    text = f"Agent says {{{{c1::{uuid.uuid4().hex[:6]}}}}}."
    meta: dict[str, Any] = {
        "notetype": "Cloze",
        "deck": "Precis::reading",
        "fields": {"Text": text},
    }
    if source is not None:
        meta["source"] = source
    ref = store.insert_ref(
        kind="anki", slug=None, title=text, meta=meta, owner_login=owner
    )
    store.add_link(src_ref_id=concept_id, dst_ref_id=ref.id, relation="represents")
    return ref.id


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
    groups = _card_groups(store, owner, forge_only=True)
    g = next(g for g in groups if g["concept_id"] == cid)
    assert {c["id"] for c in g["cards"]} == {c1, c2}
    assert g["name"].startswith("grp-")

    # Another owner's card on the same concept must not leak in.
    other = _user(store, login="alice")
    _forge_card(store, cid, other)
    g2 = next(
        g for g in _card_groups(store, owner, forge_only=True) if g["concept_id"] == cid
    )
    assert {c["id"] for c in g2["cards"]} == {c1, c2}


def test_learning_groups_include_agent_cards_but_exclude_foreign(store: Any) -> None:
    """gr742681: /learning's Current context lists every live precis-authored
    concept card — an agent-put card (no ``authored_by``) shows there but not
    on forge-only /anki; a read-only ``anki-foreign`` projection shows on
    neither."""
    owner = _user(store)
    cid = create_concept(store, name=f"agent-{uuid.uuid4().hex[:8]}")
    forge = _forge_card(store, cid, owner)
    agent = _agent_card(store, cid, owner)
    foreign = _agent_card(store, cid, owner, source="anki-foreign")

    learning = next(
        g
        for g in _card_groups(store, owner, forge_only=False)
        if g["concept_id"] == cid
    )
    assert {c["id"] for c in learning["cards"]} == {forge, agent}
    assert foreign not in {c["id"] for c in learning["cards"]}

    anki = next(
        g for g in _card_groups(store, owner, forge_only=True) if g["concept_id"] == cid
    )
    assert {c["id"] for c in anki["cards"]} == {forge}


def test_known_undo_restores_only_its_cards_and_prior_mastery(store: Any) -> None:
    owner = store.get_web_user(_user(store))
    other = store.get_web_user(_user(store, login="alice"))
    cid = create_concept(store, name=f"own-{uuid.uuid4().hex[:8]}")
    prior = {
        "state": "active",
        "mastery": 0.35,
        "mastery_updated_at": "2026-11-20T00:00:00Z",
    }
    store.update_ref(cid, meta_patch=prior)
    mine = _forge_card(store, cid, owner.login)
    dropped = _forge_card(store, cid, owner.login)
    theirs = _forge_card(store, cid, other.login)
    redropped = _forge_card(store, cid, owner.login)
    store.retire_ref(dropped)

    assert mark_known(store, owner, [cid, cid], require_cards=True) == 2
    assert mark_known(store, owner, [cid], require_cards=True) == 0
    assert store.get_ref(kind="concept", id=cid).meta["state"] == "mastered"
    assert store.get_ref(kind="anki", id=mine) is None
    assert store.get_ref(kind="anki", id=theirs) is not None
    [(entry_id, _, token)] = known_undo_entries(store, owner)
    assert entry_id == cid
    assert known_undo_entries(store, other) == []
    assert not undo_known(store, other, cid, token)

    assert store.restore_ref(redropped)
    store.retire_ref(redropped)
    store.update_ref(cid, meta_patch={"definition": "Updated after dismissal."})
    assert undo_known(store, owner, cid, token)
    assert not undo_known(store, owner, cid, token)
    assert store.get_ref(kind="anki", id=mine).meta["anki_restore_pending"] is True
    assert store.get_ref(kind="anki", id=dropped) is None
    assert store.get_ref(kind="anki", id=redropped) is None
    meta = store.get_ref(kind="concept", id=cid).meta
    assert {key: meta[key] for key in prior} == prior
    assert meta["definition"] == "Updated after dismissal."


def test_overlapping_owners_share_the_original_mastery_snapshot(store: Any) -> None:
    first = store.get_web_user(_user(store))
    second = store.get_web_user(_user(store, login="alice"))
    cid = create_concept(store, name=f"overlap-{uuid.uuid4().hex[:8]}")
    mine = _forge_card(store, cid, first.login)
    theirs = _forge_card(store, cid, second.login)
    before = store.get_ref(kind="concept", id=cid).meta

    assert mark_known(store, first, [cid], require_cards=True) == 1
    [(_, _, first_token)] = known_undo_entries(store, first)
    assert mark_known(store, second, [cid], require_cards=True) == 1
    [(_, _, second_token)] = known_undo_entries(store, second)
    assert undo_known(store, first, cid, first_token)
    assert store.get_ref(kind="concept", id=cid).meta["state"] == "mastered"
    assert store.get_ref(kind="anki", id=mine) is not None
    assert store.get_ref(kind="anki", id=theirs) is None

    assert undo_known(store, second, cid, second_token)
    assert store.get_ref(kind="anki", id=theirs) is not None
    assert store.get_ref(kind="concept", id=cid).meta == before
