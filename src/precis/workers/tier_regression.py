"""The weekly tier-ladder regression — gr595546 made durable.

``precis llm eval`` and ``src/precis/llm_eval/`` run the golden set on
demand; nothing ran them on a cadence and nothing alerted. Fired from the
weekly host-agnostic ``llm_tier_regression`` scheduler cadence:

1. **Run** — :func:`precis.llm_eval.regression.run_tier_regression` runs
   the packaged fixed set (~30 tasks with known-good outcomes) through
   every rung of every tier's resolved chain — each local and cloud
   offering the router could pick, pinned one rung at a time so a weak
   offering can't hide behind failover.
2. **Record** — the per-offering pass rates are written to
   ``app_settings['llm_tier_regression:last']`` — the same
   ``app_settings`` health row health_digest's checks snapshot uses —
   with a bounded ``history`` of prior runs.
3. **Alert** — the ``llm-tier-regression`` condition row
   (:mod:`precis.workers.conditions`) fires a per-offering finding when a
   pass rate drops past ``PRECIS_COND_TIER_REGRESSION_DROP`` against the
   last recorded run, or when an offering that scored last week comes
   back void (nothing scored) — the same alert router as
   local-serving-pressure (per-instance auto-open/auto-close). The
   recorded rate is passed over *attempted* tasks: an errored task counts
   against it (td663780), so a partial outage reads as a drop rather than
   re-normalising to 100% of a shrunken denominator. The pass itself
   raises nothing: a regression is a condition finding, not a
   ``raise_alert`` site, so a recovered offering self-resolves and a
   consistently-dark lane stays quiet.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: ``app_settings`` row holding the last recorded run — read for the
#: history append before it is overwritten, same ``key: value`` health
#: row shape as ``health_digest:checks_snapshot``.
LAST_SCORE_KEY = "llm_tier_regression:last"
#: Prior-run snapshots kept in the ``history`` list — at a weekly
#: cadence 12 entries is ~3 months of trend; ``history[-1]`` is the run
#: the condition probes against.
_HISTORY_KEEP = 12
#: Fields carried from a run doc into its ``history`` entry.
_HISTORY_KEYS = ("computed_at", "n_tasks", "pass_min", "offerings")


def _last_score(store: Store) -> dict[str, Any] | None:
    """The last recorded run doc, or ``None`` when absent/unreadable."""
    from precis.budget import settings as app_settings

    raw = app_settings.get_setting(store, LAST_SCORE_KEY)
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def run_tier_regression_pass(
    store: Store,
    *,
    tasks: list | None = None,
    dispatch_for: Any = None,
    now: datetime | None = None,
) -> BatchResult:
    """Score every ladder offering, record the run, return the tally.

    ``tasks``/``dispatch_for`` pass through to
    :func:`precis.llm_eval.regression.run_tier_regression` (tests inject a
    scripted ladder + dispatch). ``claimed`` = task runs across all
    offerings, ``ok`` = tasks passed, ``failed`` = errored + failed — so
    a regression is visible in ``worker_logs`` without opening the
    app_settings row.
    """
    from precis.llm_eval import regression

    now = now or datetime.now(UTC)
    doc = regression.run_tier_regression(
        store, tasks=tasks, dispatch_for=dispatch_for, now=now
    )

    prev = _last_score(store)
    prior_history = prev.get("history") if prev is not None else None
    history = list(prior_history) if isinstance(prior_history, list) else []
    if prev is not None:
        history.append({key: prev[key] for key in _HISTORY_KEYS if key in prev})
    doc = {**doc, "history": history[-_HISTORY_KEEP:]}

    from precis.budget import settings as app_settings

    app_settings.set_setting(store, LAST_SCORE_KEY, json.dumps(doc))

    claimed = ok = failed = 0
    for entry in doc["offerings"].values():
        claimed += entry["scored"] + entry["errors"]
        ok += entry["passed"]
        failed += entry["scored"] + entry["errors"] - entry["passed"]
    return BatchResult(
        handler="llm_tier_regression",
        claimed=claimed,
        ok=ok,
        failed=failed,
        extra={"offerings": len(doc["offerings"])},
    )


__all__ = [
    "LAST_SCORE_KEY",
    "run_tier_regression_pass",
]
