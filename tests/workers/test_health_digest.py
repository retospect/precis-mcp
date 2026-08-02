"""§D liveness-net tests — ``workers/health_digest.py``.

Mirrors the spec's acceptance matrix (docs handed to the implementer):
(a) stopped cadence reported stale within interval+margin; (b) idle-vs-stuck
for the embed backlog check; (c) zero-LLM pure-template rendering; (d) push
policy (daily heartbeat / on-degradation / no-op when green and fresh);
(e) watchdog alert dedup + auto-resolve; (f) Layer-2 derivation needs zero
digest edits for a newly-minted spec; (h) the dead-man ping.

Plus the review-finding regression set: (1) Layer-2 idle-vs-stuck (a
claimed=0-only handler is not "silent"); (2) ``chunks_extracted`` body-row +
input-aware staleness; (4) ``last_push`` only stamped on an actual push;
(5) the dead-man ping's LAN opt-in; (6) the push title keeps "degraded" even
when the heartbeat also happens to be due; (7) the ``hosts_alive`` LIMIT
matches nursery's ``host-dark`` LIMIT.
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from precis.alerts import OPS_ALERT_TARGET_ENV, list_open_alerts
from precis.workers import health_digest, nursery
from precis.workers.health_digest import (
    CheckResult,
    _cadence_staleness_checks,
    _check_chunks_extracted,
    _idle_aware_backlog_checks,
    _layer2_checks,
    _maybe_push,
    _ping_deadman,
    _render_digest,
    _sync_alerts,
    run_health_digest_pass,
)
from precis.workers.registry import ServiceKind, ServiceSpec

# ── (c) zero-LLM, pure template ──────────────────────────────────────────


def test_module_imports_no_llm() -> None:
    """The digest must still send when the LLM/agent fleet is down — assert
    there is no ``llm`` import anywhere in the module (AST-level, not a
    runtime accident)."""
    src = Path(health_digest.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "llm" in alias.name.lower():
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if "llm" in node.module.lower():
                bad.append(node.module)
    assert not bad, f"health_digest.py must not import LLM code: {bad}"


def test_render_digest_all_green_needs_no_db() -> None:
    checks = [
        CheckResult("g", "a", "ok", "fine", "warn"),
        CheckResult("g", "b", "ok", "fine", "info"),
    ]
    body = _render_digest(checks)
    assert "all green" in body
    assert "2 checks" in body


def test_render_digest_orders_worst_and_oldest_first() -> None:
    checks = [
        CheckResult("g", "info-stale", "stale", "soft rot", "info", age_hours=1.0),
        CheckResult("g", "warn-old", "stale", "old warn", "warn", age_hours=100.0),
        CheckResult("g", "warn-new", "stale", "new warn", "warn", age_hours=1.0),
    ]
    body = _render_digest(checks)
    # warn before info; among warns, oldest (highest age_hours) first.
    assert body.index("warn-old") < body.index("warn-new") < body.index("info-stale")


def test_render_digest_unknown_checks_render_separately() -> None:
    checks = [CheckResult("g", "broken", "unknown", "probe blew up", "warn")]
    body = _render_digest(checks)
    assert "could not run" in body
    assert "broken" in body


# ── (a) cadence staleness ────────────────────────────────────────────────


def _seed_lease(store, name: str, *, interval_s: int, overdue_s: float) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO scheduler_leases (name, interval_s, next_fire_at, last_fired_at) "
            "VALUES (%s, %s, now() - (%s || ' seconds')::interval, now() - (%s || ' seconds')::interval) "
            "ON CONFLICT (name) DO UPDATE SET interval_s = EXCLUDED.interval_s, "
            "next_fire_at = EXCLUDED.next_fire_at, last_fired_at = EXCLUDED.last_fired_at",
            (name, interval_s, overdue_s, overdue_s),
        )
        conn.commit()


def test_stopped_cadence_reported_stale_within_interval_plus_margin(store) -> None:
    # interval 60s, margin = max(60, 300) = 300s; overdue by 400s > margin.
    _seed_lease(store, "c-stopped", interval_s=60, overdue_s=400)
    results = _cadence_staleness_checks(store)
    hit = next(r for r in results if r.name == "c-stopped")
    assert hit.status == "stale"


def test_cadence_within_margin_is_ok(store) -> None:
    _seed_lease(store, "c-fresh", interval_s=60, overdue_s=30)
    results = _cadence_staleness_checks(store)
    hit = next(r for r in results if r.name == "c-fresh")
    assert hit.status == "ok"


# ── (b) idle-vs-stuck for the embed backlog check ───────────────────────


def test_empty_backlog_quiet_producer_is_ok(store, monkeypatch) -> None:
    monkeypatch.setattr(
        health_digest.health_checks,
        "compute_backlog_counts",
        lambda conn: {
            "embed": {"pending": 0, "done": 10, "failed": 0},
            "chunk_keywords": {"pending": 0, "done": 10, "failed": 0, "blocked": 0},
        },
    )
    results = _idle_aware_backlog_checks(conn=None)
    embed = next(r for r in results if r.name == "embed")
    assert embed.status == "ok"


def test_non_draining_backlog_past_budget_is_stale(store, monkeypatch) -> None:
    stale_ts = datetime.now(UTC) - timedelta(hours=5)  # embed budget is 2h
    monkeypatch.setattr(
        health_digest.health_checks,
        "compute_backlog_counts",
        lambda conn: {
            "embed": {"pending": 42, "done": 10, "failed": 0, "last_ts": stale_ts},
            "chunk_keywords": {"pending": 0, "done": 10, "failed": 0, "blocked": 0},
        },
    )
    results = _idle_aware_backlog_checks(conn=None)
    embed = next(r for r in results if r.name == "embed")
    assert embed.status == "stale"
    assert "NOT draining" in embed.detail


def test_backlog_pending_but_last_batch_recent_is_ok(store, monkeypatch) -> None:
    """A backlog exists but is actively draining (recent successful batch)."""
    fresh_ts = datetime.now(UTC) - timedelta(minutes=5)
    monkeypatch.setattr(
        health_digest.health_checks,
        "compute_backlog_counts",
        lambda conn: {
            "embed": {"pending": 500, "done": 10, "failed": 0, "last_ts": fresh_ts},
            "chunk_keywords": {"pending": 0, "done": 10, "failed": 0, "blocked": 0},
        },
    )
    results = _idle_aware_backlog_checks(conn=None)
    embed = next(r for r in results if r.name == "embed")
    assert embed.status == "ok"
    assert "draining" in embed.detail


# ── (d) push policy ──────────────────────────────────────────────────────


def _fake_queue_ops_message(calls: list[tuple[str, str]]):
    def _fake(store, title: str, body: str, **kwargs: object) -> bool:
        calls.append((title, body))
        return True

    return _fake


def test_all_green_daily_heartbeat_pushes(store, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        health_digest, "queue_ops_message", _fake_queue_ops_message(calls)
    )
    checks = [CheckResult("g", "a", "ok", "fine", "warn")]
    pushed = _maybe_push(store, checks, degraded=False)
    assert pushed is True
    assert len(calls) == 1
    assert "all green" in calls[0][1]


def test_degradation_triggers_off_cycle_push(store, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        health_digest, "queue_ops_message", _fake_queue_ops_message(calls)
    )
    # Simulate "just pushed" so only the degradation path should fire.
    health_digest._stamp_pushed(store)
    checks = [CheckResult("g", "a", "stale", "broke", "warn")]
    pushed = _maybe_push(store, checks, degraded=True)
    assert pushed is True
    assert len(calls) == 1


def test_no_push_when_green_and_recently_pushed(store, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        health_digest, "queue_ops_message", _fake_queue_ops_message(calls)
    )
    health_digest._stamp_pushed(store)
    checks = [CheckResult("g", "a", "ok", "fine", "warn")]
    pushed = _maybe_push(store, checks, degraded=False)
    assert pushed is False
    assert calls == []


# ── (4) last_push only stamped when the push actually queued ────────────


def test_dark_target_does_not_stamp_last_push(store, monkeypatch) -> None:
    """The real ``queue_ops_message`` returns False (no-op) when
    ``PRECIS_OPS_ALERT_TARGET`` is unset — ``_maybe_push`` must not stamp
    ``last_push`` in that case, or a dark target would silently arm the
    24h heartbeat clock forever while nothing is ever actually pushed."""
    monkeypatch.delenv(OPS_ALERT_TARGET_ENV, raising=False)
    assert health_digest._last_push_at(store) is None
    checks = [CheckResult("g", "a", "ok", "fine", "warn")]
    pushed = _maybe_push(store, checks, degraded=False)
    assert pushed is False
    assert health_digest._last_push_at(store) is None


def test_set_target_first_eval_pushes_immediately_and_stamps(
    store, monkeypatch
) -> None:
    """With no prior stamp, the first eval is heartbeat_due (last_push is
    None) and — with a target configured — pushes right away and stamps."""
    monkeypatch.setenv(OPS_ALERT_TARGET_ENV, "discord/g/c")
    assert health_digest._last_push_at(store) is None
    checks = [CheckResult("g", "a", "ok", "fine", "warn")]
    pushed = _maybe_push(store, checks, degraded=False)
    assert pushed is True
    assert health_digest._last_push_at(store) is not None


# ── (6) push title keeps "degraded" even when heartbeat is also due ─────


def test_push_title_keeps_degraded_marker_when_heartbeat_also_due(
    store, monkeypatch
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        health_digest, "queue_ops_message", _fake_queue_ops_message(calls)
    )
    # No prior stamp => heartbeat_due is True; degraded is also True.
    assert health_digest._last_push_at(store) is None
    checks = [CheckResult("g", "a", "stale", "broke", "warn")]
    pushed = _maybe_push(store, checks, degraded=True)
    assert pushed is True
    assert "degraded" in calls[0][0]


# ── (e) watchdog alert dedup + auto-resolve ─────────────────────────────


def test_watchdog_alerts_dedup_and_auto_resolve(store) -> None:
    stale = [CheckResult("mygroup", "flaky-check", "stale", "still broken", "warn")]
    _, _, first_degraded = _sync_alerts(store, stale)
    _, _, repeat_degraded = _sync_alerts(store, stale)  # repeat — must not duplicate

    open_alerts = list_open_alerts(store)
    matches = [a for a in open_alerts if a["source"] == "watchdog:mygroup"]
    assert len(matches) == 1
    assert matches[0]["title"].endswith("stale")

    # degraded (a push-worthy "new stale finding") only on the FIRST sighting
    # — a standing already-alerted condition must not keep triggering an
    # off-cycle push every hour.
    assert first_degraded is True
    assert repeat_degraded is False

    # Condition clears — the next sync with an empty live set auto-resolves it.
    _sync_alerts(store, [CheckResult("mygroup", "flaky-check", "ok", "fixed", "warn")])
    open_alerts_after = list_open_alerts(store)
    assert not any(a["source"] == "watchdog:mygroup" for a in open_alerts_after)


# ── (f) Layer-2 — zero digest edits for a new spec ──────────────────────


def test_layer2_flags_a_freshly_minted_enabled_spec_with_no_logs(store) -> None:
    fake = ServiceSpec(
        name=f"fake-pass-{id(object())}",
        label="Fake pass (test-only)",
        category="test",
        kind=ServiceKind.PASS,
        ref_pass=True,
        default_profiles=frozenset({"system"}),  # structurally enabled
    )
    results = _layer2_checks(store, specs=[fake])
    assert len(results) == 1
    assert results[0].name == fake.name
    assert results[0].status == "stale"
    assert "intended-on but silent" in results[0].detail


def test_layer2_ignores_a_disabled_spec(store) -> None:
    fake = ServiceSpec(
        name=f"fake-off-{id(object())}",
        label="Fake disabled pass",
        category="test",
        kind=ServiceKind.PASS,
        ref_pass=True,
        # no default_profiles, no enable_env → not structurally enabled,
        # and (barring a service_config override, absent here) not
        # "enabled somewhere" — must not appear as a finding.
    )
    results = _layer2_checks(store, specs=[fake])
    assert results == []


# ── (1) Layer-2 idle-vs-stuck: claimed=0-only is not "silent" ───────────


def _seed_activity_row(
    store, handler: str, *, claimed: int = 0, ok: int = 0, failed: int = 0
) -> None:
    """Insert one ``worker_logs`` row shaped exactly like ``run_loop``'s
    per-cycle log (``workers/runner.py``) — every cycle logs one of these
    regardless of ``claimed``, including a healthy-idle ``claimed=0``."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs (ts, host, process, level, logger, message, payload) "
            "VALUES (now(), 'th', 'precis-worker', 'INFO', 'precis.workers.runner', "
            "%(msg)s, %(payload)s::jsonb)",
            {
                "msg": f"worker: {handler} claimed={claimed} ok={ok} failed={failed}",
                "payload": json.dumps(
                    {"handler": handler, "claimed": claimed, "ok": ok, "failed": failed}
                ),
            },
        )
        conn.commit()


