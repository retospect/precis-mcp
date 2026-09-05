"""Catalog → geometry: a bought part's envelope and ports, derived from
its ``component`` spec values (``se-off-the-shelf-fabrication.md`` engine
1, rung 2b).

A bought part enters a design as a **binding**, never as a block someone
drew: the block's L3 realization is ``component:<slug>``, and its solid
and attachment points are *derived from the spec row*. Rung 2a made those
rows exist (the standards-series mint); this module turns them into
geometry.

**Everything here is pure.** Inputs are a category and a plain
``{spec_id: value}`` dict **already converted to metres** — se is float64
metres everywhere, while the ``component`` store is mostly millimetres
(migration 0152's unit note). The conversion belongs to the caller that
has the spec registry; this module never touches a store, a unit table or
a network, which is what makes it testable as arithmetic.

**Envelopes are bounding volumes, not shapes.** An se L1 envelope is the
volume a block occupies, so a hex-head screw is one bounding cylinder
over head *and* shank rather than a compound solid. That is not a
simplification to apologize for — it is what L1 means, and the real solid
arrives at L3 with the cad realization.

**Absence is reported, never guessed.** Every generator returns
``(envelope, why_not)`` with exactly one of them set: a part missing the
specs its geometry needs yields ``None`` and a sentence naming what was
missing. Degrading to a plausible-looking cylinder would put an invented
dimension into a clearance check, which is the failure this codebase
refuses everywhere else (suggestive-by-contract).

Dispatch is on **which specs are present**, not on a guessed sub-type
(:data:`_FASTENER_FORMS`) — `component` has one flat `fastener` category
covering screws, nuts and washers, and the spec shape is the only
evidence available for a hand-entered row that carries no series.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis_se.ops import PortSpec

#: Port roles this module coins. Free-form by design (`ops.PortSpec`
#: keeps `roles` an open capability set — the pcb pin→roles pattern), so
#: these are a documented vocabulary rather than an enforced one:
#:
#: - ``bearing-face`` — a face that transmits clamp load (a screw head
#:   underside, a washer face, a nut face).
#: - ``thread`` — a threaded interface; whether it *fits* is a declared
#:   tolerance relation, never a geometric test (threads are never
#:   modelled — see the owning backlog item).
#: - ``shank`` — the plain cylindrical body a clearance hole must pass.
#: - ``bore`` — a through-hole this part presents to something else.
#: - ``end`` — the cut end of a length of stock.
#: - ``seat`` — a surface another part is pressed onto (a bearing race).
ROLES = (
    "bearing-face",
    "thread",
    "shank",
    "bore",
    "end",
    "seat",
)


#: Length units the `component` spec registry actually uses → metres.
#: Not a general unit system: the registry's reserved conversion layer
#: (`component_spec_values.input_unit`) is that. This is the narrow
#: bridge se needs because it is metres everywhere and migration 0152's
#: geometry specs are millimetres.
_TO_METRES: dict[str, float] = {"m": 1.0, "cm": 0.01, "mm": 0.001, "um": 1e-6}


def to_metres(value: float, unit: str | None) -> float | None:
    """One spec value in metres, or ``None`` when ``unit`` isn't a length
    this bridge knows.

    ``None`` is the honest answer, not a passthrough: returning the raw
    number for an unrecognized unit is how a 2000 mm tube becomes a
    2000 m one, and an envelope is the last place a silently-wrong
    magnitude should reach. A unitless spec (``canonical_unit IS NULL``)
    is categorical — ``thread_size``, ``grade`` — and never comes here."""
    if unit is None:
        return None
    factor = _TO_METRES.get(unit)
    if factor is None:
        return None
    return float(value) * factor


@dataclass(frozen=True)
class Derived:
    """What a catalog row yields: an envelope, ports, and — when either
    is absent — the reason. ``why_not`` is prose for a human/agent
    reader; it is never a status code, because the useful thing to say is
    *which spec was missing*."""

    envelope: str | None = None
    ports: dict[str, PortSpec] | None = None
    why_not: str | None = None

    @property
    def ok(self) -> bool:
        return self.envelope is not None


def _fmt(x: float) -> str:
    """One DSL dimension: fixed-point, trailing zeros trimmed. **Not**
    ``%g`` — that emits an exponent for small magnitudes (``3.4e-03``),
    and the DSL's token regex matches only plain decimals, so a
    millimetre-scale part expressed in metres would fail to parse."""
    return f"{x:.9f}".rstrip("0").rstrip(".") or "0"


def _need(specs: dict[str, Any], *keys: str) -> tuple[list[float], list[str]]:
    """Pull ``keys`` as floats. Returns ``(values, missing)``; a key that
    is absent, ``None``, non-numeric or non-positive counts as missing —
    a zero-radius cylinder is not a degraded envelope, it is a wrong
    one."""
    values: list[float] = []
    missing: list[str] = []
    for k in keys:
        raw = specs.get(k)
        try:
            v = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            missing.append(k)
            continue
        if not math.isfinite(v) or v <= 0.0:
            missing.append(k)
            continue
        values.append(v)
    return values, missing


def _axial_ports(
    length: float, *, near: str, far: str, near_roles: list[str], far_roles: list[str]
) -> dict[str, PortSpec]:
    """The two ends of an axial part, ``-z`` and ``+z``. Direction is the
    outward normal, so a mating part's direction opposes it."""
    return {
        near: PortSpec(
            name=near,
            roles=near_roles,
            direction=[0.0, 0.0, -1.0],
            annotations={"axial_offset_m": 0.0},
        ),
        far: PortSpec(
            name=far,
            roles=far_roles,
            direction=[0.0, 0.0, 1.0],
            annotations={"axial_offset_m": length},
        ),
    }


