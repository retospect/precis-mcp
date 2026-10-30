"""precis.pcb.board_script — a pcb batch dict -> a readable workspace script.

The ``put(kind='pcb')`` batch (``components``/``nets``/``connections``/
``features``/``footprints``/``planes``/``net_classes``/``roles``/``generators``/
``measures``/``meta``/``profile`` — :meth:`PcbMixin.pcb_apply`'s contract)
becomes a ``main.py`` whose ``build().to_batch()`` is byte-equal to the
source (gr514048) without catalog substitution, except legacy measures normalize to the builder's
operand/strength fields and defaults, with reasons kept as comments.
``scripts/pcb-batch-to-board`` is a thin CLI over
:func:`emit`; ``scripts/pcb-batch-to-script`` is its offline JSON-to-file
CLI, with ``--placement keep|omit`` (default keep). Explicit placement
modes lock supplied poses or remove them; omitting the argument to
:func:`emit` preserves the original placement flags for exact readback.
:func:`board_batch` reconstructs the batch of a stored
board from the store's existing readers (``pcb_graph``/``pcb_load``/
``pcb_features_list``/``pcb_local_footprints_for`` — no new DB code), so
an ``.epro2`` imported into the dev DB can be written out as code.

Readability within byte-equality:

* Pads shared verbatim by several footprints become one ``_pads_*``
  binding per distinct pad set, referenced by name and marked as a
  synthesized footprint fallback. An optional local catalog cache replaces
  matching pad sets with pinned ``pcb.catalog_footprint`` references;
  aliases/defaults may normalize, but geometry and pin maps must agree.
  Unmatched source geometry remains inline, flagged on each part call.
* ``planes`` rows become ``b.plane_assignment(net, layer)`` calls, retaining
  routing-layer assignments without creating pour features or copper zones.
* Legacy measure ``strength`` becomes ``hard``; ``{"instance": refdes}``
  operands become refdes strings. ``reason`` becomes a single provenance
  comment on the measure call, rather than a field in the rebuilt row.
* A catalog reference spells ``lcsc=`` — never ``part=``, which would
  fire a catalog read at rebuild time and break byte-equality on a
  client-less run; a catalog *footprint* name is a plain
  ``footprint="…"`` string. Both round-trip verbatim through
  ``to_batch``.
* Pads carrying ``rot``/``layer``/``slot`` emit as raw dicts —
  ``pcb.pad()``'s signature cannot express them.
* ``<prefix>.`` refdes blocks that sit contiguous in batch order become
  an ``@module`` + ``b.use`` (the module fn creates the parts; nets and
  ties stay outside so authored order is exact).
* Explicit ``part_meta.source_sheet``/``source_block`` identity groups
  parts and exclusively internal nets into room helpers. Shared nets remain
  outside, names are unchanged, and declarations may reorder by room.
  The header reports flat sources instead of claiming inferred hierarchy.

What ``precis_geom.pcb`` cannot express is reported, never silently
dropped — a component's ``pin_decls``/``courtyard``/``centroid`` and
stored-shape ``measures`` land in ``loss``; :func:`emit` refuses on a
non-empty loss unless ``allow_loss=True``, when they are listed in a
header comment instead.

Source annotations follow ``precis.provenance.refs.find_annotations``:
universal board/part handles lead each part/net call's comment when supplied,
followed by source board slug and relational instance/net IDs as gloss.
No universal handles are invented for those relational IDs. Multi-line calls
carry their single-line annotation on the opening call line.
"""

from __future__ import annotations

import keyword
import os
import pprint
import re
from pathlib import Path
from typing import Any

#: Columns a line may not exceed (slice 2's contract).
_LIMIT = 100

#: The store's per-pin columns, for :func:`board_batch`'s raw SELECTs —
#: no existing reader returns ``pad``/``description``/``note`` and the
#: netconn ``note`` complete (``pcb_graph`` members carry tags/meta,
#: ``pcb_instance_neighbors`` pad but no meta; same read-only pattern the
#: handler's own ``pin_sem`` query uses).
_PINS_SQL = (
    "SELECT i.refdes, p.name, p.pad, p.tags, p.description, p.note, p.meta "
    "FROM pcb_instances i JOIN pcb_pins p ON p.component_id = i.component_id "
    "WHERE i.ref_id = %s AND i.retired_at IS NULL AND p.retired_at IS NULL "
    "ORDER BY i.instance_id, p.pin_id"
)
_CONNS_SQL = (
    "SELECT n.name, i.refdes, p.name, k.note "
    "FROM pcb_netconns k "
    "JOIN pcb_nets n ON n.net_id = k.net_id "
    "JOIN pcb_instances i ON i.instance_id = k.instance_id "
    "JOIN pcb_pins p ON p.pin_id = k.pin_id "
    "WHERE n.ref_id = %s AND n.retired_at IS NULL "
    "ORDER BY n.name, i.refdes, p.name"
)


class BoardScriptError(ValueError):
    """A batch key ``precis_geom.pcb`` cannot express (or a malformed
    batch) — :func:`emit` raises unless ``allow_loss``.

    ``refs`` names the entities the refusal is about
    (``part:<refdes>``/``net:<name>``/``footprint:<name>``… — the Diag
    ref vocabulary, gr522027); the error carries ``diag`` /
    ``diag_refs`` / ``diag_code`` so both the direct-caller path
    (``exc.diag``) and the traced manifest path (``_error_entry``'s
    attribute read) report it."""

    def __init__(
        self,
        msg: str,
        *,
        refs: tuple[str, ...] = (),
        next: str = "",
        op: str = "emit",
    ) -> None:
        super().__init__(msg)
        from precis.pcb import diag as pcb_diag

        self.diag = pcb_diag.error_diag(
            self, op=op, code="pcb.script-error", refs=refs, next=next
        )
        self.diag_refs = self.diag.refs
        self.diag_code = self.diag.code


#: Loss lines name their entity as ``<kind> '<name>'`` — the Diag
#: ``refs`` a loss raise carries (gr522027). ``part:<refdes>`` matches
#: ``precis.pcb.drc._object_identity``'s vocabulary.
_LOSS_REF = re.compile(r"^(part|net|footprint|generator|measure|group) '([^']+)'")