def test_layer2_idle_pass_with_only_claimed_zero_rows_is_not_flagged(store) -> None:
    """A pass that ran all day with claimed=0 (healthy idle) has no
    last_ok/last_fail — but it DID log, so it must not read as "silent"."""
    fake = ServiceSpec(
        name=f"fake-idle-{id(object())}",
        label="Fake idle pass (test-only)",
        category="test",
        kind=ServiceKind.PASS,
        ref_pass=True,
        default_profiles=frozenset({"system"}),
    )
    _seed_activity_row(store, fake.log_handler, claimed=0, ok=0, failed=0)

    results = _layer2_checks(store, specs=[fake])
    assert results == []


def test_layer2_pass_with_zero_worker_logs_rows_is_flagged(store) -> None:
    """The complement: genuinely zero rows in the window IS silent."""
    fake = ServiceSpec(
        name=f"fake-dark-{id(object())}",
        label="Fake dark pass (test-only)",
        category="test",
        kind=ServiceKind.PASS,
        ref_pass=True,
        default_profiles=frozenset({"system"}),
    )
    results = _layer2_checks(store, specs=[fake])
    assert len(results) == 1
    assert results[0].name == fake.name
    assert results[0].status == "stale"


# ── (2) chunks_extracted: body-row, input-aware staleness ───────────────


