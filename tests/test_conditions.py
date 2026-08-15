"""Condition registry + bounded_heal (self-healing-spine Layer 2, slice 3).

The declarative probe rows (pass-dead-on-host / rescue-pass-cadence /
pass-wedged / llm-degraded / dead-generation-claims), their mapping onto
the health_digest CheckResult lifecycle, and the bounded heal arm
(attempt budget + cooldown + cap-then-gripe + CAS race safety +
restart-once whitelist, dark by default).
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import loop as loop_mod
from precis.store import Store
from precis.store.types import Tag
from precis.workers.bounded_heal import HealSpec, run_bounded_heal
from precis.workers.conditions import (
    Condition,
    ConditionFinding,
    HealRequest,
    _probe_dead_generation_claims,
    _probe_llm_degraded,
    _probe_pass_dead,
    _probe_pass_wedged,
    _probe_rescue_cadence,
    _probe_settings_env_shadowed,
    run_condition_checks,
    run_condition_heals,
)

pytestmark = pytest.mark.db

_HOST = "cond-test-host"
_PROC = "precis-worker"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _seed_heartbeat(store: Store, *, meta: dict | None = None) -> None:
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM host_heartbeat WHERE host = %s", (_HOST,))
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts, meta) VALUES (%s, now(), %s::jsonb)",
            (_HOST, json.dumps(meta or {})),
        )
        conn.commit()


def _seed_pass_logs(store: Store, handler: str, *, n: int, last_age_s: float) -> None:
    """n rows over the past 7 days for (host, proc, handler), the newest
    ``last_age_s`` old — the every-cycle BatchResult log shape."""
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM worker_logs WHERE host = %s AND payload->>'handler' = %s",
            (_HOST, handler),
        )
        conn.execute(
            "INSERT INTO worker_logs (ts, host, process, level, logger, message, payload) "
            "SELECT now() - make_interval(secs => %(last)s) "
            "       - (gs || ' hours')::interval, "
            "       %(host)s, %(proc)s, 'INFO', 'precis.workers.runner', "
            "       'worker: batch', "
            "       jsonb_build_object('handler', %(handler)s::text, 'claimed', 0, "
            "                          'ok', 0, 'failed', 0) "
            "  FROM generate_series(0, %(n)s - 1) gs",
            {
                "last": last_age_s,
                "host": _HOST,
                "proc": _PROC,
                "handler": handler,
                "n": n,
            },
        )
        conn.commit()


def _mk_quest(store: Store, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _set_status(store: Store, ref_id: int, status: str) -> None:
    store.add_tag(
        ref_id,
        Tag.closed("STATUS", status),
        set_by="agent",
        replace_prefix=True,
    )


def _age_status(store: Store, ref_id: int, age_sql: str) -> None:
    """Backdate ``ref_id``'s current STATUS tag row's ``created_at``
    server-side (e.g. ``'2 minutes'``) — same trick as
    ``tests/test_quest_loop.py``'s helper, works for a quest's
    STATUS:active tag or a job's STATUS: terminal tag alike."""
    with store.pool.connection() as conn:
        conn.execute(
            """
            UPDATE ref_tags rt SET created_at = (now() - (%s)::interval)
              FROM tags t
             WHERE rt.tag_id = t.tag_id AND t.namespace = 'STATUS'
               AND rt.ref_id = %s
            """,
            (age_sql, ref_id),
        )
        conn.commit()


def _set_rest_reason(store: Store, job_id: int, reason: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (Jsonb({"rest_reason": reason}), job_id),
        )
        conn.commit()


# ── pass-dead-on-host ────────────────────────────────────────────────────


