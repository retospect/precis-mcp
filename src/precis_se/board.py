"""pcb → geometry: a bound board's envelope, ports and segments, derived
at read time from ``mechanical_profile`` — the consumer half of the 0041
bridge (``docs/backlog/pcb-se-binding.md`` §Rough box v0).

**Everything here is pure.** Inputs are the plain dicts
:func:`precis.pcb.export.mechanical_profile` returns plus the instance
rows the caller already fetched — this module never touches a store.
The *pattern* is :mod:`precis_se.catalog`'s (honest ``why_not``, a
narrow unit bridge, derived-never-stored), but the return type is its
own: a board is inherently multi-item — one slab, N per-instance
keep-out prisms, N mounting holes — where ``catalog.Derived``'s one
envelope + one ports dict is the wrong cardinality. :class:`BoardDerived`
still exposes the two attributes ``ops.effective_envelope``/
``effective_ports`` duck-type on, so every clearance/DRC/datum reader
consumes a bound board unchanged.

**Units cross exactly once.** pcb is a mm enclave; se is metres. Numbers
convert through :func:`precis_se.catalog.to_metres` and the cad DSL's
own unit tokens (``require_units=True`` parses ``w100mm`` into SI) —
there is no scale literal anywhere in this module, deliberately: the
"2000 mm tube becomes a 2000 m one" failure class is what the
spec pins a source-scan test on.

**The frame.** x/y are pcb board coordinates (mm → metres, the same
y-convention the netlist IR uses — EasyEDA's flipped axis is already
un-flipped at ingest) shifted by a fixed in-frame offset so the outline
bounding box is centred, matching the cad ``box`` primitive's x/y-centre
anchor. z = 0 sits at the **lowest extent of the derived stack**: the
bottom copper face when nothing is mounted on the bottom side — a
``box`` primitive is base-at-z=0 and cannot express a base below the
block origin, so the one conservative box that must enclose every prism
has to start at the stack's underside. The bottom copper face therefore
sits at ``z = <tallest bottom-side prism>``; top-side prisms rise above
``z = bottom + thickness``. Ports carry their own ``pose``, so this
choice never moves an attachment point.

**Heights are honest.** An instance's own ``height_mm`` wins
(``origin: user``); absent, its footprint is looked up in
:mod:`precis.pcb.package_heights` (``origin: proposed``, source noted),
with the table's largest value as the fallback. Every proposed height is
enumerated by the ``pcb_height_proposed`` DRC finding — a guessed
keep-out is a warning, never a silent fact.

v0 always emits exactly ONE segment — a rigid board is the one-segment
special case — but the list shape is load-bearing for flexboards
(``pcb-flexboard.md``), which decompose into segments joined by fold
lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.cad import dsl as cad_dsl
from precis.pcb import package_heights
from precis_se import catalog
from precis_se.ops import PortSpec


@dataclass(frozen=True)
class BoardDerived:
    """What a bound pcb design yields: one envelope, the mounting-hole
    ports, the itemised segments, and — when resolution fails — the
    reason. ``envelope``/``ports``/``why_not`` duck-type
    :class:`precis_se.catalog.Derived` so ``effective_envelope``/
    ``effective_ports`` treat it unchanged; ``segments`` is the richer
    per-item list (slab, prisms, holes) in metres, block frame."""

    envelope: str | None = None
    ports: dict[str, PortSpec] | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)
    why_not: str | None = None

    @property
    def ok(self) -> bool:
        return self.envelope is not None


def _m(value_mm: Any) -> float:
    """One pcb mm value in metres — the narrow bridge, same helper every
    number in this module crosses. ``mm`` is a unit the table always
    knows, so a ``None`` back from :func:`catalog.to_metres` means a
    non-numeric input, which is a caller bug, not a unit gap."""
    metres = catalog.to_metres(float(value_mm), "mm")
    assert metres is not None  # 'mm' is always in the table
    return metres


def _is_bottom(layer: Any) -> bool:
    return str(layer or "top").lower() in ("bottom", "bot", "b")


def _envelope_box(w_mm: float, d_mm: float, h_mm: float) -> str:
    """The one conservative box, built as a unit-tagged DSL string from
    mm values and converted by the DSL parser itself — the only mm→m
    route this module takes for geometry it *emits* (numbers a consumer
    reads numerically go through :func:`_m`). The emitted form is the
    canonical bare-metres spec every stored envelope parses as."""
    tagged = f"box:w{w_mm:g}mmd{d_mm:g}mmh{h_mm:g}mm"
    return cad_dsl.format_spec(cad_dsl.parse(tagged, require_units=True))


def derive_board(
    profile: dict[str, Any],
    instances: list[dict[str, Any]] | None = None,
) -> BoardDerived:
    """One board's ``mechanical_profile`` (mm) → its se realization
    (metres). ``instances`` is the optional ``pcb_load`` row list, used
    only to join each prism's ``footprint`` for the package-height
    table; absent, every height-less part takes the table fallback —
    degraded, still total.

    Never raises on shape: a profile with no outline and no placed parts
    reports ``why_not`` instead of inventing a board. The caller
    (``persist.attach_boards``) owns the store failure modes — an
    unknown slug arrives as a ``why_not`` it minted itself."""
    outline = profile.get("outline") or []
    if not outline:
        return BoardDerived(why_not="board has no outline and no placed parts")
    xs = [float(p[0]) for p in outline]
    ys = [float(p[1]) for p in outline]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    w_mm, d_mm = x1 - x0, y1 - y0
    t_mm = float(profile.get("thickness_mm") or 0.0)
    if w_mm <= 0.0 or d_mm <= 0.0 or t_mm <= 0.0:
        return BoardDerived(
            why_not="board outline or thickness is degenerate "
            f"(w={w_mm:g} mm, d={d_mm:g} mm, t={t_mm:g} mm)"
        )

    footprints = {str(i.get("refdes")): i.get("footprint") for i in instances or []}

    # Heights: own height_mm → the package-class table → the table's
    # largest value. Every table/fallback height is origin=proposed and
    # carries its source so pcb_height_proposed can name it.
    prisms: list[dict[str, Any]] = []
    top_max_mm = 0.0
    bottom_max_mm = 0.0
    for block in profile.get("blocks") or []:
        refdes = str(block.get("refdes"))
        bottom = _is_bottom(block.get("layer"))
        own = block.get("height_mm")
        if own is not None:
            height_mm, origin, source = float(own), "user", None
        else:
            height_mm, source = package_heights.height_mm(footprints.get(refdes))
            origin = "proposed"
        if bottom:
            bottom_max_mm = max(bottom_max_mm, height_mm)
        else:
            top_max_mm = max(top_max_mm, height_mm)
        prisms.append(
            {
                "refdes": refdes,
                "x_m": _m(float(block["x"]) - cx),
                "y_m": _m(float(block["y"]) - cy),
                "layer": "bottom" if bottom else "top",
                "height_m": _m(height_mm),
                "origin": origin,
                "source": source,
            }
        )

    thickness_m = _m(t_mm)
    bottom_m = _m(bottom_max_mm)
    # z=0 = the lowest extent (see the module docstring's frame note):
    # bottom copper face at z=bottom_m, slab above it, top prisms on top.
    slab_z = [bottom_m, bottom_m + thickness_m]
    for prism in prisms:
        h = prism["height_m"]
        prism["z_m"] = (
            [slab_z[1], slab_z[1] + h]
            if prism["layer"] == "top"
            else [bottom_m - h, bottom_m]
        )

    holes: list[dict[str, Any]] = []
    ports: dict[str, PortSpec] = {}
    for n, hole in enumerate(profile.get("holes") or [], start=1):
        if hole.get("x") is None or hole.get("y") is None:
            continue
        name = f"H{n}"
        hx, hy = _m(float(hole["x"]) - cx), _m(float(hole["y"]) - cy)
        diameter_m = _m(hole.get("diameter") or 0.0)
        holes.append(
            {
                "name": name,
                "x_m": hx,
                "y_m": hy,
                "diameter_m": diameter_m,
                "through_m": thickness_m,
            }
        )
        ports[name] = PortSpec(
            name=name,
            roles=["bore"],
            direction=[0.0, 0.0, 1.0],
            pose=[hx, hy, bottom_m],
            pose_source="bound",
            annotations={"diameter_m": diameter_m, "through_m": thickness_m},
        )

    segments = [
        {
            "slab": {
                # pcb frame (outline-origin), not the centred block frame:
                # the slab polygon is the board's own geometry.
                "outline": [[_m(px), _m(py)] for px, py in outline],
                "thickness_m": thickness_m,
                "z_m": slab_z,
            },
            "prisms": prisms,
            "holes": holes,
        }
    ]
    return BoardDerived(
        envelope=_envelope_box(w_mm, d_mm, bottom_max_mm + t_mm + top_max_mm),
        ports=ports or None,
        segments=segments,
    )


__all__ = ["BoardDerived", "derive_board"]
