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

§D Phase 2 (the remediation router, ``_route_findings``) + the §F embed-
pipeline culprit diagnosis (``_diagnose_embed_pipeline``) are covered in
their own section near the bottom of this file.
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from precis.alerts import OPS_ALERT_TARGET_ENV, list_open_alerts, raise_alert
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.workers import health_digest, nursery
from precis.workers.health_digest import (
    CheckResult,
    _cadence_staleness_checks,
    _check_chunks_extracted,
    _check_claim_hub_dedup_index,
    _diagnose_embed_pipeline,
    _idle_aware_backlog_checks,
    _layer1_checks,
    _layer2_checks,
    _maybe_push,
    _open_marker_gripes,
    _ping_deadman,
    _render_digest,
    _route_findings,
    _sync_alerts,
    run_health_digest_pass,
)
from precis.workers.registry import ServiceKind, ServiceSpec
from precis.workers.scheduler import Cadence
from precis.workers.service_config import set_service_prio
from tests.workers._helpers import make_mock_bge_m3, seed_ref

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


# ── never-seeded: eligible-everywhere-false blind spot (gr194430) ───────


def test_never_seeded_registry_cadence_is_flagged(store, monkeypatch) -> None:
    """A cadence registered in ``workers/scheduler.py`` whose ``eligible``
    gate is false on every host never calls ``claim_scheduler_lease`` — no
    ``scheduler_leases`` row for it ever exists, so the lease-row loop
    above is blind to it by construction. That sustained absence, not an
    overdue row, is the gr194430 finding."""
    fake = Cadence(name="fake-never-seeded", interval_s=60, run=lambda s, b: None)
    monkeypatch.setattr(health_digest, "CADENCES", (fake,))
    results = _cadence_staleness_checks(store)
    hit = next(r for r in results if r.name == "fake-never-seeded/never-seeded")
    assert hit.status == "stale"
    assert "no scheduler_leases row" in hit.detail


def test_allowlisted_dark_cadence_is_not_flagged(store, monkeypatch) -> None:
    """``materialize`` carries no pre-claim gate (it's flag-gated inside the
    run itself, ``PRECIS_MATERIALIZE_EMBED``) — its lease is always seeded
    regardless, and the explicit exemption documents that it must never
    false-positive here even with no lease row."""
    fake = Cadence(name="materialize", interval_s=300, run=lambda s, b: None)
    monkeypatch.setattr(health_digest, "CADENCES", (fake,))
    results = _cadence_staleness_checks(store)
    assert not any(r.name == "materialize/never-seeded" for r in results)


def test_cadence_with_fresh_lease_is_not_flagged_never_seeded(
    store, monkeypatch
) -> None:
    """A cadence that HAS won its lease at least once must not also read as
    never-seeded — the two conditions are mutually exclusive."""
    fake = Cadence(name="fake-has-lease", interval_s=60, run=lambda s, b: None)
    monkeypatch.setattr(health_digest, "CADENCES", (fake,))
    _seed_lease(store, "fake-has-lease", interval_s=60, overdue_s=30)
    results = _cadence_staleness_checks(store)
    assert not any(r.name == "fake-has-lease/never-seeded" for r in results)
    hit = next(r for r in results if r.name == "fake-has-lease")
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


def test_watchdog_alerts_resolve_all_quiet_group(store) -> None:
    """A finding-only group (e.g. Layer-2 coherence) that goes fully
    healthy emits zero checks next eval — no ``coherence`` key in
    ``by_group`` at all, not even an ``ok`` one. Regression for the bug
    where such a group's alerts (and downstream marker gripes) stayed
    open forever because the per-group loop never visited the source."""
    stale = [CheckResult("coherence", "some-pass", "stale", "silent", "warn")]
    _, _, first_degraded = _sync_alerts(store, stale)
    assert first_degraded is True

    open_alerts = list_open_alerts(store)
    assert any(a["source"] == "watchdog:coherence" for a in open_alerts)

    # Next eval: coherence is entirely quiet — no coherence-group checks at
    # all, only an unrelated group.
    _raised, resolved, _degraded = _sync_alerts(
        store, [CheckResult("mygroup", "flaky-check", "stale", "still broken", "warn")]
    )
    assert resolved >= 1

    open_alerts_after = list_open_alerts(store)
    assert not any(a["source"] == "watchdog:coherence" for a in open_alerts_after)


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
        # no default_profiles → not structurally enabled, and (barring a
        # service_config override, absent here) not "enabled somewhere" —
        # must not appear as a finding.
    )
    results = _layer2_checks(store, specs=[fake])
    assert results == []


