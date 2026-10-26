"""Local-first telemetry and a daily, advisory escalation warning.

Savings price accepted ``on`` calls at the latest observed paid model (or the
configured cloud rung when no paid call was observed). This is an estimate:
local reasoning tokens and changing routing prevent a counterfactual bill.
Warnings claim a UTC day on a paid log row before raising an in-process alert.
A failed write consumes the claim: bounded, cross-host at-most-once
delivery is preferable to duplicate warnings. No setting is ever changed.

The op stats view reads both arms, with Wilson agreement bounds, accepted
local answers per shadow helper call, and an explicitly approximate savings
figure at the observed paid model's catalog prices. Escalation rates count
distinct helper invocation tokens across both arms, so circuit skips count
and paired calls do not double the denominator. Failed arms leave agreement
unknown; the separate answer rate exposes their quality cost. A >40%
escalation rate in trailing 24h raises one warning per UTC day via
``alerts.raise_alert``. The offline harness consumes supplied samples and
requires an explicit run opt-in; dry-run has no I/O to providers or a database.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from precis import alerts
from precis.budget import meter
from precis.extract.accept import wilson_interval
from precis.store import Store
from precis.utils.llm import local_first
from precis.utils.llm.operations import LOCAL_FIRST_OPS

log = logging.getLogger(__name__)


@dataclass
class LocalFirstStats:
    calls: int = 0
    on_calls: int = 0
    attempts: int = 0
    accepted: int = 0
    escalated: int = 0
    on_attempts: int = 0
    on_escalated: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    shadow_pairs: int = 0
    shadow_agree: int = 0
    shadow_calls: int = 0
    shadow_answers: int = 0
    spent_usd: float = 0.0
    saved_usd: float | None = None
    savings_model: str | None = None
    unpriced: int = 0

    @property
    def escalation_rate(self) -> float | None:
        return self.escalated / self.calls if self.calls else None


def _configured_paid_model(op: str) -> tuple[str, str] | None:
    from precis.utils.llm import router
    from precis.utils.llm.operations import resolve_op, resolve_op_chain

    resolved = resolve_op(op)
    if resolved is None:
        return None
    tier, model = resolved
    backend = router.resolve_backend()
    chain = resolve_op_chain(op, tools_needed=False, backend=backend)
    if chain is None:
        chain = router.resolve_chain(tier, tools_needed=False, backend=backend)
    for rung in chain:
        if router._rung_is_cloud(rung):
            return rung.model or model or router.resolve_model(
                tier
            ), rung.transport.value
    return None


def _prices(store: Store, model: str, transport: str) -> tuple[float, float] | None:
    ref = store.find_ref_by_meta(kind="llm", key="model_id", value=model)
    if ref is None:
        return None
    offerings = (ref.meta or {}).get("offerings") or []
    matches = [
        o for o in offerings if isinstance(o, dict) and o.get("transport") == transport
    ]
    for offering in matches or offerings:
        if not isinstance(offering, dict) or offering.get("transport") == "local":
            continue
        try:
            price_in = float(offering["price_in"])
            price_out = float(offering["price_out"])
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(p) and p >= 0 for p in (price_in, price_out)):
            return price_in, price_out
    return None


def collect_stats(store: Store, op: str, *, window_days: int = 30) -> LocalFirstStats:
    """Read both source arms; unstamped legacy calls never enter the counts."""
    paid_source = LOCAL_FIRST_OPS[op].paid_source if op in LOCAL_FIRST_OPS else op
    stats = LocalFirstStats()
    with store.pool.connection() as conn:
        # The two arms share an invocation token. Count their union so a
        # circuit skip still contributes one call and a paired call only one.
        counts = conn.execute(
            "WITH calls AS (SELECT "
            "COALESCE(features->'local_first'->>'call', id::text) AS call, "
            "bool_or(features->'local_first'->>'mode' = 'on') AS on_mode, "
            "bool_or(features->'local_first'->>'mode' = 'shadow') AS shadow, "
            "bool_or(features->'local_first'->>'arm' = 'local' "
            "AND features->'local_first'->>'accepted' = 'true') AS answered, "
            "bool_or(features->'local_first'->>'arm' = 'paid' "
            "AND features->'local_first'->>'escalated' IS NOT NULL) AS escalated "
            "FROM llm_call_log WHERE source = ANY(%s) "
            "AND ts > now() - (%s * interval '1 day') "
            "AND features->'local_first' IS NOT NULL GROUP BY 1) "
            "SELECT count(*), count(*) FILTER (WHERE on_mode), "
            "count(*) FILTER (WHERE shadow), "
            "count(*) FILTER (WHERE shadow AND answered), "
            "count(*) FILTER (WHERE escalated), "
            "count(*) FILTER (WHERE on_mode AND escalated) FROM calls",
            (list({op, paid_source, f"{op}:local"}), window_days),
        ).fetchone()
        if counts:
            (
                stats.calls,
                stats.on_calls,
                stats.shadow_calls,
                stats.shadow_answers,
                stats.escalated,
                stats.on_escalated,
            ) = counts
        rows = conn.execute(
            "SELECT features->'local_first'->>'mode', "
            "features->'local_first'->>'arm', "
            "features->'local_first'->>'accepted', "
            "features->'local_first'->>'escalated', "
            "features->'local_first'->>'shadow', "
            "features->'local_first'->>'agree', model, transport, "
            "count(*), COALESCE(sum(cost_usd), 0), "
            "COALESCE(sum(input_tokens) FILTER (WHERE output_tokens IS NOT NULL), 0), "
            "COALESCE(sum(output_tokens) FILTER (WHERE input_tokens IS NOT NULL), 0), "
            "count(*) FILTER (WHERE input_tokens IS NULL OR output_tokens IS NULL), "
            "max(ts) FROM llm_call_log "
            "WHERE source = ANY(%s) AND ts > now() - (%s * interval '1 day') "
            "AND features->'local_first' IS NOT NULL "
            "GROUP BY 1, 2, 3, 4, 5, 6, 7, 8",
            (list({op, paid_source, f"{op}:local"}), window_days),
        ).fetchall()
    saved_in = saved_out = accepted_on = missing_tokens = 0
    paid_models: list[tuple[datetime, str, str]] = []
    for (
        mode,
        arm,
        accepted,
        reason,
        shadow,
        agree,
        model,
        transport,
        count,
        cost,
        tokens_in,
        tokens_out,
        missing,
        latest,
    ) in rows:
        if arm == "local":
            stats.attempts += count
            if mode == "on":
                stats.on_attempts += count
            if accepted == "true":
                stats.accepted += count
                if mode == "on":
                    accepted_on += count
                    saved_in += tokens_in
                    saved_out += tokens_out
                    missing_tokens += missing
            if shadow == "true" and agree in {"true", "false"}:
                stats.shadow_pairs += count
                stats.shadow_agree += count if agree == "true" else 0
        elif arm == "paid":
            if model:
                paid_models.append((latest, model, transport or ""))
            if reason is not None:
                stats.spent_usd += float(cost)
                stats.reasons[reason] = stats.reasons.get(reason, 0) + count
    if accepted_on == 0:
        stats.saved_usd = 0.0
    else:
        proxy = (
            max(paid_models)[1:] if paid_models else _configured_paid_model(paid_source)
        )
        if proxy:
            stats.savings_model = proxy[0]
            rates = _prices(store, *proxy)
            if rates is not None and missing_tokens < accepted_on:
                stats.saved_usd = (
                    saved_in * rates[0] + saved_out * rates[1]
                ) / 1_000_000
    stats.unpriced = accepted_on if stats.saved_usd is None else missing_tokens
    return stats


def render_stats(store: Store, op: str, *, window_days: int = 30) -> str:
    if op not in LOCAL_FIRST_OPS:
        return ""
    stats = collect_stats(store, op, window_days=window_days)
    if not stats.calls:
        return ""
    rate = (
        f"{stats.escalation_rate:.1%}"
        if stats.escalation_rate is not None
        else "unavailable"
    )
    lines = [
        "## local-first",
        f"local attempts: {stats.attempts}; accepted: {stats.accepted}",
        f"escalated: {stats.escalated}/{stats.calls} ({rate}) — all helper calls",
    ]
    if stats.on_calls and stats.on_calls != stats.calls:
        lines.append(
            f"on-mode escalated: {stats.on_escalated}/{stats.on_calls} "
            f"({stats.on_escalated / stats.on_calls:.1%})"
        )
    for reason, count in sorted(stats.reasons.items()):
        share = f" ({count / stats.calls:.1%} of helper calls)" if stats.calls else ""
        lines.append(f"  {reason}: {count}{share}")
    if stats.shadow_calls:
        lines.append(
            f"shadow answer rate: {stats.shadow_answers}/{stats.shadow_calls} "
            f"({stats.shadow_answers / stats.shadow_calls:.1%}) — accepted local answers"
        )
    if stats.shadow_pairs:
        lo, hi = wilson_interval(
            stats.shadow_agree, stats.shadow_pairs, z=1.959963984540054
        )
        lines.append(
            f"shadow agreement: {stats.shadow_agree}/{stats.shadow_pairs} "
            f"({stats.shadow_agree / stats.shadow_pairs:.1%}); Wilson 95%: {lo:.1%}–{hi:.1%}"
        )
    else:
        lines.append("shadow agreement: no comparable pairs")
    lines.append(f"spent_usd: ${stats.spent_usd:.4f} (escalated paid rows)")
    saved = f"${stats.saved_usd:.4f}" if stats.saved_usd is not None else "unavailable"
    model = f" at {stats.savings_model} catalog prices" if stats.savings_model else ""
    lines.append(f"saved_usd estimate: {saved}{model} (accepted on-mode calls only)")
    lines.append("Includes local reasoning tokens, so the estimate overstates savings.")
    if stats.unpriced:
        lines.append(
            f"Accepted on-mode calls missing prices or token telemetry: {stats.unpriced}"
        )
    return "\n".join(lines)


def maybe_post_alert(op: str) -> bool:
    """Warn above 40% escalations in trailing 24h, once per op/UTC day.

    Source/time predicates use the existing index. A DB advisory lock protects
    the durable log-row claim across processes and hosts, independently of the
    alert writer. Missing accounting is a conservative no-op.
    """
    if op not in LOCAL_FIRST_OPS:
        return False
    try:
        if local_first._mode(op) != "on":
            return False
        store = meter.active_store()
        if store is None:
            return False
        now = datetime.now(UTC)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        source = LOCAL_FIRST_OPS[op].paid_source
        with store.pool.connection() as conn:
            locked = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"local-first-alert:{op}",),
            ).fetchone()
            if not locked or not locked[0]:
                return False
            row = conn.execute(
                "SELECT count(DISTINCT COALESCE(features->'local_first'->>'call', id::text)), "
                "count(DISTINCT COALESCE(features->'local_first'->>'call', id::text)) "
                "FILTER (WHERE features->'local_first'->>'arm' = 'paid' "
                "AND features->'local_first'->>'escalated' IS NOT NULL), "
                "bool_or(features->'local_first'->>'alert_day' = %s) "
                "FROM llm_call_log WHERE source = ANY(%s) AND ts >= %s AND ts <= %s "
                "AND features->'local_first'->>'mode' = 'on'",
                (
                    start.date().isoformat(),
                    [source, f"{op}:local"],
                    now - timedelta(days=1),
                    now,
                ),
            ).fetchone()
            if not row or not row[0] or row[1] / row[0] <= 0.4 or row[2]:
                return False
            attempts, escalations = row[:2]
            claimed = conn.execute(
                "UPDATE llm_call_log SET features = jsonb_set(features, "
                "'{local_first,alert_day}', to_jsonb(%s::text)) WHERE id = ("
                "SELECT id FROM llm_call_log WHERE source = %s AND ts >= %s AND ts <= %s "
                "AND features->'local_first'->>'mode' = 'on' "
                "AND features->'local_first'->>'arm' = 'paid' "
                "AND features->'local_first'->>'escalated' IS NOT NULL "
                "ORDER BY ts DESC, id DESC LIMIT 1) RETURNING id",
                (start.date().isoformat(), source, start, now),
            ).fetchone()
            if claimed is None:
                return False
            conn.commit()
        note = f"local-first {op}: {escalations}/{attempts} escalations in 24h; consider switching set off"
        from precis.alert_ids import RULES_BY_ID

        alerts.raise_alert(
            store,
            source=RULES_BY_ID["local-first:escalation/{op}"].rule,
            fingerprint=op,
            title=note,
            detail=note,
        )
        return True
    except Exception:
        log.warning("local-first %s: escalation warning unavailable", op, exc_info=True)
        return False
