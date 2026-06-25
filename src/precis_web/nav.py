"""Global top-bar attention badges, injected into every page.

The nav shows two live counts:

* **Needs you** — open ``ask-user`` todos + the chunkless paper-stub
  backlog. Both are queues where *you* are the blocker: the planner is
  paused on a question, or a paper has no source yet. The ``Needs you``
  tab (:mod:`precis_web.routes.needs_you`) lands on both.
* **Alerts** — open ``kind='alert'`` rows (machine-detected ops /
  health conditions). A different colour from "Needs you" on purpose —
  system-flagged vs you-must-act, mirroring how ``alert`` is kept
  distinct from ``memory`` in the corpus.

Computed on every request via a Starlette context processor, so the
badge stays live whatever page you're on. Each count is defensive: any
failure (no runtime, stateless app, SQL drift) degrades that badge to
zero rather than 500-ing the page — same posture as the env's
``ChainableUndefined``. Two cheap ``COUNT``s per render.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

log = logging.getLogger(__name__)


def _asks_count(store: Any) -> int:
    """Open todos carrying an ``ask-user`` tag.

    Count-only mirror of the ``_load_asks`` WHERE clause in
    :mod:`precis_web.routes.asks` — keep the two filters in sync.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(DISTINCT r.ref_id)
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2
                        JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id
                         AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do')
            """,
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _alerts_count(store: Any) -> int:
    """Open ``kind='alert'`` rows — count-only mirror of alerts._rows."""
    from precis.alerts import STATE_OPEN

    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(DISTINCT r.ref_id)
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
            """,
            (STATE_OPEN,),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def nav_badges(request: Request) -> dict[str, Any]:
    """Context processor: live counts for the top-bar attention badges.

    Returns ``{nav_needs_you, nav_alerts}`` — both default to 0 so the
    template's ``{% if nav_needs_you %}`` simply hides the badge when
    there's nothing waiting (or when the app is running stateless).
    """
    needs_you = 0
    alerts = 0
    try:
        from precis_web.deps import get_store

        store = get_store(request)
    except Exception:
        # No runtime / stateless app (e.g. /healthz before boot) — no badges.
        return {"nav_needs_you": 0, "nav_alerts": 0}

    try:
        needs_you += _asks_count(store)
    except Exception:
        log.debug("nav: asks count failed", exc_info=True)
    try:
        needs_you += store.stub_backlog_count(awaiting=False)
    except Exception:
        log.debug("nav: stub-backlog count failed", exc_info=True)
    try:
        alerts = _alerts_count(store)
    except Exception:
        log.debug("nav: alerts count failed", exc_info=True)

    return {"nav_needs_you": needs_you, "nav_alerts": alerts}