def test_layer2_ignores_an_enable_env_only_spec(store) -> None:
    """§L retired ``enable_env`` as a default source: a pass gated only by
    an env var (no ``default_profiles``, no ``service_config`` row) defaults
    OFF, mirroring ``cli/worker.py::_profile_default_on`` — it must not read
    as "intended-on but silent"."""
    fake = ServiceSpec(
        name=f"fake-envonly-{id(object())}",
        label="Fake env-gated pass",
        category="test",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_FAKE_ENABLED",
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


# ── claim_hub_dedup_index: strict claim-hub definition ────────────────────
#
# docs/backlog/claim-hub-definition-divergence.md — a TAPROOT:claim finding
# without STATUS:canonical is a chase-tree finding mid-lifecycle
# (STATUS:established/dead_chain/multi_candidate), never a hub. The
# dedup-index coverage check must not count it in its denominator, or a
# fixed `block()` and a stale check would disagree about coverage.


def _embed_finding_body(store, ref_id: int, text: str, embedder) -> None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0 "
            "AND chunk_kind = 'finding_body' AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (row[0], "bge-m3", embedder.embed_one(text)),
        )
        conn.commit()


def test_claim_hub_dedup_index_excludes_taproot_claim_without_status_canonical(
    store,
) -> None:
    """A finding carrying ``TAPROOT:claim`` but ``STATUS:established`` (a
    chase-tree finding, not a minted hub) must not inflate the denominator
    -- only the one properly minted hub is counted."""
    embedder = make_mock_bge_m3()
    hub = mint_hub(store, CanonicalClaim(sentence="a minted hub claim", scope={}))
    _embed_finding_body(store, hub, "a minted hub claim", embedder)

    chase_ref = seed_ref(store, title="chase finding", kind="finding")
    store.add_tag(chase_ref, Tag.closed("TAPROOT", "claim"), set_by="system")
    store.add_tag(chase_ref, Tag.closed("STATUS", "established"), set_by="system")
    # Deliberately no finding_body / embedding -- if this row were counted
    # it would inflate BOTH the hub total and the unindexed count.

    with store.pool.connection() as conn:
        result = _check_claim_hub_dedup_index(conn)
    assert result.status == "ok"
    assert "1/1 indexed" in result.detail


def test_claim_hub_dedup_index_reports_no_hubs_yet_for_a_bare_chase_finding(
    store,
) -> None:
    """A corpus holding only a ``TAPROOT:claim``-without-``STATUS:canonical``
    finding reads as "no hubs yet", not as an unindexed hub."""
    chase_ref = seed_ref(store, title="chase finding", kind="finding")
    store.add_tag(chase_ref, Tag.closed("TAPROOT", "claim"), set_by="system")
    store.add_tag(chase_ref, Tag.closed("STATUS", "established"), set_by="system")

    with store.pool.connection() as conn:
        result = _check_claim_hub_dedup_index(conn)
    assert result.status == "ok"
    assert "no hubs yet" in result.detail


# ── (3) chunks_classified: idle-aware on the classify service_config gate
# (gr204385) — a bare freshness probe on a default-OFF pass alerts stale
# forever by construction; the check must read as idle-not-a-finding while
# the gate is off, and keep its ordinary 12h staleness semantics once on.


def _chunks_classified(results: list[CheckResult]) -> CheckResult:
    return next(r for r in results if r.name == "chunks_classified")


def test_chunks_classified_idle_when_gate_disabled(store) -> None:
    """No service_config row for `classify` -> default-OFF -> non-finding,
    with a detail line naming the cause (not a bare 'never seen')."""
    result = _chunks_classified(_layer1_checks(store))
    assert result.status != "stale"
    assert not result.is_finding
    assert "disabled" in result.detail
    assert "service_config" in result.detail


def test_chunks_classified_stale_when_gate_enabled_no_recent_tags(store) -> None:
    """Gate ON (a live `service_config` wildcard row) + zero role3 tags ever
    -> stale, exactly the pre-existing freshness semantics."""
    set_service_prio(store, "*", "classify", 5)
    result = _chunks_classified(_layer1_checks(store))
    assert result.status == "stale"


