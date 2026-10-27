"""DB round-trip tests for the secrets vault (0059) and saved hint counts (0189).

Self-provisions ``pgcrypto`` + the vault passphrase in ``vault._key`` at the
database level; skips cleanly where pgcrypto can't be created (non-superuser
test DB without it pre-installed).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from precis import secrets as vault
from precis.store import Store
from tests.conftest import (
    PG_TEST_DSN,
    _active_dsn,
    _pg_available,
    _provision_vault_key,
)

_TEST_KEY = "test-vault-key-0123456789"


@pytest.fixture
def vault_store() -> Iterator[Store]:
    """A Store on the shared test DB with the vault passphrase provisioned
    in ``vault._key`` (0239 — a DB row only the owner reads, replacing the
    session GUC trick). Skips when no postgres is reachable (e.g. the
    macOS/Windows CI legs), matching the shared ``store``/``hub`` fixtures,
    and again if pgcrypto is unavailable and uncreatable here."""
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
    _provision_vault_key(dsn, _TEST_KEY)
    store = Store.connect(dsn)
    try:
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM vault.secrets")
            conn.execute("DELETE FROM vault.events")
            conn.commit()
        yield store
    finally:
        store.close()


@pytest.mark.db
def test_key_table_and_helper_unreadable_by_non_owner() -> None:
    """gr505334: only the owning role can reach the passphrase.

    A NOLOGIN probe is denied on ``vault._key`` and on ``vault._key()``
    itself — the SECURITY DEFINER verbs are the sole path in — and PUBLIC
    holds no EXECUTE grant on the helper, so a leaked grant can't reopen it.
    """
    if not _pg_available():
        pytest.skip("postgres unreachable")
    role = "probe_vault_" + uuid.uuid4().hex[:8]
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            conn.execute(f'SET ROLE "{role}"')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT k FROM vault._key")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT vault._key()")
            conn.execute("RESET ROLE")
            assert conn.execute(
                "SELECT has_function_privilege('public', 'vault._key()', 'EXECUTE')"
            ).fetchone() == (False,)
        finally:
            conn.execute("RESET ROLE")
            conn.execute(f'DROP ROLE "{role}"')


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
    rows, _ = route._build_rows(inventory, route.secret_status.StatusSnapshot(None, {}))
    scratch = Path(__file__).parents[1] / ".scratch"
    scratch.mkdir(exist_ok=True)
    (scratch / "browser-saved-inventory.json").write_text(
        json.dumps(inventory, default=str), encoding="utf-8"
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


def test_recheck_probe_runs_against_real_postgres(vault_store: Store) -> None:
    """The recheck's privilege + versions round trip parses and answers on a
    real vault, empty or not (the LEFT JOIN keeps the grant row)."""
    assert vault._list_versions(vault_store) == {}
    vault.set_secret("A_KEY", "fake-value-for-tests-only", store=vault_store)
    versions = vault._list_versions(vault_store)
    assert versions is not None
    assert set(versions) == {"A_KEY"}


def test_set_secret_if_absent_keeps_first_value(vault_store: Store) -> None:
    """gr517874: the first mint wins; later callers get the stored value."""
    assert (
        vault.set_secret_if_absent("A_KEY", "first-value-00001", store=vault_store)
        == "first-value-00001"
    )
    assert (
        vault.set_secret_if_absent("A_KEY", "second-value-0002", store=vault_store)
        == "first-value-00001"
    )
    vault.invalidate("A_KEY")
    assert vault.get_secret("A_KEY", store=vault_store) == "first-value-00001"
    with vault_store.pool.connection() as conn:
        verbs = conn.execute(
            "SELECT verb, count(*) FROM vault.events WHERE name = %s GROUP BY verb",
            ("A_KEY",),
        ).fetchall()
        idents = conn.execute(
            "SELECT host, pid FROM vault.events WHERE name = %s AND verb = 'reveal'",
            ("A_KEY",),
        ).fetchall()
    # 'set' for the winning mint only; every read-back — winner's,
    # loser's, the verification above — is a normal vault.reveal through
    # the 6-arg overload, so each row carries the client identity (0111).
    counts = {r[0]: r[1] for r in verbs}
    assert counts["set"] == 1
    assert counts["reveal"] >= 2
    assert idents and all(host is not None and pid is not None for host, pid in idents)


def test_set_secret_if_absent_refuses_empty(vault_store: Store) -> None:
    with pytest.raises(psycopg.errors.RaiseException):
        vault.set_secret_if_absent("A_KEY", "", store=vault_store)


def test_set_secret_if_absent_grant_matches_set_secret(vault_store: Store) -> None:
    """Same lock-down as set_secret: SECURITY DEFINER, no PUBLIC EXECUTE
    (0257 narrowed the verbs to agent_rw; the PUBLIC shape died with
    gr523021) — and RETURNS boolean, never plaintext (reveal stays the
    sole decrypt path)."""
    with vault_store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT p.proname, p.prosecdef, p.prorettype::regtype::text, "
            "has_function_privilege('public', p.oid, 'EXECUTE') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'vault' "
            "AND p.proname IN ('set_secret', 'set_secret_if_absent')"
        ).fetchall()
    by_name = {r[0]: (r[1], r[2], r[3]) for r in rows}
    assert by_name["set_secret_if_absent"] == (True, "boolean", False)
    assert by_name["set_secret_if_absent"][0] == by_name["set_secret"][0]
    assert by_name["set_secret_if_absent"][2] == by_name["set_secret"][2]


def test_set_secret_if_absent_read_back_ignores_fill_miss(
    vault_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-1: the read-back bypasses the coalesced reveal cache — a
    racing in-process 'not found' miss (``_fill_miss`` /
    ``_cached_reveal``) must not look like a delete race to the
    winner."""
    monkeypatch.setattr(vault, "_cached_reveal", lambda store, name: None)
    assert (
        vault.set_secret_if_absent("A_KEY", "real-stored-value", store=vault_store)
        == "real-stored-value"
    )
    monkeypatch.undo()
    vault.invalidate("A_KEY")
    assert vault.get_secret("A_KEY", store=vault_store) == "real-stored-value"


