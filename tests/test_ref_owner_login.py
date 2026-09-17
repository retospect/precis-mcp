"""``refs.owner_login`` against real Postgres (migration 0164).

Real store, not a FakeStore: the FK to ``web_users.login`` and the
``ON DELETE SET NULL`` behaviour only exist in the DB, not in any
Python-side fake. First consumer is ``kind='anki'`` (each card belongs
to the AnkiWeb account it syncs to), so that's the kind used here.
"""

from __future__ import annotations

import psycopg
import pytest

from precis.errors import BadInput
from precis.store import Store
from precis.users import hash_password


def _make_user(store: Store, login: str = "reto", abbrev: str = "rs") -> None:
    store.create_web_user(login=login, abbrev=abbrev, password=hash_password("pw"))


def test_insert_with_owner_roundtrips_through_get_and_list(store: Store) -> None:
    _make_user(store)
    ref = store.insert_ref(kind="anki", slug=None, title="card 1", owner_login="reto")
    assert ref.owner_login == "reto"

    fetched = store.get_ref(kind="anki", id=ref.id)
    assert fetched is not None
    assert fetched.owner_login == "reto"

    listed = store.list_refs(kind="anki", owner_login="reto")
    assert [r.id for r in listed] == [ref.id]


def test_insert_without_owner_defaults_to_unowned(store: Store) -> None:
    ref = store.insert_ref(kind="anki", slug=None, title="card 2")
    assert ref.owner_login is None


def test_unowned_excludes_owned_rows(store: Store) -> None:
    _make_user(store)
    owned = store.insert_ref(kind="anki", slug=None, title="owned", owner_login="reto")
    unowned = store.insert_ref(kind="anki", slug=None, title="unowned")

    listed = store.list_refs(kind="anki", unowned=True)
    ids = {r.id for r in listed}
    assert unowned.id in ids
    assert owned.id not in ids


def test_owner_login_and_unowned_together_is_bad_input(store: Store) -> None:
    _make_user(store)
    with pytest.raises(BadInput):
        store.list_refs(kind="anki", owner_login="reto", unowned=True)


def test_claim_unowned_refs_claims_only_the_kind_unowned_live_rows(
    store: Store,
) -> None:
    _make_user(store)
    already_owned = store.insert_ref(
        kind="anki", slug=None, title="already owned", owner_login="reto"
    )
    unowned_anki = store.insert_ref(kind="anki", slug=None, title="claim me")
    unowned_other_kind = store.insert_ref(kind="memory", slug=None, title="not anki")
    deleted = store.insert_ref(kind="anki", slug=None, title="deleted")
    store.retire_ref(deleted.id)

    claimed = store.claim_unowned_refs("anki", "reto")
    assert claimed == 1

    assert store.get_ref(kind="anki", id=unowned_anki.id).owner_login == "reto"  # type: ignore[union-attr]
    assert store.get_ref(kind="anki", id=already_owned.id).owner_login == "reto"  # type: ignore[union-attr]
    assert store.get_ref(kind="memory", id=unowned_other_kind.id).owner_login is None  # type: ignore[union-attr]

    # Re-claiming is a no-op — nothing left unowned for this kind.
    assert store.claim_unowned_refs("anki", "reto") == 0


def test_set_ref_owner_sets_and_clears(store: Store) -> None:
    _make_user(store)
    ref = store.insert_ref(kind="anki", slug=None, title="card")
    assert store.set_ref_owner(ref.id, "reto")
    assert store.get_ref(kind="anki", id=ref.id).owner_login == "reto"  # type: ignore[union-attr]

    assert store.set_ref_owner(ref.id, None)
    assert store.get_ref(kind="anki", id=ref.id).owner_login is None  # type: ignore[union-attr]


def test_set_ref_owner_returns_false_for_missing_ref(store: Store) -> None:
    _make_user(store)
    assert not store.set_ref_owner(999_999_999, "reto")


def test_fk_rejects_an_unknown_login(store: Store) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.insert_ref(kind="anki", slug=None, title="card", owner_login="nobody")


def test_deleting_the_web_user_nulls_the_owner(store: Store) -> None:
    _make_user(store)
    ref = store.insert_ref(kind="anki", slug=None, title="card", owner_login="reto")
    assert store.delete_web_user("reto")

    fetched = store.get_ref(kind="anki", id=ref.id)
    assert fetched is not None
    assert fetched.owner_login is None
