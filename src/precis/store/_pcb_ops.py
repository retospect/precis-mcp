"""Store ops for the ``pcb`` kind.

A design is a slug-addressed ``refs`` row (``kind='pcb'``) keeping
**one** ``card_combined`` chunk for intent-search; the graph lives in
normalized ``pcb_*`` tables: ``pcb_components`` (a component *type*,
owns its pins), ``pcb_pins`` (pad+function+electrical tags),
``pcb_instances`` (a placement/refdes), ``pcb_nets`` (a named, classed
signal), ``pcb_netconns`` (the netlist triple net/instance/pin — a pin
is on <=1 net; composite FKs force pin+instance to share a component).

Authoring is **batch**: :meth:`pcb_apply` lays down
components+pins+instances/nets/connections in one transaction,
*re-runnable* (existing refdes/net names reused, not duplicated). Reads
(:meth:`pcb_load`, :meth:`pcb_instance_neighbors`,
:meth:`pcb_net_members`) back graph traversal; the derived layer
(ratsnest/crossings) is computed by the handler, not stored.

Canonical electrical pads have one active pin name. Conflicting authored
declarations refuse the whole batch, including on a skipped existing refdes:
silently dropping a declaration lets connections recreate it unbound. Physical
footprint pad duplicates remain geometry, not separate electrical identities.
Re-put is create-or-extend, not a pin editor or historical-data repair — pin
SEMANTICS on an existing instance are edited via :meth:`pcb_patch_pins`
(``op='pins'``; docs/backlog/pcb-datasheet-review.md slice 2).

Mixin assumes the concrete Store provides ``self.pool``/``self.tx``/
``self.insert_ref``/``self.get_ref``.
"""

from __future__ import annotations

import contextvars
import math
import time
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from itertools import islice
from typing import Any

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from precis.errors import BadInput, NotFound
from precis.pcb import (
    CAP_DIELECTRIC_RANK,
    DEFAULT_STACKUP,
    PIN_CURRENT_ROLES,
    PIN_ETYPES,
    route_summary_status,
)
from precis.pcb import generators as pcb_generators
from precis.pcb import geom as pcb_geom
from precis.pcb import padplace as pcb_padplace
from precis.pcb.capabilities import capability_for
from precis.store.types import ActorSlug
from precis.utils.units import parse_angle_deg


def _normalize_supplies(
    raw: Any, component: str, declared: set[str] | None
) -> list[dict[str, Any]]:
    """Validate a component's ``supplies`` declaration (pcb-lazy-netlist-and-
    checks.md §1b): ``{"role", "blocks": [[pin, ...], ...], optional
    "voltage_v", optional "within_block_swappable"}`` or a list of them.
    Returns the list form (what ``pcb_graph`` hands the route job).
    ``declared`` is the set of pin names declared for the component, or
    ``None`` to skip the membership check (no pin declarations to check
    against yet)."""
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(
                f"component {component}: supplies must be an object or a list of "
                f"objects {{role, blocks}}; got {type(item).__name__}"
            )
        role = item.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(
                f"component {component}: supplies needs a non-empty string 'role'"
            )
        blocks = item.get("blocks")
        if (
            not isinstance(blocks, list)
            or not blocks
            or not all(isinstance(b, list) and b for b in blocks)
        ):
            raise ValueError(
                f"component {component}: supplies role {role!r} needs 'blocks': a "
                "non-empty list of non-empty pin-name lists"
            )
        norm_blocks: list[list[str]] = []
        for b in blocks:
            names = [str(x).strip() for x in b]
            if declared is not None:
                missing = [x for x in names if x not in declared]
                if missing:
                    raise ValueError(
                        f"component {component}: supplies role {role!r} names "
                        f"undeclared pin(s) {missing!r}; declare them in 'pins'"
                    )
            norm_blocks.append(names)
        entry: dict[str, Any] = {"role": role.strip(), "blocks": norm_blocks}
        volt = item.get("voltage_v")
        if volt is not None:
            if isinstance(volt, bool) or not isinstance(volt, int | float):
                raise ValueError(
                    f"component {component}: supplies role {role!r} voltage_v "
                    "must be a number"
                )
            entry["voltage_v"] = float(volt)
        if item.get("within_block_swappable") is not None:
            entry["within_block_swappable"] = bool(item["within_block_swappable"])
        out.append(entry)
    return out


def _normalize_client_of(raw: Any, component: str, pin: str) -> dict[str, Any]:
    """A pin's ``client_of``: a role name, or ``{"role", optional
    "min_voltage_v"}``. Returns the dict form."""
    if isinstance(raw, str):
        raw = {"role": raw}
    if not isinstance(raw, dict):
        raise ValueError(
            f"component {component}: pin {pin!r} client_of must be a role name "
            "or {role, min_voltage_v}"
        )
    role = raw.get("role")
    if not isinstance(role, str) or not role.strip():
        raise ValueError(
            f"component {component}: pin {pin!r} client_of needs a non-empty 'role'"
        )
    out: dict[str, Any] = {"role": role.strip()}
    mv = raw.get("min_voltage_v")
    if mv is not None:
        if isinstance(mv, bool) or not isinstance(mv, int | float):
            raise ValueError(
                f"component {component}: pin {pin!r} client_of min_voltage_v "
                "must be a number"
            )
        out["min_voltage_v"] = float(mv)
    return out


def _normalize_roles(roles: Any) -> list[tuple[str, float | None, int | None]]:
    """Validate the top-level ``roles`` list: ``[{name, voltage_v, fanout}]``
    (``voltage_v`` number or null, ``fanout`` positive int or null =
    unbounded; absent fanout defaults to 1)."""
    if not isinstance(roles, list):
        raise ValueError("pcb roles must be a list of {name, voltage_v, fanout}")
    out: list[tuple[str, float | None, int | None]] = []
    for r in roles:
        if not isinstance(r, dict):
            raise ValueError("pcb role must be an object {name, voltage_v, fanout}")
        name = r.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("pcb role needs a non-empty string name")
        volt = r.get("voltage_v")
        if volt is not None and (
            isinstance(volt, bool) or not isinstance(volt, int | float)
        ):
            raise ValueError(f"pcb role {name!r}: voltage_v must be a number or null")
        fan = r.get("fanout", 1)
        if fan is not None and (
            isinstance(fan, bool) or not isinstance(fan, int) or fan < 1
        ):
            raise ValueError(
                f"pcb role {name!r}: fanout must be a positive integer or null"
            )
        out.append((name.strip(), None if volt is None else float(volt), fan))
    return out


def _validate_pin_declarations(
    pins: list[dict[str, Any]],
    component: str,
    pin_map: dict[str, Any] | None = None,
) -> None:
    """Check canonical ownership without choosing a winner or rebinding.

    ``pin_map`` is the component's footprint ``pin_map`` (pad number ->
    ``{"name": ...}``), when known. An explicit pad the footprint already
    maps to a different pin name is a conflict (gr467885), unless that
    other pin is itself explicitly re-padded in ``pins``."""
    by_name: dict[str, str | None] = {}
    by_pad: dict[str, str] = {}
    for pin in pins:
        name = str(pin.get("name") or "").strip()
        if not name:
            continue
        pad = str(pin["pad"]) if pin.get("pad") is not None else None
        if name in by_name and by_name[name] != pad:
            raise ValueError(
                f"component {component}: pin {name!r} declares contradictory pads "
                f"{by_name[name]!r} and {pad!r}; use one pad declaration for this "
                "canonical name or correct the erroneous pad number"
            )
        if pad is not None and pad in by_pad and by_pad[pad] != name:
            raise ValueError(
                f"component {component}: pad {pad!r} is claimed by pins "
                f"{by_pad[pad]!r} and {name!r}; use one canonical name for this "
                "physical pad in pins and connections, or correct the erroneous "
                "pad number"
            )
        by_name[name] = pad
        if pad is not None:
            by_pad[pad] = name
    for name, pad in by_name.items():
        entry = (pin_map or {}).get(pad) if pad is not None else None
        owner = entry.get("name") if isinstance(entry, dict) else None
        if owner is None or str(owner) in (name, pad):
            continue  # unmapped, same pin, or the normalizer's identity entry
        owner = str(owner)
        if by_name.get(owner) is not None and by_name[owner] != pad:
            continue  # the footprint's pin is re-padded in this same put
        raise ValueError(
            f"component {component}: pin {name!r} declares pad {pad!r}, but the "
            f"footprint maps pad {pad!r} to pin {owner!r}; use one canonical "
            "name for this physical pad in pins and connections, or correct "
            "the erroneous pad number"
        )


_BULK_BATCH = 2000  # rows per round trip in parts_bulk_replace


def _rot_deg(d: dict[str, Any]) -> float:
    """A placement's rotation in degrees (the pcb enclave's stored unit).
    ``rot_deg`` self-names; ``rot`` is the same degrees (bare number) or an
    explicit-unit string (``"90deg"``, ``"1.5708 rad"``) — gr336352."""
    key = "rot_deg" if d.get("rot_deg") is not None else "rot"
    return parse_angle_deg(d.get(key), arg_name=key, bare="degrees")


def _jsonb_or_none(value: Any) -> Jsonb | None:
    """psycopg Jsonb for a nullable JSONB column."""
    return Jsonb(value) if value is not None else None


def _first_present(d: dict[str, Any], *keys: str) -> Any:
    """The first of ``keys`` actually present (and not None) in ``d``, else
    ``None`` — the alias resolver for authoring fields whose ZERO is
    meaningful.

    The older ``d.get(a) or d.get(b)`` idiom elsewhere in this module is
    safe only where 0 and NULL mean the same thing (a 0 A current estimate
    and no estimate both fall through to the fab floor). It is wrong for a
    net deliberately annotated ``working_voltage_v: 0`` — a shield tied to
    chassis — which must stay 0 so the pairwise ``|V_a - V_b|`` term sees a
    real potential difference, not an unannotated net."""
    for key in keys:
        value = d.get(key)
        if value is not None:
            return value
    return None


#: pcb-ewod-multitile Slice 1's per-pad authoring vocabulary — the SAME
#: three questions ``docs/backlog/pcb-component-model.md`` §Features
#: drafted (role/mask/paste), kept as a flat allowlist here rather than a
#: DB CHECK constraint so a bad value fails at `put` time with the
#: offending pad's own pin name in the message, not a bare constraint
#: name from Postgres.
_LOCAL_PAD_ROLES = ("solderable", "electrode", "probe")
_LOCAL_PAD_MASKS = ("open", "covered")
_LOCAL_PAD_PASTES = ("none", "full")
_LOCAL_PAD_SHAPES = ("circle", "rect", "obround", "polygon")


def _normalize_local_footprint_pad(p: dict[str, Any]) -> dict[str, Any]:
    """One authored footprint pad -> the exact dict
    :func:`precis.pcb.padplace.place_footprint_pads` already consumes for
    an EasyEDA-cached one (``number``/``shape``/``x``/``y``/``w``/``h``/
    ``rot``/``layer``/``drill``), plus this slice's ``role``/``mask``/
    ``paste`` intent and, for ``shape: "polygon"``, the vertex ``poly``
    ring (footprint-local mm — same frame as ``x``/``y`` on every other
    shape).

    ``paste`` defaults to ``"none"`` for an ``electrode`` pad and
    ``"full"`` otherwise when the author omits it — resolved HERE, once,
    so every downstream reader (:mod:`precis.pcb.padplace`,
    :mod:`precis.pcb.gerber`) can treat the field as always-explicit
    rather than re-deriving the same default (acceptance criterion 3: "no
    paste on any role: electrode pad").
    """
    pin_name = str(p.get("pin") or p.get("number") or "").strip()
    if not pin_name:
        raise ValueError("pcb footprint pad needs a 'pin' name")
    shape = str(p.get("shape") or "rect").strip().lower()
    if shape not in _LOCAL_PAD_SHAPES:
        raise ValueError(
            f"pcb footprint pad {pin_name!r}: shape must be one of "
            f"{_LOCAL_PAD_SHAPES}, got {shape!r}"
        )
    role = str(p.get("role") or "solderable").strip().lower()
    if role not in _LOCAL_PAD_ROLES:
        raise ValueError(
            f"pcb footprint pad {pin_name!r}: role must be one of "
            f"{_LOCAL_PAD_ROLES}, got {role!r}"
        )
    mask = str(p.get("mask") or "open").strip().lower()
    if mask not in _LOCAL_PAD_MASKS:
        raise ValueError(
            f"pcb footprint pad {pin_name!r}: mask must be one of "
            f"{_LOCAL_PAD_MASKS}, got {mask!r}"
        )
    paste_raw = p.get("paste")
    paste = (
        str(paste_raw).strip().lower()
        if paste_raw is not None
        else ("none" if role == "electrode" else "full")
    )
    if paste not in _LOCAL_PAD_PASTES:
        raise ValueError(
            f"pcb footprint pad {pin_name!r}: paste must be one of "
            f"{_LOCAL_PAD_PASTES}, got {paste!r}"
        )

    pad: dict[str, Any] = {
        "number": pin_name,
        "shape": shape,
        "layer": str(p.get("layer") or "F.Cu"),
        "rot": _rot_deg(p),
        "role": role,
        "mask": mask,
        "paste": paste,
    }
    if p.get("drill") is not None:
        pad["drill"] = float(p["drill"])
        # A slotted hole (EasyEDA holeType SLOT, a USB-C shield tab) is a
        # footprint-local ``{w, h}`` extent beside the round cutter
        # diameter in ``drill`` — the shape
        # :func:`precis.pcb.padplace._slot_centre_line` reads. Dropping it
        # here stored the slot as a plain round hole of its short axis.
        slot = p.get("slot")
        if slot is not None:
            if not isinstance(slot, dict) or not all(
                isinstance(slot.get(k), (int, float))
                and math.isfinite(float(slot[k]))
                and float(slot[k]) > 0
                for k in ("w", "h")
            ):
                raise ValueError(
                    f"pcb footprint pad {pin_name!r}: slot must be "
                    "{w, h} with positive finite values"
                )
            pad["slot"] = {"w": float(slot["w"]), "h": float(slot["h"])}
    if shape == "polygon":
        poly = p.get("poly") or []
        if len(poly) < 3:
            raise ValueError(
                f"pcb footprint pad {pin_name!r}: shape='polygon' needs "
                ">=3 'poly' vertices"
            )
        verts = [(float(v[0]), float(v[1])) for v in poly]
        xs, ys = [v[0] for v in verts], [v[1] for v in verts]
        # x/y still carry the bbox CENTER (not a spare field): every
        # downstream reader that only knows circle/rect pads — the
        # courtyard/DRC bbox fallback, an SVG viewBox scan — reads
        # `pad["x"]`/`["y"]` unconditionally, so a polygon pad answers
        # that question too, even though `poly` is its real shape.
        pad["x"] = round((min(xs) + max(xs)) / 2.0, 4)
        pad["y"] = round((min(ys) + max(ys)) / 2.0, 4)
        pad["w"] = round(max(xs) - min(xs), 4) or 0.01
        pad["h"] = round(max(ys) - min(ys), 4) or 0.01
        pad["poly"] = [[round(vx, 4), round(vy, 4)] for vx, vy in verts]
    else:
        if p.get("x") is None or p.get("y") is None:
            raise ValueError(f"pcb footprint pad {pin_name!r} needs x/y")
        w = float(p.get("w") or 0.0)
        if w <= 0:
            raise ValueError(f"pcb footprint pad {pin_name!r} needs a positive w")
        pad["x"] = round(float(p["x"]), 4)
        pad["y"] = round(float(p["y"]), 4)
        pad["w"] = round(w, 4)
        pad["h"] = round(float(p.get("h") or w), 4)
    return pad


#: The keys a ``footprints[]`` entry may carry. Anything else raises: a
#: key outside this set used to be dropped without a word (an imported
#: footprint's ``holes`` among them), which is how geometry went missing.
_LOCAL_FOOTPRINT_KEYS = (
    "name",
    "pads",
    "pin_map",
    "courtyard",
    "centroid",
    "note",
    "holes",
)


def _normalize_footprint_holes(name: str, raw: Any) -> list[dict[str, float]]:
    """Footprint-local non-plated holes ``[{x, y, dia_mm}]``, validated
    (numeric, finite, ``dia_mm > 0``) and rounded like the pads."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise BadInput(f"pcb footprint {name!r}: holes must be a list of objects")
    out: list[dict[str, float]] = []
    for i, h in enumerate(raw):
        if not isinstance(h, dict):
            raise BadInput(
                f"pcb footprint {name!r}: holes[{i}] must be an object {{x, y, dia_mm}}"
            )
        extra = sorted(set(h) - {"x", "y", "dia_mm"})
        if extra:
            raise BadInput(
                f"pcb footprint {name!r}: holes[{i}] has unknown key(s) "
                f"{extra}; accepted: ['dia_mm', 'x', 'y']"
            )
        try:
            x, y, dia = float(h["x"]), float(h["y"]), float(h["dia_mm"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BadInput(
                f"pcb footprint {name!r}: holes[{i}] needs numeric x, y, dia_mm"
            ) from exc
        if not all(math.isfinite(v) for v in (x, y, dia)) or dia <= 0:
            raise BadInput(
                f"pcb footprint {name!r}: holes[{i}] needs finite x/y and dia_mm > 0"
            )
        out.append({"x": round(x, 4), "y": round(y, 4), "dia_mm": round(dia, 4)})
    return out


def _normalize_local_footprint(f: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One ``footprints[]`` batch entry -> ``(name, data)`` where ``data``
    is the same ``{pads, pin_map, courtyard, centroid}`` row shape
    ``part_footprints`` uses (:meth:`PcbMixin.pcb_footprints_for`'s own
    return shape) — so every reader downstream of either source (padplace/
    realize/gerber) takes one dict shape, not two.

    ``pin_map`` is identity by default (pad number == pin name —
    :func:`_normalize_local_footprint_pad` already made ``number`` the pin
    name), matching :mod:`precis.pcb.easyeda`'s own construction. An
    authored ``pin_map`` is honoured when given, which is what lets a
    SEMANTIC pin name reach real copper: ``padplace.pad_label`` resolves a
    netlist pin name through this map, so without it the only way to name
    a pad ``"VDD"`` was to call the *pad* that — destroying the pad number
    an EasyEDA export has to round-trip back out.

    Every key must be a real pad number, because the failure it prevents
    is silent: a typo'd or stale key would simply never match a pad, and
    the pin it meant to name would fall back to the pad number with
    nothing said. Pads the map omits keep their identity entry rather
    than dropping out, so a partial map names the pins it knows and
    leaves the rest numbered."""
    name = str(f.get("name") or "").strip()
    if not name:
        raise ValueError("pcb footprint needs a name")
    unknown = sorted(set(f) - set(_LOCAL_FOOTPRINT_KEYS))
    if unknown:
        raise BadInput(
            f"pcb footprint {name!r}: unknown key(s) {unknown}; "
            f"accepted: {sorted(_LOCAL_FOOTPRINT_KEYS)}"
        )
    raw_pads = f.get("pads") or []
    if not raw_pads:
        raise ValueError(f"pcb footprint {name!r} needs at least one pad")
    pads = [_normalize_local_footprint_pad(p) for p in raw_pads]
    pin_map = {pad["number"]: {"name": pad["number"], "tags": []} for pad in pads}
    for pad_num, entry in (f.get("pin_map") or {}).items():
        key = str(pad_num)
        if key not in pin_map:
            raise ValueError(
                f"pcb footprint {name!r}: pin_map key {key!r} is not a pad "
                f"number on this footprint (have {sorted(pin_map)})"
            )
        # Accept either the full ``{name, tags}`` row shape or the bare
        # pin name, so an importer that has only a name does not have to
        # know the row shape to use this at all.
        if isinstance(entry, str):
            pin_name, tags = entry.strip(), []
        else:
            pin_name = str((entry or {}).get("name") or "").strip()
            tags = list((entry or {}).get("tags") or [])
        if not pin_name:
            raise ValueError(
                f"pcb footprint {name!r}: pin_map[{key!r}] needs a pin name"
            )
        pin_map[key] = {"name": pin_name, "tags": tags}

    xs: list[float] = []
    ys: list[float] = []
    for pad in pads:
        if pad.get("poly"):
            xs += [v[0] for v in pad["poly"]]
            ys += [v[1] for v in pad["poly"]]
        else:
            xs += [pad["x"] - pad["w"] / 2.0, pad["x"] + pad["w"] / 2.0]
            ys += [pad["y"] - pad["h"] / 2.0, pad["y"] + pad["h"] / 2.0]
    courtyard = f.get("courtyard") or {
        "bbox": [
            round(min(xs), 4),
            round(min(ys), 4),
            round(max(xs), 4),
            round(max(ys), 4),
        ]
    }
    centroid = f.get("centroid") or {
        "x": round(sum(pad["x"] for pad in pads) / len(pads), 4),
        "y": round(sum(pad["y"] for pad in pads) / len(pads), 4),
    }
    data = {
        "pads": pads,
        "pin_map": pin_map,
        "courtyard": courtyard,
        "centroid": centroid,
        "note": f.get("note"),
        "holes": _normalize_footprint_holes(name, f.get("holes")),
    }
    return name, data


#: Sentinel default for :meth:`PcbMixin.pcb_move_instance`'s ``fixed=``
#: kwarg — lets a caller pass ``fixed=None`` to explicitly CLEAR the lock,
#: distinct from omitting the kwarg (leave the lock alone).
_UNSET: Any = object()

#: Pad-centre agreement for the local-vs-catalog footprint verdict
#: (:meth:`PcbMixin._pcb_footprint_refs_by_components`'s ``flag`` and the
#: .epro2 import's local-footprint dedup). The two sides were authored in
#: different tools (EasyEDA's footprint vs JLC's catalog cache), so a real
#: "same footprint" can sit a few hundredths of a mm apart; a mismatch
#: that actually matters (a different land pattern) is an order bigger.
_LOCAL_FP_MATCH_TOL_MM = 0.05


def pads_differ(local_pads: Any, catalog_pads: Any) -> str | None:
    """``None`` when two footprint pad lists place the same pads — same
    count and, sorted, every centre within ``_LOCAL_FP_MATCH_TOL_MM`` —
    else a short reason (``"pad count 3 vs 2"`` / ``"pad (1.20,0.30) off
    by 0.41mm"``) fit for a meta record. Position and count only: pad
    *names* legitimately differ (the catalog names pads after the part's
    pin map; an imported footprint keeps the source file's numbers), so
    this is deliberately not an equality check."""
    try:
        lp = sorted((float(p["x"]), float(p["y"])) for p in local_pads or [])
        cp = sorted((float(p["x"]), float(p["y"])) for p in catalog_pads or [])
    except (KeyError, TypeError, ValueError) as exc:
        return f"unreadable pads ({type(exc).__name__})"
    if len(lp) != len(cp):
        return f"pad count {len(lp)} vs catalog {len(cp)}"
    worst = 0.0
    for (ax, ay), (cx, cy) in zip(lp, cp):
        off = max(abs(ax - cx), abs(ay - cy))
        if off >= _LOCAL_FP_MATCH_TOL_MM:
            return (
                f"pad ({ax:.3f},{ay:.3f}) off by {off:.3f}mm "
                f"(catalog ({cx:.3f},{cy:.3f}), tol {_LOCAL_FP_MATCH_TOL_MM})"
            )
        worst = max(worst, off)
    return None if worst < _LOCAL_FP_MATCH_TOL_MM else "positions differ"


def footprint_differ(local: dict[str, Any], catalog: dict[str, Any]) -> str | None:
    """``None`` when a design-local footprint row is interchangeable with
    the catalog's cached one — the dedup verdict. Pads compare by count
    and position (:func:`pads_differ`); pin names compare per pad NUMBER,
    identity-filled — because dropping the local copy switches pin joins
    to the catalog's ``pin_map``, and a pad set that matches but names
    its pins differently would orphan the imported pins just the same."""
    why = pads_differ(local.get("pads"), catalog.get("pads"))
    if why is not None:
        return why

    def _names(pm: Any) -> dict[str, str]:
        out = {}
        for k, v in (pm or {}).items():
            if isinstance(v, dict):
                out[str(k)] = str(v.get("name") or k)
            else:
                out[str(k)] = str(v or k)
        return out

    nums = {str(p.get("number") or p.get("pin")) for p in local.get("pads") or []}
    cnums = {str(p.get("number") or p.get("pin")) for p in catalog.get("pads") or []}
    if nums != cnums:
        return f"pad numbers differ ({sorted(nums)} vs catalog {sorted(cnums)})"
    ln, cn = _names(local.get("pin_map")), _names(catalog.get("pin_map"))
    for num in sorted(nums):
        a, b = ln.get(num, num), cn.get(num, num)
        if a != b:
            return f"pin map: pad {num} is {a!r} locally, {b!r} in catalog"
    return None


#: pcb-pre-place-route-blocks Slice 1 — the fab process
#: :meth:`PcbMixin._pcb_fixed_copper_board_envelope`'s rule-envelope gate
#: compares a fixed-copper row against — pinned to ``4layer`` for the same
#: reason :data:`precis.pcb.generators._FAB_PROCESS` is (no per-board
#: process selection exists yet; lift both together once
#: ``put(stackup=...)`` authoring lands).
_FIXED_COPPER_FAB_PROCESS = "4layer"
#: envelope key -> :class:`precis.pcb.capabilities.CapabilityRow.jlc_min`
#: key. These, plus ``layers`` (from ``pcb_boards.stackup``, checked
#: separately), are the ONLY facts genuinely "on the board" today: the
#: board carries no authored clearance/track/via-size rule row anywhere in
#: the schema, so the gate compares against the same fixed capability
#: floor every generator already reads
#: (:func:`precis.pcb.generators.resolve_ewod_sizing`) rather than
#: inventing a second, unauthored source of truth. A row's envelope may
#: omit any of these — an omitted field is never checked.
_FIXED_COPPER_ENVELOPE_FIELDS = {
    "min_clearance_mm": "trace_spacing_mm",
    "min_track_mm": "trace_width_mm",
    "via_drill_mm": "drill_mm",
    "via_diameter_mm": "via_diameter_mm",
}


#: The connection of the open :meth:`PcbMixin.pcb_judged_tx`, if any. While
#: set, every pcb store read/write in the same context goes through it, so a
#: check run between a mutation and its commit sees the uncommitted rows (a
#: read on its own pool connection cannot) and a write never waits on row
#: locks this transaction itself holds.
_AMBIENT_CONN: contextvars.ContextVar[Connection | None] = contextvars.ContextVar(
    "pcb_ambient_conn", default=None
)


class PcbMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any
    retire_ref: Any  # RefsMixin — the shared ref soft-delete
    add_link: Any  # LinksMixin — the board → part ``contains`` edges
    remove_link: Any
    chunks: Any  # ChunkStore sub-store — the shared card_combined write

    # -- transaction-visible reads ----------------------------------------
    def _pcb_conn(self) -> AbstractContextManager[Connection]:
        """A connection for a pcb read: the ambient judged transaction's
        when one is open, else a fresh pool connection."""
        ambient = _AMBIENT_CONN.get()
        if ambient is not None:
            return nullcontext(ambient)
        return self.pool.connection()

    @contextmanager
    def _pcb_tx(self) -> Iterator[Connection]:
        """A transaction for a pcb write: a savepoint on the ambient judged
        transaction when one is open (its rollback undoes only this call;
        the enclosing transaction still commits or rolls back as a unit),
        else a fresh :meth:`tx`."""
        ambient = _AMBIENT_CONN.get()
        if ambient is not None:
            with ambient.transaction():
                yield ambient
            return
        with self.tx() as conn:
            yield conn

    @contextmanager
    def pcb_judged_tx(self) -> Iterator[Connection]:
        """One transaction in which every pcb store call (including reads)
        shares the yielded connection. A handler mutates inside it, re-reads
        the board to judge the result, and raises to roll the mutation back.
        Not re-entrant: an inner call joins the outer transaction."""
        ambient = _AMBIENT_CONN.get()
        if ambient is not None:
            yield ambient
            return
        with self.tx() as conn:
            token = _AMBIENT_CONN.set(conn)
            try:
                yield conn
            finally:
                _AMBIENT_CONN.reset(token)

    @contextmanager
    def pcb_view_tx(self) -> Iterator[Connection]:
        """Read-only repeatable view; no authoring ensure-board or cache locks."""
        if _AMBIENT_CONN.get() is not None:
            raise ValueError("viewer transaction must own its connection")
        with self.tx() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            token = _AMBIENT_CONN.set(conn)
            try:
                yield conn
            finally:
                _AMBIENT_CONN.reset(token)

    @contextmanager
    def pcb_guarded_tx(
        self,
        ref_id: int,
        slug: str,
        *,
        conv_id: int | None = None,
        conv_slug: str | None = None,
    ) -> Iterator[Connection]:
        """Short guard for a single stored move, never a model/network/GET.

        Cache table SHARE NOWAIT locks protect missing keys too, acquired parts
        then part_footprints before ordered FOR UPDATE refs/bindings/parents and
        captured leaves, including retired rows. FK key-share checks then block
        new children. READ COMMITTED capture after these locks observes children
        which committed while the guard waited; a prior repeatable snapshot would
        miss them. No advisory board lock or retry coordinates other writers.

        Statement/lock/idle limits are 1500/500/2000ms; application residence
        deadline is 2s, checked before commit. Deadlock/timeout/refusal rolls back.
        This does not preempt Python CPU: oversized or collateral moves refuse,
        and callers record measured residence rather than infer a time guarantee.
        """
        from precis.pcb.snapshot import IDENTITIES, TABLES

        if _AMBIENT_CONN.get() is not None:
            raise ValueError("guard must own its transaction")
        with self.tx() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            conn.execute("SET LOCAL lock_timeout='500ms'")
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout='2000ms'")
            started = time.monotonic()
            conn.execute("LOCK TABLE parts IN SHARE MODE NOWAIT")
            conn.execute("LOCK TABLE part_footprints IN SHARE MODE NOWAIT")
            ids = sorted({r for r in (ref_id, conv_id) if r is not None})
            conn.execute(
                "SELECT ref_id FROM refs WHERE ref_id=ANY(%s) ORDER BY ref_id FOR UPDATE",
                (ids,),
            ).fetchall()
            bindings = {slug: ref_id}
            if conv_id is not None and conv_slug is not None:
                bindings[conv_slug] = conv_id
            rows = conn.execute(
                "SELECT id_value,ref_id FROM ref_identifiers WHERE id_kind='cite_key' AND id_value=ANY(%s) ORDER BY id_value FOR UPDATE",
                (sorted(bindings),),
            ).fetchall()
            if (
                dict(rows) != bindings
                or self.get_ref(kind="pcb", id=ref_id, conn=conn) is None
            ):
                raise ValueError("design or conversation binding changed; reload")
            # Dependency order, not replay's ID-allocation order.
            order: tuple[str, ...] = (
                "pcb_boards",
                "pcb_components",
                "pcb_nets",
                "pcb_instances",
                "pcb_pins",
            )
            order += tuple(
                t for t in TABLES if t not in {*order, "parts", "part_footprints"}
            )
            for table in order:
                if table == "pcb_pins":
                    where = "component_id IN (SELECT component_id FROM pcb_components WHERE ref_id=%s)"
                elif table == "pcb_netconns":
                    where = "net_id IN (SELECT net_id FROM pcb_nets WHERE ref_id=%s)"
                elif table in {
                    "pcb_routes",
                    "pcb_planes",
                    "pcb_pin_swaps",
                    "pcb_copper",
                }:
                    where = (
                        "board_id IN (SELECT board_id FROM pcb_boards WHERE ref_id=%s)"
                    )
                else:
                    where = "ref_id=%s"
                key = IDENTITIES.get(table, "name")
                conn.execute(
                    f"SELECT {key} FROM {table} WHERE {where} ORDER BY {key} FOR UPDATE",
                    (ref_id,),
                ).fetchall()
            if conv_id is not None:
                conn.execute(
                    "SELECT chunk_id FROM chunks WHERE ref_id=%s ORDER BY ord,chunk_id FOR UPDATE",
                    (conv_id,),
                ).fetchall()
                conn.execute(
                    "SELECT link_id FROM links WHERE src_ref_id=%s AND relation='related-to' ORDER BY link_id FOR UPDATE",
                    (conv_id,),
                ).fetchall()
            for table in ("design_checkpoints", "design_revisions"):
                conn.execute(
                    f"SELECT id FROM {table} WHERE ref_id=%s ORDER BY id FOR UPDATE",
                    (ref_id,),
                ).fetchall()
            token = _AMBIENT_CONN.set(conn)
            try:
                yield conn
                if time.monotonic() - started >= 2.0:
                    raise ValueError("move exceeded the 2s guard deadline; deferred")
            finally:
                _AMBIENT_CONN.reset(token)

    # -- write ----------------------------------------------------------
    def pcb_apply(
        self,
        *,
        slug: str,
        title: str,
        components: list[dict[str, Any]],
        nets: list[dict[str, Any]],
        connections: list[dict[str, Any]],
        measures: list[dict[str, Any]] | None = None,
        features: list[dict[str, Any]] | None = None,
        footprints: list[dict[str, Any]] | None = None,
        generators: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
        conn: Connection | None = None,
        seeded: list[str] | None = None,
    ) -> tuple[Any, bool, dict[str, int]]:
        """Create-or-extend a design, batch. Returns ``(ref, created,
        counts)``.

        Each *component* dict creates a component TYPE + pins + **one**
        instance (1:1 convenience): keys ``refdes`` (req), ``label``,
        ``part``/``part_lcsc``, ``footprint``, ``courtyard``, ``centroid``,
        ``height_mm``, ``x``, ``y``, ``rot``, ``layer``, ``fixed``,
        ``roles``, ``note``, ``pins`` (``[{name, pad?, tags?,
        description?, note?}]``). Placement-constraint keys ride the
        instance's ``meta``: ``group``/``group_offset``,
        ``pattern``/``pattern_instance``, ``edge_mate``, and the soft pin
        ``max_travel_mm`` (mm a placer may move the part from its stored
        pose; rotation held). A *net* dict: ``name`` (req),
        ``net_class``/``class``, ``est_current_a``/``current``,
        ``width_mm``/``width``, ``note``, and the 0171 spec annotations
        ``working_voltage_v``/``voltage``, ``edge_rate_v_per_ns``/
        ``edge_rate``, ``impedance_ohm``/``impedance``,
        ``function_hint``/``function`` — all of which a re-``put`` PATCHES
        onto an existing net (presence-based, see :meth:`_pcb_patch_net`).
        ``domain`` alone is insert-only. A *connection* dict: ``net``
        (req), ``refdes`` (req), ``pin`` (req), ``note``. A *footprint*
        dict (pcb-ewod-multitile Slice 1 — authored copper with no LCSC
        part): ``name`` (req, the join key a component's own
        ``footprint`` field then names), ``pads`` (req, ``[{pin, shape:
        'circle'|'rect'|'obround'|'polygon', x, y, w?, h?, poly?, role?,
        mask?, paste?, drill?}]`` — see :func:`_normalize_local_footprint`
        for the full per-field contract), optional ``pin_map``
        (``{pad_number: 'PIN'}`` or ``{pad_number: {name, tags?}}`` —
        identity for any pad it omits, and every key must be a real pad
        number), optional ``courtyard``/
        ``centroid``/``note``. Applied BEFORE components, so a component
        in the same batch may reference a footprint just authored.
        Re-runnable: existing refdes/net names reused; an existing
        footprint NAME is upserted (an author fixing a pad's geometry
        re-authors under the same name rather than orphaning it). A
        *generator* dict (pcb-ewod-multitile Slice 2 — a computed
        component): ``name`` (req, the design-local identity AND the
        emitted component's refdes), ``generator`` (req, e.g.
        ``'ewod_pad_array'``), ``params`` — expanded via
        :func:`precis.pcb.generators.expand` into components/nets/
        connections/footprints/features BEFORE any of the batch's own
        (so a generator's output threads through the exact same
        insert/dedup logic below). Idempotent: unchanged params (compared
        against the previous apply's stored row) is a no-op; changed
        params retires the previous expansion (component/instance/pins/
        nets/connections/its mask_open feature) and re-inserts fresh —
        see :meth:`_pcb_generator_retire_expansion`.

        ``seeded``, when given, is an out-list: every refdes an expansion
        emitted at its seed pose (a fresh generator apply, a
        ``regenerate``, or a changed expansion's NEW secondary that had no
        stored pose to carry forward) is appended, so the caller can
        legalise seeds before the always-valid gate reads the board.

        Reuses ``conn`` inside an existing transaction (e.g. bundling
        the ``net_classes`` upsert so both commit/rollback as one unit);
        opens its own otherwise (mirrors :meth:`pcb_ensure_board`)."""
        if conn is not None:
            return self._pcb_apply(
                conn,
                slug=slug,
                title=title,
                components=components,
                nets=nets,
                connections=connections,
                measures=measures,
                features=features,
                footprints=footprints,
                generators=generators,
                meta=meta,
                seeded=seeded,
            )
        with self._pcb_tx() as c:
            return self._pcb_apply(
                c,
                slug=slug,
                title=title,
                components=components,
                nets=nets,
                connections=connections,
                measures=measures,
                features=features,
                footprints=footprints,
                generators=generators,
                meta=meta,
                seeded=seeded,
            )

    def _pcb_apply(
        self,
        conn: Connection,
        *,
        slug: str,
        title: str,
        components: list[dict[str, Any]],
        nets: list[dict[str, Any]],
        connections: list[dict[str, Any]],
        measures: list[dict[str, Any]] | None,
        features: list[dict[str, Any]] | None,
        footprints: list[dict[str, Any]] | None = None,
        generators: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
        seeded: list[str] | None = None,
    ) -> tuple[Any, bool, dict[str, int]]:
        """The body of :meth:`pcb_apply`, given an already-open ``conn``."""
        existing = self.get_ref(kind="pcb", id=slug)
        created = existing is None
        counts = {
            "components": 0,
            "pins": 0,
            "instances": 0,
            "nets": 0,
            "nets_patched": 0,
            "conns": 0,
            "measures": 0,
            "features": 0,
            "outline_replaced": 0,
            "footprints": 0,
            "generators": 0,
            "pins_completed": 0,
        }
        if created:
            ref = self.insert_ref(
                kind="pcb",
                slug=slug,
                title=title,
                meta=dict(meta or {}),
                conn=conn,
            )
        else:
            ref = existing
            conn.execute(
                "UPDATE refs SET title = %s WHERE ref_id = %s",
                (title, ref.id),
            )
            if meta is not None:
                conn.execute(
                    "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
                    (Jsonb(dict(meta)), ref.id),
                )

        board_id = self.pcb_ensure_board(ref.id, conn=conn)

        # Generators land BEFORE footprints/components/nets/connections/
        # features: an unchanged-params generator is a no-op (nothing
        # merged into the batch below -- `features` in particular has no
        # dedup key of its own, so merging an unchanged generator's
        # mask_open feature every re-apply would duplicate it); a new-or-
        # changed one retires its previous expansion, then its emitted
        # components/nets/connections/footprints/features are merged into
        # THIS batch's own lists so every existing insert/dedup path
        # below (refdes-exists skip, net-name-exists skip, footprint
        # upsert) handles them identically to hand-authored rows.
        components = list(components)
        nets = list(nets)
        connections = list(connections)
        footprints = list(footprints or [])
        features = list(features or [])
        measures = list(measures or [])
        net_classes: dict[str, dict[str, Any]] = {}
        # pcb-pre-place-route-blocks Slice 1 -- an expansion's `copper`
        # (fixed track/via rows) can't be written until `nets` below has
        # been merged into `net_by_name` (a row's `net` is a NAME this
        # generator's OWN nets list just introduced), so accepted
        # expansions queue their copper here and `_pcb_fixed_copper_put`
        # runs once, after the nets loop, per queued generator.
        pending_copper: list[tuple[str, str, int, list[dict[str, Any]]]] = []
        for g in generators or []:
            gname = str(g.get("name") or "").strip()
            if not gname:
                raise ValueError("pcb generator needs a name")
            gtype = str(g.get("generator") or "").strip()
            if not gtype:
                raise ValueError(f"pcb generator {gname!r} needs a 'generator' type")
            expansion = pcb_generators.expand(gtype, gname, dict(g.get("params") or {}))
            existing_gen = self._pcb_generator_row(conn, ref.id, gname)
            # `regenerate` re-expands at current code even when generator,
            # version and params all match -- the escape for output that
            # changed without a version bump. Explicit only: it retires the
            # whole expansion, placement and routing with it.
            if (
                not g.get("regenerate")
                and existing_gen is not None
                and existing_gen["generator"] == expansion.generator
                and existing_gen["version"] == expansion.version
                and existing_gen["params"] == expansion.canonical_params
            ):
                continue  # unchanged -- no-op, nothing to merge/re-insert
            # gr481705 — an expansion's emitted x/y/rot is a SEED for
            # the placer, not placement truth (`_expand_ewod_pad_array`'s
            # own sink-centroid comment). A retire-and-reinsert that
            # drops the stored pose resets a placed part back to that
            # seed — which for an `ewod_pad_array` sink can be a pose
            # the always-valid gate must refuse (its centroid lands
            # pads on the array's own plaza vias). Placement is real
            # board state a re-apply carries forward; only the
            # explicit `regenerate` escape resets it (its documented
            # contract — "retires the whole expansion, placement and
            # routing with it").
            kept: dict[str, dict[str, Any]] = {}
            if existing_gen is not None:
                if not g.get("regenerate"):
                    kept = self._pcb_generator_poses(
                        conn, ref.id, existing_gen["refdes"], existing_gen["name"]
                    )
                self._pcb_generator_retire_expansion(
                    conn, ref.id, existing_gen["refdes"], existing_gen["name"]
                )
            for c in expansion.components:
                pose = kept.get(str(c.get("refdes") or ""))
                if pose is not None:
                    c.update(pose)
                elif seeded is not None:
                    # A component that kept no committed pose lands on the
                    # emitted seed — the caller (PcbHandler.put) may
                    # legalise it before the always-valid judge sees the
                    # board (pcb-global-plan-router slice 5b).
                    seeded.append(str(c.get("refdes") or ""))
            components += expansion.components
            nets += expansion.nets
            connections += expansion.connections
            footprints += expansion.footprints
            features += expansion.features
            # gr345859 §F — a generator's declared fabric invariants are
            # `pcb_measures` rows like any authored measure; merged here so
            # they land through the same insert path (and are retired by
            # `_pcb_generator_retire_expansion` on a changed re-apply, keyed
            # by meta.generator).
            measures += expansion.measures
            net_classes.update(expansion.net_classes)
            self._pcb_generator_upsert(
                conn,
                ref.id,
                gname,
                expansion.generator,
                expansion.version,
                expansion.canonical_params,
                expansion.refdes,
                expansion.ledger,
            )
            counts["generators"] += 1
            if expansion.copper:
                pending_copper.append(
                    (gname, expansion.generator, expansion.version, expansion.copper)
                )

        # Footprints land BEFORE components: a component's own
        # `footprint` field (below) is the join key an instance authored
        # in the SAME batch may already want to reference.
        for f in footprints or []:
            name, data = _normalize_local_footprint(f)
            self._pcb_local_footprint_upsert(conn, ref.id, name, data)
            counts["footprints"] += 1

        # refdes -> (instance_id, component_id); net name -> net_id
        inst_by_refdes = self._pcb_instance_map(conn, ref.id)
        net_by_name = self._pcb_net_map(conn, ref.id)

        # Canonicalise ``part``/``part_lcsc`` once per batch (gr485250): a
        # lowercase C-number or a pasted lcsc.com/jlcpcb.com product URL
        # resolves to its ``C<n>`` here so every downstream read
        # (``_pcb_insert_component``, ``_pcb_footprint_ref(s)``,
        # ``part_cache``) sees the same key. Non-C-number values pass
        # through untouched — ``part_lcsc`` is a LOOSE reference by design.
        from precis.pcb.catalog import lcsc_id

        for c in components:
            for field in ("part", "part_lcsc"):
                raw = c.get(field)
                if raw is not None and (canon := lcsc_id(raw)) is not None:
                    c[field] = canon

        # catalog snapshots for the whole batch up front (two ANY()
        # queries) — resolving inside the loop was 2 queries per component.
        part_cache = self._pcb_resolve_parts(
            conn,
            sorted(
                {
                    str(c.get("part_lcsc") or c.get("part")).strip().upper()
                    for c in components
                    if (c.get("part_lcsc") or c.get("part"))
                }
            ),
        )

        declarations: dict[str, list[dict[str, Any]]] = {}
        pending_completions: list[tuple[dict[str, Any], int, str]] = []
        for c in components:
            refdes = str(c.get("refdes") or "").strip()
            if not refdes:
                raise ValueError("pcb component needs a refdes")
            declarations.setdefault(refdes, []).extend(c.get("pins") or [])
            _validate_pin_declarations(
                declarations[refdes],
                refdes,
                self._pcb_footprint_pin_map(conn, ref.id, c, inst_by_refdes.get(refdes))
                if any(p.get("pad") is not None for p in declarations[refdes])
                else None,
            )
            if c.get("supplies") is not None:
                declared_names = {
                    str(p.get("name") or "").strip() for p in declarations[refdes]
                }
                c = {
                    **c,
                    "supplies": _normalize_supplies(
                        c["supplies"], refdes, declared_names or None
                    ),
                }
            if refdes in inst_by_refdes:
                comp_id = inst_by_refdes[refdes][1]
            else:
                comp_id = self._pcb_insert_component(conn, ref.id, c, part_cache)
                counts["components"] += 1
                counts["pins"] += self._pcb_insert_pins(
                    conn, comp_id, c.get("pins") or [], refdes=refdes
                )
                inst_id = self._pcb_insert_instance(
                    conn, ref.id, board_id, comp_id, refdes, c
                )
                counts["instances"] += 1
                inst_by_refdes[refdes] = (inst_id, comp_id)
            pending_completions.append((c, comp_id, refdes))

        # Declare-all completion (Slice 2): every pad the cached footprint
        # names becomes a pin, whether the author wired it or not — and on
        # EVERY pass, not only first insert, so a re-apply after a late
        # ``op='footprint'`` completes a part that was placed before its
        # footprint arrived. The pin-name sets and the footprint rows are
        # each fetched for ALL pending components in one batched query,
        # not per component (review of the first slice-2 pass: N+1).
        if pending_completions:
            names_by_comp = self._pcb_pin_names_by_components(
                conn, [comp_id for _c, comp_id, _r in pending_completions]
            )
            fps_by_comp, fp_flags, fp_diffs = self._pcb_footprint_refs_by_components(
                conn, ref.id, pending_completions
            )
            for _c, comp_id, refdes in pending_completions:
                counts["pins_completed"] += self._pcb_complete_footprint_pins(
                    conn,
                    comp_id,
                    refdes,
                    fp=fps_by_comp.get(comp_id),
                    names=names_by_comp.get(comp_id, (set(), set())),
                )
                inst = inst_by_refdes.get(refdes)
                if inst is None:
                    continue
                # The local-vs-catalog footprint verdict — the record of
                # WHY a kept local footprint is special (the .epro2 import
                # drops matching copies outright). Rewritten on every
                # apply so a late-landing catalog row turns "uncached"
                # into "match"/"differ", and a component that lost its
                # local footprint drops the flag.
                flag = fp_flags.get(comp_id)
                patch = (
                    {"local_fp_lcsc": flag, "local_fp_diff": fp_diffs[comp_id]}
                    if flag == "differ"
                    else {"local_fp_lcsc": flag}
                    if flag is not None
                    else None
                )
                conn.execute(
                    "UPDATE pcb_instances "
                    "SET meta = meta - 'local_fp_lcsc' - 'local_fp_diff' "
                    + ("|| %s " if patch is not None else "")
                    + "WHERE instance_id = %s",
                    (((Jsonb(patch), inst[0])) if patch is not None else (inst[0],)),
                )

        for n in nets:
            name = str(n.get("name") or "").strip()
            if not name:
                raise ValueError("pcb net needs a name (meaningful)")
            if name in net_by_name:
                # An existing net is reused, not duplicated (this method's
                # own re-runnable contract) — but every patchable column IS
                # updated here, because the workflow that needs them
                # annotates nets that already exist: a board is authored (or
                # imported) first, and only then does someone read the
                # datasheet and say "this rail runs at 48 V, at 2 A".
                # Presence-based, so a caller that passes none of them
                # (every caller before 0171) is a no-op exactly as before.
                # 0171 scoped this to its own four columns so one commit
                # could not both add them and change what the older four
                # did; gr457053 is that second step.
                if self._pcb_patch_net(conn, net_by_name[name], n):
                    counts["nets_patched"] += 1
                continue
            net_by_name[name] = self._pcb_insert_net(conn, ref.id, n)
            counts["nets"] += 1

        # pcb-pre-place-route-blocks Slice 1 -- runs AFTER nets so a
        # generator's own copper rows (named nets it just introduced above)
        # resolve; `_pcb_fixed_copper_put` re-derives the active net map
        # itself rather than threading `net_by_name` through, so it sees
        # every net inserted above regardless of authoring order. An
        # envelope mismatch raises here, aborting this WHOLE apply (same
        # transaction as everything else in this method) -- nothing
        # partially lands.
        for gname, gtype, gversion, copper_rows in pending_copper:
            self._pcb_fixed_copper_put(
                conn, ref.id, board_id, gname, gtype, str(gversion), copper_rows
            )

        for k in connections:
            counts["conns"] += self._pcb_connect(
                conn, ref.id, k, inst_by_refdes, net_by_name
            )

        for mm in measures or []:
            self._pcb_insert_measure(conn, ref.id, mm)
            counts["measures"] += 1

        for ft in features or []:
            if str(ft.get("ftype") or ft.get("type") or "").strip() == "outline":
                # A board has ONE outline (gr464529): a new one retires the
                # old, the same retired_at path every other feature removal
                # uses, so `_outline_from_features` never picks between two.
                counts["outline_replaced"] += conn.execute(
                    "UPDATE pcb_features SET retired_at = now() "
                    "WHERE ref_id = %s AND ftype = 'outline' "
                    "AND retired_at IS NULL",
                    (ref.id,),
                ).rowcount
            self._pcb_insert_feature(conn, ref.id, board_id, ft)
            counts["features"] += 1

        if net_classes:
            # A generator's own net-class rules (e.g. ewod_pad_array's
            # dedicated electrode-gap clearance floor) -- upserted in the
            # SAME transaction as the rest of this apply, not a second
            # round-trip the caller has to remember to make. Idempotent
            # (`_pcb_upsert_net_classes` is an upsert keyed by name), so a
            # no-op generator re-apply (which never reaches this point --
            # see the `continue` above) simply leaves the previous row in
            # place, and a changed-params re-apply overwrites it with the
            # new gap.
            self._pcb_upsert_net_classes(conn, ref.id, net_classes)

        self.chunks._replace_card_combined(
            conn,
            ref_id=ref.id,
            card_text=self._pcb_card_text(conn, ref.id, title),
        )
        self._pcb_reconcile_part_edges(conn, ref.id)
        return ref, created, counts

    def _pcb_reconcile_part_edges(
        self, conn: Connection, ref_id: int, *, dry_run: bool = False
    ) -> dict[str, int]:
        """Make the board's ``contains`` → part edges match its live parts.

        One edge per (board, part) with ``links.meta = {refdes: [...], qty}``;
        a part no longer placed loses its edge; part refs are minted on
        first use (:meth:`ensure_part_ref`). A C-number outside the catalog
        that already has a ref (one the ``datasheet_pull`` job minted with
        ``uncatalogued_ok``, to record a failed pull) gets its edge like any
        part; one with no ref gets none (``uncatalogued``). Only changed edges are
        written, so an unchanged re-apply is one read. ``dry_run`` counts
        what would change and writes nothing (``precis.handlers.part``).
        """
        stats = {"mint": 0, "add": 0, "update": 0, "remove": 0, "uncatalogued": 0}
        placed = conn.execute(
            "SELECT upper(btrim(c.part_lcsc)), "
            "       array_agg(i.refdes ORDER BY i.refdes) "
            "FROM pcb_instances i "
            "JOIN pcb_components c ON c.component_id = i.component_id "
            "WHERE i.ref_id = %s AND i.retired_at IS NULL "
            "  AND btrim(coalesce(c.part_lcsc, '')) <> '' "
            "GROUP BY 1",
            (ref_id,),
        ).fetchall()
        current = {
            int(r[0]): dict(r[1] or {})
            for r in conn.execute(
                "SELECT l.dst_ref_id, l.meta FROM links l "
                "JOIN refs r ON r.ref_id = l.dst_ref_id "
                "WHERE l.src_ref_id = %s AND l.relation = 'contains' "
                "  AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL "
                "  AND r.kind = 'part'",
                (ref_id,),
            ).fetchall()
        }
        wanted: dict[int, dict[str, Any]] = {}
        for lcsc, refdes in placed:
            edge = {"refdes": list(refdes), "qty": len(refdes)}
            part_id = self.part_ref_id(lcsc, conn=conn)
            if part_id is None:
                if dry_run:
                    in_catalog = conn.execute(
                        "SELECT 1 FROM parts WHERE lcsc = %s", (lcsc,)
                    ).fetchone()
                    stats["mint" if in_catalog else "uncatalogued"] += 1
                    stats["add"] += 1 if in_catalog else 0
                    continue
                try:
                    part_id = self.ensure_part_ref(lcsc, set_by="system", conn=conn)
                except NotFound:
                    stats["uncatalogued"] += 1
                    continue
                stats["mint"] += 1
            wanted[part_id] = edge
        for part_id, edge in wanted.items():
            have = current.get(part_id)
            if have is not None and {k: have.get(k) for k in edge} == edge:
                continue
            stats["update" if have is not None else "add"] += 1
            if not dry_run:
                self.add_link(
                    src_ref_id=ref_id,
                    dst_ref_id=part_id,
                    relation="contains",
                    set_by="system",
                    meta=edge,
                    merge_meta=True,
                    conn=conn,
                )
        for part_id in current.keys() - wanted.keys():
            stats["remove"] += 1
            if not dry_run:
                self.remove_link(
                    src_ref_id=ref_id,
                    dst_ref_id=part_id,
                    relation="contains",
                    conn=conn,
                )
        return stats

    def pcb_link_parts(self, *, dry_run: bool = True) -> dict[str, int]:
        """Run :meth:`_pcb_reconcile_part_edges` over every live board (the
        ``precis pcb link-parts`` backfill). Idempotent; ``dry_run`` (the
        default) writes nothing. Returns summed stats plus ``boards``."""
        totals = {"boards": 0, "mint": 0, "add": 0, "update": 0, "remove": 0}
        totals["uncatalogued"] = 0
        with self._pcb_tx() as conn:
            boards = [
                int(r[0])
                for r in conn.execute(
                    "SELECT ref_id FROM refs WHERE kind = 'pcb' "
                    "AND retired_at IS NULL ORDER BY ref_id"
                ).fetchall()
            ]
            for board in boards:
                stats = self._pcb_reconcile_part_edges(conn, board, dry_run=dry_run)
                totals["boards"] += 1
                for k, v in stats.items():
                    totals[k] += v
        return totals

    def _pcb_card_text(self, conn: Connection, ref_id: int, title: str) -> str:
        """The one embeddable summary per design — built from the current graph
        (component labels + a few net names) so search lands on intent."""
        labels = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT label FROM pcb_components "
                "WHERE ref_id = %s AND retired_at IS NULL ORDER BY label",
                (ref_id,),
            ).fetchall()
        ]
        n_inst = conn.execute(
            "SELECT count(*) FROM pcb_instances "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        nets = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM pcb_nets WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY name LIMIT 8",
                (ref_id,),
            ).fetchall()
        ]
        n_parts = int(n_inst[0]) if n_inst else 0
        return (
            f"{title} (PCB design). {n_parts} parts: {', '.join(labels)}. "
            f"Nets: {', '.join(nets)}."
        )

    # -- write helpers --------------------------------------------------
    def _pcb_insert_component(
        self,
        conn: Connection,
        ref_id: int,
        c: dict[str, Any],
        part_cache: dict[str, dict[str, Any]],
    ) -> int:
        part_lcsc = c.get("part_lcsc") or c.get("part")
        footprint = c.get("footprint")
        height_mm = c.get("height_mm")
        courtyard = c.get("courtyard")
        # Auto-stamp from the catalog: if a C-number is given but
        # the snapshot fields are not, copy them from parts / part_footprints so
        # the design is self-contained even if the catalog later churns.
        if part_lcsc and (footprint is None or height_mm is None or courtyard is None):
            resolved = part_cache.get(str(part_lcsc).strip().upper())
            if resolved is not None:
                footprint = footprint or resolved.get("footprint")
                height_mm = (
                    height_mm if height_mm is not None else resolved.get("height_mm")
                )
                courtyard = (
                    courtyard if courtyard is not None else resolved.get("courtyard")
                )
        # `pin_swap_groups` (docs/backlog/pcb-ewod-multitile.md "Rulings
        # 2026-09-19" item 6) rides `meta` the same way `pcb_instances.meta`
        # already carries `group`/`pattern` — a caller-declared admissible
        # pin-swap set (a generator's sink instance, or a hand-authored
        # component in `put(kind='pcb')`), read back verbatim by
        # `pcb_graph` and hoisted onto the component's instance dict for
        # `pcb_route`'s job to resolve into real `pinswap.PinSwapGroup`s.
        # This module never interprets the admissible-set judgment itself
        # (same "never inferred" contract as `precis.pcb.pinswap`) — it
        # only stores and returns exactly what was authored.
        # `part_meta` is the import's catalog-identity blob — ``{value,
        # mpn, mfr, supplier, supplier_part}`` the .epro2 reader recovered
        # from the component's ATTRs (``epro._component_identity``). It is
        # *meta*, not columns: it documents what the source SAID, while
        # ``part_lcsc`` (its own column, joined into the catalog) is what
        # precis resolves on.
        # `supplies` (pcb-lazy-netlist-and-checks.md §1b) is the successor
        # and rides the same way (already normalised by `_pcb_apply`).
        component_meta = {
            k: c[k] for k in ("pin_swap_groups", "supplies") if c.get(k) is not None
        }
        if c.get("part_meta"):
            component_meta["part"] = dict(c["part_meta"])
        row = conn.execute(
            """
            INSERT INTO pcb_components
                (ref_id, label, part_lcsc, footprint, courtyard, centroid,
                 height_mm, note, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING component_id
            """,
            (
                ref_id,
                str(c.get("label") or c.get("refdes") or "part"),
                part_lcsc,
                footprint,
                _jsonb_or_none(courtyard),
                _jsonb_or_none(c.get("centroid")),
                height_mm,
                c.get("note"),
                Jsonb(component_meta),
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _pcb_footprint_ref(
        self,
        conn: Connection,
        ref_id: int,
        c: dict[str, Any],
        component_id: int | None,
    ) -> dict[str, Any] | None:
        """``{"pin_map", "pads"}`` of the footprint a component resolves
        to, by the same precedence readers use
        (``session.footprints_by_refdes``): the design-local footprint
        named by ``footprint`` FIRST when a ``pcb_local_footprints`` row
        carries it — the board's own authored geometry is ground truth;
        an imported .epro2 keeps its pads even though the component also
        has a ``part_lcsc`` (Reto 2026-10-14: "keep the board's own
        footprint where it really differs"). Only when no local row
        matches does the catalog part's cached footprint apply. An
        already-stored component contributes its stored part/footprint
        when the put omits them. ``None`` when no cached footprint exists
        — the ``undeclared_pad`` check's not-checked case."""
        part = c.get("part_lcsc") or c.get("part")
        name = c.get("footprint")
        if component_id is not None and not (part and name):
            row = conn.execute(
                "SELECT part_lcsc, footprint FROM pcb_components "
                "WHERE component_id = %s",
                (component_id,),
            ).fetchone()
            if row is not None:
                part = part or row[0]
                name = name or row[1]
        if name:
            row = conn.execute(
                "SELECT pin_map, pads FROM pcb_local_footprints "
                "WHERE ref_id = %s AND name = %s",
                (ref_id, str(name)),
            ).fetchone()
            if row is not None:
                return {"pin_map": row[0] or {}, "pads": row[1] or []}
        if part:
            row = conn.execute(
                "SELECT pin_map, pads FROM part_footprints WHERE lcsc = %s",
                (str(part).strip().upper(),),
            ).fetchone()
            if row is not None:
                return {"pin_map": row[0] or {}, "pads": row[1] or []}
        return None

    def _pcb_footprint_pin_map(
        self,
        conn: Connection,
        ref_id: int,
        c: dict[str, Any],
        existing: tuple[int, int] | None,
    ) -> dict[str, Any] | None:
        """The ``pin_map`` half of :meth:`_pcb_footprint_ref`, for callers
        (``_validate_pin_declarations``) that only need the name map."""
        fp = self._pcb_footprint_ref(
            conn, ref_id, c, existing[1] if existing is not None else None
        )
        return fp["pin_map"] if fp is not None else None

    def _pcb_pin_names_by_components(
        self, conn: Connection, component_ids: list[int]
    ) -> dict[int, tuple[set[str], set[str]]]:
        """``{component_id: (all pin names, ACTIVE pin names)}`` in ONE
        query for a whole apply batch — the dedupe material
        ``_pcb_complete_footprint_pins`` needs without an N+1.

        BOTH sets matter, and they are not interchangeable: the dedupe
        runs against ALL names — retired rows included — because a pin the
        author deliberately retired must NOT be silently un-retired by
        completion re-declaring its name (the ``ON CONFLICT DO NOTHING``
        partial index ignores retired rows, so without this the retire
        silently reverts on the next re-apply). The join-failure guard
        runs against ACTIVE names only — a retired name matching no pad
        label is a dead fact, not a live mismatch."""
        out: dict[int, tuple[set[str], set[str]]] = {}
        if not component_ids:
            return out
        for cid, name, retired in conn.execute(
            "SELECT component_id, name, (retired_at IS NOT NULL) "
            "FROM pcb_pins WHERE component_id = ANY(%s)",
            (component_ids,),
        ).fetchall():
            all_names, active = out.setdefault(int(cid), (set(), set()))
            all_names.add(str(name))
            if not retired:
                active.add(str(name))
        return out

    def _pcb_footprint_refs_by_components(
        self,
        conn: Connection,
        ref_id: int,
        pending: list[tuple[dict[str, Any], int, str]],
    ) -> tuple[dict[int, dict[str, Any]], dict[int, str], dict[int, str]]:
        """``({component_id: {"pin_map", "pads"}}, {component_id:
        verdict}, {component_id: diff reason})`` for a whole apply batch
        in ≤3 queries — the footprint half of declare-all completion's
        reads, so a 50-component put does not pay three point-lookups per
        instance. Same resolution precedence as :meth:`_pcb_footprint_ref`:
        the design-local footprint named by ``footprint`` first, else the
        catalog part's cached footprint; the component's stored
        part/footprint fill in when the put entry omits them.

        ``verdict`` records, per component that HAS both a ``part_lcsc``
        and a resolved local footprint, whether the local row matches the
        catalog's cached pads — ``"match"`` (same pad count, every pad
        centre within :data:`_LOCAL_FP_MATCH_TOL_MM`), ``"differ"`` (the
        reason lands in the third dict), or ``"uncached"`` (the C-number
        has no ``part_footprints`` row to compare against). ``pcb_apply``
        stamps them on the instance (``meta.local_fp_lcsc`` /
        ``meta.local_fp_diff``): the .epro2 import drops the local row
        outright on a match, so a *stored* local footprint with a
        ``"differ"`` verdict is the record of why it was kept."""
        if not pending:
            return {}, {}, {}
        comp_ids = [comp_id for _c, comp_id, _r in pending]
        stored: dict[int, tuple[Any, Any]] = {
            int(cid): (part, name)
            for cid, part, name in conn.execute(
                "SELECT component_id, part_lcsc, footprint FROM pcb_components "
                "WHERE component_id = ANY(%s)",
                (comp_ids,),
            ).fetchall()
        }
        parts: set[str] = set()
        fp_names: set[str] = set()
        effective: dict[int, tuple[Any, Any]] = {}
        for c, comp_id, _refdes in pending:
            part = c.get("part_lcsc") or c.get("part")
            name = c.get("footprint")
            s_part, s_name = stored.get(comp_id, (None, None))
            if not (part and name):
                part = part or s_part
                name = name or s_name
            if part:
                parts.add(str(part).strip().upper())
            if name:
                fp_names.add(str(name))
            effective[comp_id] = (part, name)
        by_lcsc: dict[str, dict[str, Any]] = {}
        if parts:
            for lcsc, pin_map, pads in conn.execute(
                "SELECT lcsc, pin_map, pads FROM part_footprints WHERE lcsc = ANY(%s)",
                (sorted(parts),),
            ).fetchall():
                by_lcsc[str(lcsc)] = {
                    "pin_map": pin_map or {},
                    "pads": pads or [],
                }
        by_name: dict[str, dict[str, Any]] = {}
        if fp_names:
            for name, pin_map, pads in conn.execute(
                "SELECT name, pin_map, pads FROM pcb_local_footprints "
                "WHERE ref_id = %s AND name = ANY(%s)",
                (ref_id, sorted(fp_names)),
            ).fetchall():
                by_name[str(name)] = {
                    "pin_map": pin_map or {},
                    "pads": pads or [],
                }
        out: dict[int, dict[str, Any]] = {}
        flags: dict[int, str] = {}
        diffs: dict[int, str] = {}
        for _c, comp_id, _r in pending:
            part, name = effective[comp_id]
            local = by_name.get(str(name)) if name is not None else None
            cat = by_lcsc.get(str(part).strip().upper()) if part is not None else None
            if local is not None:
                # Local wins whenever the instance has one — the ruling
                # behind part_lcsc finally being importable.
                out[comp_id] = local
                if part:
                    if cat is None:
                        flags[comp_id] = "uncached"
                    else:
                        why = footprint_differ(local, cat)
                        flags[comp_id] = "differ" if why else "match"
                        if why:
                            diffs[comp_id] = why
            elif cat is not None:
                out[comp_id] = cat
        return out, flags, diffs

    def _pcb_complete_footprint_pins(
        self,
        conn: Connection,
        component_id: int,
        refdes: str,
        *,
        fp: dict[str, Any] | None,
        names: tuple[set[str], set[str]],
    ) -> int:
        """Declare a pin for every pad the component's cached footprint
        names and the component did not declare — "a package should always
        have all the pins" (docs/backlog/pcb-lazy-netlist-and-checks.md
        Slice 2; Defect A of docs/backlog/ewod-controller-and-hv-supply.md,
        taken over by that slice).

        Without this, :func:`precis.pcb.realize.pads_for_ir`'s per-pin loop
        emits a pad only for declared pins, so a footprint land no pin
        claimed was invisible to ``check_clearance``, connectivity and the
        router's occupancy stamping — the gr339236/gr346744 blindness
        class. (Non-primary pads DO reach the model through
        :func:`precis.pcb.ir.footprint_pad_set`, but with no pin id: never
        a swap target, never a connectivity member, and un-nameable in a
        netlist.) A completed pin declares with no connection, so
        ``ir.from_graph`` lands it ``NO_NET`` and the maze's sentinel-owner
        scheme makes it a routing obstacle as before — geometry unchanged,
        only the pin exists now.

        The pin name is the ``pin_map`` name (the semantic one —
        ``pin_map["7"].name == "VDD"``), falling back to the pad NUMBER
        when the map carries no entry for that pad. The footprint's own
        ``pin_map`` tags ride along (a footprint-level ``nc`` tag reaches
        the pin, so a true no-connect stays out of ``unconnected_pin``);
        ``meta.completed_from_footprint`` marks the declaration's origin
        for a reader telling author intent from store completion. The
        insert is name-deduped by ``_pcb_insert_pins``'s
        ``ON CONFLICT DO NOTHING``: an author-declared pin is never
        overwritten, and a re-apply after ``op='footprint'`` cached the
        footprint late completes the declarations then — which is why this
        runs for existing components too, not only on first insert.

        ``names`` is ``(all pin names incl. retired, active pin names)``
        from :meth:`_pcb_pin_names_by_components` — see that docstring for
        why the dedupe must cover retired rows. **Join-failed instances
        are skipped, not completed**: when an ACTIVE declared pin name
        matches no footprint label, :func:`precis.pcb.ir.footprint_pad_set`
        skips the whole instance and ``pin_name_mismatch`` already reports
        the mismatched name. Completing on top would hand every footprint
        land a fresh netless pin while the one genuinely-wrong pin stays a
        synthesized bound — the same defect, buried under new noise. The
        author must fix the name; completion resumes on the next apply.

        ``fp`` is the component's resolved ``{"pin_map", "pads"}`` from
        :meth:`_pcb_footprint_refs_by_components` (``None`` = footprint
        not cached — the ``undeclared_pad`` check's not-checked case).

        Returns the number of pins actually inserted."""
        if fp is None:
            return 0
        pin_map = fp.get("pin_map") or {}
        all_names, active_names = names
        decls: list[dict[str, Any]] = []
        labels: dict[str, dict[str, Any]] = {}
        for pad in fp.get("pads") or []:
            if not str(pad.get("number") or "").strip():
                continue
            label = pcb_padplace.pad_label(pad, pin_map)
            if label and label not in labels:
                entry = pin_map.get(str(pad.get("number")))
                labels[label] = (
                    {"name": label, "tags": []}
                    if isinstance(entry, str)
                    else {
                        "name": label,
                        "tags": list((entry or {}).get("tags") or []),
                    }
                )
        if active_names - set(labels):
            return 0  # join-failed: see docstring
        for name, decl in labels.items():
            # A name two pads share (the HV507's duplicate non-channel
            # names) declares ONE pin; :func:`precis.pcb.ir.
            # footprint_pad_set` resolves BOTH pads to it by label, so the
            # second same-named pad is primary-sibling geometry, not an
            # undeclared pad. ``all_names`` (retired included) is the
            # dedupe set: re-declaring a name the author retired would
            # silently undo the retirement.
            if name in all_names:
                continue
            decls.append({**decl, "meta": {"completed_from_footprint": True}})
        return self._pcb_insert_pins(conn, component_id, decls, refdes=refdes)

    def _pcb_resolve_parts(
        self, conn: Connection, lcscs: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Footprint / height / courtyard snapshots for a batch of catalog
        C-numbers (parts.package + height, part_footprints.courtyard if
        cached) — two ``ANY()`` queries for the whole ``pcb_apply`` batch."""
        if not lcscs:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for r in conn.execute(
            "SELECT lcsc, package, height_mm FROM parts WHERE lcsc = ANY(%s)",
            (lcscs,),
        ).fetchall():
            out[str(r[0])] = {
                "footprint": r[1],
                "height_mm": r[2],
                "courtyard": None,
            }
        for r in conn.execute(
            "SELECT lcsc, courtyard FROM part_footprints WHERE lcsc = ANY(%s)",
            (lcscs,),
        ).fetchall():
            entry = out.get(str(r[0]))
            if entry is not None:
                entry["courtyard"] = r[1]
        return out

    def _pcb_insert_pins(
        self,
        conn: Connection,
        component_id: int,
        pins: list[dict[str, Any]],
        *,
        refdes: str | None = None,
    ) -> int:
        component = refdes or str(component_id)
        existing = [
            {"name": row[0], "pad": row[1]}
            for row in conn.execute(
                "SELECT name, pad FROM pcb_pins "
                "WHERE component_id = %s AND retired_at IS NULL",
                (component_id,),
            ).fetchall()
        ]
        _validate_pin_declarations(existing + pins, component)
        n = 0
        for p in pins:
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            # §1j intent markers (pcb-lazy-netlist-and-checks.md Slice 2):
            # ``meta`` passes through verbatim; the bool keys
            # ``unused``/``must_terminate``/``nc`` are the authored
            # shorthand for the same meta facts, and ``nc: true`` ALSO adds
            # the ``nc`` tag, which the existing tags vocabulary already
            # carries. ``nc`` is part-level (datasheet: no internal
            # connection), ``must_terminate`` part-level (must not float),
            # ``unused`` design-level (this board does not use it —
            # still full geometry, still matcher-available);
            # ``drc.check_unconnected_pins`` reads them back off the
            # graph's ``unconnected`` rows.
            tags = list(p.get("tags") or [])
            meta = dict(p.get("meta") or {})
            for marker in ("unused", "must_terminate", "nc"):
                if p.get(marker):
                    meta[marker] = True
            if meta.get("nc") and "nc" not in tags:
                tags.append("nc")
            # §1i pin semantics (pcb-lazy-netlist-and-checks.md Slice 5):
            # `etype` is the closed electrical-role vocabulary — it lands
            # in `tags` (with the markers) so footprint pin_map tags and
            # graph reads see one spelling. `max_voltage_v`, `vih`, `vil`
            # and `rail` are numeric/named bounds — they ride `meta`.
            etype = str(p.get("etype") or "").strip()
            if etype:
                if etype not in PIN_ETYPES:
                    raise ValueError(
                        f"component {component}: pin {name!r} etype "
                        f"{etype!r} is not in the ERC vocabulary "
                        f"{sorted(PIN_ETYPES)}"
                    )
                clash = [t for t in tags if t in PIN_ETYPES and t != etype]
                if clash:
                    raise ValueError(
                        f"component {component}: pin {name!r} declares "
                        f"etype={etype!r} but tags already carry {clash!r}"
                    )
                if etype not in tags:
                    tags.append(etype)
            for key in ("max_voltage_v", "vih", "vil", "rail"):
                if p.get(key) is not None:
                    meta[key] = p[key]
            # Slice 6: the pin is a client of a supply role.
            if p.get("client_of") is not None:
                meta["client_of"] = _normalize_client_of(
                    p["client_of"], component, name
                )
            try:
                # A DB pad conflict remains authoritative. Unwind its
                # savepoint before looking up the owner or translating it.
                with conn.transaction():
                    result = conn.execute(
                        """
                INSERT INTO pcb_pins
                    (component_id, pad, name, tags, description, note, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (component_id, name) WHERE retired_at IS NULL DO NOTHING
                """,
                        (
                            component_id,
                            p.get("pad"),
                            name,
                            tags,
                            p.get("description"),
                            p.get("note"),
                            Jsonb(meta),
                        ),
                    )
            except UniqueViolation as exc:
                if exc.diag.constraint_name != "pcb_pins_comp_pad_key":
                    raise
                owner = conn.execute(
                    "SELECT name FROM pcb_pins WHERE component_id = %s "
                    "AND pad = %s AND retired_at IS NULL",
                    (component_id, p.get("pad")),
                ).fetchone()
                raise ValueError(
                    f"component {component}: pad {p.get('pad')!r} is claimed by "
                    f"pins {owner[0] if owner else 'concurrent owner'!r} and {name!r}; "
                    "use one canonical name for this physical pad in pins and "
                    "connections, or correct the erroneous pad number"
                ) from exc
            n += result.rowcount
        return n

    #: ``op='pins'`` (docs/backlog/pcb-datasheet-review.md slice 2) — the
    #: pin-annotation dict's allowed keys. ``pad`` is deliberately absent:
    #: pad binding stays with the canonical-pad rule — this op edits a
    #: pin's SEMANTICS (what the ERC rules read), never its geometry, and
    #: ``tags``/``description``/``note`` stay authoring-time fields.
    _PIN_PATCH_KEYS = frozenset(
        {
            "name",
            "etype",
            "rail",
            "max_voltage_v",
            "vih",
            "vil",
            "max_current_a",
            "current_role",
            "nc",
            "unused",
            "must_terminate",
            "min_dielectric",
            "source",
        }
    )

    #: ``op='pins'`` keys that are numbers when present.
    _PIN_PATCH_NUMERIC = ("max_voltage_v", "vih", "vil", "max_current_a")

    #: ``op='pins'`` keys stored verbatim in ``pcb_pins.meta``.
    #: ``min_dielectric`` (slice 3) is the design-layer stand-in for the
    #: part fact's ``dielectric ≥`` — declared on the CONSUMER's pin
    #: (e.g. an IC's ``power_in``), ``drc.check_passive_rating`` binds it
    #: to every parsed capacitor on the pin's rail.
    _PIN_PATCH_META = (
        "rail",
        "max_voltage_v",
        "vih",
        "vil",
        "max_current_a",
        "current_role",
        "min_dielectric",
        "source",
    )

    def pcb_patch_pins(
        self,
        ref_id: int,
        refdes: str,
        pins: list[dict[str, Any]],
        *,
        conn: Connection | None = None,
    ) -> dict[str, Any]:
        """``op='pins'`` — patch pin SEMANTICS on one existing instance's
        pins (the design-layer annotation; docs/backlog/
        pcb-datasheet-review.md slice 2). The write a plain re-put
        deliberately never performs (:meth:`_pcb_apply`'s contract is
        create-or-extend, "not a pin editor") — this is that editor: it is
        how an imported or already-authored part gets the ``etype`` /
        ``rail`` / ``max_voltage_v`` / ``vih`` / ``vil`` /
        ``max_current_a`` / ``current_role`` / marker facts the ERC rules
        read, WITHOUT re-authoring the component.

        Every ``name`` must be one of the component's REAL pin names — a
        bad one refuses the whole call and lists the valid set (the pins
        table, not the footprint: a pin the design never declared is
        added by a re-put, not by annotation). Idempotent: a value equal
        to the stored one is reported ``unchanged``, not rewritten.
        ``source`` (who or what said so — a datasheet page, ``'import'``,
        a hand check) lands in the pin's meta; it is the origin marker the
        spec's verdict-input model consumes later. Explicit ``None`` (or
        ``false`` for the markers) CLEARS the annotation; an absent key
        leaves it alone. Returns ``{"refdes", "changed", "unchanged"}``."""
        if conn is not None:
            return self._pcb_patch_pins(conn, ref_id, refdes, pins)
        with self._pcb_tx() as c:
            return self._pcb_patch_pins(c, ref_id, refdes, pins)

    def _pcb_patch_pins(
        self,
        conn: Connection,
        ref_id: int,
        refdes: str,
        pins: list[dict[str, Any]],
    ) -> dict[str, Any]:
        inst = conn.execute(
            "SELECT component_id FROM pcb_instances "
            "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL",
            (ref_id, refdes),
        ).fetchone()
        if inst is None:
            raise NotFound(f"pcb {ref_id}: no live instance {refdes!r}")
        rows = conn.execute(
            "SELECT pin_id, name, tags, meta FROM pcb_pins "
            "WHERE component_id = %s AND retired_at IS NULL ORDER BY name",
            (int(inst[0]),),
        ).fetchall()
        by_name = {str(r[1]): r for r in rows}
        # Validate the WHOLE batch before writing — one bad name refuses
        # the call (same all-or-nothing discipline as _validate_pin_
        # declarations), and the error names the part's real pins.
        seen: set[str] = set()
        for p in pins:
            if not isinstance(p, dict):
                raise ValueError(
                    f"component {refdes}: a pins[] entry must be a dict, got {p!r}"
                )
            name = str(p.get("name") or "").strip()
            if not name:
                raise ValueError(f"component {refdes}: a pins[] entry needs a name")
            bad_keys = set(p) - self._PIN_PATCH_KEYS
            if bad_keys:
                raise ValueError(
                    f"component {refdes}: pin {name!r} carries field(s) "
                    f"{sorted(bad_keys)} — op='pins' annotates semantics "
                    f"({sorted(self._PIN_PATCH_KEYS)}); pad binding stays "
                    "with the canonical-pad rule"
                )
            if name in seen:
                raise ValueError(
                    f"component {refdes}: pin {name!r} annotated twice in "
                    "one call — give each pin once"
                )
            seen.add(name)
            if name not in by_name:
                raise ValueError(
                    f"component {refdes}: no pin {name!r} — the component's "
                    f"pins are {sorted(by_name)}"
                )
            etype = p.get("etype")
            if etype is not None and str(etype).strip() not in PIN_ETYPES:
                raise ValueError(
                    f"component {refdes}: pin {name!r} etype {etype!r} is "
                    f"not in the ERC vocabulary {sorted(PIN_ETYPES)}"
                )
            role = p.get("current_role")
            if role is not None and str(role).strip() not in PIN_CURRENT_ROLES:
                raise ValueError(
                    f"component {refdes}: pin {name!r} current_role "
                    f"{role!r} is not in {sorted(PIN_CURRENT_ROLES)}"
                )
            diel = p.get("min_dielectric")
            if (
                diel is not None
                and str(diel).strip().lower() not in CAP_DIELECTRIC_RANK
            ):
                raise ValueError(
                    f"component {refdes}: pin {name!r} min_dielectric "
                    f"{diel!r} is not a ranked dielectric class "
                    f"({sorted(CAP_DIELECTRIC_RANK)})"
                )
            for key in self._PIN_PATCH_NUMERIC:
                v = p.get(key)
                if v is not None and (
                    isinstance(v, bool) or not isinstance(v, int | float)
                ):
                    raise ValueError(
                        f"component {refdes}: pin {name!r} {key} must be a "
                        f"number, got {v!r}"
                    )
        changed: list[str] = []
        unchanged: list[str] = []
        for p in pins:
            name = str(p["name"]).strip()
            pin_id, _n, tags0, meta0 = by_name[name]
            tags = list(tags0 or [])
            meta = dict(meta0 or {})
            if "etype" in p:
                # An explicit re-annotation REPLACES the etype tag (the op
                # is the pin editor; a same-value one is just idempotent),
                # and `etype: null` clears it.
                tags = [t for t in tags if t not in PIN_ETYPES]
                if p["etype"] is not None:
                    tags.append(str(p["etype"]).strip())
            for marker in ("nc", "unused", "must_terminate"):
                if marker not in p:
                    continue
                if p[marker]:
                    meta[marker] = True
                else:
                    meta.pop(marker, None)
            if meta.get("nc") and "nc" not in tags:
                tags.append("nc")
            if not meta.get("nc") and "nc" in tags:
                tags.remove("nc")
            for key in self._PIN_PATCH_META:
                if key not in p:
                    continue
                if p[key] is None:
                    meta.pop(key, None)
                elif key == "min_dielectric":
                    # Canonicalised to the rank table's keyspace so the
                    # reader never case-folds.
                    meta[key] = str(p[key]).strip().lower()
                elif key in ("rail", "current_role", "source"):
                    meta[key] = str(p[key]).strip()
                else:
                    meta[key] = p[key]
            if sorted(tags) == sorted(list(tags0 or [])) and meta == dict(meta0 or {}):
                unchanged.append(name)
                continue
            conn.execute(
                "UPDATE pcb_pins SET tags = %s, meta = %s WHERE pin_id = %s",
                (tags, Jsonb(meta), int(pin_id)),
            )
            changed.append(name)
        return {"refdes": refdes, "changed": changed, "unchanged": unchanged}

    def _pcb_insert_instance(
        self,
        conn: Connection,
        ref_id: int,
        board_id: int,
        component_id: int,
        refdes: str,
        c: dict[str, Any],
    ) -> int:
        # Placement-constraint fields ride `meta` (jsonb) rather than
        # columns: `group`/`group_offset` (rigid super-footprint),
        # `pattern`/`pattern_instance` (repeated-tile identity) and
        # `max_travel_mm` (the soft pin) are read back verbatim by
        # `pcb_graph`, which hoists them onto the instance dict for
        # `precis.pcb.ir.from_graph` to parse.
        meta = {
            k: c[k]
            for k in (
                "group",
                "group_offset",
                "pattern",
                "pattern_instance",
                "edge_mate",
                "max_travel_mm",
            )
            if c.get(k) is not None
        }
        row = conn.execute(
            """
            INSERT INTO pcb_instances
                (ref_id, board_id, component_id, refdes, x, y, rot, layer,
                 fixed, roles, note, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING instance_id
            """,
            (
                ref_id,
                board_id,
                component_id,
                refdes,
                c.get("x"),
                c.get("y"),
                _rot_deg(c),
                str(c.get("layer") or "top"),
                c.get("fixed"),
                list(c.get("roles") or []),
                c.get("note"),
                Jsonb(meta),
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])

    #: ``nets[]`` authoring key (and its alias) -> ``pcb_nets`` column, for
    #: the four §E-1/annotation columns 0171 added. One table, so the
    #: insert and the patch below cannot drift apart on an alias.
    _NET_SPEC_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("working_voltage_v", ("working_voltage_v", "voltage")),
        ("edge_rate_v_per_ns", ("edge_rate_v_per_ns", "edge_rate")),
        ("impedance_ohm", ("impedance_ohm", "impedance")),
        ("function_hint", ("function_hint", "function")),
    )

    #: Everything a re-put may patch onto an EXISTING net: the 0171 columns
    #: above plus the four the net has carried since the beginning
    #: (gr457053). Kept as its own table rather than widening
    #: ``_NET_SPEC_FIELDS``, because that one also drives
    #: :meth:`_pcb_insert_net`'s column list, where these four are already
    #: named explicitly — folding them in would insert each twice.
    #: ``domain`` is deliberately NOT here: it is NOT NULL with a default,
    #: so a patch has no way to express "leave it alone" that differs from
    #: "set it to electrical" — and the handler refuses a non-electrical
    #: one at the door anyway (``_reject_non_electrical``), so there is no
    #: value to patch it TO that it does not already hold.
    _NET_PATCH_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
        *_NET_SPEC_FIELDS,
        ("net_class", ("net_class", "class")),
        ("est_current_a", ("est_current_a", "current")),
        ("width_mm", ("width_mm", "width")),
        ("note", ("note",)),
    )

    def _pcb_patch_net(self, conn: Connection, net_id: int, n: dict[str, Any]) -> bool:
        """Set whichever patchable columns ``n`` actually carries on an
        EXISTING net; no-op returning ``False`` when it carries none.
        Presence-based, never truthiness-based (``working_voltage_v: 0`` is
        a real annotation, ``note: ''`` a real erasure) and never a blanket
        overwrite (a caller patching only the voltage must not blank an edge
        rate someone else set).

        A generator re-expansion reaches here only for a net it declares
        that a foreign live instance also joined, which
        :meth:`_pcb_generator_retire_expansion` refuses to retire
        (gr451046). Patching it means the generator re-asserts its own
        ``net_class`` there — the same value a retire+re-insert would have
        written for a net it owned outright, so the shared case now agrees
        with the unshared one instead of quietly diverging from it."""
        sets: list[str] = []
        values: list[Any] = []
        for column, keys in self._NET_PATCH_FIELDS:
            value = _first_present(n, *keys)
            if value is None:
                continue
            sets.append(f"{column} = %s")
            values.append(value)
        # `rail`/`meta` merge into the existing jsonb (top-level keys win)
        # rather than replacing it — a rail annotation must not blank a
        # layer hint someone else set.
        meta_patch = dict(n.get("meta") or {})
        if n.get("rail") is not None:
            meta_patch["rail"] = str(n["rail"]).strip()
        if meta_patch:
            sets.append("meta = pcb_nets.meta || %s::jsonb")
            values.append(Jsonb(meta_patch))
        if not sets:
            return False
        values.append(net_id)
        conn.execute(
            f"UPDATE pcb_nets SET {', '.join(sets)} WHERE net_id = %s",
            tuple(values),
        )
        return True

    def _pcb_insert_net(self, conn: Connection, ref_id: int, n: dict[str, Any]) -> int:
        # Spec columns and their placeholders come off _NET_SPEC_FIELDS, not
        # a hand-typed list: a column order that disagreed with the value
        # tuple below would write the edge rate into the voltage column and
        # nothing would fail loudly.
        spec = [column for column, _keys in self._NET_SPEC_FIELDS]
        # `rail` (Slice 5 — the named supply rail a power net carries,
        # what `rail_crossing`/`no_decoupling` join on) and a caller's own
        # `meta` dict share the same jsonb column, `rail` spelled as a
        # first-class key so `nets: [{'name': '3V3', 'rail': '3v3'}]`
        # reads like the other net fields.
        meta = dict(n.get("meta") or {})
        if n.get("rail") is not None:
            meta["rail"] = str(n["rail"]).strip()
        row = conn.execute(
            f"""
            INSERT INTO pcb_nets
                (ref_id, name, net_class, est_current_a, width_mm, note, domain,
                 meta, {", ".join(spec)})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, {", ".join(["%s"] * len(spec))})
            RETURNING net_id
            """,
            (
                ref_id,
                str(n["name"]).strip(),
                n.get("net_class") or n.get("class"),
                n.get("est_current_a") or n.get("current"),
                n.get("width_mm") or n.get("width"),
                n.get("note"),
                str(n.get("domain") or "electrical").strip(),
                Jsonb(meta),
                # The §E-1 spec columns (0171), resolved by PRESENCE — a net
                # deliberately annotated 0 V (a shield tied to chassis) must
                # stay 0, not fall through the alias to NULL.
                *(_first_present(n, *keys) for _column, keys in self._NET_SPEC_FIELDS),
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _pcb_insert_measure(
        self, conn: Connection, ref_id: int, m: dict[str, Any]
    ) -> None:
        metric = str(m.get("metric") or "").strip()
        if not metric:
            raise ValueError("pcb measure needs a metric")
        conn.execute(
            """
            INSERT INTO pcb_measures
                (ref_id, metric, direction, goal, strength, weight, operands,
                 reason, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                ref_id,
                metric,
                m.get("direction"),
                m.get("goal"),
                str(m.get("strength") or "gauge"),
                m.get("weight"),
                Jsonb(list(m.get("operands") or [])),
                m.get("reason"),
                # `align`'s axis/offset ride here (precis-measures-help);
                # `snapped` is job-written, never authored.
                Jsonb(dict(m.get("meta") or {})),
            ),
        )

    def _pcb_insert_feature(
        self, conn: Connection, ref_id: int, board_id: int, f: dict[str, Any]
    ) -> None:
        """A non-electrical placed feature: mounting hole /
        fiducial / testpoint / keepout / outline. ``geom`` carries the shape
        (hole ``diameter``, outline ``path`` of [x,y] points, keepout poly) in
        mm — read by the mechanical exporter (the 0041 bridge, §6)."""
        ftype = str(f.get("ftype") or f.get("type") or "").strip()
        if not ftype:
            raise ValueError("pcb feature needs an ftype")
        geom = f.get("geom")
        conn.execute(
            """
            INSERT INTO pcb_features
                (ref_id, board_id, ftype, x, y, rot, layer, fixed, geom, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                ref_id,
                board_id,
                ftype,
                f.get("x"),
                f.get("y"),
                _rot_deg(f),
                f.get("layer"),
                f.get("fixed"),
                _jsonb_or_none(geom),
                f.get("note"),
            ),
        )

    def _pcb_connect(
        self,
        conn: Connection,
        ref_id: int,
        k: dict[str, Any],
        inst_by_refdes: dict[str, tuple[int, int]],
        net_by_name: dict[str, int],
    ) -> int:
        net_name = str(k.get("net") or "").strip()
        refdes = str(k.get("refdes") or "").strip()
        pin_name = str(k.get("pin") or "").strip()
        if not (net_name and refdes and pin_name):
            raise ValueError("pcb connection needs net, refdes, and pin")
        if net_name not in net_by_name:
            # auto-create a net so wiring never silently drops (name is the meaning)
            net_by_name[net_name] = self._pcb_insert_net(
                conn, ref_id, {"name": net_name}
            )
        if refdes not in inst_by_refdes:
            raise ValueError(f"pcb connection references unknown refdes {refdes!r}")
        net_id = net_by_name[net_name]
        inst_id, comp_id = inst_by_refdes[refdes]
        pin_id = self._pcb_pin_id(conn, comp_id, pin_name)
        conn.execute(
            """
            INSERT INTO pcb_netconns (net_id, instance_id, pin_id, component_id, note)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (instance_id, pin_id) DO UPDATE SET net_id = EXCLUDED.net_id
            """,
            (net_id, inst_id, pin_id, comp_id, k.get("note")),
        )
        return 1

    def _pcb_pin_id(self, conn: Connection, component_id: int, name: str) -> int:
        """Resolve a pin by name within a component; create it ad-hoc if absent
        (the PCB netlist+placement IR — pins may be created during logical wiring)."""
        row = conn.execute(
            "SELECT pin_id FROM pcb_pins "
            "WHERE component_id = %s AND name = %s AND retired_at IS NULL",
            (component_id, name),
        ).fetchone()
        if row is not None:
            return int(row[0])
        new = conn.execute(
            "INSERT INTO pcb_pins (component_id, name) VALUES (%s, %s) "
            "RETURNING pin_id",
            (component_id, name),
        ).fetchone()
        assert new is not None
        return int(new[0])

    def _pcb_instance_map(
        self, conn: Connection, ref_id: int
    ) -> dict[str, tuple[int, int]]:
        rows = conn.execute(
            "SELECT refdes, instance_id, component_id FROM pcb_instances "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
        return {str(r[0]): (int(r[1]), int(r[2])) for r in rows}

    def _pcb_net_map(self, conn: Connection, ref_id: int) -> dict[str, int]:
        rows = conn.execute(
            "SELECT name, net_id FROM pcb_nets "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    # -- boards -----------------------------------------------------------
    def pcb_ensure_board(self, ref_id: int, *, conn: Connection | None = None) -> int:
        """Get-or-create the design's default board (``name='main'``,
        :data:`precis.pcb.DEFAULT_STACKUP`). Every instance/feature write
        path threads ``board_id`` through this — the netlist != board hedge
        (pcb-guided-place-route Slice 1). Reuses ``conn`` when called inside
        an existing transaction (e.g. :meth:`pcb_apply`); opens its own
        otherwise."""
        if conn is not None:
            return self._pcb_ensure_board(conn, ref_id)
        with self._pcb_tx() as c:
            return self._pcb_ensure_board(c, ref_id)

    def _pcb_ensure_board(self, conn: Connection, ref_id: int) -> int:
        row = conn.execute(
            "SELECT board_id FROM pcb_boards "
            "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        if row is not None:
            return int(row[0])
        # Get-or-create: a concurrent pcb_apply for the same new design can
        # race this SELECT-then-INSERT and hit pcb_boards_ref_name_key
        # (the partial unique index on (ref_id, name) WHERE retired_at IS
        # NULL — the same clause the 0138 backfill uses). ON CONFLICT DO
        # NOTHING absorbs the race; re-SELECT picks up the winner's row.
        new = conn.execute(
            "INSERT INTO pcb_boards (ref_id, name, stackup) VALUES (%s, %s, %s) "
            "ON CONFLICT (ref_id, name) WHERE retired_at IS NULL DO NOTHING "
            "RETURNING board_id",
            (ref_id, "main", Jsonb(DEFAULT_STACKUP)),
        ).fetchone()
        if new is not None:
            return int(new[0])
        row = conn.execute(
            "SELECT board_id FROM pcb_boards "
            "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _pcb_board_meta(
        self, conn: Connection, ref_id: int
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, int]]:
        """The board row (board_id/name/stackup/fold_lines), the design's
        net_classes (name -> rules), and a route-status summary — shared by
        :meth:`pcb_load` (TOC) and :meth:`pcb_graph` (the eyes).

        The summary counts LIVE nets by their route status and agrees with
        :meth:`pcb_route_status` net-for-net by construction: same driving
        table, same retired-net filter, same 'unrouted' default for a net
        with no row. It used to count ``pcb_routes`` rows instead, which
        disagreed in both directions at once — see that query's comment."""
        board_row = conn.execute(
            "SELECT board_id, name, stackup, fold_lines, meta FROM pcb_boards "
            "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        board = (
            {
                "board_id": int(board_row[0]),
                "name": board_row[1],
                "stackup": board_row[2],
                "fold_lines": board_row[3],
                # `meta.phase` is the authored design phase (Slice 8's
                # ``op='phase'``): netlist/placed/routed/fab; absent means
                # ``drc.derive_phase`` derives it. The board row, not the
                # ref — fab-readiness is a claim about this board.
                "meta": dict(board_row[4] or {}),
            }
            if board_row is not None
            else None
        )
        net_classes = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, rules FROM pcb_net_classes "
                "WHERE ref_id = %s AND retired_at IS NULL ORDER BY name",
                (ref_id,),
            ).fetchall()
        }
        route_status: dict[str, int] = {}
        if board is not None:
            # Drive from the live NETS and left-join the routes — the exact
            # shape :meth:`pcb_route_status` (the `view='route-status'`
            # query) already uses, and for both of its reasons (gr451046).
            #
            # Counting `pcb_routes` rows directly, as this did, was wrong
            # twice over. It kept rows for RETIRED nets, which a generator
            # re-apply produces routinely — on `ewod-dogfood-2` that printed
            # "60 failed, 59 realized", 119 rows, for a 62-net board. And it
            # omitted live nets with no route row at all, which the sibling
            # counts as 'unrouted'. So the headline a caller sees first and
            # the detailed view disagreed in BOTH directions. One rule, two
            # call sites, drifted — this module's standing defect.
            for st, note in conn.execute(
                "SELECT rt.status, rt.note "
                "FROM pcb_nets n "
                "LEFT JOIN pcb_routes rt "
                "  ON rt.net_id = n.net_id AND rt.board_id = %s "
                "WHERE n.ref_id = %s AND n.retired_at IS NULL",
                (board["board_id"], ref_id),
            ).fetchall():
                key = route_summary_status(st, note)
                route_status[key] = route_status.get(key, 0) + 1
        return board, net_classes, route_status

    # -- read -----------------------------------------------------------
    def pcb_load(self, ref_id: int) -> dict[str, Any]:
        """The design's board/net_classes/route-status + instances + nets +
        a fanout count per net, for the netlist TOC. Components/pins are
        joined into the instance rows."""
        with self._pcb_conn() as conn:
            board, net_classes, route_status = self._pcb_board_meta(conn, ref_id)
            instances = [
                {
                    "instance_id": int(r[0]),
                    "refdes": r[1],
                    "label": r[2],
                    "part_lcsc": r[3],
                    "footprint": r[4],
                    "layer": r[5],
                    "x": r[6],
                    "y": r[7],
                    "rot": r[8],
                    "fixed": r[9],
                    "roles": list(r[10] or []),
                    "note": r[11],
                    "height_mm": r[12],
                    # The import's catalog identity (`epro._component_
                    # identity` -> `c.meta["part"]`) and the local-vs-
                    # catalog footprint verdict (i.meta) hoisted — the
                    # epro writer emits the ATTRs back from `part`.
                    **(
                        {"part": (r[14] or {})["part"]}
                        if "part" in (r[14] or {})
                        else {}
                    ),
                    **{
                        k: (r[13] or {})[k]
                        for k in ("local_fp_lcsc", "local_fp_diff")
                        if k in (r[13] or {})
                    },
                }
                for r in conn.execute(
                    "SELECT i.instance_id, i.refdes, c.label, c.part_lcsc, "
                    "       c.footprint, i.layer, i.x, i.y, i.rot, i.fixed, "
                    "       i.roles, i.note, c.height_mm, i.meta, c.meta "
                    "FROM pcb_instances i JOIN pcb_components c "
                    "  ON c.component_id = i.component_id "
                    "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                    "ORDER BY i.refdes",
                    (ref_id,),
                ).fetchall()
            ]
            nets = [
                {
                    "net_id": int(r[0]),
                    "name": r[1],
                    "net_class": r[2],
                    "est_current_a": r[3],
                    "width_mm": r[4],
                    "note": r[5],
                    "fanout": int(r[6]),
                    "working_voltage_v": r[7],
                    "edge_rate_v_per_ns": r[8],
                    "impedance_ohm": r[9],
                    "function_hint": r[10],
                }
                for r in conn.execute(
                    "SELECT n.net_id, n.name, n.net_class, n.est_current_a, "
                    "       n.width_mm, n.note, count(k.netconn_id), "
                    "       n.working_voltage_v, n.edge_rate_v_per_ns, "
                    "       n.impedance_ohm, n.function_hint "
                    "FROM pcb_nets n LEFT JOIN pcb_netconns k ON k.net_id = n.net_id "
                    "WHERE n.ref_id = %s AND n.retired_at IS NULL "
                    "GROUP BY n.net_id ORDER BY count(k.netconn_id) DESC, n.name",
                    (ref_id,),
                ).fetchall()
            ]
        return {
            "board": board,
            "net_classes": net_classes,
            "route_status": route_status,
            "instances": instances,
            "nets": nets,
        }

    def pcb_instance_neighbors(self, ref_id: int, refdes: str) -> dict[str, Any] | None:
        """The graph hop from one component instance: its pins, the net on each
        pin, and the neighbouring instances on those nets."""
        with self._pcb_conn() as conn:
            inst = conn.execute(
                "SELECT i.instance_id, i.component_id, c.part_lcsc, "
                "       pt.description "
                "FROM pcb_instances i "
                "JOIN pcb_components c ON c.component_id = i.component_id "
                "LEFT JOIN parts pt ON pt.lcsc = c.part_lcsc "
                "WHERE i.ref_id = %s AND i.refdes = %s "
                "  AND i.retired_at IS NULL",
                (ref_id, refdes),
            ).fetchone()
            if inst is None:
                return None
            inst_id, comp_id = int(inst[0]), int(inst[1])
            part_lcsc, part_description = inst[2], inst[3]
            pins = [
                {
                    "pin": r[0],
                    "pad": r[1],
                    "tags": list(r[2] or []),
                    "net": r[3],
                    "neighbors": [n for n in (r[4] or []) if n and n != refdes],
                }
                for r in conn.execute(
                    """
                    SELECT p.name, p.pad, p.tags, n.name,
                           array_agg(DISTINCT ni.refdes)
                    FROM pcb_pins p
                    LEFT JOIN pcb_netconns k
                       ON k.pin_id = p.pin_id AND k.instance_id = %s
                    LEFT JOIN pcb_nets n ON n.net_id = k.net_id
                    LEFT JOIN pcb_netconns k2 ON k2.net_id = k.net_id
                    LEFT JOIN pcb_instances ni
                       ON ni.instance_id = k2.instance_id AND ni.retired_at IS NULL
                    WHERE p.component_id = %s AND p.retired_at IS NULL
                    GROUP BY p.pin_id, p.name, p.pad, p.tags, n.name
                    ORDER BY p.name
                    """,
                    (inst_id, comp_id),
                ).fetchall()
            ]
        return {
            "refdes": refdes,
            "part_lcsc": part_lcsc,
            "part_description": part_description,
            "pins": pins,
        }

    def pcb_net_members(self, ref_id: int, name: str) -> dict[str, Any] | None:
        """A net's members: every (refdes, pin) on it."""
        with self._pcb_conn() as conn:
            net = conn.execute(
                "SELECT net_id, net_class, est_current_a, width_mm, "
                "       working_voltage_v, edge_rate_v_per_ns, impedance_ohm, "
                "       function_hint FROM pcb_nets "
                "WHERE ref_id = %s AND name = %s AND retired_at IS NULL",
                (ref_id, name),
            ).fetchone()
            if net is None:
                return None
            members = [
                {"refdes": r[0], "pin": r[1], "tags": list(r[2] or [])}
                for r in conn.execute(
                    "SELECT i.refdes, p.name, p.tags "
                    "FROM pcb_netconns k "
                    "JOIN pcb_instances i ON i.instance_id = k.instance_id "
                    "JOIN pcb_pins p ON p.pin_id = k.pin_id "
                    "WHERE k.net_id = %s ORDER BY i.refdes, p.name",
                    (int(net[0]),),
                ).fetchall()
            ]
        return {
            "name": name,
            "net_class": net[1],
            "est_current_a": net[2],
            "width_mm": net[3],
            "working_voltage_v": net[4],
            "edge_rate_v_per_ns": net[5],
            "impedance_ohm": net[6],
            "function_hint": net[7],
            "members": members,
        }

    def pcb_graph(self, ref_id: int) -> dict[str, Any]:
        """The whole design as the *eyes* consume it: the board (stackup +
        fold_lines), placed instances, nets with their (refdes, pin) members
        + domain + ``est_current_a`` (the current annotation
        :mod:`precis.pcb.rules`'s resolver derives an IPC-2221 width from)
        + the 0171 spec annotations ``working_voltage_v`` (pairwise
        IPC-2221B clearance, :mod:`precis.pcb.drc`) and
        ``edge_rate_v_per_ns``/``impedance_ohm``/``function_hint`` (the
        :class:`precis.pcb.objectives.NetAnnotation` the coupling and
        loop-inductance cost terms read), the design's net_classes, a
        route-status summary (counts by
        :class:`pcb_routes.status`; empty = all-unrouted), and the
        unconnected pins. Pure data — the analysis lives in
        :mod:`precis.pcb`. ``roles`` is ``{name: {voltage_v, fanout}}`` from
        ``pcb_roles`` (§1b supply/client roles)."""
        with self._pcb_conn() as conn:
            board, net_classes, route_status = self._pcb_board_meta(conn, ref_id)
            roles = {
                r[0]: {"voltage_v": r[1], "fanout": r[2]}
                for r in conn.execute(
                    "SELECT name, voltage_v, fanout FROM pcb_roles "
                    "WHERE ref_id = %s AND retired_at IS NULL ORDER BY name",
                    (ref_id,),
                ).fetchall()
            }
            instances = [
                {
                    "refdes": r[0],
                    "x": r[1],
                    "y": r[2],
                    "layer": r[3],
                    "roles": list(r[4] or []),
                    "label": r[5],
                    "height_mm": r[6],
                    "n_pins": int(r[7]),
                    "fixed": r[8],
                    # `rot` was WRITE-ONLY: pcb_set_pose persisted it and no
                    # reader ever selected it, so every IR rebuilt from this
                    # graph came back with every part at 0 degrees. Placement
                    # therefore handed routing a different board than the one
                    # it had settled, and the DRC view could not reproduce a
                    # single pin coordinate of the copper it was checking.
                    "rot": r[9],
                    # `part_lcsc` was the SAME class of gap: real, joined
                    # right here (`c.part_lcsc`), just never selected — so
                    # `pcb.ir.PcbIR` (built off this graph) had no field to
                    # carry it, and every caller with a Store handle and a
                    # cached `Store.pcb_footprints_for` (keyed by C-number)
                    # had no key to remap it onto a refdes-keyed dict with.
                    # `precis.pcb.realize.pad_geometry` has taken a
                    # refdes-keyed `footprints` arg all along; this is the
                    # missing join that reaches it.
                    "part_lcsc": r[10],
                    # The design-local footprint NAME (pcb-ewod-multitile
                    # Slice 1) — `c.footprint`, the SAME free-text snapshot
                    # column a catalog part's label already rides on
                    # (0047), doubling as the join key into
                    # `pcb_local_footprints` for an instance with no
                    # `part_lcsc` at all. `ir.py::from_graph` only treats
                    # it as that join key when `part_lcsc` is falsy — a
                    # catalog part's OWN footprint label (e.g. "SOT-23")
                    # is not a `pcb_local_footprints` name and is simply
                    # never looked up there.
                    "footprint": r[13],
                    # `extended_part` is the THIRD instance of this same
                    # gap: `PcbIR.inst_extended_part` (cost.py's
                    # `_extended_part_fees`) has always come back all-False
                    # on every board, because nothing here ever joined the
                    # Basic-vs-Extended signal in — `parts.basic` is a real,
                    # populated column (`pcb.catalog.normalize_jlcparts_row`,
                    # `parts_select_idx`), just never selected. JLC charges a
                    # flat per-line surcharge for every Extended part, so
                    # every estimate this system has produced understated
                    # cost by a real amount. `None`/no catalog match reads
                    # as "not (known) Extended" — same "undefined stays
                    # undefined, never silently promoted to a fee" caution
                    # as `coupling_bound_k`'s own note — rather than
                    # guessing either way for a part not in the catalog.
                    "extended_part": r[11],
                    # Placement-constraint keys hoisted from `i.meta` onto
                    # the instance dict itself — exactly the top-level
                    # shape `precis.pcb.ir.from_graph`'s
                    # `_parse_instance_groups` reads. Absent keys stay
                    # absent (no `None` placeholders) so a group-less
                    # design's graph is byte-identical to before.
                    **{
                        k: (r[12] or {})[k]
                        for k in (
                            "group",
                            "group_offset",
                            "pattern",
                            "pattern_instance",
                            "edge_mate",
                            "max_travel_mm",
                            # "match"/"differ"/"uncached" — the local-vs-
                            # catalog footprint verdict `pcb_apply` stamps
                            # (see `_pcb_footprint_refs_by_components`),
                            # plus the WHY string on a "differ".
                            "local_fp_lcsc",
                            "local_fp_diff",
                        )
                        if k in (r[12] or {})
                    },
                    # `pin_swap_groups` (Rulings 2026-09-19 item 6) hoisted
                    # from `c.meta` (a component-TYPE fact, unlike the
                    # `i.meta` block above) the same way — `pcb_route`
                    # reads it straight off the instance dict, never the
                    # raw meta blob.
                    **(
                        {"pin_swap_groups": (r[14] or {})["pin_swap_groups"]}
                        if "pin_swap_groups" in (r[14] or {})
                        else {}
                    ),
                    # `supplies` (§1b): list of {role, blocks, ...}.
                    **(
                        {"supplies": (r[14] or {})["supplies"]}
                        if "supplies" in (r[14] or {})
                        else {}
                    ),
                    # The catalog-identity blob the .epro2 reader recovered
                    # (`epro._component_identity` -> `part_meta` ->
                    # `c.meta["part"]`): {value, mpn, mfr, supplier,
                    # supplier_part}. Surfaced so the .epro2 writer can
                    # emit the ATTRs back.
                    **(
                        {"part": (r[14] or {})["part"]}
                        if "part" in (r[14] or {})
                        else {}
                    ),
                    # The catalog description the ``parts`` join reaches
                    # (slice 3 — ``drc.check_passive_rating`` parses it
                    # into a passive's value/dielectric/rating via
                    # ``catalog.parse_passive_description``). Absent when
                    # the instance has no catalog part — the parse's None
                    # is what makes an uncatalogued passive not-checked,
                    # never a guessed rating.
                    "part_description": r[15],
                }
                for r in conn.execute(
                    "SELECT i.refdes, i.x, i.y, i.layer, i.roles, c.label, "
                    "       c.height_mm, "
                    # `pn` / `pt`, never both `p`: the scalar subquery and the
                    # LEFT JOIN below used to share the alias `p`. Legal --
                    # the inner scope shadows the outer -- but the next
                    # person to reference `parts` from inside the subquery
                    # (or `pcb_pins` from the join) silently gets the other
                    # table and a plausible wrong answer.
                    "       (SELECT count(*) FROM pcb_pins pn "
                    "        WHERE pn.component_id = i.component_id "
                    "          AND pn.retired_at IS NULL), i.fixed, i.rot, "
                    "       c.part_lcsc, "
                    # `parts.lcsc` is unique, so this LEFT JOIN cannot
                    # multiply instance rows; a non-catalog part yields NULL
                    # -> false, matching `extended_part`'s documented
                    # "unknown is never silently promoted to a fee".
                    "       COALESCE(NOT pt.basic, false), i.meta, c.footprint, "
                    "       c.meta, pt.description "
                    "FROM pcb_instances i JOIN pcb_components c "
                    "  ON c.component_id = i.component_id "
                    "  LEFT JOIN parts pt ON pt.lcsc = c.part_lcsc "
                    "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                    "ORDER BY i.refdes",
                    (ref_id,),
                ).fetchall()
            ]
            net_rows = conn.execute(
                "SELECT net_id, name, net_class, domain, est_current_a, "
                "       working_voltage_v, edge_rate_v_per_ns, impedance_ohm, "
                "       function_hint, meta FROM pcb_nets "
                "WHERE ref_id = %s AND retired_at IS NULL ORDER BY name",
                (ref_id,),
            ).fetchall()
            nets = {
                int(r[0]): {
                    "name": r[1],
                    "net_class": r[2],
                    "domain": r[3],
                    "est_current_a": r[4],
                    "working_voltage_v": r[5],
                    "edge_rate_v_per_ns": r[6],
                    "impedance_ohm": r[7],
                    "function_hint": r[8],
                    # `meta.rail` is the net's named supply rail (Slice 5 —
                    # what `rail_crossing` and `no_decoupling` join on).
                    "meta": dict(r[9] or {}),
                    "members": [],
                }
                for r in net_rows
            }
            for r in conn.execute(
                # `p.tags`/`p.meta` carry the §1i etype + §1j markers +
                # the Slice-5 bounds (max_voltage_v, vih/vil, rail) the
                # ERC rules read.
                "SELECT k.net_id, i.refdes, p.name, p.tags, p.meta "
                "FROM pcb_netconns k "
                "JOIN pcb_nets n ON n.net_id = k.net_id "
                "JOIN pcb_instances i ON i.instance_id = k.instance_id "
                "JOIN pcb_pins p ON p.pin_id = k.pin_id "
                "WHERE n.ref_id = %s AND n.retired_at IS NULL "
                "ORDER BY n.name, i.refdes, p.name",
                (ref_id,),
            ).fetchall():
                nid = int(r[0])
                if nid in nets:
                    nets[nid]["members"].append(
                        {
                            "refdes": r[1],
                            "pin": r[2],
                            "tags": list(r[3] or []),
                            "meta": dict(r[4] or {}),
                        }
                    )
            unconnected = [
                # `tags`/`meta` carry the §1j intent markers
                # (`nc`/`unused`/`must_terminate`) that
                # `drc.check_unconnected_pins` reads — a netless pin is a
                # complaint only when the author did not mark WHY.
                {
                    "refdes": r[0],
                    "pin": r[1],
                    "tags": list(r[2] or []),
                    "meta": dict(r[3] or {}),
                }
                for r in conn.execute(
                    "SELECT i.refdes, p.name, p.tags, p.meta "
                    "FROM pcb_instances i "
                    "JOIN pcb_pins p ON p.component_id = i.component_id "
                    "  AND p.retired_at IS NULL "
                    "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                    "  AND NOT EXISTS (SELECT 1 FROM pcb_netconns k "
                    "                  WHERE k.instance_id = i.instance_id "
                    "                    AND k.pin_id = p.pin_id) "
                    "ORDER BY i.refdes, p.name",
                    (ref_id,),
                ).fetchall()
            ]
        return {
            "board": board,
            "instances": instances,
            "nets": list(nets.values()),
            "net_classes": net_classes,
            "roles": roles,
            "route_status": route_status,
            "unconnected": unconnected,
        }

    def pcb_route_status(self, ref_id: int) -> list[dict[str, Any]]:
        """Per-net route status for ``view='route-status'`` — a net with no
        :class:`pcb_routes` row reads as ``'unrouted'`` (the default state,
        not a missing one). ``note`` carries the row's optional legible
        reason (e.g. a dangling <2-member net is written ``'realized'``
        with a note explaining why — see ``pcb_route``'s job docstring —
        so a bare ``status`` doesn't read as an actually-routed net)."""
        with self._pcb_conn() as conn:
            board_row = conn.execute(
                "SELECT board_id FROM pcb_boards "
                "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            board_id = int(board_row[0]) if board_row is not None else None
            return [
                {
                    "name": r[0],
                    "net_class": r[1],
                    "domain": r[2],
                    "status": r[3] or "unrouted",
                    "note": r[4],
                }
                for r in conn.execute(
                    "SELECT n.name, n.net_class, n.domain, rt.status, rt.note "
                    "FROM pcb_nets n "
                    "LEFT JOIN pcb_routes rt "
                    "  ON rt.net_id = n.net_id AND rt.board_id = %s "
                    "WHERE n.ref_id = %s AND n.retired_at IS NULL "
                    "ORDER BY n.name",
                    (board_id, ref_id),
                ).fetchall()
            ]

    def pcb_upsert_net_classes(
        self,
        ref_id: int,
        classes: dict[str, dict[str, Any]],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Upsert per-design net-class rules by name (``{name: rules}``).
        Upsert only — no soft-retire of names absent from the batch;
        deletion is an explicit later op. Returns the count written.

        Reuses ``conn`` when called inside an existing transaction (e.g. the
        handler bundling this with :meth:`pcb_apply` so both writes commit
        or roll back as one unit); opens its own otherwise (mirrors
        :meth:`pcb_ensure_board`)."""
        if conn is not None:
            return self._pcb_upsert_net_classes(conn, ref_id, classes)
        with self._pcb_tx() as c:
            return self._pcb_upsert_net_classes(c, ref_id, classes)

    def _pcb_upsert_net_classes(
        self, conn: Connection, ref_id: int, classes: dict[str, dict[str, Any]]
    ) -> int:
        n = 0
        for name, rules in classes.items():
            name = str(name).strip()
            if not name:
                raise ValueError("pcb net_class needs a name")
            conn.execute(
                """
                INSERT INTO pcb_net_classes (ref_id, name, rules)
                VALUES (%s, %s, %s)
                ON CONFLICT (ref_id, name) WHERE retired_at IS NULL
                DO UPDATE SET rules = EXCLUDED.rules
                """,
                (ref_id, name, Jsonb(dict(rules or {}))),
            )
            n += 1
        return n

    def pcb_upsert_roles(
        self,
        ref_id: int,
        roles: list[dict[str, Any]],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Upsert per-design supply/client roles by name
        (``[{name, voltage_v, fanout}]``; ``fanout`` null = unbounded,
        absent = 1). Upsert only, like :meth:`pcb_upsert_net_classes`.
        Returns the count written."""
        rows = _normalize_roles(roles)
        if conn is not None:
            return self._pcb_upsert_roles(conn, ref_id, rows)
        with self._pcb_tx() as c:
            return self._pcb_upsert_roles(c, ref_id, rows)

    def _pcb_upsert_roles(
        self,
        conn: Connection,
        ref_id: int,
        rows: list[tuple[str, float | None, int | None]],
    ) -> int:
        for name, volt, fan in rows:
            conn.execute(
                """
                INSERT INTO pcb_roles (ref_id, name, voltage_v, fanout)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (ref_id, name) WHERE retired_at IS NULL
                DO UPDATE SET voltage_v = EXCLUDED.voltage_v,
                              fanout = EXCLUDED.fanout
                """,
                (ref_id, name, volt, fan),
            )
        return len(rows)

    # -- geometric DRC findings (pcb-guided-place-route Slice 8) ----------
    def pcb_set_board_phase(self, ref_id: int, phase: str | None) -> str | None:
        """The authored design phase (pcb-lazy-netlist-and-checks.md Slice
        8, ``op='phase'``) onto ``pcb_boards.meta['phase']`` — the board
        row, because fab-readiness is a claim about THIS board.
        ``phase=None`` clears it, returning the check surface to
        :func:`precis.pcb.drc.derive_phase`'s derived default. Returns the
        phase now in effect for callers to echo. Raises ``NotFound`` for a
        design with no board yet."""
        with self._pcb_tx() as conn:
            row = conn.execute(
                "SELECT board_id FROM pcb_boards "
                "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"pcb {ref_id}: no board to phase")
            if phase is None:
                conn.execute(
                    "UPDATE pcb_boards SET meta = meta - 'phase' WHERE board_id = %s",
                    (int(row[0]),),
                )
            else:
                conn.execute(
                    "UPDATE pcb_boards SET meta = meta || %s::jsonb "
                    "WHERE board_id = %s",
                    (Jsonb({"phase": phase}), int(row[0])),
                )
        return phase

    def pcb_merge_board_meta(
        self, ref_id: int, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge ``patch`` into the live ``main`` board's ``meta``
        (``meta || patch`` — top-level keys in the patch win, the rest
        survive) and return the merged dict. ``op='order'`` records its
        ``jlc_order`` cart row through here. Raises ``NotFound`` for a
        design with no board yet — same contract as
        :meth:`pcb_set_board_phase`."""
        with self._pcb_tx() as conn:
            row = conn.execute(
                "SELECT board_id, meta FROM pcb_boards "
                "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"pcb {ref_id}: no board to annotate")
            merged = {**(row[1] or {}), **patch}
            conn.execute(
                "UPDATE pcb_boards SET meta = meta || %s::jsonb WHERE board_id = %s",
                (Jsonb(dict(patch)), int(row[0])),
            )
        return merged

    def pcb_write_drc_findings(
        self,
        board_id: int,
        run_id: str,
        findings: list[dict[str, Any]],
        *,
        pads_only: bool = False,
        phase: str | None = None,
        conn: Connection | None = None,
    ) -> None:
        """Persist one DRC run — a ``pcb_drc_runs`` row (0191, written even
        when ``findings`` is empty: a clean run is a run, and the previous
        run a delta compares against) plus its ``pcb_drc_findings`` rows.
        The finding table is durable and linkable (0138): every row is a
        plain INSERT, never mutated or DELETEd by a later run (unlike
        ``pcb_copper``'s DELETE+INSERT regeneration discipline — a DRC
        run is a dated finding, not a derived-and-replaced artifact).

        Each row carries its ``finding_key`` (:func:`precis.pcb.drc.
        finding_key`, from the finding's own ``to_row``), ``margin_mm``,
        ``location`` and ``first_seen_at`` — the earliest ``created_at``
        of a row on this board with the same key, or now.

        ``phase`` (Slice 8) records the run as the delta baseline FOR THAT
        PHASE — ``pcb_boards.meta['drc_runs_by_phase'][phase] = run_id`` —
        so a phase-scoped run (a ``netlist`` eval says nothing about
        copper) never becomes the baseline a full run diffs against.
        The map lives in meta: a new phase is a key, not a migration."""
        if conn is not None:
            self._pcb_write_drc_findings(
                conn, board_id, run_id, findings, pads_only, phase
            )
            return
        with self._pcb_tx() as c:
            self._pcb_write_drc_findings(
                c, board_id, run_id, findings, pads_only, phase
            )

    def _pcb_write_drc_findings(
        self,
        conn: Connection,
        board_id: int,
        run_id: str,
        findings: list[dict[str, Any]],
        pads_only: bool,
        phase: str | None = None,
    ) -> None:
        n_error = sum(1 for f in findings if f["severity"] == "error")
        conn.execute(
            "INSERT INTO pcb_drc_runs (board_id, run_id, pads_only, n_error, n_warn) "
            "VALUES (%s, %s, %s, %s, %s)",
            (board_id, run_id, pads_only, n_error, len(findings) - n_error),
        )
        if phase is not None:
            row = conn.execute(
                "SELECT meta FROM pcb_boards WHERE board_id = %s", (board_id,)
            ).fetchone()
            meta = dict((row[0] if row else None) or {})
            meta.setdefault("drc_runs_by_phase", {})[phase] = run_id
            conn.execute(
                "UPDATE pcb_boards SET meta = %s WHERE board_id = %s",
                (Jsonb(meta), board_id),
            )
        for f in findings:
            conn.execute(
                "INSERT INTO pcb_drc_findings "
                "(board_id, run_id, rule, severity, objects, detail, "
                " finding_key, margin_mm, location, first_seen_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE("
                "  (SELECT MIN(COALESCE(first_seen_at, created_at)) "
                "     FROM pcb_drc_findings "
                "    WHERE board_id = %s AND finding_key = %s), now()))",
                (
                    board_id,
                    run_id,
                    f["rule"],
                    f["severity"],
                    Jsonb(f.get("objects") or []),
                    f.get("detail"),
                    f.get("finding_key"),
                    f.get("margin_mm"),
                    f.get("location"),
                    board_id,
                    f.get("finding_key"),
                ),
            )

    _DRC_FINDING_COLS = (
        "rule, severity, objects, detail, waived_by, "
        "finding_key, margin_mm, location, first_seen_at"
    )

    @staticmethod
    def _drc_finding_row(r: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "rule": r[0],
            "severity": r[1],
            "objects": r[2],
            "detail": r[3],
            "waived_by": r[4],
            "finding_key": r[5],
            "margin_mm": r[6],
            "location": r[7],
            "first_seen_at": r[8],
        }

    def _pcb_drc_latest_run_id(self, conn: Connection, board_id: int) -> str | None:
        """The most recent run on ``board_id``: ``pcb_drc_runs`` when any
        run has been recorded there (0191 onward — a clean run has a row
        here and none in the finding table), else the newest finding row's
        run (a pre-0191 run, which only existed if it had findings)."""
        run_row = conn.execute(
            "SELECT run_id FROM pcb_drc_runs "
            "WHERE board_id = %s ORDER BY created_at DESC LIMIT 1",
            (board_id,),
        ).fetchone()
        if run_row is None:
            run_row = conn.execute(
                "SELECT run_id FROM pcb_drc_findings "
                "WHERE board_id = %s ORDER BY created_at DESC LIMIT 1",
                (board_id,),
            ).fetchone()
        return None if run_row is None else str(run_row[0])

    def pcb_drc_findings_latest(
        self, ref_id: int
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """The most recent DRC run's ``(run_id, findings)`` for the design's
        board — ``(None, [])`` when there is no board yet or no run has
        ever been recorded (the ``netlist_drc_clean`` gate evaluator reads
        this: no run yet means "not yet", not "clean"). A clean run
        recorded since 0191 is ``(run_id, [])`` — a verdict, not an
        absence."""
        with self._pcb_conn() as conn:
            board_row = conn.execute(
                "SELECT board_id FROM pcb_boards "
                "WHERE ref_id = %s AND name = 'main' AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            if board_row is None:
                return None, []
            board_id = int(board_row[0])
            run_id = self._pcb_drc_latest_run_id(conn, board_id)
            if run_id is None:
                return None, []
            return run_id, self._pcb_drc_run_rows(conn, board_id, run_id)

    def pcb_drc_findings_for_run(
        self, board_id: int, run_id: str
    ) -> list[dict[str, Any]]:
        """Every persisted finding of one run, in ``finding_id`` order."""
        with self._pcb_conn() as conn:
            return self._pcb_drc_run_rows(conn, board_id, run_id)

    def _pcb_drc_run_rows(
        self, conn: Connection, board_id: int, run_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT {self._DRC_FINDING_COLS} "
            "FROM pcb_drc_findings WHERE board_id = %s AND run_id = %s "
            "ORDER BY finding_id",
            (board_id, run_id),
        ).fetchall()
        return [self._drc_finding_row(r) for r in rows]

    def pcb_drc_previous_run(
        self, board_id: int, *, phase: str | None = None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """The latest recorded run on ``board_id`` and its rows — what the
        run about to be written diffs against (``view='drc'`` calls this
        BEFORE :meth:`pcb_write_drc_findings`). ``(None, [])`` when no run
        has ever been recorded.

        With ``phase`` the baseline is the last run OF THAT PHASE — the
        ``meta['drc_runs_by_phase']`` map ``pcb_write_drc_findings``
        maintains. A scoped run must never become another phase's
        baseline: a ``netlist`` eval has no copper findings, so diffing
        the next full run against it would report every geometric
        finding as resolved-then-new.

        A phase with no map entry falls back to the latest run only on a
        board that has NO keyed run yet — its history predates the map.
        Once any phase is keyed, an unmapped phase has no baseline: the
        map keeps only each phase's latest run, so an older run missing
        from it is a superseded keyed run of some other phase, not an
        unkeyed one (gr488910 — a first ``routed`` run diffed the
        superseded ``placed`` run). Genuinely no baseline → ``(None, [])``."""
        with self._pcb_conn() as conn:
            if phase is not None:
                row = conn.execute(
                    "SELECT meta -> 'drc_runs_by_phase' "
                    "FROM pcb_boards WHERE board_id = %s",
                    (board_id,),
                ).fetchone()
                by_phase = dict((row[0] if row else None) or {})
                run_id = by_phase.get(phase)
                if run_id is None and not by_phase:
                    run_id = self._pcb_drc_latest_run_id(conn, board_id)
            else:
                run_id = self._pcb_drc_latest_run_id(conn, board_id)
            if run_id is None:
                return None, []
            return run_id, self._pcb_drc_run_rows(conn, board_id, run_id)

    def pcb_set_placement(
        self,
        ref_id: int,
        placement: dict[str, tuple[float, float]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Write new `(x, y)` for instances by refdes (placer).

        Never moves a `fixed` instance (guarded in SQL too). Optionally stamps
        a placement summary onto `refs.meta`. Returns the rows moved."""
        moved = 0
        with self._pcb_tx() as conn:
            for refdes, (x, y) in placement.items():
                moved += conn.execute(
                    "UPDATE pcb_instances SET x = %s, y = %s "
                    "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL "
                    "  AND (fixed IS NULL OR fixed NOT IN ('xy', 'both'))",
                    (float(x), float(y), ref_id, refdes),
                ).rowcount
            if meta is not None:
                conn.execute(
                    "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
                    (Jsonb(dict(meta)), ref_id),
                )
        return moved

    # -- pcb-guided-place-route Slice 10: job write-back + inline editors --

    def pcb_set_pose(
        self,
        ref_id: int,
        pose: dict[str, tuple[float, float, float]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Write new ``(x, y, rot)`` for instances by refdes — the joint
        placement+topology optimizer's write-back (``pcb_place``/
        ``pcb_route`` jobs). Same fixed-respecting discipline as
        :meth:`pcb_set_placement`, but per-axis: ``fixed='rot'`` still
        gets ``(x, y)`` written, ``fixed='xy'`` still gets rotation,
        ``fixed='both'`` blocks everything. :meth:`pcb_set_placement`
        stays untouched — its only caller (v1 autoplace/route
        round-trip) never writes rotation, so a coarser blanket guard is
        honest there. Returns instances with ≥1 axis written."""
        moved = 0
        with self._pcb_tx() as conn:
            for refdes, (x, y, rot) in pose.items():
                moved += conn.execute(
                    "UPDATE pcb_instances SET "
                    "  x = CASE WHEN fixed IS NULL OR fixed NOT IN ('xy', 'both') "
                    "           THEN %s ELSE x END, "
                    "  y = CASE WHEN fixed IS NULL OR fixed NOT IN ('xy', 'both') "
                    "           THEN %s ELSE y END, "
                    "  rot = CASE WHEN fixed IS NULL OR fixed NOT IN ('rot', 'both') "
                    "           THEN %s ELSE rot END "
                    "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL",
                    (float(x), float(y), float(rot), ref_id, refdes),
                ).rowcount
            if meta is not None:
                conn.execute(
                    "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
                    (Jsonb(dict(meta)), ref_id),
                )
        return moved

    def pcb_move_instance(
        self,
        ref_id: int,
        refdes: str,
        *,
        x: float | None = None,
        y: float | None = None,
        rot: float | None = None,
        layer: str | None = None,
        fixed: str | None = _UNSET,
        meta: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> bool:
        """The inline move/rotate/(un)lock editor. Unlike
        :meth:`pcb_set_pose`'s optimizer write-back, this IS the authorized
        edit path, so it never refuses a ``fixed`` instance — an LLM
        deliberately repositioning a locked part is not the failure mode
        the optimizer's guard exists for. ``fixed`` defaults to a sentinel
        so a caller can pass ``fixed=None`` to explicitly CLEAR the lock,
        distinct from omitting it (leave the lock alone). ``layer``
        (``top``/``bottom``) is the side — an EasyEDA ``--update`` carries
        a flip as an edit like any other. ``meta`` merges a patch into the
        instance's jsonb; a ``None`` value DELETES the key (``meta - 'k'``),
        which is how ``max_travel_mm`` is cleared — the soft pin lives on
        instance meta, same as ``group``/``edge_mate``. Reuses ``conn``
        inside an existing transaction. Returns whether a live instance was
        found and updated."""
        sets: list[str] = []
        params: list[Any] = []
        if x is not None:
            sets.append("x = %s")
            params.append(float(x))
        if y is not None:
            sets.append("y = %s")
            params.append(float(y))
        if rot is not None:
            sets.append("rot = %s")
            params.append(float(rot))
        if layer is not None:
            sets.append("layer = %s")
            params.append(layer)
        if fixed is not _UNSET:
            sets.append("fixed = %s")
            params.append(fixed)
        if meta is not None:
            sets.append("meta = (meta - %s::text[]) || %s::jsonb")
            params.append([k for k, v in meta.items() if v is None])
            params.append(Jsonb({k: v for k, v in meta.items() if v is not None}))
        if not sets:
            return False
        params += [ref_id, refdes]
        sql = (
            f"UPDATE pcb_instances SET {', '.join(sets)} "  # nosec B608 — sets[] is a fixed internal column-name list, never caller input
            "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL"
        )
        if conn is not None:
            return conn.execute(sql, params).rowcount > 0
        with self._pcb_tx() as c:
            return c.execute(sql, params).rowcount > 0

    def pcb_features_retire(
        self,
        ref_id: int,
        feature_ids: Iterable[int],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Retire (never delete) the live features with these ids; returns
        how many were live. Reuses ``conn`` inside an existing transaction."""
        ids = [int(i) for i in feature_ids]
        if not ids:
            return 0
        sql = (
            "UPDATE pcb_features SET retired_at = now() "
            "WHERE ref_id = %s AND feature_id = ANY(%s) AND retired_at IS NULL"
        )
        if conn is not None:
            return conn.execute(sql, (ref_id, ids)).rowcount
        with self._pcb_tx() as c:
            return c.execute(sql, (ref_id, ids)).rowcount

    def pcb_move_group(
        self,
        ref_id: int,
        board_id: int,
        poses: list[tuple[str, float, float, float]],
        *,
        generator_names: Iterable[str],
        pivot: tuple[float, float],
        target: tuple[float, float],
        dtheta_deg: float,
        rip_nets: Iterable[str],
        lock: tuple[str, str | None] | None = None,
    ) -> list[str]:
        """The rigid group move (``op='move'`` on a generator member): the
        one-transform case of :meth:`pcb_move_groups`."""
        names = list(generator_names)
        return self.pcb_move_groups(
            ref_id,
            board_id,
            poses,
            transforms=[(names, pivot, target, dtheta_deg)] if names else [],
            rip_nets=rip_nets,
            lock=lock,
        )

    def pcb_move_groups(
        self,
        ref_id: int,
        board_id: int,
        poses: list[tuple[str, float, float, float]],
        *,
        transforms: list[
            tuple[list[str], tuple[float, float], tuple[float, float], float]
        ],
        rip_nets: Iterable[str],
        lock: tuple[str, str | None] | None = None,
    ) -> list[str]:
        """The multi-part ``op='move'``, ONE transaction: every
        ``(refdes, x, y, rot)`` pose written, every active
        ``pcb_fixed_copper`` row of each ``(generator_names, pivot, target,
        dtheta_deg)`` transform carried by that rigid transform
        (:func:`precis.pcb.geom.rigid_transform_geom`), and every net of
        ``rip_nets`` that has router copper ripped (:meth:`pcb_rip_route`'s
        reset). Any failure rolls all of it back. Returns the nets actually
        ripped."""
        ripped: list[str] = []
        with self._pcb_tx() as conn:
            for refdes, x, y, rot in poses:
                if not self.pcb_move_instance(
                    ref_id, refdes, x=x, y=y, rot=rot, conn=conn
                ):
                    raise ValueError(f"pcb instance {refdes!r} not found")
            if lock is not None:  # (refdes, new `fixed` value; None clears)
                self.pcb_move_instance(ref_id, lock[0], fixed=lock[1], conn=conn)
            for names, pivot, target, dtheta_deg in transforms:
                rows = conn.execute(
                    "SELECT fixed_id, geom FROM pcb_fixed_copper "
                    "WHERE board_id = %s AND retired_at IS NULL "
                    "AND generator_name = ANY(%s) ORDER BY fixed_id",
                    (board_id, list(names)),
                ).fetchall()
                for fixed_id, geom in rows:
                    moved = pcb_geom.rigid_transform_geom(
                        geom or {}, pivot=pivot, target=target, dtheta_deg=dtheta_deg
                    )
                    conn.execute(
                        "UPDATE pcb_fixed_copper SET geom = %s WHERE fixed_id = %s",
                        (Jsonb(moved), fixed_id),
                    )
            for net in rip_nets:
                if self._pcb_rip_route(conn, ref_id, net):
                    ripped.append(net)
        return ripped

    def pcb_nets_with_router_copper(self, board_id: int) -> set[str]:
        """Names of the active nets that own at least one DERIVED
        ``pcb_copper`` row — the nets a part move can strand."""
        with self._pcb_conn() as conn:
            return {
                str(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT n.name FROM pcb_copper c "
                    "JOIN pcb_nets n ON n.net_id = c.net_id "
                    "WHERE c.board_id = %s AND n.retired_at IS NULL",
                    (board_id,),
                ).fetchall()
            }

    def pcb_net_ids(self, ref_id: int) -> dict[str, int]:
        """``{net name: net_id}`` for a design — the join key
        :meth:`pcb_routes_write`/:meth:`pcb_copper_replace`'s callers use to
        turn the IR's name-addressed sketch back into real FKs."""
        with self._pcb_conn() as conn:
            return {
                r[0]: int(r[1])
                for r in conn.execute(
                    "SELECT name, net_id FROM pcb_nets "
                    "WHERE ref_id = %s AND retired_at IS NULL",
                    (ref_id,),
                ).fetchall()
            }

    def pcb_routes_get(self, ref_id: int) -> dict[str, dict[str, Any]]:
        """Every net's persisted sketch (tree/topology/layer_assign/status/
        fail/meta), by net name — feeds
        :func:`precis.pcb.session.apply_route_overrides` so a pinned side
        choice survives the next ``pcb_route`` run's IR rebuild. A net with
        no ``pcb_routes`` row yet reads as the all-empty/``'unrouted'``
        default, same convention as :meth:`pcb_route_status`."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT n.name, rt.tree, rt.topology, rt.layer_assign, "
                "       rt.status, rt.fail, rt.meta "
                "FROM pcb_nets n "
                "JOIN pcb_boards b ON b.ref_id = n.ref_id AND b.name = 'main' "
                "  AND b.retired_at IS NULL "
                "LEFT JOIN pcb_routes rt "
                "  ON rt.net_id = n.net_id AND rt.board_id = b.board_id "
                "WHERE n.ref_id = %s AND n.retired_at IS NULL",
                (ref_id,),
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for name, tree, topology, layer_assign, status, fail, meta in rows:
            out[name] = {
                "tree": tree or [],
                "topology": topology or [],
                "layer_assign": layer_assign or [],
                "status": status or "unrouted",
                "fail": fail,
                "meta": meta or {},
            }
        return out

    def pcb_routes_write(
        self, ref_id: int, board_id: int, rows: dict[str, dict[str, Any]]
    ) -> int:
        """Upsert one ``pcb_routes`` row per named net — the ``pcb_route``
        job's checkpoint write-back. A net absent from ``rows`` (the
        sketch has nothing new to say about it, e.g. plane-served) is left
        untouched. ``row["note"]`` (optional) persists a human-legible
        reason alongside ``status`` — e.g. the dangling-net exemption,
        which writes ``status='realized'`` with a note explaining why
        rather than a bare status a later reader can't distinguish from an
        actually-routed net. Returns the number of rows written."""
        n = 0
        with self._pcb_tx() as conn:
            for net_name, row in rows.items():
                net = conn.execute(
                    "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                    "AND retired_at IS NULL",
                    (ref_id, net_name),
                ).fetchone()
                if net is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO pcb_routes
                        (board_id, net_id, tree, topology, layer_assign,
                         status, fail, note, meta, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (board_id, net_id) DO UPDATE SET
                        tree = EXCLUDED.tree,
                        topology = EXCLUDED.topology,
                        layer_assign = EXCLUDED.layer_assign,
                        status = EXCLUDED.status,
                        fail = EXCLUDED.fail,
                        note = EXCLUDED.note,
                        meta = EXCLUDED.meta,
                        updated_at = now()
                    """,
                    (
                        board_id,
                        int(net[0]),
                        _jsonb_or_none(row.get("tree")),
                        _jsonb_or_none(row.get("topology")),
                        _jsonb_or_none(row.get("layer_assign")),
                        row.get("status") or "unrouted",
                        _jsonb_or_none(row.get("fail")),
                        row.get("note"),
                        Jsonb(dict(row.get("meta") or {})),
                    ),
                )
                n += 1
        return n

    def pcb_copper_replace(self, board_id: int, rows: list[dict[str, Any]]) -> int:
        """Regenerate a board's derived copper wholesale — DELETE + INSERT,
        the same cascade discipline as chunks->embeddings the table's own
        comment promises (never a partial UPDATE). ``rows`` must be
        router-derived geometry only — NEVER the output of
        :meth:`pcb_copper_list` round-tripped back in, which would persist
        that call's unioned AUTHORED ``pcb_fixed_copper`` rows into this
        DERIVED table and defeat the whole point of keeping the two
        separate (pcb-pre-place-route-blocks Slice 1). No caller does that
        today (checked: every :meth:`pcb_copper_list` reader is a render/
        DRC consumer, never a :meth:`pcb_copper_replace` source)."""
        with self._pcb_tx() as conn:
            conn.execute("DELETE FROM pcb_copper WHERE board_id = %s", (board_id,))
            for r in rows:
                conn.execute(
                    "INSERT INTO pcb_copper "
                    "(board_id, ctype, layer, net_id, route_id, geom) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        board_id,
                        r["ctype"],
                        r["layer"],
                        r["net_id"],
                        r.get("route_id"),
                        Jsonb(r["geom"]),
                    ),
                )
        return len(rows)

    def pcb_copper_list(self, board_id: int) -> list[dict[str, Any]]:
        """Every derived copper row for a board, flattened into
        :mod:`precis.pcb.drc`'s item shape (``{"ctype", "layer", "net",
        ...geom fields}``) — the flat convention
        :mod:`precis.pcb.realize`/:mod:`precis.pcb.gerber` already share,
        so ``view='drc'`` hands this straight to
        :func:`precis.pcb.drc.run_geometric_drc` with no reshaping.
        ``net_id`` resolves to its net NAME via a join — DRC findings
        read by name, matching this layer's "names for humans/export, not
        internal identity" discipline.

        **pcb-pre-place-route-blocks Slice 1**: UNIONS in the board's
        active AUTHORED fixed copper (:meth:`pcb_fixed_copper_list`) —
        every reader (DRC, ``view='gerber'``, ``view='svg'``) sees authored
        + derived copper as one list with no separate read path to
        remember; ``fixed: True`` marks the authored rows, absent/``False``
        on derived ones. See :meth:`pcb_copper_replace`'s own docstring for
        the one thing this union must NEVER feed back into."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT c.ctype, c.layer, n.name, c.geom "
                "FROM pcb_copper c JOIN pcb_nets n ON n.net_id = c.net_id "
                "WHERE c.board_id = %s AND n.retired_at IS NULL",
                (board_id,),
            ).fetchall()
        derived = [
            {"ctype": ctype, "layer": layer, "net": net_name, **(geom or {})}
            for ctype, layer, net_name, geom in rows
        ]
        return derived + self.pcb_fixed_copper_list(board_id)

    # -- pcb_fixed_copper -- AUTHORED copper, an input parallel to
    # pcb_planes (pcb-pre-place-route-blocks Slice 1) ----------------------
    def _pcb_fixed_copper_board_envelope(
        self, conn: Connection, board_id: int
    ) -> dict[str, float | int | None]:
        """The board's CURRENT envelope facts, in the same key shape a
        row's ``envelope`` dict uses — what
        :meth:`_pcb_fixed_copper_envelope_mismatch` diffs an incoming row
        against."""
        row = conn.execute(
            "SELECT stackup FROM pcb_boards WHERE board_id = %s", (board_id,)
        ).fetchone()
        stackup = row[0] if row is not None else DEFAULT_STACKUP
        cap = capability_for(_FIXED_COPPER_FAB_PROCESS)
        out: dict[str, float | int | None] = {"layers": len(stackup)}
        for env_key, cap_key in _FIXED_COPPER_ENVELOPE_FIELDS.items():
            out[env_key] = cap.jlc_min.get(cap_key)
        return out

    def _pcb_fixed_copper_envelope_mismatch(
        self, conn: Connection, board_id: int, envelope: dict[str, Any]
    ) -> str | None:
        """``None`` when every field ``envelope`` actually carries agrees
        with the board's current envelope, else the exact refusal message
        (the spec's own worked example: "fabric solved for 4 layers, board
        has 2"). An empty/missing ``envelope`` is never checked — only a
        row that records one can be refused over it."""
        if not envelope:
            return None
        current = self._pcb_fixed_copper_board_envelope(conn, board_id)
        if "layers" in envelope:
            want = envelope["layers"]
            have = current["layers"]  # always an int -- len(stackup)
            assert have is not None
            if int(want) != int(have):
                return (
                    f"fixed copper envelope mismatch: fabric solved for "
                    f"{want} layers, board has {have}"
                )
        for env_key in _FIXED_COPPER_ENVELOPE_FIELDS:
            if env_key not in envelope:
                continue
            want = envelope[env_key]
            have = current.get(env_key)
            if have is None or not math.isclose(
                float(want), float(have), rel_tol=1e-6, abs_tol=1e-6
            ):
                return (
                    f"fixed copper envelope mismatch: fabric solved for "
                    f"{env_key}={want!r}, board's current floor is {have!r}"
                )
        return None

    def pcb_fixed_copper_put(
        self,
        ref_id: int,
        board_id: int,
        generator_name: str,
        generator: str,
        version: str,
        rows: list[dict[str, Any]],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Insert generator ``generator_name``'s authored fixed-copper
        rows (pcb-pre-place-route-blocks Slice 1) — the AUTHORED track/via
        geometry an expansion wants alongside its ordinary components/
        nets/connections (:attr:`precis.pcb.generators.GeneratorExpansion.
        copper`), parallel to ``pcb_planes``'s authored-input status. Each
        row: ``{ctype: 'track'|'via', layer, net?, geom, envelope?,
        meta?}`` — ``net`` (a net NAME) resolves against the design's
        currently active nets, raising ``ValueError`` for an unknown name
        (mirrors :meth:`_pcb_connect`'s sibling check, except this one
        never auto-creates: a copper row inventing its own net is a bug,
        not intent). A row's ``envelope``, if given, is checked against
        the board's current stackup/rule floor
        (:meth:`_pcb_fixed_copper_envelope_mismatch`) BEFORE any row is
        written — a mismatch refuses the WHOLE call, nothing partially
        lands. Reuses ``conn`` inside an existing transaction (e.g.
        :meth:`_pcb_apply`'s own), opens its own otherwise."""
        if conn is not None:
            return self._pcb_fixed_copper_put(
                conn, ref_id, board_id, generator_name, generator, version, rows
            )
        with self._pcb_tx() as c:
            return self._pcb_fixed_copper_put(
                c, ref_id, board_id, generator_name, generator, version, rows
            )

    def _pcb_fixed_copper_put(
        self,
        conn: Connection,
        ref_id: int,
        board_id: int,
        generator_name: str,
        generator: str,
        version: str,
        rows: list[dict[str, Any]],
    ) -> int:
        if not rows:
            return 0
        # Envelope gate FIRST, over every row, before any INSERT — a
        # mismatch on row 5 must not leave rows 1-4 written.
        for r in rows:
            mismatch = self._pcb_fixed_copper_envelope_mismatch(
                conn, board_id, dict(r.get("envelope") or {})
            )
            if mismatch is not None:
                raise ValueError(mismatch)
        net_by_name = self._pcb_net_map(conn, ref_id)
        n = 0
        for r in rows:
            ctype = str(r.get("ctype") or "").strip()
            if ctype not in ("track", "via"):
                raise ValueError(
                    f"pcb fixed copper row needs ctype 'track' or 'via', got {ctype!r}"
                )
            layer = str(r.get("layer") or "").strip()
            if not layer:
                raise ValueError("pcb fixed copper row needs a layer")
            geom = r.get("geom")
            if not geom:
                raise ValueError("pcb fixed copper row needs geom")
            net_id: int | None = None
            net_name = r.get("net")
            if net_name:
                net_name = str(net_name).strip()
                if net_name not in net_by_name:
                    raise ValueError(
                        f"pcb fixed copper row references unknown net {net_name!r}"
                    )
                net_id = net_by_name[net_name]
            conn.execute(
                "INSERT INTO pcb_fixed_copper "
                "(board_id, ref_id, generator_name, generator, generator_version, "
                " ctype, layer, net_id, geom, envelope, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    board_id,
                    ref_id,
                    generator_name,
                    generator,
                    version,
                    ctype,
                    layer,
                    net_id,
                    Jsonb(dict(geom)),
                    Jsonb(dict(r.get("envelope") or {})),
                    Jsonb(dict(r.get("meta") or {})),
                ),
            )
            n += 1
        return n

    def pcb_fixed_copper_list(self, board_id: int) -> list[dict[str, Any]]:
        """Every ACTIVE (``retired_at IS NULL``) authored fixed-copper row
        for a board, flattened to :meth:`pcb_copper_list`'s own item shape
        (``{"ctype", "layer", "net", ...geom fields}``) plus ``fixed:
        True``, ``generator_name`` (which ``pcb_generators`` call emitted
        it), ``envelope`` (the rule envelope it was solved under) and
        ``fixed_id`` (the row's stable identity, which a DRC finding's
        objects carry so a move can key a finding without coordinates) —
        the read-back surface :meth:`pcb_copper_list` unions in so DRC/
        gerber/SVG see it as real copper with no extra plumbing. ``net``
        is ``None`` for a row whose ``net_id`` is NULL (schema-legal —
        unlike ``pcb_copper.net_id``, this FK is nullable) or whose net has
        since been retired — the geometry row itself stays visible either
        way (never silently dropped, unlike :meth:`pcb_copper_list`'s
        derived side, which inner-joins on an active net)."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT fc.ctype, fc.layer, n.name, fc.geom, fc.generator_name, "
                "       fc.envelope, fc.fixed_id "
                "FROM pcb_fixed_copper fc "
                "LEFT JOIN pcb_nets n ON n.net_id = fc.net_id AND n.retired_at IS NULL "
                "WHERE fc.board_id = %s AND fc.retired_at IS NULL",
                (board_id,),
            ).fetchall()
        return [
            {
                "ctype": ctype,
                "layer": layer,
                "net": net_name,
                **(geom or {}),
                "fixed": True,
                "generator_name": gname,
                "envelope": envelope or {},
                "fixed_id": fixed_id,
            }
            for ctype, layer, net_name, geom, gname, envelope, fixed_id in rows
        ]

    def pcb_fixed_copper_retire(
        self, ref_id: int, generator_name: str, *, conn: Connection | None = None
    ) -> int:
        """Soft-delete every active fixed-copper row generator
        ``generator_name`` owns — the copper-table half of
        :meth:`_pcb_generator_retire_expansion`'s changed-params retire
        (which calls this internally); exposed publicly for a caller that
        wants to retire a generator's fabric without re-expanding it.
        Reuses ``conn`` inside an existing transaction, opens its own
        otherwise."""
        if conn is not None:
            return self._pcb_fixed_copper_retire(conn, ref_id, generator_name)
        with self._pcb_tx() as c:
            return self._pcb_fixed_copper_retire(c, ref_id, generator_name)

    def _pcb_fixed_copper_retire(
        self, conn: Connection, ref_id: int, generator_name: str
    ) -> int:
        cur = conn.execute(
            "UPDATE pcb_fixed_copper SET retired_at = now() "
            "WHERE ref_id = %s AND generator_name = %s AND retired_at IS NULL",
            (ref_id, generator_name),
        )
        return cur.rowcount

    def pcb_rip_route(
        self, ref_id: int, net_name: str, *, conn: Connection | None = None
    ) -> bool:
        """The rip-up primitive: reset one net's sketch back to
        ``'unrouted'`` and drop its realized copper — the LLM's lever when
        the realizer reports an over-capacity gap (backlog's rip-up loop).
        A subsequent ``op='route'`` re-decides that net's topology fresh —
        a rip clears the whole persisted sketch, not just the realized
        geometry, since a pinned choice that caused the squeeze shouldn't
        survive the rip that was meant to escape it. Returns whether a
        route row existed to rip (``False`` for an already-unrouted net —
        nothing to rip). Reuses ``conn`` inside an existing transaction."""
        if conn is not None:
            return self._pcb_rip_route(conn, ref_id, net_name)
        with self._pcb_tx() as c:
            return self._pcb_rip_route(c, ref_id, net_name)

    def _pcb_rip_route(self, conn: Connection, ref_id: int, net_name: str) -> bool:
        row = conn.execute(
            "SELECT rt.route_id, rt.board_id, rt.net_id FROM pcb_routes rt "
            "JOIN pcb_nets n ON n.net_id = rt.net_id "
            "WHERE n.ref_id = %s AND n.name = %s AND n.retired_at IS NULL",
            (ref_id, net_name),
        ).fetchone()
        if row is None:
            return False
        route_id, board_id, net_id = int(row[0]), int(row[1]), int(row[2])
        conn.execute(
            "UPDATE pcb_routes SET tree = NULL, topology = NULL, "
            "layer_assign = NULL, status = 'unrouted', fail = NULL, "
            "updated_at = now() WHERE route_id = %s",
            (route_id,),
        )
        conn.execute(
            "DELETE FROM pcb_copper WHERE board_id = %s AND net_id = %s",
            (board_id, net_id),
        )
        # The last route run's digest no longer describes the stored
        # copper: count the rips on it so view='congestion' says so. The
        # next route run overwrites `last_route` wholesale, resetting it.
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{last_route,ripped}', "
            "to_jsonb(COALESCE((meta #>> '{last_route,ripped}')::int, 0) + 1)) "
            "WHERE ref_id = %s AND jsonb_typeof(meta -> 'last_route') = 'object'",
            (ref_id,),
        )
        return True

    def pcb_pin_topology(
        self, ref_id: int, net_name: str, a: str, b: str, side: int
    ) -> bool:
        """Pin one segment's side choice — a targeted MERGE into the net's
        persisted ``pcb_routes.topology`` (replacing at most the one entry
        keyed by ``(a, b)``, never the wholesale replace
        :meth:`pcb_routes_write`'s checkpoint does), so the pin survives
        until the next ``op='route'`` run reads it back via
        :func:`precis.pcb.session.apply_route_overrides`. Creates the
        net's ``pcb_routes`` row (status ``'unrouted'``) if none exists yet
        — pinning a side is a legitimate first edit to an as-yet-unrouted
        net's sketch. Returns whether the net resolved."""
        key = "|".join(sorted((a, b)))
        with self._pcb_tx() as conn:
            board_id = self._pcb_ensure_board(conn, ref_id)
            net = conn.execute(
                "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                "AND retired_at IS NULL",
                (ref_id, net_name),
            ).fetchone()
            if net is None:
                return False
            net_id = int(net[0])
            row = conn.execute(
                "SELECT route_id, topology FROM pcb_routes "
                "WHERE board_id = %s AND net_id = %s",
                (board_id, net_id),
            ).fetchone()
            entries = list(row[1]) if row is not None and row[1] else []
            entries = [
                e
                for e in entries
                if "|".join(sorted((e.get("a", ""), e.get("b", "")))) != key
            ]
            entries.append({"a": a, "b": b, "side": int(side)})
            if row is None:
                conn.execute(
                    "INSERT INTO pcb_routes (board_id, net_id, topology, status) "
                    "VALUES (%s, %s, %s, 'unrouted')",
                    (board_id, net_id, Jsonb(entries)),
                )
            else:
                conn.execute(
                    "UPDATE pcb_routes SET topology = %s, updated_at = now() "
                    "WHERE route_id = %s",
                    (Jsonb(entries), int(row[0])),
                )
        return True

    def pcb_assign_plane(
        self,
        ref_id: int,
        layer_name: str,
        net_name: str,
        *,
        conn: Connection | None = None,
    ) -> int:
        """Author a plane assignment (``pcb_planes``) — the inline "assign
        plane net" editor. Idempotent by (board, layer, net); returns
        plane_id, or 0 when ``net_name`` doesn't resolve.

        Reuses ``conn`` when called inside an existing transaction, the
        same hedge :meth:`pcb_ensure_board` offers: the ``.epro2`` import
        writes the design, the stackup and the planes as ONE unit, because
        a board that got its instances but not its plane assignments would
        silently fall back to :data:`precis.pcb.DEFAULT_STACKUP`'s claim of
        a GND plane on In1.Cu.

        Always stamps ``meta.source = 'authored'`` (merged on conflict,
        never overwriting unrelated meta keys) — a human's explicit
        instruction must never later look like an optimizer guess. If a
        live *derived* row already occupies this (board, layer, net) key,
        promotes it to authored in place — a human claiming a spot the
        optimizer merely guessed at. The reverse (derived silently
        clobbering authored) must never happen — see
        :meth:`pcb_planes_replace_derived`."""
        if conn is not None:
            return self._pcb_assign_plane(conn, ref_id, layer_name, net_name)
        with self._pcb_tx() as c:
            return self._pcb_assign_plane(c, ref_id, layer_name, net_name)

    def _pcb_assign_plane(
        self, conn: Connection, ref_id: int, layer_name: str, net_name: str
    ) -> int:
        board_id = self._pcb_ensure_board(conn, ref_id)
        net = conn.execute(
            "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
            "AND retired_at IS NULL",
            (ref_id, net_name),
        ).fetchone()
        if net is None:
            return 0
        row = conn.execute(
            """
            INSERT INTO pcb_planes (board_id, layer, net_id, meta)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (board_id, layer, net_id) WHERE retired_at IS NULL
            DO UPDATE SET meta = pcb_planes.meta || EXCLUDED.meta
            RETURNING plane_id
            """,
            (board_id, layer_name, int(net[0]), Jsonb({"source": "authored"})),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def pcb_planes_list(self, ref_id: int) -> list[dict[str, Any]]:
        """Every live plane assignment for a design — human-``authored``
        and optimizer-``derived`` (:meth:`pcb_planes_replace_derived`,
        gr267526) both load. Doubles as the ``view='planes'`` read and the
        seed :mod:`precis.workers.job_types.pcb_route` warm-starts a
        fresh anneal from (a prior derived guess is still a reasonable
        start).

        ``source`` defaults ``'authored'`` for a row with no
        ``meta.source`` (pre-this-change rows) — the safe direction,
        since misreading a row as ``'derived'`` would let a later
        replace silently retire a human's instruction."""
        with self._pcb_conn() as conn:
            return [
                {
                    "layer": r[0],
                    "net": r[1],
                    "region_hint": r[2],
                    "source": r[3] or "authored",
                }
                for r in conn.execute(
                    "SELECT pl.layer, n.name, pl.region_hint, pl.meta->>'source' "
                    "FROM pcb_planes pl "
                    "JOIN pcb_boards b ON b.board_id = pl.board_id "
                    "JOIN pcb_nets n ON n.net_id = pl.net_id "
                    "WHERE b.ref_id = %s AND pl.retired_at IS NULL "
                    "ORDER BY pl.layer, n.name",
                    (ref_id,),
                ).fetchall()
            ]

    def pcb_planes_replace_derived(
        self, ref_id: int, board_id: int, assignments: dict[str, str]
    ) -> int:
        """Optimizer-derived plane write-back — the ``pcb_route`` job's
        checkpoint for :meth:`precis.pcb.ir.PcbIR.promote_plane` state
        (gr267526: previously dropped, so GND/VCC-class nets never got a
        plane and threaded as full-length traces instead).

        Provenance-scoped **replace** (same DELETE(retire)+INSERT cascade
        as :meth:`pcb_copper_replace`): retires every existing
        ``source='derived'`` row for this board, then inserts fresh, so a
        re-run replaces rather than accumulates. **Never touches
        ``source='authored'``** — the retire query filters on it, and the
        insert is guarded by ``ON CONFLICT ... DO NOTHING`` as a second
        line of defense; the caller
        (:mod:`precis.workers.job_types.pcb_route`) must still exclude
        authored nets from ``assignments`` via :meth:`pcb_planes_list`'s
        ``source`` field, so an authored net's decision is never silently
        shadowed by a derived one.

        ``assignments`` is ``{net_name: layer_name}``. Returns nets
        attempted (not necessarily inserted)."""
        with self._pcb_tx() as conn:
            conn.execute(
                "UPDATE pcb_planes SET retired_at = now() "
                "WHERE board_id = %s AND retired_at IS NULL "
                "AND coalesce(meta->>'source', 'authored') = 'derived'",
                (board_id,),
            )
            n = 0
            for net_name, layer_name in assignments.items():
                net = conn.execute(
                    "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                    "AND retired_at IS NULL",
                    (ref_id, net_name),
                ).fetchone()
                if net is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO pcb_planes (board_id, layer, net_id, meta)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (board_id, layer, net_id) WHERE retired_at IS NULL
                    DO NOTHING
                    """,
                    (board_id, layer_name, int(net[0]), Jsonb({"source": "derived"})),
                )
                n += 1
        return n

    def pcb_pin_swaps_list(self, ref_id: int) -> list[dict[str, Any]]:
        """Every live pin<->net override for a design (docs/backlog/
        pcb-engine-plan.md "PIN_SWAP is not persisted") — both a future
        human-``authored`` override (no authoring verb yet; ``source``
        discipline wired ahead of it, same shape as
        :meth:`pcb_planes_list`) and optimizer-``derived`` rows
        (:meth:`pcb_pin_swaps_replace_derived`). ``pcb_netconns`` itself
        never changes — this is the override layered on top, resolved by
        durable identity (refdes+pin name, not the ephemeral IR-local pin
        int) so a caller can re-apply it onto a freshly-built IR
        (:mod:`precis.pcb.session`).

        ``source`` defaults ``'authored'`` for a row with no
        ``meta.source``, same safe direction as :meth:`pcb_planes_list`."""
        with self._pcb_conn() as conn:
            return [
                {
                    "refdes": r[0],
                    "pin": r[1],
                    "net": r[2],
                    "source": r[3] or "authored",
                }
                for r in conn.execute(
                    "SELECT i.refdes, p.name, n.name, sw.meta->>'source' "
                    "FROM pcb_pin_swaps sw "
                    "JOIN pcb_boards b ON b.board_id = sw.board_id "
                    "JOIN pcb_instances i ON i.instance_id = sw.instance_id "
                    "JOIN pcb_pins p ON p.pin_id = sw.pin_id "
                    "JOIN pcb_nets n ON n.net_id = sw.net_id "
                    "WHERE b.ref_id = %s AND sw.retired_at IS NULL "
                    "ORDER BY i.refdes, p.name",
                    (ref_id,),
                ).fetchall()
            ]

    def pcb_pin_swaps_replace_derived(
        self, ref_id: int, board_id: int, overrides: list[dict[str, Any]]
    ) -> int:
        """Optimizer-derived pin-swap write-back — the ``pcb_route`` job's
        checkpoint for :meth:`precis.pcb.ir.PcbIR.swap_pins` state
        (docs/backlog/pcb-engine-plan.md "PIN_SWAP is not persisted":
        previously dropped, so a genuinely-beneficial swap reverted to
        ``pcb_netconns``'s original wiring once the job ended, leaving
        persisted netlist and persisted copper describing two different
        boards).

        Provenance-scoped **replace**, same DELETE(retire)+INSERT cascade
        as :meth:`pcb_planes_replace_derived`: retires every existing
        ``source='derived'`` row, inserts fresh. **Never touches
        ``source='authored'``** — the retire filters on it, and the
        insert is guarded by ``ON CONFLICT ... DO NOTHING`` on the live
        physical-pin key; the caller
        (:mod:`precis.workers.job_types.pcb_route`) must still exclude
        authored pins from ``overrides`` via :meth:`pcb_pin_swaps_list`'s
        ``source`` field.

        ``overrides`` is ``[{"refdes", "pin", "net"}, ...]`` — the settled
        net name each pin now carries. An unresolvable ``(refdes, pin)``
        or ``net`` is silently skipped (netlist changed under it), never
        an error. Returns overrides attempted (not necessarily
        inserted)."""
        with self._pcb_tx() as conn:
            conn.execute(
                "UPDATE pcb_pin_swaps SET retired_at = now() "
                "WHERE board_id = %s AND retired_at IS NULL "
                "AND coalesce(meta->>'source', 'authored') = 'derived'",
                (board_id,),
            )
            n = 0
            for entry in overrides:
                pin_row = conn.execute(
                    """
                    SELECT i.instance_id, p.pin_id, i.component_id
                    FROM pcb_instances i
                    JOIN pcb_pins p
                        ON p.component_id = i.component_id AND p.retired_at IS NULL
                    WHERE i.ref_id = %s AND i.retired_at IS NULL
                      AND i.refdes = %s AND p.name = %s
                    """,
                    (ref_id, entry.get("refdes"), entry.get("pin")),
                ).fetchone()
                if pin_row is None:
                    continue
                net = conn.execute(
                    "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                    "AND retired_at IS NULL",
                    (ref_id, entry.get("net")),
                ).fetchone()
                if net is None:
                    continue
                instance_id, pin_id, component_id = (
                    int(pin_row[0]),
                    int(pin_row[1]),
                    int(pin_row[2]),
                )
                conn.execute(
                    """
                    INSERT INTO pcb_pin_swaps
                        (board_id, instance_id, pin_id, component_id, net_id, meta)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instance_id, pin_id) WHERE retired_at IS NULL
                    DO NOTHING
                    """,
                    (
                        board_id,
                        instance_id,
                        pin_id,
                        component_id,
                        int(net[0]),
                        Jsonb({"source": "derived"}),
                    ),
                )
                n += 1
        return n

    def pcb_set_class_rules(
        self,
        ref_id: int,
        name: str,
        rules: dict[str, Any],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Thin single-class wrapper around
        :meth:`pcb_upsert_net_classes` — the inline "set class rules"
        editor's write path. Reuses ``conn`` inside an existing transaction."""
        return self.pcb_upsert_net_classes(ref_id, {name: rules}, conn=conn)

    def pcb_set_stackup(
        self,
        board_id: int,
        stackup: list[dict[str, Any]],
        *,
        conn: Connection | None = None,
    ) -> None:
        """Replace a board's layer stackup — the write half of
        ``put(args={'op':'stackup'})``, the counterpart to the
        :data:`precis.pcb.DEFAULT_STACKUP` every board is BORN with in
        :meth:`_pcb_ensure_board`.

        Dumb on purpose: the entry shape is checked by
        :func:`precis.pcb.ir.validate_stackup` and the "does this strand
        copper already on a layer this drops" question by the handler,
        which is the side that has the design loaded and an error type to
        raise. This method writes what it is handed.

        Reuses ``conn`` when called inside an existing transaction — see
        :meth:`pcb_assign_plane` for why the ``.epro2`` import needs the
        design, the stackup and the planes to land together."""
        if conn is not None:
            self._pcb_set_stackup(conn, board_id, stackup)
            return
        with self._pcb_tx() as c:
            self._pcb_set_stackup(c, board_id, stackup)

    def _pcb_set_stackup(
        self, conn: Connection, board_id: int, stackup: list[dict[str, Any]]
    ) -> None:
        conn.execute(
            "UPDATE pcb_boards SET stackup = %s "
            "WHERE board_id = %s AND retired_at IS NULL",
            (Jsonb(stackup), board_id),
        )

    def pcb_measures_list(self, ref_id: int) -> list[dict[str, Any]]:
        """Live measures of a design. ``measure_id`` and ``meta`` ride along
        for `align` (axis/offset authored in ``meta``; ``meta.snapped`` is
        stamped by :meth:`pcb_measures_mark_snapped`)."""
        with self._pcb_conn() as conn:
            return [
                {
                    "measure_id": int(r[7]),
                    "meta": dict(r[8] or {}),
                    "metric": r[0],
                    "direction": r[1],
                    "goal": r[2],
                    "strength": r[3],
                    "weight": r[4],
                    "operands": list(r[5] or []),
                    "reason": r[6],
                }
                for r in conn.execute(
                    "SELECT metric, direction, goal, strength, weight, operands, "
                    "       reason, measure_id, meta FROM pcb_measures "
                    "WHERE ref_id = %s AND retired_at IS NULL ORDER BY measure_id",
                    (ref_id,),
                ).fetchall()
            ]

    def pcb_measures_mark_snapped(self, ref_id: int, snapped: Iterable[int]) -> int:
        """Stamp ``meta.snapped = true`` on exactly the live `align` measures
        in ``snapped`` and clear it from every other live `align` measure of
        the design — called by the place/route jobs after each optimize, so
        the flag always describes the LAST run (a measure the snap pass did
        not touch this time is not "snapped"). Returns rows now flagged."""
        ids = [int(i) for i in snapped]
        with self._pcb_conn() as conn:
            conn.execute(
                "UPDATE pcb_measures SET meta = meta - 'snapped' "
                "WHERE ref_id = %s AND metric = 'align' AND retired_at IS NULL "
                "  AND meta ? 'snapped'",
                (ref_id,),
            )
            if not ids:
                return 0
            cur = conn.execute(
                "UPDATE pcb_measures "
                "SET meta = meta || jsonb_build_object('snapped', true) "
                "WHERE ref_id = %s AND metric = 'align' AND retired_at IS NULL "
                "  AND measure_id = ANY(%s)",
                (ref_id, ids),
            )
            return int(cur.rowcount)

    def pcb_features_list(self, ref_id: int) -> list[dict[str, Any]]:
        """Live non-electrical features of a design — the
        board outline + mounting holes the mechanical exporter / the 0041
        enclosure bridge consume."""
        with self._pcb_conn() as conn:
            return [
                {
                    "feature_id": int(r[0]),
                    "ftype": r[1],
                    "x": r[2],
                    "y": r[3],
                    "rot": r[4],
                    "layer": r[5],
                    "fixed": r[6],
                    "geom": r[7],
                    "note": r[8],
                }
                for r in conn.execute(
                    "SELECT feature_id, ftype, x, y, rot, layer, fixed, geom, note "
                    "FROM pcb_features "
                    "WHERE ref_id = %s AND retired_at IS NULL ORDER BY feature_id",
                    (ref_id,),
                ).fetchall()
            ]

    def pcb_footprints_for(self, ref_id: int) -> dict[str, dict[str, Any]]:
        """Cached Flow-B footprints (pads + pin_map) keyed by C-number for every
        part the design's instances reference. The DSN exporter (§6) uses real
        pad geometry where present and falls back to centroid pins otherwise."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT f.lcsc, f.pads, f.pin_map, f.courtyard, f.centroid, f.holes "
                "FROM part_footprints f "
                "WHERE f.lcsc IN ("
                "  SELECT DISTINCT c.part_lcsc FROM pcb_instances i "
                "  JOIN pcb_components c ON c.component_id = i.component_id "
                "  WHERE i.ref_id = %s AND i.retired_at IS NULL "
                "    AND c.part_lcsc IS NOT NULL)",
                (ref_id,),
            ).fetchall()
        return {
            str(r[0]): {
                "pads": r[1],
                "pin_map": r[2],
                "courtyard": r[3],
                "centroid": r[4],
                "holes": r[5] or [],
            }
            for r in rows
        }

    def _pcb_local_footprint_upsert(
        self, conn: Connection, ref_id: int, name: str, data: dict[str, Any]
    ) -> None:
        """The write half of a design-local footprint (pcb-ewod-multitile
        Slice 1) — an upsert by ``(ref_id, name)``, given an already-open
        ``conn`` (shared by :meth:`pcb_local_footprint_put`'s own-tx
        wrapper and :meth:`_pcb_apply`'s batch)."""
        conn.execute(
            """
            INSERT INTO pcb_local_footprints
                (ref_id, name, pads, pin_map, courtyard, centroid, note, holes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ref_id, name) DO UPDATE SET
                pads=EXCLUDED.pads, pin_map=EXCLUDED.pin_map,
                courtyard=EXCLUDED.courtyard, centroid=EXCLUDED.centroid,
                note=EXCLUDED.note, holes=EXCLUDED.holes
            """,
            (
                ref_id,
                name,
                Jsonb(data["pads"]),
                _jsonb_or_none(data.get("pin_map")),
                _jsonb_or_none(data.get("courtyard")),
                _jsonb_or_none(data.get("centroid")),
                data.get("note"),
                Jsonb(data.get("holes") or []),
            ),
        )

    def pcb_local_footprint_put(
        self, ref_id: int, name: str, data: dict[str, Any]
    ) -> None:
        """Upsert one design-local footprint outside a batch ``put`` — same
        row shape :meth:`pcb_local_footprints_for` reads back, own
        transaction. :meth:`_pcb_apply`'s ``footprints`` block is the
        normal authoring path; this is here for a caller that already has
        a normalized ``{pads, pin_map, courtyard, centroid}`` dict (a
        generator, in a later slice)."""
        with self._pcb_tx() as conn:
            self._pcb_local_footprint_upsert(conn, ref_id, name, data)

    def pcb_designs_using_part(
        self, lcsc: str, *, exclude_ref_id: int | None = None
    ) -> list[str]:
        """Slugs of the live pcb designs with a live instance of C-number
        ``lcsc`` (the footprint cache is catalogue-wide), ``exclude_ref_id``
        left out. Sorted."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ri.id_value FROM refs r "
                "JOIN ref_identifiers ri "
                "  ON ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key' "
                "JOIN pcb_instances i ON i.ref_id = r.ref_id "
                "JOIN pcb_components c ON c.component_id = i.component_id "
                "WHERE r.kind = 'pcb' AND r.retired_at IS NULL "
                "  AND i.retired_at IS NULL "
                "  AND upper(btrim(coalesce(c.part_lcsc, ''))) = %s "
                "  AND r.ref_id <> %s "
                "ORDER BY ri.id_value",
                (lcsc.strip().upper(), exclude_ref_id if exclude_ref_id else -1),
            ).fetchall()
        return [str(r[0]) for r in rows]

    def pcb_local_footprints_for(self, ref_id: int) -> dict[str, dict[str, Any]]:
        """Every design-local footprint (pcb-ewod-multitile Slice 1),
        keyed by name — the local-authoring counterpart to
        :meth:`pcb_footprints_for`'s C-number-keyed catalog cache. Not
        filtered to footprints an instance currently references (unlike
        that method): this table is already ref_id-scoped and small, and
        an unreferenced-but-authored footprint should still show up for
        inspection."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT name, pads, pin_map, courtyard, centroid, holes "
                "FROM pcb_local_footprints WHERE ref_id = %s",
                (ref_id,),
            ).fetchall()
        return {
            str(r[0]): {
                "pads": r[1],
                "pin_map": r[2],
                "courtyard": r[3],
                "centroid": r[4],
                "holes": r[5] or [],
            }
            for r in rows
        }

    # -- generators (pcb-ewod-multitile Slice 2) -------------------------
    def _pcb_generator_row(
        self, conn: Connection, ref_id: int, name: str
    ) -> dict[str, Any] | None:
        """The stored identity of generator ``name`` (design-local), if
        any — what :meth:`_pcb_apply` diffs a fresh
        :class:`precis.pcb.generators.GeneratorExpansion` against to
        decide no-op vs. retire-and-reinsert."""
        row = conn.execute(
            "SELECT name, generator, version, params, refdes, ledger "
            "FROM pcb_generators WHERE ref_id = %s AND name = %s",
            (ref_id, name),
        ).fetchone()
        if row is None:
            return None
        return {
            "name": row[0],
            "generator": row[1],
            "version": int(row[2]),
            "params": row[3],
            "refdes": row[4],
            "ledger": row[5],
        }

    def _pcb_generator_upsert(
        self,
        conn: Connection,
        ref_id: int,
        name: str,
        generator: str,
        version: int,
        params: dict[str, Any],
        refdes: str,
        ledger: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO pcb_generators
                (ref_id, name, generator, version, params, refdes, ledger,
                 updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (ref_id, name) DO UPDATE SET
                generator=EXCLUDED.generator, version=EXCLUDED.version,
                params=EXCLUDED.params, refdes=EXCLUDED.refdes,
                ledger=EXCLUDED.ledger, updated_at=now()
            """,
            (ref_id, name, generator, version, Jsonb(params), refdes, Jsonb(ledger)),
        )

    def _pcb_generator_poses(
        self, conn: Connection, ref_id: int, refdes: str, generator_name: str
    ) -> dict[str, dict[str, Any]]:
        """Live ``{refdes: {x, y, rot, layer}}`` for the instances a
        generator expansion owns — the same ``refdes = <refdes> OR refdes
        LIKE '<generator_name>_%'`` ownership convention
        :meth:`_pcb_generator_retire_expansion` retires by (the expansion's
        own component plus its ``{name}_SINK_{i}``-style secondaries). The
        caller folds these onto a re-expansion's emitted components so a
        re-apply keeps the placement the placer/user committed instead of
        resetting every instance to the emitted seed pose (gr481705 — the
        ``ewod_pad_array`` sink centroid seed can itself be a pose the
        always-valid gate refuses)."""
        rows = conn.execute(
            "SELECT refdes, x, y, rot, layer FROM pcb_instances "
            "WHERE ref_id = %s AND retired_at IS NULL "
            "AND (refdes = %s OR refdes LIKE %s)",
            (ref_id, refdes, generator_name + "\\_%"),
        ).fetchall()
        return {
            str(r[0]): {"x": r[1], "y": r[2], "rot": r[3], "layer": r[4]} for r in rows
        }

    def _pcb_generator_retire_expansion(
        self, conn: Connection, ref_id: int, refdes: str, generator_name: str
    ) -> None:
        """Undo a PREVIOUS apply of generator ``generator_name`` before
        its changed-params re-expansion is inserted: retires the
        component/instance/pins it owns (freeing ``refdes`` — both carry
        a partial-unique index ``WHERE retired_at IS NULL``), hard-deletes
        its netconns (``pcb_netconns`` has no ``retired_at`` — "re-wire =
        delete+insert" is its own table comment), retires the nets it
        SOLELY owns (named ``f"{name}_..."`` — see :func:`precis.pcb.
        generators._expand_ewod_pad_array`'s net-naming — but NEVER one a
        foreign live instance is still connected to; see that query's own
        comment and gr451046), and retires its
        mask_open feature (tagged ``note='generator:{name}'`` at emission
        time, the same free-text-column-doubles-as-a-tag convention
        ``layer`` already uses for a mask_open region's side).

        Retires every instance whose refdes is EITHER the generator's own
        (exact match, e.g. the ``ewod_pad_array`` component itself) OR
        prefixed ``f"{generator_name}_"`` — the same LIKE convention the
        net-retirement query below already uses, generalised to
        instances/components/pins too (round 7, ``sink_grid``): a
        generator that emits SECONDARY components under its own name
        (``ewod_pad_array``'s ``{name}_SINK_{i}`` bottom-side sink
        instances, chain-index ``i``) would otherwise orphan them on a
        changed-params re-apply — the exact-refdes-only query above only
        ever retired the array's own single component, never the sinks it
        also created, so a sink whose channel wiring changed (e.g. a
        different ``channels_per_sink``) would keep its STALE pins/
        netconns forever while its own escape nets got silently retired
        out from under it by the net-LIKE query below."""
        rows = conn.execute(
            "SELECT instance_id, component_id FROM pcb_instances "
            "WHERE ref_id = %s AND retired_at IS NULL "
            "AND (refdes = %s OR refdes LIKE %s)",
            (ref_id, refdes, generator_name + "\\_%"),
        ).fetchall()
        for row in rows:
            instance_id, component_id = int(row[0]), int(row[1])
            conn.execute(
                "DELETE FROM pcb_netconns WHERE instance_id = %s", (instance_id,)
            )
            conn.execute(
                "UPDATE pcb_pins SET retired_at = now() "
                "WHERE component_id = %s AND retired_at IS NULL",
                (component_id,),
            )
            conn.execute(
                "UPDATE pcb_instances SET retired_at = now() WHERE instance_id = %s",
                (instance_id,),
            )
            conn.execute(
                "UPDATE pcb_components SET retired_at = now() WHERE component_id = %s",
                (component_id,),
            )
        # A net the generator declares but SOMEONE ELSE also joined is NOT
        # the generator's to retire (gr451046). `pcb_netconns` has no
        # `retired_at` — a connection is live exactly while its instance and
        # its net are — and the DELETE above only clears connections for
        # instances this generator owns. So retiring such a net by name left
        # a foreign component's connection pointing into the graveyard,
        # while the re-expansion minted a fresh net under the SAME NAME that
        # only the generator's own connections joined. Net identity is
        # `net_id`; treating the NAME as recreatable silently disconnected
        # real authored work — on `ewod-dogfood-2` it severed the top-plate
        # terminal and the serial header, and `view='route-status'` then
        # reported both in the *realized* column as "dangling net, nothing
        # to route".
        #
        # By the time this runs, this generator's own instances are retired
        # and their connections deleted, so any net still carrying a
        # connection from a LIVE instance is by construction joined by
        # something the generator does not own. Leave those alone: the
        # re-expansion's insert path resolves a net BY NAME (the same reason
        # a re-apply extends the array component rather than duplicating
        # it), so the generator's new connections land on the very row the
        # foreign component is still sitting on.
        conn.execute(
            "UPDATE pcb_nets SET retired_at = now() "
            "WHERE ref_id = %s AND name LIKE %s AND retired_at IS NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM pcb_netconns nc "
            "  JOIN pcb_instances i ON i.instance_id = nc.instance_id "
            "  WHERE nc.net_id = pcb_nets.net_id AND i.retired_at IS NULL"
            ")",
            (ref_id, generator_name + "\\_%"),
        )
        conn.execute(
            "UPDATE pcb_features SET retired_at = now() "
            "WHERE ref_id = %s AND note = %s AND retired_at IS NULL",
            (ref_id, f"generator:{generator_name}"),
        )
        # gr345859 §F — the generator's declared invariant measure rows,
        # keyed by `meta.generator` (emitted with it at expansion time).
        # Same identity discipline as the mask_open feature above: a
        # changed-params re-apply replaces them wholesale.
        conn.execute(
            "UPDATE pcb_measures SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL "
            "AND meta ->> 'generator' = %s",
            (ref_id, generator_name),
        )
        # pcb-pre-place-route-blocks Slice 1 -- a changed-params re-apply
        # must retire this generator's authored fixed copper too, the same
        # "the previous expansion's rows never survive under stale params"
        # discipline as everything else this method retires.
        self._pcb_fixed_copper_retire(conn, ref_id, generator_name)

    def pcb_generators_for(self, ref_id: int) -> dict[str, dict[str, Any]]:
        """Every generator call authored on this design, keyed by its
        design-local ``name`` — ``{generator, version, params, refdes,
        ledger}`` each, the same shape :meth:`_pcb_generator_row` reads
        inside a transaction (this is the outside-a-batch read-back,
        e.g. for a capability-map view)."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT name, generator, version, params, refdes, ledger "
                "FROM pcb_generators WHERE ref_id = %s ORDER BY name",
                (ref_id,),
            ).fetchall()
        return {
            str(r[0]): {
                "generator": r[1],
                "version": int(r[2]),
                "params": r[3],
                "refdes": r[4],
                "ledger": r[5],
            }
            for r in rows
        }

    # -- parts catalog ------------------------------------
    def parts_import(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Upsert normalized catalog rows (:func:`precis.pcb.catalog.
        normalize_jlcparts_row`) + update the turnover signal. Returns
        ``{upserted, restocked}``.

        Upsert (not the atomic swap) keeps the table live and the FK-free
        ``part_footprints`` / ``part_availability`` caches intact; the
        staging + atomic-swap (:meth:`parts_bulk_replace`) is the scale
        lever for the full dump (v1 ~300k, v2 ~2M rows) (the PCB netlist+placement IR —
        "drop-index trick optional at our row count").

        Nullable descriptive columns merge (``COALESCE`` with the stored
        value): a thinner source — the parts-library listing
        (:mod:`precis.pcb.jlc_library`) carries no height and sometimes no
        datasheet — refreshes stock/price/basic without blanking what the
        dump recorded (gr464537)."""
        counts = {"upserted": 0, "restocked": 0}
        with self._pcb_tx() as conn:
            for r in rows:
                lcsc = r["lcsc"]
                new_stock = int(r.get("stock") or 0)
                if self._parts_update_availability(conn, lcsc, new_stock):
                    counts["restocked"] += 1
                conn.execute(
                    """
                    INSERT INTO parts
                        (lcsc, mfr, mfr_part, description, jlcpcb_assemblable,
                         basic, stock, price, package, height_mm, params,
                         datasheet_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (lcsc) DO UPDATE SET
                        mfr=COALESCE(EXCLUDED.mfr, parts.mfr),
                        mfr_part=COALESCE(EXCLUDED.mfr_part, parts.mfr_part),
                        description=COALESCE(NULLIF(EXCLUDED.description, ''),
                                             parts.description),
                        jlcpcb_assemblable=EXCLUDED.jlcpcb_assemblable,
                        basic=EXCLUDED.basic, stock=EXCLUDED.stock,
                        price=COALESCE(EXCLUDED.price, parts.price),
                        package=COALESCE(EXCLUDED.package, parts.package),
                        height_mm=COALESCE(EXCLUDED.height_mm, parts.height_mm),
                        params=COALESCE(EXCLUDED.params, parts.params),
                        datasheet_url=COALESCE(EXCLUDED.datasheet_url,
                                               parts.datasheet_url),
                        refreshed_at=now()
                    """,
                    (
                        lcsc,
                        r.get("mfr"),
                        r.get("mfr_part"),
                        r.get("description") or "",
                        bool(r.get("jlcpcb_assemblable", True)),
                        bool(r.get("basic", False)),
                        new_stock,
                        _jsonb_or_none(r.get("price")),
                        r.get("package"),
                        r.get("height_mm"),
                        _jsonb_or_none(r.get("params")),
                        r.get("datasheet_url"),
                    ),
                )
                counts["upserted"] += 1
        return counts

    def parts_bulk_replace(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        min_fraction: float = 0.5,
        force: bool = False,
    ) -> dict[str, int]:
        """Full-catalog reload via staging + atomic swap (0047's design,
        see that migration's header) — the scale lever for the whole
        ~300k-row jlcparts dump, vs :meth:`parts_import`'s per-row upsert
        (right for an incremental API page, wrong for a full reload).
        ``rows`` may be a one-shot generator, consumed while loading
        staging, so a full dump never needs to fit in memory as a list.

        Everything — staging creation, every row load, the
        ``part_availability`` turnover roll, and the swap — runs inside
        ONE transaction: a raise anywhere leaves the live ``parts`` table
        untouched, since the swap (drop old, promote staging) is the LAST
        step. ``part_footprints``/``part_availability`` are deliberately
        FK-free (0047), never touched by the swap, and survive it
        unconditionally; this still rolls ``part_availability``'s
        turnover signal per row like :meth:`parts_import`.

        **Shrink guard (do not remove).** Staging loading far fewer rows
        than the live catalog is almost never a real shrink — it's a
        truncated download, a drifted-schema normalization failure, or an
        early-died API walk. Promoting it silently destroys the catalog,
        and the transaction can't save us (the empty load is a
        *successful* one). So the swap refuses unless staging holds ≥
        ``min_fraction`` of the live row count, and refuses empty staging
        outright. ``force=True`` overrides for a genuine teardown; the
        CLI surfaces it as an explicit flag so nobody trips it by
        accident.

        Returns ``{loaded, restocked}``.
        """
        counts = {"loaded": 0, "restocked": 0}
        # A local (not module-level) name, not a string literal directly in
        # the .execute() call — deliberately so
        # tests/test_sql_schema_drift.py's static AST extractor can't
        # statically resolve the INSERT below and counts it as
        # skipped-dynamic rather than EXPLAIN-ing it against the migrated
        # schema, where a scratch table created+dropped inside this one
        # transaction (never a real migrated object) would false-positive
        # as "relation does not exist". Same idiom
        # :func:`precis.pcb.catalog.read_jlcparts_sqlite` already uses for
        # its dynamic column list.
        staging = "parts_staging"
        with self._pcb_tx() as conn:
            conn.execute(f"DROP TABLE IF EXISTS {staging}")
            conn.execute(f"CREATE TABLE {staging} (LIKE parts INCLUDING ALL)")
            it = iter(rows)
            while batch := list(islice(it, _BULK_BATCH)):
                counts["restocked"] += self._parts_update_availability_many(
                    conn, [(r["lcsc"], int(r.get("stock") or 0)) for r in batch]
                )
                with conn.cursor() as cur:
                    cur.executemany(
                        f"""
                        INSERT INTO {staging}
                            (lcsc, mfr, mfr_part, description,
                             jlcpcb_assemblable, basic, stock, price, package,
                             height_mm, params, datasheet_url)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        [
                            (
                                r["lcsc"],
                                r.get("mfr"),
                                r.get("mfr_part"),
                                r.get("description") or "",
                                bool(r.get("jlcpcb_assemblable", True)),
                                bool(r.get("basic", False)),
                                int(r.get("stock") or 0),
                                _jsonb_or_none(r.get("price")),
                                r.get("package"),
                                r.get("height_mm"),
                                _jsonb_or_none(r.get("params")),
                                r.get("datasheet_url"),
                            )
                            for r in batch
                        ],
                    )
                counts["loaded"] += len(batch)
            # Shrink guard — see the docstring. A clean-but-empty load is
            # the dangerous case precisely because nothing raised.
            live = conn.execute("SELECT count(*) FROM parts").fetchone()
            live_n = int(live[0]) if live else 0
            floor = int(live_n * min_fraction)
            if not force and (counts["loaded"] == 0 or counts["loaded"] < floor):
                raise ValueError(
                    f"refusing to swap: staging loaded {counts['loaded']} row(s) "
                    f"but the live catalog holds {live_n} (floor {floor} = "
                    f"{min_fraction:.0%}). A shrunken load is almost always a "
                    "truncated dump, drifted upstream column names, or a walk "
                    "that died early — not a catalog that really shrank. Pass "
                    "force=True (CLI: --allow-shrink) if this is intended."
                )
            # The atomic swap: promote the fully-loaded staging table into
            # ``parts``'s place. Reached only if every row above loaded
            # clean — an exception anywhere in the loop propagates out of
            # ``self.tx()`` and rolls back the whole transaction, DDL
            # included, so ``parts`` is never dropped on a failed load.
            conn.execute("DROP TABLE parts")
            conn.execute(f"ALTER TABLE {staging} RENAME TO parts")
            # ``LIKE ... INCLUDING ALL`` names the cloned indexes after the
            # STAGING table, and RENAME TO does not rename a table's own
            # indexes — so without this, 0047's deliberately-named
            # parts_select_idx / parts_tsv_gin / parts_params_gin are gone
            # after the first reload, and each later reload accumulates
            # another auto-suffixed variant. Rename them back so the live
            # table's index names stay the ones the migration documents.
            for (idx_name,) in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'parts' AND indexname LIKE %s",
                (f"{staging}%",),
            ).fetchall():
                canonical = "parts" + idx_name[len(staging) :]
                conn.execute(f'ALTER INDEX "{idx_name}" RENAME TO "{canonical}"')
            conn.execute(
                "COMMENT ON TABLE parts IS "
                "'LCSC/JLCPCB catalog (ADR 0042 §5, Flow A) — bulk from "
                "the jlcparts dump via staging + atomic swap. NO inbound "
                "FK (the swap drops the table).'"
            )
        return counts

    def _parts_update_availability_many(
        self, conn: Connection, items: list[tuple[str, int]]
    ) -> int:
        """Batched :meth:`_parts_update_availability` (same turnover maths):
        one SELECT + two ``executemany`` per batch instead of ~3 round trips
        per part — what makes a ~2M-row reload tractable. A duplicate lcsc
        inside one batch rolls forward sequentially. Returns restock count."""
        prev = {
            r[0]: (int(r[1] or 0), float(r[2] or 0.0))
            for r in conn.execute(
                "SELECT lcsc, stock_now, ewma_stock FROM part_availability "
                "WHERE lcsc = ANY(%s)",
                ([lcsc for lcsc, _ in items],),
            ).fetchall()
        }
        inserts: dict[str, tuple[Any, ...]] = {}
        updates: list[tuple[Any, ...]] = []
        restocked = 0
        for lcsc, new_stock in items:
            if lcsc not in prev:
                inserts[lcsc] = (lcsc, new_stock, new_stock, float(new_stock))
                prev[lcsc] = (new_stock, float(new_stock))
                continue
            old_stock, old_ewma = prev[lcsc]
            up = new_stock > old_stock
            restocked += up
            ewma = 0.7 * old_ewma + 0.3 * new_stock
            if lcsc in inserts:  # first seen earlier in this same batch
                inserts[lcsc] = (lcsc, new_stock, old_stock, ewma)
            else:
                updates.append(
                    (new_stock, ewma, new_stock - old_stock, 1 if up else 0, up, lcsc)
                )
            prev[lcsc] = (new_stock, ewma)
        with conn.cursor() as cur:
            if inserts:
                cur.executemany(
                    "INSERT INTO part_availability "
                    "(lcsc, stock_now, stock_prev, ewma_stock, restock_count, "
                    "trend) VALUES (%s, %s, %s, %s, 0, 0)",
                    list(inserts.values()),
                )
            if updates:
                cur.executemany(
                    "UPDATE part_availability SET stock_prev = stock_now, "
                    "stock_now = %s, ewma_stock = %s, trend = %s, "
                    "restock_count = restock_count + %s, "
                    "last_restock_at = CASE WHEN %s THEN now() "
                    "ELSE last_restock_at END, "
                    "discontinued = false, updated_at = now() WHERE lcsc = %s",
                    updates,
                )
        return restocked

    def _parts_update_availability(
        self, conn: Connection, lcsc: str, new_stock: int
    ) -> bool:
        """Roll the turnover signal for one part; returns True if restocked
        (stock rose vs the previous dump)."""
        prev = conn.execute(
            "SELECT stock_now, ewma_stock FROM part_availability WHERE lcsc = %s",
            (lcsc,),
        ).fetchone()
        if prev is None:
            conn.execute(
                "INSERT INTO part_availability "
                "(lcsc, stock_now, stock_prev, ewma_stock, restock_count, trend) "
                "VALUES (%s, %s, %s, %s, 0, 0)",
                (lcsc, new_stock, new_stock, float(new_stock)),
            )
            return False
        old_stock = int(prev[0] or 0)
        old_ewma = float(prev[1] or 0.0)
        restocked = new_stock > old_stock
        conn.execute(
            "UPDATE part_availability SET stock_prev = stock_now, stock_now = %s, "
            "ewma_stock = %s, trend = %s, restock_count = restock_count + %s, "
            "last_restock_at = CASE WHEN %s THEN now() ELSE last_restock_at END, "
            "discontinued = false, updated_at = now() WHERE lcsc = %s",
            (
                new_stock,
                0.7 * old_ewma + 0.3 * new_stock,
                new_stock - old_stock,
                1 if restocked else 0,
                restocked,
                lcsc,
            ),
        )
        return restocked

    def parts_search(self, q: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """The JLCPCB-native selector: hard-filter to assemblable
        parts; rank Basic-first then **turnover** (restock frequency + healthy
        EWMA stock) — prefer parts that keep being available, not the last reel.
        """
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT p.lcsc, p.mfr_part, p.description, p.basic, p.stock, "
                "       p.package, p.price, coalesce(a.restock_count, 0), "
                "       a.ewma_stock "
                "FROM parts p LEFT JOIN part_availability a ON a.lcsc = p.lcsc "
                "WHERE p.jlcpcb_assemblable "
                "  AND p.description_tsv @@ plainto_tsquery('english', %s) "
                "ORDER BY p.basic DESC, coalesce(a.restock_count, 0) DESC, "
                "         coalesce(a.ewma_stock, p.stock, 0) DESC "
                "LIMIT %s",
                (q, limit),
            ).fetchall()
        return [
            {
                "lcsc": r[0],
                "mfr_part": r[1],
                "description": r[2],
                "basic": bool(r[3]),
                "stock": r[4],
                "package": r[5],
                "price": r[6],
                "restock_count": int(r[7]),
                "ewma_stock": r[8],
            }
            for r in rows
        ]

    def part_row(self, lcsc: str) -> dict[str, Any] | None:
        with self._pcb_conn() as conn:
            r = conn.execute(
                "SELECT p.lcsc, p.mfr, p.mfr_part, p.description, "
                "       p.jlcpcb_assemblable, p.basic, p.stock, p.package, "
                "       p.height_mm, p.datasheet_url, a.restock_count, a.ewma_stock "
                "FROM parts p LEFT JOIN part_availability a ON a.lcsc = p.lcsc "
                "WHERE p.lcsc = %s",
                (lcsc,),
            ).fetchone()
        if r is None:
            return None
        return {
            "lcsc": r[0],
            "mfr": r[1],
            "mfr_part": r[2],
            "description": r[3],
            "jlcpcb_assemblable": bool(r[4]),
            "basic": bool(r[5]),
            "stock": r[6],
            "package": r[7],
            "height_mm": r[8],
            "datasheet_url": r[9],
            "restock_count": r[10],
            "ewma_stock": r[11],
        }

    def pcb_bom_components(self, ref_id: int) -> list[dict[str, Any]]:
        """The design's component TYPES with their instance counts and the
        catalog parametrics — the rows the bom-view consolidation nudge
        groups (migration 0205). ``params``/``package``/``datasheet_url``
        come off ``parts`` by the loose ``part_lcsc`` join; a hand-entered
        component (no LCSC) gets ``params=None``."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT c.component_id, c.label, c.part_lcsc, c.footprint, "
                "       count(i.instance_id) AS qty, "
                "       p.params, p.package, p.datasheet_url, p.description "
                "FROM pcb_components c "
                "JOIN pcb_instances i ON i.component_id = c.component_id "
                "                    AND i.retired_at IS NULL "
                "LEFT JOIN parts p ON p.lcsc = c.part_lcsc "
                "WHERE c.ref_id = %s AND c.retired_at IS NULL "
                "GROUP BY c.component_id, c.label, c.part_lcsc, c.footprint, "
                "         p.params, p.package, p.datasheet_url, p.description "
                "ORDER BY min(i.refdes)",
                (ref_id,),
            ).fetchall()
        return [
            {
                "component_id": int(r[0]),
                "label": r[1],
                "part_lcsc": r[2],
                "footprint": r[3],
                "qty": int(r[4]),
                "params": r[5],
                "package": r[6],
                "datasheet_url": r[7],
                "description": r[8],
            }
            for r in rows
        ]

    def pcb_bom_availability(self, ref_id: int) -> list[dict[str, Any]]:
        """Per component TYPE: its C-number, refdes set, qty, and the
        catalog/stock/lifecycle facts the ``view='availability'`` EOL sweep
        renders (gr485250). A hand-entered component (no ``part_lcsc``)
        still lists — its catalog columns are NULL. ``params`` carries the
        live-listing ``_lifecycle``/``_lcsc_url`` keys when a live row wrote
        them; ``discontinued`` is ``part_availability``'s absent-from-dump
        flag."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT c.component_id, c.label, c.part_lcsc, "
                "       array_agg(i.refdes ORDER BY i.refdes), "
                "       count(i.instance_id), "
                "       p.mfr_part, p.basic, p.stock, p.price, p.params, "
                "       p.refreshed_at, "
                "       a.discontinued, a.ewma_stock, a.restock_count "
                "FROM pcb_components c "
                "JOIN pcb_instances i ON i.component_id = c.component_id "
                "                    AND i.retired_at IS NULL "
                "LEFT JOIN parts p ON p.lcsc = c.part_lcsc "
                "LEFT JOIN part_availability a ON a.lcsc = c.part_lcsc "
                "WHERE c.ref_id = %s AND c.retired_at IS NULL "
                "GROUP BY c.component_id, c.label, c.part_lcsc, "
                "         p.mfr_part, p.basic, p.stock, p.price, p.params, "
                "         p.refreshed_at, a.discontinued, a.ewma_stock, "
                "         a.restock_count "
                "ORDER BY min(i.refdes)",
                (ref_id,),
            ).fetchall()
        return [
            {
                "component_id": int(r[0]),
                "label": r[1],
                "part_lcsc": r[2],
                "refdes": [str(x) for x in (r[3] or [])],
                "qty": int(r[4]),
                "mfr_part": r[5],
                "basic": r[6],
                "stock": r[7],
                "price": r[8],
                "params": r[9],
                "refreshed_at": r[10],
                "discontinued": bool(r[11]) if r[11] is not None else False,
                "ewma_stock": r[12],
                "restock_count": r[13],
            }
            for r in rows
        ]

    def pcb_part_design_count(
        self,
        *,
        exclude_ref_id: int,
        part_lcsc: str | None,
        label: str | None,
        footprint: str | None,
    ) -> int:
        """How many OTHER live pcb designs use this part — the pcb half of
        the nudge's "used in N other designs" ranking signal. Keyed on the
        catalog C-number when there is one, else (label, footprint)."""
        with self._pcb_conn() as conn:
            row = conn.execute(
                "SELECT count(DISTINCT c.ref_id) FROM pcb_components c "
                "JOIN refs r ON r.ref_id = c.ref_id AND r.retired_at IS NULL "
                "WHERE c.retired_at IS NULL AND c.ref_id <> %s AND ("
                "  (%s::text IS NOT NULL AND c.part_lcsc = %s) OR "
                "  (%s::text IS NULL AND c.part_lcsc IS NULL "
                "   AND c.label = %s AND c.footprint IS NOT DISTINCT FROM %s))",
                (exclude_ref_id, part_lcsc, part_lcsc, part_lcsc, label, footprint),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def part_ref_id(self, lcsc: str, *, conn: Connection | None = None) -> int | None:
        """The live part ref for an LCSC C-number, or ``None``. Never mints."""

        def _do(c: Connection) -> int | None:
            row = c.execute(
                "SELECT ri.ref_id FROM ref_identifiers ri "
                "JOIN refs r ON r.ref_id = ri.ref_id "
                "WHERE ri.id_kind = 'lcsc' AND ri.id_value = %s "
                "  AND r.kind = 'part' AND r.retired_at IS NULL",
                (lcsc.strip().upper(),),
            ).fetchone()
            return int(row[0]) if row is not None else None

        if conn is not None:
            return _do(conn)
        with self._pcb_conn() as c:
            return _do(c)

    def ensure_part_ref(
        self,
        lcsc: str,
        *,
        set_by: ActorSlug = "agent",
        uncatalogued_ok: bool = False,
        conn: Connection | None = None,
    ) -> int:
        """Find-or-mint the lazy ``part`` ref for a catalog C-number.

        A part ref is ``refs(kind='part', slug=<C-no>)`` plus a
        ``ref_identifiers('lcsc', <C-no>)`` row, chunkless (the embed and
        summary cascade never sees it), titled from the catalog row at
        mint time (``precis.handlers.part`` docstring). An existing live ref
        is returned whether or not its row is still in ``parts``; a mint
        needs the row and raises :class:`NotFound` without it, unless
        ``uncatalogued_ok`` (the ``datasheet_pull`` job, which must record a
        failed pull on a part the catalog does not hold) titles it by the
        C-number alone.

        Race: two first links both miss the probe and both insert; the
        ``lcsc`` identifier's primary key serialises them, the loser's
        insert claims nothing, its savepoint rolls back and it re-probes
        onto the winner. A retired owner's identifier is reclaimed, as
        :meth:`insert_ref` does for ``cite_key``.
        """
        key = lcsc.strip().upper()

        class _Lost(Exception):
            pass

        def _do(c: Connection) -> int:
            hit = self.part_ref_id(key, conn=c)
            if hit is not None:
                return hit
            row = c.execute(
                "SELECT mfr_part, description FROM parts WHERE lcsc = %s", (key,)
            ).fetchone()
            if row is None and not uncatalogued_ok:
                raise NotFound(
                    f"part {key} is not in the catalog and has no ref",
                    next="search(kind='part', q='...') for a catalog C-number",
                )
            title = (
                " — ".join(s for s in (row[0], row[1]) if s) if row is not None else ""
            ) or key
            try:
                with c.transaction():
                    ref = self.insert_ref(
                        kind="part",
                        slug=key,
                        title=title,
                        meta={"set_by": set_by}
                        | ({"uncatalogued": True} if row is None else {}),
                        conn=c,
                    )
                    claimed = c.execute(
                        "INSERT INTO ref_identifiers "
                        "(id_kind, id_value, ref_id, source) "
                        "VALUES ('lcsc', %s, %s, %s) "
                        "ON CONFLICT (id_kind, id_value) DO UPDATE "
                        "  SET ref_id = EXCLUDED.ref_id, source = EXCLUDED.source "
                        "  WHERE EXISTS (SELECT 1 FROM refs r "
                        "    WHERE r.ref_id = ref_identifiers.ref_id "
                        "      AND r.retired_at IS NOT NULL) "
                        "RETURNING ref_id",
                        (key, ref.id, set_by),
                    ).fetchone()
                    if claimed is None:
                        raise _Lost
                    return int(ref.id)
            except _Lost:
                won = self.part_ref_id(key, conn=c)
                assert won is not None, f"lcsc {key} claimed by no live part ref"
                return won

        if conn is not None:
            return _do(conn)
        with self._pcb_conn() as c:
            return _do(c)

    def part_footprint_get(self, lcsc: str) -> dict[str, Any] | None:
        """The Flow B EasyEDA cache row for a C-number, or None. ``escape``
        is the precomputed footprint escape graph (pcb-guided-place-route
        Slice 5, `precis.pcb.escape.compute_escape_graph`) — footprint-
        intrinsic, so it round-trips here rather than in any board-scoped
        table."""
        with self._pcb_conn() as conn:
            r = conn.execute(
                "SELECT pads, pin_map, courtyard, centroid, kicad_mod, source, raw, escape, holes "
                "FROM part_footprints WHERE lcsc = %s",
                (lcsc,),
            ).fetchone()
        if r is None:
            return None
        return {
            "lcsc": lcsc,
            "pads": r[0],
            "pin_map": r[1],
            "courtyard": r[2],
            "centroid": r[3],
            "kicad_mod": r[4],
            "source": r[5],
            "raw": r[6],
            "escape": r[7],
            "holes": r[8] or [],
        }

    def part_footprints_many(
        self, lcscs: list[str] | set[str]
    ) -> dict[str, dict[str, Any]]:
        """``{lcsc: {"pads", "pin_map", "package"}}`` for a set of
        C-numbers in ONE query — the import's local-footprint dedup
        (``precis.ingest.pcb_epro``) compares every imported local
        footprint against the catalog's cached pads for its C-number.
        ``package`` joins ``parts.package`` so a deduped component's
        ``footprint`` can carry the catalog's footprint name."""
        if not lcscs:
            return {}
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT f.lcsc, f.pads, f.pin_map, p.package "
                "FROM part_footprints f "
                "LEFT JOIN parts p ON p.lcsc = f.lcsc "
                "WHERE f.lcsc = ANY(%s)",
                ([str(c) for c in lcscs],),
            ).fetchall()
        return {
            str(r[0]): {
                "pads": r[1] or [],
                "pin_map": r[2] or {},
                "package": r[3],
            }
            for r in rows
        }

    def parts_by_mpns(
        self, mpns: list[str] | set[str]
    ) -> dict[str, list[tuple[str, str]]]:
        """``{lowercase mfr_part: [(lcsc, mfr)]}`` — the manufacturer+MPN
        half of the import's part lookup (pcb-datasheet-review.md slice
        1: "manufacturer + MPN first, then the C-number"). ``mfr_part``
        comparisons are case-insensitive; callers disambiguate a multi-
        hit with the manufacturer string."""
        if not mpns:
            return {}
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT lower(mfr_part), lcsc, coalesce(mfr, '') FROM parts "
                "WHERE lower(mfr_part) = ANY(%s)",
                ([str(m).lower() for m in mpns],),
            ).fetchall()
        out: dict[str, list[tuple[str, str]]] = {}
        for mpn, lcsc, mfr in rows:
            out.setdefault(str(mpn), []).append((str(lcsc), str(mfr)))
        return out

    def parts_in_package(self, package: str) -> list[tuple[str, dict[str, Any]]]:
        """``[(lcsc, params)]`` for one JLC package name — the parametric
        fallback of the import's part lookup. ``params`` (the jlcparts
        attribute blob) is where a value like ``"100nF"`` lives."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT lcsc, coalesce(params, '{}'::jsonb) FROM parts "
                "WHERE package = %s",
                (package,),
            ).fetchall()
        return [(str(r[0]), dict(r[1])) for r in rows]

    def part_footprint_put(self, lcsc: str, data: dict[str, Any]) -> None:
        """Cache a fetched footprint (Flow B). Upsert by C-number. ``raw``
        (the untouched EasyEDA component JSON) round-trips so a future
        parser improvement can reparse from cache without re-fetching.
        ``escape`` (the precomputed escape graph, opaque here — see
        :mod:`precis.pcb.escape`) round-trips the same way as every other
        field: an upsert with the key omitted (or ``None``) writes NULL,
        same as ``kicad_mod``/``source`` above."""
        with self._pcb_tx() as conn:
            conn.execute(
                """
                INSERT INTO part_footprints
                    (lcsc, pads, pin_map, courtyard, centroid, kicad_mod, source, raw, escape, holes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (lcsc) DO UPDATE SET
                    pads=EXCLUDED.pads, pin_map=EXCLUDED.pin_map,
                    courtyard=EXCLUDED.courtyard, centroid=EXCLUDED.centroid,
                    kicad_mod=EXCLUDED.kicad_mod, source=EXCLUDED.source,
                    raw=EXCLUDED.raw, escape=EXCLUDED.escape,
                    holes=EXCLUDED.holes, fetched_at=now()
                """,
                (
                    lcsc,
                    _jsonb_or_none(data.get("pads")),
                    _jsonb_or_none(data.get("pin_map")),
                    _jsonb_or_none(data.get("courtyard")),
                    _jsonb_or_none(data.get("centroid")),
                    data.get("kicad_mod"),
                    data.get("source"),
                    _jsonb_or_none(data.get("raw")),
                    _jsonb_or_none(data.get("escape")),
                    Jsonb(data.get("holes") or []),
                ),
            )

    # -- argument ledger (the se_notes shape; pcb-argue-with-design.md) --
    def pcb_handles(self, ref_id: int) -> dict[str, Any]:
        """Every live handle on a design, by class: ``parts`` (refdes),
        ``pins`` (``{refdes: [pin, ...]}``), ``nets`` (names), ``features``
        (distinct ftypes). The resolver's one read; the 400 a bad handle
        earns quotes it back so the user can pick a real one."""
        with self._pcb_conn() as conn:
            pins: dict[str, list[str]] = {}
            for refdes, pin in conn.execute(
                "SELECT i.refdes, p.name FROM pcb_instances i "
                "JOIN pcb_pins p ON p.component_id = i.component_id "
                "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                "  AND p.retired_at IS NULL ORDER BY i.refdes, p.name",
                (ref_id,),
            ).fetchall():
                pins.setdefault(refdes, []).append(pin)
            parts = [
                r[0]
                for r in conn.execute(
                    "SELECT refdes FROM pcb_instances "
                    "WHERE ref_id = %s AND retired_at IS NULL ORDER BY refdes",
                    (ref_id,),
                ).fetchall()
            ]
            nets = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM pcb_nets "
                    "WHERE ref_id = %s AND retired_at IS NULL ORDER BY name",
                    (ref_id,),
                ).fetchall()
            ]
            features = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT ftype FROM pcb_features "
                    "WHERE ref_id = %s AND retired_at IS NULL ORDER BY ftype",
                    (ref_id,),
                ).fetchall()
            ]
        return {"parts": parts, "pins": pins, "nets": nets, "features": features}

    def pcb_features_of_type(self, ref_id: int, ftype: str) -> list[dict[str, Any]]:
        """The live ``pcb_features`` rows of one ftype — a ``feature:<ftype>``
        handle's resolution."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT ftype, x, y, rot, layer, geom, note FROM pcb_features "
                "WHERE ref_id = %s AND ftype = %s AND retired_at IS NULL "
                "ORDER BY feature_id",
                (ref_id, ftype),
            ).fetchall()
        return [
            {
                "ftype": r[0],
                "x": r[1],
                "y": r[2],
                "rot": r[3],
                "layer": r[4],
                "geom": r[5],
                "note": r[6],
            }
            for r in rows
        ]

    def pcb_note_insert(
        self,
        ref_id: int,
        *,
        name: str,
        kind: str,
        body: str,
        re: str | None = None,
        about: list[str] | None = None,
        origin: str = "user",
    ) -> int:
        """One ledger row. ``body`` is stored byte for byte; ``about`` is
        the caller's handle list (existence is a read-time concern)."""
        with self._pcb_tx() as conn:
            row = conn.execute(
                "INSERT INTO pcb_notes (ref_id, name, kind, body, re, about, origin) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (ref_id, name, kind, body, re, Jsonb(about or []), origin),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def pcb_note_retire(self, ref_id: int, name: str) -> bool:
        """Retire (never delete) the live note of this name — a genuine
        retraction, mirroring ``se_notes``'s ``remove_note``."""
        with self._pcb_tx() as conn:
            cur = conn.execute(
                "UPDATE pcb_notes SET retired_at = now() "
                "WHERE ref_id = %s AND name = %s AND retired_at IS NULL",
                (ref_id, name),
            )
        return cur.rowcount > 0

    def pcb_notes_list(self, ref_id: int) -> list[dict[str, Any]]:
        """The live ledger, oldest first."""
        with self._pcb_conn() as conn:
            rows = conn.execute(
                "SELECT name, kind, body, re, about, origin, created_at "
                "FROM pcb_notes WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY created_at, id",
                (ref_id,),
            ).fetchall()
        return [
            {
                "name": r[0],
                "kind": r[1],
                "body": r[2],
                "re": r[3],
                "about": list(r[4] or []),
                "origin": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]

    # -- delete ---------------------------------------------------------
    def pcb_delete(self, ref_id: int) -> dict[str, int]:
        """Soft-delete a design: mark the ref deleted, retire its graph rows,
        drop its search card — atomically."""
        counts = {}
        with self._pcb_tx() as conn:
            self.retire_ref(ref_id, conn=conn)
            for tbl in (
                "pcb_instances",
                "pcb_components",
                "pcb_nets",
                "pcb_measures",
                "pcb_features",
                "pcb_notes",
            ):
                counts[tbl] = conn.execute(
                    f"UPDATE {tbl} SET retired_at = now() "
                    "WHERE ref_id = %s AND retired_at IS NULL",
                    (ref_id,),
                ).rowcount
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
                (ref_id,),
            )
        return counts
