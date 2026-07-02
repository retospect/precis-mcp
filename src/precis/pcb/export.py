"""Exporters off the placed netlist IR (ADR 0042 §6, §13).

Export is the **only** place the design leaves the relational graph. Every
exporter is pure (IR in → text/dict out), so each is trivially testable and
none needs a binary — the binaries (Freerouting, kicad-cli) are gated at the
*router* step (:mod:`precis.pcb.route`), not here.

Targets, in fab order:

- :func:`bom_csv`   — the JLCPCB **BOM** (``Comment,Designator,Footprint,
  LCSC Part #``), one row per distinct part, designators grouped.
- :func:`cpl_csv`   — the JLCPCB **CPL / pick-and-place**
  (``Designator,Mid X,Mid Y,Layer,Rotation``). Carries the one real
  coordinate-frame conversion in the system (:func:`jlc_rotation`) — our
  internal **CW-from-north** rotation → JLCPCB's **CCW** convention.
- :func:`kicad_netlist` — a minimal KiCad s-expr netlist (components + nets).
- :func:`specctra_dsn`  — the **Specctra .dsn** the rented autorouter eats:
  board boundary + per-instance images + the network. Uses real Flow-B pad
  geometry where cached, a spread-pin placeholder otherwise (clearly so).
- :func:`mechanical_profile` — the **0041 bridge**: board outline + mounting
  holes + component height-blocks as a 2D/2.5D profile a ``cad`` enclosure
  references.

The IR these consume is the :func:`export_model` normalisation of
``store.pcb_load`` (instance detail: refdes/lcsc/footprint/pose) +
``store.pcb_graph`` (net membership + the unconnected pins).
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

# Default fab/board constants (JLCPCB 4-layer default, ADR 0042 §"Layers").
DEFAULT_THICKNESS_MM = 1.6
DEFAULT_TRACK_UM = 250  # 0.25 mm — JLCPCB economical minimum is 0.127
DEFAULT_CLEARANCE_UM = 200  # 0.2 mm
DEFAULT_VIA_UM = 600  # 0.6 mm pad / 0.3 mm drill
_DSN_PLACEHOLDER_PITCH_MM = 1.0  # spread placeholder pins so they never overlap


# ─────────────────────────────────────────────────────────────────────
# model
# ─────────────────────────────────────────────────────────────────────
def export_model(
    load: dict[str, list[dict[str, Any]]],
    graph: dict[str, Any],
) -> dict[str, Any]:
    """Normalise ``pcb_load`` + ``pcb_graph`` into one export model.

    Returns ``{instances, nets}`` where each instance carries its full pose +
    the set of pin names it owns (connected ∪ unconnected) and each net carries
    its ``members`` (``{refdes, pin}``) + class + width.
    """
    instances = [dict(i) for i in load["instances"]]
    by_refdes = {i["refdes"]: i for i in instances}
    for i in instances:
        i["pins"] = []
    # pins: from net membership + the unconnected list (the union = real pads).
    seen: dict[str, set[str]] = {i["refdes"]: set() for i in instances}
    for net in graph["nets"]:
        for m in net["members"]:
            seen.setdefault(m["refdes"], set()).add(m["pin"])
    for u in graph.get("unconnected", []):
        seen.setdefault(u["refdes"], set()).add(u["pin"])
    for refdes, pins in seen.items():
        if refdes in by_refdes:
            by_refdes[refdes]["pins"] = sorted(pins, key=_natural_key)

    # nets: membership from the graph; class/width from the load rows.
    width_by_name = {n["name"]: n.get("width_mm") for n in load["nets"]}
    nets = [
        {
            "name": n["name"],
            "net_class": n.get("net_class"),
            "width_mm": width_by_name.get(n["name"]),
            "members": list(n["members"]),
        }
        for n in graph["nets"]
        if n["members"]
    ]
    return {"instances": instances, "nets": nets}


def _natural_key(s: str) -> tuple[str, int, str]:
    """Sort ``R2`` before ``R10`` before ``U1`` — letter prefix, then number."""
    m = re.match(r"^([A-Za-z]*)(\d*)(.*)$", str(s))
    if not m:
        return (str(s), 0, "")
    pre, num, rest = m.groups()
    return (pre, int(num) if num else 0, rest)


# ─────────────────────────────────────────────────────────────────────
# BOM
# ─────────────────────────────────────────────────────────────────────
def bom_csv(model: dict[str, Any]) -> str:
    """JLCPCB BOM CSV: one row per distinct (comment, footprint, LCSC),
    designators comma-joined. Parts with no LCSC number are still listed
    (LCSC blank) — they cannot be JLCPCB-assembled and the caller should flag
    them."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for i in model["instances"]:
        key = (
            str(i.get("label") or ""),
            str(i.get("footprint") or ""),
            str(i.get("part_lcsc") or ""),
        )
        groups.setdefault(key, []).append(i["refdes"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
    for (comment, footprint, lcsc), refs in sorted(
        groups.items(), key=lambda kv: _natural_key(min(kv[1], key=_natural_key))
    ):
        designators = ",".join(sorted(refs, key=_natural_key))
        w.writerow([comment, designators, footprint, lcsc])
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────
# CPL / pick-and-place
# ─────────────────────────────────────────────────────────────────────
def jlc_rotation(rot: float | None, *, bottom: bool = False) -> float:
    """Convert our internal rotation (**CW from north**, ADR 0042 coordinate
    frame) to JLCPCB's CPL convention (**CCW positive**, KiCad-aligned). Both
    treat 0° as the part's natural footprint orientation. This is the
    documented CPL footgun — kept in one place so every exporter agrees.

    Top side: a pure negation mod 360. Bottom side: JLCPCB reads bottom-layer
    rotations as viewed from the *bottom* (the board flipped), so the mirror
    cancels the negation and adds the 180° flip — ``(rot + 180) % 360``, the
    KiCad Fabrication-Toolkit mapping ``(180 - θ_ccw) % 360`` translated into
    our CW frame. The Specctra DSN does **not** use this: DSN coordinates stay
    top-view for both sides (the ``back`` side token carries the mirror)."""
    if bottom:
        return (float(rot or 0.0) + 180.0) % 360.0
    return (360.0 - float(rot or 0.0)) % 360.0


def cpl_csv(model: dict[str, Any]) -> str:
    """JLCPCB CPL CSV (``Designator,Mid X,Mid Y,Layer,Rotation``). Positions
    are mm relative to the board-outline origin (our native frame); rotation is
    converted to JLCPCB's CCW convention. Unplaced instances (no x/y) are
    skipped — the caller flags them."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
    for i in sorted(model["instances"], key=lambda r: _natural_key(r["refdes"])):
        if i.get("x") is None or i.get("y") is None:
            continue
        bottom = str(i.get("layer") or "top").lower() in ("bottom", "bot", "b")
        w.writerow(
            [
                i["refdes"],
                f"{float(i['x']):.4f}",
                f"{float(i['y']):.4f}",
                "Bottom" if bottom else "Top",
                f"{jlc_rotation(i.get('rot'), bottom=bottom):g}",
            ]
        )
    return buf.getvalue()


def unplaced(model: dict[str, Any]) -> list[str]:
    """Refdes of instances with no coordinates (a CPL/route blocker)."""
    return [
        i["refdes"]
        for i in model["instances"]
        if i.get("x") is None or i.get("y") is None
    ]


def missing_lcsc(model: dict[str, Any]) -> list[str]:
    """Refdes of instances with no LCSC number (cannot be JLCPCB-assembled)."""
    return [i["refdes"] for i in model["instances"] if not i.get("part_lcsc")]


# ─────────────────────────────────────────────────────────────────────
# KiCad netlist (s-expr)
# ─────────────────────────────────────────────────────────────────────
def kicad_netlist(model: dict[str, Any], *, name: str = "design") -> str:
    """A minimal but well-formed KiCad s-expr netlist (components + nets).
    Enough for ``kicad-cli``/EDA round-tripping; the DSN is the autorouter's
    input."""
    out = [
        '(export (version "E")',
        "  (design",
        f'    (source "{name}"))',
        "  (components",
    ]
    for i in sorted(model["instances"], key=lambda r: _natural_key(r["refdes"])):
        fp = str(i.get("footprint") or "")
        val = str(i.get("label") or i["refdes"])
        line = f'    (comp (ref "{i["refdes"]}") (value "{_esc(val)}")'
        if fp:
            line += f' (footprint "{_esc(fp)}")'
        if i.get("part_lcsc"):
            line += f' (property (name "LCSC") (value "{i["part_lcsc"]}"))'
        out.append(line + ")")
    out.append("  )")
    out.append("  (nets")
    for code, net in enumerate(
        sorted(model["nets"], key=lambda n: _natural_key(n["name"])), start=1
    ):
        out.append(f'    (net (code "{code}") (name "{_esc(net["name"])}")')
        for m in sorted(net["members"], key=lambda x: _natural_key(x["refdes"])):
            out.append(f'      (node (ref "{m["refdes"]}") (pin "{_esc(m["pin"])}"))')
        out.append("    )")
    out.append("  ))")
    return "\n".join(out) + "\n"


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


# ─────────────────────────────────────────────────────────────────────
# Specctra .dsn (the autorouter input)
# ─────────────────────────────────────────────────────────────────────
def board_bbox(
    model: dict[str, Any], *, margin_mm: float = 5.0
) -> tuple[float, float, float, float]:
    """Bounding box (x0, y0, x1, y1) of the placed parts + a margin — the
    fallback board boundary when no explicit outline feature exists."""
    xs = [float(i["x"]) for i in model["instances"] if i.get("x") is not None]
    ys = [float(i["y"]) for i in model["instances"] if i.get("y") is not None]
    if not xs or not ys:
        return (0.0, 0.0, 50.0, 50.0)
    return (
        min(xs) - margin_mm,
        min(ys) - margin_mm,
        max(xs) + margin_mm,
        max(ys) + margin_mm,
    )


def _pin_offsets(
    inst: dict[str, Any], footprints: dict[str, dict[str, Any]]
) -> list[tuple[str, float, float]]:
    """(pin_name, dx_mm, dy_mm) for an instance. Real Flow-B pad geometry when
    the footprint is cached; a non-overlapping spread otherwise (so the DSN is
    structurally valid even before easyeda2kicad conversion lands)."""
    lcsc = str(inst.get("part_lcsc") or "")
    fp = footprints.get(lcsc) if lcsc else None
    pins = list(inst.get("pins") or [])
    if fp and fp.get("pads") and fp.get("pin_map"):
        pin_map = fp["pin_map"]  # pad-id -> pin name
        by_name = {v: k for k, v in pin_map.items()}
        out = []
        for name in pins:
            pad = next(
                (p for p in fp["pads"] if str(p.get("n")) == str(by_name.get(name))),
                None,
            )
            if pad is not None:
                out.append((name, float(pad.get("x", 0.0)), float(pad.get("y", 0.0))))
            else:  # pin not in the cached pad set — spread it
                out.append((name, 0.0, 0.0))
        if any(dx or dy for _, dx, dy in out):
            return out
    # placeholder: lay pins on a centred row at a fixed pitch.
    n = len(pins)
    x0 = -(_DSN_PLACEHOLDER_PITCH_MM * (n - 1)) / 2.0
    return [
        (name, x0 + k * _DSN_PLACEHOLDER_PITCH_MM, 0.0) for k, name in enumerate(pins)
    ]


def specctra_dsn(
    model: dict[str, Any],
    *,
    footprints: dict[str, dict[str, Any]] | None = None,
    outline: list[list[float]] | None = None,
    name: str = "design",
    track_um: int = DEFAULT_TRACK_UM,
    clearance_um: int = DEFAULT_CLEARANCE_UM,
) -> str:
    """Emit a Specctra ``.dsn`` (the rented autorouter's input). Coordinates in
    0.1 µm units — ``(resolution um 10)`` means 10 units per µm, KiCad's
    convention, so every emitted integer is ``mm × 10000``. One image per
    instance (avoids cross-part pin-set clashes); a single shared round
    padstack; the network from the model's nets.

    With cached Flow-B footprints the pin geometry is real; without them pins
    are spread on a placeholder grid so the file is valid and routes
    centroid-ish — honest until easyeda2kicad conversion lands (Slice 2
    deferred item)."""
    footprints = footprints or {}

    # (resolution um 10) ⇒ 10 units per µm; a raw-µm emission here is the
    # 10×-undersized-board bug, so scale ALL geometry (coords, widths, radii).
    units_per_um = 10

    def um(mm: float) -> int:
        return round(mm * 1000.0 * units_per_um)

    placed = [
        i
        for i in model["instances"]
        if i.get("x") is not None and i.get("y") is not None
    ]
    if outline:
        path = " ".join(f"{um(p[0])} {um(p[1])}" for p in outline)
        # 'pcb' is the reserved board-outline layer id (KiCad-compatible);
        # 'signal' here is a layer *type* token Freerouting cannot resolve.
        boundary = f"(boundary (path pcb 0 {path}))"
    else:
        x0, y0, x1, y1 = board_bbox(model)
        boundary = f"(boundary (rect pcb {um(x0)} {um(y0)} {um(x1)} {um(y1)}))"

    pad_r = (DEFAULT_VIA_UM // 2) * units_per_um
    out = [
        f"(pcb {_dsn_id(name)}",
        '  (parser (string_quote ") (space_in_quoted_tokens on)'
        ' (host_cad "precis") (host_version "0042"))',
        "  (resolution um 10)",
        "  (unit um)",
        "  (structure",
        "    (layer F.Cu (type signal) (property (index 0)))",
        "    (layer B.Cu (type signal) (property (index 1)))",
        f"    {boundary}",
        f'    (via "Via[0-1]_{DEFAULT_VIA_UM}:0")',
        f"    (rule (width {track_um * units_per_um}) "
        f"(clearance {clearance_um * units_per_um}))",
        "  )",
        "  (placement",
    ]
    for i in sorted(placed, key=lambda r: _natural_key(r["refdes"])):
        img = _dsn_id(f"img_{i['refdes']}")
        side = (
            "back" if str(i.get("layer") or "top").lower().startswith("b") else "front"
        )
        rot = f"{jlc_rotation(i.get('rot')):g}"
        out.append(f"    (component {img}")
        out.append(
            f"      (place {_dsn_id(i['refdes'])} {um(float(i['x']))} "
            f"{um(float(i['y']))} {side} {rot}))"
        )
    out.append("  )")
    out.append("  (library")
    for i in sorted(placed, key=lambda r: _natural_key(r["refdes"])):
        img = _dsn_id(f"img_{i['refdes']}")
        out.append(f"    (image {img}")
        for name_, dx, dy in _pin_offsets(i, footprints):
            out.append(
                f"      (pin Round[A]Pad_{DEFAULT_VIA_UM}_um {_dsn_id(name_)} "
                f"{um(dx)} {um(dy)})"
            )
        out.append("    )")
    out.append(
        f"    (padstack Round[A]Pad_{DEFAULT_VIA_UM}_um "
        f"(shape (circle F.Cu {pad_r})) (shape (circle B.Cu {pad_r})) "
        "(attach off))"
    )
    out.append("  )")
    out.append("  (network")
    placed_refs = {i["refdes"] for i in placed}
    for net in sorted(model["nets"], key=lambda n: _natural_key(n["name"])):
        pins = " ".join(
            f"{_dsn_id(m['refdes'])}-{_dsn_id(m['pin'])}"
            for m in sorted(net["members"], key=lambda x: _natural_key(x["refdes"]))
            if m["refdes"] in placed_refs
        )
        if pins:
            out.append(f"    (net {_dsn_id(net['name'])} (pins {pins}))")
    out.append("  )")
    out.append(")")
    return "\n".join(out) + "\n"


def _dsn_id(s: str) -> str:
    """A DSN-safe token (no spaces/parens); quote if it has odd chars."""
    s = str(s)
    if re.fullmatch(r"[A-Za-z0-9_./+\-]+", s):
        return s
    return '"' + s.replace('"', "'") + '"'


# ─────────────────────────────────────────────────────────────────────
# Mechanical profile — the 0041 enclosure bridge
# ─────────────────────────────────────────────────────────────────────
def mechanical_profile(
    model: dict[str, Any],
    features: list[dict[str, Any]],
    *,
    thickness_mm: float = DEFAULT_THICKNESS_MM,
) -> dict[str, Any]:
    """Board outline + mounting holes + component height-blocks as a 2.5D
    profile a ``cad`` enclosure references (ADR 0042 §6, the 0041 bridge).

    Outline: an explicit ``outline`` feature's ``geom.path`` if present, else
    the placed-parts bounding box. Holes: ``mounting_hole`` features with a
    ``geom.diameter``. Blocks: each placed instance's courtyard (best-effort)
    × ``height_mm`` — a coarse keep-out volume for the lid."""
    holes = []
    outline_path: list[list[float]] | None = None
    for f in features:
        ft = str(f.get("ftype") or "")
        geom = f.get("geom") or {}
        if ft == "outline" and isinstance(geom.get("path"), list):
            outline_path = [[float(p[0]), float(p[1])] for p in geom["path"]]
        elif ft == "mounting_hole":
            holes.append(
                {
                    "x": f.get("x"),
                    "y": f.get("y"),
                    "diameter": geom.get("diameter") or geom.get("d"),
                }
            )
    if outline_path is None:
        x0, y0, x1, y1 = board_bbox(model, margin_mm=2.0)
        outline_path = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    blocks = []
    for i in model["instances"]:
        if i.get("x") is None or i.get("y") is None:
            continue
        blocks.append(
            {
                "refdes": i["refdes"],
                "x": float(i["x"]),
                "y": float(i["y"]),
                "layer": i.get("layer") or "top",
                "height_mm": i.get("height_mm"),
            }
        )
    return {
        "units": "mm",
        "thickness_mm": thickness_mm,
        "outline": outline_path,
        "holes": holes,
        "blocks": blocks,
    }


__all__ = [
    "board_bbox",
    "bom_csv",
    "cpl_csv",
    "export_model",
    "jlc_rotation",
    "kicad_netlist",
    "mechanical_profile",
    "missing_lcsc",
    "specctra_dsn",
    "unplaced",
]
