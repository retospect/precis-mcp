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
    own, by design)."""
    for f in features:
        geom = f.get("geom") or {}
        if str(f.get("ftype") or "") == "outline" and isinstance(
            geom.get("path"), list
        ):
            return [[float(p[0]), float(p[1])] for p in geom["path"]]
    return None


def build_ir(
    graph: dict[str, Any], *, outline: list[list[float]] | None = None
) -> pcb_ir.PcbIR:
    """Build a fresh L0(+L3) IR from a :meth:`Store.pcb_graph` payload.
    ``outline`` (see :func:`outline_from_features`) is optional — a caller
    with no board-outline feature yet (or one that hasn't been updated to
    fetch it) simply gets an IR whose ``outline`` is ``None``, same as
    today; ``cost.py``'s ``board_edge_clearance`` term and ``optimize.
    py``'s TRANSLATE clamp both already degrade cleanly for that case."""
    board = graph.get("board") or {}
    stackup = board.get("stackup")
    return pcb_ir.from_graph(sorted_graph(graph), stackup=stackup, outline=outline)


def signal_layers(ir: pcb_ir.PcbIR) -> list[int]:
    """Stackup indices whose role is ``'signal'`` — the only layers that
    ever carry a routed trace (plane/dielectric/stiffener layers don't)."""
    return [i for i, layer in enumerate(ir.stackup) if layer.get("role") == "signal"]


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
    "apply_route_overrides",
    "build_ir",
    "content_hash",
    "extract_sketch",
    "outline_from_features",
    "positions",
    "segment_key",
    "signal_layers",
    "sorted_graph",
]