def test_pass_dead_fires_for_silent_handler_on_live_host(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _uniq("classifyish")
    monkeypatch.setenv("PRECIS_COND_PASS_DEAD_BUDGET_S", "3600")
    monkeypatch.setenv("PRECIS_COND_PASS_DEAD_MIN_ROWS", "5")
    _seed_heartbeat(store)
    _seed_pass_logs(store, handler, n=10, last_age_s=4 * 3600.0)
    found = [f for f in _probe_pass_dead(store) if handler in f.key]
    assert len(found) == 1
    assert found[0].heal == HealRequest("restart-worker", _HOST, _PROC)
    assert "silent" in found[0].detail


def test_pass_dead_quiet_when_handler_recent(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _uniq("livepass")
    monkeypatch.setenv("PRECIS_COND_PASS_DEAD_BUDGET_S", "3600")
    monkeypatch.setenv("PRECIS_COND_PASS_DEAD_MIN_ROWS", "5")
    _seed_heartbeat(store)
    _seed_pass_logs(store, handler, n=10, last_age_s=60.0)
    assert [f for f in _probe_pass_dead(store) if handler in f.key] == []


def test_pass_dead_quiet_when_host_dark(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead HOST is the nursery's host-dark business — this per-pass row
    must not pile on."""
    handler = _uniq("darkhost")
    monkeypatch.setenv("PRECIS_COND_PASS_DEAD_BUDGET_S", "3600")
    monkeypatch.setenv("PRECIS_COND_PASS_DEAD_MIN_ROWS", "5")
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM host_heartbeat WHERE host = %s", (_HOST,))
        conn.commit()
    _seed_pass_logs(store, handler, n=10, last_age_s=4 * 3600.0)
    assert [f for f in _probe_pass_dead(store) if handler in f.key] == []


def test_pass_dead_respects_service_config_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prio=0 (live off switch) means silence is deliberate, not a death."""
    handler = _uniq("switchedoff")
    monkeypatch.setenv("PRECIS_COND_PASS_DEAD_BUDGET_S", "3600")
    monkeypatch.setenv("PRECIS_COND_PASS_DEAD_MIN_ROWS", "5")
    _seed_heartbeat(store)
    _seed_pass_logs(store, handler, n=10, last_age_s=4 * 3600.0)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) VALUES (%s, %s, 0) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = 0",
            (_HOST, handler),
        )
        conn.commit()
    try:
        assert [f for f in _probe_pass_dead(store) if handler in f.key] == []
    finally:
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM service_config WHERE host = %s AND service = %s",
                (_HOST, handler),
            )
            conn.commit()


# ── rescue-pass-cadence (gr204708 — demand-gated quest_loop_reconcile) ────


