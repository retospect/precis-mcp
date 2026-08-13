"""Smoke tests for the /settings editor route (:mod:`precis_web.routes.settings`).

Fake-store route tests via the shared ``client`` fixture, mirroring
``test_secrets_route.py``/``test_budget_route.py``'s shape. Unlike the
resolver-layer tests in ``tests/test_settings.py``, the shared
``FakeStore``'s ``pool.connection()`` doesn't parse SQL (every read is a
no-op empty cursor) — so wherever a test needs a write to be visible to a
following read, ``precis.settings.{set_setting,clear_setting,list_settings}``
are monkeypatched onto a small in-memory dict standing in for
``app_settings``. That asserts the *route wiring* (refusal / validation /
redirect / re-render), not the SQL — covered by ``tests/test_settings.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis import settings as psettings


def test_settings_page_renders_registry(client: TestClient) -> None:
    """No monkeypatching needed: the real resolver against the FakeStore's
    empty-cursor pool degrades cleanly to compiled defaults."""
    r = client.get("/settings")
    assert r.status_code == 200
    for key in psettings.REGISTRY:
        assert key in r.text
    assert 'action="/settings/set"' in r.text


def test_settings_set_unregistered_key_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        psettings, "set_setting", lambda key, value, *, store: calls.append(key)
    )
    r = client.post(
        "/settings/set",
        data={"key": "not.a.registered.key", "value": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=unregistered" in r.headers["location"]
    assert calls == []


def test_settings_set_validates_type(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        psettings, "set_setting", lambda key, value, *, store: calls.append(key)
    )
    r = client.post(
        "/settings/set",
        data={"key": "budget.hourly_usd", "value": "not-a-float"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "not+a+valid+float" in r.headers["location"]
    assert calls == []  # rejected before the write


def test_settings_set_recorded_value_resolves_at_db_layer_on_next_get(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    db: dict[str, str] = {}

    def _set(key: str, value: object, *, store: Any) -> None:
        db[key] = str(value)

    def _list(*, store: Any) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for key in sorted(psettings.REGISTRY):
            entry = psettings.REGISTRY[key]
            if key in db:
                rows.append(
                    {
                        "key": key,
                        "value": db[key],
                        "layer": "db",
                        "type": entry.type,
                        "doc": entry.doc,
                        "env_var": entry.env_var,
                        "updated_at": None,
                        "updated_by": None,
                    }
                )
            else:
                rows.append(
                    {
                        "key": key,
                        "value": entry.default,
                        "layer": "default",
                        "type": entry.type,
                        "doc": entry.doc,
                        "env_var": entry.env_var,
                        "updated_at": None,
                        "updated_by": None,
                    }
                )
        return rows

    monkeypatch.setattr(psettings, "set_setting", _set)
    monkeypatch.setattr(psettings, "list_settings", _list)

    r = client.post(
        "/settings/set",
        data={"key": "budget.hourly_usd", "value": "7.5"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"
    assert db["budget.hourly_usd"] == "7.5"

    page = client.get("/settings")
    assert page.status_code == 200
    assert "7.5" in page.text


def test_settings_clear_reverts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        psettings, "clear_setting", lambda key, *, store: calls.append(key)
    )
    r = client.post(
        "/settings/clear", data={"key": "budget.hourly_usd"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"
    assert calls == ["budget.hourly_usd"]
