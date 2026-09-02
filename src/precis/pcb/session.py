"""Shared IR<->DB session glue for the ``pcb_place``/``pcb_route`` worker
jobs (docs/backlog/pcb-guided-place-route.md Slice 10 — the phase-machine
gate hookup needs the tool surface to actually RUN ``optimize.py``/
``realize.py``, not just unit-test them).

Deliberately thin and self-contained: builds a
:class:`~precis.pcb.ir.PcbIR` from :meth:`Store.pcb_graph`, re-applies any
previously-persisted sketch choices (pinned side / layer assignment) onto
the fresh IR before a run, and serializes the settled IR back into the
``pcb_routes`` shape after one. No optimizer/cost/realizer logic lives
here — this module never imports :mod:`precis.pcb.optimize`,
:mod:`precis.pcb.cost`, or :mod:`precis.pcb.drc`.

**Segment identity must be durable across IR rebuilds** — the IR is never
cached between runs (``build_ir`` is called fresh every job), so the
in-memory integer ``seg_id`` :mod:`precis.pcb.ir` uses is meaningless
across two runs. :func:`segment_key` names a segment by its two endpoint
pins (``"REFDES.PIN"``, sorted) instead — that identity survives a rebuild,
which is what lets a pinned topology choice (or a targeted rip-up) persist
in ``pcb_routes`` and still land on the SAME segment next time.

**Hub selection must be deterministic, which the raw graph isn't.**
:func:`~precis.pcb.ir.from_graph` stars each net off its first
``members`` entry (the "hub"); :meth:`Store.pcb_graph`'s SQL carries no
``ORDER BY`` on net membership, so which pin comes first is whatever join
order Postgres happens to pick that call — silently reshuffling every
segment's identity run to run. :func:`sorted_graph` fixes this once, at
the IR-build boundary, by sorting each net's members by ``(refdes, pin)``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from precis.pcb import geom as pcb_geom
from precis.pcb import ir as pcb_ir


def _pin_key(refdes: str, pin: str) -> str:
    return f"{refdes}.{pin}"


def sorted_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """A shallow copy of ``graph`` with every net's ``members`` sorted by
    ``(refdes, pin)`` — see the module docstring for why this must run
    before :func:`build_ir` calls :func:`~precis.pcb.ir.from_graph`."""
    nets = []
    for net in graph.get("nets") or []:
        members = sorted(
            net.get("members") or [], key=lambda m: (m["refdes"], m["pin"])
        )
        nets.append({**net, "members": members})
    return {**graph, "nets": nets}


def outline_from_features(features: list[dict[str, Any]]) -> list[list[float]] | None:
    """The ``ftype='outline'`` feature's ``geom.path`` (a polygon ring of
    ``[x, y]`` pairs), or ``None`` when the design hasn't authored one yet
    — the same extraction :meth:`~precis.handlers.pcb.PcbHandler.
    _outline_from_features` does off :meth:`Store.pcb_features_list`
    (mirrored here, not imported: that handler owns the tool surface and
    isn't this module's to reach into — see the module docstring's "never
    imports optimize/cost/drc" boundary, which extends to not importing
    the handler layer either). ``build_ir``'s own caller is responsible
    for fetching ``features`` (this module has no store handle of its
    own, by design).

    ``geom.corner_radius_mm`` is honoured here exactly as the handler
    mirror honours it (fillet + polygonize via :func:`precis.pcb.geom.
    rounded_polygon`) — the two extractions drifting on the radius would
    hand the router a SHARP outline for a board every render draws
    rounded, i.e. copper poured into corners the fab mills away."""
    for f in features:
        geom = f.get("geom") or {}
        if str(f.get("ftype") or "") == "outline" and isinstance(
            geom.get("path"), list
        ):
            path = [[float(p[0]), float(p[1])] for p in geom["path"]]
            radius = geom.get("corner_radius_mm")
            if radius is not None and float(radius) > 0:
                ring: list[pcb_geom.Point] = [(p[0], p[1]) for p in path]
                return [
                    [p[0], p[1]] for p in pcb_geom.rounded_polygon(ring, float(radius))
                ]
            return path
    return None


def mounting_holes_from_features(
    features: list[dict[str, Any]],
) -> tuple[pcb_ir.MountingHole, ...]:
    """Every ``ftype='mounting_hole'`` feature as a
    :class:`~precis.pcb.ir.MountingHole` — the board-config companion to
    :func:`outline_from_features`, and carried onto the IR for the same
    reason the outline is: a hole only the handler's feature list knows
    about is one the router draws copper through (round-3 review item 4,
    the ``npth_clearance`` family). ``geom.diameter`` is the drill;
    optional ``geom.ring_dia_mm`` (+ ``geom.plated``) describe a
    solder-nut's copper annulus."""
    out: list[pcb_ir.MountingHole] = []
    for f in features:
        if str(f.get("ftype") or "") != "mounting_hole":
            continue
        geom = f.get("geom") or {}
        try:
            x, y = float(f.get("x", 0.0)), float(f.get("y", 0.0))
            drill = float(geom.get("diameter") or 0.0)
        except (TypeError, ValueError):
            continue
        if drill <= 0.0:
            continue
        out.append(
            pcb_ir.MountingHole(
                x=x,
                y=y,
                drill_mm=drill,
                ring_dia_mm=float(geom.get("ring_dia_mm") or 0.0),
                plated=bool(geom.get("plated")),
            )
        )
    return tuple(out)


