"""Global top-bar attention badges, injected into every page.

The nav shows three live counts:

* **Needs you** — open ``ask-user`` todos + papers tagged
  ``needs-triage``. Both are queues where *you* must act: the planner is
  paused on a question, or a paper's auto-derived metadata was junk and
  needs a human fix. (The chunkless paper-stub *fetch* backlog is
  deliberately excluded — the fetcher works that automatically; it lives
  under Browse, not here.) The ``Needs you`` tab
  (:mod:`precis_web.routes.needs_you`) lands on both.
* **Gripes** — live ``kind='gripe'`` rows (every ``STATUS:`` value but
  the terminal ``done``/``wontfix`` — the workbench's default "live"
  filter, :mod:`precis_web.routes.gripes`). A distinct colour from both
  other badges — dev-bug-tracker attention, not an operator health
  signal or a planner block.
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
from typing import TYPE_CHECKING, Any

from fastapi import Request

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)


def _asks_count(store: Store) -> int:
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
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
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


def _alerts_count(store: Store) -> int:
    """Open ``kind='alert'`` rows — count-only mirror of alerts._rows."""
    from precis.alerts import STATE_OPEN

    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(DISTINCT r.ref_id)
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
            """,
            (STATE_OPEN,),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# store stays Any: tests pass a hand-rolled fake narrower than Store
def _gripes_count(store: Any) -> int:
    """Live (non-terminal) ``kind='gripe'`` rows.

    Count-only mirror of ``gripes.py::_rows``'s default ``status='live'``
    filter — keep the two in sync (both exclude
    ``gripes.TERMINAL_VALUES``: ``wontfix`` plus the ``STATUS:done``
    drift agents leave on fixed-but-unretired gripes).
    """
    from precis_web.routes.gripes import TERMINAL_VALUES

    terminals = ", ".join(f"'{v}'" for v in TERMINAL_VALUES)
    with store.pool.connection() as conn:
        row = conn.execute(
            f"""
            SELECT count(DISTINCT r.ref_id)
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND t.namespace = 'STATUS' AND t.value NOT IN ({terminals})
            """,
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _nav_user(request: Request) -> Any:
    """The signed-in :class:`precis.users.WebUser`, or ``None``.

    Parked on the request by :class:`precis_web.auth.BasicAuthMiddleware`.
    ``None`` when the gate is off — the top bar then shows a plain
    "Account" link rather than an abbrev chip, because there is no
    identity to abbreviate.
    """
    state = request.scope.get("state") or {}
    return state.get("web_user")


def nav_badges(request: Request) -> dict[str, Any]:
    """Context processor: live counts for the top-bar attention badges,
    plus the signed-in user behind the Account chip.

    Returns ``{nav_needs_you, nav_gripes, nav_alerts, nav_user}`` — the
    counts default to 0 so a template's ``{% if nav_alerts %}`` simply
    hides the badge when there's nothing waiting (or when the app is
    running stateless).
    """
    needs_you = 0
    gripes = 0
    alerts = 0
    user = _nav_user(request)
    try:
        from precis_web.deps import get_store

        store = get_store(request)
    except Exception:
        # No runtime / stateless app (e.g. /healthz before boot) — no badges.
        return {
            "nav_needs_you": 0,
            "nav_gripes": 0,
            "nav_alerts": 0,
            "nav_user": user,
        }

    try:
        needs_you += _asks_count(store)
    except Exception:
        log.debug("nav: asks count failed", exc_info=True)
    try:
        # Papers whose metadata automation gave up — a human must fix
        # them. Same filter the /papers/triage queue paginates over.
        needs_you += store.count_refs(kind="paper", tags=["needs-triage"])
    except Exception:
        log.debug("nav: triage count failed", exc_info=True)
    try:
        # Agent-proposed nanopub hypotheses waiting to be approved or
        # dropped. Prepared and gate-checked, but approve/sign are human
        # doors by design — so this is the third thing that genuinely
        # waits on a person.
        from precis.handlers._finding_hypothesis import PROPOSED_TAG

        needs_you += store.count_refs(kind="finding", tags=[PROPOSED_TAG])
    except Exception:
        log.debug("nav: proposed-hypothesis count failed", exc_info=True)
    try:
        gripes = _gripes_count(store)
    except Exception:
        log.debug("nav: gripes count failed", exc_info=True)
    try:
        alerts = _alerts_count(store)
    except Exception:
        log.debug("nav: alerts count failed", exc_info=True)

    return {
        "nav_needs_you": needs_you,
        "nav_gripes": gripes,
        "nav_alerts": alerts,
        "nav_user": user,
    }
