"""Real-PG tests for the factory console's SQL helpers (slice 3).

The route degrades to empty panels on error; these prove the happy path
against a real DB: host strip from ``host_heartbeat``, prio overrides
from ``service_config``, and last-ok/last-fail from ``worker_logs``
BatchResult payloads keyed by ``payload.handler``.
"""

from __future__ import annotations

import json

from precis.workers.service_config import (
    clear_reserve,
    set_reserve,
    set_service_model,
    set_service_prio,
)
from precis_web.routes.factory import (
    _activity,
    _config_rows,
    _duration,
    _errors_by_host,
    _hosts,
    _next_run,
    _quests,
    _reserves,
    _scheduler_leases,
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


def _err(
    conn,
    host: str,
    *,
    pass_: str,
    message: str,
    level: str = "ERROR",
    ts_ago_s: float = 0,
) -> None:
    # ``now()`` is the *transaction* timestamp in Postgres — every row
    # inserted before a commit shares one instant, so an explicit backward
    # offset is what gives ordered-by-recency tests a deterministic newest
    # row instead of a same-ts tie.
    conn.execute(
        "INSERT INTO worker_logs (host, process, pass, level, logger, message, ts) "
        "VALUES (%s, 'p', %s, %s, 'l', %s, now() - make_interval(secs => %s))",
        (host, pass_, level, message, ts_ago_s),
    )


def test_errors_by_host_groups_recent_errors(store) -> None:
    """Per-machine WARNING+ readout (gr162694 #2): count + newest samples,
    INFO ignored, WARNING included (not just ERROR/CRITICAL)."""
    with store.pool.connection() as conn:
        _err(conn, "melchior", pass_="plan_tick", message="boom one")
        _err(conn, "melchior", pass_="review", message="boom two", level="CRITICAL")
        _err(conn, "melchior", pass_="x", message="ok", level="INFO")  # ignored
        _err(conn, "spark", pass_="embed", message="spark boom", level="WARNING")
        conn.commit()
    by_host = _errors_by_host(store)
    assert by_host["melchior"]["count"] == 2  # INFO excluded
    assert by_host["spark"]["count"] == 1  # WARNING now counted
    # samples carry pass + trimmed message for the mouseover
    msgs = {s["msg"] for s in by_host["melchior"]["samples"]}
    assert "boom one" in msgs and "boom two" in msgs
    assert "ok" not in msgs


def test_errors_by_host_latest_is_newest_truncated_with_full_title(store) -> None:
    """The visible chip text is the single newest line, truncated ~80 chars —
    not just a count behind a hover (gr162694 #2); the full line + level +
    pass + absolute ts still carry in ``latest.title`` for the mouseover."""
    with store.pool.connection() as conn:
        _err(
            conn,
            "melchior",
            pass_="plan_tick",
            message="an older warning",
            level="WARNING",
            ts_ago_s=60,
        )
        long_msg = "x" * 200
        _err(conn, "melchior", pass_="review", message=long_msg, level="CRITICAL")
        conn.commit()
    by_host = _errors_by_host(store)
    latest = by_host["melchior"]["latest"]
    assert latest is not None
    assert latest["line"] == long_msg[:80]
    assert long_msg in latest["title"]  # full text preserved in the title
    assert "CRITICAL" in latest["title"]
    assert "review" in latest["title"]


def test_scheduler_leases_reads_next_fire(store) -> None:
    """The Services tab's "next run" column reads scheduler_leases rows
    directly (gr162694 #1) — interval/last_fired_at/last_host carry through
    for the cell's tooltip."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO scheduler_leases "
            "(name, interval_s, next_fire_at, last_fired_at, last_host) "
            "VALUES ('watch_poll', 3600, now() + interval '10 minutes', "
            "        now() - interval '50 minutes', 'melchior')"
        )
        conn.commit()
    leases = _scheduler_leases(store)
    assert leases["watch_poll"]["interval_s"] == 3600
    assert leases["watch_poll"]["last_host"] == "melchior"
    assert leases["watch_poll"]["last_fired_at"] is not None


def test_duration_buckets_match_ago_style() -> None:
    """Mirrors ``_ago``'s bucket boundaries, minus the trailing 'ago' (used
    for both past and future deltas by ``_next_run``)."""
    assert _duration(45) == "45s"
    assert _duration(300) == "5m"
    assert _duration(7200) == "2h"
    assert _duration(200_000) == "2d"


def test_next_run_shows_lease_fire_for_cadence_service(store) -> None:
    """A cadence-backed service (e.g. watch_poll) shows its wall-clock next
    fire from ``scheduler_leases`` (gr162694 #1)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO scheduler_leases (name, interval_s, next_fire_at) "
            "VALUES ('watch_poll', 3600, now() + interval '10 minutes')"
        )
        conn.commit()
    leases = _scheduler_leases(store)
    row = _next_run("watch_poll", "pass", leases)
    assert row is not None
    assert row["text"].startswith("in ")
    assert "every 3600s" in row["title"]


