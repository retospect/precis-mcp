"""The weekly memory-recall check — the durable form of the P4 scorer.

Measures "never found", the failure mode once repo-dev memory is
discovered by recall rather than preloaded (context-memory-hierarchy P4,
landed 2026-10-18). A hand-run ``precis memory index --set`` is a score
nobody watches; this makes it durable. Fired from the weekly host-agnostic ``memory_recall`` scheduler cadence:

1. **Score** — :func:`precis.cli.memory_recall.measure_recall` over the
   packaged P4 question set (``precis.data``
   :data:`~precis.cli.memory.RECALL_SET_RESOURCE`; the same set and scorer
   ``--set`` runs, added in 32f1dc70c) at k=3/5/8/12, with the deployment's real
   embedder — lexical-only would mislead, so a host that cannot build one
   is ruled out by the cadence's ``eligible`` gate and a fire that still
   loses its embedder raises instead of recording a junk score.
2. **Record** — the run's k=5 score (total + native/mirror split, per
   :func:`~precis.cli.memory._mirrored_handles`) is written to
   ``app_settings['memory_recall:last']`` — the same ``app_settings``
   health row health_digest's checks snapshot uses (no new table) — with
   a bounded ``history`` of prior scores. Additive ``queries``/``curve``
   fields retain ranks, recall and rendered chars/tokens (``chars/4``)
   through agent runtime dispatch, including both fisheye get variants.
3. **Alert** — a score below the last recorded one (total or either
   origin split), or median index-plus-fisheye tokens at k=5 up more than
   25%, raises one WARN ``kind='alert'``. A run with neither regression
   resolves it. The first run only records a baseline.

Question set rules (``src/precis/data/memory_recall_set.json``, one entry
per real case: ``query``, prod answer ``want`` handle, title + hook
snapshot, ``source``, ``why``; no body text, hostnames or addresses, the
repo is public):

- Entries were written by an agent that read the answer nodes, so query
  wording leaks node vocabulary and the score is an upper bound. Grow the
  set from observed misses: when a session finds a memory only after the
  mistake, add its first recall query and the node.
- A prod title or hook edit updates that entry's snapshot in the same
  commit. Fixes are meta-only edits (title and/or ``meta.hook``, no body
  rewrite); an edited header card has no embedding until the embed worker
  fills it, so measure after it has a vector.
- ``tests/test_memory_recall_native_vs_mirror.py::test_recall_set_holds_its_floor``
  scores the set with the hash embedder (lexical in effect, guards the
  scorer and seeding path, not wording); ``SET_FLOOR`` is raised when a
  fix lands, never lowered. Prod is the only signal for title/hook fixes.
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
    "token_estimator",
    "rank_k",
    "queries",
    "curve",
    "median_tokens_k5",
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
    from precis.cli.memory import load_recall_set
    from precis.cli.memory_recall import measure_recall

    now = now or datetime.now(UTC)
    entries = load_recall_set() if entries is None else entries
    if embedder is None:
        from precis.config import load_config
        from precis.embedder import make_embedder_from_config

        embedder = make_embedder_from_config(load_config(), dim=store.embedding_dim())

    measurements = measure_recall(store, entries, k=k, embedder=embedder)
    rows = measurements["queries"]
    tally = {"native": [0, 0], "mirror": [0, 0]}
    misses: list[str] = []
    for entry in rows:
        origin = entry["origin"]
        hit = entry["recall_at_k"][str(k)]
        tally[origin][1] += 1
        tally[origin][0] += hit
        if not hit:
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
        **measurements,
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
        old_tokens = prev.get("median_tokens_k5")
        tokens = score["median_tokens_k5"]
        if (
            prev.get("token_estimator") == score["token_estimator"]
            and isinstance(old_tokens, (int, float))
            and old_tokens > 0
            and tokens is not None
            and tokens > old_tokens * 1.25
        ):
            drops.append(
                f"k=5 median tokens {tokens:.1f} > {old_tokens:.1f} "
                f"(+{(tokens / old_tokens - 1) * 100:.1f}%)"
            )

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
            title=(
                f"[memory_recall] recall/cost regression at k={k}: " + "; ".join(drops)
            ),
            detail=(
                f"P4 set scored {score['hits']}/{score['total']} at k={k} "
                f"(native {score['native_hits']}/{score['native_total']}, "
                f"mirror {score['mirror_hits']}/{score['mirror_total']}), "
                f"regressions versus the last recorded run: {'; '.join(drops)}; misses: "
                f"{', '.join(misses) or 'none'}.\n"
                "Scores live in app_settings['memory_recall:last'] "
                "(prior scores in 'history'); `precis memory index --set` "
                "prints the per-query HIT/MISS table; add --curve for recall "
                "versus median tokens (index + fisheye+1hop, chars/4). Fix "
                "the regressed titles/hooks, index path or render cost, "
                "then resolve this alert."
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


def memory_recall_summary(store: Store) -> str:
    """One best-effort morning-brief line; absent/legacy runs stay silent."""
    try:
        doc = _last_score(store)
        if doc is None or doc.get("median_tokens_k5") is None:
            return ""
        row = next(row for row in doc["curve"] if row["k"] == 5)
        return (
            f"Memory recall: {row['hits']}/{row['total']} at k=5; "
            f"median {row['median_tokens']:.0f} tokens "
            f"(index + fisheye+1hop, {doc['token_estimator']}; "
            f"{doc['computed_at']})."
        )
    except Exception:
        log.debug("memory recall summary unavailable", exc_info=True)
        return ""


__all__ = [
    "ALERT_FINGERPRINT",
    "ALERT_SOURCE",
    "LAST_SCORE_KEY",
    "RECALL_K",
    "memory_recall_summary",
    "run_memory_recall_pass",
]