_PART_KEYS = {
    "refdes",
    "label",
    "part",
    "part_lcsc",
    "footprint",
    "value",
    "height_mm",
    "roles",
    "pins",
    "note",
    "x",
    "y",
    "rot",
    "layer",
    "fixed",
    "max_travel_mm",
    "edge_mate",
    "group",
    "group_offset",
    "pattern",
    "pattern_instance",
    "part_meta",
    "supplies",
    "pin_swap_groups",
}
#: batch net key -> the ``b.net`` kwarg (``None`` = the key itself).
_NET_KEYS = {
    "name": None,
    "class": "cls",
    "net_class": "cls",
    "current": "current",
    "est_current_a": "current",
    "working_voltage_v": "voltage",
    "voltage": "voltage",
    "impedance": "impedance",
    "impedance_ohm": "impedance",
    "edge_rate": "edge_rate",
    "edge_rate_v_per_ns": "edge_rate",
    "function": "function",
    "function_hint": "function",
    "note": "note",
    "domain": "domain",
    "width_mm": None,
    "meta": None,
}
_PAD_CALL_KEYS = {
    "pin",
    "shape",
    "x",
    "y",
    "w",
    "h",
    "poly",
    "drill",
    "role",
    "mask",
    "paste",
}
_PAD_KEYS = ("w", "h", "poly", "drill", "role", "mask", "paste")
#: Run stamps a traced ``to_batch`` puts on entity rows (gr521596) —
#: construction provenance, not authored content: known, never emitted
#: (a rebuilt run re-mints its own ``maker`` ids).
_RUN_KEYS = frozenset({"frame"})
_MEASURE_KEYS = {
    "metric",
    "operands",
    "bound",
    "goal",
    "hard",
    "weight",
    "strength",
    "reason",
}


class _Names:
    """Stable Python identifiers for refdes/net names, collision-free."""

    def __init__(self) -> None:
        self.used: set[str] = set()

    def mint(self, raw: str, prefix: str) -> str:
        ident = "".join(ch if ch.isalnum() else "_" for ch in raw).lower()
        if not ident or ident[0].isdigit():
            ident = f"{prefix}_{ident}"
        if keyword.iskeyword(ident) or ident in {
            "b",
            "pcb",
            "build",
            "board",
            "parts",
            "nets",
            "catalog",
        }:
            ident = f"{prefix}_{ident}"
        base, n = ident, 2
        while ident in self.used:
            ident = f"{base}_{n}"
            n += 1
        self.used.add(ident)
        return ident


def _kw(**kw: Any) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in kw.items() if v is not None)


def _source_comment(annotations: dict[str, Any], kind: str, key: str, slug: str) -> str:
    """Universal handles first, relational IDs as gloss (provenance.refs)."""
    handles = [str(annotations["board"])] if annotations.get("board") else []
    custom = (annotations.get(kind) or {}).get(key)
    if custom:
        handles.append(str(custom))
    board_slug = annotations.get("board_slug", slug)
    fields = [f"board={board_slug!r}"]
    ids_key, label = (
        ("instance_ids", "instance_id") if kind == "parts" else ("net_ids", "net_id")
    )
    row_id = (annotations.get(ids_key) or {}).get(key)
    if row_id is not None:
        fields.append(f"{label}={row_id}")
    comment = " ".join([*handles, *fields])
    return " ".join(comment.splitlines())


def _str(s: str, budget: int = 60) -> str:
    """``s`` as adjacent string literals each under ``budget`` chars — a
    long ``note=`` wraps instead of breaking the 100-column ceiling."""
    if len(repr(s)) <= budget:
        return repr(s)
    parts: list[str] = []
    cur = ""
    for word in s.split(" "):
        cand = f"{cur} {word}" if cur else word
        if cur and len(repr(cand)) > budget:
            parts.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        parts.append(cur)
    out: list[str] = []
    for p in parts:
        while len(repr(p)) > budget:
            out.append(p[:budget])
            p = p[budget:]
        out.append(p)
    return " ".join(repr(x) for x in out)


def _value(v: Any, indent: int = 8) -> str:
    """A Python literal for ``v`` wrapped to the column limit (meta and
    raw pad dicts); ``pprint`` keeps order and stays valid Python."""
    return pprint.pformat(v, width=_LIMIT - indent, sort_dicts=False)


def _pad_literal(p: dict[str, Any]) -> str:
    """One pad: ``pcb.pad(...)`` when its keys fit the signature, else the
    raw dict — ``rot``/``layer``/``slot`` ride verbatim either way."""
    if set(p) <= _PAD_CALL_KEYS:
        extra = {k: p[k] for k in _PAD_KEYS if k in p}
        args = f"{p['pin']!r}, {p['shape']!r}, {p['x']!r}, {p['y']!r}"
        if extra:
            args += ", " + _kw(**extra)
        return f"pcb.pad({args})"
    return _value(p)


def _emit_footprint(fp: dict[str, Any], pads_var: str, loss: list[str]) -> list[str]:
    unknown = set(fp) - {
        "name",
        "pads",
        "pin_map",
        "courtyard",
        "centroid",
        "note",
        "holes",
        "graphics",
    }
    if unknown:
        loss.append(f"footprint {fp['name']!r}: unknown keys {sorted(unknown)}")
    rest: dict[str, Any] = {
        k: fp[k] for k in ("pin_map", "courtyard", "centroid", "note") if k in fp
    }
    if fp.get("holes"):
        rest["holes"] = fp["holes"]
    ctor = (
        f"pcb.Footprint({fp['name']!r}, {pads_var}"
        + ((", " + _kw(**rest)) if rest else "")
        + (
            ", graphics=" + _value(fp["graphics"], indent=12)
            if fp.get("graphics")
            else ""
        )
        + ")"
    )
    one = f"b.footprint({ctor})"
    if "\n" not in ctor and len("    " + one) <= _LIMIT:
        return [one]
    return ["b.footprint(", f"    {ctor}", ")"]


def _emit_feature(f_: dict[str, Any]) -> str:
    f_ = dict(f_)
    ftype = f_.pop("ftype")
    note = f_.pop("note", None)
    if ftype == "outline" and set(f_) == {"geom"}:
        return f"b.outline({_value(f_['geom'])}{', ' + _kw(note=note) if note else ''})"
    geom = f_.get("geom") or {}
    if (
        ftype == "mounting_hole"
        and set(f_) == {"x", "y", "geom"}
        and set(geom) <= {"diameter", "ring_dia_mm", "plated"}
    ):
        kw = _kw(drill=geom["diameter"], ring=geom["ring_dia_mm"])
        if "plated" in geom:
            kw += f", plated={geom['plated']!r}"
        if note:
            kw += ", " + _kw(note=note)
        return f"b.hole({f_['x']!r}, {f_['y']!r}, {kw})"
    if note is not None:
        f_["note"] = note
    return f"b.feature({ftype!r}, **{_value(f_, indent=4)})"


