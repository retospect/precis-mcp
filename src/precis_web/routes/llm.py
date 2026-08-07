"""LLM selector preview — the structured-selection widget's data source.

``GET /api/llm/resolve`` mirrors :func:`~precis.utils.llm.router.resolve_selection`
over HTTP: given ``(model=alias, placement?, reasoning?, temperature?)`` it
returns what :func:`~precis.utils.llm.router.dispatch` would actually pick,
without making a call. The shared ``_llm_selector.html.j2`` macro (used by
smartdraft's ask toolbar, and any future structured picker) fetches this on
every control change to render its live "→ model · placement" preview line.
Read-only, never raises — a bad/unknown alias comes back 200 with the row's
``error`` field set rather than a 4xx, so the widget can render the message
inline instead of special-casing a fetch failure.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from precis.utils.llm.router import resolve_selection

router = APIRouter(prefix="/api/llm", tags=["llm"])


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