def test_set_secret_if_absent_concurrent_converges(vault_store: Store) -> None:
    """N racing minters on one name all return the single stored value."""
    from concurrent.futures import ThreadPoolExecutor

    candidates = [f"racer-{i:016d}" for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda v: vault.set_secret_if_absent("A_KEY", v, store=vault_store),
                candidates,
            )
        )
    assert len(set(results)) == 1
    assert results[0] in candidates
    vault.invalidate("A_KEY")
    assert vault.get_secret("A_KEY", store=vault_store) == results[0]


def test_ensure_pepper_concurrent_mint_converges(
    vault_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr517874: racing ``ensure_pepper`` calls converge on one pepper."""
    from concurrent.futures import ThreadPoolExecutor

    from precis import users

    monkeypatch.delenv(users.PEPPER_SECRET, raising=False)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: users.ensure_pepper(store=vault_store), range(8))
        )
    assert len(set(results)) == 1
    vault.invalidate(users.PEPPER_SECRET)
    assert users.resolve_pepper(store=vault_store) == results[0]
    # A second round resolves the stored pepper — no re-mint.
    assert users.ensure_pepper(store=vault_store) == results[0]


@pytest.mark.db
def test_ensure_file_link_key_concurrent_mint_converges(
    vault_store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gr517874: racing ``ensure_file_link_key`` calls converge on one
    key — a signer on the losing mint would 403 every link it made."""
    from concurrent.futures import ThreadPoolExecutor

    from precis import file_links

    monkeypatch.delenv(file_links.FILE_LINK_SECRET, raising=False)
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: file_links.ensure_file_link_key(store=vault_store),
                range(8),
            )
        )
    assert len(set(results)) == 1
    vault.invalidate(file_links.FILE_LINK_SECRET)
    # A minted token verifies under the stored key — the winner is live.
    token = file_links.mint("get", "a" * 64, 42, store=vault_store)
    assert file_links.verify(token, op="get", sha="a" * 64, store=vault_store)