def test_chunks_classified_ok_when_gate_enabled_fresh_tag(store) -> None:
    """Gate ON + a fresh ROLE3 chunk tag -> ok, within the 12h budget."""
    set_service_prio(store, "*", "classify", 5)
    paper = _seed_paper(store, hours_ago=1)
    _seed_chunk(store, paper, ord=0, hours_ago=0.1)
    store.add_tag(paper, Tag.closed("ROLE3", "own"), pos=0)

    result = _chunks_classified(_layer1_checks(store))
    assert result.status == "ok"


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


# ── §D Phase 2: remediation router ──────────────────────────────────────


def _seed_watchdog_alert(
    store,
    *,
    group: str,
    name: str,
    hours_old: float,
    severity: str = "warn",
    title: str | None = None,
    detail: str = "",
) -> int:
    """Raise a ``watchdog:<group>`` alert (via the real :func:`raise_alert`
    dedup path, exactly like ``_sync_alerts`` would) then backdate
    ``created_at`` — mirrors ``_seed_paper``'s backdate idiom above."""
    ref_id, _ = raise_alert(
        store,
        source=f"watchdog:{group}",
        fingerprint=name,
        title=title or f"[{group}] {name} stale",
        detail=detail,
        severity=severity,
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - (%s || ' hours')::interval "
            "WHERE ref_id = %s",
            (hours_old, ref_id),
        )
        conn.commit()
    return int(ref_id)


def test_router_files_exactly_one_gripe_and_dedups_on_second_eval(store) -> None:
    # "coherence" group budget is 24h.
    _seed_watchdog_alert(store, group="coherence", name="some-pass", hours_old=30)

    routed = _route_findings(store)
    assert ("coherence", "some-pass") in routed

    gripes = store.list_refs(kind="gripe", tags=["STATUS:open"])
    assert len(gripes) == 1
    body = store.blocks.list_blocks_for_ref(gripes[0].id)[0].text
    assert body.startswith("watchdog-condition: watchdog:coherence/some-pass")
    assert "STATUS:open" in " ".join(str(t) for t in store.tags_for(gripes[0].id))

    # Second eval — the marker dedup must not file a duplicate.
    routed2 = _route_findings(store)
    assert routed2 == routed
    assert len(store.list_refs(kind="gripe", tags=["STATUS:open"])) == 1


def test_router_under_budget_alert_files_nothing(store) -> None:
    # 1h old, well under coherence's 24h self-heal budget.
    _seed_watchdog_alert(store, group="coherence", name="some-pass", hours_old=1)

    routed = _route_findings(store)
    assert routed == frozenset()
    assert store.list_refs(kind="gripe", tags=["STATUS:open"]) == []


def test_router_never_gripe_group_stays_silent_regardless_of_age(store) -> None:
    """The ``meta`` group (``alert_backlog_rot``) never routes to a gripe —
    a gripe about gripe-rot would feed the very rot it reports."""
    _seed_watchdog_alert(
        store, group="meta", name="alert_backlog_rot", hours_old=1000, severity="info"
    )

    routed = _route_findings(store)
    assert routed == frozenset()
    assert store.list_refs(kind="gripe", tags=["STATUS:open"]) == []


def test_router_auto_closes_when_condition_clears(store) -> None:
    from precis.alerts import resolve_alert
    from precis.store.types import BlockInsert

    alert_id = _seed_watchdog_alert(
        store, group="coherence", name="some-pass", hours_old=30
    )
    _route_findings(store)
    gripes = store.list_refs(kind="gripe", tags=["STATUS:open"])
    assert len(gripes) == 1
    gripe_id = int(gripes[0].id)

    # A plain, non-watchdog gripe must be untouched by the auto-close sweep.
    other = store.insert_ref(kind="gripe", slug=None, title="unrelated", meta={})
    store.blocks.insert_blocks(
        other.id,
        [
            BlockInsert(
                pos=0, text="an ordinary gripe", meta={"chunk_kind": "gripe_body"}
            )
        ],
    )
    store.add_tag(
        other.id, Tag.closed("STATUS", "open"), set_by="system", replace_prefix=True
    )

    # A marker gripe whose alert is STILL open must stay open.
    routed_still_live = _route_findings(store)
    assert ("coherence", "some-pass") in routed_still_live
    with store.pool.connection() as conn:
        still_open = conn.execute(
            "SELECT deleted_at FROM refs WHERE ref_id = %s", (gripe_id,)
        ).fetchone()
    assert still_open[0] is None

    # Condition clears (mirrors what resolve_stale_alerts does when a check
    # goes fresh again).
    resolve_alert(store, alert_id)
    routed_after = _route_findings(store)
    assert routed_after == frozenset()

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM refs WHERE ref_id = %s", (gripe_id,)
        ).fetchone()
        comment = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'gripe_comment' "
            "ORDER BY ord DESC LIMIT 1",
            (gripe_id,),
        ).fetchone()
        other_row = conn.execute(
            "SELECT deleted_at FROM refs WHERE ref_id = %s", (other.id,)
        ).fetchone()
    assert row[0] is not None  # soft-deleted
    assert comment is not None and "auto-closed by health_digest" in comment[0]
    assert other_row[0] is None  # non-watchdog gripe untouched


