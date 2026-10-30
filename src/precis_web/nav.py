"""Global top-bar attention badges, injected into every page.

The nav shows three live counts:

* **Needs you** — showstopper and ``ask-user`` decision rows from
  :func:`precis.todo.asks_view.asks_model` (a lane is stopped / the next
  slice waits on a question) + papers tagged ``needs-triage``. Both are
  queues where *you* must act: the planner is paused on a question, or a
  paper's auto-derived metadata was junk and needs a human fix.
  Discussions and a bare ``waiting-for:reto`` are on the ``/asks`` page
  but not the badge. (The chunkless paper-stub *fetch* backlog is
  deliberately excluded — the fetcher works that automatically; it lives
  under Browse, not here.) The ``Needs you`` tab
  (:mod:`precis_web.routes.needs_you`) lands on both.
* **Gripes** — ``kind='gripe'`` rows tagged ``STATUS:triaged``, the pick
  list: the workbench's default filter
  (:mod:`precis_web.routes.gripes`), so the badge reads what the page
  opens on. A distinct colour from both other badges — dev-bug-tracker
  attention, not an operator health signal or a planner block.
  A permanent large count stops meaning anything.
* **Alerts** — open ``kind='alert'`` rows (machine-detected ops /
  health conditions). A different colour from "Needs you" on purpose —
  system-flagged vs you-must-act, mirroring how ``alert`` is kept
  distinct from ``memory`` in the corpus.

Computed on every request via a Starlette context processor, so the
badge stays live whatever page you're on. Each count is defensive: any
failure (no runtime, stateless app, SQL drift) degrades that badge to
zero rather than 500-ing the page — same posture as the env's
``ChainableUndefined``. Two cheap ``COUNT``s per render.

The same context processor also carries the header's "?" tour-launch
button: ``tour_slug`` (``routes/manual.py::tour_slug_for_path``, matched
against the request path against the already-cached tour manifests — no
extra client fetch) and ``tour_href`` (the current URL with ``tour``/
``step`` replaced by ``tour=<slug>``, so the template just renders an
anchor when ``tour_slug`` is set).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import Request

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)


def _asks_count(store: Store) -> int:
    """Showstoppers plus ask-user decisions from the asks model.

    The ``/asks`` page's default view (td541979) shows the same rows:
    every ``showstopper``-class row, and ``decision`` rows that are
    ``ask-user`` todos. Discussions and the rest of the decision class
    (a bare ``waiting-for:reto``, ``needs:ruling`` gripes) stay on the
    page behind its filter chips but off the badge. Classification is
    :func:`precis.todo.asks_view.asks_model`'s — not reimplemented here.
    """
    from precis.todo.asks_view import DECISION, SHOWSTOPPER, asks_model

    return sum(
        1
        for r in asks_model(store).rows
        if r.klass == SHOWSTOPPER or (r.klass == DECISION and "ask-user" in r.reasons)
    )


def _alerts_count(store: Store) -> int:
    """Open ``kind='alert'`` rows — count-only mirror of alerts._rows."""
    from precis.alerts import ACKED_SQL, STATE_OPEN

    with store.pool.connection() as conn:
        row = conn.execute(
            f"""
            SELECT count(DISTINCT r.ref_id)
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
               AND NOT {ACKED_SQL}
            """,
            (STATE_OPEN,),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# store stays Any: tests pass a hand-rolled fake narrower than Store
def _gripes_count(store: Any) -> int:
    """Un-retired ``kind='gripe'`` rows tagged ``STATUS:triaged`` — the pick list.

    Count-only mirror of ``gripes.py::_rows`` at the page's default
    filter (``gripes.DEFAULT_STATUS``, ``triaged``) — keep the two in
    sync. The full live list is one click away on ``/gripes`` and is
    deliberately not what the badge counts.
    """
    from precis_web.routes.gripes import DEFAULT_STATUS

    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(*)
              FROM refs r
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt
                     JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                      AND t.value = %s)
            """,
            (DEFAULT_STATUS,),
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


def _tour_href(request: Request, slug: str) -> str:
    """Current URL with ``tour``/``step`` replaced by ``tour=<slug>``.

    Every other query param is kept — the "?" button works the same on a
    filtered/paginated page as a bare one — and any stale ``tour``/``step``
    from the current URL is dropped rather than carried forward, so the
    link always launches a fresh run of *this* page's tour at step 1.
    """
    kept = [
        (k, v)
        for k, v in request.query_params.multi_items()
        if k not in ("tour", "step")
    ]
    kept.append(("tour", slug))
    return f"{request.url.path}?{urlencode(kept)}"


def nav_badges(request: Request) -> dict[str, Any]:
    """Context processor: live counts for the top-bar attention badges,
    the signed-in user behind the Account chip, and the header's "?"
    tour-launch button.

    Returns ``{nav_needs_you, nav_gripes, nav_alerts, nav_user, tour_slug,
    tour_href}`` — the counts default to 0 and ``tour_slug``/``tour_href``
    default to ``None`` so a template's ``{% if ... %}`` simply hides the
    badge/button when there's nothing to show (or when the app is running
    stateless).
    """
    needs_you = 0
    gripes = 0
    alerts = 0
    user = _nav_user(request)
    # Server-side page match against the cached tour manifests — no store,
    # no extra client fetch. Computed before the store lookup below so it
    # still lands on a stateless-app response.
    from precis_web.routes.manual import tour_slug_for_path

    tour_slug = tour_slug_for_path(request.url.path)
    tour_href = _tour_href(request, tour_slug) if tour_slug else None
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
            "tour_slug": tour_slug,
            "tour_href": tour_href,
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
        "tour_slug": tour_slug,
        "tour_href": tour_href,
    }