def _emit_part(
    c: dict[str, Any],
    var: str,
    annotations: dict[str, Any],
    loss: list[str],
    *,
    board: str = "b",
    refdes: str | None = None,
    synthesized: bool = False,
    catalog_ref: str | None = None,
) -> list[str]:
    for k in set(c) - _PART_KEYS - _RUN_KEYS:
        loss.append(f"part {c['refdes']!r}: {k!r} cannot emit")
    refdes = refdes if refdes is not None else c["refdes"]
    pins: list[Any] = []
    for p in c.get("pins") or []:
        pins.append(p["name"] if set(p) == {"name"} else p)
    kw_parts = [
        f"{k}={v!r}"
        for k, v in (
            ("label", c.get("label")),
            ("lcsc", None if catalog_ref else c.get("part") or c.get("part_lcsc")),
            ("footprint", c.get("footprint")),
            ("value", c.get("value")),
            ("height_mm", c.get("height_mm")),
            ("roles", c.get("roles") or None),
            ("pins", pins or None),
            ("note", c.get("note")),
            ("part_meta", c.get("part_meta")),
            ("supplies", c.get("supplies")),
            ("pin_swap_groups", c.get("pin_swap_groups")),
        )
        if v is not None
    ]
    if catalog_ref:
        kw_parts.insert(0, f"part={catalog_ref}")
    head = f"{var} = {board}.part("
    call = head + ", ".join([f"{refdes!r}", *kw_parts]) + ")"
    tail = ""
    if "x" in c:
        tail = (
            f".at({c['x']!r}, {c['y']!r}, rot={c.get('rot', 0.0)!r}, "
            f"side={c.get('layer', 'top')!r}, fixed={c.get('fixed')!r})"
        )
    elif c.get("fixed") is not None:
        loss.append(f"part {c['refdes']!r}: 'fixed' without a pose")
    if c.get("max_travel_mm") is not None:
        tail += f".leash({c['max_travel_mm']!r})"
    em = c.get("edge_mate")
    if em is not None:
        for k in set(em) - {"face", "require", "at"}:
            loss.append(f"part {c['refdes']!r}: edge_mate key {k!r}")
        tail += (
            f".on_edge({em['face']!r}"
            + (f", at={em['at']!r}" if "at" in em else "")
            + (f", require={em['require']!r}" if "require" in em else "")
            + ")"
        )
    comment = (annotations.get("parts") or {}).get(c["refdes"])
    if comment is None and (c.get("part") or c.get("part_lcsc")):
        comment = f"lcsc:{c.get('part') or c.get('part_lcsc')}"
    if synthesized:
        comment = (f"{comment}; " if comment else "") + "synthesized footprint"
    if comment is not None:
        comment = " ".join(str(comment).splitlines())
    one = call + tail
    out: list[str] = []
    if comment is not None and len("    " + one) + 3 + len(comment) > _LIMIT:
        # Put provenance/fallback on the part call even when its args wrap.
        out.append(head + f"  # {comment}")
        out += [f"    {a}," for a in [repr(refdes), *kw_parts]]
        out.append(")")
        out += _tail_statements(var, c)
        for key in ("pattern", "pattern_instance"):
            if c.get(key) is not None:
                out.append(f"{var}.{key} = {c[key]!r}")
        return out
    if len("    " + one) <= _LIMIT:
        out.append(one + (f"  # {comment}" if comment else ""))
    else:
        out.append(head)
        out += [f"    {a}," for a in [repr(refdes), *kw_parts]]
        out.append(")")
        if tail:
            out += _tail_statements(var, c)
    for key in ("pattern", "pattern_instance"):
        if c.get(key) is not None:
            out.append(f"{var}.{key} = {c[key]!r}")
    return out


def _tail_statements(var: str, c: dict[str, Any]) -> list[str]:
    out = []
    if "x" in c:
        out.append(
            f"{var}.at({c['x']!r}, {c['y']!r}, rot={c.get('rot', 0.0)!r}, "
            f"side={c.get('layer', 'top')!r}, fixed={c.get('fixed')!r})"
        )
    if c.get("max_travel_mm") is not None:
        out.append(f"{var}.leash({c['max_travel_mm']!r})")
    em = c.get("edge_mate")
    if em is not None:
        out.append(
            f"{var}.on_edge({em['face']!r}"
            + (f", at={em['at']!r}" if "at" in em else "")
            + (f", require={em['require']!r}" if "require" in em else "")
            + ")"
        )
    return out


def _emit_groups(components: list[dict[str, Any]], var_of: dict[str, str]) -> list[str]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in components:
        if c.get("group") is not None:
            groups.setdefault(c["group"], []).append(c)
    lines = []
    for name, members in groups.items():
        parts = ", ".join(var_of[c["refdes"]] for c in members)
        offs = [c.get("group_offset") or {} for c in members]
        if any(offs):
            if any(set(o) != {"x", "y", "rot"} for o in offs):
                raise BoardScriptError(
                    f"group {name!r}: every member needs a full x/y/rot offset",
                    refs=(f"group:{name}",),
                )
            tuples = ", ".join(f"({o['x']!r}, {o['y']!r}, {o['rot']!r})" for o in offs)
            lines.append(f"b.group({name!r}, [{parts}], offsets=[{tuples}])")
        else:
            lines.append(f"b.group({name!r}, [{parts}])")
    return lines


def _emit_connections(
    conns: list[dict[str, Any]], net_var: dict[str, str], part_var: dict[str, str]
) -> list[str]:
    lines: list[str] = []
    run: list[dict[str, Any]] = []

    def flush() -> None:
        if not run:
            return
        note = run[0].get("note")
        head = f"{net_var[run[0]['net']]}.tie("
        args = [f"{part_var[c['refdes']]}[{c['pin']!r}]" for c in run]
        if note:
            args.append(f"note={_str(note)}")
        one = head + ", ".join(args) + ")"
        if len("    " + one) <= _LIMIT:
            lines.append(one)
        else:
            lines.append(head)
            lines.extend(f"    {a}," for a in args)
            lines.append(")")
        run.clear()

    for c in conns:
        if run and (c["net"] != run[0]["net"] or c.get("note") != run[0].get("note")):
            flush()
        run.append(c)
    flush()
    return lines