# ── fastener: three forms, told apart by spec shape ───────────────────


def _screw(specs: dict[str, Any]) -> Derived:
    (vals, missing) = _need(specs, "head_diameter", "head_height", "length")
    if missing:
        return Derived(why_not=f"screw needs {', '.join(missing)}")
    head_d, head_h, length = vals
    shank_d = float(specs.get("outer_diameter") or 0.0)
    radius = max(head_d, shank_d) / 2.0
    total = head_h + length
    ports = _axial_ports(
        total,
        near="head",
        far="thread",
        near_roles=["bearing-face"],
        far_roles=["thread"],
    )
    ports["shank"] = PortSpec(
        name="shank",
        roles=["shank"],
        direction=[0.0, 0.0, 1.0],
        annotations={"axial_offset_m": head_h, "diameter_m": shank_d or None},
    )
    return Derived(envelope=f"cyl:r{_fmt(radius)}h{_fmt(total)}", ports=ports)


def _nut(specs: dict[str, Any]) -> Derived:
    (vals, missing) = _need(specs, "across_flats", "height")
    if missing:
        return Derived(why_not=f"nut needs {', '.join(missing)}")
    across_flats, height = vals
    # `hex:` takes a CIRCUMradius; across-flats is twice the inradius, and
    # for a regular hexagon circumradius = inradius * 2/sqrt(3).
    radius = across_flats / math.sqrt(3.0)
    ports = _axial_ports(
        height,
        near="face_a",
        far="face_b",
        near_roles=["bearing-face"],
        far_roles=["bearing-face"],
    )
    ports["thread"] = PortSpec(
        name="thread",
        roles=["thread", "bore"],
        direction=[0.0, 0.0, 1.0],
        annotations={"diameter_m": specs.get("inner_diameter")},
    )
    return Derived(envelope=f"hex:r{_fmt(radius)}h{_fmt(height)}", ports=ports)


def _washer(specs: dict[str, Any]) -> Derived:
    (vals, missing) = _need(specs, "outer_diameter", "thickness")
    if missing:
        return Derived(why_not=f"washer needs {', '.join(missing)}")
    outer_d, thickness = vals
    ports = _axial_ports(
        thickness,
        near="face_a",
        far="face_b",
        near_roles=["bearing-face"],
        far_roles=["bearing-face"],
    )
    ports["bore"] = PortSpec(
        name="bore",
        roles=["bore"],
        direction=[0.0, 0.0, 1.0],
        annotations={"diameter_m": specs.get("inner_diameter")},
    )
    return Derived(
        envelope=f"cyl:r{_fmt(outer_d / 2.0)}h{_fmt(thickness)}", ports=ports
    )


#: Fastener forms in **precedence order**, each with the spec that
#: identifies it. First match wins, so the discriminator must be a spec
#: only that form carries: a head belongs to a screw, an across-flats to a
#: nut (a hex-head *screw* is caught by the head rule first, which is
#: correct — its envelope is head-over-shank, not the nut's prism), and a
#: washer is what is left with a thickness.
_FASTENER_FORMS: tuple[tuple[str, str, Callable[[dict[str, Any]], Derived]], ...] = (
    ("head_diameter", "screw", _screw),
    ("head_height", "screw", _screw),
    ("across_flats", "nut", _nut),
    ("thickness", "washer", _washer),
)


