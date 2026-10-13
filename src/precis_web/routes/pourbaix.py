"""Pourbaix tab — the resting-state surface-Pourbaix validity map (slice 2).

* ``GET /pourbaix/{handle}`` — the map for one structure handle (``st<id>``
  or slug) or, for a quest handle (``qu<id>``/id), every candidate structure
  that serves it. The map object is the one ``view='pourbaix'`` renders as
  text (:mod:`precis.quest.pourbaix_map`, reached here through the
  pathway-side name :mod:`precis_pathway.pourbaix_map` — ``precis_web`` sits
  outside the core→plugin import boundary): validity regions per U interval,
  each boundary with its propagated and model-form bands drawn apart, and
  the bulk verdict as a dashed, labelled uncertain layer.

Static for now — the template keeps a slider-bank slot that slice 3 fills
(the staircase panel rides the same bank).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis.utils import handle_registry
from precis_web.deps import get_store, templates
from precis_web.pourbaix_svg import map_svg
from precis_web.ref_urls import ref_url

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["pourbaix"])

log = logging.getLogger(__name__)


def _resolve(store: Store, handle: str) -> tuple[str, Any] | None:
    """``(kind, ref)`` for a quest or structure handle, else ``None``.

    ``qu<N>``/``st<N>`` parse through the handle registry; anything else is
    tried as a structure slug, then a bare ref_id on both kinds."""
    parsed = handle_registry.parse(handle)
    if parsed is not None and not parsed[1] and parsed[0] in ("quest", "structure"):
        ref = store.get_ref(kind=parsed[0], id=parsed[2])
        return (parsed[0], ref) if ref is not None else None
    ref = store.get_ref(kind="structure", id=handle)
    if ref is not None:
        return "structure", ref
    if handle.strip().isdigit():
        ref = store.get_ref(kind="quest", id=int(handle))
        if ref is not None:
            return "quest", ref
        ref = store.get_ref(kind="structure", id=int(handle))
        if ref is not None:
            return "structure", ref
    return None


def _map_ctx(m: dict[str, Any]) -> dict[str, Any]:
    """One map for the template: the SVG plus its provenance footer."""
    return {
        "svg": map_svg(m),
        "anchor_key": str(m.get("anchor_key") or "?"),
        "models": m.get("models") or [],
        "engine_version": m.get("engine_version"),
        "corrections": m.get("corrections"),
        "uncorrected": bool(m.get("uncorrected")),
        "scan_jobs": m.get("scan_jobs") or [],
        "sigma_e_eV": m.get("sigma_e_eV"),
        "incomplete": m.get("incomplete") or [],
        "bulk": m.get("bulk"),
    }


def _candidate_ctx(store: Store, ref: Any) -> dict[str, Any]:
    from precis_pathway import pourbaix_map

    handle = handle_registry.try_format("structure", int(ref.id)) or f"st{ref.id}"
    return {
        "ref_id": int(ref.id),
        "handle": handle,
        "title": (ref.title or "").splitlines()[0] if ref.title else "",
        "url": ref_url("structure", ref.id, getattr(ref, "slug", None)),
        "maps": [_map_ctx(m) for m in pourbaix_map.maps_for_structure(store, ref)],
    }


@router.get("/pourbaix/{handle}", response_class=HTMLResponse)
async def pourbaix_detail(request: Request, handle: str) -> HTMLResponse:
    store = get_store(request)
    from precis_pathway import pourbaix_map

    resolved = _resolve(store, handle)
    if resolved is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Pourbaix map not found",
                "detail": f"no live quest or structure with handle {handle!r}",
                "status": 404,
            },
            status_code=404,
        )
    kind, ref = resolved
    if kind == "quest":
        entries = []
        for e in pourbaix_map.quest_maps(store, ref):
            srv = e["ref"]
            entries.append(
                {
                    "ref_id": int(srv.id),
                    "handle": e["handle"],
                    "title": (srv.title or "").splitlines()[0] if srv.title else "",
                    "url": ref_url("structure", srv.id, getattr(srv, "slug", None)),
                    "maps": [_map_ctx(m) for m in e["maps"]],
                }
            )
        subject = {
            "kind": "quest",
            "handle": handle_registry.try_format("quest", ref.id) or f"qu{ref.id}",
            "title": (ref.title or "").splitlines()[0] if ref.title else "",
        }
    else:
        entries = [_candidate_ctx(store, ref)]
        subject = {
            "kind": "structure",
            "handle": handle_registry.try_format("structure", ref.id) or f"st{ref.id}",
            "title": (ref.title or "").splitlines()[0] if ref.title else "",
        }
    return templates.TemplateResponse(
        request,
        "pourbaix/detail.html.j2",
        {
            "active_tab": "pourbaix",
            "subject": subject,
            "title": f"surface-Pourbaix — {subject['handle']}",
            "entries": entries,
            "bulk_note": pourbaix_map.BULK_LAYER_NOTE,
        },
    )


__all__ = ["router"]
