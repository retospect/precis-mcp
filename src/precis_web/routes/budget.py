"""``/budget`` — the spend meter + web-editable circuit-breaker caps.

Shows the same rolling tote as the Status page (hourly + 24h spend vs cap,
with by-model / by-source breakdowns) plus a form to set the caps at runtime.
A set cap persists to ``app_settings`` (migration 0067) and overrides the
``PRECIS_BUDGET_*`` env default without a redeploy; "reset" reverts to the env
default. Mirrors the /secrets editor precedent (ADR 0055) in shape.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis.budget import meter
from precis.budget import settings as budget_settings
from precis_web.deps import get_store, templates
from precis_web.routes.status import _budget_tote

router = APIRouter(prefix="/budget", tags=["budget"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the spend tote + the cap editor, prefilled with effective caps."""
    store = get_store(request)
    tote = _budget_tote(store)
    hourly_override = budget_settings.get_float(store, budget_settings.HOURLY_KEY)
    daily_override = budget_settings.get_float(store, budget_settings.DAILY_KEY)
    status = meter.current_status(store, use_cache=False)
    return templates.TemplateResponse(
        request,
        "budget/index.html.j2",
        {
            "active_tab": "budget",
            "budget": tote,
            "hourly_cap": status.hourly_cap if status else None,
            "daily_cap": status.daily_cap if status else None,
            "hourly_custom": hourly_override is not None,
            "daily_custom": daily_override is not None,
        },
    )


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
