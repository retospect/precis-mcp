"""Offline place/route/DRC/gerbers — the laptop half of
``docs/backlog/pcb-workspace-code.md`` §Q3 (slice 4, the
``precis-geom[pcb-kernel]`` extra).

The script-side surface is ``precis_geom.pcb.local`` (a lazy alias for
this module): ``pcb.local.place`` / ``pcb.local.route`` /
``pcb.local.drc`` / ``pcb.local.gerbers`` drive the SAME engines the
``pcb_place``/``pcb_route`` job types run — :mod:`precis.pcb.optimize`,
:mod:`precis.pcb.realize`, :mod:`precis.pcb.session`, :mod:`precis.pcb.drc`,
:mod:`precis.pcb.gerber` — in-process, with the batch dict the builder
emits standing in for the store rows. Nothing here reimplements an
engine; what the store path gets from ``pcb_graph``/``pcb_planes``/
``pcb_footprints`` this module derives from ``board.to_batch()``.

**Preview, not board state.** A laptop run writes ``out/*.json`` /
``gerbers.zip`` files only — the never-worse guard, judged mutation,
``last_route`` and recorded DRC runs are all server-side. Only a cluster
``pcb.place``/``pcb.route`` result is board state (§Risks: two placers of
record — the wheel version may differ from the server's).

Footprints: a laptop has no ``part_footprints`` cache, so a part with
``lcsc=`` and no authored ``footprint=`` falls to the ``landpattern``
synthesized bound exactly as the fixtures flag it. Two ways to supply
real pads offline: ``meta.catalog_footprints[lcsc]`` may carry the cache
row verbatim (``{pads, pin_map, courtyard, centroid, holes}`` — the shape
a design export embeds), and every ``place``/``route`` call takes
``catalog={lcsc: row}`` for the rows a caller already holds.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
from typing import Any

from precis.pcb import DANGLING_NET_NOTE, DEFAULT_STACKUP, STACKUP_PRESETS
from precis.pcb import drc as pcb_drc
from precis.pcb import planes as pcb_planes
from precis.pcb import profiles as pcb_profiles
from precis.pcb import realize as pcb_realize
from precis.pcb import rows as pcb_rows
from precis.pcb import session as pcb_session
from precis.pcb.geom import segments_cross
from precis.pcb.ir import PcbIR, net_member_counts, segment_points
from precis.pcb.optimize import (
    PLACE_ITERS,
    PLACE_ONLY_SCHEDULE,
    OptimizeConfig,
    optimize,
    resolve_measures,
)


class LocalError(ValueError):
    """A refusal the offline run raises before spending the solve — the
    laptop analogue of a job's ``record_failure`` (outline defect,
    fixed-copper collision, pre-route placement gate, unsupported
    board shape). Carries the same fix text a job summary would."""


# ── batch → engine surfaces ────────────────────────────────────────────
#
# ``to_batch()`` emits the ``pcb_apply`` argument shape; the engines read
# the ``pcb_graph`` shape. The store normalises between them at insert —
# this block mirrors that normalisation 1:1 (component keys hoisted the
# same way, pin markers folded into tags/meta the same way), so an
# offline board and its server-side twin hydrate identical IRs.


def _stackup(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """The board's stackup rows — ``meta.stackup_preset`` names a
    :data:`STACKUP_PRESETS` entry, ``meta.stackup_layers`` is the
    authored layer list verbatim, else the house default. All go
    through :func:`precis.pcb.ir.validate_stackup`, the same normaliser
    ``op='stackup'`` runs (bare layer names become signal rows first —
    ``op='stackup'`` itself takes dicts only)."""
    from precis.pcb.ir import OUTER_LAYER_NAMES, validate_stackup

    meta = batch.get("meta") or {}
    preset = meta.get("stackup_preset")
    if preset is not None:
        if str(preset) not in STACKUP_PRESETS:
            raise LocalError(
                f"unknown stackup preset {preset!r} — known: {sorted(STACKUP_PRESETS)}"
            )
        layers = copy.deepcopy(STACKUP_PRESETS[str(preset)])
        # The server refuses the same disagreement
        # (``store._pcb_apply_meta_stackup``) — an offline run accepting
        # what the put rejects would break the twin parity.
        authored = meta.get("stackup_layers")
        if isinstance(authored, list) and len(authored) != len(layers):
            raise LocalError(
                f"stackup_preset {preset!r} expands to {len(layers)} layers "
                f"but stackup_layers lists {len(authored)} — the two keys "
                "disagree"
            )
    elif meta.get("stackup_layers"):
        layers = copy.deepcopy(meta["stackup_layers"])
    else:
        layers = copy.deepcopy(DEFAULT_STACKUP)
    try:
        # ``Board(stackup=4)``/an authored ``meta.stackup_layers`` emits
        # bare names — validate_stackup wants dicts, so a bare name
        # becomes a signal row. Inner rows take the explicit
        # ``routable: True`` DEFAULT_STACKUP stamps, matching the
        # normalisation ``store._pcb_apply_meta_stackup`` runs on the
        # same keys server-side (gr515320) — the parity must be literal.
        return validate_stackup(
            [
                entry
                if isinstance(entry, dict)
                else {
                    "name": str(entry),
                    "role": "signal",
                    **(
                        {"routable": True}
                        if str(entry) not in OUTER_LAYER_NAMES
                        else {}
                    ),
                }
                for entry in layers
            ]
        )
    except ValueError as exc:
        raise LocalError(str(exc)) from exc


def batch_to_graph(
    batch: dict[str, Any], *, footprints: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A ``to_batch()`` payload as a ``Store.pcb_graph``-shaped dict —
    ``{board, instances, nets (with members), net_classes, roles,
    unconnected}``, STORE-ordered (instances by refdes, nets by name,
    members and unconnected by ``(refdes, pin)``) so a wheel batch and its
    ``pcb_apply``-then-``pcb_graph`` twin compare literally.

    Generators in the batch are expanded first
    (:func:`precis.pcb.generators.expand`, the trusted-side engine run
    in-process for the preview). ``footprints=`` is the LCSC-keyed
    ``part_footprints`` stand-in (``_catalog_footprints``'s output) — it
    joins components via ``part_lcsc`` exactly as
    ``_pcb_footprint_ref`` resolves, AFTER a design-local footprint named
    by ``footprint``. Both feed the two store passes a preview otherwise
    misses: the pad-number → pin-name connection rewrite
    (:func:`precis.pcb.rows.map_pad_numbers`, gr495291) and declare-all
    completion (every footprint pad label becomes a pin — the ghost pins
    in ``unconnected``'s count and ``n_pins``)."""
    batch = _expand_generators(batch)
    components = batch.get("components") or []
    local_fps = _local_footprints(batch)
    cat_fps = {str(k).strip().upper(): v for k, v in (footprints or {}).items()}
    pin_decls: dict[str, dict[str, dict[str, Any]]] = {}
    declared: dict[str, set[str]] = {}
    fp_by_refdes: dict[str, dict[str, Any]] = {}
    completed: dict[str, dict[str, dict[str, Any]]] = {}
    live: dict[str, set[str]] = {}
    for c in components:
        refdes = str(c.get("refdes") or "").strip()
        decls = pin_decls.setdefault(refdes, {})
        for p in c.get("pins") or []:
            decls[str(p.get("name"))] = p
        declared[refdes] = set(decls)
        fp = None
        if c.get("footprint") is not None:
            fp = local_fps.get(str(c["footprint"]))
        if fp is None:
            raw = c.get("part_lcsc") or c.get("part") or c.get("lcsc")
            key = str(pcb_rows.lcsc_id(raw) or raw or "").strip().upper()
            if key:
                fp = cat_fps.get(key)
        if fp is not None:
            fp_by_refdes[refdes] = fp
        labels = pcb_rows.footprint_pin_labels(fp)
        # ``_pcb_complete_footprint_pins``: join-failed instances (an
        # active declared name matching no pad label) complete NOTHING;
        # a label a declared pin already owns mints nothing either.
        done = (
            {k: v for k, v in labels.items() if k not in declared[refdes]}
            if fp is not None and not (declared[refdes] - set(labels))
            else {}
        )
        completed[refdes] = done
        live[refdes] = declared[refdes] | set(done)

    connections, _mapped = pcb_rows.map_pad_numbers(
        list(batch.get("connections") or []), fp_by_refdes, live
    )

    def _facts(refdes: str, pin: str) -> tuple[list[str], dict[str, Any]]:
        d = pin_decls.get(refdes, {}).get(pin)
        if d is not None:
            return pcb_rows.pin_tags_meta(d, refdes, declared.get(refdes))
        ent = completed.get(refdes, {}).get(pin)
        if ent is not None:
            return list(ent.get("tags") or []), {"completed_from_footprint": True}
        return [], {}  # a connection-minted ghost pin — no facts

    # ``_pcb_connect`` upserts per (instance, pin): a pin wired twice
    # lands ONE netconn on the LAST connection's net — dedupe the same
    # way or a pin wired by pad number AND name doubles its member.
    pin_net: dict[tuple[str, str], str] = {}
    for conn in connections:
        refdes = str(conn.get("refdes") or "").strip()
        pin = str(conn.get("pin") or "").strip()
        pin_net[(refdes, pin)] = str(conn.get("net") or "").strip()
    members: dict[str, list[dict[str, Any]]] = {}
    wired: dict[str, set[str]] = {}
    for (refdes, pin), net in pin_net.items():
        tags, meta = _facts(refdes, pin)
        members.setdefault(net, []).append(
            {"refdes": refdes, "pin": pin, "tags": tags, "meta": meta}
        )
        wired.setdefault(refdes, set()).add(pin)

    nets_by_name: dict[str, dict[str, Any]] = {}
    for n in batch.get("nets") or []:
        row = pcb_rows.net_row(n)
        if row["name"] in nets_by_name:
            pcb_rows.net_patch(nets_by_name[row["name"]], n)
        else:
            nets_by_name[row["name"]] = row
    for net_name in members:  # ``_pcb_connect`` auto-creates the net
        if net_name not in nets_by_name:
            nets_by_name[net_name] = pcb_rows.net_row({"name": net_name})
    nets = []
    for name in sorted(nets_by_name):
        row = nets_by_name[name]
        row["members"] = sorted(
            members.get(name, []), key=lambda m: (m["refdes"], m["pin"])
        )
        nets.append(row)

    unconnected = []
    for refdes in sorted(live):
        wired_here = wired.get(refdes) or set()
        for pin in sorted(live[refdes] - wired_here):
            tags, meta = _facts(refdes, pin)
            unconnected.append(
                {"refdes": refdes, "pin": pin, "tags": tags, "meta": meta}
            )

    instances = []
    for c in sorted(components, key=lambda c: str(c.get("refdes") or "").strip()):
        refdes = str(c.get("refdes") or "").strip()
        instances.append(
            pcb_rows.instance_row(
                c,
                pin_names=live.get(refdes, set()) | wired.get(refdes, set()),
                declared=declared.get(refdes) or None,
            )
        )
    raw_roles = batch.get("roles") or {}
    role_items = (
        ((r.get("name"), r) for r in raw_roles)
        if isinstance(raw_roles, list)
        else raw_roles.items()
    )
    roles = {
        str(name): {
            "voltage_v": (r or {}).get("voltage", (r or {}).get("voltage_v")),
            "fanout": (r or {}).get("fanout", 1),
        }
        for name, r in role_items
    }
    return {
        "board": {
            "stackup": _stackup(batch),
            "meta": dict(batch.get("meta") or {}),
        },
        "instances": instances,
        "nets": nets,
        "net_classes": dict(batch.get("net_classes") or {}),
        "roles": roles,
        "unconnected": unconnected,
    }


