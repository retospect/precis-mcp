"""``endpoint_dark`` (gr633497): a client label that was busy and then
silent while the ledger stayed live; quiet nights are not findings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from precis import health_checks
from precis.workers.health_digest import _endpoint_dark_check


def _seed(
    store: Any,
    client: str | None,
    sessions: int,
    per: int,
    age_s: int,
    *,
    at: datetime | None = None,
) -> None:
    with store.pool.connection() as conn:
        for s in range(sessions):
            for i in range(per):
                conn.execute(
                    "INSERT INTO tool_calls (ts, verb, outcome, latency_ms, "
                    "client, session_id, seq) VALUES "
                    "(coalesce(%s::timestamptz, now()) - make_interval(secs => %s), "
                    "'get', 'ok', 5, %s, %s, %s)",
                    (at, age_s + i, client, f"{client}-{s}-{age_s}", i + 1),
                )
        conn.commit()


def _dark(store: Any, *, as_of: datetime | None = None) -> list[str]:
    with store.pool.connection() as conn:
        return health_checks.endpoint_dark(conn, as_of=as_of)


def test_silent_client_beside_live_ledger_is_dark(store) -> None:
    _seed(store, "claude-code/1.0", 3, 10, age_s=2400)
    _seed(store, "bot/2", 3, 10, age_s=60)
    _seed(store, "bot/2", 3, 10, age_s=2400)
    assert _dark(store) == ["claude-code"]
    with store.pool.connection() as conn:
        c = _endpoint_dark_check(conn)
    assert c.status == "stale" and "claude-code" in c.detail


def test_quiet_ledger_is_not_dark(store) -> None:
    _seed(store, "claude-code/1.0", 3, 10, age_s=2400)
    assert _dark(store) == []


def test_still_calling_or_sparse_client_is_not_dark(store) -> None:
    _seed(store, "claude-code/1.0", 3, 10, age_s=2400)
    # Bridge to now with calls <1800 s apart: no >=30 min gap exists.
    _seed(store, "claude-code/1.0", 1, 1, age_s=1300)
    _seed(store, "claude-code/1.0", 1, 1, age_s=60)
    _seed(store, "tiny/1", 1, 5, age_s=2400)
    _seed(store, "bot/2", 3, 10, age_s=60)
    assert _dark(store) == []
    with store.pool.connection() as conn:
        assert _endpoint_dark_check(conn).status == "ok"


def test_incident_timeline_caught_across_digest_gap(store: Any) -> None:
    # 78 calls in the 14:00Z bucket, two stragglers in the 14:50Z
    # bucket, then recovery by the 16:00Z evaluation.
    at = datetime(2026, 11, 19, 16, tzinfo=UTC)
    _seed(store, "claude-code/1.0", 3, 26, age_s=6660, at=at)
    _seed(store, "claude-code/1.0", 1, 2, age_s=4140, at=at)
    _seed(store, "claude-code/1.1", 3, 10, age_s=0, at=at)
    for age_s in range(60, 7200, 600):
        _seed(store, "bot/2", 4, 10, age_s=age_s, at=at)

    assert _dark(store, as_of=at - timedelta(hours=1)) == ["claude-code"]
    assert _dark(store, as_of=at) == ["claude-code"]
    assert _dark(store, as_of=at + timedelta(hours=1)) == []


def test_short_silence_mid_incident_is_not_dark(store) -> None:
    # Ten minutes of silence by itself is below the gap threshold.
    _seed(store, "claude-code/1.0", 3, 10, age_s=600)
    _seed(store, "bot/2", 3, 10, age_s=60)
    assert _dark(store) == []


def test_retired_client_beyond_max_silence_is_not_dark(store) -> None:
    # The gap started before the probe's lookback.
    _seed(store, "claude-code/1.0", 3, 10, age_s=8000)
    _seed(store, "bot/2", 3, 10, age_s=60)
    assert _dark(store) == []
