"""The one row-shaping layer for ``pcb`` batch authoring (gr514460, R-e).

Two writers shape the same authored dicts — :meth:`Store.pcb_apply` at
``put`` time and the wheel-side ``precis.pcb.local.batch_to_graph``
preview — and they had drifted into two implementations ("Not DRY … It
should see prod to get stuff the normal way"). This module is the single
normaliser both call. It ships inside the ``[pcb-kernel]`` wheel subset
(stdlib + ``precis.errors`` + ``precis.pcb`` consts only), so the offline
path uses the store's own rules byte-for-byte rather than a mirror.

Contents, by seam:

- **Authored-row validators**, moved verbatim out of
  ``store/_pcb_ops.py``: :func:`normalize_supplies`,
  :func:`normalize_client_of`, :func:`validate_tuple_clients`,
  :func:`normalize_roles`, :func:`validate_pin_declarations`,
  :func:`normalize_pad`, :func:`normalize_footprint_holes`,
  :func:`normalize_footprint`, plus the helpers :func:`rot_deg` /
  :func:`first_present` and the pad vocabulary consts.
  :func:`export_footprint_row` is the read-side counterpart: the stored
  row -> the authoring document (the ``view='footprints'`` JSON export).
- **:func:`lcsc_id`** — the C-number canonicaliser, moved out of
  ``catalog.py`` (which the wheel excludes); ``catalog.py`` re-exports it.
- **Graph-row emitters** — the ``pcb_graph`` dict shape computed without
  a DB: :func:`pin_tags_meta` (the ``_pcb_insert_pins`` fold),
  :func:`net_row` / :func:`net_patch`, :func:`instance_row`,
  :func:`footprint_pin_labels` (the declare-all completion's decl
  material), :func:`map_pad_numbers`
  (``_pcb_map_pad_number_pins``'s pad-number → pin-name rewrite), and
  :data:`NET_SPEC_FIELDS`.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Iterable
from typing import Any

from precis.errors import BadInput
from precis.pcb import MAX_FOOTPRINT_GRAPHICS, MAX_GRAPHIC_POINTS, PIN_ETYPES
from precis.pcb import padplace as pcb_padplace
from precis.utils.units import parse_angle_deg

# ── authored-row validators (moved from store/_pcb_ops.py) ──────────────


def normalize_supplies(
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


def normalize_client_of(
    raw: Any, component: str, pin: str, declared: set[str] | None = None
) -> dict[str, Any]:
    """A pin's ``client_of``: a role name, or ``{"role", optional
    "min_voltage_v", optional "members"}``. Returns the dict form.

    ``members`` makes the pin the head of a tuple client (§1c, a
    differential pair): distinct pin names on this component, headed by
    ``pin`` itself, each in ``declared`` when that is given. The tuple
    matches only a same-size supply block, carried as a unit."""
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
    members = raw.get("members")
    if members is not None:
        if (
            not isinstance(members, list)
            or not members
            or not all(isinstance(m, str) and m.strip() for m in members)
        ):
            raise ValueError(
                f"component {component}: pin {pin!r} client_of members must be "
                "a non-empty list of pin names"
            )
        names = [m.strip() for m in members]
        if names[0] != pin or len(set(names)) != len(names):
            raise ValueError(
                f"component {component}: pin {pin!r} client_of members must be "
                f"distinct and headed by {pin!r} itself, got {names!r}"
            )
        if declared is not None:
            missing = [m for m in names if m not in declared]
            if missing:
                raise ValueError(
                    f"component {component}: pin {pin!r} client_of members "
                    f"name undeclared pin(s) {missing!r}; declare them in 'pins'"
                )
        if len(names) > 1:
            out["members"] = names
    return out


def validate_tuple_clients(pins: list[dict[str, Any]], component: str) -> None:
    """A tuple client's tail members are carried by its head: refuse a
    tail that declares its own ``client_of``, or one claimed by two heads."""
    tails: dict[str, str] = {}
    for p in pins:
        co = p.get("client_of")
        if not isinstance(co, dict) or not isinstance(co.get("members"), list):
            continue
        head = str(p.get("name") or "").strip()
        if not co["members"] or str(co["members"][0]).strip() != head:
            continue  # ``normalize_client_of`` refuses an unheaded list
        for raw in co["members"][1:]:
            m = str(raw).strip()
            if m in tails:
                raise ValueError(
                    f"component {component}: pin {m!r} is a client_of member "
                    f"of both {tails[m]!r} and {head!r}"
                )
            tails[m] = head
    for p in pins:
        name = str(p.get("name") or "").strip()
        if name in tails and p.get("client_of") is not None:
            raise ValueError(
                f"component {component}: pin {name!r} is a client_of member of "
                f"{tails[name]!r}; declare client_of on the head pin only"
            )


def normalize_roles(roles: Any) -> list[tuple[str, float | None, int | None]]:
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


def validate_pin_declarations(
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


def rot_deg(d: dict[str, Any]) -> float:
    """A placement's rotation in degrees (the pcb enclave's stored unit).
    ``rot_deg`` self-names; ``rot`` is the same degrees (bare number) or an
    explicit-unit string (``"90deg"``, ``"1.5708 rad"``) — gr336352."""
    key = "rot_deg" if d.get("rot_deg") is not None else "rot"
    return parse_angle_deg(d.get(key), arg_name=key, bare="degrees")


def first_present(d: dict[str, Any], *keys: str) -> Any:
    """The first of ``keys`` actually present (and not None) in ``d``, else
    ``None`` — the alias resolver for authoring fields whose ZERO is
    meaningful.

    The older ``d.get(a) or d.get(b)`` idiom is wrong for an explicit
    zero-current declaration, which must pass the current precondition,
    and for a net annotated ``working_voltage_v: 0`` — a shield tied to
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
_LOCAL_PAD_ROLES = ("solderable", "electrode", "probe", "tape_land")
_LOCAL_PAD_MASKS = ("open", "covered")
_LOCAL_PAD_PASTES = ("none", "full")
_LOCAL_PAD_SHAPES = ("circle", "rect", "obround", "polygon")

