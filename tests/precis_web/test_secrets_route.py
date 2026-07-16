"""Smoke tests for the /secrets vault editor route (ADR 0055).

Fake-store route tests via the shared ``client`` fixture: the masked inventory
renders, the write-only set form wires through, and a blank submit is a no-op
(never touches the vault). The vault SQL itself is monkeypatched — the fake
store can't parse ``vault.list()`` — so these assert the *route wiring*, not the
crypto (covered by the vault unit tests). Previously the route was only exercised
by app-boot import.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis import secrets as vault


def test_secrets_page_renders_masked_inventory(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        vault,
        "list_secrets",
        lambda *, store: [
            {
                "name": "PRECIS_CORE_API_KEY",
                "hint": "sk-…9f2",
                "updated_at": datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            }
        ],
    )
    r = client.get("/secrets")
    assert r.status_code == 200
    assert "PRECIS_CORE_API_KEY" in r.text
    assert "sk-…9f2" in r.text  # masked hint shown, never a plaintext
    assert 'action="/secrets/set"' in r.text


def test_secrets_set_writes_and_redirects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        vault, "set_secret", lambda name, value, *, store: calls.append((name, value))
    )
    r = client.post(
        "/secrets/set",
        data={"name": "PRECIS_CORE_API_KEY", "value": "sk-secret"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/secrets"
    assert calls == [("PRECIS_CORE_API_KEY", "sk-secret")]


def test_secrets_set_blank_value_is_noop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        vault, "set_secret", lambda name, value, *, store: calls.append((name, value))
    )
    # Write-only guard: a blank value must never round-trip an existing secret.
    r = client.post(
        "/secrets/set",
        data={"name": "PRECIS_CORE_API_KEY", "value": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert calls == []
