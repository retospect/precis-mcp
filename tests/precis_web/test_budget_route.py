"""Tests for the /budget page + the settings/tote against real Postgres.

Two layers:

* Fake-store route smoke tests (the shared ``client`` fixture): the page
  renders and the set/reset forms wire through without a DB.
* Live-PG tests (the ``store`` fixture): the ``app_settings`` round-trip, the
  meter's DB-override cap resolution, and the tote's by-model/by-source rollup
  exercise the real SQL that the fake store can't parse.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.budget import meter
from precis.budget import settings as budget_settings
from precis_web.routes.status import _budget_tote


@pytest.fixture(autouse=True)
def _reset_meter() -> Any:
    """The /budget POST handlers rebind the process meter store; reset it after
    each test so a fake store never leaks into another test's dispatch path."""
    yield
    meter.bind_store(None)


# ── fake-store route smoke ───────────────────────────────────────────────


def test_budget_page_renders(client: TestClient) -> None:
    r = client.get("/budget")
    assert r.status_code == 200
    assert "Budget" in r.text
    assert 'action="/budget/set"' in r.text
    # Default caps surface (env defaults: $5 / $20).
    assert "Hourly" in r.text
    assert "24h" in r.text
    # The claude-OAuth quota lane + resume control render even with no snapshot.
    assert "Claude subscription" in r.text
    assert 'action="/budget/resume"' in r.text


def test_budget_resume_redirects(client: TestClient) -> None:
    r = client.post("/budget/resume", data={"hours": "2"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/budget"


def test_budget_resume_clear_redirects(client: TestClient) -> None:
    r = client.post("/budget/resume/clear", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/budget"


def test_budget_set_redirects(client: TestClient) -> None:
    r = client.post("/budget/set", data={"hourly_usd": "3.50"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/budget"


def test_budget_reset_redirects(client: TestClient) -> None:
    r = client.post(
        "/budget/reset",
        data={"key": "budget.hourly_usd"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/budget"


# ── live-PG: settings round-trip + meter override + tote ─────────────────


def test_settings_roundtrip_pg(store: Any) -> None:
    assert budget_settings.get_float(store, budget_settings.DAILY_KEY) is None
    budget_settings.set_float(store, budget_settings.DAILY_KEY, 4.25)
    assert budget_settings.get_float(store, budget_settings.DAILY_KEY) == 4.25
    # Upsert replaces rather than duplicating.
    budget_settings.set_float(store, budget_settings.DAILY_KEY, 9.5)
    assert budget_settings.get_float(store, budget_settings.DAILY_KEY) == 9.5
    budget_settings.clear_setting(store, budget_settings.DAILY_KEY)
    assert budget_settings.get_float(store, budget_settings.DAILY_KEY) is None


def test_resume_override_roundtrip_pg(store: Any) -> None:
    assert budget_settings.resume_active(store) is False
    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    budget_settings.set_setting(store, budget_settings.RESUME_UNTIL_KEY, future)
    assert budget_settings.resume_active(store) is True
    assert budget_settings.get_resume_until(store) is not None
    budget_settings.clear_setting(store, budget_settings.RESUME_UNTIL_KEY)
    assert budget_settings.resume_active(store) is False


def test_meter_db_override_pg(store: Any) -> None:
    budget_settings.set_float(store, budget_settings.DAILY_KEY, 3.0)
    status = meter.current_status(store, use_cache=False)
    assert status is not None
    assert status.daily_cap == 3.0
    budget_settings.clear_setting(store, budget_settings.DAILY_KEY)


def test_budget_tote_rolls_up_llm_call_log_pg(store: Any) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_call_log (source, model, cost_usd) VALUES (%s, %s, %s)",
            ("dream", "claude-opus-4-8", 0.5),
        )
        conn.commit()
    tote = _budget_tote(store)
    assert tote  # non-empty when a store + a priced row exist
    models = {m["label"]: m["cost"] for m in tote["by_model"]}
    assert models.get("claude-opus-4-8") == pytest.approx(0.5)
    sources = {s["label"] for s in tote["by_source"]}
    assert "dream" in sources
    assert [w["label"] for w in tote["windows"]] == ["Hourly", "24h"]
