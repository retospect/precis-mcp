"""LLM routing pages + the selector preview's data source.

``GET /api/llm/resolve`` mirrors :func:`~precis.utils.llm.router.resolve_selection`
over HTTP: given ``(model=alias, placement?, reasoning?, temperature?)`` it
returns what :func:`~precis.utils.llm.router.route` would actually pick,
without making a call. The shared ``_llm_selector.html.j2`` macro (used by
smartdraft's ask toolbar, and any future structured picker) fetches this on
every control change to render its live "→ model · placement" preview line.
Read-only, never raises — a bad/unknown alias comes back 200 with the row's
``error`` field set rather than a 4xx, so the widget can render the message
inline instead of special-casing a fetch failure.

``GET /llm`` / ``/llm/chain`` / ``/llm/op`` are the human read-only
baseline the operator reaches for first (gr484946): the only ``/llm/*``
paths that ever existed were the ``/factory`` editor's POST endpoints,
so the guessed GETs 404'd. ``/llm/chain`` renders the same live rung
table as the Models tab header (``_active_routing_ctx`` — resolved via
the same ``resolve_chain``/``resolve_model`` dispatch walks, so it can't
drift) plus the operator-override JSON; ``/llm/op`` the per-operation
routing rollup (``_llm_ops_ctx``). Editing stays on
``/status?tab=services`` — these pages link there.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from precis.utils.llm import keyword_route
from precis.utils.llm.router import (
    LlmRequest,
    Tier,
    _keyword_plan,
    resolve_backend,
    resolve_selection,
)
from precis_web.deps import get_store, templates
from precis_web.routes.status import (
    _active_routing_ctx,
    _llm_chain_ctx,
    _llm_ops_ctx,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm", tags=["llm"])

#: The read-only ``/llm`` section — see the module docstring.
pages = APIRouter(prefix="/llm", tags=["llm"])


def _parse_temperature(raw: str | None) -> float | None:
    """Best-effort float parse — junk or absent both degrade to ``None``
    rather than 400ing the preview fetch (mirrors
    :func:`~precis.utils.llm.router.llm_select_from_payload`'s leniency)."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@router.get("/resolve")
async def resolve(
    request: Request,
    model: str = "big",
    placement: str | None = None,
    reasoning: str | None = None,
    temperature: str | None = None,
) -> JSONResponse:
    """Preview a structured ``(model=alias, placement, reasoning,
    temperature)`` selection — the JSON body is
    :func:`~precis.utils.llm.router.resolve_selection`'s return dict verbatim
    (``tier`` is rendered as its plain string value, already JSON-safe)."""
    row = resolve_selection(
        model,
        placement=placement or None,
        reasoning=reasoning or None,
        temperature=_parse_temperature(temperature),
    )
    return JSONResponse(row)


@pages.get("", response_class=HTMLResponse)
@pages.get("/", response_class=HTMLResponse)
async def llm_index(request: Request) -> RedirectResponse:
    """``/llm`` lands on the chain view — the routing baseline (gr484946)."""
    return RedirectResponse(url="/llm/chain", status_code=302)


@pages.get("/chain", response_class=HTMLResponse)
async def llm_chain(request: Request) -> HTMLResponse:
    """Per-tier live placement chains: every rung's slot, model,
    transport and placement, as ``dispatch`` would walk them right now
    (``_active_routing_ctx``) — plus the cloud dial and any operator
    ``llm.chain.<tier>`` override JSON (``_llm_chain_ctx``)."""
    store = get_store(request)
    ctx: dict[str, Any] = {"active_tab": "llm"}
    ctx.update(await asyncio.to_thread(_active_routing_ctx, store))
    ctx.update(await asyncio.to_thread(_llm_chain_ctx, store))
    return templates.TemplateResponse(request, "llm/chain.html.j2", ctx)


@pages.get("/op", response_class=HTMLResponse)
async def llm_op(request: Request) -> HTMLResponse:
    """Per-operation routing rollup — default vs effective model for
    every registered/observed call site (``_llm_ops_ctx``). Read-only;
    the Services tab owns the edits."""
    store = get_store(request)
    ctx: dict[str, Any] = {"active_tab": "llm"}
    ctx.update(await asyncio.to_thread(_llm_ops_ctx, store))
    return templates.TemplateResponse(request, "llm/op.html.j2", ctx)


# ── keyword rules (precis.utils.llm.keyword_route) ──────────────────
#
# The ``/llm/rules`` section: list, add, disable and reorder the ordered
# (pattern → target) rules ``route()`` matches prompt text against, plus
# the ``/api/llm/keyword-route`` preview naming the rule a sample text
# fires and the plan it would dispatch. Writes hit ``llm_keyword_routes``
# directly and bust the snapshot so this process sees the edit at once.


def _scope_list(raw: str) -> list[str] | None:
    """A comma-joined scope field → a list, or ``None`` (matches all)."""
    vals = [v.strip() for v in raw.split(",") if v.strip()]
    return vals or None


def _add_rule(
    store: Any,
    *,
    pattern: str,
    is_regex: bool,
    target: dict[str, Any],
    sources: list[str] | None,
    tiers: list[str] | None,
    note: str,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_keyword_routes "
            "(position, pattern, is_regex, target, sources, tiers, enabled, note) "
            "VALUES (COALESCE((SELECT max(position) FROM llm_keyword_routes), 0) "
            "+ 1, %s, %s, %s::jsonb, %s, %s, true, %s)",
            (
                pattern,
                is_regex,
                json.dumps(target),
                sources,
                tiers,
                note or None,
            ),
        )
    keyword_route.reset_cache()


def _toggle_rule(store: Any, rule_id: int, enabled: bool) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE llm_keyword_routes SET enabled = %s, updated_at = now() "
            "WHERE id = %s",
            (enabled, rule_id),
        )
    keyword_route.reset_cache()


