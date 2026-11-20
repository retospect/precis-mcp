"""Allowance display shares the live pacing rules (gr623034)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from precis.store import Store
from precis.utils.llm.accounts import Pool
from precis_web.routes import _llm_pools

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def _pool(**changes: Any) -> Pool:
    return replace(
        Pool(
            provider="llm:claude:weekly",
            capacity=100,
            refill_per_sec=0,
            tokens=0,
            daily_cap=None,
            cls=None,
            unit="credit",
            limit_reported=100,
            used_reported=40,
            remaining_reported=None,
            reported_at=NOW,
            source="provider",
            confidence=None,
            reset_rule="rolling:604800",
            resets_at=NOW + timedelta(days=1),
            expires_at=None,
            parked_until=None,
            stale=False,
        ),
        **changes,
    )


def test_view_model_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    pools = [
        _pool(),
        _pool(provider="llm:kimi:promo", limit_reported=None, reset_rule=None),
        _pool(
            provider="llm:claude:short",
            parked_until=NOW + timedelta(hours=2),
            stale=True,
        ),
    ]
    monkeypatch.setattr(_llm_pools, "list_pools", lambda store: pools)
    rows = _llm_pools.llm_pools_view(cast(Store, object()), NOW)
    assert [row["account"] for row in rows] == ["claude", "claude", "kimi"]
    parked, weekly, unknown = rows
    assert weekly["credit_left_pct"] == pytest.approx(60)
    assert weekly["window_left_pct"] == pytest.approx(100 / 7)
    assert weekly["relative"] == "resets in 1d 0h 0m"
    assert weekly["pacing_state"] == "behind"
    assert unknown["credit_left_pct"] is None
    assert unknown["window_left_pct"] is None
    assert unknown["pacing_state"] == "unknown"
    assert parked["pacing_state"] == "exhausted"
    assert parked["retry_at"] == "2026-11-17 02:00Z"
    assert parked["stale"] is True


def test_pools_page_renders(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset = datetime.now(UTC) + timedelta(days=1)
    monkeypatch.setattr(
        _llm_pools, "list_pools", lambda store: [_pool(resets_at=reset)]
    )
    response = client.get("/llm/pools")
    assert response.status_code == 200
    assert "claude" in response.text
    assert "60%" in response.text
    assert "behind" in response.text
    assert reset.strftime("%Y-%m-%d %H:%MZ") in response.text
    monkeypatch.setattr(_llm_pools, "list_pools", lambda store: [])
    response = client.get("/llm/pools")
    assert response.status_code == 200
    assert "no pools" in response.text.lower()