def _seed_paper(store, *, hours_ago: float) -> int:
    ref = store.insert_ref(
        kind="paper", slug=f"test-paper-{id(object())}", title="P", meta={}
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - (%s || ' hours')::interval "
            "WHERE ref_id = %s",
            (hours_ago, ref.id),
        )
        conn.commit()
    return int(ref.id)


def _seed_chunk(
    store,
    ref_id: int,
    *,
    ord: int = 0,
    hours_ago: float = 0.0,
    chunk_kind: str = "paragraph",
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, created_at) "
            "VALUES (%s, %s, %s, 'x', now() - (%s || ' hours')::interval)",
            (ref_id, ord, chunk_kind, hours_ago),
        )
        conn.commit()


def test_chunks_extracted_quiet_when_no_new_papers(store) -> None:
    """No paper has landed since the newest body chunk — a stale-looking
    chunk with nothing new waiting behind it is healthy idle, not stuck."""
    paper = _seed_paper(store, hours_ago=20)
    _seed_chunk(store, paper, ord=0, hours_ago=10)  # well past the 6h budget

    with store.pool.connection() as conn:
        result = _check_chunks_extracted(conn)
    assert result.status == "ok"


def test_chunks_extracted_stale_when_paper_newer_than_chunk_past_budget(store) -> None:
    """A paper landed (well past budget) after the newest body chunk, with
    nothing extracted for it since — a genuinely stalled pipeline."""
    old_paper = _seed_paper(store, hours_ago=30)
    _seed_chunk(store, old_paper, ord=0, hours_ago=20)  # newest body chunk
    _seed_paper(store, hours_ago=10)  # landed after it, still > 6h budget old

    with store.pool.connection() as conn:
        result = _check_chunks_extracted(conn)
    assert result.status == "stale"


