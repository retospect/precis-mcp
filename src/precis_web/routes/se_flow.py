"""Flow tab — the slice-2b particle player of docs/backlog/se-flow-kernel.md.

* ``GET /se/{slug}/flow`` — the player page: a three.js scene of the
  chamber (floor topography receiving the shadows of the particles,
  translucent walls and lid, ports and the tracking plane as overlays)
  with play/pause, scrubber, speed, loop, colour modes, trails and the
  regime card. Offers every ``flow:`` chamber-spec pocket of the design
  (:func:`precis_se.flow.flow_pockets`); ``?block=&pocket=`` picks one.
* ``GET /se/{slug}/flow.json`` — the ``view='flow'`` ``format='json'``
  payload (:func:`precis_se.flow.flow_payload`) for that pocket. Query
  args override the pocket's spec and add the trace and regime inputs:
  ``n``, ``frames``, ``seed``, ``profile``, ``dv``, ``nx``, ``size``
  (m — the chamber size the clock and the card judge), ``mu``, ``rho``,
  ``u0`` | ``dp``, ``particle_d``, ``rho_p``, ``molecular_d``,
  ``mean_free_path``. 404 unknown design; 422 ``{"error", "next"}`` on
  a refused input (no flow pocket, bad number, non-thin chamber).

The chamber build (ray-marching the envelope) and the trace dominate —
tens of seconds on a ``field:`` chamber — so both are memoised per
design version; a regime-card or size change reuses them.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from precis.errors import BadInput, NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis_se import flow as se_flow
from precis_se import persist as se_persist
from precis_web.deps import get_store, templates

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["se"])

log = logging.getLogger(__name__)

#: Player defaults: particle count and recorded frames (2k particles ×
#: 200 frames ≈ 4.8 MB of float32 — the player interpolates between).
DEFAULT_N = 2000
DEFAULT_FRAMES = 200
#: The instanced mesh's ceiling (spec: 2 k–20 k particles).
MAX_N = 20_000
MAX_FRAMES = 1000

_NUM_ARGS = (
    "n",
    "frames",
    "seed",
    "dv",
    "nx",
    "zplane",
    "size",
    "mu",
    "rho",
    "u0",
    "dp",
    "particle_d",
    "rho_p",
    "molecular_d",
    "mean_free_path",
)
_INT_ARGS = {"n", "frames", "seed", "nx"}
_CHAMBER_KEYS = ("block", "inlet", "outlet", "nx", "zplane")
_TRACE_KEYS = (*_CHAMBER_KEYS, "dv", "profile", "n", "frames", "seed")

_CACHE_SIZE = 8
_lock = threading.Lock()
_chambers: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
_traces: OrderedDict[tuple[Any, ...], Any] = OrderedDict()


def _memo(
    cache: OrderedDict[tuple[Any, ...], Any], key: tuple[Any, ...], build: Any
) -> Any:
    with _lock:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    value = build()
    with _lock:
        cache[key] = value
        while len(cache) > _CACHE_SIZE:
            cache.popitem(last=False)
    return value


def _query_args(request: Request) -> dict[str, Any]:
    """Numeric query args parsed; a malformed one is a :class:`BadInput`."""
    out: dict[str, Any] = {}
    q = request.query_params
    for key in _NUM_ARGS:
        raw = (q.get(key) or "").strip()
        if not raw:
            continue
        try:
            out[key] = int(raw) if key in _INT_ARGS else float(raw)
        except ValueError:
            raise BadInput(
                f"flow: {key} must be a number, got {raw!r}",
                next=f"drop {key}= or give a number",
            ) from None
    for key in ("profile", "block", "pocket"):
        raw = (q.get(key) or "").strip()
        if raw:
            out[key] = raw
    return out


def _pick_pocket(
    tree: Any, block: str | None, pocket: str | None
) -> se_flow.FlowPocket:
    found = se_flow.flow_pockets(tree)
    if not found:
        raise BadInput(
            "this design has no flow chamber spec",
            next=(
                "add a pocket shaped 'flow:in=<patch>;out=<patch>;dv=<d_v*>' "
                "on the chamber block"
            ),
        )
    for fp in found:
        if (block in (None, fp.block)) and (pocket in (None, fp.pocket)):
            return fp
    names = ", ".join(f"{fp.block}/{fp.pocket}" for fp in found)
    raise BadInput(
        f"no flow pocket {block or '*'}/{pocket or '*'}",
        next=f"pick one of: {names}",
    )


def player_payload(store: Store, slug: str, query: dict[str, Any]) -> dict[str, Any]:
    """The player payload for ``slug`` under ``query`` (see the module
    docstring) — memoised chamber and trace, fresh card."""
    ref = resolve_live_slug_ref(store, kind="se", id=slug)
    tree = se_persist.load_tree(store, ref.id)
    fp = _pick_pocket(tree, query.get("block"), query.get("pocket"))
    args: dict[str, Any] = {
        "block": fp.block,
        "inlet": fp.spec.inlet,
        "outlet": fp.spec.outlet,
        "dv": fp.spec.d_v_star,
        "profile": fp.spec.profile,
        "zplane": fp.spec.zplane,
        "n": DEFAULT_N,
        "frames": DEFAULT_FRAMES,
    }
    args.update({k: v for k, v in query.items() if k not in ("block", "pocket")})
    if not 1 <= int(args["n"]) <= MAX_N:
        raise BadInput(f"flow: n must lie in 1..{MAX_N}", next=f"n={DEFAULT_N}")
    if not 2 <= int(args["frames"]) <= MAX_FRAMES:
        raise BadInput(
            f"flow: frames must lie in 2..{MAX_FRAMES}", next=f"frames={DEFAULT_FRAMES}"
        )
    version = (ref.id, str(getattr(ref, "updated_at", "") or ""))
    ckey = (*version, *(args.get(k) for k in _CHAMBER_KEYS))
    chamber = _memo(_chambers, ckey, lambda: se_flow.flow_chamber(store, tree, args))
    tkey = (*version, *(args.get(k) for k in _TRACE_KEYS))
    _field, paths, profile, dv = _memo(
        _traces, tkey, lambda: se_flow.flow_trace(chamber, args)
    )
    card, scale = se_flow.flow_scale(chamber, args)
    payload = se_flow.flow_payload(
        chamber,
        paths,
        card,
        u0=scale["u0"],
        profile=profile,
        d_v_star=dv,
        block=fp.block,
        scale=scale,
    )
    payload["pocket"] = fp.pocket
    payload["breakthrough"] = paths.breakthrough
    return payload


def _refused(exc: BadInput) -> JSONResponse:
    return JSONResponse(
        {"error": str(exc), "next": getattr(exc, "next", None)}, status_code=422
    )


@router.get("/se/{slug}/flow.json")
async def se_flow_json(request: Request, slug: str) -> Response:
    store = get_store(request)
    try:
        query = _query_args(request)
        payload = await asyncio.to_thread(player_payload, store, slug, query)
    except NotFound:
        return JSONResponse({"error": f"no live se design {slug!r}"}, status_code=404)
    except BadInput as exc:
        return _refused(exc)
    resp = JSONResponse(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/se/{slug}/flow", response_class=HTMLResponse)
async def se_flow_page(request: Request, slug: str) -> Response:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="se", id=slug)
    except NotFound:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "se design not found",
                "detail": f"no live se design with slug {slug!r}",
                "status": 404,
            },
            status_code=404,
        )
    tree = await asyncio.to_thread(se_persist.load_tree, store, ref.id)
    pockets = se_flow.flow_pockets(tree)
    q = request.query_params
    block, pocket = q.get("block"), q.get("pocket")
    chosen = next(
        (
            fp
            for fp in pockets
            if block in (None, fp.block) and pocket in (None, fp.pocket)
        ),
        None,
    )
    base = f"/se/{quote(ref.slug or slug, safe='')}"
    return templates.TemplateResponse(
        request,
        "blocktree/flow.html.j2",
        {
            "active_tab": "se",
            "kind": "se",
            "kind_label": "Structural envelopes",
            "slug": ref.slug or slug,
            "title": ref.title or ref.slug or slug,
            "view3d_url": base,
            "pockets": [
                {
                    "block": fp.block,
                    "pocket": fp.pocket,
                    "url": (
                        f"{base}/flow?block={quote(fp.block, safe='')}"
                        f"&pocket={quote(fp.pocket, safe='')}"
                    ),
                    "chosen": fp is chosen,
                }
                for fp in pockets
            ],
            "payload_url": (
                f"{base}/flow.json?block={quote(chosen.block, safe='')}"
                f"&pocket={quote(chosen.pocket, safe='')}"
                if chosen is not None
                else None
            ),
            "defaults": {
                "n": DEFAULT_N,
                "frames": DEFAULT_FRAMES,
                "profile": chosen.spec.profile if chosen is not None else "parabolic",
            },
        },
    )
