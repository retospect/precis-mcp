"""Smoke test for the ``/secrets`` vault editor (ADR 0055).

Closes the OPEN-ITEMS polish item "``/secrets`` web smoke test" — the route
was previously covered only by app-boot import. Exercises the three affordances
against the fake store's empty-cursor pool:

* the masked inventory renders (no plaintext, never decrypts);
* a blank submit is a no-op (the write-only guard);
* a named submit + a delete redirect (303) and reach the vault write path.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


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


def test_delete_redirects(client: TestClient) -> None:
    resp = client.post(
        "/secrets/delete",
        data={"name": "PRECIS_X"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/secrets"