def _expand_generators(batch: dict[str, Any]) -> dict[str, Any]:
    """Merge every ``generators[]`` recipe's expansion into the batch —
    the same ``components``/``nets``/``connections``/``footprints``/
    ``features``/``net_classes`` merge ``_pcb_apply`` runs, plus the
    expansion's authored ``copper`` as ``_fixed_copper`` (the laptop has
    no ``pcb_fixed_copper`` table, so generated copper rides this one
    side-channel the run then feeds to ``fixed_copper=``)."""
    gens = batch.get("generators") or []
    if not gens:
        return batch
    from precis.pcb import generators as pcb_generators

    merged = {
        k: (copy.deepcopy(v) if k != "generators" else v) for k, v in batch.items()
    }
    fixed: list[dict[str, Any]] = list(merged.pop("_fixed_copper", []) or [])
    for g in gens:
        exp = pcb_generators.expand(
            str(g["generator"]), str(g["name"]), dict(g.get("params") or {})
        )
        for key in (
            "footprints",
            "components",
            "nets",
            "connections",
            "features",
            "measures",
        ):
            if getattr(exp, key):
                merged.setdefault(key, [])
                merged[key] = merged[key] + copy.deepcopy(getattr(exp, key))
        for name, rules in (exp.net_classes or {}).items():
            merged.setdefault("net_classes", {})[name] = copy.deepcopy(rules)
        fixed += copy.deepcopy(exp.copper or [])
    if fixed:
        merged["_fixed_copper"] = fixed
    return merged


