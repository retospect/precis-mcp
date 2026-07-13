"""DB round-trip tests for the secrets vault (migration 0059).

Self-provisions ``pgcrypto`` + a session ``app.secret_key`` at the database
level; skips cleanly where pgcrypto can't be created (non-superuser test DB
without it pre-installed).
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from precis import secrets as vault
from precis.store import Store
from tests.conftest import _active_dsn

_TEST_KEY = "test-vault-key-0123456789"


@pytest.fixture
def vault_store() -> Iterator[Store]:
    """A Store whose connections carry app.secret_key as a session-local
    startup option (no shared-DB mutation — safe under xdist). Skips if
    pgcrypto is unavailable and uncreatable here."""
    dsn = _active_dsn()
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("pgcrypto not installed and not creatable as the test role")
    # `-c app.secret_key=...` sets the GUC at session start for every pool
    # connection — allowed for any role (custom placeholder), and isolated to
    # this pool so parallel workers on the shared DB are unaffected.
    keyed_dsn = make_conninfo(dsn, options=f"-c app.secret_key={_TEST_KEY}")
    store = Store.connect(keyed_dsn)
    try:
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM vault.secrets")
            conn.execute("DELETE FROM vault.events")
            conn.commit()
        yield store
    finally:
        store.close()


@pytest.fixture(autouse=True)
def _no_env_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_secret is env-override-wins; clear the names these tests use so the
    vault path is what's exercised (the container env sets PERPLEXITY_API_KEY)."""
    for n in ("PERPLEXITY_API_KEY", "SOME_TOKEN", "A_KEY", "PIN", "TMP", "AUDIT_ME"):
        monkeypatch.delenv(n, raising=False)
    vault.invalidate()


def test_set_get_roundtrip(vault_store: Store) -> None:
    vault.set_secret("PERPLEXITY_API_KEY", "pk-live-abcdef123456", store=vault_store)
    assert (
        vault.get_secret("PERPLEXITY_API_KEY", store=vault_store)
        == "pk-live-abcdef123456"
    )


def test_stored_value_is_encrypted(vault_store: Store) -> None:
    secret = "super-secret-value-xyz"
    vault.set_secret("SOME_TOKEN", secret, store=vault_store)
    with vault_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ciphertext, hint FROM vault.secrets WHERE name = %s",
            ("SOME_TOKEN",),
        ).fetchone()
    assert row is not None
    ciphertext, hint = row
    # Ciphertext is bytea and does not contain the plaintext.
    assert secret.encode() not in bytes(ciphertext)
    # Hint is masked — reveals at most the ends, never the middle.
    assert hint != secret
    assert secret not in hint


def test_list_is_masked(vault_store: Store) -> None:
    vault.set_secret("A_KEY", "abcdefghijklmnop", store=vault_store)
    rows = vault.list_secrets(store=vault_store)
    names = {r["name"] for r in rows}
    assert "A_KEY" in names
    row = next(r for r in rows if r["name"] == "A_KEY")
    assert "abcdefghijklmnop" not in str(row["hint"])


def test_short_secret_fully_masked(vault_store: Store) -> None:
    vault.set_secret("PIN", "1234", store=vault_store)
    row = next(r for r in vault.list_secrets(store=vault_store) if r["name"] == "PIN")
    assert "1234" not in str(row["hint"])  # under 12 chars ⇒ no chars revealed


def test_delete(vault_store: Store) -> None:
    vault.set_secret("TMP", "to-be-removed-soon", store=vault_store)
    vault.delete_secret("TMP", store=vault_store)
    assert vault.get_secret("TMP", store=vault_store, default="gone") == "gone"


def test_reveal_writes_audit(vault_store: Store) -> None:
    vault.set_secret("AUDIT_ME", "value-to-reveal-12345", store=vault_store)
    vault.invalidate("AUDIT_ME")  # force a real reveal, not a cache hit
    vault.get_secret("AUDIT_ME", store=vault_store)
    with vault_store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM vault.events WHERE name = %s AND verb = 'reveal'",
            ("AUDIT_ME",),
        ).fetchone()
    assert n is not None and n[0] >= 1