def test_chunks_extracted_ignores_card_forge_rewrite(store) -> None:
    """A fresh card_forge ord<0 rewrite must not mask a stalled body
    extraction — only ord>=0 rows count as 'extracted' output."""
    old_paper = _seed_paper(store, hours_ago=30)
    _seed_chunk(store, old_paper, ord=0, hours_ago=20)
    _seed_chunk(
        store, old_paper, ord=-1, hours_ago=0.1, chunk_kind="card_combined"
    )  # fresh, but not a body row
    _seed_paper(store, hours_ago=10)  # new input, still nothing extracted

    with store.pool.connection() as conn:
        result = _check_chunks_extracted(conn)
    assert result.status == "stale"


# ── (h) dead-man ping ────────────────────────────────────────────────────


def test_deadman_ping_fires_via_safe_get_after_green_eval(monkeypatch) -> None:
    calls: list[str] = []

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_http_client(**kwargs):
        return _FakeClient()

    def fake_safe_get(client, url, /, **kwargs):
        calls.append(url)

        @dataclass
        class _Resp:
            status_code: int = 200

        return _Resp()

    monkeypatch.setenv(
        health_digest.DEADMAN_PING_URL_ENV, "https://example.invalid/ping/abc"
    )
    monkeypatch.setattr("precis.utils.http.http_client", fake_http_client)
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)

    _ping_deadman()

    assert calls == ["https://example.invalid/ping/abc"]


