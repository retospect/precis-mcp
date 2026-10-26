"""Offline place/DRC/gerbers — the laptop half of
``docs/backlog/pcb-workspace-code.md`` §Q3 (slice 4, the
``precis-geom[pcb-kernel]`` extra).

The script-side surface is ``precis_geom.pcb.local`` (a lazy alias for
this module): ``pcb.local.place`` / ``pcb.local.drc`` /
``pcb.local.gerbers`` drive the SAME engines the ``pcb_place`` job type
runs — :mod:`precis.pcb.optimize`, :mod:`precis.pcb.realize` (pad
geometry and the stored-copper model), :mod:`precis.pcb.session`,
:mod:`precis.pcb.drc`, :mod:`precis.pcb.gerber` — in-process, with the
batch dict the builder emits standing in for the store rows. Nothing
here reimplements an engine; what the store path gets from
``pcb_graph``/``pcb_planes``/``pcb_footprints`` this module derives from
``board.to_batch()``.

**Routing is retired** (Reto 2026-10-25): the vector router was deleted
(tag ``vroute-final``); the topological router is the only router and
its realiser is not landed yet. ``pcb.local.route`` raises
:class:`LocalError` (:data:`precis.pcb.ROUTE_RETIRED`); the DRC/render
functions take a :class:`LocalPlacement` and honestly emit a placed
board — pads and authored fixed copper, no tracks.

**Preview, not board state.** A laptop run writes ``out/*.json`` /
``gerbers.zip`` files only — judged mutation, ``last_route`` and
recorded DRC runs are all server-side. Only a cluster ``pcb.place``
result is board state (§Risks: two placers of
record — the wheel version may differ from the server's).

Footprints: a laptop has no ``part_footprints`` cache, so a part with
``lcsc=`` and no authored ``footprint=`` falls to the ``landpattern``
synthesized bound exactly as the fixtures flag it. Three ways to supply
real pads offline, lowest precedence first: a ``footprints.json`` cache
file — ``$PRECIS_FOOTPRINT_CACHE`` (a JSON file, or a directory holding
``footprints.json``), else ``footprints.json`` next to the running script
— carrying the ``get(kind='pcb', view='footprints',
args={'format':'json'})`` export document (gr520330);
``meta.catalog_footprints[lcsc]`` may carry the cache row verbatim
(``{pads, pin_map, courtyard, centroid, holes}`` — the shape a design
export embeds); and every ``place`` call takes ``catalog={lcsc: row}``
for the rows a caller already holds.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.pcb import (
    DEFAULT_STACKUP,
    ROUTE_RETIRED,
    ROUTE_RETIRED_NEXT,
    STACKUP_PRESETS,
)
from precis.pcb import drc as pcb_drc
from precis.pcb import profiles as pcb_profiles
from precis.pcb import realize as pcb_realize
from precis.pcb import rows as pcb_rows
from precis.pcb import session as pcb_session
from precis.pcb.ir import PcbIR
from precis.pcb.optimize import (
    PLACE_ITERS,
    PLACE_ONLY_SCHEDULE,
    OptimizeConfig,
    optimize,
    resolve_measures,
)

log = logging.getLogger(__name__)


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


#: The env var naming the laptop's footprint cache — a JSON file, or a
#: directory holding ``footprints.json`` (gr520330).
FOOTPRINT_CACHE_ENV = "PRECIS_FOOTPRINT_CACHE"

#: The cache filename looked up under ``$PRECIS_FOOTPRINT_CACHE``'s
#: directory form and next to the running script.
FOOTPRINT_CACHE_NAME = "footprints.json"

#: The export call a footprint-cache miss names — the read-only door that
#: produces the document ``footprints.json`` holds.
FOOTPRINT_EXPORT_CALL = (
    "get(kind='pcb', id={slug!r}, view='footprints', args={{'format':'json'}})"
)


def _footprint_cache_path() -> Path | None:
    """The laptop's footprint-cache file, when one exists:
    ``$PRECIS_FOOTPRINT_CACHE`` (a JSON file, or a directory holding
    ``footprints.json``), else ``footprints.json`` next to the running
    script (``sys.argv[0]``). An env var that resolves to no file is a
    loud warning, not a silent miss — a typo'd path reads exactly like
    the no-cache bug this fixes."""
    env = os.environ.get(FOOTPRINT_CACHE_ENV, "").strip()
    if env:
        p = Path(env).expanduser()
        cand = p / FOOTPRINT_CACHE_NAME if p.is_dir() else p
        if cand.is_file():
            return cand
        log.warning(
            "pcb.local: %s=%s names no footprint cache file — ignoring",
            FOOTPRINT_CACHE_ENV,
            env,
        )
        return None
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and argv0 not in ("-", "-c"):
        cand = Path(argv0).resolve().parent / FOOTPRINT_CACHE_NAME
        if cand.is_file():
            return cand
    return None


def _load_footprint_cache(path: Path) -> dict[str, dict[str, Any]]:
    """``footprints.json`` → the LCSC-keyed row map. Accepts the
    ``view='footprints', format=json`` export document
    (``{"footprints": {lcsc: row}, "missing": [...]}``) or a bare
    ``{lcsc: row}`` map; a row without ``pads`` is not geometry and is
    skipped. A present-but-unreadable file refuses: silently ignoring a
    stale or truncated cache reads exactly like the no-cache bug this
    fixes."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalError(f"footprint cache {path}: unreadable ({exc})") from exc
    if not isinstance(doc, dict):
        raise LocalError(f"footprint cache {path}: expected a JSON object")
    inner = doc.get("footprints")
    rows = inner if isinstance(inner, dict) else doc
    return {
        str(lcsc).strip().upper(): row
        for lcsc, row in rows.items()
        if isinstance(row, dict) and row.get("pads")
    }


