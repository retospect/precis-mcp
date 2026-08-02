"""``/budget`` — the spend meter + web-editable circuit-breaker caps.

WS3 (docs/proposals/web-ui-rationalization.md) folded the page formerly
served at ``GET /budget`` into the "Budget" sub-tab of the merged System
page (``/status?tab=budget``); ``GET /budget`` now just redirects there.
The rendering (tote, quota lane, cap-editor state — see ``status.py``'s
``_budget_ctx`` / ``_quota_view``, and ``_budget_tote`` which already lived
in ``status.py``) moved with it, avoiding a circular import (``status.py``
can't import from here without one, since this module used to import
``_budget_tote`` from ``status.py``). Only the
``POST /budget/{set,reset,resume,resume/clear}`` write endpoints stay in
this module, at their original paths; their redirect target is now the
Budget sub-tab. A set cap persists to
``app_settings`` (migration 0067) and overrides the ``PRECIS_BUDGET_*`` env
default without a redeploy; "reset" reverts to the env default. Mirrors the
/secrets editor precedent (ADR 0055) in shape.

``POST /budget/dream-interval/{set,reset}`` is a near-copy of the same
pattern for the dream pass's cadence knob (Wave-0 §G,
:mod:`precis.workers.dream_throttle`): a web-set ``app_settings`` row
overrides ``PRECIS_DREAM_MIN_INTERVAL_MINUTES``, which overrides the
compiled 15-min default (the launchd plist's own cadence — unchanged).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from precis.budget import meter
from precis.budget import settings as budget_settings
from precis.workers import dream_throttle
from precis_web.deps import get_store

router = APIRouter(prefix="/budget", tags=["budget"])

#: Default span of a "resume paid work now" override — one five-hour window,
#: enough to ride out the binding claude quota window.
_RESUME_HOURS = 5


@router.get("", response_model=None)
@router.get("/", response_model=None)
async def index() -> RedirectResponse:
    """``/budget`` is retired (WS3) — redirect to the Budget sub-tab."""
    return RedirectResponse(url="/status?tab=budget", status_code=307)


@router.post("/resume")
async def resume_now(request: Request, hours: str = Form("")) -> Response:
    """Set a "resume paid work now" override — bypass a soft trip (dollar cap or
    quota ceiling) for a span. A hard Anthropic rejection still fails at the
    provider; this only lifts our own pre-emptive pause."""
    store = get_store(request)
    try:
        span = float(hours) if hours.strip() else float(_RESUME_HOURS)
    except ValueError:
        span = float(_RESUME_HOURS)
    span = max(0.25, min(span, 168.0))
    until = datetime.now(UTC) + timedelta(hours=span)
    budget_settings.set_setting(
        store, budget_settings.RESUME_UNTIL_KEY, until.isoformat()
    )
    meter.bind_store(store)
    return RedirectResponse("/status?tab=budget", status_code=303)


@router.post("/resume/clear")
async def resume_clear(request: Request) -> Response:
    """Cancel an active resume override (re-arm the breaker immediately)."""
    store = get_store(request)
    budget_settings.clear_setting(store, budget_settings.RESUME_UNTIL_KEY)
    meter.bind_store(store)
    return RedirectResponse("/status?tab=budget", status_code=303)


@router.post("/set")
async def set_caps(
    request: Request,
    hourly_usd: str = Form(""),
    daily_usd: str = Form(""),
) -> Response:
    """Set/replace either cap. A blank or non-positive field is a no-op."""
    store = get_store(request)
    for raw, key in (
        (hourly_usd, budget_settings.HOURLY_KEY),
        (daily_usd, budget_settings.DAILY_KEY),
    ):
        raw = raw.strip()
        if not raw:
            continue
        try:
            budget_settings.set_float(store, key, float(raw))
        except ValueError:
            continue
    meter.bind_store(store)  # drop the cached status so the new cap is live
    return RedirectResponse("/status?tab=budget", status_code=303)


@router.post("/reset")
async def reset_cap(request: Request, key: str = Form(...)) -> Response:
    """Clear one cap override, reverting to the env / compiled default."""
    store = get_store(request)
    if key in (budget_settings.HOURLY_KEY, budget_settings.DAILY_KEY):
        budget_settings.clear_setting(store, key)
    meter.bind_store(store)
    return RedirectResponse("/status?tab=budget", status_code=303)


@router.post("/dream-interval/set")
async def set_dream_interval(
    request: Request, min_interval_minutes: str = Form("")
) -> Response:
    """Set the dream pass's cadence knob (``dream.min_interval_minutes``) —
    a blank or non-positive value is a no-op. Launchd keeps firing every 15
    min unchanged; this only changes whether a given fire does real work
    (:mod:`precis.workers.dream_throttle`). Live — takes effect on the next
    pass fire, no redeploy."""
    store = get_store(request)
    raw = min_interval_minutes.strip()
    if raw:
        try:
            budget_settings.set_float(
                store, dream_throttle.MIN_INTERVAL_KEY, float(raw)
            )
        except ValueError:
            pass
    return RedirectResponse("/status?tab=budget", status_code=303)


@router.post("/dream-interval/reset")
async def reset_dream_interval(request: Request) -> Response:
    """Clear the dream cadence override, reverting to the env
    (``PRECIS_DREAM_MIN_INTERVAL_MINUTES``) / compiled (15-min) default."""
    store = get_store(request)
    budget_settings.clear_setting(store, dream_throttle.MIN_INTERVAL_KEY)
    return RedirectResponse("/status?tab=budget", status_code=303)