def build_ir(
    graph: dict[str, Any],
    *,
    outline: list[list[float]] | None = None,
    mounting_holes: tuple[pcb_ir.MountingHole, ...] = (),
) -> pcb_ir.PcbIR:
    """Build a fresh L0(+L3) IR from a :meth:`Store.pcb_graph` payload.
    ``outline`` (see :func:`outline_from_features`) is optional — a caller
    with no board-outline feature yet (or one that hasn't been updated to
    fetch it) simply gets an IR whose ``outline`` is ``None``, same as
    today; ``cost.py``'s ``board_edge_clearance`` term and ``optimize.
    py``'s TRANSLATE clamp both already degrade cleanly for that case.
    ``mounting_holes`` (see :func:`mounting_holes_from_features`) rides
    the same pattern — ``()`` degrades to today's router-blind behavior.

    An instance row in ``graph["instances"]`` may also carry ``"group"``/
    ``"group_offset"`` (an authored rigid "super footprint") or
    ``"pattern"``/``"pattern_instance"`` (an auto-derived rigid tile) —
    parsed straight through by :func:`~precis.pcb.ir.from_graph` (see
    :func:`precis.pcb.ir._parse_instance_groups`) into
    :attr:`~precis.pcb.ir.PcbIR.inst_group` and friends; this function has
    no group-specific logic of its own, it just forwards whatever the
    graph rows already carry, same as every other per-instance field."""
    board = graph.get("board") or {}
    stackup = board.get("stackup")
    return pcb_ir.from_graph(
        sorted_graph(graph),
        stackup=stackup,
        outline=outline,
        mounting_holes=mounting_holes,
    )