def test_router_flood_cap_limits_new_gripes_per_eval(store) -> None:
    for i in range(5):
        _seed_watchdog_alert(store, group="coherence", name=f"pass-{i}", hours_old=30)

    routed = _route_findings(store)
    assert len(routed) == 3
    assert len(store.list_refs(kind="gripe", tags=["STATUS:open"])) == 3

    # The other two are picked up on the next eval (no flood-cap re-check
    # needed here — this just proves nothing was lost, only deferred).
    routed2 = _route_findings(store)
    assert len(routed | routed2) == 5
    assert len(store.list_refs(kind="gripe", tags=["STATUS:open"])) == 5


def test_open_marker_gripes_parses_source_and_fingerprint(store) -> None:
    _seed_watchdog_alert(store, group="coherence", name="some-pass", hours_old=30)
    _route_findings(store)
    markers = _open_marker_gripes(store)
    assert ("watchdog:coherence", "some-pass") in markers


# ── §D Phase 3 (alert-triage): nursery capped-backlog aggregate hand-off ──


def _seed_nursery_backlog_alert(
    store,
    *,
    source: str,
    ref_id: int,
    hours_old: float,
    total: int,
    fingerprint: str | None = None,
) -> int:
    """Raise one per-ref nursery alert the way ``run_nursery_pass`` now does
    for a capped-backlog category — ``extra_meta={"total": ...}`` — then
    backdate ``created_at``, mirroring ``_seed_watchdog_alert``."""
    fp = fingerprint or f"{source.removeprefix('nursery:')}:{ref_id}"
    alert_id, _ = raise_alert(
        store,
        source=source,
        fingerprint=fp,
        title=f"[{source}] finding {ref_id}",
        detail="some finding",
        severity="info",
        subject_ref_id=ref_id,
        extra_meta={"total": total},
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - (%s || ' hours')::interval "
            "WHERE ref_id = %s",
            (hours_old, alert_id),
        )
        conn.commit()
    return int(alert_id)


def test_nursery_backlog_non_draining_files_one_aggregate_gripe(store) -> None:
    """A non-draining nursery source (open backlog alerts older than the
    24h budget) files exactly ONE aggregate marker gripe whose body
    carries the total — never one gripe per surfaced ref."""
    for i in range(5):
        _seed_nursery_backlog_alert(
            store, source="nursery:orphan", ref_id=100 + i, hours_old=30, total=297
        )

    routed = _route_findings(store)
    assert ("nursery-backlog", "nursery:orphan") in routed

    gripes = store.list_refs(kind="gripe", tags=["STATUS:open"])
    assert len(gripes) == 1
    body = store.blocks.list_blocks_for_ref(gripes[0].id)[0].text
    assert body.startswith("watchdog-condition: nursery:orphan/backlog")
    assert "297" in body


def test_nursery_backlog_drained_auto_closes_aggregate_gripe(store) -> None:
    """When the backlog fully clears (no open alert left for the source),
    the aggregate gripe auto-closes — nursery's own ``resolve_stale_alerts``
    already closed the alert; this only sweeps the marker gripe."""
    from precis.alerts import resolve_alert

    alert_id = _seed_nursery_backlog_alert(
        store, source="nursery:orphan", ref_id=200, hours_old=30, total=297
    )
    _route_findings(store)
    gripes = store.list_refs(kind="gripe", tags=["STATUS:open"])
    assert len(gripes) == 1
    gripe_id = int(gripes[0].id)

    resolve_alert(store, alert_id)
    routed_after = _route_findings(store)
    assert ("nursery-backlog", "nursery:orphan") not in routed_after

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM refs WHERE ref_id = %s", (gripe_id,)
        ).fetchone()
    assert row[0] is not None


def test_nursery_backlog_under_budget_files_nothing(store) -> None:
    """A nursery backlog source under the 24h self-heal budget files no
    gripe — the per-ref alert itself stays open, unrouted, until aged."""
    _seed_nursery_backlog_alert(
        store, source="nursery:stuck-doable", ref_id=300, hours_old=1, total=60
    )

    routed = _route_findings(store)
    assert routed == frozenset()
    assert store.list_refs(kind="gripe", tags=["STATUS:open"]) == []


