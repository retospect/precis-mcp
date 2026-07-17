"""Real-PG tests for the factory console's SQL helpers (slice 3).

The route degrades to empty panels on error; these prove the happy path
against a real DB: host strip from ``host_heartbeat``, prio overrides
from ``service_config``, and last-ok/last-fail from ``worker_logs``
BatchResult payloads keyed by ``payload.handler``.
"""

from __future__ import annotations

import json

from precis.workers.service_config import set_service_model, set_service_prio
from precis_web.routes.factory import _activity, _config_rows, _hosts


def _log(conn, handler: str, *, ok: int, failed: int) -> None:
    conn.execute(
        "INSERT INTO worker_logs (host, process, level, logger, message, payload) "
        "VALUES ('h', 'p', 'INFO', 'precis.workers.runner', 'worker: x', %s::jsonb)",
        (
            json.dumps(
                {"handler": handler, "claimed": ok + failed, "ok": ok, "failed": failed}
            ),
        ),
    )


def test_hosts_reports_liveness(store) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts, load1, load5, load15) "
            "VALUES ('melchior', now(), 1.5, 1.2, 1.0), "
            "       ('caspar', now() - interval '2 hours', 0.1, 0.1, 0.1)"
        )
        conn.commit()
    hosts = {h["host"]: h for h in _hosts(store)}
    assert hosts["melchior"]["alive"] is True
    assert hosts["melchior"]["load1"] == 1.5
    assert hosts["caspar"]["alive"] is False  # stale (2h old)


def test_config_rows_returns_all_rows(store) -> None:
    set_service_prio(store, "melchior", "classify", 0)
    set_service_prio(store, "*", "classify", 3)
    set_service_model(store, "caspar", "briefing", "claude-opus-4-8")
    rows = _config_rows(store)
    triples = {(s, h, p) for (s, h, p, _m) in rows}
    assert ("classify", "melchior", 0) in triples
    assert ("classify", "*", 3) in triples
    # model row carries its model_pref
    briefing = [r for r in rows if r[0] == "briefing"][0]
    assert briefing[3] == "claude-opus-4-8"


def test_activity_keys_by_payload_handler(store) -> None:
    with store.pool.connection() as conn:
        _log(conn, "fetch_oa", ok=3, failed=0)  # a successful fetch batch
        _log(conn, "classify", ok=0, failed=2)  # a failing classify batch
        _log(conn, "classify", ok=5, failed=0)  # …then a good one
        conn.commit()
    act = _activity(store)
    # keyed by the BatchResult.handler string (what ServiceSpec.log_handler yields)
    assert act["fetch_oa"]["last_ok"] is not None
    assert act["fetch_oa"]["last_fail"] is None
    assert act["classify"]["last_ok"] is not None
    assert act["classify"]["last_fail"] is not None


def test_activity_ignores_non_batchresult_rows(store) -> None:
    """A payload without a numeric ok/failed must not break the cast."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs (host, level, logger, message, payload) "
            "VALUES ('h', 'INFO', 'l', 'boot', %s::jsonb)",
            (json.dumps({"handler": "weird", "event": "boot"}),),
        )
        conn.commit()
    act = _activity(store)
    # row is present (has 'handler') but neither ok nor fail is numeric → both None
    assert act.get("weird", {}).get("last_ok") is None
    assert act.get("weird", {}).get("last_fail") is None
