"""Model-free periodic usage signals for the shared LLM pool ledger.

Dark until service_config lights pool_refresh. Fleet reports supply Codex's
weekly percentage; Devin's existing pause supplies its retry horizon. Missing
signals leave pools alone, so idle fleet reports never manufacture fresh zero
usage. Kimi, OpenRouter credits and Devin ACUs have no refresh leg here.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

from precis.corpus_layout import host_name
from precis.store import Store
from precis.utils.llm.accounts import record_pool_failure, record_pool_report
from precis.workers.runner import BatchResult
from precis.workers.service_config import ServiceConfigResolver

log = logging.getLogger(__name__)
SERVICE_NAME = "pool_refresh"


def _timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def _codex_report(store: Store) -> tuple[datetime, float] | None:
    """Newest host report containing a weekly slot, regardless of slot name."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE kind = 'fleet' "
            "AND meta->>'type' = 'host' AND retired_at IS NULL"
        ).fetchall()
    newest: tuple[datetime, float] | None = None
    for (meta,) in rows:
        at = _timestamp(meta.get("reported_at"))
        quota = meta.get("quota")
        codex = quota.get("codex") if isinstance(quota, dict) else None
        if at is None or not isinstance(codex, dict):
            continue
        for slot in codex.values():
            if not isinstance(slot, dict):
                continue
            minutes, used = slot.get("window_minutes"), slot.get("used_percent")
            if not isinstance(minutes, (int, float)) or minutes < 10000:
                continue
            if not isinstance(used, (int, float)) or not math.isfinite(used):
                continue
            if newest is None or at > newest[0]:
                newest = (at, float(used))
    return newest


def run_pool_refresh_pass(store: Store) -> BatchResult:
    """Copy current Codex and Devin signals through the one pool writer."""
    if not ServiceConfigResolver(store, host_name()).enabled(
        SERVICE_NAME, default_on=False
    ):
        return BatchResult(handler=SERVICE_NAME, claimed=0, ok=0, failed=0)
    claimed = ok = failed = 0
    try:
        report = _codex_report(store)
        if report is not None:
            claimed += 1
            at, used = report
            ok += record_pool_report(
                store,
                "llm:codex:weekly",
                used=used,
                remaining=100 - used,
                limit=100,
                source="provider",
                reported_at=at,
            )
    except Exception:
        failed += 1
        log.warning("pool_refresh: Codex fleet report failed", exc_info=True)
    try:
        until = _timestamp(store.get_setting("devin_exec:paused_until"))
        # devin_api never clears the setting, so a lapsed pause stays stored:
        # copying it would pin a past retry_at on the pool and report a write
        # every cycle. Only a pause still in force is a signal.
        if until is not None and until > datetime.now(UTC):
            claimed += 1
            ok += record_pool_failure(
                store,
                "llm:devin:sessions",
                reason_class="paused",
                retry_at=until,
            )
    except Exception:
        failed += 1
        log.warning("pool_refresh: Devin pause copy failed", exc_info=True)
    return BatchResult(handler=SERVICE_NAME, claimed=claimed, ok=ok, failed=failed)