def test_next_run_every_cycle_for_non_cadence_pass() -> None:
    """A ``pass``-kind row with no lease row runs every worker loop cycle."""
    row = _next_run("embed", "pass", {})
    assert row == {
        "text": "every cycle",
        "title": (
            "Runs every worker loop cycle — no fixed schedule, "
            "checks for due work each tick."
        ),
    }


def test_next_run_blank_for_job_executor_passes() -> None:
    """``job_*`` executor passes drain a queue reactively (fire only when
    work exists) — no fixed next-run (gr162694 #1's "pull/claim executor
    passes blank")."""
    assert _next_run("job_ssh_node", "pass", {}) is None
    assert _next_run("job_claude_inproc", "pass", {}) is None
    assert _next_run("job_claude_docker", "pass", {}) is None


def test_next_run_blank_for_non_pass_kind() -> None:
    assert _next_run("llama_swap", "serving", {}) is None
    assert _next_run("embedder", "daemon", {}) is None
    assert _next_run("struct_relax", "compute", {}) is None


def test_reserves_reports_unexpired_host_row(store) -> None:
    """§B-2's reserve pseudo-service, surfaced (gr162694 #4) instead of
    left invisible in the config table."""
    set_reserve(store, "melchior", hours=2, actor="alice")
    reserves = _reserves(store)
    assert reserves["melchior"]["actor"] == "alice"
    assert reserves["melchior"]["expires_at"] is not None


def test_reserves_wildcard_row_keyed_by_star(store) -> None:
    set_reserve(store, "*", hours=1, actor="bob")
    assert "*" in _reserves(store)


def test_reserves_excludes_expired_rows(store) -> None:
    """An expired reserve is inert — the console must not show a stale
    banner (mirrors ``reserve_active``'s own ``expires_at > now()`` gate)."""
    set_reserve(store, "spark", hours=1)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE service_config SET expires_at = now() - interval '1 hour' "
            "WHERE host = 'spark' AND service = 'reserve'"
        )
        conn.commit()
    assert "spark" not in _reserves(store)


def test_reserves_cleared_row_disappears(store) -> None:
    set_reserve(store, "melchior", hours=1)
    assert "melchior" in _reserves(store)
    clear_reserve(store, "melchior")
    assert "melchior" not in _reserves(store)


def test_quests_no_budget_shows_spend_only(store, monkeypatch) -> None:
    import re

    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    monkeypatch.delenv("PRECIS_QUEST_WEEKLY_CHARS", raising=False)
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text="Lone quest", tags=["PRIO:normal"])
    qid_m = re.search(r"\bqu(\d+)\b", resp.body)
    assert qid_m is not None, f"create-ack missing qu<id>; got {resp.body!r}"
    qid = int(qid_m.group(1))
    out = _quests(store)
    assert out["budget"] is None
    row = {r["id"]: r for r in out["rows"]}[qid]
    assert row["share"] is None and row["pct"] is None and row["over"] is False