def _planes_rows(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``plane_net`` features as ``pcb_planes`` rows (``source='authored'``
    — a script declaration is the human's instruction, exactly what
    ``op='plane_net'`` writes)."""
    return [
        {
            "layer": str(f_["layer"]),
            "net": str(f_["net"]),
            "region_hint": f_.get("region_hint"),
            "source": "authored",
            "region": f_.get("region"),
            "priority": int(f_.get("priority") or 0),
        }
        for f_ in features
        if str(f_.get("ftype") or "") == "plane_net"
    ]


def _catalog_footprints(
    batch: dict[str, Any], catalog: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """LCSC-keyed footprint rows — the ``catalog=`` argument plus any
    ``meta.catalog_footprints`` entries that carry a full row (the
    embedded-cache export shape); a string value there is the fixture's
    provenance note, not geometry."""
    out: dict[str, dict[str, Any]] = {}
    meta = batch.get("meta") or {}
    for lcsc, row in (meta.get("catalog_footprints") or {}).items():
        if isinstance(row, dict) and row.get("pads"):
            out[str(lcsc)] = row
    for lcsc, row in (catalog or {}).items():
        out[str(lcsc)] = row
    return out


def _local_footprints(batch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``footprints[]`` as ``pcb_local_footprints_for``'s name-keyed map —
    each entry through :func:`precis.pcb.rows.normalize_footprint`, the
    store's insert-time normalisation itself (pad defaults, identity
    ``pin_map``, courtyard/centroid fallbacks), not a mirror."""
    out: dict[str, dict[str, Any]] = {}
    for f_ in batch.get("footprints") or []:
        name, data = pcb_rows.normalize_footprint(f_)
        out[name] = data
    return out


def _fixed_copper_rows(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """Authored fixed copper — the generator-expansion side-channel plus
    an explicit ``fixed_copper[]`` batch key (the export-embedded
    shape). Generator rows carry geometry under ``geom``; the engines'
    fixed-copper readers want it flat."""
    rows = list(batch.get("_fixed_copper") or []) + list(
        batch.get("fixed_copper") or []
    )
    out = []
    for i, row in enumerate(rows):
        geom = dict(row.get("geom") or {})
        flat = {k: v for k, v in row.items() if k != "geom"}
        flat.update(geom)
        flat["fixed_id"] = flat.get("fixed_id", i + 1)
        flat["fixed"] = True
        out.append(flat)
    return out


@dataclass
class _Design:
    """Everything the engines need, derived once per run — the offline
    analogue of :class:`precis.pcb.session_store.DesignSnapshot`."""

    batch: dict[str, Any]
    graph: dict[str, Any]
    features: list[dict[str, Any]]
    outline: list[list[float]] | None
    mounting_holes: Any
    planes: list[dict[str, Any]]
    measures: list[dict[str, Any]]
    fixed_copper: list[dict[str, Any]]
    by_lcsc: dict[str, dict[str, Any]]
    local_by_name: dict[str, dict[str, Any]]
    fab_caps: Any
    profile: str | None
    notes: list[str] = field(default_factory=list)

    @property
    def stackup(self) -> list[dict[str, Any]]:
        return self.graph["board"]["stackup"]

    @property
    def layer_names(self) -> list[str]:
        return [str(layer.get("name")) for layer in self.stackup]


def _batch_of(board: Any) -> dict[str, Any]:
    batch = board.to_batch() if hasattr(board, "to_batch") else board
    if not isinstance(batch, dict):
        raise LocalError("place/route wants a precis_geom Board or a batch dict")
    return batch


def _design(board: Any, catalog: dict[str, dict[str, Any]] | None) -> _Design:
    batch = _batch_of(board)
    by_lcsc = _catalog_footprints(batch, catalog)
    graph = batch_to_graph(batch, footprints=by_lcsc)
    features = copy.deepcopy(batch.get("features") or [])
    outline = pcb_session.outline_from_features(features)
    defect = pcb_session.outline_defect(outline)
    if defect is not None:
        raise LocalError(
            f"refusing to run — {defect}. Fix: {pcb_session.OUTLINE_FIX_HINT}"
        )
    meta = (graph.get("board") or {}).get("meta") or {}
    profile = pcb_profiles.board_profile(meta)
    try:
        fab_caps = pcb_session.board_capability(graph["board"]["stackup"], profile)
    except ValueError as exc:
        raise LocalError(f"{exc}") from exc
    notes: list[str] = []
    if not graph.get("instances"):
        raise LocalError("the board has no components — nothing to place or route")
    for inst in graph["instances"]:
        for key in ("supplies", "pin_swap_groups"):
            if inst.get(key):
                raise LocalError(
                    f"{inst['refdes']} declares {key} — supply/client "
                    "matching and pin-swap warm starts are server-side "
                    "only (the rebind lives on the resolved snapshot the "
                    "laptop does not have); run the board through "
                    "put(op='route') instead"
                )
    for pin in graph.get("unconnected") or []:
        if (pin.get("meta") or {}).get("client_of"):
            raise LocalError(
                f"{pin['refdes']}.{pin['pin']} declares client_of — "
                "supply/client matching is server-side only; see above"
            )
    return _Design(
        batch=batch,
        graph=graph,
        features=features,
        outline=outline,
        mounting_holes=pcb_session.mounting_holes_from_features(features),
        planes=_planes_rows(features),
        measures=copy.deepcopy(batch.get("measures") or []),
        fixed_copper=_fixed_copper_rows(batch),
        by_lcsc=by_lcsc,
        local_by_name=_local_footprints(batch),
        fab_caps=fab_caps,
        profile=profile,
        notes=notes,
    )


def _build_ir(design: _Design) -> PcbIR:
    return pcb_session.build_ir(
        design.graph,
        outline=design.outline,
        mounting_holes=design.mounting_holes,
        footprints_by_lcsc=design.by_lcsc,
        local_footprints_by_name=design.local_by_name,
        fixed_copper=design.fixed_copper,
    )


def _footprints(design: _Design, ir: PcbIR) -> dict[str, dict[str, Any]]:
    return pcb_session.footprints_by_refdes(
        ir,
        design.by_lcsc,
        local_footprints_by_name=design.local_by_name,
        local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(design.graph),
    )


def _net_voltages(graph: dict[str, Any]) -> dict[str, float]:
    """Annotated nets only — a missing ``working_voltage_v`` is "not
    annotated", never 0 V (the job's ``_graph_net_voltages`` rule)."""
    return {
        str(n["name"]): float(n["working_voltage_v"])
        for n in graph.get("nets") or []
        if n.get("working_voltage_v") is not None
    }


def _prepare(design: _Design) -> tuple[PcbIR, dict[str, dict[str, Any]], Any]:
    """IR + refdes-keyed footprints + resolved measures — the shared head
    of both engines (the jobs' ``build_ir`` → planes → measures →
    clearance-floor block)."""
    ir = _build_ir(design)
    applied = pcb_session.apply_plane_assignments(ir, design.planes)
    design.notes.extend(applied.notes)
    floors = pcb_session.net_clearance_floors(design.graph, design.fab_caps)
    if floors:
        ir.net_clearance_floor_mm = floors
    measures = resolve_measures(design.measures, features=design.features)
    footprints = _footprints(design, ir)
    collisions = pcb_session.fixed_copper_findings(
        ir, footprints, design.fixed_copper, design.fab_caps
    )
    if collisions:
        shown = "\n".join(f"  {f.where}: {f.detail}" for f in collisions[:10])
        raise LocalError(
            f"refusing to route — {len(collisions)} fixed-copper clearance "
            "error(s) against another net's copper, which no routing can "
            f"remove:\n{shown}"
        )
    return ir, footprints, (applied, measures)


def _placement_errors(
    design: _Design, ir: PcbIR, footprints: dict[str, dict[str, Any]]
) -> tuple[int, str | None]:
    """The job's pre-route placement-DRC gate, fail-open the same way."""
    try:
        findings = pcb_session.placement_drc_findings(
            ir,
            capability=design.fab_caps,
            footprints=footprints,
            fixed_copper=design.fixed_copper,
            outline=design.outline,
        )
        return pcb_session.placement_drc_report(
            findings,
            fixed_refdes=[
                str(ir.instance_refdes[i])
                for i in range(ir.n_instances)
                if bool(ir.inst_fixed_xy[i])
            ],
        )
    except Exception:
        return 0, None


def _realize_config(design: _Design) -> pcb_realize.RealizeConfig:
    rules = pcb_profiles.profile_rules(design.profile)
    config = pcb_realize.RealizeConfig(
        fab_caps=design.fab_caps,
        class_rules=design.graph.get("net_classes"),
        thermal=pcb_planes.thermal_spec(rules),
    )
    if rules.via_body_cost_mm is not None:
        config = dataclasses.replace(config, via_body_cost_mm=rules.via_body_cost_mm)
    return config


def _derive_regions(design: _Design, ir: PcbIR, locked: frozenset[str]) -> None:
    """Re-derive the power regions for the settled placement — the job's
    ``_apply_derived_regions``: unlocked poured nets demote first, so a
    net whose region no longer fits routes as traces."""
    stale = {
        net
        for (net, _layer), (region, _prio) in ir.plane_regions.items()
        if region is not None and net not in locked
    }
    for n in sorted(stale):
        ir.demote_plane(n)
    regions, notes = pcb_realize.derive_power_regions(ir, _realize_config(design))
    design.notes.extend(notes)
    for n, layer, region in regions:
        ir.promote_plane(n, layer, region=region.region, priority=region.priority)


def _crossing_fail(ir: PcbIR, plane_net_ids: set[int]) -> dict[str, list[str]]:
    """Residual same-layer chord crossings by net name — the job's
    ``_residual_crossings`` sweep, per-net ``with`` preserved."""
    failing: dict[str, list[str]] = {}
    for layer in pcb_session.signal_layers(ir):
        segs = [
            s
            for s in range(ir.n_segments)
            if int(ir.seg_layer[s]) == layer and int(ir.seg_net[s]) not in plane_net_ids
        ]
        pts: dict[int, tuple[tuple[float, float], tuple[float, float]]] = {}
        for s in segs:
            p = segment_points(ir, s)
            if p is not None:
                pts[s] = p
        segs = [s for s in segs if s in pts]
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                sa, sb = segs[i], segs[j]
                na, nb = int(ir.seg_net[sa]), int(ir.seg_net[sb])
                if na == nb:
                    continue
                (a1, a2), (b1, b2) = pts[sa], pts[sb]
                if segments_cross(a1, a2, b1, b2):
                    failing.setdefault(str(ir.net_name[na]), []).append(
                        str(ir.net_name[nb])
                    )
                    failing.setdefault(str(ir.net_name[nb]), []).append(
                        str(ir.net_name[na])
                    )
    return failing


# ── place ──────────────────────────────────────────────────────────────


@dataclass
class LocalPlacement:
    """What ``pcb.local.place`` returns — the laptop analogue of the
    ``place: hit (run N)`` readback: poses, the crossing count, and the
    placed design context ``route()`` continues from. ``ready`` is always
    True (there is no queued-job arm offline)."""

    slug: str
    poses: dict[str, tuple[float, float, float]]
    crossings: int
    iters: int
    cost_before: float
    cost_after: float
    notes: list[str]
    design: _Design = field(repr=False)
    ir: PcbIR = field(repr=False)
    footprints: dict[str, dict[str, Any]] = field(repr=False)
    locked_plane_nets: frozenset[int] = field(repr=False, default=frozenset())
    measures: Any = field(repr=False, default=None)
    ready: bool = True

    def summary(self) -> str:
        return (
            f"placed {len(self.poses)} part(s): {self.crossings} "
            f"crossing(s), cost {self.cost_before:.1f} → {self.cost_after:.1f}"
        )


def place(
    board: Any,
    *,
    seed: int = 1,
    iters: int | None = None,
    groups: bool = False,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> LocalPlacement:
    """The ``pcb_place`` job's anneal, in-process: placement-only moves
    (:data:`precis.pcb.optimize.PLACE_ONLY_SCHEDULE`) on the board the
    batch describes. ``catalog=`` supplies LCSC-keyed footprint rows the
    laptop has no cache for."""
    design = _design(board, catalog)
    ir, footprints, (applied, measures) = _prepare(design)
    config = OptimizeConfig(
        iters=iters or PLACE_ITERS,
        seed=seed,
        schedule=PLACE_ONLY_SCHEDULE,
        measures=measures,
        support_groups=groups,
    )
    result = optimize(ir, config)
    poses = pcb_session.positions(ir)
    from precis.pcb.ir import same_layer_crossing_count

    crossings = sum(
        same_layer_crossing_count(ir, layer) for layer in pcb_session.signal_layers(ir)
    )
    return LocalPlacement(
        slug=str(design.batch.get("meta", {}).get("slug") or ""),
        poses=poses,
        crossings=int(crossings),
        iters=result.iters,
        cost_before=result.cost_before,
        cost_after=result.cost_after,
        notes=design.notes,
        design=design,
        ir=ir,
        footprints=footprints,
        locked_plane_nets=applied.locked,
        measures=measures,
    )


# ── route ──────────────────────────────────────────────────────────────


@dataclass
class LocalRouting:
    """What ``pcb.local.route`` returns — per-net statuses in the job's
    vocabulary (``realized``/``failed``/``sketched``/dangling), the
    post-strip realized result, and the DRC findings the gate measured."""

    slug: str
    poses: dict[str, tuple[float, float, float]]
    rows: dict[str, dict[str, Any]]
    routed: int
    failed: int
    dangling: int
    unrouted_segments: int
    vias: int
    tracks: int
    findings: list[Any]
    unrouted_reasons: dict[str, str]
    notes: list[str]
    design: _Design = field(repr=False)
    ir: PcbIR = field(repr=False)
    rres: Any = field(repr=False)
    footprints: dict[str, dict[str, Any]] = field(repr=False)
    committed_round: int = 0
    ready: bool = True

    @property
    def nets(self) -> int:
        return len(self.rows)

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    def failed_nets(self) -> list[str]:
        return sorted(n for n, r in self.rows.items() if r["status"] == "failed")

    def summary(self) -> str:
        failed = self.failed_nets()
        tail = f" — failed: {', '.join(failed[:8])}" if failed else ""
        return (
            f"{self.routed}/{self.nets} routed, {self.errors} DRC error(s), "
            f"{self.vias} via(s){tail}"
        )


def _status_rows(
    design: _Design,
    ir: PcbIR,
    rres: Any,
    drc_fail: dict[str, list[dict[str, Any]]],
    split: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The job's per-net status ladder, minus the store write — same
    precedence: problems → ``failed``, copper or plane → ``realized``,
    sketch only → ``sketched``, <2 members → dangling."""
    reason_by_seg = {r.seg_id: r for r in rres.unrouted_reasons}
    unrouted_fail: dict[str, list[dict[str, Any]]] = {}
    unrouted_reasons: dict[str, str] = {}
    for seg_id in rres.unrouted:
        net_name = str(ir.net_name[int(ir.seg_net[seg_id])])
        reason = reason_by_seg.get(seg_id)
        key = pcb_session.segment_key(ir, seg_id)
        unrouted_reasons[key] = reason.kind if reason else "unknown"
        unrouted_fail.setdefault(net_name, []).append(
            {
                "kind": "unrouted",
                "reason": reason.kind if reason else "unknown",
                "message": (
                    reason.message if reason else f"no route found for segment {key}"
                ),
            }
        )
    congestion_fail: dict[str, list[dict[str, Any]]] = {}
    for w in rres.warnings:
        for net_name in w.nets:
            congestion_fail.setdefault(net_name, []).append(
                {"kind": "gap-capacity", "reason": "congestion", "message": w.message()}
            )
    unstitched_fail: dict[str, list[dict[str, Any]]] = {}
    for u in rres.unstitched:
        if u.fragments - u.bare_fragments > 1:
            unstitched_fail.setdefault(u.net, []).append(
                {
                    "kind": "unstitched-plane",
                    "reason": "unstitched-plane",
                    "message": u.message,
                }
            )
    plane_net_ids = {n for n in range(ir.n_nets) if int(ir.net_plane_layers[n]) != 0}
    crossing_fail = {
        net: [
            {
                "kind": "same-layer-crossing",
                "reason": "same-layer-crossing",
                "message": f"crosses {', '.join(sorted(set(others)))}",
            }
        ]
        for net, others in _crossing_fail(ir, plane_net_ids).items()
    }
    fixed_realized = {int(ir.seg_net[s]) for s in rres.fixed_realized}
    routed_ids = {int(t.net_id) for t in rres.tracks} | fixed_realized
    member_counts = net_member_counts(ir)
    sketch = pcb_session.extract_sketch(ir)
    rows: dict[str, dict[str, Any]] = {}
    for net_id in range(ir.n_nets):
        net_name = str(ir.net_name[net_id])
        entry = sketch.get(net_name)
        if entry is None:
            if member_counts.get(net_id, 0) < 2:
                rows[net_name] = {"status": "realized", "note": DANGLING_NET_NOTE}
            continue
        problems = (
            ([] if net_id in routed_ids else crossing_fail.get(net_name, []))
            + ([] if net_id in routed_ids else congestion_fail.get(net_name, []))
            + unrouted_fail.get(net_name, [])
            + unstitched_fail.get(net_name, [])
            + drc_fail.get(net_name, [])
        )
        island = split.get(net_name)
        if (
            not problems
            and island is not None
            and (net_id in routed_ids or int(ir.net_plane_layers[net_id]) != 0)
        ):
            problems = [
                {
                    "kind": "disconnected",
                    "reason": "disconnected",
                    "message": f"copper is in {island.components} disconnected pieces",
                }
            ]
        if problems:
            kinds = sorted({str(p["reason"]) for p in problems if p.get("reason")})
            details = [str(p["message"])[:220] for p in problems if p.get("message")]
            note = f"failed: {', '.join(kinds)}"
            if details:
                note += f" — {'; '.join(details[:3])}"
                if len(details) > 3:
                    note += f" (+{len(details) - 3} more)"
            rows[net_name] = {
                **entry,
                "status": "failed",
                "fail": {"problems": problems},
                "note": note,
            }
        elif net_id in routed_ids or int(ir.net_plane_layers[net_id]) != 0:
            rows[net_name] = {**entry, "status": "realized", "note": None}
        else:
            rows[net_name] = {**entry, "status": "sketched", "note": None}
    return rows, unrouted_reasons


def _route_on(
    design: _Design,
    ir: PcbIR,
    footprints: dict[str, dict[str, Any]],
    locked: frozenset[str],
    *,
    force: bool,
) -> tuple[Any, list[Any], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """realize + the post-route legality gates — the shared tail of the
    single-anneal and loop paths. Returns ``(stripped_rres, findings,
    drc_fail, split)``."""
    _derive_regions(design, ir, locked)
    n_errors, report = _placement_errors(design, ir, footprints)
    if n_errors and not force:
        raise LocalError(
            f"refusing to route — {n_errors} pre-route placement DRC "
            f"error(s):\n{report}"
        )
    rres = pcb_realize.realize(
        ir,
        config=_realize_config(design),
        footprints=footprints,
        fixed_copper=design.fixed_copper,
    )
    findings = pcb_session.routed_drc_findings(
        ir,
        rres,
        capability=design.fab_caps,
        footprints=footprints,
        fixed_copper=design.fixed_copper,
        outline=design.outline,
        net_rules=pcb_session.clearance_net_rules(design.graph, design.fab_caps),
        net_voltages=_net_voltages(design.graph),
    )
    rres, drc_fail = pcb_session.strip_drc_violating_nets(ir, rres, findings)
    split = pcb_session.split_nets(
        ir, rres, footprints=footprints, fixed_copper=design.fixed_copper
    )
    return rres, findings, drc_fail, split


def route(
    board: Any,
    *,
    placement: LocalPlacement | None = None,
    seed: int = 1,
    iters: int | None = None,
    groups: bool = False,
    router: str = "vector",
    force: bool = False,
    place: bool | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> LocalRouting:
    """The ``pcb_route`` job's pipeline, in-process: joint place+sketch
    anneal → ``realize`` → post-route strip and the per-net status
    ladder. ``placement=`` continues a ``place()`` result's IR and
    defaults ``place=False`` — the job's route-only mode (``hold_poses``,
    ``iters=0``): the settled sketch and poses reach the realizer
    untouched. A bare board defaults ``place=True``, the joint anneal."""
    if router != "vector":
        raise LocalError(
            f"router={router!r} — the grid maze router was removed; "
            "only 'vector' exists"
        )
    route_only = (placement is not None) if place is None else (not place)
    if placement is not None:
        design = placement.design
        ir = placement.ir
        footprints = placement.footprints
        applied_locked = placement.locked_plane_nets
        measures = placement.measures
    else:
        design = _design(board, catalog)
        ir, footprints, (applied, measures) = _prepare(design)
        applied_locked = applied.locked
    locked_names = frozenset(str(ir.net_name[n]) for n in applied_locked)
    config = OptimizeConfig(
        iters=0 if route_only else (iters or 3000),
        hold_poses=route_only,
        seed=seed,
        locked_plane_nets=applied_locked,
        measures=measures,
        support_groups=groups,
    )
    optimize(ir, config)
    collisions = pcb_session.fixed_copper_findings(
        ir, footprints, design.fixed_copper, design.fab_caps
    )
    if collisions:
        raise LocalError(
            f"the anneal placed pads on fixed copper — {len(collisions)} collision(s)"
        )
    rres, findings, drc_fail, split = _route_on(
        design, ir, footprints, locked_names, force=force
    )
    rows, unrouted_reasons = _status_rows(design, ir, rres, drc_fail, split)
    counts = {"realized": 0, "failed": 0, "dangling": 0}
    for row in rows.values():
        if row["status"] == "realized":
            counts["realized"] += 1 if row.get("note") != DANGLING_NET_NOTE else 0
            counts["dangling"] += 1 if row.get("note") == DANGLING_NET_NOTE else 0
        elif row["status"] == "failed":
            counts["failed"] += 1
    slug = str(design.batch.get("meta", {}).get("slug") or "")
    return LocalRouting(
        slug=slug,
        poses=pcb_session.positions(ir),
        rows=rows,
        routed=counts["realized"],
        failed=counts["failed"],
        dangling=counts["dangling"],
        unrouted_segments=len(rres.unrouted),
        vias=len(rres.vias),
        tracks=len(rres.tracks),
        findings=findings,
        unrouted_reasons=unrouted_reasons,
        notes=design.notes,
        design=design,
        ir=ir,
        rres=rres,
        footprints=footprints,
    )


# ── topo_place (topo-place spec §1c) ───────────────────────────────────


@dataclass
class TopoRun:
    """What ``pcb.local.topo_place`` returns — the phase-0 topo-place
    preview: the kernel result, the pin assignment, the pairwise part
    map and the run files written under ``out`` (the laptop analogue of
    a ``placer='topo'`` job's ``meta.topo_run.files``).
    ``summary()`` renders the Decision-19 one-screen text."""

    slug: str
    out: str
    poses: dict[str, tuple[float, float, float, float]]
    crossings: int
    files: dict[str, Any]
    notes: list[str]
    design: Any = field(repr=False)
    result: Any = field(repr=False)
    assignment: Any = field(repr=False)
    pair_map: Any = field(repr=False)
    design_ctx: _Design | None = field(repr=False, default=None)

    def summary(self) -> str:
        return str(self.files["summary.txt"])


def topo_place(
    board: Any,
    *,
    preset: str = "compact",
    objective: Any = None,
    effort: int = 20000,
    seed: int = 1,
    out: str = "out/topo",
    catalog: dict[str, dict[str, Any]] | None = None,
) -> TopoRun:
    """The phase-0 topo-place run, in-process (topo-place spec §1c):
    ``design_from_ir`` → the proxy placer → ``assign_pins`` → the run
    files under ``out`` (``manifest.json``/``elites.json``/``pairs.json``/
    ``pins.json``/``summary.txt``/``placement.svg``, byte-deterministic
    per Decision 22). ``preset`` names a share vector
    (:data:`~precis.pcb.topoplace.design.PRESETS`); ``objective=``
    overrides it with an ``ObjectiveShares`` or a ``{crossings,
    ratsnest}`` mapping. ``catalog=`` supplies LCSC-keyed footprint
    rows, exactly as :func:`place`. A :class:`PinConflict` surfaces
    unchanged — it is the board script's error, not the run's."""
    from precis.pcb import topoplace as _tp
    from precis.pcb.topoplace import runstore as _runstore
    from precis.pcb.topoplace import summary as _tpsummary

    if objective is None:
        if preset not in _tp.PRESETS:
            raise LocalError(
                f"unknown topo-place preset {preset!r} — known: {sorted(_tp.PRESETS)}"
            )
        obj = _tp.PRESETS[preset]
    elif isinstance(objective, _tp.ObjectiveShares):
        obj = objective
    else:
        obj = _tp.ObjectiveShares(
            crossings=float(objective.get("crossings", 0.8)),
            ratsnest=float(objective.get("ratsnest", 0.2)),
        )

    design = _design(board, catalog)
    ir = _build_ir(design)
    footprints = _footprints(design, ir)
    td = _tp.design_from_ir(ir, footprints)
    _tp.check_slots(td)
    result = _tp.topo_place(td, objective=obj, effort=effort, seed=seed)
    assignment = _tp.assign_pins(td, result.best_pose)
    pm = _tp.pair_map(td)

    import hashlib
    import json

    slug = str(design.batch.get("meta", {}).get("slug") or "")
    board_hash = hashlib.sha256(
        json.dumps(design.graph, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    summary_text = _tpsummary.render_summary(
        td,
        result,
        assignment,
        pm,
        objective=obj,
        effort=effort,
        seed=seed,
        slug=slug,
    )
    files = _runstore.run_files(
        td,
        result,
        assignment,
        pm,
        summary=summary_text,
        manifest_payload=_runstore.manifest(
            td,
            result,
            objective=obj,
            effort=effort,
            board_hash=board_hash,
            parts=_runstore.manifest_parts(td, design.graph),
            footprints=_runstore.footprint_shas(footprints),
        ),
    )
    _runstore.write_run(files, out)
    poses = {
        str(refdes): (
            float(result.best_pose[i, 0]),
            float(result.best_pose[i, 1]),
            float(result.best_pose[i, 2]) * 90.0,
            float(result.best_pose[i, 3]),
        )
        for i, refdes in enumerate(td.parts)
    }
    return TopoRun(
        slug=slug,
        out=str(out),
        poses=poses,
        crossings=int(result.crossings),
        files=files,
        notes=design.notes,
        design=td,
        result=result,
        assignment=assignment,
        pair_map=pm,
        design_ctx=design,
    )


# ── model, DRC, exports ────────────────────────────────────────────────


def _model(routing: LocalRouting) -> dict[str, Any]:
    """The gerber/svg-shaped board model — ``to_gerber_model`` plus the
    extras ``export_fab``/``run_geometric_drc`` read (drills, silk,
    mask opens, instances, net names, plane fills)."""
    design = routing.design
    ir = routing.ir
    layers = design.layer_names
    model: dict[str, Any] = pcb_realize.to_gerber_model(
        routing.rres,
        ir,
        layers=layers,
        outline=design.outline or [],
        footprints=routing.footprints,
    )
    pads = model.get("pads") or []
    drills = [
        {"x": p["x"], "y": p["y"], "dia_mm": p["drill"], "plated": True}
        for p in pads
        if p.get("drill")
    ]
    drills += [
        {"x": h.x, "y": h.y, "dia_mm": h.drill_mm, "plated": h.plated}
        for h in design.mounting_holes
    ]
    copper_vias = [c for c in model.get("copper") or [] if c.get("ctype") == "via"]
    from precis.pcb import silk as pcb_silk

    instance_sides = {
        str(inst["refdes"]): str(inst.get("layer") or "top")
        for inst in design.graph.get("instances") or []
    }
    silk_res = None
    silk_draws: dict[str, list[dict[str, Any]]] = {"top": [], "bottom": []}
    try:
        silk_res = pcb_silk.build_silk(
            ir,
            pads,
            vias=copper_vias,
            instance_sides=instance_sides,
            outline=(
                [(float(p[0]), float(p[1])) for p in design.outline]
                if design.outline
                else None
            ),
            capability=design.fab_caps,
            polarized=pcb_session.polarized_refdes(design.graph),
            label_spots=pcb_session.label_spots(design.graph),
        )
        silk_draws = silk_res.draws
    except Exception:
        silk_res = None
    mask_opens = [
        {"layer": f_.get("layer"), "geom": f_.get("geom")}
        for f_ in design.features
        if str(f_.get("ftype") or "") == "mask_open"
    ]
    instances = []
    poses = routing.poses
    for inst in design.graph.get("instances") or []:
        refdes = str(inst["refdes"])
        x, y, rot = poses.get(refdes, (inst.get("x"), inst.get("y"), inst.get("rot")))
        instances.append({**inst, "x": x, "y": y, "rot": rot})
    model.update(
        {
            "drills": drills,
            "silkscreen": silk_draws,
            "soldermask_expansion_mm": pcb_silk.soldermask_expansion_mm(design.fab_caps)
            if silk_res is not None
            else None,
            "mask_open_regions": mask_opens,
            "instances": instances,
            "nets": [str(n["name"]) for n in design.graph.get("nets") or []],
            "planes": {
                str(e["name"]): str(e.get("plane_net") or e.get("fill"))
                for e in design.stackup
                if e.get("plane_net") or e.get("fill")
            },
        }
    )
    model["_silk_census"] = getattr(silk_res, "census", None)
    return model


def drc(routing: LocalRouting) -> list[Any]:
    """``run_geometric_drc`` over the routed model at the board's profile
    capability row — the geometric rule set ``view='drc'`` runs, on the
    laptop's model rather than the store's."""
    design = routing.design
    model = _model(routing)
    return pcb_drc.run_geometric_drc(
        model,
        capability=design.fab_caps,
        outline=design.outline,
        net_rules=pcb_session.clearance_net_rules(design.graph, design.fab_caps),
        net_voltages=_net_voltages(design.graph),
        unconnected=design.graph.get("unconnected"),
        phase="routed",
        profile_rules=pcb_profiles.profile_rules(design.profile),
    )


def findings_text(findings: list[Any], *, header: str = "") -> str:
    """The ``drc.txt`` rendering — one ``rule  severity  where  detail``
    line per finding, errors first."""
    lines = [header] if header else []
    for f in sorted(
        findings,
        key=lambda f_: (
            f_.severity != "error",
            str(f_.rule),
            str(getattr(f_, "where", "")),
        ),
    ):
        where = getattr(f, "where", "")
        detail = getattr(f, "detail", "")
        lines.append(f"{f.rule}  {f.severity}  {where}  {detail}".rstrip())
    if not lines or lines == ([header] if header else []):
        lines.append("no findings")
    return "\n".join(lines) + "\n"


def gerbers(
    routing: LocalRouting, *, name: str = "board", allow_synthesized: bool = True
) -> dict[str, str]:
    """The fab bundle — :func:`precis.pcb.gerber.export_fab` over the
    routed model. ``allow_synthesized`` defaults True offline (a laptop
    has no ``part_footprints`` cache, so synthesized bounds are the rule,
    not the exception); the preview files are marked by the notes file
    when synthesized pads are present."""
    from precis.pcb import gerber as pcb_gerber

    model = _model(routing)
    model.pop("_silk_census", None)
    return pcb_gerber.export_fab(model, name=name, allow_synthesized=allow_synthesized)


def gerbers_zip(routing: LocalRouting, *, name: str = "board") -> bytes:
    """``gerbers()`` zipped — the ``out/gerbers.zip`` payload."""
    from precis.pcb import gerber as pcb_gerber

    return pcb_gerber.zip_fab(gerbers(routing, name=name))


def netlist_net(design: _Design, *, name: str = "design") -> str:
    """``out/netlist.net`` — the KiCad s-expr netlist."""
    from precis.pcb import export as pcb_export

    model = {
        "instances": design.graph.get("instances") or [],
        "nets": design.graph.get("nets") or [],
    }
    return pcb_export.kicad_netlist(model, name=name)


def schematic_svg(design: _Design) -> str:
    """``out/schematic.svg`` — the net-label schematic."""
    from precis.pcb import schematic as pcb_schematic

    return pcb_schematic.render_schematic_svg(
        design.graph, title=str(design.batch.get("meta", {}).get("slug") or "board")
    )


def board_svg(routing: LocalRouting) -> str:
    """``out/board.svg`` — the routed model rendered."""
    from precis.pcb import svg as pcb_svg

    model = _model(routing)
    model.pop("_silk_census", None)
    return pcb_svg.render_board(model, title=routing.slug or "board")


def ratsnest_svg(design: _Design, ir: PcbIR | None = None) -> str:
    """``out/ratsnest.svg`` — airwires over pads + outline."""
    from precis.pcb import svg as pcb_svg
    from precis.pcb.ir import pin_point

    ir = ir or _build_ir(design)
    layers = design.layer_names
    copper = []
    for s in range(ir.n_segments):
        a = pin_point(ir, int(ir.seg_pin_a[s]))
        b = pin_point(ir, int(ir.seg_pin_b[s]))
        if a is None or b is None:
            continue
        copper.append(
            {
                "ctype": "track",
                "layer": layers[0] if layers else "F.Cu",
                "net": str(ir.net_name[int(ir.seg_net[s])]),
                "width_mm": 0.05,
                "segments": [{"shape": "line", "start": list(a), "end": list(b)}],
            }
        )
    model = {
        "layers": layers,
        "outline": design.outline or [],
        "copper": copper,
        "pads": pcb_realize.pads_for_ir(ir, layers, _footprints(design, ir)),
    }
    return pcb_svg.render_board(model, title="ratsnest")


def placement_json(placement: LocalPlacement) -> dict[str, Any]:
    """``out/placement.json`` — poses + crossings + the anneal's cost pair."""
    return {
        "seed_result": {
            "iters": placement.iters,
            "cost_before": placement.cost_before,
            "cost_after": placement.cost_after,
        },
        "crossings": placement.crossings,
        "poses": {
            r: {"x": p[0], "y": p[1], "rot": p[2]}
            for r, p in sorted(placement.poses.items())
        },
        "notes": placement.notes,
    }


def copper_json(routing: LocalRouting) -> dict[str, Any]:
    """``out/copper.json`` — the realized copper rows + per-net statuses
    (the ``pcb_copper``/``pcb_routes`` pair a server run persists, as the
    preview file §Q3 names it)."""
    design = routing.design
    return {
        "copper": pcb_realize.result_copper_rows(
            routing.rres, routing.ir, design.layer_names
        ),
        "routes": routing.rows,
        "unrouted_reasons": routing.unrouted_reasons,
        "committed_round": routing.committed_round,
    }


def bom_csv(routing: LocalRouting) -> str:
    from precis.pcb import export as pcb_export

    return pcb_export.bom_csv(_model(routing))


def cpl_csv(routing: LocalRouting) -> str:
    from precis.pcb import export as pcb_export

    return pcb_export.cpl_csv(_model(routing))


# ── the main.py entry ──────────────────────────────────────────────────


def run(
    board: Any,
    out_dir: str,
    *,
    do_place: bool = False,
    do_route: bool = False,
    seed: int = 1,
    iters: int | None = None,
    name: str | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Write every §Q3 output ``precis_geom.pcb.run`` can't (it owns
    ``manifest.json``/``trace.json``/``check.txt``): ``netlist.net``,
    ``schematic.svg`` and ``ratsnest.svg`` always; ``placement.json``
    for ``do_place``/``do_route``; the route run's
    ``copper.json``/``drc.txt``/``gerbers.zip``/``board.svg``/``bom.csv``/
    ``cpl.csv`` for ``do_route``. Returns the written filenames."""
    import json
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    batch = _batch_of(board)
    slug = name or str((batch.get("meta") or {}).get("slug") or "board")
    design = _design(batch, catalog)
    written: list[str] = []

    def w(fn: str, text: str) -> None:
        (out / fn).write_text(text, encoding="utf-8")
        written.append(fn)

    w("netlist.net", netlist_net(design, name=slug))
    w("schematic.svg", schematic_svg(design))
    w("ratsnest.svg", ratsnest_svg(design))
    if not (do_place or do_route):
        return written
    if do_route:
        placement = place(batch, seed=seed, iters=iters, catalog=catalog)
        routing = route(batch, placement=placement, seed=seed, catalog=catalog)
        placement_doc = {
            "poses": {
                r: {"x": p[0], "y": p[1], "rot": p[2]}
                for r, p in sorted(routing.poses.items())
            },
            "notes": routing.notes,
            "committed_round": routing.committed_round,
        }
        w("placement.json", json.dumps(placement_doc, indent=1) + "\n")
        w("copper.json", json.dumps(copper_json(routing), indent=1) + "\n")
        findings = list(routing.findings) + drc(routing)
        w(
            "drc.txt",
            findings_text(
                findings,
                header=f"{slug}: {routing.summary()}",
            ),
        )
        gerber_files = gerbers(routing, name=slug)
        (out / "gerbers.zip").write_bytes(pcb_gerber_zip(gerber_files))
        written.append("gerbers.zip")
        w("board.svg", board_svg(routing))
        w("bom.csv", bom_csv(routing))
        w("cpl.csv", cpl_csv(routing))
    else:
        placement = place(batch, seed=seed, iters=iters, catalog=catalog)
        w(
            "placement.json",
            json.dumps(placement_json(placement), indent=1) + "\n",
        )
    return written


def pcb_gerber_zip(files: dict[str, str]) -> bytes:
    from precis.pcb import gerber as pcb_gerber

    return pcb_gerber.zip_fab(files)


#: Re-exported conveniences — canonical in :mod:`precis.pcb.optimize`
#: so this wrapper and the ``pcb_place`` worker share the one
#: placement-only schedule and iteration default.

__all__ = [
    "PLACE_ITERS",
    "PLACE_ONLY_SCHEDULE",
    "LocalError",
    "LocalPlacement",
    "LocalRouting",
    "TopoRun",
    "batch_to_graph",
    "board_svg",
    "bom_csv",
    "copper_json",
    "cpl_csv",
    "drc",
    "findings_text",
    "gerbers",
    "gerbers_zip",
    "netlist_net",
    "place",
    "placement_json",
    "ratsnest_svg",
    "route",
    "run",
    "schematic_svg",
    "topo_place",
]