def footprints_by_refdes(
    ir: pcb_ir.PcbIR, footprints_by_lcsc: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Remap :meth:`~precis.store.Store.pcb_footprints_for`'s C-number-keyed
    cache onto the refdes-keyed shape :func:`precis.pcb.realize.pad_geometry`
    (and everything built on top of it — :func:`~precis.pcb.realize.
    pads_for_ir`, :func:`~precis.pcb.realize.realize`) accepts as
    ``footprints``.

    This is the missing join, not a new lookup: :attr:`~precis.pcb.ir.
    PcbIR.instance_part_lcsc` (from :meth:`Store.pcb_graph`'s own
    ``pcb_components`` join) is the ONLY thing on the IR side that knows an
    instance's C-number, and ``pcb_footprints_for``'s cache is the ONLY
    thing on the store side that knows a C-number's real pad geometry —
    every caller with both a :class:`PcbIR` and a live ``Store`` handle
    needs exactly this remap before ``footprints=`` means anything.

    An instance with no linked catalog part (``instance_part_lcsc`` is
    ``None``), or whose C-number has no cached footprint row yet, is
    simply absent from the result — :func:`~precis.pcb.realize.
    pad_geometry`'s own per-pin fallback to synthesized geometry already
    handles "no real data for this instance" correctly; this function
    doesn't need a second way to say the same thing.
    """
    out: dict[str, dict[str, Any]] = {}
    for inst_id in range(ir.n_instances):
        lcsc = ir.instance_part_lcsc[inst_id]
        if not lcsc:
            continue
        fp = footprints_by_lcsc.get(str(lcsc))
        if fp:
            out[str(ir.instance_refdes[inst_id])] = fp
    return out


def signal_layers(ir: pcb_ir.PcbIR) -> list[int]:
    """The stackup indices that may carry a routed trace — a thin
    delegate to :func:`precis.pcb.ir.routable_layers` (the single answer
    to "may this layer be routed", now independent of whether the SAME
    layer also carries a copper pour; see that function's docstring).
    Kept under this name (rather than renamed to ``routable_layers``
    everywhere) only because it is this module's own established public
    name; :mod:`precis.pcb.realize` used to keep a second, duplicated
    four-line copy of this exact query rather than import it (its own
    docstring said so) — that duplication is now gone, both modules call
    :func:`precis.pcb.ir.routable_layers` directly."""
    return pcb_ir.routable_layers(ir)


def segment_key(ir: pcb_ir.PcbIR, seg_id: int) -> str:
    """``seg_id``'s durable identity: its two endpoint pins, sorted."""
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
    ka = _pin_key(str(ir.instance_refdes[ia]), str(ir.pin_label[a]))
    kb = _pin_key(str(ir.instance_refdes[ib]), str(ir.pin_label[b]))
    return "|".join(sorted((ka, kb)))


def apply_route_overrides(
    ir: pcb_ir.PcbIR, routes_by_net: dict[str, dict[str, Any]]
) -> None:
    """Re-apply a design's previously-PERSISTED sketch (pinned side
    choices + layer assignments, ``pcb_routes.topology``/``layer_assign``)
    onto a freshly-built IR, matched via :func:`segment_key`. A segment
    named in the persisted sketch that no longer exists (the netlist
    changed under it) is silently skipped — never an error, since the
    optimizer will just re-decide it fresh."""
    seg_by_key = {segment_key(ir, s): s for s in range(ir.n_segments)}
    for row in routes_by_net.values():
        if not row:
            continue
        for entry in row.get("topology") or []:
            seg_id = seg_by_key.get(
                "|".join(sorted((entry.get("a", ""), entry.get("b", ""))))
            )
            side = entry.get("side")
            if seg_id is not None and side is not None:
                ir.set_side(seg_id, int(side))
        for entry in row.get("layer_assign") or []:
            seg_id = seg_by_key.get(
                "|".join(sorted((entry.get("a", ""), entry.get("b", ""))))
            )
            layer = entry.get("layer")
            if seg_id is not None and layer is not None:
                ir.set_layer(seg_id, int(layer))


def apply_pin_swap_overrides(ir: pcb_ir.PcbIR, overrides: list[dict[str, Any]]) -> None:
    """Re-apply a design's previously-PERSISTED pin<->net overrides
    (``pcb_pin_swaps``, docs/backlog/pcb-engine-plan.md "PIN_SWAP is not
    persisted") onto a freshly-built IR. ``PcbIR.swap_pins`` operates on
    IR-local pin ints that are meaningless across a rebuild (the same
    reason :func:`segment_key` exists for segments), so each override is
    matched by durable identity (``refdes``/``pin`` name, via
    :func:`_pin_key`) instead.

    A fresh ``build_ir`` always starts from ``pcb_netconns``'s authored
    wiring (the override's baseline), so re-applying an override is
    "find whichever OTHER pin on this instance currently holds the target
    net, and swap with it" — the same fixed-pivot transposition
    :mod:`precis.pcb.pinswap` already uses, just driven by the persisted
    target instead of a fresh min-cost solve. An override naming a pin
    that no longer exists, a net that no longer resolves, or an instance
    whose OTHER pins no longer carry the target net at all (the netlist
    changed under it) is silently skipped — never an error, since a fresh
    optimizer run is free to re-decide it; :meth:`PcbIR.swap_pins`'s own
    equal-rotation-degree guard is honoured the same way (``ValueError``
    from a genuinely stale override is swallowed, not raised)."""
    pin_by_key: dict[str, int] = {}
    for p in range(ir.n_pins):
        inst = int(ir.pin_instance[p])
        pin_by_key[_pin_key(str(ir.instance_refdes[inst]), str(ir.pin_label[p]))] = p
    net_name_to_id = {str(ir.net_name[n]): n for n in range(ir.n_nets)}
    for entry in overrides:
        pin_id = pin_by_key.get(
            _pin_key(str(entry.get("refdes", "")), str(entry.get("pin", "")))
        )
        target_net = net_name_to_id.get(str(entry.get("net", "")))
        if pin_id is None or target_net is None:
            continue
        if int(ir.pin_net[pin_id]) == target_net:
            continue
        inst = int(ir.pin_instance[pin_id])
        partner = next(
            (
                p
                for p in range(ir.n_pins)
                if int(ir.pin_instance[p]) == inst and int(ir.pin_net[p]) == target_net
            ),
            None,
        )
        if partner is None:
            continue
        try:
            ir.swap_pins(pin_id, partner)
        except ValueError:
            continue


def pin_swap_diff(ir: pcb_ir.PcbIR, baseline_pin_net: Any) -> list[dict[str, Any]]:
    """Every physical pin whose settled net (``ir.pin_net``, after an
    anneal) differs from ``baseline_pin_net`` (a copy of ``ir.pin_net``
    taken right after :func:`build_ir`, i.e. ``pcb_netconns``'s authored
    wiring) — the derived pin-swap write-back's raw material, mirroring
    how the plane write-back reads the anneal's FINAL ``net_plane_layer``
    state rather than replaying its move history. Each entry is durably
    keyed (``refdes``/``pin``/settled ``net`` name), ready for
    :meth:`~precis.store._pcb_ops.PcbMixin.pcb_pin_swaps_replace_derived`
    once the caller has excluded any pin already covered by an authored
    override."""
    out: list[dict[str, Any]] = []
    for p in range(ir.n_pins):
        settled_net = int(ir.pin_net[p])
        if settled_net == int(baseline_pin_net[p]):
            continue
        inst = int(ir.pin_instance[p])
        out.append(
            {
                "refdes": str(ir.instance_refdes[inst]),
                "pin": str(ir.pin_label[p]),
                # `pcb_ir.NO_NET` is -1, a LIVE numpy index -- `ir.net_name
                # [-1]` doesn't raise, it silently wraps to the LAST real
                # net's name. A swap that moves a pin OFF onto NO_NET
                # (there is no such move today, but nothing prevents one)
                # would otherwise get written back as a real connection to
                # whatever net happens to sit last in the array. Same
                # convention `precis.pcb.realize.pads_for_ir` settled on
                # for the identical sentinel-collision hazard: an empty
                # net name, matching `connectivity._pad_primitives`'s own
                # "empty net name is skipped" rule.
                "net": (
                    ""
                    if settled_net == pcb_ir.NO_NET
                    else str(ir.net_name[settled_net])
                ),
            }
        )
    return out


def extract_sketch(ir: pcb_ir.PcbIR) -> dict[str, dict[str, Any]]:
    """The settled IR's per-net sketch, ready for
    :meth:`Store.pcb_routes_write` — ``tree``/``topology``/``layer_assign``
    each a list of segment records keyed by :func:`segment_key`'s two pin
    endpoints, never by the ephemeral ``seg_id``."""
    out: dict[str, dict[str, Any]] = {}
    for seg_id in range(ir.n_segments):
        net_id = int(ir.seg_net[seg_id])
        net_name = str(ir.net_name[net_id])
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
        ka = _pin_key(str(ir.instance_refdes[ia]), str(ir.pin_label[a]))
        kb = _pin_key(str(ir.instance_refdes[ib]), str(ir.pin_label[b]))
        entry = out.setdefault(
            net_name, {"tree": [], "topology": [], "layer_assign": []}
        )
        entry["tree"].append({"a": ka, "b": kb})
        entry["topology"].append({"a": ka, "b": kb, "side": int(ir.seg_side[seg_id])})
        layer = int(ir.seg_layer[seg_id])
        if layer != pcb_ir.UNSET_LAYER:
            entry["layer_assign"].append({"a": ka, "b": kb, "layer": layer})
    return out


def positions(ir: pcb_ir.PcbIR) -> dict[str, tuple[float, float, float]]:
    """Every PLACED instance's ``(x, y, rot)`` — an instance the optimizer
    never seeded (still ``NaN``) is omitted rather than written as a
    phantom ``(nan, nan, ...)`` pose."""
    out: dict[str, tuple[float, float, float]] = {}
    for i in range(ir.n_instances):
        x, y, rot = float(ir.inst_x[i]), float(ir.inst_y[i]), float(ir.inst_rot[i])
        if x == x and y == y:  # NaN != NaN
            out[str(ir.instance_refdes[i])] = (x, y, rot)
    return out


def content_hash(graph: dict[str, Any], params: dict[str, Any]) -> str:
    """A stable digest of the netlist+placement+params an op runs against
    — the ``content-hash`` half of the ``(design, op, content-hash)``
    idempotency key (backlog, verbatim). Deliberately excludes the
    volatile ``route_status``/``board_id`` summary fields
    :meth:`Store.pcb_graph` also carries — including those would make a
    route job's OWN write-back change the hash of an otherwise-identical
    re-submit, defeating idempotency."""
    instances = sorted(
        (i["refdes"], i.get("x"), i.get("y"), i.get("rot"), i.get("fixed"))
        for i in graph.get("instances") or []
    )
    nets = sorted(
        (
            n["name"],
            n.get("net_class"),
            tuple(sorted((m["refdes"], m["pin"]) for m in n.get("members") or [])),
        )
        for n in graph.get("nets") or []
    )
    stackup = (graph.get("board") or {}).get("stackup")
    payload = json.dumps(
        {"instances": instances, "nets": nets, "stackup": stackup, "params": params},
        sort_keys=True,
        default=str,
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()


__all__ = [
    "apply_pin_swap_overrides",
    "apply_route_overrides",
    "build_ir",
    "content_hash",
    "extract_sketch",
    "footprints_by_refdes",
    "mounting_holes_from_features",
    "outline_from_features",
    "pin_swap_diff",
    "positions",
    "segment_key",
    "signal_layers",
    "sorted_graph",
]