def _catalog_footprints(
    batch: dict[str, Any], catalog: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """LCSC-keyed footprint rows — lowest precedence first: the
    ``footprints.json`` cache file (:func:`_footprint_cache_path`), any
    ``meta.catalog_footprints`` entries that carry a full row (the
    embedded-cache export shape; a string value there is the fixture's
    provenance note, not geometry), then the ``catalog=`` argument."""
    out: dict[str, dict[str, Any]] = {}
    cache = _footprint_cache_path()
    if cache is not None:
        out.update(_load_footprint_cache(cache))
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


def _uncached_lcscs(
    graph: dict[str, Any],
    by_lcsc: dict[str, dict[str, Any]],
    local_by_name: dict[str, dict[str, Any]],
) -> list[str]:
    """The catalog C-numbers a run will synthesize — every instance whose
    ``part_lcsc`` resolves no ``part_footprints`` row and whose
    ``footprint`` names no design-local row either (the same precedence
    :func:`precis.pcb.session.footprints_by_refdes` applies)."""
    cached = {str(k).strip().upper() for k in by_lcsc}
    out: set[str] = set()
    for inst in graph.get("instances") or []:
        lcsc = str(inst.get("part_lcsc") or "").strip().upper()
        if not lcsc or lcsc in cached:
            continue
        local = local_by_name.get(str(inst.get("footprint") or ""))
        if local is None:
            out.add(lcsc)
    return sorted(out)


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
        raise LocalError("the board has no components — nothing to place")
    for inst in graph["instances"]:
        for key in ("supplies", "pin_swap_groups"):
            if inst.get(key):
                raise LocalError(
                    f"{inst['refdes']} declares {key} — supply/client "
                    "matching and pin-swap warm starts are server-side "
                    "only (the rebind lives on the resolved snapshot the "
                    "laptop does not have)"
                )
    for pin in graph.get("unconnected") or []:
        if (pin.get("meta") or {}).get("client_of"):
            raise LocalError(
                f"{pin['refdes']}.{pin['pin']} declares client_of — "
                "supply/client matching is server-side only; see above"
            )
    local_by_name = _local_footprints(batch)
    missing = _uncached_lcscs(graph, by_lcsc, local_by_name)
    if missing:
        slug = str(meta.get("slug") or "<slug>")
        log.warning(
            "pcb.local: %d catalog part(s) have no cached footprint (%s) — "
            "their pins stay at the synthesized landpattern bound. Export "
            "the real rows: %s, save the body as %s next to the script (or "
            "set %s=<path>)",
            len(missing),
            ", ".join(missing),
            FOOTPRINT_EXPORT_CALL.format(slug=slug),
            FOOTPRINT_CACHE_NAME,
            FOOTPRINT_CACHE_ENV,
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
        local_by_name=local_by_name,
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
            f"refusing — {len(collisions)} fixed-copper clearance "
            "error(s) against another net's copper, which no routing can "
            f"remove:\n{shown}"
        )
    return ir, footprints, (applied, measures)


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


# ── route (retired) ────────────────────────────────────────────────────


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
) -> None:
    """Retired — the vector router (vroute) was deleted 2026-10-25 (tag
    ``vroute-final``); the topological router is the only router and its
    realiser is not landed yet. Always raises :class:`LocalError`;
    :func:`place` plus the ``drc``/render exports below still work on the
    placed board."""
    raise LocalError(f"{ROUTE_RETIRED} Next: {ROUTE_RETIRED_NEXT}")


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
            overlap=float(objective.get("overlap", 0.0)),
        )

    design = _design(board, catalog)
    ir = _build_ir(design)
    footprints = _footprints(design, ir)
    td = _tp.design_from_ir(
        ir,
        footprints,
        net_classes=design.graph.get("net_classes"),
        features=design.features,
        fixed_copper=design.fixed_copper,
    )
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