#: Vendor spellings a stored or re-imported pad may carry -> the
#: canonical ``_LOCAL_PAD_SHAPES`` word (gr536876). EasyEDA ``PAD``
#: primitives are stored in this dialect — ``padplace._SHAPE_MAP`` is the
#: same translation on the uppercase tokens — so a ``view='footprints'``
#: export or a ``footprints.json`` row authored back through
#: ``op='footprint'`` must read it, or the round-trip refuses on
#: ``'oval'``. Keyed on the post-``lower()`` spelling.
_VENDOR_PAD_SHAPES = {
    "ellipse": "circle",
    "oval": "obround",
}


def normalize_pad(p: dict[str, Any]) -> dict[str, Any]:
    """One authored footprint pad -> the exact dict
    :func:`precis.pcb.padplace.place_footprint_pads` already consumes for
    an EasyEDA-cached one (``number``/``shape``/``x``/``y``/``w``/``h``/
    ``rot``/``layer``/``drill``), plus the ``role``/``mask``/``paste``
    intent and, for ``shape: "polygon"``, the vertex ``poly`` ring
    (footprint-local mm — same frame as ``x``/``y`` on every other shape).
    The authored alias ``pin`` normalises to the canonical ``number``;
    ``shape`` takes the vendor spellings in :data:`_VENDOR_PAD_SHAPES` as
    aliases for the canonical vocabulary (``oval`` -> ``obround``,
    ``ellipse`` -> ``circle``), so an exported cache row re-authors.

    ``paste`` defaults to ``"none"`` for an ``electrode`` or
    ``tape_land`` pad and ``"full"`` otherwise when the author omits it —
    resolved HERE, once, so every downstream reader
    (:mod:`precis.pcb.padplace`, :mod:`precis.pcb.gerber`) can treat the
    field as always-explicit rather than re-deriving the same default
    (acceptance criterion 3: "no paste on any role: electrode pad"; a
    tape land takes copper tape, never stencil-printed solder —
    ewod-controller-and-hv-supply.md Defect B).
    """
    pin_name = str(p.get("pin") or p.get("number") or "").strip()
    if not pin_name:
        raise ValueError("pcb footprint pad needs a 'pin' name")
    shape = str(p.get("shape") or "rect").strip().lower()
    shape = _VENDOR_PAD_SHAPES.get(shape, shape)
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
        else ("none" if role in ("electrode", "tape_land") else "full")
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
        "rot": rot_deg(p),
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
    "graphics",
)