def _emit_measure(m: dict[str, Any], parts: dict[str, str]) -> str:
    """Normalize legacy intent fields into the builder's measure call."""
    kw = {k: m[k] for k in ("bound", "goal", "hard", "weight") if k in m}
    if "strength" in m:
        strength = m["strength"]
        if strength not in ("hard", "soft"):
            raise BoardScriptError(
                f"measure {m['metric']!r}: unknown strength {strength!r}"
            )
        hard = strength == "hard"
        if "hard" in kw and kw["hard"] != hard:
            raise BoardScriptError(
                f"measure {m['metric']!r}: conflicting hard and strength"
            )
        kw["hard"] = hard
    operands = []
    for operand in m["operands"]:
        if isinstance(operand, dict):
            if set(operand) != {"instance"} or not isinstance(operand["instance"], str):
                raise BoardScriptError(
                    f"measure {m['metric']!r}: unsupported operand {operand!r}"
                )
            operand = operand["instance"]
            if operand not in parts:
                raise BoardScriptError(
                    f"measure {m['metric']!r}: unknown instance {operand!r}"
                )
        operands.append(repr(operand))
    line = f"b.measure({m['metric']!r}, [{', '.join(operands)}]"
    line += (", " + _kw(**kw) if kw else "") + ")"
    if m.get("reason"):
        reason = " ".join(str(m["reason"]).splitlines())
        line += f"  # {reason}"
    return line


def _emit_net(n: dict[str, Any], var: str, comment: str) -> list[str]:
    kw = {
        _NET_KEYS[k] or k: v
        for k, v in n.items()
        if k != "name" and k not in _RUN_KEYS and v is not None
    }
    args = [repr(n["name"]), *[f"{k}={v!r}" for k, v in kw.items()]]
    line = f"{var} = b.net({', '.join(args)})"
    if len("    " + line + comment) + 4 <= _LIMIT:
        return [line + f"  # {comment}"]
    return [f"{var} = b.net(  # {comment}", *[f"    {a}," for a in args], ")"]