def _model(placement: LocalPlacement) -> dict[str, Any]:
    """The gerber/svg-shaped board model — ``to_gerber_model`` plus the
    extras ``export_fab``/``run_geometric_drc`` read (drills, silk,
    mask opens, instances, net names, plane fills). The placed board's
    ``RealizeResult`` is empty — routing is retired, so pads and authored
    fixed copper are all there is to model."""
    design = placement.design
    ir = placement.ir
    layers = design.layer_names
    model: dict[str, Any] = pcb_realize.to_gerber_model(
        pcb_realize.RealizeResult(tracks=(), vias=(), warnings=()),
        ir,
        layers=layers,
        outline=design.outline or [],
        footprints=placement.footprints,
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
    poses = placement.poses
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


def drc(placement: LocalPlacement) -> list[Any]:
    """``run_geometric_drc`` over the placed model at the board's profile
    capability row — the geometric rule set ``view='drc'`` runs, on the
    laptop's model rather than the store's."""
    design = placement.design
    model = _model(placement)
    return pcb_drc.run_geometric_drc(
        model,
        capability=design.fab_caps,
        outline=design.outline,
        net_rules=pcb_session.clearance_net_rules(design.graph, design.fab_caps),
        net_voltages=_net_voltages(design.graph),
        unconnected=design.graph.get("unconnected"),
        phase="placed",
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
    placement: LocalPlacement, *, name: str = "board", allow_synthesized: bool = True
) -> dict[str, str]:
    """The fab bundle — :func:`precis.pcb.gerber.export_fab` over the
    placed model (pads + fixed copper — no routed copper exists).
    ``allow_synthesized`` defaults True offline (a laptop
    has no ``part_footprints`` cache, so synthesized bounds are the rule,
    not the exception); the preview files are marked by the notes file
    when synthesized pads are present."""
    from precis.pcb import gerber as pcb_gerber

    model = _model(placement)
    model.pop("_silk_census", None)
    return pcb_gerber.export_fab(model, name=name, allow_synthesized=allow_synthesized)


def gerbers_zip(placement: LocalPlacement, *, name: str = "board") -> bytes:
    """``gerbers()`` zipped — the ``out/gerbers.zip`` payload."""
    from precis.pcb import gerber as pcb_gerber

    return pcb_gerber.zip_fab(gerbers(placement, name=name))


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


def board_svg(placement: LocalPlacement) -> str:
    """``out/board.svg`` — the placed model rendered."""
    from precis.pcb import svg as pcb_svg

    model = _model(placement)
    model.pop("_silk_census", None)
    return pcb_svg.render_board(model, title=placement.slug or "board")


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


def copper_json(placement: LocalPlacement) -> dict[str, Any]:
    """``out/copper.json`` — the copper rows + per-net statuses (the
    ``pcb_copper``/``pcb_routes`` pair a server run persists, as the
    preview file §Q3 names it). Empty: routing is retired, so a placed
    board carries no router copper."""
    return {
        "copper": [],
        "routes": {},
        "unrouted_reasons": {},
        "digest": {},
        "committed_round": 0,
    }


def bom_csv(placement: LocalPlacement) -> str:
    from precis.pcb import export as pcb_export

    return pcb_export.bom_csv(_model(placement))


def cpl_csv(placement: LocalPlacement) -> str:
    from precis.pcb import export as pcb_export

    return pcb_export.cpl_csv(_model(placement))


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
    for ``do_place``/``do_route``; the placed board's
    ``copper.json``/``drc.txt``/``gerbers.zip``/``board.svg``/``bom.csv``/
    ``cpl.csv`` for ``do_route`` (no router yet — routing is retired, so
    the "route" outputs honestly describe the placed board). Returns the
    written filenames."""
    import json
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    batch = _batch_of(board)
    slug = name or (batch.get("meta") or {}).get("slug") or "board"
    slug = str(slug)
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
    placement = place(batch, seed=seed, iters=iters, catalog=catalog)
    w(
        "placement.json",
        json.dumps(placement_json(placement), indent=1) + "\n",
    )
    if not do_route:
        return written
    print("no router yet — routing is retired; writing the placed board")
    w("copper.json", json.dumps(copper_json(placement), indent=1) + "\n")
    w(
        "drc.txt",
        findings_text(
            drc(placement),
            header=f"{slug}: {placement.summary()} (unrouted — no router yet)",
        ),
    )
    gerber_files = gerbers(placement, name=slug)
    (out / "gerbers.zip").write_bytes(pcb_gerber_zip(gerber_files))
    written.append("gerbers.zip")
    w("board.svg", board_svg(placement))
    w("bom.csv", bom_csv(placement))
    w("cpl.csv", cpl_csv(placement))
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