#: The ``kind`` values a footprint graphic may carry. ``region`` is the
#: filled-area shape (a pour outline, a cutout, a keepout/rule area —
#: ``keepout: true`` names the last).
_GRAPHIC_KINDS = frozenset(
    {"line", "polyline", "polygon", "arc", "circle", "region", "text"}
)

#: The keys each ``kind`` may carry, beyond ``kind``/``layer``/``keepout``.
_GRAPHIC_KEYS: dict[str, frozenset[str]] = {
    "line": frozenset({"points", "width_mm"}),
    "polyline": frozenset({"points", "width_mm"}),
    "polygon": frozenset({"points", "width_mm"}),
    "region": frozenset({"points", "width_mm"}),
    "arc": frozenset(
        {"start", "end", "width_mm", "angle_deg", "radius_mm", "cw", "large_arc"}
    ),
    "circle": frozenset({"center", "radius_mm", "width_mm"}),
    "text": frozenset({"x", "y", "text", "height_mm", "rot_deg", "mirror"}),
}

#: Minimum vertex counts for the ``points``-carrying kinds.
_GRAPHIC_MIN_POINTS = {"line": 2, "polyline": 2, "polygon": 3, "region": 3}


def _graphic_num(value: Any, name: str, i: int, key: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"pcb footprint {name!r}: graphics[{i}].{key} must be numeric"
        ) from exc
    if not math.isfinite(out):
        raise BadInput(f"pcb footprint {name!r}: graphics[{i}].{key} must be finite")
    return out


def _graphic_point(value: Any, name: str, i: int, key: str) -> list[float]:
    """One ``[x, y]`` pair, validated and rounded like the pads."""
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or isinstance(value, str)
    ):
        raise BadInput(
            f"pcb footprint {name!r}: graphics[{i}].{key} entries must be [x, y] pairs"
        )
    return [
        round(_graphic_num(value[0], name, i, key), 4),
        round(_graphic_num(value[1], name, i, key), 4),
    ]


