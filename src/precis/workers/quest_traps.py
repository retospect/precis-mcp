"""The weekly planted-trap quest regression — gr593141 made durable.

``precis.quest.trap_check`` scores the quest loop against a sandbox quest
whose candidate network carries three planted untrusted rows (a
stale-engine measure, a swapped endpoint, an absurd barrier — one clean
control): the frontier must exclude every planted row, the rewritten
dossier must not claim one as a normal result, and the tick must file a
trap-related ask (a ``needs`` entry that lands its marker gripe —
``reruns`` are recorded but cannot land under ``compute=False``) rather
than propose on them. A
hand-run score nobody watches is a score that rots; this makes it
durable, fired from the weekly host-agnostic ``quest_traps`` scheduler
cadence:

1. **Score** — :func:`precis.quest.trap_check.run_trap_check` runs
   :data:`~precis.quest.trap_check.DEFAULT_RUNS` ticks on the sandbox
   quest with the real router (the arm the gripe wants measured — the
   tick's actual model, not a script).
2. **Record** — the run's score (per-run pass/fail, gate leg, ask leg)
   is written to ``app_settings['quest_traps:last']`` — the same
   ``app_settings`` health row health_digest's checks snapshot uses —
   with a bounded ``history`` of prior scores.
3. **Alert** — a gate flip (a planted row leaking into the confirmed
   frontier is a trust-path regression, code-side and binary) or a drop
   in the passed-run count versus the last recorded run raises one WARN
   ``kind='alert'``. A run with neither regression resolves it. The first
   run only records a baseline.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert, resolve_alert_by_fingerprint
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: ``app_settings`` row holding the last recorded score — read for the
#: drop comparison before it is overwritten, same ``key: value`` health
#: row shape as ``health_digest:checks_snapshot``.
LAST_SCORE_KEY = "quest_traps:last"
ALERT_SOURCE = RULES_BY_ID["quest_traps/score-dropped"].rule
ALERT_FINGERPRINT = "score-dropped"
#: Prior-score snapshots kept in the ``history`` list — at a weekly
#: cadence 12 entries is ~3 months of trend.
_HISTORY_KEEP = 12
#: Fields carried from a score doc into its ``history`` entry.
_HISTORY_KEYS = (
    "computed_at",
    "n_runs",
    "gate_ok",
    "report_ok",
    "ask_hits",
    "trap_proposals",
    "passed",
    "score",
)


def _last_score(store: Store) -> dict[str, Any] | None:
    """The last recorded score doc, or ``None`` when absent/unreadable."""
    from precis.budget import settings as app_settings

    raw = app_settings.get_setting(store, LAST_SCORE_KEY)
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _score_drops(prev: dict[str, Any], score: dict[str, Any]) -> list[str]:
    """Regression lines versus the last recorded run.

    Two legs: the gate is binary and code-side — a flip from clean to
    leaking always names the regression; the ask leg is model-stochastic,
    so only a strictly lower passed count (against the same-or-larger run
    count) counts as a drop — single-run noise stays a score, not an
    alert."""
    drops: list[str] = []
    if prev.get("gate_ok") is True and not score.get("gate_ok"):
        detail = "; ".join(
            d for r in score.get("runs") or [] for d in r.get("gate_detail") or []
        )
        drops.append(f"frontier gate leaked ({detail or 'unspecified'})")
    old_passed = prev.get("passed")
    old_total = prev.get("n_runs")
    if (
        isinstance(old_passed, int)
        and isinstance(old_total, int)
        and old_total > 0
        and score["n_runs"] >= old_total
        and score["passed"] < old_passed
    ):
        drops.append(
            f"passed {score['passed']}/{score['n_runs']} < {old_passed}/{old_total}"
        )
    return drops


def run_quest_traps_pass(
    store: Store,
    *,
    runs: int | None = None,
    tier: Any = None,
    dispatch_fn: Any = None,
    now: datetime | None = None,
) -> BatchResult:
    """Score the planted-trap fixture, record it, and alert on a drop.

    ``runs`` (default ``trap_check.DEFAULT_RUNS``) ticks against the
    sandbox quest; ``dispatch_fn`` defaults to the real router — tests
    inject a scripted one. ``claimed`` = ticks run, ``ok`` = runs that
    passed both legs, ``failed`` = the rest — so a regression is visible
    in ``worker_logs`` without opening the app_settings row.
    """
    from precis.quest import trap_check

    now = now or datetime.now(UTC)
    score = trap_check.run_trap_check(
        store,
        runs=runs if runs is not None else trap_check.DEFAULT_RUNS,
        tier=tier,
        dispatch_fn=dispatch_fn,
        by="agent",
        now=now,
    )

    prev = _last_score(store)
    drops = _score_drops(prev, score) if prev is not None else []

    prior_history = prev.get("history") if prev is not None else None
    history = list(prior_history) if isinstance(prior_history, list) else []
    if prev is not None:
        history.append({key: prev[key] for key in _HISTORY_KEYS if key in prev})
    doc = {**score, "history": history[-_HISTORY_KEEP:]}

    from precis.budget import settings as app_settings

    app_settings.set_setting(store, LAST_SCORE_KEY, json.dumps(doc))

    if drops:
        raise_alert(
            store,
            source=ALERT_SOURCE,
            fingerprint=ALERT_FINGERPRINT,
            title=("[quest_traps] planted-trap regression: " + "; ".join(drops)),
            detail=(
                f"The sandbox quest fixture scored {score['passed']}/"
                f"{score['n_runs']} runs (gate {'ok' if score['gate_ok'] else 'LEAKED'}, "
                f"ask filed on {score['ask_hits']}/{score['n_runs']}, "
                f"{score['trap_proposals']} trap-proposals), regressions "
                f"versus the last recorded run: {'; '.join(drops)}.\n"
                "Scores live in app_settings['quest_traps:last'] (prior "
                "scores in 'history'); qu"
                f"{score['quest_id']} is the sandbox quest — its logbook "
                "and provisional frontier carry each tick's reasoning. Fix "
                "the regressed gate/ask path, then resolve this alert."
            ),
            severity="warn",
        )
    else:
        resolve_alert_by_fingerprint(
            store,
            source=ALERT_SOURCE,
            fingerprint=ALERT_FINGERPRINT,
            resolved_by="quest_traps",
        )

    return BatchResult(
        handler="quest_traps",
        claimed=score["n_runs"],
        ok=score["passed"],
        failed=score["n_runs"] - score["passed"],
    )


def quest_traps_summary(store: Store) -> str:
    """One best-effort morning-brief line; absent/legacy runs stay silent."""
    try:
        doc = _last_score(store)
        if doc is None or doc.get("n_runs") is None:
            return ""
        gate = "gate ok" if doc.get("gate_ok") else "GATE LEAKED"
        return (
            f"Quest traps: {gate}, {doc['passed']}/{doc['n_runs']} runs "
            f"asked-clean ({doc['computed_at']})."
        )
    except Exception:
        log.debug("quest traps summary unavailable", exc_info=True)
        return ""


__all__ = [
    "ALERT_FINGERPRINT",
    "ALERT_SOURCE",
    "LAST_SCORE_KEY",
    "quest_traps_summary",
    "run_quest_traps_pass",
]