def test_nursery_backlog_never_files_per_ref(store) -> None:
    """Even with many surfaced per-ref alerts over budget, exactly ONE
    gripe files (the aggregate) — never one per ref_id, the exact
    per-leaf noise this design forbids."""
    for i in range(10):
        _seed_nursery_backlog_alert(
            store,
            source="nursery:child-failed-parked",
            ref_id=400 + i,
            hours_old=30,
            total=55,
        )

    _route_findings(store)
    gripes = store.list_refs(kind="gripe", tags=["STATUS:open"])
    assert len(gripes) == 1


def test_watchdog_routing_unchanged_alongside_nursery_backlog(store) -> None:
    """A watchdog alert and a nursery backlog alert both over budget in the
    same eval: watchdog behaviour stays byte-identical (its own marker
    gripe, unaffected by the nursery aggregate logic living alongside
    it) — existing router tests above still pass unmodified."""
    _seed_watchdog_alert(store, group="coherence", name="some-pass", hours_old=30)
    _seed_nursery_backlog_alert(
        store, source="nursery:orphan", ref_id=500, hours_old=30, total=100
    )

    routed = _route_findings(store)
    assert ("coherence", "some-pass") in routed
    assert ("nursery-backlog", "nursery:orphan") in routed

    gripes = store.list_refs(kind="gripe", tags=["STATUS:open"])
    assert len(gripes) == 2
    bodies = [store.blocks.list_blocks_for_ref(g.id)[0].text for g in gripes]
    assert any(
        b.startswith("watchdog-condition: watchdog:coherence/some-pass") for b in bodies
    )
    assert any(
        b.startswith("watchdog-condition: nursery:orphan/backlog") for b in bodies
    )


# ── §F coupling: embed-pipeline culprit diagnosis ───────────────────────


def test_diagnose_embed_no_jobs_minted_and_silent_materializer(store) -> None:
    """(a) Nothing minted, materialize has never logged — stage 1 fires."""
    with store.pool.connection() as conn:
        result = _diagnose_embed_pipeline(conn)
    assert "materializer silent" in result


def test_diagnose_embed_queued_jobs_but_no_embedder_slots(store) -> None:
    """(b) A job WAS minted (stage 1 passes) and is queued, but no
    ``resource_slots`` row for ``embedder`` exists anywhere — stage 2
    fires."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="embed_batch (test)",
        meta={"job_type": "embed_batch"},
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "queued"), set_by="system", replace_prefix=True
    )

    with store.pool.connection() as conn:
        result = _diagnose_embed_pipeline(conn)
    assert "no embedder capacity advertised" in result


def test_diagnose_embed_jobs_claimed_but_failing(store) -> None:
    """(c) A job was minted and reached a terminal ``failed`` status with
    no successes in the window — stage 3 fires, with the error snippet."""
    from precis.store.types import BlockInsert

    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="embed_batch (test)",
        meta={"job_type": "embed_batch"},
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "failed"), set_by="system", replace_prefix=True
    )
    store.blocks.insert_blocks(
        ref.id,
        [
            BlockInsert(
                pos=0,
                text="embed_batch: embedder unreachable: boom",
                meta={"chunk_kind": "job_event"},
            )
        ],
    )

    with store.pool.connection() as conn:
        result = _diagnose_embed_pipeline(conn)
    assert "jobs claimed but failing" in result
    assert "boom" in result


def test_diagnose_embed_all_nominal_when_nothing_explains_it(store) -> None:
    """(d) A job succeeded recently and nothing else is amiss — the honest
    fallback, not a false-precision guess."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="embed_batch (test)",
        meta={"job_type": "embed_batch"},
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )

    with store.pool.connection() as conn:
        result = _diagnose_embed_pipeline(conn)
    assert "all stages nominal" in result


# ── router ↔ digest render: routed findings prefix louder ────────────────


def test_render_digest_prefixes_routed_findings() -> None:
    checks = [CheckResult("coherence", "some-pass", "stale", "broke", "warn")]
    body = _render_digest(checks, routed=frozenset({("coherence", "some-pass")}))
    assert "⛳ gripe filed:" in body


def test_render_digest_no_prefix_when_not_routed() -> None:
    checks = [CheckResult("coherence", "some-pass", "stale", "broke", "warn")]
    body = _render_digest(checks)
    assert "⛳" not in body