def _normalize_graphic(name: str, i: int, g: Any) -> dict[str, Any]:
    """One ``graphics[]`` entry, validated and rounded — raises
    :class:`BadInput` on anything off. :func:`normalize_footprint_graphics`
    decides per call whether that raise propagates (authored input) or
    demotes to a dropped-graphic warning (vendor-decoded input)."""
    if not isinstance(g, dict):
        raise BadInput(f"pcb footprint {name!r}: graphics[{i}] must be an object")
    kind = g.get("kind")
    if kind not in _GRAPHIC_KINDS:
        raise BadInput(
            f"pcb footprint {name!r}: graphics[{i}].kind must be one of "
            f"{sorted(_GRAPHIC_KINDS)}, got {kind!r}"
        )
    layer = g.get("layer")
    if not isinstance(layer, str) or not layer.strip():
        raise BadInput(f"pcb footprint {name!r}: graphics[{i}] needs a nonempty layer")
    accepted = _GRAPHIC_KEYS[kind] | {"kind", "layer", "keepout"}
    extra = sorted(set(g) - accepted)
    if extra:
        raise BadInput(
            f"pcb footprint {name!r}: graphics[{i}] ({kind}) has unknown "
            f"key(s) {extra}; accepted: {sorted(accepted)}"
        )
    item: dict[str, Any] = {"kind": kind, "layer": layer.strip()}
    if g.get("keepout") is not None:
        item["keepout"] = bool(g["keepout"])
    if "width_mm" in g:
        width = _graphic_num(g["width_mm"], name, i, "width_mm")
        if width < 0:
            raise BadInput(
                f"pcb footprint {name!r}: graphics[{i}].width_mm must be >= 0"
            )
        item["width_mm"] = round(width, 4)
    if kind in _GRAPHIC_MIN_POINTS:
        points = g.get("points")
        if not isinstance(points, (list, tuple)):
            raise BadInput(
                f"pcb footprint {name!r}: graphics[{i}] ({kind}) needs a points list"
            )
        if len(points) < _GRAPHIC_MIN_POINTS[kind]:
            raise BadInput(
                f"pcb footprint {name!r}: graphics[{i}] ({kind}) needs at "
                f"least {_GRAPHIC_MIN_POINTS[kind]} points"
            )
        if len(points) > MAX_GRAPHIC_POINTS:
            raise BadInput(
                f"pcb footprint {name!r}: graphics[{i}] ({kind}) has "
                f"{len(points)} points — the cap is {MAX_GRAPHIC_POINTS}"
            )
        item["points"] = [_graphic_point(p, name, i, "points") for p in points]
    elif kind == "arc":
        for key in ("start", "end"):
            if g.get(key) is None:
                raise BadInput(
                    f"pcb footprint {name!r}: graphics[{i}] (arc) needs {key}"
                )
            item[key] = _graphic_point(g[key], name, i, key)
        for key in ("angle_deg", "radius_mm"):
            if g.get(key) is not None:
                item[key] = round(_graphic_num(g[key], name, i, key), 4)
        for key in ("cw", "large_arc"):
            if g.get(key) is not None:
                item[key] = bool(g[key])
    elif kind == "circle":
        if g.get("center") is None:
            raise BadInput(
                f"pcb footprint {name!r}: graphics[{i}] (circle) needs center"
            )
        item["center"] = _graphic_point(g["center"], name, i, "center")
        radius = _graphic_num(g.get("radius_mm"), name, i, "radius_mm")
        if radius <= 0:
            raise BadInput(
                f"pcb footprint {name!r}: graphics[{i}].radius_mm must be > 0"
            )
        item["radius_mm"] = round(radius, 4)
    else:  # text
        for key in ("x", "y"):
            if g.get(key) is None:
                raise BadInput(
                    f"pcb footprint {name!r}: graphics[{i}] (text) needs {key}"
                )
            item[key] = round(_graphic_num(g[key], name, i, key), 4)
        text = g.get("text")
        if not isinstance(text, str) or not text:
            raise BadInput(
                f"pcb footprint {name!r}: graphics[{i}] (text) needs nonempty text"
            )
        item["text"] = text
        height = _graphic_num(g.get("height_mm"), name, i, "height_mm")
        if height <= 0:
            raise BadInput(
                f"pcb footprint {name!r}: graphics[{i}].height_mm must be > 0"
            )
        item["height_mm"] = round(height, 4)
        if g.get("rot_deg") is not None:
            item["rot_deg"] = round(_graphic_num(g["rot_deg"], name, i, "rot_deg"), 4)
        if g.get("mirror") is not None:
            item["mirror"] = bool(g["mirror"])
    return item