def _move_rule(store: Any, rule_id: int, direction: str) -> None:
    """Swap ``rule_id`` with its neighbour in match order, then renumber
    ``position`` densely so ordering never drifts into gaps."""
    with store.pool.connection() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM llm_keyword_routes ORDER BY position, id"
            ).fetchall()
        ]
        if rule_id not in ids:
            return
        i = ids.index(rule_id)
        j = i - 1 if direction == "up" else i + 1
        if not 0 <= j < len(ids):
            return
        ids[i], ids[j] = ids[j], ids[i]
        conn.cursor().executemany(
            "UPDATE llm_keyword_routes SET position = %s, updated_at = now() "
            "WHERE id = %s",
            [(pos, rid) for pos, rid in enumerate(ids, 1)],
        )
    keyword_route.reset_cache()


def _target_from_form(target_kind: str, target_value: str) -> dict[str, Any] | None:
    """The rule ``target`` JSON from the add form — the ``llm.op.*``
    grammar. ``None`` ⇒ the form was malformed; the write is skipped."""
    value = target_value.strip()
    if target_kind == "model" and value:
        return {"model": value}
    if target_kind == "tier" and value in {t.value for t in Tier}:
        return {"tier": value}
    if target_kind == "chain" and value:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
        return {"chain": parsed} if isinstance(parsed, list) and parsed else None
    return None


@pages.get("/rules", response_class=HTMLResponse)
async def llm_rules(request: Request) -> HTMLResponse:
    """The keyword-routing rule list, in match order, with enable/reorder
    controls and the sample-text preview."""
    ctx: dict[str, Any] = {
        "active_tab": "llm",
        "kw_rules": await asyncio.to_thread(keyword_route.rules),
        "kw_tiers": [t.value for t in Tier],
    }
    return templates.TemplateResponse(request, "llm/rules.html.j2", ctx)


@pages.post("/rules/add", response_model=None)
async def llm_rule_add(
    request: Request,
    pattern: str = Form(...),
    is_regex: str = Form(""),
    target_kind: str = Form("model"),
    target_value: str = Form(""),
    sources: str = Form(""),
    tiers: str = Form(""),
    note: str = Form(""),
) -> RedirectResponse:
    """Append a keyword rule. A malformed pattern/target is a no-op."""
    store = get_store(request)
    try:
        target = _target_from_form(target_kind, target_value)
        if pattern.strip() and target is not None:
            await asyncio.to_thread(
                _add_rule,
                store,
                pattern=pattern.strip(),
                is_regex=is_regex == "on",
                target=target,
                sources=_scope_list(sources),
                tiers=_scope_list(tiers),
                note=note.strip(),
            )
    except Exception:
        log.warning("llm: rule add failed", exc_info=True)
    return RedirectResponse(url="/llm/rules", status_code=303)


@pages.post("/rules/toggle", response_model=None)
async def llm_rule_toggle(
    request: Request,
    rule_id: int = Form(...),
    enabled: str = Form(...),
) -> RedirectResponse:
    """Enable/disable one rule — disable is the operator's delete."""
    store = get_store(request)
    try:
        await asyncio.to_thread(
            _toggle_rule, store, rule_id, enabled.strip().lower() == "true"
        )
    except Exception:
        log.warning("llm: rule toggle failed", exc_info=True)
    return RedirectResponse(url="/llm/rules", status_code=303)


@pages.post("/rules/move", response_model=None)
async def llm_rule_move(
    request: Request,
    rule_id: int = Form(...),
    direction: str = Form(...),
) -> RedirectResponse:
    """Move one rule up/down in match order (first match wins)."""
    store = get_store(request)
    try:
        await asyncio.to_thread(_move_rule, store, rule_id, direction.strip().lower())
    except Exception:
        log.warning("llm: rule move failed", exc_info=True)
    return RedirectResponse(url="/llm/rules", status_code=303)


@router.get("/keyword-route")
async def keyword_route_preview(
    request: Request,
    text: str = "",
    source: str = "",
    tier: str = "",
) -> JSONResponse:
    """Preview which keyword rule a sample text fires and what it would
    dispatch — matched rule id/pattern/target plus the resolved plan (or
    the fallback reason when the target can't take the call). Read-only;
    a bad ``tier`` or an empty ``text`` degrades, never 4xxs."""
    rule = await asyncio.to_thread(
        keyword_route.match, text, source=source or None, tier=tier or None
    )
    row: dict[str, Any] = {"matched": rule is not None}
    if rule is None:
        return JSONResponse(row)
    row.update(rule_id=rule.rule_id, pattern=rule.pattern, target=rule.target)
    try:
        req_tier = Tier(tier)
    except ValueError:
        req_tier = Tier.BIG
    req = LlmRequest(tier=req_tier, prompt=text, source=source)
    plan = await asyncio.to_thread(_keyword_plan, rule, req, backend=resolve_backend())
    if isinstance(plan, str):
        row["fallback"] = plan
    else:
        ladder, model, plan_tier = plan
        row["model"] = model
        row["tier"] = plan_tier.value
        row["rungs"] = [
            {"model": r.model, "transport": r.transport.value} for r in ladder
        ]
    return JSONResponse(row)
