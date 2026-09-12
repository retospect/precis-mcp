"""The ``se``/``nm`` web reader — round 1 (docs/backlog thread pending
its own transcription pass; the spec of record is prod gripe gr335242,
items 1-3): project a design's posed envelope union to SVG, isolate a
named subtree, and step through a discrete abstraction ladder. One
router serves both kinds off the shared projector
(:mod:`precis_web.blocktree_svg`) — ``se``'s and ``nm``'s block trees
are both :mod:`precis.blocktree` subclasses carrying the same L1
envelope/pose triple (module docstring there); only se additionally
carries the axial-member stability overlay (:mod:`precis_se.stability`)
— nm's header stops at validate + fill-fraction, honestly, since nm has
no whole-structure verdict concept.

* ``GET  /se``, ``GET  /nm`` — the design list (mirrors
  ``routes/cad.py``'s ``/cad``).
* ``GET  /se/{slug}``, ``GET  /nm/{slug}`` — the reader page: axis/level/
  colour/isolate selectors (a plain GET form — no client JS needed for
  round 1) over an ``<img>`` of the SVG endpoint.
* ``GET  /se/{slug}/view.svg``, ``GET  /nm/{slug}/view.svg`` — the
  render itself. Query params: ``axis`` (x|y|z, default z — top view),
  ``level`` (the named abstraction ladder, default 'refined'),
  ``colour`` (the fill channel, default 'part'), ``isolate`` (a block
  name — render only its subtree, recentred).

Round 2 (explicitly out of scope here — see the gripe): argue-with-
points (click → anchor → job), the anchored-notes layer, and the
Three.js/three-cad-viewer route.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from precis.blocktree.types import BlockNode, Tree
from precis.errors import NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis_nm import persist as nm_persist
from precis_nm import validate as nm_validate
from precis_nm.ops import effective_envelope as nm_effective_envelope
from precis_se import persist as se_persist
from precis_se import stability as se_stability
from precis_se import validate as se_validate
from precis_se.ops import effective_envelope as se_effective_envelope
from precis_web.blocktree_svg import (
    AXES,
    COLOUR_CHANNELS,
    LEVELS,
    Axis,
    MemberLine,
    Tier,
    build_block_draws,
    children_map,
    fill_fraction_line,
    force_colour,
    plan_visibility,
    project_point,
    render_svg,
    validator_summary,
)
from precis_web.deps import get_store, templates
from precis_web.timefmt import ago as _ago

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["blocktree"])

#: List-view cap (a browse surface, not an export) — mirrors cad.py.
_LIST_LIMIT = 100

_TIER_RANK = {"ok": 0, "warn": 1, "error": 2}


def _combine_tier(a: Tier, b: Tier) -> Tier:
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


def _stability_tier(verdict: str) -> Tier:
    """Stability-verdict → header tier.

    ``classify()``'s verdict strings carry dynamic counts (``"rigid
    (statically indeterminate — 2 self-stress state(s))"``), so this
    matches by prefix, never exact equality. Three buckets:

    * ``rigid (...)``, ``prestress-stabilized (...)``, ``no axial
      members ...`` — fine, tier 'ok'.
    * the tripwire line (:data:`precis_se.stability.TRIPWIRE_LINE`,
      verbatim) — an honest "the second-order test couldn't run", never
      a confirmed failure — tier 'warn'. The header still renders the
      line's full text (never a shortened label): a "not checked"
      statement earns the qualifier, not just the colour.
    * every other verdict — by construction (``stability._verdict``) that
      is one of the "first-order mobile ... NOT stabilized ..." /
      "... infeasible for the declared members ..." phrasings: a
      *confirmed* unstabilized mechanism — tier 'error'.
    """
    if verdict == se_stability.TRIPWIRE_LINE:
        return "warn"
    if (
        verdict.startswith("rigid")
        or verdict.startswith("prestress-stabilized")
        or verdict.startswith("no axial members")
    ):
        return "ok"
    return "error"


@dataclass
class _Adapter:
    kind: str
    label: str
    load_tree: Any  # Callable[[Store, int], Tree[BlockNode, Any]]
    effective_envelope: Any  # blocktree_svg.EffectiveEnvelopeFn, per the domain's own tree/block subclasses
    validate: Any  # Callable[[Tree[BlockNode, Any]], list[Any]]
    is_realized: Any  # Callable[[BlockNode], bool]
    has_stability: bool


def _se_is_realized(node: Any) -> bool:
    return bool(getattr(node, "mode", None)) or bool(getattr(node, "bound_kind", None))


def _nm_is_realized(node: Any) -> bool:
    return bool(getattr(node, "bound_design", None))


_ADAPTERS: dict[str, _Adapter] = {
    "se": _Adapter(
        kind="se",
        label="Structural envelopes",
        load_tree=se_persist.load_tree,
        effective_envelope=se_effective_envelope,
        validate=se_validate.validate,
        is_realized=_se_is_realized,
        has_stability=True,
    ),
    "nm": _Adapter(
        kind="nm",
        label="Molecular machines",
        load_tree=nm_persist.load_tree,
        effective_envelope=nm_effective_envelope,
        validate=nm_validate.validate,
        is_realized=_nm_is_realized,
        has_stability=False,
    ),
}

#: One SQL statement per kind's own block table — table names are never
#: interpolated from a request, so this stays two plain literals rather
#: than a dynamic-identifier query.
_LIST_SQL = {
    "se": """
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'
                 ORDER BY created_at DESC LIMIT 1)             AS slug,
               r.title,
               (SELECT count(*) FROM se_blocks b
                 WHERE b.ref_id = r.ref_id AND b.retired_at IS NULL) AS n_blocks,
               r.updated_at
          FROM refs r
         WHERE r.kind = 'se' AND r.retired_at IS NULL
         ORDER BY r.ref_id DESC
         LIMIT %s
    """,
    "nm": """
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'
                 ORDER BY created_at DESC LIMIT 1)             AS slug,
               r.title,
               (SELECT count(*) FROM nm_blocks b
                 WHERE b.ref_id = r.ref_id AND b.retired_at IS NULL) AS n_blocks,
               r.updated_at
          FROM refs r
         WHERE r.kind = 'nm' AND r.retired_at IS NULL
         ORDER BY r.ref_id DESC
         LIMIT %s
    """,
}


def _list_rows(store: Store, kind: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(_LIST_SQL[kind], (_LIST_LIMIT,)).fetchall()
    return [
        {
            "ref_id": int(r[0]),
            "slug": r[1],
            "title": r[2] or r[1],
            "n_blocks": int(r[3]),
            "updated": _ago(r[4]),
        }
        for r in rows
    ]


def _require_ref(store: Store, kind: str, slug: str) -> Any:
    return resolve_live_slug_ref(store, kind=kind, id=slug)


async def _list_page(request: Request, kind: str) -> HTMLResponse:
    store = get_store(request)
    adapter = _ADAPTERS[kind]
    rows = _list_rows(store, kind)
    return templates.TemplateResponse(
        request,
        "blocktree/list.html.j2",
        {
            "active_tab": kind,
            "kind": kind,
            "kind_label": adapter.label,
            "designs": rows,
            "total": len(rows),
        },
    )


async def _detail_page(
    request: Request,
    kind: str,
    slug: str,
    *,
    axis: str,
    level: str,
    colour: str,
    isolate: str | None,
) -> Any:
    store = get_store(request)
    adapter = _ADAPTERS[kind]
    try:
        ref = _require_ref(store, kind, slug)
    except NotFound:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": f"{adapter.label} design not found",
                "detail": f"no live {kind} design with slug {slug!r}",
                "status": 404,
            },
            status_code=404,
        )
    tree = await asyncio.to_thread(adapter.load_tree, store, ref.id)
    block_names = sorted(tree.blocks)
    axis = axis if axis in AXES else "z"
    level = level if level in LEVELS else "refined"
    colour = colour if colour in COLOUR_CHANNELS else "part"
    isolate = isolate if isolate in tree.blocks else None
    # slug/isolate are design-controlled (block/slug names accept almost
    # any character) — url-encode both so a name like "fork & tip" or one
    # containing '#' can't corrupt/truncate the query string.
    svg_url = (
        f"/{kind}/{quote(slug, safe='')}/view.svg"
        f"?axis={axis}&level={level}&colour={colour}"
    )
    if isolate:
        svg_url += f"&isolate={quote(isolate, safe='')}"
    return templates.TemplateResponse(
        request,
        "blocktree/detail.html.j2",
        {
            "active_tab": kind,
            "kind": kind,
            "kind_label": adapter.label,
            "slug": ref.slug,
            "title": ref.title or ref.slug,
            "axes": AXES,
            "levels": LEVELS,
            "colour_channels": COLOUR_CHANNELS,
            "axis": axis,
            "level": level,
            "colour": colour,
            "isolate": isolate or "",
            "block_names": block_names,
            "svg_url": svg_url,
        },
    )


def _build_svg(
    store: Store,
    kind: str,
    ref_id: int,
    *,
    axis: Axis,
    level: str,
    colour: str,
    isolate: str | None,
) -> str | None:
    """Off the event loop (store round-trip + the tessellate/hull work).
    Returns ``None`` when ``isolate`` names a block that doesn't exist —
    the route maps that to a 400, never a silent empty image."""
    adapter = _ADAPTERS[kind]
    tree: Tree[BlockNode, Any] = adapter.load_tree(store, ref_id)
    kids = children_map(tree)
    try:
        plan = plan_visibility(tree, kids, level=level, isolate=isolate)
    except KeyError:
        return None
    draws = build_block_draws(
        tree,
        adapter.effective_envelope,
        kids,
        plan,
        axis,
        is_realized=adapter.is_realized,
    )
    fill_line = fill_fraction_line(tree, adapter.effective_envelope)
    findings = adapter.validate(tree)
    vline, vtier = validator_summary(findings)
    members: list[MemberLine] = []
    if adapter.has_stability:
        report = se_stability.classify(tree)  # type: ignore[arg-type]
        header_lines = [
            f"stability: {report.verdict}",
            f"validate: {vline}",
            fill_line,
        ]
        tier = _combine_tier(_stability_tier(report.verdict), vtier)
        for row in report.members:
            if row.skipped is not None:
                continue
            a = tree.blocks.get(row.a_block)
            b = tree.blocks.get(row.b_block)
            if a is None or b is None:
                continue
            members.append(
                MemberLine(
                    a=project_point(a.pose, axis),
                    b=project_point(b.pose, axis),
                    colour=force_colour(row.role, row.self_stress),
                    subject=row.subject,
                )
            )
    else:
        header_lines = [f"validate: {vline}", fill_line]
        tier = vtier
    return render_svg(
        draws, members, channel=colour, header_lines=header_lines, tier=tier
    )


async def _svg_response(
    request: Request,
    kind: str,
    slug: str,
    *,
    axis: str,
    level: str,
    colour: str,
    isolate: str | None,
) -> Response:
    store = get_store(request)
    try:
        ref = _require_ref(store, kind, slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    if axis not in AXES:
        return JSONResponse(
            {"error": f"unknown axis {axis!r} — one of {AXES}"}, status_code=400
        )
    if level not in LEVELS:
        return JSONResponse(
            {"error": f"unknown level {level!r} — one of {LEVELS}"}, status_code=400
        )
    if colour not in COLOUR_CHANNELS:
        return JSONResponse(
            {"error": f"unknown colour {colour!r} — one of {COLOUR_CHANNELS}"},
            status_code=400,
        )

    def _build() -> str | None:
        return _build_svg(
            store,
            kind,
            ref.id,
            axis=axis,
            level=level,
            colour=colour,
            isolate=isolate,
        )

    svg = await asyncio.to_thread(_build)
    if svg is None:
        return JSONResponse({"error": f"no such subtree {isolate!r}"}, status_code=400)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


# ── se routes ────────────────────────────────────────────────────────────


@router.get("/se", response_class=HTMLResponse)
async def se_list(request: Request) -> HTMLResponse:
    return await _list_page(request, "se")


@router.get("/se/{slug}")
async def se_detail(
    request: Request,
    slug: str,
    axis: str = "z",
    level: str = "refined",
    colour: str = "part",
    isolate: str | None = None,
) -> Any:
    return await _detail_page(
        request, "se", slug, axis=axis, level=level, colour=colour, isolate=isolate
    )


@router.get("/se/{slug}/view.svg")
async def se_view_svg(
    request: Request,
    slug: str,
    axis: str = "z",
    level: str = "refined",
    colour: str = "part",
    isolate: str | None = None,
) -> Response:
    return await _svg_response(
        request, "se", slug, axis=axis, level=level, colour=colour, isolate=isolate
    )


# ── nm routes ────────────────────────────────────────────────────────────


@router.get("/nm", response_class=HTMLResponse)
async def nm_list(request: Request) -> HTMLResponse:
    return await _list_page(request, "nm")


@router.get("/nm/{slug}")
async def nm_detail(
    request: Request,
    slug: str,
    axis: str = "z",
    level: str = "refined",
    colour: str = "part",
    isolate: str | None = None,
) -> Any:
    return await _detail_page(
        request, "nm", slug, axis=axis, level=level, colour=colour, isolate=isolate
    )


@router.get("/nm/{slug}/view.svg")
async def nm_view_svg(
    request: Request,
    slug: str,
    axis: str = "z",
    level: str = "refined",
    colour: str = "part",
    isolate: str | None = None,
) -> Response:
    return await _svg_response(
        request, "nm", slug, axis=axis, level=level, colour=colour, isolate=isolate
    )