def test_rescue_cadence_quest_reconcile_fires_when_active_quest_waited(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_COND_RESCUE_GAP_BUDGET_S", "60")
    _seed_heartbeat(store)
    _seed_pass_logs(store, "quest_loop_reconcile", n=1, last_age_s=120.0)
    q = _mk_quest(store, "a striving that needs a fresh loop")
    job_id, _ = loop_mod.ensure_quest_loop(store, q)
    assert job_id is not None
    _set_status(store, job_id, "cancelled")
    # A second, non-STATUS tag on the job (the reaper stamps
    # reaped:reboot-orphan on exactly this cancelled shape) — regression
    # guard for the demand SQL's DISTINCT ON tie-break: the extra ref_tags
    # row must not shadow the STATUS row.
    store.add_tag(job_id, Tag.open("reaped:reboot-orphan"), set_by="agent")
    _age_status(store, job_id, "2 minutes")

    found = [
        f
        for f in _probe_rescue_cadence(store)
        if f.key == "rescue-gap:quest_loop_reconcile"
    ]
    assert len(found) == 1
    assert "waiting" in found[0].detail


def test_rescue_cadence_quest_reconcile_quiet_with_no_active_quests(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gr204708 false positive: a cadence gap with nothing waiting."""
    monkeypatch.setenv("PRECIS_COND_RESCUE_GAP_BUDGET_S", "60")
    _seed_heartbeat(store)
    _seed_pass_logs(store, "quest_loop_reconcile", n=1, last_age_s=120.0)

    found = [
        f
        for f in _probe_rescue_cadence(store)
        if f.key == "rescue-gap:quest_loop_reconcile"
    ]
    assert found == []


def test_rescue_cadence_quest_reconcile_quiet_when_loop_live(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-terminal (running) loop means the loop is live — not waiting."""
    monkeypatch.setenv("PRECIS_COND_RESCUE_GAP_BUDGET_S", "60")
    _seed_heartbeat(store)
    _seed_pass_logs(store, "quest_loop_reconcile", n=1, last_age_s=120.0)
    q = _mk_quest(store, "a striving mid-tick")
    job_id, _ = loop_mod.ensure_quest_loop(store, q)
    assert job_id is not None
    _set_status(store, job_id, "running")

    found = [
        f
        for f in _probe_rescue_cadence(store)
        if f.key == "rescue-gap:quest_loop_reconcile"
    ]
    assert found == []


def test_rescue_cadence_quest_reconcile_quiet_during_failed_backoff(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A most-recent 'failed' loop is deliberate exponential backoff
    (``_failed_rest_cooldown_active``) — not starvation."""
    monkeypatch.setenv("PRECIS_COND_RESCUE_GAP_BUDGET_S", "60")
    _seed_heartbeat(store)
    _seed_pass_logs(store, "quest_loop_reconcile", n=1, last_age_s=120.0)
    q = _mk_quest(store, "a striving resting on failure")
    job_id, _ = loop_mod.ensure_quest_loop(store, q)
    assert job_id is not None
    _set_status(store, job_id, "failed")
    _age_status(store, job_id, "2 minutes")

    found = [
        f
        for f in _probe_rescue_cadence(store)
        if f.key == "rescue-gap:quest_loop_reconcile"
    ]
    assert found == []


def test_rescue_cadence_quest_reconcile_quiet_during_dry_rest(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry rest (succeeded + meta.rest_reason='dry') is a deliberate
    cooldown (``_dry_rest_cooldown_active``) — not starvation."""
    monkeypatch.setenv("PRECIS_COND_RESCUE_GAP_BUDGET_S", "60")
    _seed_heartbeat(store)
    _seed_pass_logs(store, "quest_loop_reconcile", n=1, last_age_s=120.0)
    q = _mk_quest(store, "a striving resting dry")
    job_id, _ = loop_mod.ensure_quest_loop(store, q)
    assert job_id is not None
    _set_status(store, job_id, "succeeded")
    _set_rest_reason(store, job_id, "dry")
    _age_status(store, job_id, "2 minutes")

    found = [
        f
        for f in _probe_rescue_cadence(store)
        if f.key == "rescue-gap:quest_loop_reconcile"
    ]
    assert found == []


def test_rescue_cadence_sweeper_fires_on_gap_alone(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pure-cadence handlers (system-profile, fast rotation) fire on the
    gap alone — no demand probe is consulted."""
    monkeypatch.setenv("PRECIS_COND_RESCUE_GAP_BUDGET_S", "60")
    _seed_heartbeat(store)
    _seed_pass_logs(store, "sweeper", n=1, last_age_s=120.0)

    found = [f for f in _probe_rescue_cadence(store) if f.key == "rescue-gap:sweeper"]
    assert len(found) == 1
    assert "waiting" not in found[0].detail


# ── pass-wedged ──────────────────────────────────────────────────────────


def test_pass_wedged_fires_on_stale_activity(store: Store) -> None:
    _seed_heartbeat(
        store,
        meta={
            "activity": {
                _PROC: {"pass": "_bib_parse_pass", "since": "2020-01-01T00:00:00+00:00"}
            }
        },
    )
    found = [f for f in _probe_pass_wedged(store) if _HOST in f.key]
    assert len(found) == 1
    assert "_bib_parse_pass" in found[0].detail
    assert found[0].heal == HealRequest("restart-worker", _HOST, _PROC)


def test_pass_wedged_quiet_when_idle_or_fresh(store: Store) -> None:
    _seed_heartbeat(
        store,
        meta={"activity": {_PROC: {"idle": True, "last_pass": "x", "finished": "y"}}},
    )
    assert [f for f in _probe_pass_wedged(store) if _HOST in f.key] == []


# ── llm-degraded ─────────────────────────────────────────────────────────


def test_llm_degraded_fires_on_error_rate(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _uniq("mdl")
    monkeypatch.setenv("PRECIS_COND_LLM_MIN_CALLS", "5")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_call_log (source, tier, transport, model, errored) "
            "SELECT 'cond-test', 'SMALL', 'local', %(model)s, gs %% 2 = 0 "
            "  FROM generate_series(1, 10) gs",
            {"model": model},
        )
        conn.commit()
    found = [f for f in _probe_llm_degraded(store) if model in f.key]
    assert len(found) == 1
    assert "50%" in found[0].detail


# ── dead-generation-claims (the claims-vs-liveness panel row) ────────────


def test_dead_generation_claims_fires_on_stale_epoch_dead_hold(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_COND_DEAD_GEN_AGE_S", "60")
    res = _uniq("llm:cond")
    _seed_heartbeat(store, meta={"boot_ids": {_PROC: "11ce11ce" * 4}})
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM resource_slot_holds WHERE resource = %s", (res,))
        conn.execute(
            "INSERT INTO resource_slot_holds "
            "(host, resource, units, holder, acquired_at, expires_at, "
            " holder_host, holder_process, holder_boot_id) "
            "VALUES (%s, %s, 1, 'cond:1', now() - interval '2 hours', "
            "        now() + interval '1 hour', %s, %s, %s)",
            (_HOST, res, _HOST, _PROC, "deadbeef" * 4),
        )
        conn.commit()
    found = [
        f for f in _probe_dead_generation_claims(store) if f"{_HOST}/{_PROC}" in f.key
    ]
    assert len(found) == 1
    assert "reclaim lane" in found[0].detail
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM resource_slot_holds WHERE resource = %s", (res,))
        conn.commit()


# ── settings-env-shadowed (db-resident-settings.md slice 4 visibility) ───


def test_settings_env_shadowed_fires_when_db_row_now_wins(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis import settings as psettings

    key = "contact.polite_email"
    monkeypatch.delenv("PRECIS_UNPAYWALL_EMAIL", raising=False)
    psettings.invalidate(key)
    psettings.set_setting(key, "ops@example.org", store=store)
    try:
        _seed_heartbeat(store, meta={"settings_env_present": [key]})
        found = [
            f
            for f in _probe_settings_env_shadowed(store)
            if f.key == f"settings-env-shadowed:{_HOST}/{key}"
        ]
        assert len(found) == 1
        assert "safe to drop" in found[0].detail
    finally:
        psettings.clear_setting(key, store=store)


def test_settings_env_shadowed_quiet_when_no_db_row(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis import settings as psettings

    key = "contact.polite_email"
    monkeypatch.delenv("PRECIS_UNPAYWALL_EMAIL", raising=False)
    psettings.clear_setting(key, store=store)  # ensure no DB row lingers
    _seed_heartbeat(store, meta={"settings_env_present": [key]})
    found = [
        f
        for f in _probe_settings_env_shadowed(store)
        if f.key == f"settings-env-shadowed:{_HOST}/{key}"
    ]
    assert found == []


def test_settings_env_shadowed_ignores_unregistered_keys(store: Store) -> None:
    _seed_heartbeat(store, meta={"settings_env_present": ["not.a.real.key"]})
    assert _probe_settings_env_shadowed(store) == []


# ── run_condition_checks mapping ─────────────────────────────────────────


def test_checks_mapping_green_stale_unknown(store: Store) -> None:
    def _green(_s: Store) -> list[ConditionFinding]:
        return []

    def _fired(_s: Store) -> list[ConditionFinding]:
        return [ConditionFinding(key="x:1", detail="d")]

    def _boom(_s: Store) -> list[ConditionFinding]:
        raise RuntimeError("probe broke")

    checks, findings = run_condition_checks(
        store,
        conditions=(
            Condition("green-row", "warn", _green),
            Condition("fired-row", "warn", _fired),
            Condition("broken-row", "warn", _boom),
        ),
    )
    by_status = {c.status: c for c in checks}
    assert by_status["ok"].name == "green-row"
    assert by_status["stale"].name == "x:1" and by_status["stale"].is_finding
    assert by_status["unknown"].name == "broken-row"
    assert not by_status["unknown"].is_finding  # never a false alarm
    assert [f.key for f in findings] == ["x:1"]


# ── bounded_heal ─────────────────────────────────────────────────────────


def _spec(key: str, *, cap: int = 3, cooldown: float = 3600.0) -> HealSpec:
    return HealSpec(key=key, cap=cap, base_cooldown_s=cooldown, title="t", detail="d")


def test_bounded_heal_first_attempt_runs_then_cooldown(store: Store) -> None:
    key = _uniq("heal")
    calls = {"n": 0}

    def _act() -> bool:
        calls["n"] += 1
        return True

    assert run_bounded_heal(store, _spec(key), _act) == "healed"
    assert calls["n"] == 1
    # Immediately again: inside the cooldown, no second action.
    assert run_bounded_heal(store, _spec(key), _act) == "cooldown"
    assert calls["n"] == 1


def test_bounded_heal_cap_latches_and_files_one_gripe(store: Store) -> None:
    key = _uniq("healcap")
    assert run_bounded_heal(store, _spec(key, cap=1), lambda: True) == "healed"
    # cap reached → latch + gripe, action NOT run again
    ran = {"n": 0}

    def _never() -> bool:
        ran["n"] += 1
        return True

    assert run_bounded_heal(store, _spec(key, cap=1), _never) == "capped"
    assert run_bounded_heal(store, _spec(key, cap=1), _never) == "latched"
    assert ran["n"] == 0
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'gripe' AND title LIKE %s",
            (f"[bounded-heal] {key}%",),
        ).fetchone()
    assert n is not None and int(n[0]) == 1


def test_bounded_heal_state_ages_out_to_fresh_budget(store: Store) -> None:
    key = _uniq("healage")
    assert run_bounded_heal(store, _spec(key, cap=1), lambda: True) == "healed"
    # Backdate the state past reset_after_s → a NEW incident gets a budget.
    with store.pool.connection() as conn:
        raw = conn.execute(
            "SELECT value FROM app_settings WHERE key = %s",
            (f"bounded_heal:{key}",),
        ).fetchone()
        assert raw is not None
        state = json.loads(raw[0])
        state["last_at"] = "2020-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE app_settings SET value = %s WHERE key = %s",
            (json.dumps(state, sort_keys=True), f"bounded_heal:{key}"),
        )
        conn.commit()
    assert run_bounded_heal(store, _spec(key, cap=1), lambda: True) == "healed"


def test_bounded_heal_failed_action_still_burns_attempt(store: Store) -> None:
    key = _uniq("healfail")
    assert run_bounded_heal(store, _spec(key), lambda: False) == "failed"
    assert run_bounded_heal(store, _spec(key), lambda: True) == "cooldown"


# ── the restart-once heal arm ────────────────────────────────────────────


def _wedged_finding() -> ConditionFinding:
    return ConditionFinding(
        key=f"pass-wedged:{_HOST}/{_PROC}",
        detail="wedged",
        heal=HealRequest("restart-worker", _HOST, _PROC),
    )


def test_heal_arm_dark_by_default(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_RESTART_ONCE_ENABLED", raising=False)
    ran: list[list[str]] = []
    assert run_condition_heals(store, [_wedged_finding()], runner=ran.append) == 0  # type: ignore[arg-type]
    assert ran == []


def test_heal_arm_runs_vetted_command_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_RESTART_ONCE_ENABLED", "1")
    _seed_heartbeat(store, meta={"platform": "Darwin"})
    # unique key per test run: clear any prior bounded_heal state
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM app_settings WHERE key = %s",
            (f"bounded_heal:restart-worker:{_HOST}:{_PROC}",),
        )
        conn.commit()
    ran: list[list[str]] = []

    def _runner(cmd: list[str]) -> bool:
        ran.append(cmd)
        return True

    n = run_condition_heals(
        store, [_wedged_finding(), _wedged_finding()], runner=_runner
    )
    assert n == 1  # deduped per (host, process) within a pass
    assert len(ran) == 1
    cmd = ran[0]
    assert cmd[0] == "ssh" and f"deploy@{_HOST}" in cmd
    assert cmd[-4:] == ["/bin/launchctl", "kickstart", "-k", "system/com.precis.worker"]
    # Second tick, still red: cap=1 → no second bounce (capped → gripe).
    n2 = run_condition_heals(store, [_wedged_finding()], runner=_runner)
    assert n2 == 0
    assert len(ran) == 1


def test_heal_arm_ignores_unwhitelisted_process(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_RESTART_ONCE_ENABLED", "1")
    ran: list[list[str]] = []

    def _runner(cmd: list[str]) -> bool:
        ran.append(cmd)
        return True

    f = ConditionFinding(
        key="x", detail="d", heal=HealRequest("restart-worker", _HOST, "asa-bot")
    )
    assert run_condition_heals(store, [f], runner=_runner) == 0
    assert ran == []
