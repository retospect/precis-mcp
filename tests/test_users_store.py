"""``web_users`` CRUD against real Postgres.

The route-level tests fake the store, which by construction can't catch
a typo'd column or a constraint that doesn't do what the migration
claims. These run the SQL, so migration 0131 is exercised too: the
lowercase CHECKs, both UNIQUEs, and the disable/enable toggle.
"""

from __future__ import annotations

import psycopg
import pytest

from precis.store import Store
from precis.users import hash_password, mint_feed_token


def _make(store: Store, login: str = "reto", abbrev: str = "rs", **kw):
    return store.create_web_user(
        login=login, abbrev=abbrev, password=hash_password("pw"), **kw
    )


def test_create_and_read_back(store: Store) -> None:
    user = _make(store, full_name="Reto Stamm", email="Reto@Example.COM")
    assert user.login == "reto"
    assert user.abbrev == "rs"
    assert user.enabled

    fetched = store.get_web_user("RETO")  # case-folded on the way in
    assert fetched is not None
    assert fetched.full_name == "Reto Stamm"
    assert fetched.email == "reto@example.com"
    assert store.count_web_users() == 1


def test_credentials_roundtrip_through_the_columns(store: Store) -> None:
    from precis.users import verify_password

    record = hash_password("s3cret", pepper="pep")
    store.create_web_user(login="reto", abbrev="rs", password=record)
    found = store.get_web_user_credentials("reto")
    assert found is not None
    _, stored = found
    assert verify_password("s3cret", stored, pepper="pep")


def test_duplicate_login_is_refused(store: Store) -> None:
    """An accidental second ``users add`` must not silently reset the
    existing account's password."""
    _make(store)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _make(store, abbrev="r2")


def test_duplicate_abbrev_is_refused(store: Store) -> None:
    _make(store)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _make(store, login="other")


def test_disable_hides_from_the_enabled_count_but_keeps_the_row(
    store: Store,
) -> None:
    _make(store)
    assert store.set_web_user_disabled("reto", disabled=True)
    assert store.count_web_users() == 0
    assert store.count_web_users(enabled_only=False) == 1
    user = store.get_web_user("reto")
    assert user is not None and not user.enabled

    assert store.set_web_user_disabled("reto", disabled=False)
    assert store.count_web_users() == 1


def test_password_change(store: Store) -> None:
    from precis.users import verify_password

    _make(store)
    assert store.set_web_user_password("reto", hash_password("new"))
    found = store.get_web_user_credentials("reto")
    assert found is not None
    assert verify_password("new", found[1])
    assert not store.set_web_user_password("nobody", hash_password("x"))


def test_update_display_fields(store: Store) -> None:
    _make(store)
    assert store.update_web_user("reto", full_name="R. Stamm", email="R@X.COM")
    user = store.get_web_user("reto")
    assert user is not None
    assert user.full_name == "R. Stamm"
    assert user.email == "r@x.com"
    # No fields given → nothing to do, and say so rather than issuing a
    # no-op UPDATE that would report success.
    assert not store.update_web_user("reto")


def test_feed_token_lookup_and_rotation(store: Store) -> None:
    _make(store)
    token, digest = mint_feed_token()
    assert store.set_web_user_feed_token("reto", digest)
    found = store.get_web_user_by_feed_token(digest)
    assert found is not None and found.login == "reto"

    _, second = mint_feed_token()
    store.set_web_user_feed_token("reto", second)
    assert store.get_web_user_by_feed_token(digest) is None
    assert store.get_web_user_by_feed_token(second) is not None


def test_disabled_user_feed_token_stops_working(store: Store) -> None:
    """Disabling an account must kill its podcast feed in the same
    keystroke — otherwise the token outlives the login."""
    _make(store)
    _, digest = mint_feed_token()
    store.set_web_user_feed_token("reto", digest)
    store.set_web_user_disabled("reto", disabled=True)
    assert store.get_web_user_by_feed_token(digest) is None


def test_touch_last_login(store: Store) -> None:
    _make(store)
    assert store.get_web_user("reto").last_login_at is None  # type: ignore[union-attr]
    store.touch_web_user_login("reto")
    assert store.get_web_user("reto").last_login_at is not None  # type: ignore[union-attr]


def test_delete(store: Store) -> None:
    _make(store)
    assert store.delete_web_user("reto")
    assert store.get_web_user("reto") is None
    assert not store.delete_web_user("reto")


def test_roster_orders_enabled_first(store: Store) -> None:
    _make(store, login="alice", abbrev="al")
    _make(store, login="bob", abbrev="bo")
    store.set_web_user_disabled("alice", disabled=True)
    assert [u.login for u in store.list_web_users()] == ["bob", "alice"]
