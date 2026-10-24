"""pcb → geometry: a bound board's envelope, ports and segments, derived
at read time from ``mechanical_profile`` — the consumer half of the 0041
bridge (``docs/backlog/pcb-se-binding.md`` §Rough box v0).

**Everything here is pure**, and since pcb-workspace-code slice 0b the
*derivation itself* is not here: :func:`precis_geom.pcb_shape.
derive_shape` produces the itemised shape (slab, per-part prisms with
the ``origin: proposed`` honesty pair, holes, keepouts) in mm in the
board frame — the one derivation the workspace container also runs. This
module does the two se-side mappings on top, exactly once:

- **Units cross once.** pcb is a mm enclave; se is metres. Every number
  converts through :func:`precis_se.catalog.to_metres` and the cad DSL's
  own unit tokens (``require_units=True`` parses ``w100mm`` into SI) —
  there is no scale literal anywhere in this module, deliberately: the
  "2000 mm tube becomes a 2000 m one" failure class is what the
  spec pins a source-scan test on.
- **The frame.** x/y are recentred on the outline bounding box (the cad
  ``box`` primitive's x/y-centre anchor); z is shifted so z = 0 sits at
  the **lowest extent of the derived stack**: the bottom copper face
  when nothing is mounted on the bottom side — a ``box`` primitive is
  base-at-z=0 and cannot express a base below the block origin, so the
  one conservative box that must enclose every prism has to start at the
  stack's underside. Ports carry their own ``pose``, so this choice
  never moves an attachment point.

**Heights are honest** — inside the shared derivation: an instance's own
``height_mm`` wins (``origin: user``); absent, its footprint is looked
up in the package-class table (``origin: proposed``, source noted), with
the table's largest value as the fallback. Every proposed height is
enumerated by the ``pcb_height_proposed`` DRC finding — a guessed
keep-out is a warning, never a silent fact.

The *pattern* is :mod:`precis_se.catalog`'s (honest ``why_not``, a
narrow unit bridge, derived-never-stored), but the return type is its
own: a board is inherently multi-item — one slab, N per-instance
keep-out prisms, N mounting holes — where ``catalog.Derived``'s one
envelope + one ports dict is the wrong cardinality. :class:`BoardDerived`
still exposes the two attributes ``ops.effective_envelope``/
``effective_ports`` duck-type on, so every clearance/DRC/datum reader
consumes a bound board unchanged.

v0 always emits exactly ONE segment — a rigid board is the one-segment
special case — but the list shape is load-bearing for flexboards
(``pcb-flexboard.md``), which decompose into segments joined by fold
lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.cad import dsl as cad_dsl
from precis_geom import pcb_shape
from precis_se import catalog
from precis_se.ops import PortSpec


@dataclass(frozen=True)
class BoardDerived:
    """What a bound pcb design yields: one envelope, the mounting-hole
    ports, the itemised segments, and — when resolution fails — the
    reason. ``envelope``/``ports``/``why_not`` duck-type
    :class:`precis_se.catalog.Derived` so ``effective_envelope``/
    ``effective_ports`` treat it unchanged; ``segments`` is the richer
    per-item list (slab, prisms, holes, keepouts) in metres, block frame."""

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
    details = {
        str(i.get("refdes")): {"footprint": i.get("footprint")} for i in instances or []
    }
    shape = pcb_shape.derive_shape(profile, details=details)
    if shape.why_not is not None:
        return BoardDerived(why_not=shape.why_not)

    cx_mm, cy_mm = shape.centre_mm
    # pcb board frame has z = 0 at the bottom copper face; the block
    # frame puts z = 0 at the lowest extent (see the module docstring's
    # frame note), i.e. shifted down by the tallest bottom-side prism.
    z_shift_mm = shape.bottom_extent_mm

    prisms: list[dict[str, Any]] = []
    for p in shape.prisms:
        prisms.append(
            {
                "refdes": p.refdes,
                "x_m": _m(p.x_mm - cx_mm),
                "y_m": _m(p.y_mm - cy_mm),
                "layer": p.side,
                "height_m": _m(p.height_mm),
                "origin": p.origin,
                "source": p.source,
                "z_m": [_m(z + z_shift_mm) for z in p.z_mm],
            }
        )

    holes: list[dict[str, Any]] = []
    ports: dict[str, PortSpec] = {}
    for hole in shape.holes:
        hx, hy = _m(hole.x_mm - cx_mm), _m(hole.y_mm - cy_mm)
        diameter_m = _m(hole.diameter_mm)
        holes.append(
            {
                "name": hole.name,
                "x_m": hx,
                "y_m": hy,
                "diameter_m": diameter_m,
                "through_m": _m(hole.through_mm),
            }
        )
        ports[hole.name] = PortSpec(
            name=hole.name,
            roles=["bore"],
            direction=[0.0, 0.0, 1.0],
            pose=[hx, hy, _m(z_shift_mm)],
            pose_source="bound",
            annotations={"diameter_m": diameter_m, "through_m": _m(hole.through_mm)},
        )

    keepouts = [
        {
            "poly": [[_m(px), _m(py)] for px, py in k.poly],
            "layers": list(k.layers),
            "reason": k.reason,
        }
        for k in shape.keepouts
    ]

    env = shape.envelope_mm()
    segments = [
        {
            "slab": {
                # pcb frame (outline-origin), not the centred block frame:
                # the slab polygon is the board's own geometry.
                "outline": [[_m(px), _m(py)] for px, py in shape.outline],
                "thickness_m": _m(shape.thickness_mm),
                "z_m": [_m(z_shift_mm), _m(shape.thickness_mm + z_shift_mm)],
            },
            "prisms": prisms,
            "holes": holes,
            "keepouts": keepouts,
        }
    ]
    return BoardDerived(
        envelope=_envelope_box(env["w"], env["d"], env["h"]),
        ports=ports or None,
        segments=segments,
    )


__all__ = ["BoardDerived", "derive_board"]
