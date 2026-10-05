"""DB round-trip tests for the secrets vault (0059) and saved hint counts (0189).

Self-provisions ``pgcrypto`` + a session ``app.secret_key`` at the database
level; skips cleanly where pgcrypto can't be created (non-superuser test DB
without it pre-installed).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from precis import secrets as vault
from precis.store import Store
from tests.conftest import PG_TEST_DSN, _active_dsn, _pg_available

_TEST_KEY = "test-vault-key-0123456789"


@pytest.fixture
def vault_store() -> Iterator[Store]:
    """A Store whose connections carry app.secret_key as a session-local
    startup option (no shared-DB mutation — safe under xdist). Skips when
    no postgres is reachable (e.g. the macOS/Windows CI legs), matching the
    shared ``store``/``hub`` fixtures, and again if pgcrypto is unavailable
    and uncreatable here."""
    if not _pg_available():
        pytest.skip(
            f"postgres unreachable at {PG_TEST_DSN}; set PRECIS_TEST_PG_URL "
            "or start a server to run db-tagged tests"
        )
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


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_web_multiline_create_replace_roundtrip(
    vault_store: Store, monkeypatch: pytest.MonkeyPatch, newline: str
) -> None:
    """HTTP form values survive encryption/reveal; blank replacement is inert."""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from precis_web.routes import secrets as route

    name = "TEST_WEB_MULTILINE"
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(route, "get_store", lambda request: vault_store)
    app = FastAPI()
    app.include_router(route.router)
    with TestClient(app) as client:
        for body in ("FIRST_SYNTHETIC_VALUE", "REPLACEMENT_SYNTHETIC_VALUE"):
            value = newline.join(["BEGIN", body + "😀e\u0301", "", "END", ""])
            response = client.post(
                "/secrets/set",
                data={"name": name, "value": value},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/secrets"
            assert body not in response.text
            vault.invalidate(name)
            assert vault.get_secret(name, store=vault_store) == value
            saved = next(
                r for r in vault.list_secrets(store=vault_store) if r["name"] == name
            )
            assert str(saved["hint"]).endswith(f" · {len(value)} chars · 5 lines")
            assert body not in str(saved["hint"])
        before_blank = next(
            r for r in vault.list_secrets(store=vault_store) if r["name"] == name
        )
        response = client.post(
            "/secrets/set",
            data={"name": name, "value": ""},
            follow_redirects=False,
        )
        assert response.status_code == 303
        vault.invalidate(name)
        assert vault.get_secret(name, store=vault_store) == value
        assert (
            next(r for r in vault.list_secrets(store=vault_store) if r["name"] == name)
            == before_blank
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


@pytest.mark.parametrize(
    "value", ["abc", "😀e\u0301", "\n", "A\rB\r", "A\r\n\r\n", "synthetic\n\nend\n"]
)
def test_saved_hint_counts_and_roundtrip(vault_store: Store, value: str) -> None:
    name = "TEST_COUNTS"
    vault.set_secret(name, value, store=vault_store)
    row = next(r for r in vault.list_secrets(store=vault_store) if r["name"] == name)
    lines = len(re.split(r"\r\n|\r|\n", value))
    assert str(row["hint"]).endswith(f" · {len(value)} chars · {lines} lines")
    assert value not in str(row["hint"])
    assert vault.get_secret(name, store=vault_store) == value


def test_hint_empty_helper_counts(vault_store: Store) -> None:
    with vault_store.pool.connection() as conn:
        assert conn.execute("SELECT vault._hint(''), vault._hint(NULL)").fetchone() == (
            "(empty) · 0 chars · 0 lines",
            "(empty) · 0 chars · 0 lines",
        )


def test_saved_hint_html_is_write_only(
    vault_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from pathlib import Path

    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

    from precis_web.routes import secrets as route

    name = "TEST_COUNTS_HTML"
    value = "BEGIN_SYNTHETIC\nSECRET_SENTINEL😀\n\nEND\n"
    vault.set_secret(name, value, store=vault_store)
    inventory = [r for r in vault.list_secrets(store=vault_store) if r["name"] == name]
    monkeypatch.setattr(route.secret_status, "KNOWN_SECRETS", ())
    rows, _ = route._build_rows(inventory, {}, store=vault_store)
    scratch = Path(__file__).parents[1] / ".scratch"
    scratch.mkdir(exist_ok=True)
    (scratch / "browser-saved-inventory.json").write_text(
        json.dumps(inventory, default=str)
    )
    rows = [r for r in rows if r["name"] == name]
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html.j2": "{% block content %}{% endblock %}"}),
                FileSystemLoader(
                    Path(__file__).parents[1] / "src/precis_web/templates"
                ),
            ]
        ),
        autoescape=True,
    )
    env.filters["ago"] = lambda value: "synthetic time"
    html = env.get_template("secrets/index.html.j2").render(
        rows=rows, per_user_count=0, checked_at=None
    )
    assert f" · {len(value)} chars · 5 lines" in html
    assert "Entered: 0 chars · 0 lines" in html
    assert "SECRET_SENTINEL" not in html
    assert '<textarea name="value"' in html
    assert "</textarea>" in html
    assert value not in html
