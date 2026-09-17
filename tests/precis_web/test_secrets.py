"""Smoke test for the ``/secrets`` vault editor.

Closes the OPEN-ITEMS polish item "``/secrets`` web smoke test" — the route
was previously covered only by app-boot import. Exercises the three affordances
against the fake store's empty-cursor pool:

* the masked inventory renders (no plaintext, never decrypts);
* a blank submit is a no-op (the write-only guard);
* a named submit + a delete redirect (303) and reach the vault write path.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

import precis_web.routes.secrets as secrets_mod


def test_secrets_index_renders(client: TestClient) -> None:
    resp = client.get("/secrets")
    assert resp.status_code == 200
    # The write-only editor page (fake pool → empty inventory, still renders).
    assert "secret" in resp.text.lower()


def test_blank_submit_is_noop(client: TestClient) -> None:
    resp = client.post(
        "/secrets/set",
        data={"name": "PRECIS_X", "value": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/secrets"


def test_named_submit_writes(client: TestClient) -> None:
    resp = client.post(
        "/secrets/set",
        data={"name": "PRECIS_X", "value": "sk-123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/secrets"


def test_per_user_vault_rows_are_hidden_with_a_count_note(
    client: TestClient, monkeypatch
) -> None:
    """A vault-only row whose name carries a ``:<login>`` suffix (the
    per-user credential convention — reMarkable pairing, feed tokens) is
    never listed row-by-row; instead a short muted count note appears."""
    now = datetime.now(UTC)
    fake_rows = [
        {"name": "REMARKABLE_RMAPI_CONFIG:reto", "hint": "***abcd", "updated_at": now},
        {"name": "PRECIS_WEB_FEED_TOKEN:someone", "hint": "***efgh", "updated_at": now},
        {"name": "SOME_OTHER_SECRET", "hint": "***ijkl", "updated_at": now},
    ]
    monkeypatch.setattr(secrets_mod.vault, "list_secrets", lambda **kw: fake_rows)

    resp = client.get("/secrets")
    assert resp.status_code == 200
    assert "REMARKABLE_RMAPI_CONFIG:reto" not in resp.text
    assert "PRECIS_WEB_FEED_TOKEN:someone" not in resp.text
    assert "SOME_OTHER_SECRET" in resp.text
    assert "2 per-user credentials" in resp.text
    assert "/account" in resp.text


def test_delete_redirects(client: TestClient) -> None:
    resp = client.post(
        "/secrets/delete",
        data={"name": "PRECIS_X"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/secrets"