def _fastener(specs: dict[str, Any]) -> Derived:
    for key, _form, fn in _FASTENER_FORMS:
        if specs.get(key) is not None:
            return fn(specs)
    return Derived(
        why_not=(
            "fastener needs one of head_diameter/head_height (screw), "
            "across_flats (nut) or thickness (washer) to tell which form "
            "it is"
        )
    )


# ── stock and rotating parts ──────────────────────────────────────────


def _tube(specs: dict[str, Any]) -> Derived:
    (vals, missing) = _need(specs, "outer_diameter", "length_overall")
    if missing:
        return Derived(why_not=f"pipe/profile needs {', '.join(missing)}")
    outer_d, length = vals
    ports = _axial_ports(
        length, near="end_a", far="end_b", near_roles=["end"], far_roles=["end"]
    )
    bore = specs.get("inner_diameter")
    if bore is not None:
        ports["bore"] = PortSpec(
            name="bore",
            roles=["bore"],
            direction=[0.0, 0.0, 1.0],
            annotations={"diameter_m": bore},
        )
    return Derived(envelope=f"cyl:r{_fmt(outer_d / 2.0)}h{_fmt(length)}", ports=ports)


def _bearing(specs: dict[str, Any]) -> Derived:
    (vals, missing) = _need(specs, "outer_diameter", "width")
    if missing:
        return Derived(why_not=f"bearing needs {', '.join(missing)}")
    outer_d, width = vals
    ports = _axial_ports(
        width, near="face_a", far="face_b", near_roles=["seat"], far_roles=["seat"]
    )
    ports["od"] = PortSpec(
        name="od",
        roles=["seat"],
        direction=[1.0, 0.0, 0.0],
        annotations={"diameter_m": outer_d},
    )
    bore = specs.get("bore_diameter_bearing") or specs.get("inner_diameter")
    if bore is not None:
        ports["bore"] = PortSpec(
            name="bore",
            roles=["bore", "seat"],
            direction=[0.0, 0.0, 1.0],
            annotations={"diameter_m": bore},
        )
    return Derived(envelope=f"cyl:r{_fmt(outer_d / 2.0)}h{_fmt(width)}", ports=ports)


def _sheet(specs: dict[str, Any]) -> Derived:
    (vals, missing) = _need(specs, "thickness")
    if missing:
        return Derived(why_not=f"sheet needs {', '.join(missing)}")
    (thickness,) = vals
    plan, plan_missing = _need(specs, "width", "height")
    if plan_missing:
        # A stock thickness with no plan dimensions is the NORMAL case for
        # sheet: the outline comes from the design, not the catalog. Say
        # so precisely rather than inventing a square.
        return Derived(
            why_not=(
                f"sheet has thickness but no plan size ({', '.join(plan_missing)}) "
                "— a cut part's outline comes from the design, not the "
                "catalog row; set the block's own envelope"
            )
        )
    width, height = plan
    ports = _axial_ports(
        thickness,
        near="face_a",
        far="face_b",
        near_roles=["bearing-face"],
        far_roles=["bearing-face"],
    )
    return Derived(
        envelope=f"box:w{_fmt(width)}d{_fmt(height)}h{_fmt(thickness)}", ports=ports
    )


#: One generator per `component` category. A category with no generator
#: is not an error — it is a part whose geometry we have not taught yet,
#: and :func:`derive` says so by name.
GENERATORS: dict[str, Callable[[dict[str, Any]], Derived]] = {
    "fastener": _fastener,
    "pipe": _tube,
    "profile": _tube,
    "bearing": _bearing,
    "laminate": _sheet,
}


def derive(category: str | None, specs: dict[str, Any]) -> Derived:
    """The envelope and ports for one bound component.

    ``specs`` values must already be **metres** (module docstring). An
    unknown or absent category, or a spec set too thin for its generator,
    yields a :class:`Derived` carrying only ``why_not`` — which callers
    render as an honest gap, never as a fallback shape."""
    if not category:
        return Derived(why_not="component has no category")
    fn = GENERATORS.get(category)
    if fn is None:
        return Derived(
            why_not=(
                f"no envelope generator for category {category!r} "
                f"(have: {', '.join(sorted(GENERATORS))})"
            )
        )
    return fn(specs)
