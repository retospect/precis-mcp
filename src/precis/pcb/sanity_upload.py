"""Put a routed sanity board into a store as a frozen ``pcb`` board, for
viewing only.

The board lands exactly as the in-memory run left it: every part
``fixed='both'`` (the ``import-epro --freeze`` mechanism: a later route
cannot re-place it), the routed tracks and vias as router copper. Slugs
live in the ``sanity-`` namespace and an upload NEVER replaces a board: an
existing slug is refused (or, with ``version=True``, the upload takes the
next free ``-vN``). ``pcb_apply`` would silently extend an existing board,
so the check is made here, before any write.
"""

from __future__ import annotations

from typing import Any

from precis.pcb import sanity_board as sb

SLUG_PREFIX = "sanity-"


class UploadRefused(Exception):
    """The upload would touch a board it must not; nothing was written."""


def base_slug(spec: sb.SanitySpec) -> str:
    return f"{SLUG_PREFIX}{spec.name}"


def resolve_slug(store: Any, base: str, *, version: bool = False) -> str:
    """``base`` if free; else the next free ``base-vN`` when ``version``,
    else :class:`UploadRefused`. Anything outside the namespace is refused."""
    if not base.startswith(SLUG_PREFIX):
        raise UploadRefused(f"slug {base!r} is outside the {SLUG_PREFIX!r} namespace")
    if store.get_ref(kind="pcb", id=base) is None:
        return base
    n = 2
    while store.get_ref(kind="pcb", id=f"{base}-v{n}") is not None:
        n += 1
    if not version:
        raise UploadRefused(
            f"pcb {base!r} already exists and is never replaced; "
            f"pass --version to upload as {base}-v{n}"
        )
    return f"{base}-v{n}"


def _components(result: sb.SanityResult, graph: dict[str, Any]) -> list[dict[str, Any]]:
    comps = []
    for inst in graph["instances"]:
        x, y, rot = result.poses[inst["refdes"]]
        comps.append(
            {
                "refdes": inst["refdes"],
                "label": "sanity header",
                "footprint": sb.FOOTPRINT,
                "x": round(x, 4),
                "y": round(y, 4),
                "rot": round(rot, 4),
                "layer": inst["layer"],
                "fixed": "both",
                "pins": [{"name": f"P{i}"} for i in range(1, sb.PINS + 1)],
            }
        )
    return comps


def _footprint() -> dict[str, Any]:
    pads = [
        {"pin": f"P{p['number']}", **{k: p[k] for k in ("shape", "x", "y", "w", "h")}}
        for p in sb.footprint()["pads"]
    ]
    return {"name": sb.FOOTPRINT, "pads": pads}


def _copper_rows(
    result: sb.SanityResult, ir: Any, net_ids: dict[str, int]
) -> list[dict[str, Any]]:
    """Track and via rows in the shape ``pcb_route`` persists (no pours on
    this board)."""
    names = [s["name"] for s in sb.STACKUP]
    rows: list[dict[str, Any]] = []
    for t in result.rres.tracks:
        net_id = net_ids.get(str(ir.net_name[t.net_id]))
        if net_id is None:
            continue
        rows.append(
            {
                "ctype": "track",
                "layer": names[max(0, min(t.layer, len(names) - 1))],
                "net_id": net_id,
                "route_id": None,
                "geom": {
                    "segments": list(t.segments),
                    "length_mm": round(t.length_mm, 4),
                    "width_mm": t.width_mm,
                    "is_dogbone": t.is_dogbone,
                },
            }
        )
    for v in result.rres.vias:
        net_id = net_ids.get(str(ir.net_name[v.net_id]))
        if net_id is None:
            continue
        rows.append(
            {
                "ctype": "via",
                "layer": names[v.layer_lo],
                "net_id": net_id,
                "route_id": None,
                "geom": {
                    "x": round(v.x, 4),
                    "y": round(v.y, 4),
                    "dia_mm": v.dia_mm,
                    "drill_mm": v.drill_mm,
                    "span": [names[v.layer_lo], names[v.layer_hi]],
                },
            }
        )
    return rows


def upload(
    store: Any,
    result: sb.SanityResult,
    built: sb.Built,
    *,
    version: bool = False,
) -> str:
    """Write ``result`` as a frozen board; returns the slug. Raises
    :class:`UploadRefused` (nothing written) when the slug is taken and
    ``version`` is off."""
    slug = resolve_slug(store, base_slug(result.spec), version=version)
    graph = built.graph
    verdict = f"{result.routed}/{result.nets} routed, {sum(result.errors_by_rule.values())} DRC errors"
    with store.tx() as conn:
        ref, _created, _counts = store.pcb_apply(
            slug=slug,
            title=f"Sanity board {result.spec.name} ({verdict})",
            components=_components(result, graph),
            nets=[
                {"name": n["name"], "net_class": n["net_class"]} for n in graph["nets"]
            ],
            connections=[
                {"net": n["name"], "refdes": m["refdes"], "pin": m["pin"]}
                for n in graph["nets"]
                for m in n["members"]
            ],
            features=[{"ftype": "outline", "geom": {"path": sb.outline()}}],
            footprints=[_footprint()],
            meta={"sanity": {"variant": result.spec.name, "verdict": verdict}},
            conn=conn,
        )
        ref_id = int(ref.id)
        board_id = store.pcb_ensure_board(ref_id, conn=conn)
        store.pcb_set_stackup(board_id, sb.STACKUP, conn=conn)
    store.pcb_copper_replace(
        board_id, _copper_rows(result, built.ir, store.pcb_net_ids(ref_id))
    )
    return slug


def page_path(slug: str) -> str:
    return f"/pcb/{slug}"