def normalize_footprint_graphics(
    name: str, raw: Any, *, dropped: list[str] | None = None
) -> list[dict[str, Any]]:
    """A footprint's non-pad graphics, ``[{kind, layer, ...}]`` in
    footprint-local mm (the pads' frame, Y up), validated and rounded.

    Inspectable only: a ``keepout: true`` region is the footprint's own
    rule area (an antenna or crystal "no copper" zone), stored so a
    later slice can bind it — nothing enforces it yet.

    ``dropped`` picks the failure mode per call site: ``None`` (the
    default, agent-authored input) raises :class:`BadInput` on the first
    bad graphic so the author hears about it; a list (a vendor-decoded
    import) drops just that graphic and appends the refusal text — one
    bad primitive must not reject the whole footprint.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise BadInput(f"pcb footprint {name!r}: graphics must be a list of objects")
    out: list[dict[str, Any]] = []
    for i, g in enumerate(raw):
        if len(out) >= MAX_FOOTPRINT_GRAPHICS:
            overflow = len(raw) - i
            if dropped is None:
                raise BadInput(
                    f"pcb footprint {name!r}: at most {MAX_FOOTPRINT_GRAPHICS} "
                    "graphics per footprint"
                )
            dropped.append(
                f"pcb footprint {name!r}: {overflow} graphic(s) over the "
                f"{MAX_FOOTPRINT_GRAPHICS}-per-footprint cap — dropped at import"
            )
            break
        try:
            out.append(_normalize_graphic(name, i, g))
        except BadInput as exc:
            if dropped is None:
                raise
            dropped.append(f"{exc} — dropped at import")
    return out


def normalize_footprint_holes(name: str, raw: Any) -> list[dict[str, float]]:
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


def normalize_footprint(f: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One ``footprints[]`` batch entry -> ``(name, data)`` where ``data``
    is the same ``{pads, pin_map, courtyard, centroid}`` row shape
    ``part_footprints`` uses (:meth:`PcbMixin.pcb_footprints_for`'s own
    return shape) — so every reader downstream of either source (padplace/
    realize/gerber) takes one dict shape, not two.

    ``pin_map`` is identity by default (pad number == pin name —
    :func:`normalize_pad` already made ``number`` the pin name), matching
    :mod:`precis.pcb.easyeda`'s own construction. An authored ``pin_map``
    is honoured when given, which is what lets a SEMANTIC pin name reach
    real copper: ``padplace.pad_label`` resolves a netlist pin name
    through this map, so without it the only way to name a pad ``"VDD"``
    was to call the *pad* that — destroying the pad number an EasyEDA
    export has to round-trip back out.

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
    pads = [normalize_pad(p) for p in raw_pads]
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
        "holes": normalize_footprint_holes(name, f.get("holes")),
        "graphics": normalize_footprint_graphics(name, f.get("graphics")),
    }
    return name, data


#: The keys the ``view='footprints'`` JSON export carries per row — the
#: authoring surface of ``op='footprint'``'s ``args.footprint`` (which adds
#: ``name`` itself off ``args.part``), minus ``note`` (a
#: ``part_footprints`` row has no note column to export).
EXPORT_FOOTPRINT_KEYS = (
    "pads",
    "pin_map",
    "courtyard",
    "centroid",
    "holes",
    "graphics",
)


def export_footprint_row(name: str, row: dict[str, Any]) -> dict[str, Any]:
    """A stored ``part_footprints`` row -> the exact document
    ``put(args={'op':'footprint','footprint': ...})`` accepts
    (gr536876): ``{pads, pin_map, courtyard, centroid, holes,
    graphics}``, run through :func:`normalize_footprint` itself so the
    export IS an authoring row, not a look-alike.

    That is the round-trip contract ``view='footprints',
    args={'format':'json'}`` needs twice over. A verbatim cache row
    fails authoring in two ways: its pads speak the vendor shape dialect
    (``OVAL`` — :data:`_VENDOR_PAD_SHAPES` now maps it, but the export
    must still EMIT the canonical word), and its ``pin_map`` may be
    NULL, which re-authors as the identity map — so authoring the export
    must not store something different from what the export said. And
    only a row emitted already-normalised re-exports byte-identically:
    ``normalize_pad`` adds the explicit ``role``/``mask``/``paste``
    defaults and rounds every coordinate, so an unnormalised export
    would differ from the export of the row it just authored. ``name``
    names the footprint in a validation error (the lcsc at the one call
    site); it is not emitted — ``args.footprint`` takes no ``name``.
    """
    _, data = normalize_footprint(
        {
            "name": name,
            "pads": row.get("pads"),
            "pin_map": row.get("pin_map"),
            "courtyard": row.get("courtyard"),
            "centroid": row.get("centroid"),
            "holes": row.get("holes"),
            "graphics": row.get("graphics"),
        }
    )
    return {k: data[k] for k in EXPORT_FOOTPRINT_KEYS}


# ── the C-number canonicaliser (moved from catalog.py) ──────────────────

#: An agent-typed C-number: ``C`` + 3–8 digits (any case). The smallest
#: catalogued parts are 3-digit (C67, …) and the dump walks well into
#: 8 digits.
_LCSC_ID_RE = re.compile(r"[Cc](\d{3,8})")

#: A C-number embedded in a pasted product URL — ``…_C2655145.html``
#: (lcsc.com), ``…/C2655145`` (jlcpcb.com partdetail/urlSuffix),
#: ``?searchTxt=C2655145``. Alphanumeric boundaries (not ``\b`` — ``_``
#: is a word char) so ``RN60C9992FB14`` does not match.
_LCSC_URL_RE = re.compile(r"(?<![A-Za-z0-9])[Cc](\d{3,8})(?![A-Za-z0-9])")


def lcsc_id(raw: Any) -> str | None:
    """Canonical ``C<n>`` for an agent-supplied part id, or ``None``.

    Accepts a bare ``C…`` (any case) and a pasted LCSC/JLCPCB product URL —
    the C-number is extracted from the string; the URL is never fetched.
    Anything else returns ``None`` (callers keep their existing handling
    for non-C-number ids)."""
    if raw is None:
        return None
    s = str(raw).strip()
    m = _LCSC_ID_RE.fullmatch(s)
    if m is not None:
        return f"C{m.group(1)}"
    m = _LCSC_URL_RE.search(s)
    return f"C{m.group(1)}" if m is not None else None


# ── the pcb_graph emit shape, no DB ─────────────────────────────────────

#: ``nets[]`` authoring key (and its alias) -> ``pcb_nets`` column, for the
#: four §E-1/annotation columns 0171 added. One table, so the insert and
#: the patch cannot drift apart on an alias. (Was
#: ``PcbMixin._NET_SPEC_FIELDS`` — the class attr remains, aliased here.)
NET_SPEC_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("working_voltage_v", ("working_voltage_v", "voltage")),
    ("edge_rate_v_per_ns", ("edge_rate_v_per_ns", "edge_rate")),
    ("impedance_ohm", ("impedance_ohm", "impedance")),
    ("function_hint", ("function_hint", "function")),
)


def pin_tags_meta(
    p: dict[str, Any], component: str, declared: set[str] | None = None
) -> tuple[list[str], dict[str, Any]]:
    """``(tags, meta)`` for one declared pin — the ``_pcb_insert_pins``
    fold, verbatim: ``meta`` passes through; the bool keys
    ``unused``/``must_terminate``/``nc`` are the authored shorthand for
    the same meta facts, and ``nc: true`` ALSO adds the ``nc`` tag;
    ``etype`` is a TOP-LEVEL key only — validated against
    :data:`PIN_ETYPES` and joined to the tags — while an
    ``etype`` authored under ``meta`` rides meta verbatim and earns no
    tag (the store rule: ``p['etype']``, never ``meta['etype']``);
    ``max_voltage_v``/``vih``/``vil``/``rail`` ride ``meta``;
    ``client_of`` lands in meta normalised (:func:`normalize_client_of`).
    """
    name = str(p.get("name") or "").strip()
    tags = list(p.get("tags") or [])
    meta = dict(p.get("meta") or {})
    for marker in ("unused", "must_terminate", "nc"):
        if p.get(marker):
            meta[marker] = True
    if meta.get("nc") and "nc" not in tags:
        tags.append("nc")
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
    # gr520810: the first-use classification keys ride meta verbatim
    # (``fmax_hz``/``assume`` are new here; ``max_current_a``/
    # ``est_current_a``/``source`` already existed for ``op='pins'``
    # but a batch-authored top-level copy was silently dropped).
    for key in (
        "max_voltage_v",
        "vih",
        "vil",
        "rail",
        "max_current_a",
        "est_current_a",
        "fmax_hz",
        "source",
        "assume",
    ):
        if p.get(key) is not None:
            meta[key] = p[key]
    if p.get("client_of") is not None:
        meta["client_of"] = normalize_client_of(
            p["client_of"], component, name, declared
        )
    return tags, meta


def footprint_pin_labels(fp: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """``{pin name: {"name", "tags"}}`` for every pad a footprint row
    names — the declare-all completion's decl material
    (``_pcb_complete_footprint_pins``): the ``pin_map`` name wins
    (``padplace.pad_label``), its tags ride along; a duplicate label
    keeps the first."""
    out: dict[str, dict[str, Any]] = {}
    pin_map = (fp or {}).get("pin_map") or {}
    for pad in (fp or {}).get("pads") or []:
        if not str(pad.get("number") or "").strip():
            continue
        label = pcb_padplace.pad_label(pad, pin_map)
        if label and label not in out:
            entry = pin_map.get(str(pad.get("number")))
            out[label] = (
                {"name": label, "tags": []}
                if isinstance(entry, str)
                else {"name": label, "tags": list((entry or {}).get("tags") or [])}
            )
    return out


def net_row(n: dict[str, Any]) -> dict[str, Any]:
    """One authored ``nets[]`` entry as the ``pcb_graph`` net dict —
    ``_pcb_insert_net``'s normalisation: alias collapse
    (``class``/``current``), ``domain`` default ``"electrical"``, the
    §E-1 spec columns resolved by PRESENCE off :data:`NET_SPEC_FIELDS`,
    ``rail`` folded into ``meta``."""
    name = str(n.get("name") or "").strip()
    if not name:
        raise ValueError("pcb net needs a name (meaningful)")
    meta = dict(n.get("meta") or {})
    if n.get("rail") is not None:
        meta["rail"] = str(n["rail"]).strip()
    row: dict[str, Any] = {
        "name": name,
        "net_class": n.get("net_class") or n.get("class"),
        "domain": str(n.get("domain") or "electrical").strip(),
        # PRESENCE, not truthiness — an explicit `current: 0` (a deliberate
        # "carries nothing" annotation) must not fall through the alias to
        # NULL (gr520542).
        "est_current_a": first_present(n, "est_current_a", "current"),
    }
    for column, keys in NET_SPEC_FIELDS:
        row[column] = first_present(n, *keys)
    row["meta"] = meta
    row["members"] = []
    return row


#: The ``_NET_PATCH_FIELDS`` columns ``pcb_graph`` actually emits —
#: ``width_mm``/``note`` patch the DB but are not graph keys, so they
#: cannot appear in a re-declared net's merged row.
_NET_PATCH_GRAPH_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    *NET_SPEC_FIELDS,
    ("net_class", ("net_class", "class")),
    ("est_current_a", ("est_current_a", "current")),
)


def net_patch(row: dict[str, Any], n: dict[str, Any]) -> None:
    """Merge a second same-named ``nets[]`` dict into an emitted
    :func:`net_row` — ``_pcb_patch_net``'s graph-visible half:
    presence-based overwrite of the emitted spec columns, ``rail``/``meta``
    merged (top-level keys win) rather than replaced."""
    for column, keys in _NET_PATCH_GRAPH_FIELDS:
        value = first_present(n, *keys)
        if value is None:
            continue
        row[column] = value
    meta_patch = dict(n.get("meta") or {})
    if n.get("rail") is not None:
        meta_patch["rail"] = str(n["rail"]).strip()
    if meta_patch:
        row["meta"] = {**(row.get("meta") or {}), **meta_patch}


def instance_row(
    c: dict[str, Any],
    *,
    pin_names: Iterable[str] = (),
    declared: set[str] | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One authored component dict as the ``pcb_graph`` instance row —
    ``_pcb_insert_component`` + ``_pcb_insert_instance``'s normalisation
    and ``pcb_graph``'s hoist, in one place: defaults
    (``layer``/``label``), ``rot`` parsed via :func:`rot_deg`,
    ``part_lcsc`` canonicalised via :func:`lcsc_id`, the i.meta
    placement-constraint keys (``group``/``group_offset``/``pattern``/
    ``pattern_instance``/``edge_mate``/``local_fp_lcsc``/``local_fp_diff``)
    and the c.meta component facts (``pin_swap_groups``/``pin_decls``/
    ``supplies`` — normalised — /``part_meta`` -> ``part``) hoisted when
    present, the
    0219 lock columns hoisted when set. ``polarized`` IS emitted now
    (gr520373 — the explicit flag is a real authored fact, stored on
    ``c.meta`` and authoritative over the label regex); ``value``/``note``
    are still deliberately NOT emitted — ``pcb_graph`` never carries them.

    ``pin_names`` is the component's LIVE pin-name set (declared pins ∪
    footprint-completed ∪ connection-minted ghosts) — the store counts
    live ``pcb_pins`` rows for ``n_pins``. ``catalog=`` is the
    ``_pcb_resolve_parts`` shape ``{lcsc: {footprint, height_mm,
    courtyard}}``: a C-number's unset snapshot fields auto-stamp from it
    like ``_pcb_insert_component``; ``None`` = the uncatalogued path
    (authored values only). ``extended_part`` reads False — correct for
    Basic/non-catalog, wrong-but-safe for Extended (the "unknown is never
    silently promoted to a fee" caution ``pcb_graph`` documents)."""
    refdes = str(c.get("refdes") or "").strip()
    if not refdes:
        raise ValueError("pcb component needs a refdes")
    raw_part = c.get("part_lcsc") or c.get("part") or c.get("lcsc")
    part_lcsc = lcsc_id(raw_part) or raw_part
    footprint = c.get("footprint")
    height_mm = c.get("height_mm")
    resolved = (
        (catalog or {}).get(str(part_lcsc).strip().upper())
        if part_lcsc is not None
        else None
    )
    if resolved is not None:
        footprint = footprint or resolved.get("footprint")
        if height_mm is None:
            height_mm = resolved.get("height_mm")
    inst: dict[str, Any] = {
        "refdes": refdes,
        "x": c.get("x"),
        "y": c.get("y"),
        "layer": str(c.get("layer") or "top"),
        "roles": list(c.get("roles") or []),
        "label": str(c.get("label") or c.get("refdes") or "part"),
        "height_mm": height_mm,
        "n_pins": len({str(n) for n in pin_names}),
        "fixed": c.get("fixed"),
        "rot": rot_deg(c),
        "part_lcsc": part_lcsc,
        "footprint": footprint,
        "extended_part": False,
        "part_description": None,
    }
    for k in (
        "group",
        "group_offset",
        "pattern",
        "pattern_instance",
        "edge_mate",
        "local_fp_lcsc",
        "local_fp_diff",
    ):
        if c.get(k) is not None:
            inst[k] = copy.deepcopy(c[k])
    if c.get("pin_swap_groups") is not None:
        inst["pin_swap_groups"] = copy.deepcopy(c["pin_swap_groups"])
    if c.get("pin_decls") is not None:
        inst["pin_decls"] = copy.deepcopy(c["pin_decls"])
    if c.get("supplies") is not None:
        inst["supplies"] = normalize_supplies(c["supplies"], refdes, declared)
    if c.get("part_meta") is not None:
        inst["part"] = dict(c["part_meta"])
    if c.get("polarized") is not None:
        inst["polarized"] = bool(c["polarized"])
    if c.get("max_travel_mm") is not None:
        inst["max_travel_mm"] = float(c["max_travel_mm"])
    if c.get("fixed_side"):
        inst["fixed_side"] = True
    if c.get("group_fixed"):
        inst["group_fixed"] = True
    if c.get("room_id") is not None:
        inst["room_id"] = int(c["room_id"])
    return inst