def _source_rooms(batch: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Explicit schematic identity, never guessed from mechanical groups."""
    members: dict[str, str] = {}
    labels: dict[str, str] = {}
    for c in batch["components"]:
        meta = c.get("part_meta") or {}
        for kind in ("source_sheet", "source_block"):
            item = meta.get(kind)
            if isinstance(item, str) and item:
                identity, label = item, item
            elif isinstance(item, dict) and (item.get("id") or item.get("name")):
                identity = str(item.get("id") or item["name"])
                label = str(item.get("name") or identity)
            else:
                continue
            key = f"{kind}:{identity}"
            members[c["refdes"]] = key
            labels[key] = label
            break
    return members, labels


def _modules(
    components: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """``{prefix: [member refdes in batch order]}`` for every
    ``<prefix>.`` refdes block that sits contiguous in the batch — the
    only shape an ``@module`` + ``b.use`` emits without reordering parts.
    An interleaved prefix, or a ``.`` in a lone refdes, stays flat."""
    order = [
        c["refdes"].split(".")[0] if "." in c["refdes"] else "" for c in components
    ]
    members: dict[str, list[str]] = {}
    for c, p in zip(components, order):
        if p and p.isidentifier():
            members.setdefault(p, []).append(c["refdes"])
    if not members or any(not p.isidentifier() for p in order if p):
        return {}
    seen: set[str] = set()
    prev = order[0] if order else ""
    for cur in order:
        if cur != prev:
            if cur in seen or (cur == "" and prev != "" and "" in seen):
                return {}
            seen.add(prev)
            prev = cur
    return members


def _with_placement(batch: dict[str, Any], placement: str | None) -> dict[str, Any]:
    """Copy only changed rows; placement policy never mutates the source."""
    if placement is None:
        return batch
    if placement not in {"keep", "omit"}:
        raise BoardScriptError("placement must be 'keep' or 'omit'")
    components = []
    for component in batch["components"]:
        c = dict(component)
        if placement == "keep":
            if "x" in c or "y" in c:
                if c.get("x") is None or c.get("y") is None:
                    raise BoardScriptError(
                        f"part {c['refdes']!r}: placement needs both x and y"
                    )
                c["fixed"] = "both"
        else:
            for key in (
                "x",
                "y",
                "rot",
                "layer",
                "fixed",
                "max_travel_mm",
                "group_offset",
            ):
                c.pop(key, None)
            if c.get("edge_mate") is not None:
                c["edge_mate"] = {k: v for k, v in c["edge_mate"].items() if k != "at"}
        components.append(c)
    return {**batch, "components": components}


def emit(
    batch: dict[str, Any],
    slug: str,
    source: str,
    *,
    annotations: dict[str, Any] | None = None,
    allow_loss: bool = False,
    placement: str | None = None,
    catalog_cache: str | Path | None = None,
    catalog_relative_path: str | None = None,
) -> str:
    """The ``main.py`` text for ``batch``; with default placement,
    ``build().to_batch()`` equals ``batch`` apart from legacy measure
    normalization. Raises :class:`BoardScriptError` on keys
    ``precis_geom.pcb`` cannot express unless ``allow_loss`` (then they
    are listed in the header comment).

    Args:
        batch: Board batch, including components, nets and connections.
        slug: Workspace board name.
        source: Provenance label or source filename, recorded in the header.
        annotations: Optional board handle/board_slug, part and footprint
            handle comments, and instance_ids/net_ids keyed by refdes/name.
        allow_loss: Report unsupported fields in comments instead of refusing.
        placement: ``keep`` locks supplied poses; ``omit`` removes poses,
            leashes, group offsets and edge positions. ``None`` preserves
            authored placement flags for the original exact round trip.
        catalog_cache: Offline LCSC-keyed footprint JSON used to prove matches.
        catalog_relative_path: Cache path relative to the emitted script;
            defaults to the cache filename. Keep that file with the script.
    """
    batch = _with_placement(batch, placement)
    source_annotations = annotations or {}
    annotations = {
        **source_annotations,
        "parts": {
            c["refdes"]: _source_comment(source_annotations, "parts", c["refdes"], slug)
            + (
                f" lcsc:{c.get('part') or c.get('part_lcsc')}"
                if (c.get("part") or c.get("part_lcsc"))
                else ""
            )
            for c in batch["components"]
        },
    }
    library: dict[str, tuple[str, str]] = {}
    catalog_refs: dict[str, str] = {}
    local_names = {f["name"] for f in batch.get("footprints") or []}
    if catalog_cache is not None:
        from precis.pcb.board_library import catalog_digest, catalog_matches

        library, cached, geometry_errors = catalog_matches(batch, Path(catalog_cache))
        for c in batch["components"]:
            if error := geometry_errors.get(c.get("footprint")):
                annotations["parts"][c["refdes"]] += f"; {error}"
            lcsc = c.get("part") or c.get("part_lcsc")
            row = cached.get(lcsc)
            if (
                isinstance(row, dict)
                and row.get("pads")
                and (
                    c.get("footprint") not in local_names
                    or c.get("footprint") in library
                )
            ):
                catalog_refs[lcsc] = catalog_digest(row)
    cache_path = catalog_relative_path or (
        Path(catalog_cache).name if catalog_cache else ""
    )

    def synthesized(c: dict[str, Any]) -> bool:
        if c.get("footprint") in library:
            return False
        if c.get("footprint") in local_names:
            return True
        return not (c.get("footprint") or c.get("part") or c.get("part_lcsc"))

    def catalog_ref(c: dict[str, Any]) -> str | None:
        lcsc = c.get("part") or c.get("part_lcsc")
        if lcsc in catalog_refs and not synthesized(c):
            return f"catalog[{lcsc!r}]"
        return None

    unknown = set(batch) - {
        "components",
        "nets",
        "connections",
        "features",
        "footprints",
        "planes",
        "net_classes",
        "roles",
        "generators",
        "measures",
        "meta",
        "profile",
    }
    if unknown:
        raise BoardScriptError(f"batch: unknown top-level keys {sorted(unknown)}")
    loss: list[str] = []
    net_names = {n["name"] for n in batch["nets"]}
    for plane in batch.get("planes") or []:
        if not isinstance(plane, dict) or not {"net", "layer"} <= plane.keys():
            raise BoardScriptError("plane assignment needs net and layer")
        if not isinstance(plane["net"], str) or plane["net"] not in net_names:
            raise BoardScriptError(f"plane assignment: unknown net {plane['net']!r}")
        if not isinstance(plane["layer"], str) or not plane["layer"].strip():
            raise BoardScriptError("plane assignment: layer must be a nonempty name")
        extra = set(plane) - {"net", "layer"}
        if extra:
            loss.append(
                f"plane assignment {plane['net']!r}: unknown keys {sorted(extra)}"
            )
    for g in batch.get("generators") or []:
        extra = set(g) - {"name", "generator", "params"}
        if extra:
            loss.append(f"generator {g.get('name')!r}: keys {sorted(extra)}")
    for m in batch.get("measures") or []:
        if set(m) - _MEASURE_KEYS:
            loss.append(
                f"measure {m.get('metric')!r}: keys "
                f"{sorted(set(m) - _MEASURE_KEYS)} are the stored shape, "
                "not the builder's"
            )
    for n in batch["nets"]:
        for k in set(n) - set(_NET_KEYS) - _RUN_KEYS:
            loss.append(f"net {n['name']!r}: {k!r} cannot emit")
    for k in batch["connections"]:
        if set(k) - {"net", "refdes", "pin", "note"}:
            loss.append(f"connection {k!r}: unknown keys")
    for c in batch["components"]:
        for k in set(c) - _PART_KEYS - _RUN_KEYS:
            loss.append(f"part {c['refdes']!r}: {k!r} cannot emit")
    for fp in batch.get("footprints") or []:
        for k in set(fp) - {
            "name",
            "pads",
            "pin_map",
            "courtyard",
            "centroid",
            "note",
            "holes",
            "graphics",
        }:
            loss.append(f"footprint {fp['name']!r}: unknown key {k!r}")
    if loss and not allow_loss:
        raise BoardScriptError(
            "batch carries keys precis_geom.pcb cannot express: "
            + "; ".join(loss[:8])
            + (" …" if len(loss) > 8 else ""),
            refs=tuple(
                dict.fromkeys(
                    f"{m.group(1)}:{m.group(2)}"
                    for l in loss
                    if (m := _LOSS_REF.match(l)) is not None
                )
            ),
        )

    room_of, room_labels = _source_rooms(batch)
    mods = {} if room_of else _modules(batch["components"])
    names = _Names()
    prelude: list[str] = []
    body: list[str] = []
    ctor = _kw(profile=batch.get("profile"))
    body.append(f"b = pcb.Board({slug!r}{', ' + ctor if ctor else ''})")
    if catalog_refs:
        body.append("catalog = {")
        for lcsc, digest in catalog_refs.items():
            body += [
                f"    {lcsc!r}: pcb.catalog_part(",
                f"        {lcsc!r}, Path(__file__).parent / {cache_path!r},",
                f"        sha256={digest!r},",
                "    ),",
            ]
        body.append("}")

    fps = batch.get("footprints") or []
    pad_var: dict[str, str] = {}
    pad_defs: list[str] = []
    pad_groups: dict[str, list[str]] = {}
    fp_by_name = {str(f["name"]): f for f in fps}
    for f in fps:
        if f["name"] in library:
            continue
        key = pprint.pformat(f.get("pads") or [], sort_dicts=True)
        pad_groups.setdefault(key, []).append(str(f["name"]))
    for names_in_group in pad_groups.values():
        first = names_in_group[0]
        var = "_pads_" + names.mint(first, "pads")
        for name in names_in_group:
            pad_var[name] = var
        lits = [_pad_literal(p) for p in (fp_by_name[first].get("pads") or [])]
        pad_defs.append(f"# synthesized footprint: {first!r} (source pads)")
        pad_defs.append(f"{var} = [")
        pad_defs += [f"    {l}," for l in lits]
        pad_defs.append("]")
    if fps:
        body.append("")
        body += pad_defs
        body.append("")
        body.append("# footprints (one pads binding per distinct pad set)")
        for fp in fps:
            comment = (annotations.get("footprints") or {}).get(fp["name"])
            if comment is not None:
                comment = " ".join(str(comment).splitlines())
            if fp["name"] in library:
                lcsc, digest = library[fp["name"]]
                rest = {k: v for k, v in fp.items() if k not in {"name", "pads"}}
                lines = [
                    "b.footprint(pcb.catalog_footprint(",
                    f"    {fp['name']!r}, {lcsc!r}, Path(__file__).parent / {cache_path!r},",
                    f"    sha256={digest!r}, **{_value(rest)},",
                    "))",
                ]
            else:
                lines = _emit_footprint(fp, pad_var[fp["name"]], loss)
            if comment is not None and (
                len(lines) > 1 or len("    " + lines[0]) + 3 + len(comment) > _LIMIT
            ):
                body.append(f"# {comment}")
                comment = None
            body += lines[:-1] + [lines[-1] + (f"  # {comment}" if comment else "")]
    if batch.get("features"):
        body.append("")
        body.append("# outline and mechanical features")
        body += [_emit_feature(f_) for f_ in batch["features"]]

    net_var: dict[str, str] = {}
    part_var: dict[str, str] = {}
    net_rooms: dict[str, set[str | None]] = {}
    for conn in batch["connections"]:
        net_rooms.setdefault(conn["net"], set()).add(room_of.get(conn["refdes"]))
    for room, label in room_labels.items():
        fn = names.mint(f"room_{label}", "room")
        pvar, nvar = names.mint(f"{fn}_parts", "room"), names.mint(f"{fn}_nets", "room")
        lines = [
            f"{('Source room ' + room + ' — ' + label)!r}",
            "parts = {}",
            "nets = {}",
        ]
        for n in batch["nets"]:
            if net_rooms.get(n["name"]) != {room}:
                continue
            var = names.mint(n["name"].split("/")[-1], "net")
            lines += _emit_net(
                n, var, _source_comment(source_annotations, "nets", n["name"], slug)
            )
            lines.append(f"nets[{n['name']!r}] = {var}")
            net_var[n["name"]] = f"{nvar}[{n['name']!r}]"
        for c in batch["components"]:
            if room_of.get(c["refdes"]) != room:
                continue
            var = names.mint(c["refdes"], "part")
            lines += _emit_part(
                c,
                var,
                annotations,
                loss,
                synthesized=synthesized(c),
                catalog_ref=catalog_ref(c),
            )
            lines.append(f"parts[{c['refdes']!r}] = {var}")
            part_var[c["refdes"]] = f"{pvar}[{c['refdes']!r}]"
        lines.append("return parts, nets")
        prelude += [
            "",
            f"def {fn}(b: pcb.Board{', catalog: dict[str, pcb.CatalogPart]' if catalog_refs else ''}) -> tuple[dict[str, pcb.Part], dict[str, pcb.Net]]:",
            *["    " + line.replace("\n", "\n    ") for line in lines],
        ]
        body += [
            "",
            f"# source room {room!r}",
            f"{pvar}, {nvar} = {fn}(b{', catalog' if catalog_refs else ''})",
        ]
    body.append("")
    body.append("# shared nets" if room_of else "# nets")
    for n in batch["nets"]:
        if n["name"] in net_var:
            continue
        var = names.mint(n["name"].split("/")[-1], "net")
        net_var[n["name"]] = var
        comment = _source_comment(source_annotations, "nets", n["name"], slug)
        body += _emit_net(n, var, comment)

    if batch.get("planes"):
        body.append("")
        body.append("# routing-layer net assignments")
        for plane in batch["planes"]:
            body.append(
                f"b.plane_assignment({net_var[plane['net']]}, {plane['layer']!r})"
            )

    emitted_mods: set[str] = set()
    body.append("")
    body.append("# parts")
    for c in batch["components"]:
        refdes = c["refdes"]
        if refdes in part_var:
            continue
        prefix = refdes.split(".")[0] if "." in refdes else ""
        if prefix and prefix in mods:
            if prefix in emitted_mods:
                continue
            emitted_mods.add(prefix)
            members = mods[prefix]
            mvar = names.mint(prefix, "mod")
            fn = names.mint(f"{prefix}_block", "mod")
            fn_body = ["parts = {}"]
            for refdes_m in members:
                cm = next(x for x in batch["components"] if x["refdes"] == refdes_m)
                local = refdes_m[len(prefix) + 1 :]
                lv = names.mint(local, "part")
                fn_body += _emit_part(
                    cm,
                    lv,
                    annotations,
                    loss,
                    board="board",
                    refdes=local,
                    synthesized=synthesized(cm),
                    catalog_ref=catalog_ref(cm),
                )
                fn_body.append(f"parts[{local!r}] = {lv}")
            prelude += [
                "",
                "@pcb.module",
                f"def {fn}(board: pcb.Board{', catalog: dict[str, pcb.CatalogPart]' if catalog_refs else ''}) -> dict[str, pcb.Part]:",
                *["    " + l for l in fn_body],
                "    return parts",
            ]
            for refdes_m in members:
                part_var[refdes_m] = f"{mvar}[{refdes_m[len(prefix) + 1 :]!r}]"
            body.append("")
            body.append(f"# module {prefix}")
            body.append(
                f"{mvar} = b.use({fn}, prefix={prefix!r}{', catalog=catalog' if catalog_refs else ''})"
            )
            continue
        var = names.mint(refdes.split(".")[-1], "part")
        part_var[refdes] = var
        body += _emit_part(
            c,
            var,
            annotations,
            loss,
            synthesized=synthesized(c),
            catalog_ref=catalog_ref(c),
        )
    groups = _emit_groups(batch["components"], part_var)
    if groups:
        body.append("")
        body.append("# rigid groups")
        body += groups
    for name, rules in (batch.get("net_classes") or {}).items():
        body.append(f"b.net_class({name!r}, **{_value(rules)})")
    body.append("")
    body.append("# connections (authored order)")
    body += _emit_connections(batch["connections"], net_var, part_var)
    if batch.get("meta"):
        body.append("")
        body.append(f"b.meta.update({_value(batch['meta'], indent=4)})")
    for name, r in (batch.get("roles") or {}).items():
        v = r.get("voltage") if isinstance(r, dict) else None
        body.append(
            f"b.role({name!r}{', voltage=' + repr(v) if v is not None else ''})"
        )
    for g in batch.get("generators") or []:
        body.append(
            f"b.generate({g['name']!r}, generator={g['generator']!r}, "
            f"**{_value(g.get('params') or {})})"
        )
    for m in batch.get("measures") or []:
        body.append(_emit_measure(m, part_var))
    body.append("return b")
    indented = "\n".join(
        ("    " + ln.replace("\n", "\n    ")) if ln else "" for ln in body
    )
    writer = "pcb-batch-to-script" if placement is not None else "pcb-batch-to-board"
    fidelity = (
        "``build().to_batch()`` preserves source parts, nets and pads;\n"
        + (
            "supplied positions are locked (fixed='both').\n"
            if placement == "keep"
            else "placement and pose-dependent constraints are omitted.\n"
        )
        if placement is not None
        else "``build().to_batch()`` is byte-equal to that source\n"
    )
    if batch.get("measures"):
        if placement is None:
            fidelity = "``build().to_batch()`` preserves source content with normalized measures.\n"
        fidelity += "Measures use builder operands, hard flags and defaults; reasons are comments.\n"
    if library:
        fidelity = (
            "Catalog footprints use a pinned local cache; pad aliases/defaults may normalize.\n"
            + (
                fidelity
                if placement is not None
                else "Source geometry and connectivity are preserved.\n"
            )
        )
    hierarchy = (
        "Hierarchy: grouped source rooms; declaration order may follow room membership."
        if room_of
        else (
            "Hierarchy: refdes prefix blocks."
            if mods
            else "Hierarchy: flat; source carries no schematic sheets or blocks."
        )
    )
    if room_of and placement is None:
        fidelity = (
            "Source content is preserved; room grouping may reorder declarations.\n"
        )
    header = f'''"""{slug} — the batch of ``{source}`` as a workspace script.

Generated by ``scripts/{writer}`` (``precis.pcb.board_script``);
{fidelity}(``tests/test_pcb_board_script.py``). The source stays the board's
documentation; this file is the construction.
{hierarchy}
'''
    if loss:
        header += (
            "\nNOT byte-equal — dropped keys (precis_geom.pcb cannot express them):\n"
        )
        header += "".join(f"#   - {l}\n" for l in loss)
    # Source labels are data even when they contain quotes or backslashes.
    header = (
        '"""' + header[3:].replace("\\", "\\\\").replace('"""', r"\"\"\"") + '"""\n'
    )
    return (
        header
        + """
from __future__ import annotations

"""
        + ("from pathlib import Path\n\n" if library or catalog_refs else "")
        + "import precis_geom.pcb as pcb\n\n"
        + "\n".join(prelude)
        + (
            """

def build() -> pcb.Board:
"""
            + indented
            + """


if __name__ == "__main__":
    pcb.write_manifest(build(), "out")
"""
        )
    )


# --------------------------------------------------------------------------
# Read-back: a stored board -> the batch (+ annotation handles)
# --------------------------------------------------------------------------


def _deauth_pad(p: dict[str, Any]) -> dict[str, Any]:
    """A normalized ``pcb_local_footprints`` pad -> the authored batch
    shape (``pin`` for ``number``; ``layer``/``rot``/``role``/``mask``/
    ``paste`` kept — they are authored keys too, so the batch stays
    byte-equal through ``to_batch``)."""
    return {
        k: v
        for k, v in (
            ("pin", p.get("number")),
            ("shape", p.get("shape")),
            ("x", p.get("x")),
            ("y", p.get("y")),
            ("w", p.get("w")),
            ("h", p.get("h")),
            ("poly", p.get("poly")),
            ("drill", p.get("drill")),
            ("slot", p.get("slot")),
            ("rot", p.get("rot")),
            ("rot_deg", p.get("rot_deg")),
            ("layer", p.get("layer")),
            ("role", p.get("role")),
            ("mask", p.get("mask")),
            ("paste", p.get("paste")),
        )
        if v is not None
    }


def board_batch(store: Any, slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``pcb_apply``-shaped batch of a stored board plus annotation
    handles — board ``pb<id>``, source slug, relational instance/net IDs,
    and ``{"parts": {refdes: "pn<id>"}}`` for every part whose LCSC
    resolves to a live part ref (:meth:`part_ref_id`).

    Reads only: ``get_ref``/``pcb_graph``/``pcb_load``/
    ``pcb_features_list``/``pcb_local_footprints_for``/
    ``pcb_measures_list``/``pcb_generators_for``/``part_ref_id``, plus the
    two raw SELECTs above for pin ``pad``/``description``/``note`` and the
    netconn ``note``, which no reader returns complete. Order is stored
    order (instance / net name / stored netconn), not the lost authored
    order.
    """
    ref = store.get_ref(kind="pcb", id=slug)
    if ref is None:
        raise BoardScriptError(f"no pcb {slug!r}", op="board_batch")
    ref_id = int(ref.id)
    graph = store.pcb_graph(ref_id)
    load = store.pcb_load(ref_id)
    load_nets = {str(n["name"]): n for n in (load.get("nets") or [])}

    pins: dict[str, list[dict[str, Any]]] = {}
    connections: list[dict[str, Any]] = []
    with store.pool.connection() as conn:
        for row in conn.execute(_PINS_SQL, (ref_id,)).fetchall():
            pin: dict[str, Any] = {"name": row[1]}
            if row[2] is not None:
                pin["pad"] = row[2]
            if row[3]:
                pin["tags"] = list(row[3])
            if row[4] is not None:
                pin["description"] = row[4]
            if row[5] is not None:
                pin["note"] = row[5]
            if row[6]:
                pin["meta"] = dict(row[6])
            pins.setdefault(str(row[0]), []).append(pin)
        for row in conn.execute(_CONNS_SQL, (ref_id,)).fetchall():
            k: dict[str, Any] = {"net": row[0], "refdes": row[1], "pin": row[2]}
            if row[3] is not None:
                k["note"] = row[3]
            connections.append(k)

    graph_by_refdes = {str(i["refdes"]): i for i in graph["instances"]}
    components = []
    for inst in load["instances"]:
        refdes = str(inst["refdes"])
        gi = graph_by_refdes.get(refdes) or {}
        c: dict[str, Any] = {"refdes": refdes}
        if inst.get("label") is not None:
            c["label"] = inst["label"]
        if inst.get("part_lcsc"):
            c["part"] = inst["part_lcsc"]
        if inst.get("footprint") is not None:
            c["footprint"] = inst["footprint"]
        if inst.get("height_mm") is not None:
            c["height_mm"] = inst["height_mm"]
        if inst.get("roles"):
            c["roles"] = list(inst["roles"])
        c["pins"] = pins.get(refdes) or []
        if inst.get("note") is not None:
            c["note"] = inst["note"]
        if inst.get("x") is not None:
            c["x"] = inst["x"]
            c["y"] = inst["y"]
            c["rot"] = inst.get("rot") or 0.0
            c["layer"] = inst.get("layer") or "top"
        if inst.get("fixed") is not None:
            c["fixed"] = inst["fixed"]
        if inst.get("max_travel_mm") is not None:
            c["max_travel_mm"] = inst["max_travel_mm"]
        # component meta blobs pcb_apply wrote from the batch —
        # graph's hoisted `part` is c.meta["part"] (the catalog-identity
        # blob), NOT the lcsc.
        if isinstance(gi.get("part"), dict):
            c["part_meta"] = dict(gi["part"])
        for key in ("supplies", "pin_swap_groups", "pin_decls"):
            if gi.get(key) is not None:
                c[key] = gi[key]
        for key in (
            "group",
            "group_offset",
            "pattern",
            "pattern_instance",
            "edge_mate",
        ):
            if gi.get(key) is not None:
                c[key] = gi[key]
        components.append(c)

    nets = []
    for n in graph["nets"]:
        name = str(n["name"])
        d: dict[str, Any] = {"name": name}
        if n.get("net_class") is not None:
            d["class"] = n["net_class"]
        for col, key in (
            ("est_current_a", "current"),
            ("working_voltage_v", "working_voltage_v"),
            ("impedance_ohm", "impedance"),
            ("edge_rate_v_per_ns", "edge_rate"),
            ("function_hint", "function"),
            ("domain", "domain"),
        ):
            if n.get(col) is not None:
                d[key] = n[col]
        ln = load_nets.get(name) or {}
        if ln.get("width_mm") is not None:
            d["width_mm"] = ln["width_mm"]
        if ln.get("note") is not None:
            d["note"] = ln["note"]
        if n.get("meta"):
            d["meta"] = dict(n["meta"])
        nets.append(d)

    features = [
        {k: v for k, v in f.items() if v is not None and k != "feature_id"}
        for f in store.pcb_features_list(ref_id)
    ]
    footprints = []
    for name, fp in sorted(store.pcb_local_footprints_for(ref_id).items()):
        entry: dict[str, Any] = {
            "name": name,
            "pads": [_deauth_pad(p) for p in (fp.get("pads") or [])],
        }
        for key in ("pin_map", "courtyard", "centroid"):
            if fp.get(key) is not None:
                entry[key] = fp[key]
        if fp.get("holes"):
            entry["holes"] = fp["holes"]
        if fp.get("graphics"):
            entry["graphics"] = fp["graphics"]
        footprints.append(entry)

    batch: dict[str, Any] = {
        "components": components,
        "nets": nets,
        "connections": connections,
    }
    if features:
        batch["features"] = features
    if footprints:
        batch["footprints"] = footprints
    if graph.get("net_classes"):
        batch["net_classes"] = {
            str(k): dict(v) for k, v in graph["net_classes"].items()
        }
    if graph.get("roles"):
        batch["roles"] = {
            str(k): (
                {"name": str(k), "voltage": v["voltage_v"]}
                if v.get("voltage_v") is not None
                else {"name": str(k)}
            )
            for k, v in graph["roles"].items()
        }
    generators = store.pcb_generators_for(ref_id)
    if generators:
        batch["generators"] = [
            {
                "name": name,
                "generator": g["generator"],
                "params": dict(g["params"] or {}),
            }
            for name, g in sorted(generators.items())
        ]
    measures = store.pcb_measures_list(ref_id)
    if measures:
        batch["measures"] = [
            {k: v for k, v in m.items() if v is not None and k != "measure_id"}
            for m in measures
        ]
    meta = dict(ref.meta or {})
    if meta:
        batch["meta"] = meta
    board = graph.get("board") or {}
    profile = (board.get("meta") or {}).get("profile")
    if profile is not None:
        batch["profile"] = profile

    annotations: dict[str, Any] = {
        "board": f"pb{ref_id}",
        "board_slug": slug,
        "parts": {},
        "footprints": {},
        "instance_ids": {
            str(i["refdes"]): i["instance_id"]
            for i in load["instances"]
            if i.get("instance_id") is not None
        },
        "net_ids": {
            str(n["name"]): n["net_id"]
            for n in load["nets"]
            if n.get("net_id") is not None
        },
    }
    for lcsc in sorted({str(c["part"]) for c in components if c.get("part")}):
        pid = store.part_ref_id(lcsc)
        if pid is not None:
            for c in components:
                if c.get("part") == lcsc:
                    annotations["parts"][c["refdes"]] = f"pn{pid}"
    return batch, annotations


def main(argv: list[str] | None = None) -> int:
    """Write a batch JSON file as offline workspace Python.

    Args:
        argv: CLI arguments, or ``None`` to read process arguments.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Write a local PCB workspace script from a board batch JSON file."
    )
    parser.add_argument("batch", type=Path, help="input board batch JSON")
    parser.add_argument("out", type=Path, help="output workspace Python file")
    parser.add_argument("--placement", choices=("keep", "omit"), default="keep")
    parser.add_argument(
        "--catalog-footprints", type=Path, help="offline footprint cache JSON"
    )
    args = parser.parse_args(argv)
    try:
        if args.batch.resolve() == args.out.resolve():
            raise BoardScriptError(
                "input batch and output script must be different files"
            )
        if args.out.suffix != ".py":
            raise BoardScriptError("output workspace script must have a .py suffix")
        batch = json.loads(args.batch.read_text(encoding="utf-8"))
        if not isinstance(batch, dict) or any(
            not isinstance(batch.get(key), list)
            for key in ("components", "nets", "connections")
        ):
            raise BoardScriptError("batch needs components, nets and connections lists")
        relative = (
            os.path.relpath(
                args.catalog_footprints.resolve(), args.out.parent.resolve()
            )
            if args.catalog_footprints
            else None
        )
        text = emit(
            batch,
            args.batch.stem,
            str(args.batch),
            placement=args.placement,
            catalog_cache=args.catalog_footprints,
            catalog_relative_path=relative,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
    print(
        f"wrote {args.out} ({len(batch['components'])} parts, placement={args.placement})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
