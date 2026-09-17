"""Tests for per-user AnkiWeb credentials in the secrets vault
(:mod:`precis.anki.creds`).

``vaulted_store`` builds a second :class:`Store` pointed at the *same*
(already-truncated, per-test-isolated) database as the shared ``store``
fixture, but with ``app.secret_key`` baked into every pooled connection's
startup options — the same trick :mod:`tests.test_secrets_vault` uses —
so ``set_secret``/``get_secret`` exercise the real pgcrypto round trip
instead of falling through to "vault unavailable".
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from precis.anki.creds import (
    anki_logins,
    clear_user_credentials,
    get_user_credentials,
    password_secret,
    set_user_credentials,
    user_anki_configured,
    user_secret,
)
from precis.store import Store
from precis.users import hash_password

_TEST_KEY = "test-anki-creds-vault-key-0123456789"


@pytest.fixture
def vaulted_store(store: Store) -> Iterator[Store]:
    dsn = store.dsn
    assert dsn is not None
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("pgcrypto not installed and not creatable as the test role")
    keyed_dsn = make_conninfo(dsn, options=f"-c app.secret_key={_TEST_KEY}")
    vstore = Store.connect(keyed_dsn)
    try:
        yield vstore
    finally:
        vstore.close()


def _make_user(store: Store, login: str = "reto", abbrev: str = "rs") -> None:
    store.create_web_user(login=login, abbrev=abbrev, password=hash_password("pw"))


class TestSecretNames:
    def test_names_are_colon_suffixed(self) -> None:
        assert user_secret("reto") == "ANKI_USER:reto"
        assert password_secret("reto") == "ANKI_PASSWORD:reto"


class TestCredentialLifecycle:
    def test_unconfigured_by_default(self, vaulted_store: Store) -> None:
        assert not user_anki_configured(vaulted_store, "reto")
        assert get_user_credentials(vaulted_store, "reto") is None

    def test_set_then_get_roundtrips(self, vaulted_store: Store) -> None:
        set_user_credentials(vaulted_store, "reto", "reto@example.com", "hunter2")
        assert user_anki_configured(vaulted_store, "reto")
        assert get_user_credentials(vaulted_store, "reto") == (
            "reto@example.com",
            "hunter2",
        )

    def test_clear_removes_both(self, vaulted_store: Store) -> None:
        set_user_credentials(vaulted_store, "reto", "reto@example.com", "hunter2")
        assert clear_user_credentials(vaulted_store, "reto")
        assert not user_anki_configured(vaulted_store, "reto")
        assert get_user_credentials(vaulted_store, "reto") is None

    def test_two_users_are_independent(self, vaulted_store: Store) -> None:
        set_user_credentials(vaulted_store, "reto", "reto@example.com", "pw1")
        set_user_credentials(vaulted_store, "alice", "alice@example.com", "pw2")
        assert get_user_credentials(vaulted_store, "reto") == (
            "reto@example.com",
            "pw1",
        )
        assert get_user_credentials(vaulted_store, "alice") == (
            "alice@example.com",
            "pw2",
        )
        clear_user_credentials(vaulted_store, "reto")
        assert get_user_credentials(vaulted_store, "reto") is None
        assert get_user_credentials(vaulted_store, "alice") == (
            "alice@example.com",
            "pw2",
        )


class TestAnkiLogins:
    def test_no_web_users_is_empty(self, vaulted_store: Store) -> None:
        assert anki_logins(vaulted_store) == []

    def test_web_user_without_credentials_is_excluded(
        self, vaulted_store: Store
    ) -> None:
        _make_user(vaulted_store, "reto")
        assert anki_logins(vaulted_store) == []

    def test_configured_user_is_included_sorted(self, vaulted_store: Store) -> None:
        _make_user(vaulted_store, "reto", "rs")
        _make_user(vaulted_store, "alice", "al")
        set_user_credentials(vaulted_store, "reto", "reto@example.com", "pw1")
        set_user_credentials(vaulted_store, "alice", "alice@example.com", "pw2")
        assert anki_logins(vaulted_store) == ["alice", "reto"]

    def test_disabled_user_is_excluded_even_when_configured(
        self, vaulted_store: Store
    ) -> None:
        _make_user(vaulted_store, "reto", "rs")
        set_user_credentials(vaulted_store, "reto", "reto@example.com", "pw1")
        vaulted_store.set_web_user_disabled("reto", disabled=True)
        assert anki_logins(vaulted_store) == []