def map_pad_numbers(
    connections: list[dict[str, Any]],
    fp_by_refdes: dict[str, dict[str, Any]],
    live_by_refdes: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """``_pcb_map_pad_number_pins`` without the DB (gr495291): rewrite a
    connection ``pin`` given as a PAD NUMBER of the refdes's resolved
    footprint to that pad's pin name. ``fp_by_refdes`` is
    ``{refdes: footprint row}`` (``{pin_map, pads}`` — the
    :func:`normalize_footprint` / ``part_footprints`` shape) and
    ``live_by_refdes`` the refdes's live pin-name set (declared ∪
    footprint-completed). A pin that is already a live pin name, or a
    footprint pin name, or not a pad number, is left alone.

    Returns ``(connections, ["U1.8 -> GND", ...])``."""
    by_number: dict[str, dict[str, str]] = {}
    for refdes, fp in fp_by_refdes.items():
        pin_map = (fp or {}).get("pin_map") or {}
        # ``pad_label`` is a function of the pad number, so each number
        # names exactly one pin; a number that is itself some pad's pin
        # name is a name, never remapped.
        numbers = {
            str(p.get("number") or "").strip(): pcb_padplace.pad_label(p, pin_map)
            for p in (fp or {}).get("pads") or []
        }
        labels = set(numbers.values())
        by_number[refdes] = {
            n: label for n, label in numbers.items() if n and label and n not in labels
        }
    out: list[dict[str, Any]] = []
    mapped: list[str] = []
    for k in connections:
        refdes = str(k.get("refdes") or "").strip()
        pin = str(k.get("pin") or "").strip()
        target = by_number.get(refdes, {}).get(pin)
        if target is None or pin in (live_by_refdes.get(refdes) or set()):
            out.append(k)
            continue
        out.append({**k, "pin": target})
        mapped.append(f"{refdes}.{pin} -> {target}")
    return out, mapped
