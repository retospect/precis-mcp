"""``/budget`` — the spend meter + web-editable circuit-breaker caps.

Shows the same rolling tote as the Status page (hourly + 24h spend vs cap,
with by-model / by-source breakdowns) plus a form to set the caps at runtime.
A set cap persists to ``app_settings`` (migration 0067) and overrides the
``PRECIS_BUDGET_*`` env default without a redeploy; "reset" reverts to the env
default. Mirrors the /secrets editor precedent (ADR 0055) in shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis.budget import meter, quota
from precis.budget import settings as budget_settings
from precis_web.deps import get_store, templates
from precis_web.routes.status import _budget_tote

router = APIRouter(prefix="/budget", tags=["budget"])

#: Default span of a "resume paid work now" override — one five-hour window,
#: enough to ride out the binding claude quota window.
_RESUME_HOURS = 5


def _quota_view(store: object) -> dict[str, object]:
    """The claude-OAuth quota lane: the snapshot's windows + the live pause
    decision (if any). Degrades to an empty view when no snapshot exists."""
    try:
        row = store.read_claude_quota()  # type: ignore[attr-defined]
    except Exception:
        row = None
    windows: list[dict[str, object]] = []
    ts = None
    if row is not None:
        ts = row.ts
        raw = row.data.get("windows")
        if isinstance(raw, dict):
            for name, bucket in raw.items():
                if not isinstance(bucket, dict):
                    continue
                windows.append(
                    {
                        "name": name,
                        "status": str(bucket.get("status", "") or "—"),
                        "used": bucket.get("used_percentage"),
                        "resets_at": bucket.get("resets_at"),
                    }
                )
    pause = quota.evaluate(store)  # type: ignore[arg-type]
    return {
        "ts": ts,
        "windows": windows,
        "paused": pause is not None,
        "pause_reason": pause.reason if pause is not None else None,
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the spend tote + quota lane + the cap editor and resume control."""
    store = get_store(request)
    tote = _budget_tote(store)
    hourly_override = budget_settings.get_float(store, budget_settings.HOURLY_KEY)
    daily_override = budget_settings.get_float(store, budget_settings.DAILY_KEY)
    status = meter.current_status(store, use_cache=False)
    resume_until = budget_settings.get_resume_until(store)
    resume_active = budget_settings.resume_active(store)
    return templates.TemplateResponse(
        request,
        "budget/index.html.j2",
        {
            "active_tab": "budget",
            "budget": tote,
            "quota": _quota_view(store),
            "hourly_cap": status.hourly_cap if status else None,
            "daily_cap": status.daily_cap if status else None,
            "hourly_custom": hourly_override is not None,
            "daily_custom": daily_override is not None,
            "resume_until": resume_until,
            "resume_active": resume_active,
        },
    )


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
    return RedirectResponse("/budget", status_code=303)


@router.post("/resume/clear")
async def resume_clear(request: Request) -> Response:
    """Cancel an active resume override (re-arm the breaker immediately)."""
    store = get_store(request)
    budget_settings.clear_setting(store, budget_settings.RESUME_UNTIL_KEY)
    meter.bind_store(store)
    return RedirectResponse("/budget", status_code=303)


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
    return RedirectResponse("/budget", status_code=303)


@router.post("/reset")
async def reset_cap(request: Request, key: str = Form(...)) -> Response:
    """Clear one cap override, reverting to the env / compiled default."""
    store = get_store(request)
    if key in (budget_settings.HOURLY_KEY, budget_settings.DAILY_KEY):
        budget_settings.clear_setting(store, key)
    meter.bind_store(store)
    return RedirectResponse("/budget", status_code=303)