def test_deadman_ping_failure_does_not_raise(monkeypatch) -> None:
    def boom_http_client(**kwargs):
        raise RuntimeError("network is down")

    monkeypatch.setenv(
        health_digest.DEADMAN_PING_URL_ENV, "https://example.invalid/ping/abc"
    )
    monkeypatch.setattr("precis.utils.http.http_client", boom_http_client)

    _ping_deadman()  # must not raise


def test_deadman_ping_dark_when_unset(monkeypatch) -> None:
    calls: list[str] = []

    def _called_http_client(**kwargs: object) -> None:
        calls.append("called")

    monkeypatch.delenv(health_digest.DEADMAN_PING_URL_ENV, raising=False)
    monkeypatch.setattr("precis.utils.http.http_client", _called_http_client)
    _ping_deadman()
    assert calls == []


# ── (5) LAN dead-man target: SSRF-blocked by default, opt-in bypass ─────


def test_deadman_ping_blocked_without_optin_logs_and_does_not_raise(
    monkeypatch, caplog
) -> None:
    from precis.utils.safe_fetch import SsrfBlocked

    def boom_http_client(**kwargs):
        raise SsrfBlocked("refusing host 'lan-target': private range")

    monkeypatch.setenv(health_digest.DEADMAN_PING_URL_ENV, "http://192.168.1.5/ping")
    monkeypatch.delenv(health_digest.DEADMAN_ALLOW_PRIVATE_ENV, raising=False)
    monkeypatch.setattr("precis.utils.http.http_client", boom_http_client)

    with caplog.at_level("WARNING"):
        _ping_deadman()  # must not raise

    assert any(
        health_digest.DEADMAN_ALLOW_PRIVATE_ENV in rec.message for rec in caplog.records
    )


def test_deadman_ping_optin_allows_private_target(monkeypatch) -> None:
    calls: list[str] = []

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResp()

    class _FakeHttpx:
        @staticmethod
        def Client(**kwargs):
            return _FakeClient()

    monkeypatch.setenv(health_digest.DEADMAN_PING_URL_ENV, "http://192.168.1.5/ping")
    monkeypatch.setenv(health_digest.DEADMAN_ALLOW_PRIVATE_ENV, "1")
    monkeypatch.setattr("precis.utils.http.require_httpx", lambda: _FakeHttpx)

    _ping_deadman()

    assert calls == ["http://192.168.1.5/ping"]


# ── (7) hosts_alive LIMIT matches nursery's host-dark LIMIT ─────────────


def test_hosts_alive_limit_matches_nursery_host_dark_limit() -> None:
    """The digest's non-paging ``hosts_alive`` line and nursery's paging
    ``host-dark`` detector must show the same LIMIT — the digest line is
    documented as mirroring the alert, so it must not silently truncate to
    a different count."""
    hd_src = inspect.getsource(health_digest._check_hosts_alive)
    nursery_src = inspect.getsource(nursery._detect_host_dark)
    assert "LIMIT 50" in hd_src
    assert "LIMIT 50" in nursery_src


# ── end-to-end smoke ──────────────────────────────────────────────────────


def test_run_health_digest_pass_end_to_end_smoke(store, monkeypatch) -> None:
    """A full fire with no findings, no push target, no dead-man URL —
    must not raise and must report a sane BatchResult."""
    monkeypatch.delenv(health_digest.DEADMAN_PING_URL_ENV, raising=False)
    result = run_health_digest_pass(store, specs=[])  # no Layer-2 candidates
    assert result.handler == "health_digest"
    assert result.claimed >= 1  # at least the curated + cadence checks ran
