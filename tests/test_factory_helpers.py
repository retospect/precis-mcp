"""Real-PG tests for the factory console's SQL helpers (slice 3).

The route degrades to empty panels on error; these prove the happy path
against a real DB: host strip from ``host_heartbeat``, prio overrides
from ``service_config``, and last-ok/last-fail from ``worker_logs``
BatchResult payloads keyed by ``payload.handler``.
"""

from __future__ import annotations

import json

from precis.workers.service_config import set_service_model, set_service_prio
from precis_web.routes.factory import (
    _activity,
    _config_rows,
    _errors_by_host,
    _hosts,
    _quests,
    _slot_desc,
    _slots_by_host,
)


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


def test_slots_by_host_groups_advertised_resources(store) -> None:
    """The host strip's capability chips come from ``resource_slots``."""
    store.sync_host_resource_slots("melchior", {"gpu": 1, "tts": 1})
    store.sync_host_resource_slots("spark", {"gpu": 2})
    by_host = _slots_by_host(store)
    mel = {s["resource"]: s for s in by_host["melchior"]}
    assert mel["gpu"]["capacity"] == 1 and mel["gpu"]["free"] == 1
    assert set(mel) == {"gpu", "tts"}
    assert by_host["spark"][0]["resource"] == "gpu"
    assert by_host["spark"][0]["capacity"] == 2


def test_slots_by_host_flags_memory_pressure(store) -> None:
    """The soft ``mem`` gauge is annotated with a pressure level so the console
    can colour it as a RAM-pressure indicator (what to watch when a host runs a
    container runtime). Hard capability rows carry no level."""
    store.sync_soft_signal("h-crit", "mem", 0, 2)  # 0 free → under pressure
    store.sync_soft_signal("h-warn", "mem", 1, 4)  # <50% → low
    store.sync_soft_signal("h-ok", "mem", 2, 2)  # full → plenty
    store.sync_host_resource_slots("h-hard", {"gpu": 1})
    by_host = _slots_by_host(store)

    def _mem(host: str) -> dict:
        return {s["resource"]: s for s in by_host[host]}["mem"]

    assert _mem("h-crit")["pressure"] == "crit"
    assert _mem("h-warn")["pressure"] == "warn"
    assert _mem("h-ok")["pressure"] == "ok"
    gpu = {s["resource"]: s for s in by_host["h-hard"]}["gpu"]
    assert gpu["pressure"] is None


def test_slots_by_host_renders_container_agent_capability(store) -> None:
    """The soft ``container_agent`` gauge renders as its own green/red chip: a
    verified host is 'ok' (green), an opted-in-but-degraded host is 'crit' (red)
    — surfaced, not silent. Its label + tooltip differ from mem's RAM copy."""
    store.sync_soft_signal("h-ok", "container_agent", 1, 1)  # verified
    store.sync_soft_signal("h-degraded", "container_agent", 0, 1)  # opted in, can't
    by_host = _slots_by_host(store)

    def _ca(host: str) -> dict:
        return {s["resource"]: s for s in by_host[host]}["container_agent"]

    ok, bad = _ca("h-ok"), _ca("h-degraded")
    assert ok["pressure"] == "ok" and bad["pressure"] == "crit"
    # Its own label (not "RAM") and a capability-flavoured tooltip.
    assert ok["label"] == "agent" and bad["label"] == "agent"
    assert "verified" in ok["ptitle"].lower()
    assert "degraded" in bad["ptitle"].lower()


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


def test_quests_reports_share_bar(store, monkeypatch) -> None:
    """The quests panel surfaces windowed spend vs proportional share (§9)."""
    import re

    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler
    from precis.quest.logbook import append_entry

    h = QuestHandler(hub=Hub(store=store))

    def _mk(text: str, prio: str) -> int:
        resp = h.put(text=text, tags=[prio])
        m = re.search(r"\bqu(\d+)\b", resp.body)
        assert m is not None, resp.body
        return int(m.group(1))

    a = _mk("Quest A", "PRIO:normal")
    b = _mk("Quest B", "PRIO:normal")
    append_entry(store, a, text="spend", entry_type="cost", by="agent", chars=6)
    monkeypatch.setenv("PRECIS_QUEST_WEEKLY_CHARS", "10")

    out = _quests(store)
    assert out["budget"] == 10
    rows = {r["id"]: r for r in out["rows"]}
    # equal prio → 5-char share each; A used 6 → over (100%), B nothing.
    assert rows[a]["over"] is True and rows[a]["pct"] == 100.0
    assert rows[b]["spend"] == 0.0 and rows[b]["over"] is False
    # heaviest share-consumer first
    assert out["rows"][0]["id"] == a


def test_slots_carry_mouseover_desc(store) -> None:
    """Each capability chip gets an explanatory mouseover (podman/gpu/mem)."""
    store.sync_host_resource_slots("melchior", {"podman": 2})
    by_host = _slots_by_host(store)
    podman = {s["resource"]: s for s in by_host["melchior"]}["podman"]
    # the desc names the resource, its free/capacity, and explains it
    assert podman["desc"].startswith("podman: 2/2")
    assert "container" in podman["desc"].lower()
    # unknown resources still get *a* tooltip (generic fallback), never blank
    assert _slot_desc("frobnicator", 1, 3, "hard")
    assert "frobnicator: 1/3" in _slot_desc("frobnicator", 1, 3, "hard")


def _err(conn, host: str, *, pass_: str, message: str, level: str = "ERROR") -> None:
    conn.execute(
        "INSERT INTO worker_logs (host, process, pass, level, logger, message) "
        "VALUES (%s, 'p', %s, %s, 'l', %s)",
        (host, pass_, level, message),
    )


def test_errors_by_host_groups_recent_errors(store) -> None:
    """Per-machine ERROR/CRITICAL readout: count + newest samples, INFO ignored."""
    with store.pool.connection() as conn:
        _err(conn, "melchior", pass_="plan_tick", message="boom one")
        _err(conn, "melchior", pass_="review", message="boom two", level="CRITICAL")
        _err(conn, "melchior", pass_="x", message="ok", level="INFO")  # ignored
        _err(conn, "spark", pass_="embed", message="spark boom")
        conn.commit()
    by_host = _errors_by_host(store)
    assert by_host["melchior"]["count"] == 2  # INFO excluded
    assert by_host["spark"]["count"] == 1
    # samples carry pass + trimmed message for the mouseover
    msgs = {s["msg"] for s in by_host["melchior"]["samples"]}
    assert "boom one" in msgs and "boom two" in msgs
    assert "ok" not in msgs


def test_quests_no_budget_shows_spend_only(store, monkeypatch) -> None:
    import re

    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    monkeypatch.delenv("PRECIS_QUEST_WEEKLY_CHARS", raising=False)
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text="Lone quest", tags=["PRIO:normal"])
    qid = int(re.search(r"\bqu(\d+)\b", resp.body).group(1))
    out = _quests(store)
    assert out["budget"] is None
    row = {r["id"]: r for r in out["rows"]}[qid]
    assert row["share"] is None and row["pct"] is None and row["over"] is False
