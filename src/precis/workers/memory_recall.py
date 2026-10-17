"""The weekly memory-recall check — the durable form of the P4 scorer.

``docs/backlog/memory-recall-quality.md`` measures "never found", the
failure mode once repo-dev memory is discovered by recall rather than
preloaded. Until this pass the only check was a hand-run
``precis memory index --set`` — a score nobody watches drifts silently.
Fired from the weekly host-agnostic ``memory_recall`` scheduler cadence:

1. **Score** — :func:`precis.cli.memory.recall_report_rows` over the
   packaged P4 question set (``precis.data``
   :data:`~precis.cli.memory.RECALL_SET_RESOURCE`; the same set and scorer
   ``--set`` runs, added in 32f1dc70c) at k=5, with the deployment's real
   embedder — lexical-only would mislead, so a host that cannot build one
   is ruled out by the cadence's ``eligible`` gate and a fire that still
   loses its embedder raises instead of recording a junk score.
2. **Record** — the run's k=5 score (total + native/mirror split, per
   :func:`~precis.cli.memory._mirrored_handles`) is written to
   ``app_settings['memory_recall:last']`` — the same ``app_settings``
   health row health_digest's checks snapshot uses (no new table) — with
   a bounded ``history`` of prior scores.
3. **Alert** — a score below the last recorded one (total or either
   origin split) raises one WARN ``kind='alert'``; an equal-or-higher
   run resolves it. The first run only records a baseline.
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
LAST_SCORE_KEY = "memory_recall:last"
ALERT_SOURCE = RULES_BY_ID["memory_recall/recall-dropped"].rule
ALERT_FINGERPRINT = "recall-dropped"
RECALL_K = 5
#: Prior-score snapshots kept in the ``history`` list — at a weekly
#: cadence 12 entries is ~3 months of trend.
_HISTORY_KEEP = 12
#: Fields carried from a score doc into its ``history`` entry.
_HISTORY_KEYS = (
    "computed_at",
    "k",
    "hits",
    "total",
    "native_hits",
    "native_total",
    "mirror_hits",
    "mirror_total",
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


def run_memory_recall_pass(
    store: Store,
    *,
    entries: list[dict[str, Any]] | None = None,
    embedder: Any = None,
    k: int = RECALL_K,
    now: datetime | None = None,
) -> BatchResult:
    """Score the recall set, record it, and alert on a drop.

    ``entries``/``embedder`` default to the packaged P4 set and the
    deployment's configured embedder (``make_embedder_from_config``;
    tests inject their own). ``claimed`` = questions scored, ``ok`` =
    hits at k, ``failed`` = misses — so a regression is visible in
    ``worker_logs`` without opening the app_settings row.
    """
    from precis.cli.memory import (
        load_recall_set,
        recall_report_rows,
    )

    now = now or datetime.now(UTC)
    entries = load_recall_set() if entries is None else entries
    if embedder is None:
        from precis.config import load_config
        from precis.embedder import make_embedder_from_config

        embedder = make_embedder_from_config(load_config(), dim=store.embedding_dim())

    rows = recall_report_rows(store, entries, k=k, embedder=embedder)
    tally = {"native": [0, 0], "mirror": [0, 0]}
    misses: list[str] = []
    for entry, rank, origin in rows:
        tally[origin][1] += 1
        tally[origin][0] += rank is not None
        if rank is None:
            misses.append(str(entry["want"]))
    score: dict[str, Any] = {
        "k": k,
        "hits": tally["native"][0] + tally["mirror"][0],
        "total": len(rows),
        "native_hits": tally["native"][0],
        "native_total": tally["native"][1],
        "mirror_hits": tally["mirror"][0],
        "mirror_total": tally["mirror"][1],
        "misses": misses,
    }

    prev = _last_score(store)
    drops: list[str] = []
    if prev is not None:
        for label, cur, old in (
            ("total", score["hits"], prev.get("hits")),
            ("native", score["native_hits"], prev.get("native_hits")),
            ("mirror", score["mirror_hits"], prev.get("mirror_hits")),
        ):
            if isinstance(old, int) and cur < old:
                drops.append(f"{label} {cur} < {old}")

    prior_history = prev.get("history") if prev is not None else None
    history = list(prior_history) if isinstance(prior_history, list) else []
    if prev is not None:
        history.append({key: prev[key] for key in _HISTORY_KEYS if key in prev})
    doc = {"computed_at": now.isoformat(), **score, "history": history[-_HISTORY_KEEP:]}

    from precis.budget import settings as app_settings

    app_settings.set_setting(store, LAST_SCORE_KEY, json.dumps(doc))

    if drops:
        raise_alert(
            store,
            source=ALERT_SOURCE,
            fingerprint=ALERT_FINGERPRINT,
            title=(f"[memory_recall] recall dropped at k={k}: " + "; ".join(drops)),
            detail=(
                f"P4 set scored {score['hits']}/{score['total']} at k={k} "
                f"(native {score['native_hits']}/{score['native_total']}, "
                f"mirror {score['mirror_hits']}/{score['mirror_total']}), "
                f"below the last recorded run; misses: "
                f"{', '.join(misses) or 'none'}.\n"
                "Scores live in app_settings['memory_recall:last'] "
                "(prior scores in 'history'); `precis memory index --set` "
                "prints the per-query HIT/MISS table. Fix the regressed "
                "titles/hooks or the index path, then resolve this alert."
            ),
            severity="warn",
        )
    else:
        resolve_alert_by_fingerprint(
            store,
            source=ALERT_SOURCE,
            fingerprint=ALERT_FINGERPRINT,
            resolved_by="memory_recall",
        )

    return BatchResult(
        handler="memory_recall",
        claimed=len(rows),
        ok=score["hits"],
        failed=score["total"] - score["hits"],
    )


__all__ = [
    "ALERT_FINGERPRINT",
    "ALERT_SOURCE",
    "LAST_SCORE_KEY",
    "RECALL_K",
    "run_memory_recall_pass",
]
