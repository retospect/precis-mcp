"""PcbHandler — the electronics / PCB design kind.

A ``pcb`` design is a slug-addressed ref whose graph lives in the dedicated
``pcb_*`` tables (components+pins / instances / nets / netconns). The agent
**authors it in batch** and **reads it as a traversable graph** — never
pixels. The verbs map onto the seven-verb surface:

- ``put``    — create / extend a design (``id=`` slug; ``args={components,
  nets, connections, net_classes, footprints, generators}`` — see
  :meth:`PcbHandler.put`).
  ``footprints`` (pcb-ewod-multitile Slice 1) authors named, design-local
  pad geometry — real copper for a part with no LCSC catalog C-number —
  referenced by a component's own ``footprint`` field, the same join key
  a catalog part's own snapshot label already occupies. ``generators``
  (pcb-ewod-multitile Slice 2) names a COMPUTED component
  (``{name, generator, params}``, e.g. ``generator='ewod_pad_array'``) —
  expanded deterministically into components/nets/connections/footprints/
  features (:mod:`precis.pcb.generators`); re-applying the SAME params is
  a no-op, changed params retire-and-reinsert the previous expansion.
  Re-runnable. Every design gets a default board (``pcb-guided-place-route``
  Slice 1 — one 4-layer FR-4 board, ``pcb_boards``) on first write; nets are
  electrical-only in v1 (``domain`` != ``'electrical'`` is rejected).
  ``args={'op': ...}`` is the pcb-guided-place-route Slice 10 tool surface:
  ``op='place'``/``op='route'`` **enqueue** a ``pcb_place``/``pcb_route``
  worker job (never compute inline — the optimizer measures ~880 moves/s,
  minutes per board) idempotent per (design, op, content-hash); ``op='move'``/
  ``'rip'``/``'pin_side'``/``'plane_net'``/``'class_rules'`` are cheap inline
  edits. ``op='footprint'`` (gr341532 fix 3) pulls a catalog part's real pad
  geometry into the ``part_footprints`` cache (``part='C639448'`` or
  ``parts=[...]``, ``force=True`` to re-pull) through the live EasyEDA/JLC
  clients (:mod:`precis.pcb.footprint`), or authors one directly
  (``footprint={...}`` with ``part=``) when the API has nothing for the
  C-number — per-part failures report in that part's own row, never raise.
  See ``precis-pcb-route-help``.
- ``get``    — list designs (no id); a design's netlist TOC (``id=slug``,
  now with the board/stackup + net_classes + route-status summary); one
  instance's neighbourhood (``id='slug#U3'`` — its pins, the net on each, and
  the connected instances); a net's members (``id='slug@SCL'``); the *eyes*
  (``view='crossings'|'ratsnest'|'drc'|'trace'|'proximity'|'measures'|
  'feasibility'|'route-status'|'congestion'|'planes'``); ``view='footprints'``
  — which catalog-part instances have a cached footprint, the read-side
  counterpart to ``op='footprint'``; ``view='svg'`` — a
  publication-quality vector figure (``args={'level':'board'|'sketch'|'fab'}``,
  see :mod:`precis.pcb.svg`); ``view='capability'`` — a generator's
  capability map (usable/reserved pads, plaza slots, pin names, computed
  sizing; ``args={'name'?,'format':'svg'|'ledger'}``, pcb-ewod-multitile
  Slice 2); or an *export*
  (``view='bom'|'cpl'|'netlist'|'dsn'|'mechanical'|'gerber'|'epro'`` writes a
  JLCPCB fab artifact — ``'gerber'`` is the full manufacturable bundle
  (gerbers + Excellon, zipped) off our own realizer/pads, never
  Freerouting/kicad-cli; ``view='route'`` runs the Freerouting
  place↔route round-trip, the demoted escape hatch —
  :mod:`precis.pcb.export` / :mod:`precis.pcb.route`).
- ``search`` — over design names + descriptions (the one summary card).
- ``delete`` — soft-retire a whole design.

The in-house topological place+route engine (:mod:`precis.pcb.optimize` /
:mod:`precis.pcb.realize`, run via the ``pcb_place``/``pcb_route`` jobs) is
the primary path; Freerouting/gerbers-via-DSN is the rented, demoted escape
hatch. Export is the one place the design leaves the relational graph.
See ``precis-pcb-help`` and ``precis-pcb-route-help``.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import logging
import math
import re
import tempfile
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from precis.config import load_config
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.pcb import connectivity as pcb_connectivity
from precis.pcb import cost as pcb_cost
from precis.pcb import datasheets as pcb_datasheets
from precis.pcb import drc as pcb_drc
from precis.pcb import epro_write as pcb_epro_write
from precis.pcb import export as pcb_export
from precis.pcb import (
    eyes,
    format_route_summary,
    gerber_view,
    padplace,
    place,
    ratsnest,
    route_summary_status,
)
from precis.pcb import generators as pcb_generators
from precis.pcb import geom as pcb_geom
from precis.pcb import gerber as pcb_gerber
from precis.pcb import ir as pcb_ir
from precis.pcb import layer_lock as pcb_layer_lock
from precis.pcb import optimize as pcb_optimize
from precis.pcb import planes as pcb_planes
from precis.pcb import realize as pcb_realize
from precis.pcb import route as pcb_route
from precis.pcb import session as pcb_session
from precis.pcb import silk as pcb_silk
from precis.pcb import svg as pcb_svg
from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.footprint import fetch_footprint
from precis.pcb.landpattern import place_points, rotate_offset
from precis.pcb.rules import NetRules, resolve_net_rules
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.store._pcb_ops import (
    _normalize_local_footprint as normalize_local_footprint,
)
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit

log = logging.getLogger(__name__)


def _export_date() -> str:
    """Today, UTC, for the silkscreen title block.

    :func:`precis.pcb.silk.build_title_block` refuses to invent a date and
    requires the caller to supply one, which is right for a library: a
    date on a board is a claim about that artwork. The handler is the
    layer that legitimately holds one — this IS the export, so the export
    date is a fact it has rather than a guess.

    There is no stored design revision to pair it with (``pcb_boards``
    carries ``board_id``/``name``/``stackup``/``fold_lines`` and no
    version or timestamp column), so the block renders name + date and
    omits the revision rather than fabricating one.
    """
    return datetime.now(UTC).date().isoformat()


#: The "eyes" — analytic, computed-on-read (Slice 4/5).
_PROBE_VIEWS = (
    "crossings",
    "ratsnest",
    "drc",
    "trace",
    "proximity",
    "measures",
    "feasibility",
)
#: Pure file exporters off the IR (Slice 6). ``gerber`` (pcb-fab-output-
#: unwired) is the odd one out here — it doesn't read ``_export_model``
#: (the netlist IR), it assembles :mod:`precis.pcb.gerber`'s copper+pad
#: model straight off the store, see :meth:`PcbHandler._render_gerber`.
_EXPORT_VIEWS = ("bom", "cpl", "netlist", "dsn", "mechanical", "gerber", "epro")
#: How many errors ``view='gerber'``'s DRC banner quotes before pointing at
#: ``view='drc'`` for the rest — enough to see what kind of failure it is.
_GERBER_BANNER_FINDINGS = 8
#: The rented autorouter round-trip (Slice 6, gated on Freerouting).
_ROUTE_VIEWS = ("route",)
#: pcb-guided-place-route Slice 1 — per-net route status (all-unrouted v1).
_STATUS_VIEWS = ("route-status",)
#: pcb-guided-place-route Slice 10 — the enqueued optimizer/realizer's
#: read-side: congestion warnings from the latest ``op='route'`` run, and
#: authored plane assignments.
_SESSION_VIEWS = ("congestion", "planes")
#: pcb-svg-render — publication-quality vector figures off the same
#: copper model Gerber writes from (:mod:`precis.pcb.svg`), plus the L3
#: rubber-band sketch off the IR. See :meth:`PcbHandler._render_svg`.
#: ``schematic`` is the placement-free sibling: the netlist as a
#: net-label schematic (:mod:`precis.pcb.schematic`) — renders before the
#: first ``op='place'`` because it reads intent, not copper. ``capability``
#: (pcb-ewod-multitile Slice 2) is a generator's own capability map — usable
#: vs reserved/suppressed pads, plaza slot allocation, pin naming and
#: computed sizing — off the ``pcb_generators`` ledger, SVG or a
#: machine-readable table (:meth:`PcbHandler._render_capability`).
_RENDER_VIEWS = ("svg", "schematic", "capability")
#: gr341532 fix 3 — the ``part_footprints`` cache gap made visible per
#: catalog-part instance, the read-side counterpart to ``op='footprint'``.
_OTHER_VIEWS = ("links", "footprints", "pinout")
_VIEWS = (
    *_PROBE_VIEWS,
    *_EXPORT_VIEWS,
    *_ROUTE_VIEWS,
    *_STATUS_VIEWS,
    *_SESSION_VIEWS,
    *_RENDER_VIEWS,
    *_OTHER_VIEWS,
)

#: pcb-guided-place-route Slice 10 tool surface — the two heavy ops that
#: enqueue a worker job (never compute inline) and the inline edits that
#: are cheap enough to run in the request path.
_JOB_OPS = ("place", "route")
_INLINE_EDIT_OPS = (
    "move",
    "rip",
    "pin_side",
    "plane_net",
    "class_rules",
    "stackup",
)
#: gr341532 fix 3 — pull/author a catalog part's real footprint into the
#: ``part_footprints`` cache. A handful of small HTTP calls (EasyEDA), not
#: the router's per-board compute, so it runs inline like the edits above,
#: never as an enqueued worker job.
_FOOTPRINT_OPS = ("footprint",)
_OPS = (*_JOB_OPS, *_INLINE_EDIT_OPS, *_FOOTPRINT_OPS, "datasheets")

#: :meth:`PcbHandler._polarized_refdes`'s label-inference half — an
#: instance whose ``label`` matches this (case-insensitively) is treated
#: as polarized even with no explicit ``"polarized": true`` flag. Matches
#: the abbreviations a real BOM label uses: "ELEC" (electrolytic), "TANT"
#: (tantalum), "POL" (generic "polarized").
_POLARIZED_LABEL_RE = re.compile(r"ELEC|TANT|POL", re.IGNORECASE)


#: A pre-existing finding the group move makes more negative than this is a
#: new fault, not a standing one.
_GROUP_MOVE_MARGIN_EPS_MM = 1e-4
_MOVES_EXAMPLE = (
    "args={'op':'move','moves':[{'refdes':'U1','x':10,'y':5},"
    "{'refdes':'U2','x':20,'y':5,'rot':90}]}"
)


def _finding_object_identity(o: dict[str, Any], prefix: str = "") -> str:
    """Coordinate-free identity of one object of a DRC finding: a pad is
    ``refdes/pin``, authored fixed copper its ``fixed_id``. An object with
    neither is a courtyard finding's ``a``/``b`` pair (``part:<refdes>``, or
    ``hole:<label>``); an object with none of these falls back to
    ``ctype:net:layer``."""
    refdes, pin = o.get(prefix + "refdes"), o.get(prefix + "pin")
    if refdes and pin:
        return f"pad:{refdes}/{pin}"
    fid = o.get(prefix + "fixed_id")
    if fid is not None:
        return f"fixed:{fid}"
    a, b = o.get(prefix + "a"), o.get(prefix + "b")
    if a is not None and b is not None:
        # courtyard_overlap / courtyard_hole: ``a`` is a refdes, ``b`` another
        # refdes or a hole label. Unordered, so the pair keys the same from
        # either end.
        side = [f"part:{a}", f"hole:{b}" if str(b).startswith("hole") else f"part:{b}"]
        return "+".join(sorted(side))
    role = o.get(prefix + "role")
    if role is not None:
        # outline_containment of a silk draw: role + refdes + side.
        return f"silk:{role}:{o.get(prefix + 'refdes')}:{o.get(prefix + 'side')}"
    if refdes:
        # outline_containment of a whole part (courtyard): no pin.
        return f"part:{refdes}"
    return (
        f"{o.get(prefix + 'ctype')}:{o.get(prefix + 'net')}:{o.get(prefix + 'layer')}"
    )


#: A pose conflict the move deepens by more than this is a new fault (mm²;
#: the via-keepout shortfall is a length, same threshold).
_POSE_DEPTH_EPS = 1e-6


def _pose_delta(
    before: Sequence[tuple[tuple[Any, int, str], tuple[float, float, float]]],
    after: Sequence[tuple[tuple[Any, int, str], tuple[float, float, float]]],
    *,
    skip: dict[str, set[str]],
) -> tuple[list[tuple[str, str, str, str]], list[tuple[str, str, str, str]]]:
    """The pose half of a move as a DELTA, like the carried-copper half:
    conflicts keyed by identity (never coordinates), a standing one may
    stay or shrink, a new or deeper one is refused.

    ``before`` / ``after`` hold one ``((engine, instance index, refdes),
    (x, y, rot))`` per moved part: the engine built over that side's
    board and the pose to judge the part at. Returns ``(problems, standing)`` as ``(rule, refdes, other, suffix)``.

    Keys: ``courtyard_overlap`` is the unordered refdes pair (seen once
    from either end when both are moved); ``courtyard_hole``,
    ``via_pad_keepout`` and ``outline`` are per part (hole id /
    ``via`` / ``board outline``). ``skip[refdes]`` is the part's own rigid
    group, whose members never conflict with each other."""

    def collect(
        side: Sequence[tuple[tuple[Any, int, str], tuple[float, float, float]]],
    ) -> dict[tuple[Any, ...], tuple[float, str, str, str]]:
        found: dict[tuple[Any, ...], tuple[float, str, str, str]] = {}
        for (engine, inst, rd), (x, y, r) in side:
            for rule, other, depth in engine.pose_conflict_depths(inst, x, y, r):
                if other in skip.get(rd, ()):
                    continue
                if rule == "courtyard_overlap":
                    key: tuple[Any, ...] = (rule, frozenset((rd, other)))
                else:
                    key = (rule, rd, "via" if rule == "via_pad_keepout" else other)
                found.setdefault(key, (depth, rule, rd, other))
        return found

    old = collect(before)
    problems: list[tuple[str, str, str, str]] = []
    standing: list[tuple[str, str, str, str]] = []
    for key, (depth, rule, rd, other) in collect(after).items():
        prev = old.get(key)
        unit = "mm" if rule == "via_pad_keepout" else "mm\u00b2"
        if prev is None:
            problems.append((rule, rd, other, ""))
        elif depth > prev[0] + _POSE_DEPTH_EPS:
            problems.append(
                (
                    rule,
                    rd,
                    other,
                    f" (worse: {depth:.4f} {unit} vs {prev[0]:.4f} {unit} "
                    "before the move)",
                )
            )
        else:
            standing.append((rule, rd, other, ""))
    return problems, standing


def _finding_identity(f: pcb_drc.DrcFinding) -> tuple[Any, ...]:
    """``(rule, sorted object identities, layer)`` — no coordinates, so a
    finding between two members of a rigidly moved group keeps its key."""
    if f.rule == "via_pad_keepout" and f.objects:
        o = f.objects[0]
        via = (
            f"fixed:{o['via_fixed_id']}"
            if o.get("via_fixed_id") is not None
            else f"via:{o.get('via_net')}"
        )
        pad = (
            f"pad:{o.get('pad_refdes')}/{o.get('pad_pin')}"
            if o.get("pad_refdes") and o.get("pad_pin")
            else f"pad:{o.get('pad_net')}"
        )
        return (f.rule, tuple(sorted((via, pad))), o.get("pad_layer"))
    ids = tuple(sorted(_finding_object_identity(o) for o in f.objects))
    layer = f.where.rsplit(" on ", 1)[-1] if " on " in f.where else None
    return (f.rule, ids, layer)


def _margin_delta[T](
    before: Iterable[tuple[tuple[Any, ...], float]],
    after: Iterable[tuple[tuple[Any, ...], float, T]],
) -> tuple[list[tuple[T, float, float | None]], list[T]]:
    """Pair findings by identity key, worst margin first, and split ``after``
    into ``(new_or_worse, standing)``.

    ``new_or_worse`` holds ``(payload, margin, old_margin)`` — ``old_margin``
    is ``None`` for a finding with no counterpart before. A finding is worse
    when its margin fell by more than :data:`_GROUP_MOVE_MARGIN_EPS_MM`;
    otherwise it is standing. Several findings under one key (e.g. many
    clearance hits between the same two parts) pair rank for rank, the most
    negative with the most negative, so one new hit among old ones is the
    one that is reported."""
    old: dict[tuple[Any, ...], list[float]] = collections.defaultdict(list)
    for key, margin in before:
        old[key].append(margin)
    for margins in old.values():
        margins.sort()
    new: dict[tuple[Any, ...], list[tuple[float, T]]] = collections.defaultdict(list)
    for key, margin, payload in after:
        new[key].append((margin, payload))
    worse: list[tuple[T, float, float | None]] = []
    standing: list[T] = []
    for key, items in new.items():
        items.sort(key=lambda it: it[0])
        olds = old.get(key, [])
        for i, (margin, payload) in enumerate(items):
            if i >= len(olds):
                worse.append((payload, margin, None))
            elif margin < olds[i] - _GROUP_MOVE_MARGIN_EPS_MM:
                worse.append((payload, margin, olds[i]))
            else:
                standing.append(payload)
    return worse, standing


_FOOTPRINT_SCHEMA = [
    "lcsc",
    "cached",
    "source",
    "n_pads",
    "n_pins",
    "courtyard",
    "error",
]


@dataclasses.dataclass
class JudgeReport:
    """What a judged mutation left behind: ``ripped`` maps each router net it
    had to rip to the rule that did it; ``standing`` counts validity findings
    the board already had and the change did not worsen."""

    ripped: dict[str, str] = dataclasses.field(default_factory=dict)
    standing: int = 0
    #: Problems reported instead of refused (``refuse=False``): what the
    #: change made visible and left standing.
    visible: list[str] = dataclasses.field(default_factory=list)
    #: New warn-tier findings on pads/authored copper only: a shortfall
    #: against a class or house requirement, listed but never refused.
    margins: list[str] = dataclasses.field(default_factory=list)
    #: Set when the judge itself crashed and the change was stored unjudged
    #: (op='footprint' only: the real footprint is always stored).
    unjudged: str = ""

    def lines(self) -> str:
        """Response lines for the report, each ending in a newline."""
        out = [
            f"{net} ripped: {rule} after this change — re-route\n"
            for net, rule in sorted(self.ripped.items())
        ]
        out.extend(
            f"now visible (class requirement, not refused): {m}\n"
            for m in self.margins[:8]
        )
        if len(self.margins) > 8:
            out.append(f"+{len(self.margins) - 8} more\n")
        if self.visible:
            shown = "; ".join(self.visible[:8])
            more = f" (+{len(self.visible) - 8} more)" if len(self.visible) > 8 else ""
            out.append(f"now visible, standing until re-placed: {shown}{more}\n")
        if self.standing:
            out.append(
                f"{self.standing} standing finding(s) not caused by this change\n"
            )
        return "".join(out)


class PcbHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="pcb",
        title="PCB",
        description=(
            "Electronics/PCB design — a netlist + placement graph "
            "the LLM authors in batch and reads as a traversable graph, never "
            "pixels. put creates/extends a design (id=slug, args={components:"
            "[{refdes,label,part?,footprint?,pins:[{name,pad?,tags?}],x?,y?,"
            "layer?,roles?}], nets:[{name,class?,current?,domain?}], "
            "connections:[{net,refdes,pin}], net_classes:{name:rules}, "
            "footprints:[{name,pads:[{pin,shape:'circle'|'rect'|'obround'|"
            "'polygon',x?,y?,w?,h?,poly?,role?,mask?,paste?}]}]}); a component "
            "with no part/part_lcsc names a footprints[] entry via its own "
            "'footprint' field (authored copper for a part with no LCSC "
            "C-number — pcb-ewod-multitile Slice 1); generators:[{name,"
            "generator:'ewod_pad_array',params:{grid?:[rows,cols],pads?,"
            "variant?:'full'|'rim',pitch?,gap?,via?:{dia,drill},"
            "hv_separation?,drive_voltage_v?,edge?:{tooth_depth,tooth_pitch},"
            "external_edge?,reserve?,x?,y?}}] authors a COMPUTED component — "
            "one component whose pins are the electrode nets, expanded "
            "deterministically (pcb-ewod-multitile Slice 2; idempotent — "
            "same params is a no-op, changed params retire+reinsert); nets "
            "default domain='electrical' "
            "(v1 rejects fluidic/thermal — schema-reserved). Every design gets "
            "a default 4-layer board (pcb_boards) on first write. "
            "get lists designs, a design's netlist TOC (id=slug, incl. board/"
            "stackup + net_classes + route-status summary), one "
            "instance's neighbourhood (id='slug#U3'), a net's members "
            "(id='slug@SCL'), the eyes (view='crossings'|'ratsnest'|'drc'|"
            "'trace'|'proximity'|'measures'|'feasibility'|'route-status'|"
            "'congestion'|'planes'), which catalog parts have a cached "
            "footprint (view='footprints', one row per catalog-part "
            "instance), one instance's actual physical pinout "
            "(id='slug#J1', view='pinout', read-only), a vector figure (view='svg', "
            "args={'level':'board'|'sketch'|'fab','layers':[...],'include':[...]}), "
            "a net-label schematic SVG off the netlist alone "
            "(view='schematic', works before any placement), "
            "a generator's capability map -- usable/reserved pads, plaza "
            "slot allocation, pin naming, computed sizing "
            "(view='capability', args={'name'?,'format':'svg'|'ledger'}, "
            "pcb-ewod-multitile Slice 2), "
            "or an export (view='bom'|'cpl'|'netlist'|"
            "'dsn'|'mechanical'|'gerber' writes a JLCPCB fab artifact -- "
            "'gerber' is the full manufacturable bundle (gerbers+Excellon, "
            "zipped); view='epro' writes an EasyEDA Pro .epro2 (outline, "
            "parts, pads, nets; no copper yet, no schematic; not yet opened "
            "in EasyEDA Pro); view='route' runs "
            "the demoted Freerouting escape hatch), all with args={...}; "
            "put(args={'op':'place'}) / args={'op':'route'} ENQUEUE a worker "
            "job (never inline; idempotent per design+op+content-hash) — "
            "args={'autoplace':{...}} is a deprecated alias for op='place'; "
            "put(args={'op':'move'|'rip'|'pin_side'|'plane_net'|'class_rules'}) "
            "are cheap inline edits; put(args={'op':'footprint','part':"
            "'C639448'}) (or parts=[...], force=True to re-pull, or "
            "footprint={...} to author one directly under part=) pulls/"
            "authors a catalog part's real pad geometry into the "
            "part_footprints cache -- fixes the synthesized_footprint DRC "
            "finding; search over names; delete soft-retires. "
            "Postgres-canonical; routing/gerbers are downstream export. "
            "See precis-pcb-help and precis-pcb-route-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_search=True,
        supports_search_hits=True,
        supports_delete=True,
        is_numeric=False,
        id_required=False,
        views=_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("pcb: store required")
        self.hub = hub
        self.store = hub.store
        self.embedder = hub.embedder

    # ── put ──────────────────────────────────────────────────────────
    def put(
        self,
        *,
        id: str | int | None = None,
        title: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Author a design in batch (components/nets/features/generators/
        net_classes...) or run an ``op``.

        The batch path is JUDGED (:meth:`_judged_mutation`): the board it
        leaves must not carry a validity finding it did not already have. A
        new one that names only pads/authored copper refuses the whole put
        (nothing is stored); one that names router copper rips that net,
        reported in the response. A put that leaves the design with no
        placed instance is never judged."""
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='pcb') requires id= (the design slug)",
                next=(
                    "put(kind='pcb', id='sensor-node', args={'components': "
                    "[{'refdes':'U1','label':'ESP32-C3','pins':[{'name':'SCL'},"
                    "{'name':'SDA'}]}], 'nets':[{'name':'I2C_SCL','class':'i2c'}],"
                    " 'connections':[{'net':'I2C_SCL','refdes':'U1','pin':'SCL'}]})"
                ),
            )
        slug = str(id).strip()
        args = args or {}
        op = args.get("op")
        if op is not None:
            # pcb-guided-place-route Slice 10 tool surface: place/route/the
            # inline edits address an EXISTING design and never blend with
            # bulk netlist authoring in the same call (unlike the retired
            # `autoplace` alias below, which historically did) — keeps the
            # enqueue/edit paths from having to reason about a simultaneous
            # components/nets diff.
            ref = resolve_live_slug_ref(self.store, kind="pcb", id=slug)
            return self._dispatch_op(ref, str(op), args)
        components = list(args.get("components") or [])
        nets = list(args.get("nets") or [])
        connections = list(args.get("connections") or [])
        measures = list(args.get("measures") or [])
        features = list(args.get("features") or [])
        footprints = list(args.get("footprints") or [])
        generators = list(args.get("generators") or [])
        autoplace = args.get("autoplace")
        meta = args.get("meta") if isinstance(args.get("meta"), dict) else None
        net_classes = args.get("net_classes")
        ttl = (title or slug).strip() or slug

        self._reject_non_electrical(nets)

        try:
            # One transaction spans pcb_apply + the net_classes upsert (both
            # accept an external conn=) so a validation error in either half
            # (e.g. a blank net_class name) rolls back the whole put — never
            # a committed design followed by a BadInput implying nothing
            # happened (gr — pcb-guided-place-route Slice 1 review).
            existing = self.store.get_ref(kind="pcb", id=slug)

            def apply(conn: Any) -> tuple[Any, bool, dict[str, int], int]:
                ref, created, counts = self.store.pcb_apply(
                    slug=slug,
                    title=ttl,
                    components=components,
                    nets=nets,
                    connections=connections,
                    measures=measures,
                    features=features,
                    footprints=footprints,
                    generators=generators,
                    meta=meta,
                    conn=conn,
                )
                n_classes = 0
                if isinstance(net_classes, dict) and net_classes:
                    n_classes = self.store.pcb_upsert_net_classes(
                        ref.id, net_classes, conn=conn
                    )
                return ref, created, counts, n_classes

            (ref, created, counts, n_classes), judged = self._judged_mutation(
                existing.id if existing is not None else None,
                apply,
                ref_id_of=lambda r: int(r[0].id),
            )
        except ValueError as exc:
            raise BadInput(f"pcb: {exc}") from exc

        # After the commit, never inside the judged transaction: queue the
        # datasheet pulls (jobs; nothing is fetched here).
        queued = self._queue_datasheets(ref.id)

        if autoplace:
            # Retired as a computed-inline behavior (pcb-guided-place-route
            # Slice 10) — enqueues the SAME `pcb_place` job `op='place'`
            # does; kept as a one-release alias so an existing caller's
            # `args={'autoplace':{...}}` still works, just asynchronously
            # now. See precis-pcb-route-help for the replacement.
            opts = autoplace if isinstance(autoplace, dict) else {}
            body = self._enqueue_op(ref, "place", opts)
            return Response(
                body=(
                    "⚠️  DEPRECATED: args={'autoplace':...} now ENQUEUES a "
                    "worker job instead of computing inline — use "
                    "args={'op':'place', ...} directly (same params, same "
                    "job); this alias will be removed. See precis-pcb-route-help.\n\n"
                    + body
                )
            )

        verb = "created" if created else "extended"
        design = self.store.pcb_load(ref.id)
        extra = f", +{counts['measures']} measure(s)" if counts["measures"] else ""
        if counts["features"]:
            extra += f", +{counts['features']} feature(s)"
        if counts.get("footprints"):
            extra += f", +{counts['footprints']} footprint(s)"
        if counts.get("generators"):
            extra += f", {counts['generators']} generator(s) applied"
        # An annotation pass re-puts nets that already exist, so +0 net(s)
        # is the NORMAL result and says nothing about whether the edit
        # landed. Report the patch separately or it reads as a no-op
        # (gr457053).
        if counts.get("nets_patched"):
            extra += f", {counts['nets_patched']} net(s) patched"
        if n_classes:
            extra += f", +{n_classes} net_class(es)"
        head = (
            f"# {slug} — {verb}: +{counts['components']} part(s), "
            f"+{counts['nets']} net(s), +{counts['conns']} conn(s){extra}  "
            f"(now {len(design['instances'])} part(s), {len(design['nets'])} net(s))"
        )
        notes = [
            n for n in (self._pin_name_note(ref.id), self._stale_note(ref.id)) if n
        ]
        return Response(
            body=head
            + "\n"
            + judged.lines()
            + "".join(n + "\n" for n in notes)
            + (f"{queued} datasheet pull(s) queued\n" if queued else "")
            + self._toc(design)
        )

    def _queue_datasheets(self, ref_id: int, *, force: bool = False) -> int:
        """Enqueue a ``datasheet_pull`` job per C-number on the board that has
        no datasheet and no recorded attempt (:mod:`precis.pcb.datasheets`);
        returns how many were newly queued."""
        return pcb_datasheets.enqueue_pulls(
            self.store, self.hub.sibling("job"), ref_id, force=force
        )

    def _stale_note(self, ref_id: int) -> str:
        """Warn when a stored generator expansion is older than the code's
        (:func:`precis.pcb.generators.stale_generators`) — a same-params
        re-put and a route both leave that copper in place."""
        return pcb_generators.stale_generator_note(
            pcb_generators.stale_generators(self.store.pcb_generators_for(ref_id))
        )

    def _pin_name_note(self, ref_id: int) -> str:
        """The put response's warning when a declared pin name matches no
        pad on its part's cached footprint (:func:`precis.pcb.session.
        pin_name_mismatches`) — said at authoring time, with both name
        sets in hand, rather than only as a DRC finding after a route."""
        graph = self.store.pcb_graph(ref_id)
        if not graph.get("instances"):
            return ""
        ir = self._build_ir(ref_id, graph)
        footprints = pcb_session.footprints_by_refdes(
            ir,
            self.store.pcb_footprints_for(ref_id),
            local_footprints_by_name=self.store.pcb_local_footprints_for(ref_id),
            local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
        )
        return pcb_session.pin_name_mismatch_note(
            pcb_session.pin_name_mismatches(ir, footprints)
        )

    def _reject_non_electrical(self, nets: list[dict[str, Any]]) -> None:
        """v1 routes electrical nets only — ``domain`` is schema-reserved
        for fluidic/thermal co-design (pcb-guided-place-route). Nets
        without a ``domain`` key default to electrical (schema default)."""
        for n in nets:
            domain = n.get("domain")
            if domain is not None and str(domain).strip() != "electrical":
                raise BadInput(
                    f"pcb: net {n.get('name')!r} has domain={domain!r} — v1 routes "
                    "electrical nets only; fluidic/thermal are schema-reserved",
                    next="omit 'domain' (defaults to electrical) or use "
                    "domain='electrical'",
                )

    # ── get ──────────────────────────────────────────────────────────
    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if view == "pinout":
            selector = str(id or "").strip()
            if selector.count("#") != 1 or "@" in selector:
                raise BadInput(
                    "pinout requires one board#REFDES selector",
                    next="get(kind='pcb', id='<board>#<REFDES>', view='pinout')",
                )
            slug, refdes = (part.strip() for part in selector.split("#", 1))
            if not slug or not refdes:
                raise BadInput(
                    "pinout requires a nonempty board and REFDES",
                    next="get(kind='pcb', id='<board>#<REFDES>', view='pinout')",
                )
            ref = resolve_live_slug_ref(self.store, kind="pcb", id=slug)
            return self._render_pinout(ref, refdes)
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        s = str(id).strip()

        # sub-paths: slug#REFDES (instance neighbourhood) / slug@NET (net members)
        if "#" in s:
            slug, refdes = s.split("#", 1)
            ref = resolve_live_slug_ref(self.store, kind="pcb", id=slug.strip())
            return self._render_instance(ref.id, refdes.strip())
        if "@" in s:
            slug, net = s.split("@", 1)
            ref = resolve_live_slug_ref(self.store, kind="pcb", id=slug.strip())
            return self._render_net(ref.id, net.strip())

        ref = resolve_live_slug_ref(self.store, kind="pcb", id=s)
        if view is not None:
            return self._render_view(ref.id, view, args or {})
        design = self.store.pcb_load(ref.id)
        head = (
            f"# {ref.slug} — {len(design['instances'])} part(s), "
            f"{len(design['nets'])} net(s)"
        )
        return Response(body=head + "\n" + self._toc(design))

    # ── auto-place ─────────────────────────────────────
    def _place_and_store(
        self,
        ref_id: int,
        *,
        iters: int,
        seed: int,
        measures: list[dict[str, Any]],
    ) -> tuple[place.PlaceResult, int]:
        """Anneal over the current graph and persist the placement + the
        ``last_place`` meta stamp. Shared by the put-time autoplace and the
        route round-trip's per-pass re-place (one copy of the store logic)."""
        graph = pcb_session.sorted_graph(self.store.pcb_graph(ref_id))
        res = place.autoplace(
            graph["instances"],
            graph["nets"],
            measures=measures,
            iters=iters,
            seed=seed,
            features=self.store.pcb_features_list(ref_id),
        )
        moved = self.store.pcb_set_placement(
            ref_id,
            res.positions,
            meta={
                "last_place": {
                    "crossings": res.crossings_after,
                    "length_mm": round(res.length_after, 2),
                    "objective": round(res.objective_after, 2),
                    "iters": res.iters,
                }
            },
        )
        return res, moved

    # ── pcb-guided-place-route Slice 10 tool surface ────────────────────
    def _dispatch_op(self, ref: Any, op: str, args: dict[str, Any]) -> Response:
        """``put(args={'op': ..., ...})`` — the enqueued heavy ops
        (``place``/``route``, never computed inline) and the cheap inline
        edits. See precis-pcb-route-help for the full surface."""
        if op in _JOB_OPS:
            return Response(body=self._enqueue_op(ref, op, args))
        if op == "move":
            return self._op_move(ref, args)
        if op == "rip":
            return self._op_rip(ref, args)
        if op == "pin_side":
            return self._op_pin_side(ref, args)
        if op == "plane_net":
            return self._op_plane_net(ref, args)
        if op == "class_rules":
            return self._op_class_rules(ref, args)
        if op == "stackup":
            return self._op_stackup(ref, args)
        if op == "footprint":
            return self._op_footprint(ref, args)
        if op == "datasheets":
            n = self._queue_datasheets(ref.id, force=bool(args.get("force")))
            return Response(
                body=f"{ref.slug}: {n} datasheet pull(s) queued"
                + ("" if args.get("force") else " (force=true re-queues failed pulls)")
            )
        raise BadInput(
            f"unknown op {op!r}",
            options=list(_OPS),
            next=(
                "put(kind='pcb', id='slug', args={'op':'place'}) or "
                "args={'op':'route'} (enqueued jobs); or one of "
                f"{_INLINE_EDIT_OPS} for an inline edit — see precis-pcb-route-help"
            ),
        )

    def _enqueue_op(self, ref: Any, op: str, opts: dict[str, Any]) -> str:
        """Enqueue a ``pcb_place``/``pcb_route`` worker job — NEVER
        computes inline (the serve thread-pool starvation lesson; the
        optimizer measures ~880 moves/s, so minutes per board, not
        milliseconds). Idempotent per (design, op, content-hash): a
        re-submit against unchanged design state + params collapses onto
        the in-flight/prior job rather than minting a duplicate."""
        graph = self.store.pcb_graph(ref.id)
        stackup = (graph.get("board") or {}).get("stackup") or []
        if len(stackup) != 4:
            raise BadInput(
                f"pcb: {ref.slug!r} has a {len(stackup)}-layer stackup — v1 "
                "place/route only supports the default 4-layer board",
                next="a non-4-layer stackup is out of v1 scope (2-layer "
                "boards route power like signals, a different problem)",
            )
        params: dict[str, Any] = {"pcb_ref_id": ref.id}
        if opts.get("iters") is not None:
            params["iters"] = int(opts["iters"])
        if opts.get("seed") is not None:
            params["seed"] = int(opts["seed"])
        # Opt-in negotiated congestion (realize.RealizeConfig.
        # negotiate_iterations); off unless asked for.
        if op == "route" and opts.get("negotiate") is not None:
            negotiate = int(opts["negotiate"])
            cap = pcb_realize.MAX_NEGOTIATE_ITERATIONS
            if not 0 <= negotiate <= cap:
                raise BadInput(
                    f"pcb: negotiate={negotiate} is out of range; give 0 (off) "
                    f"to {cap} negotiated-congestion iterations."
                )
            params["negotiate"] = negotiate
        board_id = (graph.get("board") or {}).get("board_id")
        # Raw store rows, verbatim -- `content_hash` itself does the
        # narrowing (job-written-field exclusion, authored-vs-derived
        # filtering) so there is exactly one place that can get it wrong.
        session_state = {
            "features": self.store.pcb_features_list(ref.id),
            "routes": self.store.pcb_routes_get(ref.id),
            "pin_swaps": self.store.pcb_pin_swaps_list(ref.id),
            "planes": self.store.pcb_planes_list(ref.id),
            "measures": self.store.pcb_measures_list(ref.id),
            # `pcb_fixed_copper_list` is keyed by board_id, not ref_id --
            # `graph["board"]["board_id"]` is already in hand from the
            # `pcb_graph` call above (`_pcb_board_meta`), so this costs no
            # extra round trip. A board-less design (no `pcb_boards` row
            # yet) has nothing authored to hash here.
            "fixed_copper": (
                self.store.pcb_fixed_copper_list(int(board_id))
                if board_id is not None
                else []
            ),
        }
        digest = pcb_session.content_hash(graph, params, session_state=session_state)
        idem_key = f"pcb_{op}:{ref.id}:{digest}"
        if op == "route":
            # Route code changes alter the result for an unchanged board;
            # the version keeps an old pending/complete job from deduping it.
            from precis.workers.job_types import pcb_route as pcb_route_job

            idem_key = f"pcb_route:{ref.id}:v{pcb_route_job.CODE_VERSION}:{digest}"
        job_resp = self.hub.sibling("job").put(
            job_type=f"pcb_{op}",
            executor="job_inproc",
            parent_id=ref.id,
            params=params,
            idem_key=idem_key,
        )
        status_view = "route-status" if op == "route" else "crossings"
        from precis.handlers.skill import code_stamp

        stale = self._stale_note(ref.id)
        return (
            f"# {op} {ref.slug} — enqueued\n{job_resp.body}\n\n"
            + (stale + "\n\n" if stale else "")
            + (
                f"Runs on the cluster worker's code, not necessarily this "
                f"session's ({code_stamp()}) — the job's `ran_on:` line records "
                "which build actually ran.\n"
                f"Next: get(kind='pcb', id='{ref.slug}', view='{status_view}') "
                "to check progress once the job lands."
            )
        )

    @staticmethod
    def _pose_kwargs(src: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        for k in ("x", "y", "rot"):
            if src.get(k) is not None:
                kwargs[k] = float(src[k])
        return kwargs

    def _op_move(self, ref: Any, args: dict[str, Any]) -> Response:
        if "moves" in args:
            raw = args["moves"]
            if not isinstance(raw, list) or not raw:
                raise BadInput(
                    "op='move' args.moves must be a non-empty list of poses",
                    next=_MOVES_EXAMPLE,
                )
            entries: list[tuple[str, dict[str, Any]]] = []
            for item in raw:
                rd = (
                    str(item.get("refdes") or "").strip()
                    if isinstance(item, dict)
                    else ""
                )
                pose_kw = self._pose_kwargs(item) if isinstance(item, dict) else {}
                if not rd or not pose_kw:
                    raise BadInput(
                        f"op='move' moves entry {item!r} needs refdes and at least "
                        "one of x / y / rot",
                        next=_MOVES_EXAMPLE,
                    )
                entries.append((rd, pose_kw))
            body = self._move_poses(ref, entries, single_form=False)
            assert body is not None  # only the single form returns None
            return Response(body=body)
        refdes = str(args.get("refdes") or "").strip()
        if not refdes:
            raise BadInput(
                "op='move' needs args.refdes",
                next="args={'op':'move','refdes':'U1','x':10.0,'y':5.0,'rot':90}",
            )
        kwargs = self._pose_kwargs(args)
        if "fixed" in args:
            kwargs["fixed"] = args["fixed"]
        if not kwargs:
            raise BadInput(
                "op='move' needs at least one of x / y / rot / fixed",
                next="args={'op':'move','refdes':'U1','fixed':'xy'}",
            )
        if {"x", "y", "rot"} & kwargs.keys():
            group_body = self._move_poses(ref, [(refdes, kwargs)], single_form=True)
            if group_body is not None:
                return Response(body=group_body)
            standing = self._refuse_illegal_pose(ref, refdes, kwargs)
        else:
            standing = []
        ok = self.store.pcb_move_instance(ref.id, refdes, **kwargs)
        if not ok:
            raise NotFound(f"pcb instance {refdes!r} not found in {ref.slug!r}")
        body = f"# {refdes} moved — {kwargs}"
        if standing:
            body += (
                f"\nboard still has {len(standing)} pre-existing DRC error(s) the "
                "move did not add: " + "; ".join(standing[:5])
            )
        return Response(body=body)

    def _move_poses(
        self,
        ref: Any,
        entries: list[tuple[str, dict[str, Any]]],
        *,
        single_form: bool,
    ) -> str | None:
        """``op='move'`` on one or more parts
        (docs/backlog/pcb-always-valid-board-invariant.md, Reto's rulings 1
        and 2, 2026-10-02: "a part's own footprint copper always moves with
        it"; a list of poses is validated together as ONE resulting state).

        A generator member brings its WHOLE generator group as one rigid
        body — every instance the generator emitted (its own refdes, or
        ``{name}_…``: the convention ``_pcb_generator_retire_expansion``
        retires by) and every ``pcb_fixed_copper`` row it owns, rotated
        about the moved instance's old origin and translated to its new one.
        The board is judged AFTER every listed move is applied, before
        anything is written: each moved part's pose by the placer's rule
        (``pose_conflicts`` on an engine built over the resulting board, so
        a part moved into the slot another listed part is leaving is legal
        — a swap needs no parking spot), the carried copper by the route
        gate's rule against every other part's pads and fixed copper. Router
        copper that would dangle (a net on a moved pad) or collide is
        ripped, not kept: the board stays valid and goes incomplete. One
        transaction (:meth:`Store.pcb_move_groups`).

        Returns the response body; with ``single_form`` it returns ``None``
        when the refdes belongs to no generator (the plain single-part
        path, whose response and refusal predate the list form)."""
        gens = self.store.pcb_generators_for(ref.id)
        graph = self.store.pcb_graph(ref.id)
        by_refdes = {str(i["refdes"]): i for i in graph.get("instances") or []}

        def owns(name: str, g: dict[str, Any], rd: str) -> bool:
            return rd == g.get("refdes") or rd.startswith(name + "_")

        # One plan per listed part: its rigid body (members), the transform.
        plans: list[dict[str, Any]] = []
        claimed: dict[str, str] = {}  # member refdes -> the listed refdes
        for refdes, pose in entries:
            if refdes in {p["refdes"] for p in plans}:
                raise BadInput(
                    f"pcb: {refdes} is listed twice in moves",
                    next="list each part once; nothing was changed",
                )
            if refdes not in by_refdes:
                if single_form:
                    return None  # the plain path reports the missing refdes
                raise NotFound(f"pcb instance {refdes!r} not found in {ref.slug!r}")
            names = [n for n, g in gens.items() if owns(n, g, refdes)]
            if single_form and not names:
                return None
            members = (
                sorted(
                    rd for rd in by_refdes if any(owns(n, gens[n], rd) for n in names)
                )
                if names
                else [refdes]
            )
            for m in members:
                if m in claimed:
                    raise BadInput(
                        f"pcb: {refdes} and {claimed[m]} are listed in one move but "
                        f"share a generator group ({m} belongs to both): a group "
                        "moves as one rigid body, so list only one of them",
                        next="nothing was changed",
                    )
                claimed[m] = refdes
            old = by_refdes[refdes]
            if names and (old["x"] is None or old["y"] is None):
                raise BadInput(
                    f"pcb: {refdes} belongs to generator {', '.join(names)} but has "
                    "no placed pose, so its group has no frame to move rigidly from",
                    next="run put(args={'op':'place'}) first",
                )
            ox = float(old["x"]) if old["x"] is not None else math.nan
            oy = float(old["y"]) if old["y"] is not None else math.nan
            old_rot = float(old["rot"] or 0.0)
            nx, ny = float(pose.get("x", ox)), float(pose.get("y", oy))
            if math.isnan(nx) or math.isnan(ny):
                raise BadInput(
                    f"pcb: {refdes} is unplaced; its move needs both x and y",
                    next="args={'op':'move','moves':[{'refdes':'U1','x':1,'y':2}]}",
                )
            new_rot = float(pose.get("rot", old_rot))
            dtheta = new_rot - old_rot
            poses: list[tuple[str, float, float, float]] = []
            for rd in members:
                inst = by_refdes[rd]
                if rd == refdes:
                    poses.append((rd, nx, ny, new_rot))
                elif inst["x"] is not None and inst["y"] is not None:
                    dx, dy = rotate_offset(
                        float(inst["x"]) - ox, float(inst["y"]) - oy, dtheta
                    )
                    rot = (float(inst["rot"] or 0.0) + dtheta) % 360.0
                    poses.append((rd, nx + dx, ny + dy, rot))
            plans.append(
                {
                    "refdes": refdes,
                    "names": names,
                    "members": set(members),
                    "member_list": members,
                    "poses": poses,
                    "pivot": (ox, oy),
                    "target": (nx, ny),
                    "dtheta": dtheta,
                    "pose": pose,
                }
            )
        member_set: set[str] = set().union(*(p["members"] for p in plans))
        all_names = [n for p in plans for n in p["names"]]
        all_poses = [t for p in plans for t in p["poses"]]
        new_pose = {rd: (x, y, r) for rd, x, y, r in all_poses}
        graph_new = {
            **graph,
            "instances": [
                {
                    **i,
                    "x": new_pose[str(i["refdes"])][0],
                    "y": new_pose[str(i["refdes"])][1],
                    "rot": new_pose[str(i["refdes"])][2],
                }
                if str(i["refdes"]) in new_pose
                else i
                for i in graph["instances"]
            ],
        }
        label = ", ".join(p["refdes"] for p in plans)

        board_id = (graph.get("board") or {}).get("board_id")
        fixed_old = (
            self.store.pcb_fixed_copper_list(int(board_id))
            if board_id is not None
            else []
        )
        carried_old: list[dict[str, Any]] = []
        carried_new: list[dict[str, Any]] = []
        for p in plans:
            rows = [r for r in fixed_old if r.get("generator_name") in p["names"]]
            carried_old += rows
            carried_new += [
                pcb_geom.rigid_transform_geom(
                    r, pivot=p["pivot"], target=p["target"], dtheta_deg=p["dtheta"]
                )
                for r in rows
            ]
        foreign = [r for r in fixed_old if r.get("generator_name") not in all_names]

        def capabilities() -> Any:
            # No capability row = no rule to judge copper by. Refuse rather
            # than move unchecked copper (legality never fails open).
            try:
                return capability_for(
                    pcb_drc.process_for_stackup((graph.get("board") or {})["stackup"])
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise BadInput(
                    f"pcb: cannot judge {label}'s copper on this board — {exc}"
                ) from exc

        caps: Any = capabilities() if (all_names or carried_old) else None

        def footprints_for(ir: pcb_ir.PcbIR) -> dict[str, dict[str, Any]]:
            return pcb_session.footprints_by_refdes(
                ir,
                self.store.pcb_footprints_for(ref.id),
                local_footprints_by_name=self.store.pcb_local_footprints_for(ref.id),
                local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(
                    graph
                ),
            )

        problems: list[str] = []

        # (a) every moved part's pose against the placer's rule, on the board
        # AFTER every listed move. A group's own carried vias are left out of
        # this IR: they travel with the lands, so their relation is rigid —
        # only authored vias a member could newly land on are the placer's.
        ir_pose = self._build_ir(ref.id, graph_new, fixed_copper=foreign)
        ir_pose.outline = self._move_outline(ref.id)
        engine = pcb_optimize.OptimizeEngine(ir_pose, pcb_optimize.OptimizeConfig())
        idx = {str(r): i for i, r in enumerate(ir_pose.instance_refdes)}
        # The same rule as a DELTA (a standing overlap must be repairable
        # by the move): judge every moved part on the board before and
        # after, refuse only a conflict that is new or deeper.
        ir_pose_old = self._build_ir(ref.id, graph, fixed_copper=foreign)
        ir_pose_old.outline = ir_pose.outline
        engine_old = pcb_optimize.OptimizeEngine(
            ir_pose_old, pcb_optimize.OptimizeConfig()
        )
        idx_old = {str(r): i for i, r in enumerate(ir_pose_old.instance_refdes)}
        before_side: list[tuple[tuple[Any, int, str], tuple[float, float, float]]] = []
        after_side: list[tuple[tuple[Any, int, str], tuple[float, float, float]]] = []
        for p in plans:
            for rd, x, y, r in p["poses"]:
                after_side.append(((engine, idx[rd], rd), (x, y, r)))
                o = by_refdes[rd]
                if o["x"] is not None and o["y"] is not None:
                    before_side.append(
                        (
                            (engine_old, idx_old[rd], rd),
                            (float(o["x"]), float(o["y"]), float(o["rot"] or 0.0)),
                        )
                    )
        pose_problems, pose_standing = _pose_delta(
            before_side,
            after_side,
            skip={rd: p["members"] for p in plans for rd in p["members"]},
        )
        problems.extend(
            f"{rule}: {rd} with {other}{sfx}" for rule, rd, other, sfx in pose_problems
        )

        # (b) the carried copper and the moved lands against everything
        # else, by the route gate's rule. Only findings the move ADDS or
        # WORSENS count: each finding is keyed by the identity of its
        # objects (pad = refdes/pin, fixed copper = fixed_id), never by
        # coordinates, so a pre-existing finding between two group members
        # keeps its key across the rigid move. Same key is not enough: the
        # measured margin must not get worse (a deeper overlap is a new
        # fault). What the move leaves standing is reported, not refused.
        ir_new = self._build_ir(ref.id, graph_new, fixed_copper=foreign + carried_new)
        standing: list[str] = [
            f"{rule}: {rd} with {other}" for rule, rd, other, _ in pose_standing
        ]
        if carried_old:

            def keyed(
                ir: pcb_ir.PcbIR, rows: list[dict[str, Any]]
            ) -> list[tuple[tuple[Any, ...], float, str]]:
                fps = footprints_for(ir)
                found = [
                    *pcb_session.fixed_copper_findings(ir, fps, rows, caps),
                    *pcb_session.fixed_via_pad_findings(ir, fps, rows, caps),
                ]
                return [
                    (_finding_identity(f), f.margin_mm or 0.0, f"{f.rule}: {f.where}")
                    for f in found
                ]

            ir_old = self._build_ir(ref.id, graph, fixed_copper=fixed_old)
            worse, kept = _margin_delta(
                [(k, m) for k, m, _ in keyed(ir_old, fixed_old)],
                [(k, m, line) for k, m, line in keyed(ir_new, foreign + carried_new)],
            )
            for line, margin, old_margin in worse:
                problems.append(
                    line
                    if old_margin is None
                    else f"{line} (worse: {margin:.4f}mm vs {old_margin:.4f}mm "
                    "before the move)"
                )
            standing.extend(kept)
        first = plans[0]
        if problems:
            shown = "; ".join(problems[:8])
            more = f" (+{len(problems) - 8} more)" if len(problems) > 8 else ""
            if len(plans) == 1 and first["names"]:
                tx, ty = first["target"]
                raise BadInput(
                    f"pcb: moving {label} to ({tx:g}, {ty:g}) would leave an invalid "
                    f"board — it carries its generator group "
                    f"({', '.join(first['member_list'])}) and "
                    f"{len(carried_old)} fixed-copper row(s) with it: {shown}{more}",
                    next="pick a pose clear of these; nothing was changed",
                )
            targets = ", ".join(
                f"{p['refdes']} to ({p['target'][0]:g}, {p['target'][1]:g})"
                for p in plans
            )
            raise BadInput(
                f"pcb: moving {targets} together would leave an invalid board: "
                f"{shown}{more}",
                next="pick poses clear of these; nothing was changed",
            )

        # Router copper: a net on a moved pad dangles, and any other net
        # whose copper now collides with the moved parts has to yield.
        rip: dict[str, str] = {}
        have_copper = (
            self.store.pcb_nets_with_router_copper(int(board_id))
            if board_id is not None
            else set()
        )
        for net in graph.get("nets") or []:
            if str(net["name"]) in have_copper and any(
                str(m.get("refdes")) in member_set for m in net.get("members") or []
            ):
                rip[str(net["name"])] = "on a moved pad"
        if have_copper and board_id is not None:
            router_rows = [
                r
                for r in self.store.pcb_copper_list(int(board_id))
                if not r.get("fixed") and r.get("net") not in rip
            ]
            conflicts = pcb_session.router_copper_conflicts(
                ir_new,
                footprints_for(ir_new),
                router_rows=router_rows,
                carried_rows=carried_new,
                members=member_set,
                fab_caps=caps if caps is not None else capabilities(),
            )
            for net, lines in conflicts.items():
                rip.setdefault(net, f"collides with the moved group ({lines[0]})")
        lock = (
            (first["refdes"], first["pose"]["fixed"])
            if single_form and "fixed" in first["pose"]
            else None
        )
        try:
            ripped = self.store.pcb_move_groups(
                ref.id,
                int(board_id) if board_id is not None else 0,
                all_poses,
                transforms=[
                    (p["names"], p["pivot"], p["target"], p["dtheta"])
                    for p in plans
                    if p["names"] and board_id is not None
                ],
                rip_nets=sorted(rip),
                lock=lock,
            )
        except ValueError as exc:
            raise NotFound(str(exc)) from exc
        if single_form:
            tx, ty = first["target"]
            px, py = first["pivot"]
            body = (
                f"# {label} moved — {first['pose']}\n"
                f"generator group {', '.join(first['names'])} moved rigidly "
                f"(dx={tx - px:g}, dy={ty - py:g}, drot={first['dtheta']:g}): "
                f"{', '.join(rd for rd, *_ in all_poses)}; "
                f"{len(carried_old)} fixed-copper row(s) carried with it"
            )
        else:
            body = (
                f"# moved {len(plans)} part(s) together — {label}\n"
                + "\n".join(
                    f"{rd}: ({x:g}, {y:g}, rot {r:g})" for rd, x, y, r in all_poses
                )
                + f"\n{len(carried_old)} fixed-copper row(s) carried with them"
            )
        if ripped:
            body += (
                f"\nripped {len(ripped)} net(s): "
                + "; ".join(f"{n} ({rip[n]})" for n in ripped)
                + "; re-route with op='route'"
            )
        if standing:
            body += (
                f"\nboard still has {len(standing)} pre-existing DRC error(s) the "
                "move did not add: "
                + "; ".join(standing[:5])
                + (f" (+{len(standing) - 5} more)" if len(standing) > 5 else "")
            )
        return body

    def _move_outline(self, ref_id: int) -> list[tuple[float, float]] | None:
        """The authored board outline for a move's pose judgement (the IR
        ``_build_ir`` returns carries none; attaching it there would move
        the placer's bounds for every other view)."""
        outline = self._outline_from_features(ref_id)
        if not outline or len(outline) < 3:
            return None
        return [(float(p[0]), float(p[1])) for p in outline]

    def _refuse_illegal_pose(
        self, ref: Any, refdes: str, pose: dict[str, Any]
    ) -> list[str]:
        """docs/backlog/pcb-always-valid-board-invariant.md: a move that
        would put ``refdes``'s courtyard on another part or a mounting hole,
        or a solder land on an authored via, is refused with the rule and
        the pair — the placer's own rule
        (:meth:`precis.pcb.optimize.OptimizeEngine.pose_conflicts`), so a
        hand edit cannot store what the anneal may not. Only the moved
        part is judged: a conflict elsewhere on the board is not this
        move's to refuse."""
        graph = self.store.pcb_graph(ref.id)
        ir = self._build_ir(ref.id, graph, with_fixed_copper=True)
        ir.outline = self._move_outline(ref.id)
        idx = {str(r): i for i, r in enumerate(ir.instance_refdes)}.get(refdes)
        if idx is None:
            return []  # pcb_move_instance reports the missing refdes
        x = float(pose.get("x", ir.inst_x[idx]))
        y = float(pose.get("y", ir.inst_y[idx]))
        if math.isnan(x) or math.isnan(y):
            return []  # unplaced: a rotation alone has no geometry to judge
        rot = float(pose.get("rot", ir.inst_rot[idx]))
        engine = pcb_optimize.OptimizeEngine(ir, pcb_optimize.OptimizeConfig())
        old_x, old_y = float(ir.inst_x[idx]), float(ir.inst_y[idx])
        old_rot = float(ir.inst_rot[idx])
        before_side: list[tuple[tuple[Any, int, str], tuple[float, float, float]]] = []
        if not (math.isnan(old_x) or math.isnan(old_y)):
            before_side.append(
                (
                    (engine, idx, refdes),
                    (old_x, old_y, 0.0 if math.isnan(old_rot) else old_rot),
                )
            )
        problems, standing = _pose_delta(
            before_side,
            [((engine, idx, refdes), (x, y, 0.0 if math.isnan(rot) else rot))],
            skip={},
        )
        if problems:
            raise BadInput(
                f"pcb: moving {refdes} to ({x:g}, {y:g}) would leave an invalid "
                "board: "
                + "; ".join(
                    f"{rule} with {other}{sfx}" for rule, _rd, other, sfx in problems
                ),
                next=(
                    "pick a pose clear of these, or run put(args={'op':'place'}) "
                    "to let the placer find one"
                ),
            )
        return [f"{rule} with {other}" for rule, _rd, other, _s in standing]

    def _op_rip(self, ref: Any, args: dict[str, Any]) -> Response:
        net = str(args.get("net") or "").strip()
        if not net:
            raise BadInput(
                "op='rip' needs args.net",
                next="args={'op':'rip','net':'I2C_SCL'}",
            )
        ripped = self.store.pcb_rip_route(ref.id, net)
        if not ripped:
            return Response(body=f"net {net!r} has no route to rip (already unrouted)")
        return Response(
            body=f"# ripped {net} — sketch cleared, back to unrouted\n"
            "Next: put(args={'op':'route'}) to re-decide its topology, or "
            "args={'op':'pin_side'} to steer it first."
        )

    def _op_pin_side(self, ref: Any, args: dict[str, Any]) -> Response:
        net = str(args.get("net") or "").strip()
        a, b = str(args.get("a") or "").strip(), str(args.get("b") or "").strip()
        side = args.get("side")
        if not (net and a and b) or side is None:
            raise BadInput(
                "op='pin_side' needs args.net, args.a, args.b (each "
                "'REFDES.PIN'), args.side",
                next="args={'op':'pin_side','net':'I2C_SCL','a':'U1.SCL',"
                "'b':'R1.1','side':1}",
            )
        ok = self.store.pcb_pin_topology(ref.id, net, a, b, int(side))
        if not ok:
            raise NotFound(f"net {net!r} not found in {ref.slug!r}")
        return Response(
            body=f"# pinned {a} ↔ {b} on {net} to side={side}\n"
            "Takes effect on the next put(args={'op':'route'})."
        )

    def _op_plane_net(self, ref: Any, args: dict[str, Any]) -> Response:
        layer = str(args.get("layer") or "").strip()
        net = str(args.get("net") or "").strip()
        if not (layer and net):
            raise BadInput(
                "op='plane_net' needs args.layer and args.net",
                next="args={'op':'plane_net','layer':'In1.Cu','net':'GND'}",
            )
        design = self.store.pcb_load(ref.id)
        layer_names = [str(entry.get("name")) for entry in design["board"]["stackup"]]
        if layer not in layer_names:
            raise BadInput(
                f"layer {layer!r} is not in this board's stackup",
                options=layer_names,
            )
        # A layer is ONE sheet of copper, so two nets cannot both be poured
        # on it — a hard constraint, not a price. Without this, a second
        # op='plane_net' on the same layer was accepted, and
        # planes.plane_pours' "lowest net id wins" tie-break then silently
        # dropped one of them: the caller's instruction was stored, agreed
        # to, and never appeared on the board. Same failure the annealer's
        # one-net-per-plane-layer fix closed on the derived side.
        conflict = next(
            (
                row
                for row in self.store.pcb_planes_list(ref.id)
                if row["layer"] == layer and row["net"] != net
            ),
            None,
        )
        if conflict is not None:
            raise BadInput(
                f"layer {layer!r} already carries plane net {conflict['net']!r} "
                f"({conflict['source']}) — a layer is one sheet of copper, so "
                "reassign it or pick a different layer",
                options=[n for n in layer_names if n != layer],
            )
        plane_id = self.store.pcb_assign_plane(ref.id, layer, net)
        if not plane_id:
            raise NotFound(f"net {net!r} not found in {ref.slug!r}")
        return Response(
            body=f"# {net} assigned to plane layer {layer}\n"
            "Takes effect on the next put(args={'op':'route'}) — its pins "
            "fan out (dog-bone) instead of routing."
        )

    def _op_class_rules(self, ref: Any, args: dict[str, Any]) -> Response:
        """``put(args={'op':'class_rules','name':...,'rules':{...}})`` — set
        one net class's rules. Judged (:meth:`_judged_mutation`): router
        copper the new rules no longer admit is ripped (its net goes
        unrouted and is listed); a new error between pads/authored copper
        refuses the change and stores nothing."""
        name = str(args.get("name") or "").strip()
        rules = args.get("rules")
        if not name or not isinstance(rules, dict):
            raise BadInput(
                "op='class_rules' needs args.name and args.rules (a dict)",
                next="args={'op':'class_rules','name':'i2c',"
                "'rules':{'clearance_mm':0.2}}",
            )
        _, judged = self._judged_mutation(
            ref.id,
            lambda conn: self.store.pcb_set_class_rules(ref.id, name, rules, conn=conn),
        )
        return Response(
            body=f"# net class {name!r} rules set: {rules}\n" + judged.lines()
        )

    def _op_stackup(self, ref: Any, args: dict[str, Any]) -> Response:
        """``put(args={'op':'stackup','layers':[...]})`` — author the
        board's copper stackup, which until now every board was STUCK on
        (:data:`precis.pcb.DEFAULT_STACKUP`, stamped at board birth by
        ``store.pcb_ensure_board`` and never writable after).

        This is the authoring seam the engine already routes through. The
        stackup decides which layers may carry a trace
        (:func:`precis.pcb.ir.layer_is_routable`), which the annealer may
        pour on its own (:func:`~precis.pcb.ir.layer_is_pourable`), and
        which DRC capability row the board is checked against
        (:func:`precis.pcb.drc.process_for_stackup`) — all three already
        read the BOARD's stackup, so nothing downstream changes shape
        here; a fact that was engine policy becomes design data.

        Concretely: ``DEFAULT_STACKUP`` makes In1.Cu/In2.Cu planes, so
        B.Cu is the only layer a 4-layer board can route on today. An
        author who wants two routing layers and keeps the ground plane
        says so — ``In1.Cu`` plane/GND, ``In2.Cu`` signal — instead of
        waiting for the engine to change its mind.
        """
        layers = args.get("layers")
        try:
            stackup = pcb_ir.validate_stackup(layers)
            # Layer COUNT is the capability table's question, not the
            # entry validator's — asked here so a 6-layer stackup is
            # refused at authoring time rather than at the first
            # view='drc', with that function's own precise message.
            pcb_drc.process_for_stackup(stackup)
        except ValueError as exc:
            raise BadInput(
                str(exc),
                next=(
                    "args={'op':'stackup','layers':["
                    "{'name':'F.Cu','role':'signal'},"
                    "{'name':'In1.Cu','role':'plane','plane_net':'GND'},"
                    "{'name':'In2.Cu','role':'signal'},"
                    "{'name':'B.Cu','role':'signal'}]}"
                ),
            ) from exc

        design = self.store.pcb_load(ref.id)
        board_id = int(design["board"]["board_id"])
        names = {entry["name"] for entry in stackup}

        # Refuse to STRAND copper. Every copper row names its layer (a
        # via names two, in `span`), and `pcb_copper_list` already unions
        # the authored fixed-copper rows in, so one read covers both.
        # Dropping a layer out from under them would leave rows no
        # exporter, DRC pass or router could resolve — silently absent
        # from the fab output rather than loudly rejected here.
        planes = self.store.pcb_planes_list(ref.id)
        in_use: set[str] = {str(row["layer"]) for row in planes}
        for row in self.store.pcb_copper_list(board_id):
            if row.get("layer"):
                in_use.add(str(row["layer"]))
            in_use.update(str(x) for x in (row.get("span") or []))
        stranded = sorted(in_use - names)
        if stranded:
            raise BadInput(
                f"stackup: {stranded} still carries copper or a plane "
                "assignment on this board",
                next=(
                    "put(args={'op':'rip'}) to clear routed copper first, or "
                    "keep those layers in the stackup"
                ),
            )

        # `plane_net` on a stackup entry was a DEAD key until now — nothing
        # in the engine read it, so DEFAULT_STACKUP's own In1.Cu/GND
        # declaration has never poured anything. Applying it through the
        # same `pcb_planes` write `op='plane_net'` uses makes the
        # declaration true instead of decorative; a named net that does not
        # exist is an error, never a silent skip.
        declared = {e["name"]: e["plane_net"] for e in stackup if "plane_net" in e}
        existing = {str(row["layer"]): str(row["net"]) for row in planes}
        clashes = [
            f"{layer} already pours {existing[layer]!r}"
            for layer, net in declared.items()
            if layer in existing and existing[layer] != net
        ]
        if clashes:
            raise BadInput(
                "stackup: " + "; ".join(clashes) + " — a layer is one sheet "
                "of copper, so drop the plane_net here or reassign the layer",
                next="put(args={'op':'plane_net','layer':'In1.Cu','net':'GND'})",
            )
        # CHECKED before any of it is WRITTEN. `pcb_assign_plane` returns 0
        # for a net that does not resolve, so assigning-and-checking in one
        # pass would leave the first declaration written and the second
        # rejected — the board keeping half of an instruction this call
        # then reports as an error, with the stackup itself never stored.
        # `design["nets"]` is already in hand and already filters retired
        # rows; a second `pcb_graph` read for the same names would be a
        # heavier query for a fact this one carries.
        known = {str(net["name"]) for net in design["nets"]}
        missing = [
            f"{layer}: no net {net!r}"
            for layer, net in declared.items()
            if net not in known
        ]
        if missing:
            raise BadInput(
                "stackup: " + "; ".join(missing) + " — author the nets before "
                "declaring a plane_net for them, or omit the key",
                next=f"get(kind='pcb', id='{ref.slug}', view='nets')",
            )

        self.store.pcb_set_stackup(board_id, stackup)
        # The `known` check above is a READ, so a concurrent retire of one
        # of these nets can still land between it and this loop. Re-checked
        # on the return value rather than trusted: 0 means the assignment
        # did not happen, and the one thing this op must never do is report
        # success over a declaration it silently dropped. The stackup IS
        # stored by then — that half succeeded — so the message says so.
        for layer, net in declared.items():
            if not self.store.pcb_assign_plane(ref.id, layer, net):
                raise BadInput(
                    f"stackup stored, but {layer}'s plane_net {net!r} could "
                    "not be applied — that net stopped existing during this "
                    "call (a concurrent edit retired it)",
                    next=(
                        f"put(args={{'op':'plane_net','layer':'{layer}',"
                        f"'net':'{net}'}}) once the net is back"
                    ),
                )
        routable = [e["name"] for e in stackup if pcb_ir.layer_is_routable(e)]
        return Response(
            body=f"# {ref.slug} stackup set — {len(stackup)} layers\n"
            + "\n".join(
                f"- {e['name']}: "
                + ", ".join(
                    filter(
                        None,
                        [
                            f"role={e.get('role', '—')}",
                            "routable" if pcb_ir.layer_is_routable(e) else None,
                            "pourable" if pcb_ir.layer_is_pourable(e) else None,
                            f"plane={e['plane_net']}" if "plane_net" in e else None,
                        ],
                    )
                )
                for e in stackup
            )
            + f"\n\nRoutable: {routable}. Takes effect on the next "
            "put(args={'op':'route'}).",
        )

    # ── footprint cache (gr341532 fix 3) ────────────────────────────────
    def _op_footprint(self, ref: Any, args: dict[str, Any]) -> Response:
        """``put(args={'op':'footprint', ...})`` — fill the ``part_footprints``
        cache (``part_footprint_get``/``part_footprint_put``), which nothing in
        this handler used to fill: a catalog-part instance
        with no cached row silently DRC'd at a synthesized bound (the
        ``synthesized_footprint`` finding, :func:`precis.pcb.drc.
        check_synthesized_footprint`) with no MCP-facing way to close the
        gap. Two modes:

        ``args.part``/``args.parts`` — pull each C-number through the real
        EasyEDA fetcher (:func:`precis.pcb.easyeda.fetch_component`, wired
        through :func:`precis.utils.safe_fetch.safe_get`). ``args.force``
        re-pulls even when already cached. A per-part failure (network,
        vendor, no-footprint) is reported in that part's own row — never
        raised — so one bad C-number in a batch doesn't lose the rest.

        ``args.footprint`` (with ``args.part`` naming exactly one C-number)
        authors a footprint directly, for a part the API has nothing for —
        the same ``{pads, ...}`` shape and the same validator
        (:func:`precis.store._pcb_ops._normalize_local_footprint`) the
        design-local ``footprints:[...]`` batch key already uses, cached
        under ``source='authored'``.

        Cached pad geometry is read lazily at IR-build time
        (:meth:`_build_ir` -> :func:`precis.pcb.session.
        apply_real_pin_offsets`), off ``pcb_footprints_for``/
        ``part_footprint_get`` fresh on every call — nothing derived is
        stored on the design itself, so there is no re-derive step here."""
        footprint_data = args.get("footprint")
        raw_parts = args.get("parts")
        if raw_parts is None:
            single = args.get("part")
            raw_parts = [single] if single else []
        parts = [str(p).strip() for p in raw_parts if str(p or "").strip()]

        if footprint_data is not None:
            if len(parts) != 1:
                raise BadInput(
                    "op='footprint' with args.footprint needs exactly one "
                    "args.part (the C-number to author it under)",
                    next="put(kind='pcb', id='slug', args={'op':'footprint',"
                    "'part':'C639448','footprint':{'pads':[...]}})",
                )
            lcsc = parts[0].upper()
            try:
                _, data = normalize_local_footprint({**footprint_data, "name": lcsc})
            except ValueError as exc:
                raise BadInput(f"pcb: {exc}") from exc
            data["source"] = "authored"
            judged = self._put_footprints_judged(ref, {lcsc: data})
            row = self.store.part_footprint_get(lcsc)
            return Response(
                body=f"# footprint {lcsc} — authored\n"
                + render_agent_table(
                    [self._footprint_summary_row(lcsc, row, error=None)],
                    schema=_FOOTPRINT_SCHEMA,
                )
                + self._footprint_judge_lines(ref, [lcsc], judged)
            )

        if not parts:
            raise BadInput(
                "op='footprint' needs args.part (one C-number) or "
                "args.parts (a list), or args.footprint to author one",
                next="put(kind='pcb', id='slug', args={'op':'footprint',"
                "'part':'C639448'})",
            )
        force = bool(args.get("force"))
        # Fetch first, outside any transaction (a network round-trip must
        # not hold one open); only the cache writes run in the judged tx.
        fetched: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        for raw in parts:
            lcsc = raw.upper()
            try:
                if not force and self.store.part_footprint_get(lcsc) is not None:
                    continue
                pulled = fetch_footprint(lcsc)
                if pulled is not None:
                    fetched[lcsc] = pulled
            except Exception as exc:
                # Broad on purpose — per-part isolation is the whole point
                # (gr341532 fix 3's spec: "must NOT raise for the batch"); a
                # vendor/network failure on one C-number reports in that
                # row, the rest of the batch still runs.
                errors[lcsc] = f"{type(exc).__name__}: {exc}"
        judged = self._put_footprints_judged(ref, fetched) if fetched else JudgeReport()
        rows: list[dict[str, str]] = []
        n_ok = 0
        for raw in parts:
            lcsc = raw.upper()
            error = errors.get(lcsc)
            row = self.store.part_footprint_get(lcsc)
            if row is None and error is None:
                error = (
                    "no EasyEDA footprint for this part (symbol-only, or unrecognized)"
                )
            if row is not None:
                n_ok += 1
            rows.append(self._footprint_summary_row(lcsc, row, error=error))
        head = f"# footprint — {n_ok}/{len(parts)} cached"
        return Response(
            body=head
            + "\n"
            + render_agent_table(rows, schema=_FOOTPRINT_SCHEMA)
            + self._footprint_judge_lines(ref, list(fetched), judged)
        )

    def _put_footprints_judged(
        self, ref: Any, footprints: dict[str, dict[str, Any]]
    ) -> JudgeReport:
        """Write footprint cache rows inside a judged transaction on ``ref``
        (the real footprint wins: ``refuse=False``). Router copper the new
        pads or courtyard now collide with is ripped; pad/placement
        collisions are kept in ``JudgeReport.visible``."""

        def apply(_conn: Any) -> None:
            # part_footprint_put joins the ambient judged transaction.
            for lcsc, data in footprints.items():
                self.store.part_footprint_put(lcsc, data)

        try:
            _, judged = self._judged_mutation(ref.id, apply, refuse=False)
        except Exception as exc:
            # The ruling is that the real footprint is always stored: a judge
            # crash rolled the transaction back, so write it unjudged.
            for lcsc, data in footprints.items():
                self.store.part_footprint_put(lcsc, data)
            return JudgeReport(unjudged=f"{type(exc).__name__}: {exc}")
        return judged

    def _footprint_judge_lines(
        self, ref: Any, lcscs: list[str], judged: JudgeReport
    ) -> str:
        """The footprint op's judge report: ripped nets, what is now visible
        (first 8), and the other designs sharing the catalogue-wide cache
        rows just written."""
        out = [
            f"{net} ripped: {rule} after this footprint change — re-route\n"
            for net, rule in sorted(judged.ripped.items())
        ]
        shown = [*judged.margins, *judged.visible]
        out.extend(
            f"now visible (real footprint): {v} — standing until a re-place\n"
            for v in shown[:8]
        )
        if len(shown) > 8:
            out.append(f"+{len(shown) - 8} more\n")
        if judged.unjudged:
            out.append(
                "could not judge this design after the footprint change: "
                f"{judged.unjudged} — check view='drc'\n"
            )
        for lcsc in lcscs:
            others = self.store.pcb_designs_using_part(lcsc, exclude_ref_id=ref.id)
            if others:
                names = ", ".join(others[:5]) + (
                    f" (+{len(others) - 5} more)" if len(others) > 5 else ""
                )
                out.append(
                    f"{len(others)} other design(s) use {lcsc}: {names} — "
                    "check view='drc' there\n"
                )
        return "".join(out)

    @staticmethod
    def _footprint_summary_row(
        lcsc: str, row: dict[str, Any] | None, *, error: str | None
    ) -> dict[str, str]:
        """One ``op='footprint'``/``view='footprints'`` table row off a
        ``part_footprint_get`` row (or ``None`` on a miss) — the shared
        shape both surfaces render so a pulled/authored part and a
        still-uncached one read the same way."""
        if row is None:
            return {
                "lcsc": lcsc,
                "cached": "no",
                "source": "",
                "n_pads": "",
                "n_pins": "",
                "courtyard": "",
                "error": error or "",
            }
        pads = row.get("pads") or []
        pin_map = row.get("pin_map") or {}
        return {
            "lcsc": lcsc,
            "cached": "yes",
            "source": str(row.get("source") or ""),
            "n_pads": str(len(pads)),
            "n_pins": str(len(pin_map)),
            "courtyard": "yes" if row.get("courtyard") else "no",
            "error": error or "",
        }

    # ── the eyes ───────────────────────────────────────
    def _render_view(self, ref_id: int, view: str, args: dict[str, Any]) -> Response:
        if view not in _VIEWS:
            raise BadInput(
                f"unknown pcb view {view!r}",
                next=f"view= one of {list(_VIEWS)}, or omit for the netlist TOC",
            )
        if view in _EXPORT_VIEWS:
            return self._render_export(ref_id, view, args)
        if view in _ROUTE_VIEWS:
            return self._render_route(ref_id, args)
        if view in _STATUS_VIEWS:
            return self._render_route_status(ref_id)
        if view == "congestion":
            return self._render_congestion(ref_id)
        if view == "planes":
            return self._render_planes(ref_id)
        if view == "drc":
            return self._render_drc(ref_id)
        if view == "svg":
            return self._render_svg(ref_id, args)
        if view == "capability":
            return self._render_capability(ref_id, args)
        if view == "schematic":
            from precis.pcb import schematic

            ref = self.store.fetch_refs_by_ids([ref_id]).get(ref_id)
            slug = (ref.slug if ref is not None else None) or "pcb"
            return Response(
                body=schematic.render_schematic_svg(
                    self.store.pcb_graph(ref_id), title=slug
                )
            )
        if view == "footprints":
            return self._render_footprints_view(ref_id)
        if view == "links":
            # Graph-completeness audit item 1 (OPEN-ITEMS.md 🕸️) — sweep of
            # every Handler-direct kind alongside the paper fix.
            from precis.handlers._links_render import render_links_view

            ref = self.store.fetch_refs_by_ids([ref_id]).get(ref_id)
            if ref is None:
                raise NotFound(f"pcb id={ref_id} not found")
            return render_links_view(self.store, ref, sense="pcb")
        graph = pcb_session.sorted_graph(self.store.pcb_graph(ref_id))
        if view in ("crossings", "ratsnest", "feasibility"):
            placed = {
                i["refdes"]: (float(i["x"]), float(i["y"]))
                for i in graph["instances"]
                if i["x"] is not None and i["y"] is not None
            }
            # gr449579 / td450119: the side set is what makes the MST's via
            # bias fire. Resolved HERE from the same `pcb_instances.layer`
            # predicate everything else reads — `ratsnest` deliberately
            # never re-derives top/bottom itself.
            bottom = frozenset(
                i["refdes"]
                for i in graph["instances"]
                if padplace.is_bottom_instance(i)
            )
            wires = ratsnest.build_airwires(placed, graph["nets"], bottom=bottom)
            if view == "feasibility":
                f = place.route_feasibility(wires)
                return Response(
                    body=(
                        f"# route feasibility (estimate, not real routing)\n"
                        f"airwires: {f['airwires']}  (H {f['h_layer']} / "
                        f"V {f['v_layer']})\n"
                        f"residual same-layer crossings: {f['residual_crossings']}\n"
                        f"≈ vias needed: {f['vias_estimate']} (crossings only)\n"
                        + self._layer_lock_lines(ref_id, graph)
                        + "Note: a coarse H/V Manhattan estimate; the rented "
                        "router (Slice 6) is authoritative."
                    )
                )
            if view == "ratsnest":
                rows = [
                    {"net": w.net, "from": w.a, "to": w.b, "len_mm": f"{w.length:g}"}
                    for w in wires
                ]
                return Response(
                    body=f"# ratsnest — {len(wires)} airwire(s), "
                    f"{ratsnest.total_length(wires):g} mm total "
                    f"({len(graph['instances']) - len(placed)} unplaced part(s) "
                    "excluded)\n"
                    + render_agent_table(rows, schema=["net", "from", "to", "len_mm"])
                )
            xs = ratsnest.crossings(wires)
            rows = [
                {
                    "net_a": a.net,
                    "wire_a": f"{a.a}-{a.b}",
                    "net_b": b.net,
                    "wire_b": f"{b.a}-{b.b}",
                }
                for a, b in xs
            ]
            head = (
                f"# crossings — {len(xs)} (the pre-routing objective; "
                f"plane nets excluded). ratsnest {ratsnest.total_length(wires):g} mm"
            )
            if not xs:
                return Response(body=head + "\n(no crossings — planar so far ✓)")
            return Response(
                body=head
                + "\n"
                + render_agent_table(
                    rows, schema=["net_a", "wire_a", "net_b", "wire_b"]
                )
            )
        if view == "proximity":
            a, b = str(args.get("a") or ""), str(args.get("b") or "")
            if not (a and b):
                raise BadInput(
                    "view='proximity' needs args.a and args.b (refdes)",
                    next="get(kind='pcb', id='slug', view='proximity', "
                    "args={'a':'U1','b':'C1'})",
                )
            try:
                pr = eyes.proximity(graph, a, b)
            except KeyError as exc:
                raise BadInput(str(exc)) from exc
            return Response(body=f"{a} ↔ {b}: {pr['gap_mm']:g} mm (centroid)")
        if view == "trace":
            net = str(args.get("net") or "")
            if not net:
                raise BadInput(
                    "view='trace' needs args.net (a net name)",
                    next="get(kind='pcb', id='slug', view='trace', "
                    "args={'net':'I2C_SCL'})",
                )
            try:
                tr = eyes.trace(graph, net)
            except KeyError as exc:
                raise BadInput(str(exc)) from exc
            path = " → ".join(
                f"{p['net']}" + (f" (via {p['via']})" if p["via"] != "—" else "")
                for p in tr["path"]
            )
            ends = ", ".join(tr["ends"]) or "—"
            return Response(body=f"# trace from {net}\n{path}\nends: {ends}")
        # measures
        measures = self.store.pcb_measures_list(ref_id)
        if not measures:
            return Response(
                body="no measures on this design\n\nNext: add them via "
                "put(kind='pcb', id='slug', args={'measures':[{'metric':"
                "'separation','operands':[{'role':'sensitive'},{'role':'noisy'}],"
                "'goal':10,'strength':'soft','reason':'keep the opamp off the FET'}]})"
            )
        results = eyes.evaluate_measures(
            graph, measures, self.store.pcb_features_list(ref_id)
        )
        rows = [
            {
                "metric": r["metric"],
                "strength": r["strength"],
                "goal": "" if r["goal"] is None else f"{r['goal']:g}",
                "value": "" if r["value"] is None else f"{r['value']:g}",
                "verdict": r["verdict"],
                "reason": (r["reason"] or "")[:40],
                "detail": r.get("detail", ""),
            }
            for r in results
        ]
        schema = ["metric", "strength", "goal", "value", "verdict", "reason"]
        if any(r["detail"] for r in rows):  # e.g. an align the snap pass closed
            schema.append("detail")
        return Response(
            body=f"# measures — {len(results)}\n"
            + render_agent_table(rows, schema=schema)
        )

    # ── exporters ──────────────────────────────────────
    def _export_model(self, ref_id: int) -> dict[str, Any]:
        """The normalised export IR (placement detail + net membership),
        plus ``board_features`` — the instances that are etched copper, not
        parts, which the BOM/CPL writers leave out."""
        model = pcb_export.export_model(
            self.store.pcb_load(ref_id), self.store.pcb_graph(ref_id)
        )
        model["board_features"] = pcb_export.board_feature_refdes(
            model["instances"], self.store.pcb_local_footprints_for(ref_id)
        )
        return model

    def _export_dir(self, slug: str) -> Path:
        """Where artifacts land: ``<PRECIS_CORPUS_DIR>/pcb/<slug>/`` when the
        corpus root is set, else a temp dir. Override per-call with
        args={'dir': '...'}."""
        root = load_config().corpus_dir
        base = Path(root) / "pcb" if root else Path(tempfile.gettempdir())
        out = base / slug
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _outline_from_features(self, ref_id: int) -> list[list[float]] | None:
        """The board outline, as a plain point polygon — the single
        parse point every view/export reads (gerber, board/fab SVG, DRC,
        silk/fiducial furniture) so an ``outline`` feature's optional
        ``corner_radius_mm`` (fillet + polygonize, see
        :func:`precis.pcb.geom.rounded_polygon`) applies exactly once and
        every downstream consumer inherits the rounded corners for free —
        none of them special-case a radius, they just get more points.
        ``corner_radius_mm`` absent or ``<= 0`` returns the sharp-corner
        path unchanged, byte for byte."""
        for f in self.store.pcb_features_list(ref_id):
            geom = f.get("geom") or {}
            if str(f.get("ftype") or "") == "outline" and isinstance(
                geom.get("path"), list
            ):
                path = [[float(p[0]), float(p[1])] for p in geom["path"]]
                radius = geom.get("corner_radius_mm")
                if radius is not None and float(radius) > 0:
                    ring: list[pcb_geom.Point] = [(p[0], p[1]) for p in path]
                    return [
                        [p[0], p[1]]
                        for p in pcb_geom.rounded_polygon(ring, float(radius))
                    ]
                return path
        return None

    def _build_ir(
        self,
        ref_id: int,
        graph: dict[str, Any],
        *,
        with_fixed_copper: bool = False,
        fixed_copper: list[dict[str, Any]] | None = None,
    ) -> pcb_ir.PcbIR:
        """:func:`precis.pcb.session.build_ir` with the board-config
        features the store knows attached — one wrapper so no view can
        build an IR that forgot the mounting holes. A hole missing from
        the IR is invisible to the router's occupancy grid
        (:func:`precis.pcb.realize._claim_mounting_holes`), which is the
        ``npth_clearance`` finding family: measured 2026-09-01 as all
        four nano-fixture corner holes violated, every seed.

        Both footprint caches ride along for the same "no view may build a
        forgetful IR" reason: they are what turn every pin's position from
        a landpattern GUESS into the real footprint's own coordinate
        (:func:`precis.pcb.session.apply_real_pin_offsets`, gripe 338983).
        Threading them here rather than at each view means ``view='drc'``,
        the ratsnest, the SVG previews and the place/route ops all measure
        the same board the gerber writer exports.

        Persisted pin swaps ride along too (``pcb_pin_swaps`` — the route
        job's settled ``PIN_SWAP`` decisions, written back per run). Until
        ``op='route'`` fed ``pin_swap_groups`` (docs/backlog/
        pcb-ewod-multitile.md ruling 6, 2026-09-19) no swap ever
        persisted, so a view that skipped them measured the same board;
        the first real swaps made DRC/gerber/ratsnest disagree with the
        routed copper about which sink pin carries which electrode.

        ``with_fixed_copper`` adds the authored vias the placer keeps solder
        lands off (``pcb_place``'s own hydration) — for a pose check.
        ``fixed_copper`` supplies that list directly instead (a HYPOTHETICAL
        board: ``op='move'`` of a generator group judges the carried copper
        at its new pose before anything is written)."""
        board_id = (graph.get("board") or {}).get("board_id")
        ir = pcb_session.build_ir(
            graph,
            mounting_holes=pcb_session.mounting_holes_from_features(
                self.store.pcb_features_list(ref_id)
            ),
            footprints_by_lcsc=self.store.pcb_footprints_for(ref_id),
            local_footprints_by_name=self.store.pcb_local_footprints_for(ref_id),
            fixed_copper=(
                fixed_copper
                if fixed_copper is not None
                else self.store.pcb_fixed_copper_list(int(board_id))
                if with_fixed_copper and board_id is not None
                else None
            ),
        )
        pcb_session.apply_pin_swap_overrides(ir, self.store.pcb_pin_swaps_list(ref_id))
        return ir

    def _layer_lock_lines(self, ref_id: int, graph: dict[str, Any]) -> str:
        """``view='feasibility'``'s net-class layer-lock section
        (:mod:`precis.pcb.layer_lock`): empty when no class names
        ``"layers"``, so an unlocked board's output is unchanged. The
        crossings estimate above cannot see this constraint at all — on
        ``ewod-dogfood-6`` it printed 0 vias for 55 escapes each forced
        off F.Cu by its own class."""
        class_rules = graph.get("net_classes") or {}
        if not any((r or {}).get("layers") for r in class_rules.values()):
            return ""
        board = graph.get("board") or {}
        ir = self._build_ir(ref_id, graph)
        layers = [str(layer.get("name")) for layer in ir.stackup]
        footprints = pcb_session.footprints_by_refdes(
            ir,
            self.store.pcb_footprints_for(ref_id),
            local_footprints_by_name=self.store.pcb_local_footprints_for(ref_id),
            local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
        )
        pads = pcb_realize.pads_for_ir(ir, layers, footprints)
        board_id = board.get("board_id")
        fixed_copper = (
            self.store.pcb_fixed_copper_list(int(board_id))
            if board_id is not None
            else []
        )
        terminals = pcb_connectivity.fixed_copper_pin_terminals(
            {
                "layers": layers,
                "pads": pads,
                "copper": [
                    {**row, "net": row.get("net") or ""} for row in fixed_copper
                ],
            }
        )
        report = pcb_layer_lock.layer_locked_pins(
            pads,
            {str(n["name"]): n.get("net_class") for n in graph.get("nets") or []},
            class_rules,
            terminals,
        )
        if not report.forced:
            return "net-class layer locks: every locked pin already sits on an allowed layer\n"
        out = (
            f"net-class layer locks: {report.forced} pin(s) sit on a layer "
            "their net class forbids\n"
            f"  {len(report.bridged)} reach an allowed layer through authored "
            "copper (no new via)\n"
            f"  {len(report.stranded)} have no authored copper on an allowed "
            "layer — the router will not place a via at a pad, so these fail "
            "as layer_lock\n"
        )
        if report.stranded:
            shown = ", ".join(f"{r}.{p} ({n})" for n, r, p in report.stranded[:8])
            more = len(report.stranded) - 8
            out += f"  stranded: {shown}" + (f" … +{more}" if more > 0 else "") + "\n"
        return out

    def _furniture_clearance_mm(self, stackup: list[dict[str, Any]]) -> float | None:
        """The clearance :meth:`_board_furniture` hands to
        :func:`precis.pcb.planes.cut_antipads` for a fiducial's antipad
        ring — resolved from :mod:`precis.pcb.capabilities` through
        :func:`precis.pcb.rules.resolve_net_rules`, the SAME resolver
        :meth:`_render_drc` already calls per-net a few lines down from
        its own ``capability_for`` (comment there, verbatim: "an arbitrary
        True is fine here; realize.py is the caller that resolves
        per-layer"). No net-class override applies to a fiducial (it
        carries no net), so this is the fab-floor figure
        ``resolve_net_rules`` falls back to absent one — the SAME floor
        :func:`precis.pcb.realize._pour_planes` folds into the
        ``clearance_mm`` it hands ``plane_pours`` for every OTHER
        antipad on the same pour. One number, one source, read the same
        way by both the realize-time cut and this render-time one.

        ``None`` for a stackup layer count :func:`precis.pcb.drc.
        process_for_stackup` has no capability row for, rather than
        raising: :meth:`_render_gerber`/:meth:`_render_fab_svg` (unlike
        :meth:`_render_drc`) never called ``capability_for`` at all
        before this ring existed, and a board that has not been routed
        yet — so carries no pour, hence nothing for ``cut_antipads`` to
        cut — must still export/preview cleanly on an unsupported layer
        count exactly as it did before. Any board that DOES carry a pour
        already passed this same ``capability_for`` call at REALIZE time
        (:func:`precis.workers.job_types.pcb_route._process_for_stackup`),
        so this can only return ``None`` when there is no pour to cut in
        the first place."""
        capability = self._stackup_capability(stackup)
        if capability is None:
            return None
        return resolve_net_rules(
            "", layer_is_outer=True, fab_caps=capability
        ).clearance_mm

    def _stackup_capability(
        self, stackup: list[dict[str, Any]]
    ) -> CapabilityRow | None:
        """This board's fab-capability row, or ``None`` for a layer count
        :func:`precis.pcb.drc.process_for_stackup` has no row for.

        The row, not one figure off it: :meth:`_board_furniture` needs it
        whole (:func:`precis.pcb.silk.silk_clearance_mm` reads two fields,
        the gerber model a third), and resolving it once here is what
        stops the silk-clearance chain and the soldermask film being
        derived from two different lookups. ``None`` rather than raising,
        for exactly the reason :meth:`_furniture_clearance_mm` documents —
        a board on an unsupported layer count still has to export and
        preview."""
        try:
            return capability_for(pcb_drc.process_for_stackup(stackup))
        except ValueError:
            return None

    def _polarized_refdes(self, design: dict[str, Any]) -> frozenset[str]:
        """The refdes of every instance that carries REAL polarity — the
        ``polarized`` set :func:`~precis.pcb.silk.build_silk` (via
        :meth:`_board_furniture`) uses to decide which R/C/L/FB parts
        still get a pin-1 mark (that module's own docstring, "Not every
        part needs one"). The IR silk.py builds from has refdes but no
        labels, so this determination has to happen here, off the raw
        design's ``instances`` list — the same list :meth:`_board_furniture`
        callers already read for ``instance_sides``.

        An instance is polarized when it declares ``"polarized": true``
        outright, OR its ``label`` matches (case-insensitively) one of
        ELEC/TANT/POL — the abbreviations a real part label uses for
        "electrolytic"/"tantalum"/generic "polarized" (e.g. this module's
        own nano fixture's C1, labelled ``"CAP-ELEC-16V-100uF-THT"``).
        Neither test is authoritative on its own: a label is free text a
        BOM author may not have written consistently, and a design with no
        ``polarized`` flag at all (the common case today) must still infer
        polarity from whatever label it has, rather than mark every R/C
        pin-1-less by default regardless of what the part actually is."""
        out: set[str] = set()
        for i in design.get("instances") or []:
            refdes = str(i.get("refdes") or "")
            if not refdes:
                continue
            if i.get("polarized"):
                out.add(refdes)
                continue
            label = str(i.get("label") or "")
            if _POLARIZED_LABEL_RE.search(label):
                out.add(refdes)
        return frozenset(out)

    def _board_furniture(
        self,
        ir: pcb_ir.PcbIR,
        pads: list[dict[str, Any]],
        *,
        vias: list[dict[str, Any]],
        instance_sides: dict[str, str],
        copper: list[dict[str, Any]],
        outline: list[list[float]] | list[tuple[float, float]] | None,
        layer_names: list[str],
        slug: str,
        clearance_mm: float | None,
        capability: CapabilityRow | None = None,
        date: str | None = None,
        polarized: frozenset[str] = frozenset(),
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, list[dict[str, Any]]],
        list[str],
        tuple[pcb_silk.SilkPlacement, ...],
        list[dict[str, Any]],
    ]:
        """Fiducials, title block and silkscreen, assembled ONCE.

        Returns ``(pads, silk_draws, warnings, census, copper)`` — ``pads``
        with the fiducial flashes folded in (they are ordinary
        ``model["pads"]`` entries, so copper and the swelled mask opening
        fall out of the existing pad pipeline untouched), ``census`` (one
        :class:`~precis.pcb.silk.SilkPlacement` per per-instance courtyard/
        pin-1/refdes item :func:`~precis.pcb.silk.build_silk` considered)
        for a caller wiring ``view='drc'`` (:meth:`_render_drc`) — the
        structured record :func:`precis.pcb.drc.check_silk_missing`/
        :func:`~precis.pcb.drc.check_silk_printability` read, rather than
        this function's own ``warnings`` prose — and ``copper``, which is
        the ``copper`` PARAMETER given back with any plane pour, ON EVERY
        LAYER a fiducial occupies, antipadded around the fiducial discs
        just placed (:func:`precis.pcb.planes.cut_antipads`, called below
        once :func:`~precis.pcb.silk.build_fiducials` has run). A caller
        MUST use this returned ``copper``, not the one it passed in, for
        everything downstream (the gerber/DRC/fab-SVG model's
        ``"copper"`` key) — this function does not mutate the input list
        in place, so keeping the old reference silently un-does the cut
        this exists to make. ``clearance_mm`` is the caller's own
        resolved figure (:meth:`_furniture_clearance_mm`) for that same
        cut — ``None`` only when the board's stackup has no capability
        row at all (an unsupported layer count), which reports as a
        warning and leaves any pour on any layer a fiducial occupies
        un-cut rather than raising, since a board in that state carries no
        pour to cut in the first place (that same docstring's own
        reasoning).

        ``polarized`` is passed straight through to
        :func:`~precis.pcb.silk.build_silk` — the refdes of every instance
        that carries REAL polarity (an electrolytic/tantalum cap, a
        polarized inductor), determined by the caller from the design's
        component flags/labels (this function only sees ``ir``/``pads``,
        never a label). Defaults to empty, same "every pre-2026-09-01
        caller's behaviour is unchanged" contract ``build_silk`` itself
        documents.

        **This exists because there are two render sites** —
        :meth:`_render_gerber` (the fab set a human orders from) and the
        fab SVG (the picture they inspect it with) — and this subsystem's
        recurring defect is one rule implemented at two call sites that
        then drift. A board whose picture shows fiducials the gerbers
        lack is exactly that bug, and it would look like a rendering
        nicety rather than a fab defect.

        **Order is load-bearing, not stylistic — and this paragraph
        describes what the code below actually does, not the reverse.**
        An EARLIER version of this function placed fiducials first (they
        are optical targets a machine is told to look at, so the instinct
        is "they should not have to move for anything else") — but a
        fiducial pinned to the outline's bottom-right corner then blocked
        every candidate in the title block's own margin ladder and
        silently dropped it from the rendered board (found by rendering
        and looking, not by any test; see the comment ahead of the title
        block call below). The title block and the S/N patch are each
        anchored to a specific outline-bbox corner with a short,
        fixed-order fallback ladder (:func:`precis.pcb.silk.
        build_title_block`/:func:`~precis.pcb.silk.build_sn_patch`) — they
        have almost nowhere else to go. Fiducials, by contrast,
        :func:`~precis.pcb.silk.build_fiducials` walks ALL four corners
        and is content with whichever three clear first, so THEY are the
        one board-level feature with real room to move around something
        already committed. The actual order below is therefore: **title
        block first, the S/N patch second** (against the title block,
        which becomes an obstacle for it, not merely an anchor — see the
        comment ahead of that call), **fiducials third** (checked against
        both), **and per-part silk last of all**, against everything the
        first three placed — the only ordering under which every board-
        level feature that CAN move out of the way for another one
        actually does.
        """
        warnings: list[str] = []
        if not outline:
            # Both builders need a bounding box. Say so rather than
            # returning a board that is quietly missing its fab targets.
            warnings.append(
                "no outline feature — skipped fiducials and the title block "
                "(both are placed relative to the board's bounding box)"
            )
            silk_only = pcb_silk.build_silk(
                ir,
                pads,
                vias=vias,
                instance_sides=instance_sides,
                capability=capability,
                polarized=polarized,
            )
            warnings.extend(f"silk: {m}" for m in silk_only.dropped)
            warnings.extend(f"silk: {m}" for m in silk_only.relocated)
            return pads, silk_only.draws, warnings, silk_only.census, copper

        # **Title block FIRST, fiducials second.** Both want a corner of the
        # same bounding box, and only the fiducials can go somewhere else:
        # `build_fiducials` walks a list of corners and takes the first that
        # are clear, while `build_title_block` stacks at ONE corner and is
        # dropped outright if that corner is occupied.
        #
        # Placing fiducials first put one at the outline's bottom-right,
        # which blocked every title-block candidate inside its margin ladder
        # and silently dropped the block from the rendered board — found by
        # rendering and looking, not by any test. `build_title_block`'s own
        # docstring says to place it first; this is that instruction.
        # Routed vias are obstacles for BOTH furniture pieces (silk
        # dodges copper, the module's normal direction): neither builder
        # used to see them, and the S/N patch — the one silk feature
        # that exists to be WRITTEN on — ended up with stitch-via bumps
        # under the Sharpie area (round-3 review item 2).
        via_avoid = pcb_silk.via_obstacles(vias)
        # Part COURTYARDS too (as bbox obstacles): the furniture ladders
        # gained inward rungs (edge-slide, edge-centre — to escape corner
        # mounting hardware), which can now propose spots inside the
        # parts field. The builders only check pads, so a slid title
        # block landed beside C14 on the 40mm stress fixture and cost
        # that part its courtyard silk (clipped below legibility) — the
        # furniture must yield to per-part silk it cannot see.
        # Inflated past the bare courtyard hull (`world_courtyard_rings`
        # is clearance-0): the DRAWN courtyard outline sits a silk
        # clearance further out, and a furniture box that clears the hull
        # but not the stroke still clips that stroke below legibility —
        # exactly the C14 drop above, reproduced at hull-only inflation.
        # At least the slot a refdes label hung above/below the box needs
        # (`refdes_label_slot_mm`): a tighter margin lets furniture settle
        # where every label candidate is blocked -> silk_missing.
        silk_clearance = pcb_silk.silk_clearance_mm(
            capability, stroke_width_mm=pcb_silk.DEFAULT_SILK_WIDTH_MM
        )
        court_margin = max(
            silk_clearance + 1.0, pcb_silk.refdes_label_slot_mm(silk_clearance)
        )
        courtyard_avoid = [
            pcb_silk.obstacle_from_bbox(
                [
                    (
                        min(p[0] for p in ring) - court_margin,
                        min(p[1] for p in ring) - court_margin,
                    ),
                    (
                        max(p[0] for p in ring) + court_margin,
                        min(p[1] for p in ring) - court_margin,
                    ),
                    (
                        max(p[0] for p in ring) + court_margin,
                        max(p[1] for p in ring) + court_margin,
                    ),
                    (
                        min(p[0] for p in ring) - court_margin,
                        max(p[1] for p in ring) + court_margin,
                    ),
                ]
            )
            for ring in pcb_silk.world_courtyard_rings(ir)
            if ring
        ]
        furniture_avoid = courtyard_avoid + via_avoid
        title = pcb_silk.build_title_block(
            outline,
            pads,
            name=slug,
            date=date,
            avoid=furniture_avoid,
            capability=capability,
        )
        warnings.extend(f"title block: {m}" for m in title.dropped)

        # The serial-number patch sits against the title block, so it is
        # placed immediately after it and before anything that would
        # compete for the same corner.
        # The title block is an OBSTACLE for the patch, not merely an anchor.
        # Passing only `title_bbox` positioned the patch relative to the
        # block but left nothing stopping it from overlapping — and a solid
        # silk box painted over the board name erases it, which is worse
        # than either element being dropped.
        sn = pcb_silk.build_sn_patch(
            outline,
            pads,
            title_bbox=title.bbox,
            avoid=(
                [pcb_silk.obstacle_from_bbox(title.bbox)]
                if title.bbox is not None
                else []
            )
            + furniture_avoid,
            capability=capability,
        )
        warnings.extend(f"S/N patch: {m}" for m in sn.dropped)

        # Every copper layer, not just the top one: a fiducial is a
        # whole-stack registration mark — inner layers and the bottom film
        # need the same optical target the top one gets, or a fab has
        # nothing to align them against (this is that fix). Falls back to
        # a single synthetic "F.Cu" only for the no-stackup-known case
        # `build_fiducials` already tolerated before this change.
        fid_layers = list(layer_names) if layer_names else ["F.Cu"]
        # `obstacle_from_bbox` returns a pad-shaped dict, which is exactly
        # what `build_fiducials` checks its candidate corners against — so
        # the block becomes an ordinary obstacle rather than a special case.
        fid_obstacles = list(pads)
        for placed_bbox in (title.bbox, sn.bbox):
            if placed_bbox is not None:
                fid_obstacles.append(pcb_silk.obstacle_from_bbox(placed_bbox))
        # `ir=` feeds the parts-thicket exclusion — the SAME filter the
        # router's pre-claim (`realize._claim_fiducial_keepouts`) applies,
        # off the same IR, so the mint can only ever choose a site the
        # router already reserved copper-free.
        fid = pcb_silk.build_fiducials(outline, fid_obstacles, layers=fid_layers, ir=ir)
        warnings.extend(f"fiducial: {m}" for m in fid.dropped)

        # `FiducialResult.plane_blockers` cut in HERE, at render time, off
        # the already-realized+persisted pours — `planes.plane_pours` ran
        # at REALIZE time, before these fiducials existed, so it could not
        # have antipadded them (see that dataclass's own docstring for the
        # timing gap). `cut_antipads` punches the same no-pour ring
        # `plane_pours` would have, sized off the SAME `clearance_mm` this
        # handler resolves from `capabilities.py` (the caller's own
        # docstring), around each fiducial's MASK opening (the blocker
        # discs are already sized to `mask_dia_mm`, not the bare copper) —
        # ON EVERY LAYER a blocker names (`fid.plane_blockers` now carries
        # all of `fid_layers`, so this one call already cuts a pour on an
        # inner layer or B.Cu exactly as it always cut one on F.Cu; no
        # per-layer loop needed, `cut_antipads` matches a blocker's
        # `layers` list against each pour's own layer internally). Non-pour
        # copper (tracks/vias/pads) passes through untouched.
        pour_items = [c for c in copper if c.get("ctype") == "pour"]
        if fid.plane_blockers and pour_items and clearance_mm is None:
            # No capability row for this stackup (see
            # `_furniture_clearance_mm`'s own docstring) — a board that
            # somehow carries a pour anyway (it should not: pouring one
            # requires the same lookup to have already succeeded at
            # realize time) is reported here rather than crashing a
            # render that otherwise has nothing to do with this cut.
            warnings.append(
                "fiducial: no fab capability row for this stackup — "
                "pour(s) left uncut around the fiducials on every layer"
            )
        elif fid.plane_blockers and pour_items:
            assert clearance_mm is not None  # narrowed by the branch above
            cut_pours = pcb_planes.cut_antipads(
                pour_items, fid.plane_blockers, clearance_mm=clearance_mm
            )
            copper = [c for c in copper if c.get("ctype") != "pour"] + cut_pours
            # The cut is geometry, not a promise — verify the invariant it
            # exists for (a fiducial's mask opening is clear of copper on
            # EVERY layer it occupies, not just one) rather than trusting
            # it silently. The one way this can legitimately still fail:
            # the antipad ring's own area falls under `MIN_FRAGMENT_MM2`
            # (`cut_antipads` reuses the SAME noise floor `plane_pours`
            # uses for a sliver fragment, shared via `_pour_item`), so the
            # hole is filtered out as buffering noise and the pour
            # re-solidifies over the fiducial — a real fiducial-mask-
            # opening-vs-fab-noise-floor collision, not a timing gap this
            # function can no longer close.
            for fx, fy in fid.fiducials:
                still_covered = sorted(
                    {
                        str(p.get("layer") or "")
                        for p in cut_pours
                        if pcb_planes.point_in_pour(p, fx, fy)
                    }
                )
                if still_covered:
                    warnings.append(
                        f"fiducial: antipad ring at ({fx:.2f}, {fy:.2f}) on "
                        f"{', '.join(still_covered)} was too small to survive "
                        f"as a real hole (< {pcb_planes.MIN_FRAGMENT_MM2}mm^2) "
                        "— the pour still covers this fiducial there"
                    )

        pads = pads + fid.pads

        # Everything already placed becomes an obstacle for the per-part
        # labels, on BOTH sides: a fiducial is a copper/mask feature and
        # blocks silk wherever it lands.
        board_obstacles = list(fid.silk_keepouts)
        for placed_bbox in (title.bbox, sn.bbox):
            if placed_bbox is not None:
                board_obstacles.append(pcb_silk.obstacle_from_bbox(placed_bbox))
        reserved = {side: list(board_obstacles) for side in ("top", "bottom")}

        silk_result = pcb_silk.build_silk(
            ir,
            pads,
            vias=vias,
            instance_sides=instance_sides,
            reserved=reserved,
            capability=capability,
            outline=[(float(p[0]), float(p[1])) for p in outline],
            polarized=polarized,
        )
        warnings.extend(f"silk: {m}" for m in silk_result.dropped)
        warnings.extend(f"silk: {m}" for m in silk_result.relocated)

        draws = {side: list(items) for side, items in silk_result.draws.items()}
        draws.setdefault("top", []).extend(title.draws)
        draws.setdefault("top", []).extend(sn.draws)
        return pads, draws, warnings, silk_result.census, copper

    def _drc_pads(self, ref_id: int, layers: list[str]) -> list[dict[str, Any]]:
        """This design's pads, in :mod:`precis.pcb.gerber` model shape.

        Delegates to :func:`precis.pcb.realize.pads_for_ir` — pad geometry
        has exactly one definition, for the reasons that function's
        docstring records. Real per-pin geometry where a part's footprint
        is cached (:meth:`Store.pcb_footprints_for`, remapped onto refdes
        via :func:`precis.pcb.session.footprints_by_refdes` — the join
        ``PcbIR.instance_part_lcsc`` exists for); a package-family
        synthesized bound (``synthesized=True``) everywhere else, same as
        before. This is what makes ``view='gerber'``'s fallback (this
        design has no ``board_pads`` output yet) and ``view='svg'
        args={'level':'fab'}`` both describe the SAME board a fully-cached
        design's real ``export_fab`` call would.
        """
        graph = self.store.pcb_graph(ref_id)
        if not graph.get("nets"):
            return []
        ir = self._build_ir(ref_id, graph)
        footprints = pcb_session.footprints_by_refdes(
            ir,
            self.store.pcb_footprints_for(ref_id),
            local_footprints_by_name=self.store.pcb_local_footprints_for(ref_id),
            local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
        )
        return pcb_realize.pads_for_ir(ir, layers, footprints)

    def _drc_geometry(
        self, ref_id: int, layers: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, list[tuple[float, float]]]]:
        """DRC's pads (the same IR-sourced geometry :meth:`_drc_pads`
        builds, off ONE ``build_ir`` call here rather than two independent
        ones) plus each instance's courtyard POLYGON in local-frame
        coordinates, keyed by refdes.

        **The shape is not computed here, and since 2026-08-30 there is
        only one of it.** :func:`precis.pcb.ir.instance_courtyard_polygon`
        — the convex hull of the part's own pad outlines, offset outward —
        is what the placer reserves
        (``OptimizeEngine._keepout_poly``/``seed_placement``), what
        ``courtyard_overlap`` and ``outline_containment`` check, and what
        :mod:`precis.pcb.silk` draws. Three consumers, one definition,
        which is the whole point of ``docs/backlog/
        pcb-courtyard-polygon.md``.

        **The three differ only in the CLEARANCE they offset by, and that
        is a real difference, not drift.** The placer and this view use
        :data:`~precis.pcb.ir.COURTYARD_CLEARANCE_MM` — how much room a router
        needs to escape between two land patterns. Silk uses
        :func:`precis.pcb.silk.silk_clearance_mm`, walked down the fab
        chain — how far ink must sit from a mask opening it must not
        touch. Different questions against different standards, answered
        at different points in the pipeline; what must NOT differ is the
        shape they are offsets of.

        Before this, a circle stood in for all of it, and a flat
        :data:`~precis.pcb.drc.DEFAULT_COURTYARD_RADIUS_MM` before that
        made ``courtyard_overlap`` structurally dormant: smaller than any
        real part's keep-out, so placement always separated parts further
        than the check would ever flag — a TO-220's several-millimetre
        courtyard checked against nothing."""
        graph = self.store.pcb_graph(ref_id)
        ir = self._build_ir(ref_id, graph)
        footprints = pcb_session.footprints_by_refdes(
            ir,
            self.store.pcb_footprints_for(ref_id),
            local_footprints_by_name=self.store.pcb_local_footprints_for(ref_id),
            local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
        )
        pads = (
            pcb_realize.pads_for_ir(ir, layers, footprints) if graph.get("nets") else []
        )
        polys = pcb_ir.instance_courtyard_polygons(
            ir,
            clearance_mm=pcb_ir.COURTYARD_CLEARANCE_MM,
            fallback_half_extent_mm=pcb_cost.COURTYARD_MIN_SEPARATION_MM / 2.0,
        )
        return pads, {
            str(ir.instance_refdes[i]): polys[i] for i in range(ir.n_instances)
        }

    def _drc_drills(self, ref_id: int) -> list[dict[str, Any]]:
        """``model['drills']`` for :func:`precis.pcb.drc.
        check_npth_clearance` — ``mounting_hole`` features, the mechanical
        holes that check exists to protect (a screw hole is never a
        soldered lead, so it is unplated by construction). Before this,
        ``_render_drc`` never populated ``drills`` at all, so
        ``check_npth_clearance`` — wired into ``run_geometric_drc`` and
        reading ``model.get('drills')`` — was structurally incapable of
        firing regardless of design content, on every board, every seed.

        Field names match BOTH of ``model['drills']``'s existing consumers
        verbatim — :func:`precis.pcb.drc.check_npth_clearance`'s own
        ``{"x","y","dia_mm","plated"}`` reads and
        :func:`precis.pcb.gerber.excellon_files` /
        :func:`precis.pcb.padplace.place_footprint_pads`'s identical
        shape for a through-hole component pad's drill — so this never
        invents a third spelling.

        **A through-hole component pad's own drill is NOT included here**:
        DRC's pad source (:func:`precis.pcb.realize.pads_for_ir`, the IR
        path this view deliberately uses so pads and connectivity share
        ONE source — see :meth:`_render_drc`'s own comment) carries no
        drill/through-hole concept at all yet. Reaching into
        :mod:`precis.pcb.padplace`'s cached-footprint pad source instead
        would reintroduce exactly the "one rule, two call sites, drifted"
        pad-source split that comment already warns against — a distinct,
        deferred gap that belongs in ``realize.py``, not patched around
        here."""
        out: list[dict[str, Any]] = []
        for f in self.store.pcb_features_list(ref_id):
            if str(f.get("ftype") or "") != "mounting_hole":
                continue
            geom = f.get("geom") or {}
            dia = geom.get("diameter") or geom.get("d")
            x, y = f.get("x"), f.get("y")
            if dia is None or x is None or y is None:
                continue
            # ``plated`` now comes from the feature: a solder-on nut's
            # hole is plated (its ring is pad copper — emitted by
            # ``pads_for_ir`` off ``ir.mounting_holes`` — so clearance
            # rules cover it), and ``check_npth_clearance`` correctly
            # skips it; a bare screw hole stays the unplated default
            # that check exists to protect.
            out.append(
                {
                    "x": float(x),
                    "y": float(y),
                    "dia_mm": float(dia),
                    "plated": bool(geom.get("plated")),
                }
            )
        return out

    def _mask_open_regions(self, ref_id: int) -> list[dict[str, Any]]:
        """``ftype='mask_open'`` features -> :mod:`precis.pcb.gerber`'s
        ``model["mask_open_regions"]`` shape (pcb-ewod-multitile Slice 1):
        one field-wide soldermask opening covering pads AND gaps, instead
        of a per-pad swelled opening with a mask dam between every
        neighbour — the shape a tight electrode field cannot fab any other
        way. ``layer`` doubles as the region's SIDE ('top'/'bottom') —
        the same free-text column an ``outline``/``mounting_hole`` feature
        already carries a purpose-specific meaning in, not a new column.
        A feature missing a usable ``geom.polygon`` (or a ``layer`` other
        than top/bottom) is skipped, not fatal — a malformed one region
        should not blank a whole gerber export."""
        out: list[dict[str, Any]] = []
        for f in self.store.pcb_features_list(ref_id):
            if str(f.get("ftype") or "") != "mask_open":
                continue
            side = str(f.get("layer") or "").strip().lower()
            if side not in ("top", "bottom"):
                continue
            geom = f.get("geom") or {}
            poly = geom.get("polygon")
            if not isinstance(poly, list) or len(poly) < 3:
                continue
            out.append(
                {
                    "side": side,
                    "polygon": [[float(p[0]), float(p[1])] for p in poly],
                }
            )
        return out

    def _render_export(self, ref_id: int, view: str, args: dict[str, Any]) -> Response:
        """Write a fab artifact (BOM / CPL / KiCad netlist / Specctra DSN /
        mechanical profile / the gerber bundle) off the IR. Pure — no binary
        needed; the file lands under the corpus (or a temp dir)."""
        if view == "gerber":
            return self._render_gerber(ref_id, args)
        if view == "epro":
            return self._render_epro(ref_id, args)
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        model = self._export_model(ref_id)
        raw_dir = args.get("dir")
        out_dir = Path(str(raw_dir)).expanduser() if raw_dir else self._export_dir(slug)
        out_dir.mkdir(parents=True, exist_ok=True)

        # one table per view — extension + builder together, so they can't
        # drift apart (was an ext map + a parallel if/elif chain).
        builders: dict[str, tuple[str, Callable[[], str]]] = {
            "bom": ("csv", lambda: pcb_export.bom_csv(model)),
            "cpl": ("csv", lambda: pcb_export.cpl_csv(model)),
            "netlist": ("net", lambda: pcb_export.kicad_netlist(model, name=slug)),
            "dsn": (
                "dsn",
                lambda: pcb_export.specctra_dsn(
                    model,
                    footprints=self.store.pcb_footprints_for(ref_id),
                    outline=self._outline_from_features(ref_id),
                    name=slug,
                ),
            ),
            "mechanical": (
                "json",
                lambda: json.dumps(
                    pcb_export.mechanical_profile(
                        model, self.store.pcb_features_list(ref_id)
                    ),
                    indent=2,
                ),
            ),
        }
        ext, build = builders[view]
        content = build()

        # BOM and CPL are both .csv: a shared `{slug}.csv` let one export
        # silently overwrite the other in the same directory.
        stem = f"{slug}-{view}" if view in ("bom", "cpl") else slug
        path = out_dir / f"{stem}.{ext}"
        path.write_text(content, encoding="utf-8")
        warns = self._export_warnings(model, view)
        head = f"# exported {slug} → {view.upper()}\n{path}  ({len(content):,} bytes)"
        if warns:
            head += "\n" + "\n".join(f"⚠️  {w}" for w in warns)
        # Echo a short preview so the agent sees the shape without re-reading.
        preview = "\n".join(content.splitlines()[:12])
        if view == "bom":
            # Not a column of the file (JLCPCB's BOM upload has fixed
            # headers): the datasheet state of each C-number, beside it.
            head += self._datasheet_status_block(ref_id)
        return Response(body=head + "\n\n```\n" + preview + "\n```")

    def _datasheet_status_block(self, ref_id: int) -> str:
        rows = [
            f"  {lcsc}: "
            + pcb_datasheets.status_line(
                pcb_datasheets.datasheet_state(self.store, lcsc)
            )
            for lcsc in pcb_datasheets.live_lcscs(self.store, ref_id)
        ]
        return ("\ndatasheets:\n" + "\n".join(rows)) if rows else ""

    def _export_warnings(self, model: dict[str, Any], view: str) -> list[str]:
        out = []
        if view in ("cpl", "dsn"):  # route never reaches here (_render_route)
            up = pcb_export.unplaced(model)
            if up:
                out.append(
                    f"{len(up)} unplaced part(s) excluded: {', '.join(up[:8])}"
                    + ("…" if len(up) > 8 else "")
                    + " — run autoplace first"
                )
        if view in ("bom", "cpl"):
            ml = pcb_export.missing_lcsc(model)
            if ml:
                out.append(
                    f"{len(ml)} part(s) without an LCSC number "
                    f"(not JLCPCB-assemblable): {', '.join(ml[:8])}"
                    + ("…" if len(ml) > 8 else "")
                )
        return out

    def _fab_model(
        self, ref_id: int, *, slug: str
    ) -> tuple[dict[str, Any], list[str]] | None:
        """The geometry-complete board model + its assembly warnings — the
        ONE assembly of ``{layers, outline, copper, pads, drills,
        silkscreen, soldermask_expansion_mm, mask_open_regions,
        instances}``. ``None`` when the design has no board row yet.

        Unlike every other ``_EXPORT_VIEWS`` entry this does NOT read
        ``_export_model`` (that IR has no realized copper or pad
        geometry): copper comes straight off ``pcb_copper`` (the
        realizer's own output shape, see :mod:`precis.pcb.realize`), pads
        are newly placed by :mod:`precis.pcb.padplace` off each instance's
        pose + its cached footprint, and the silkscreen is GENERATED
        (:mod:`precis.pcb.silk` — refdes labels, courtyard outlines, pin-1
        ticks, built off the same IR and checked against these same pads
        AND this same realized copper's vias, so nothing prints where a
        fab would scrape it off).

        **Extracted so every geometry consumer reads one assembly.**
        ``view='gerber'`` was the only caller when this lived inline; a
        second consumer that re-derived pads, synthesized-pad fill-in,
        mounting-hole drills and the outline fallback would be the same
        "one rule, N call sites, drifted" defect
        :func:`precis.pcb.realize.pads_for_ir` and
        :func:`precis.pcb.padplace.pad_label` each already record."""
        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is None:
            return None
        board_id = int(board["board_id"])
        layer_names = [str(layer.get("name")) for layer in board["stackup"]]
        copper = self.store.pcb_copper_list(board_id)

        warnings: list[str] = []
        outline = self._outline_from_features(ref_id)
        if outline is None:
            x0, y0, x1, y1 = pcb_export.board_bbox({"instances": design["instances"]})
            outline = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            warnings.append(
                "no 'outline' feature — the board edge is the placed-parts "
                "bounding box, not a real board outline"
            )
        if not copper:
            warnings.append(
                "no realized copper yet — run put(args={'op':'route'}) first, "
                "or the gerbers will carry pads/outline/drills only"
            )

        footprints = self.store.pcb_footprints_for(ref_id)
        local_footprints = self.store.pcb_local_footprints_for(ref_id)
        graph = self.store.pcb_graph(ref_id)
        ir = self._build_ir(ref_id, graph)
        pin_to_net = {
            (m["refdes"], m["pin"]): net["name"]
            for net in graph["nets"]
            for m in net["members"]
        }
        pads, drills = padplace.board_pads(
            design["instances"],
            footprints,
            layers=layer_names,
            pin_to_net=pin_to_net,
            local_footprints=local_footprints,
        )
        # Mounting-hole drills belong in the SAME Excellon set as the
        # component drills — without these the fab bundle carried a
        # solder-nut's copper ring with NO hole through it (measured on
        # the round-3 nano render: 43 via drills, zero mounting drills).
        # Same {"x","y","dia_mm","plated"} shape as `_drc_drills`.
        drills = drills + [
            {"x": h.x, "y": h.y, "dia_mm": h.drill_mm, "plated": h.plated}
            for h in ir.mounting_holes
        ]
        placed = [
            i
            for i in design["instances"]
            if i.get("x") is not None and i.get("y") is not None
        ]
        has_pads = {
            i["refdes"]
            for i in placed
            if (footprints.get(str(i.get("part_lcsc") or "")) or {}).get("pads")
            or (
                not i.get("part_lcsc")
                and (local_footprints.get(str(i.get("footprint") or "")) or {}).get(
                    "pads"
                )
            )
        }
        missing = sorted({i["refdes"] for i in placed} - has_pads)
        if missing:
            warnings.append(
                f"{len(missing)} placed part(s) have no cached footprint — pads "
                "fall back to SYNTHESIZED land patterns (bounds, not the real "
                f"part): {', '.join(missing[:8])}"
                + ("…" if len(missing) > 8 else "")
                + " (fetch via precis.pcb.footprint first)"
            )
        # `board_pads` (module docstring, verbatim) "contributes nothing"
        # for an instance with no cached footprint, and — per PIN — nothing
        # for a design pin whose name never joins the cached footprint's
        # `pin_map` (gr346009: a pre-symbol-name sink wired OUT0..63 against
        # a cache row naming its pins differently). Left as-is either gap
        # silently dropped those pads from the fab set ENTIRELY rather than
        # marking them synthesized: the board came out looking clean
        # because export_fab's synthesized-pad check saw NOTHING to refuse,
        # not because the geometry was real. So coverage is decided per
        # placed PIN, never per instance: every placed pin `board_pads` did
        # not produce is filled in from the SAME synthesized-or-real merge
        # point `_drc_pads`/DRC already uses (`pads_for_ir` ->
        # `pad_geometry`), so those pads carry `synthesized: True` and
        # export_fab's refusal actually fires.
        covered = {(str(p.get("refdes")), str(p.get("pin"))) for p in pads}
        footprints_by_refdes = pcb_session.footprints_by_refdes(
            ir,
            footprints,
            local_footprints_by_name=local_footprints,
            local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
        )
        ir_pads = pcb_realize.pads_for_ir(ir, layer_names, footprints_by_refdes)
        unjoined: dict[str, int] = {}
        filled: list[dict[str, Any]] = []
        for pad in ir_pads:
            key = (str(pad["refdes"]), str(pad["pin"]))
            if key in covered:
                continue
            filled.append(pad)
            if key[0] not in missing:
                unjoined[key[0]] = unjoined.get(key[0], 0) + 1
        pads = pads + filled
        if unjoined:
            named = sorted(unjoined)
            warnings.append(
                f"{sum(unjoined.values())} placed pin(s) of {len(unjoined)} "
                "part(s) have a cached footprint whose pin names do not join "
                "the design's — those pads are SYNTHESIZED (bounds, not the "
                "real part): "
                + ", ".join(f"{r} ({unjoined[r]})" for r in named[:8])
                + ("…" if len(named) > 8 else "")
                + " (rename the pins to the footprint's pin_map names)"
            )
        if not pads:
            # No placed instance contributed a pad at all (nothing above
            # could have added one either — `missing` would itself be
            # empty in that case). Kept as a last-resort safety net, same
            # as before: fall back to the SAME land patterns the router
            # routed to, so the fab set describes the board the engine
            # actually built, and let export_fab refuse to call it
            # manufacturable.
            pads = self._drc_pads(ref_id, layer_names)

        instance_sides = {
            str(i.get("refdes")): str(i.get("layer") or "top")
            for i in design["instances"]
        }
        vias = [c for c in copper if c.get("ctype") == "via"]
        capability = self._stackup_capability(board["stackup"])
        pads, silk_draws, furniture_warnings, _silk_census, copper = (
            self._board_furniture(
                ir,
                pads,
                vias=vias,
                instance_sides=instance_sides,
                polarized=self._polarized_refdes(design),
                copper=copper,
                outline=outline,
                layer_names=layer_names,
                slug=slug,
                clearance_mm=self._furniture_clearance_mm(board["stackup"]),
                capability=capability,
                date=_export_date(),
            )
        )
        warnings.extend(furniture_warnings)

        model: dict[str, Any] = {
            "layers": layer_names,
            "outline": outline,
            "copper": copper,
            "pads": pads,
            "drills": drills,
            "silkscreen": silk_draws,
            # The SAME row the silk above was placed against — a mask film
            # drawn with one expansion under silk cleared for another is two
            # numbers for one physical edge (`soldermask_gerber`).
            "soldermask_expansion_mm": pcb_silk.soldermask_expansion_mm(capability),
            "mask_open_regions": self._mask_open_regions(ref_id),
            # Placed instance rows — the pose/refdes/footprint half a
            # component-oriented writer needs to group these flat pads back
            # into parts. :mod:`precis.pcb.gerber` ignores keys it does not
            # know (its model docstring is the contract), so this is
            # additive for the fab path.
            "instances": design["instances"],
        }
        return model, warnings

    def _render_gerber(self, ref_id: int, args: dict[str, Any]) -> Response:
        """Write the manufacturable fab bundle — gerbers + Excellon, zipped
        (:mod:`precis.pcb.gerber`) — off :meth:`_fab_model`, closing
        ``docs/backlog/pcb-fab-output-unwired.md``'s "the export tail is
        unwired" gap."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        built = self._fab_model(ref_id, slug=slug)
        if built is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        model, warnings = built
        pads, drills, copper = model["pads"], model["drills"], model["copper"]
        try:
            files = pcb_gerber.export_fab(model, name=slug)
        except pcb_gerber.SynthesizedPadError as exc:
            raise BadInput(
                f"pcb: {exc} Use view='svg' args={{'level':'fab'}} to inspect "
                "the board as gerbers without exporting it."
            ) from exc
        blob = pcb_gerber.zip_fab(files)

        raw_dir = args.get("dir")
        out_dir = Path(str(raw_dir)).expanduser() if raw_dir else self._export_dir(slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{slug}-fab.zip"
        path.write_bytes(blob)
        banner, _n_drc_errors = self._gerber_drc_banner(ref_id, slug)

        head = (
            f"{banner}\n\n"
            f"# exported {slug} → GERBER (fab bundle)\n{path}  "
            f"({len(blob):,} bytes zipped, {len(files)} file(s))\n"
            f"pads: {len(pads)}  drills: {len(drills)}  via holes: "
            f"{sum(1 for c in copper if c.get('ctype') == 'via')}  "
            f"copper item(s): {len(copper)}"
        )
        if warnings:
            head += "\n" + "\n".join(f"⚠️  {w}" for w in warnings)
        listing = "\n".join(sorted(files))
        return Response(body=head + "\n\n```\n" + listing + "\n```")

    def _render_epro(self, ref_id: int, args: dict[str, Any]) -> Response:
        """Write an EasyEDA Pro ``.epro2`` (:mod:`precis.pcb.epro_write`)
        off :meth:`_fab_model` — parts, pads, nets and the outline, so a
        colleague can open the board and keep working. Refuses synthesized
        pads unless ``args={'allow_synthesized': true}``."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        built = self._fab_model(ref_id, slug=slug)
        if built is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        model, warnings = built
        try:
            exported = pcb_epro_write.epro_files(
                model,
                slug=slug,
                allow_synthesized=bool(args.get("allow_synthesized")),
            )
        except pcb_gerber.SynthesizedPadError as exc:
            raise BadInput(
                f"pcb: {exc} Use view='svg' args={{'level':'fab'}} to inspect "
                "the board without exporting it."
            ) from exc
        except ValueError as exc:  # a name the record separator cannot carry
            raise BadInput(f"pcb: cannot write .epro2: {exc}") from exc
        blob = pcb_epro_write.zip_epro(exported.files)

        raw_dir = args.get("dir")
        out_dir = Path(str(raw_dir)).expanduser() if raw_dir else self._export_dir(slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{slug}.epro2"
        path.write_bytes(blob)

        st = exported.stats
        head = (
            f"# exported {slug} → EPRO2 (EasyEDA Pro)\n{path}  "
            f"({len(blob):,} bytes zipped)\n"
            f"components: {st['components']}  pads: {st['pads']}  "
            f"nets: {st['nets']}\n"
            "No schematic is included, on purpose: do NOT run 'Update PCB from "
            "schematic' in Pro — it would rewrite the netlist and destroy the "
            "board.\n"
            "Copper (tracks, vias, pours) is NOT exported yet (slice 2c): the "
            "file carries the outline, parts, pads and nets only.\n"
            # Remove in the commit that records a human opening one in Pro
            # (docs/backlog/pcb-epro-export.md, 2b acceptance).
            "UNVERIFIED: no file from this writer has been opened in EasyEDA "
            "Pro yet; check part positions (bottom side especially) before "
            "relying on it."
        )
        # `_fab_model` speaks for the gerber bundle: its silk-placement notes
        # (one per relocated label, ~170 on a 140-part board — they pushed
        # this response past the frame) and its "route first" hint are about
        # artifacts this file does not carry. Count the silk ones, drop both.
        silk_notes = [w for w in warnings if w.startswith("silk: ")]
        fab_notes = [
            w
            for w in warnings
            if not w.startswith("silk: ") and not w.startswith("no realized copper yet")
        ]
        if silk_notes:
            fab_notes.append(
                f"{len(silk_notes)} silk-placement note(s) not shown: silk is not "
                "in this file (view='gerber' lists them)"
            )
        notes = [*exported.warnings, *fab_notes]
        if notes:
            head += "\n" + "\n".join(f"⚠️  {w}" for w in notes)
        listing = "\n".join(sorted(exported.files))
        return Response(body=head + "\n\n```\n" + listing + "\n```")

    def _render_route(self, ref_id: int, args: dict[str, Any]) -> Response:
        """The §9 place↔route round-trip via Freerouting headless. Re-places
        (escalating annealing) and re-routes until the route completes or
        ``max_passes`` is hit. Degrades to a single ``.dsn``-only pass when no
        router is installed (the gate is at this step only)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        # max(1,…): '0' is truthy, and 0 passes would report a .dsn that was
        # never written (the write lives inside the pass loop).
        max_passes = max(1, int(args.get("max_passes") or 3))
        base_iters = int(args.get("iters") or 1500)
        raw_dir = args.get("dir")
        out_dir = Path(str(raw_dir)).expanduser() if raw_dir else self._export_dir(slug)

        measures = self.store.pcb_measures_list(ref_id)

        def place_fn(iters: int, seed: int) -> dict[str, Any]:
            res, _moved = self._place_and_store(
                ref_id, iters=iters, seed=seed, measures=measures
            )
            return {"crossings_after": res.crossings_after}

        footprints = self.store.pcb_footprints_for(ref_id)
        outline = self._outline_from_features(ref_id)

        def dsn_fn(model: dict[str, Any]) -> str:
            return pcb_export.specctra_dsn(
                model, footprints=footprints, outline=outline, name=slug
            )

        rt = pcb_route.place_route_round_trip(
            lambda: self._export_model(ref_id),
            place_fn,
            dsn_fn,
            out_dir,
            max_passes=max_passes,
            base_iters=base_iters,
            name=slug,
        )
        rows = [
            {
                "pass": h["pass"],
                "iters": h["iters"],
                "crossings": ""
                if h["crossings_after"] is None
                else h["crossings_after"],
                "routed": "✓" if h["routed_ok"] else ("skip" if h["skipped"] else "✗"),
                "unrouted": "" if h["unrouted"] is None else h["unrouted"],
            }
            for h in rt.history
        ]
        if rt.route.skipped:
            tail = (
                "No Freerouting backend — emitted the .dsn only. Install it and "
                "set PRECIS_FREEROUTING_JAR, then re-run view='route'. The .dsn "
                "also opens in the EasyEDA/KiCad router as a manual escape hatch."
            )
        elif rt.ok:
            tail = (
                f"Routed ✓ → {rt.route.ses}\n"
                "Next: import the .ses into KiCad and run kicad-cli for gerbers + "
                "the BOM/CPL (view='bom', view='cpl') to order at JLCPCB."
            )
        else:
            tail = (
                f"Route incomplete after {rt.passes} pass(es) "
                f"({rt.route.unrouted} unrouted). Add area / relax density / pin "
                "more parts and re-run, or open the .dsn in a manual router.\n"
                f"log: {rt.route.log_tail[-400:]}"
            )
        head = f"# route {slug} — {rt.passes} pass(es), {'ok' if rt.ok else 'not complete'}\n{rt.dsn}"
        return Response(
            body=head
            + "\n"
            + render_agent_table(
                rows, schema=["pass", "iters", "crossings", "routed", "unrouted"]
            )
            + "\n"
            + tail
        )

    # ── route status (pcb-guided-place-route Slice 1) ──────────────────
    def _render_route_status(self, ref_id: int) -> Response:
        """The per-net route status table — a net with no ``pcb_routes`` row
        reads as ``unrouted`` (the default state before ``op='route'`` has
        ever run against it, not a missing one). A row's ``note`` (e.g. a
        dangling <2-member net's ``'realized'`` exemption — see
        ``pcb_route``'s job docstring) is appended so it never reads as an
        actually-routed net."""
        rows = self.store.pcb_route_status(ref_id)
        if not rows:
            return Response(body="no nets on this design yet")
        counts: dict[str, int] = {}
        for r in rows:
            key = route_summary_status(r["status"], r.get("note"))
            counts[key] = counts.get(key, 0) + 1
        table_rows = [
            {
                "net": r["name"],
                "class": r["net_class"] or "—",
                "domain": r["domain"] or "electrical",
                "status": r["status"] + (f" ({r['note']})" if r.get("note") else ""),
            }
            for r in rows
        ]
        return Response(
            body=f"# route status — {format_route_summary(counts)}\n"
            + render_agent_table(
                table_rows, schema=["net", "class", "domain", "status"]
            )
        )

    def _render_congestion(self, ref_id: int) -> Response:
        """The latest ``op='route'`` run's congestion digest — per-gap
        capacity warnings stamped onto ``refs.meta.last_route`` by the
        ``pcb_route`` job (backlog acceptance criterion: a routing failure
        must name the blocking gap, the participants, and the clearance
        arithmetic, not just report "unrouted")."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        if ref is None:
            raise NotFound(f"pcb id={ref_id} not found")
        last_route = (ref.meta or {}).get("last_route")
        if not last_route:
            return Response(
                body="no route run yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'op':'route'}) then re-check this view."
            )
        warnings = last_route.get("warnings") or []
        head = (
            f"# congestion — last route: {last_route.get('realized', 0)} realized, "
            f"{last_route.get('failed', 0)} failed, {len(warnings)} gap warning(s)"
        )
        ripped = int(last_route.get("ripped") or 0)
        if ripped:
            # The digest outlived its copper (op='rip' since the run): say
            # what is stored now instead of passing the old run off as it.
            status = collections.Counter(
                str(r["status"]) for r in self.store.pcb_route_status(ref_id)
            )
            head += (
                f"\n⚠️ STALE — {ripped} net(s) ripped since that run; stored now: "
                f"{status.get('realized', 0)} realized, "
                f"{status.get('unrouted', 0)} unrouted (view='route-status')"
            )
            return Response(body=head + "".join(f"\n- {w}" for w in warnings))
        if not warnings:
            # No tick under failed nets: "no over-capacity gaps" only rules
            # out ONE cause, and read beside "40 failed" it reads as a pass.
            if int(last_route.get("failed") or 0):
                return Response(
                    body=head + "\n(no over-capacity gaps — the failures have "
                    "another cause; per-net status: view='route-status')"
                )
            return Response(body=head + "\n(no over-capacity gaps ✓)")
        return Response(body=head + "\n" + "\n".join(f"- {w}" for w in warnings))

    def _render_planes(self, ref_id: int) -> Response:
        """Plane assignments (``pcb_planes``) — which nets are
        plane-served on which layer, so the LLM can see the
        fanout-vs-route split before running ``op='route'``. Shows
        ``source`` so a reader can tell a human's explicit
        ``op='plane_net'`` instruction (``authored``) apart from the
        anneal's own settled decision (``derived``, written back by the
        ``pcb_route`` job, gr267526) — an authored row is never touched
        by a later route run; a derived one is replaced each run."""
        rows = self.store.pcb_planes_list(ref_id)
        if not rows:
            return Response(
                body="no plane assignments yet\n\nNext: put(kind='pcb', "
                "id='slug', args={'op':'plane_net','layer':'In1.Cu',"
                "'net':'GND'})"
            )
        table_rows = [
            {"layer": r["layer"], "net": r["net"], "source": r["source"]} for r in rows
        ]
        return Response(
            body=f"# planes — {len(rows)} assignment(s)\n"
            + render_agent_table(table_rows, schema=["layer", "net", "source"])
        )

    def _render_footprints_view(self, ref_id: int) -> Response:
        """gr341532 fix 3 — one row per catalog-part instance (``part``/
        ``part_lcsc`` set), showing whether its ``part_footprints`` cache
        row exists: the read-side counterpart to ``op='footprint'`` and
        the gap the ``synthesized_footprint`` DRC finding
        (:func:`precis.pcb.drc.check_synthesized_footprint`) points at.
        Instances that name a design-local ``footprint=`` (authored copper
        with no LCSC C-number) are out of scope here by construction —
        they carry no ``part_lcsc`` and can never be "synthesized" in
        this sense."""
        design = self.store.pcb_load(ref_id)
        catalog = [i for i in design["instances"] if i.get("part_lcsc")]
        if not catalog:
            return Response(
                body="no catalog-part instances on this board (every "
                "component is a design-local footprint, or has neither)"
            )
        rows_by_lcsc = {
            lcsc: self.store.part_footprint_get(lcsc)
            for lcsc in sorted({str(i["part_lcsc"]).strip().upper() for i in catalog})
        }
        # Per PIN, not per cache row: a cached footprint whose pin names do
        # not join the design's still leaves those pins synthesized
        # (gr346009 — dogfood-1 read "synthesized: no" with 56 synthesized
        # pins). Same merge point DRC and the gerber view use.
        graph = self.store.pcb_graph(ref_id)
        ir = self._build_ir(ref_id, graph)
        footprints = pcb_session.footprints_by_refdes(
            ir, self.store.pcb_footprints_for(ref_id)
        )
        geoms = pcb_realize.pad_geometry(ir, footprints)
        mismatch_note = pcb_session.pin_name_mismatch_note(
            pcb_session.pin_name_mismatches(ir, footprints)
        )
        synth_by_refdes: dict[str, list[int]] = {}
        for pid, geom in enumerate(geoms):
            refdes = str(ir.instance_refdes[int(ir.pin_instance[pid])])
            tally = synth_by_refdes.setdefault(refdes, [0, 0])
            tally[1] += 1
            if geom.synthesized:
                tally[0] += 1
        rows = []
        n_cached = 0
        for i in sorted(catalog, key=lambda x: str(x["refdes"])):
            lcsc = str(i["part_lcsc"]).strip().upper()
            row = rows_by_lcsc.get(lcsc)
            if row is not None:
                n_cached += 1
            summary = self._footprint_summary_row(lcsc, row, error=None)
            n_synth, n_pins = synth_by_refdes.get(str(i["refdes"]), [0, 0])
            if row is None:
                synthesized = "yes"
            elif n_synth == 0:
                synthesized = "no"
            else:
                synthesized = f"{n_synth}/{n_pins} pins"
            rows.append(
                {
                    "refdes": str(i["refdes"]),
                    "lcsc": lcsc,
                    "cached": summary["cached"],
                    "source": summary["source"],
                    "n_pads": summary["n_pads"],
                    "synthesized": synthesized,
                }
            )
        head = (
            f"# footprints — {n_cached}/{len(catalog)} catalog-part instance(s) cached"
        )
        if n_cached < len(catalog):
            head += (
                "\n\nNext: put(kind='pcb', id='slug', args={'op':'footprint',"
                "'part':'<C-number>'}) for each uncached part"
            )
        if mismatch_note:
            head += "\n\n" + mismatch_note
        return Response(
            body=head
            + "\n"
            + render_agent_table(
                rows,
                schema=["refdes", "lcsc", "cached", "source", "n_pads", "synthesized"],
            )
        )

    def _render_drc(self, ref_id: int) -> Response:
        """Geometric DRC (pcb-guided-place-route Slice 8, :mod:`precis.pcb.
        drc`) — the L5 check. Superseded ``eyes.drc_lite`` (graph-shape
        sanity only, no geometry); the graph-feasibility half of DRC
        (``ir.py``, L0-L4) stays inside the optimizer, not this view.
        Every call is itself a DRC "run" — findings are persisted to
        ``pcb_drc_findings`` under a fresh ``run_id`` so
        ``netlist_drc_clean`` and a human reviewer can both read the same
        durable record afterward.

        **Runs whenever there is REAL pad geometry, not only after a
        route (round 4, docs/backlog/pcb-ewod-multitile.md's decisions
        log).** Realized copper (``pcb_copper``, once ``op='route'`` has
        run) is checked when present; a board with real (non-synthesized)
        pads but no realized copper yet still gets a full pads-only pass
        (courtyard, clearance, keep-outs, annular ring, unrouted-net
        status — everything that doesn't need router output) rather than
        the old blanket "no realized copper yet" bail, which made
        ``view='drc'`` structurally unable to ever check a board whose
        every net is fanout-1 (an ``ewod_pad_array`` standalone board is
        the motivating case: its escape geometry is footprint-pad copper,
        never something a router touches, so it NEVER got a
        ``pcb_copper`` row regardless of whether ``op='route'`` ran). The
        response states the reduced scope explicitly
        (``(pads-only DRC — no routed copper yet)``) so a clean pads-only
        pass is never misread as a full one. The bail stays ONLY when
        every placed pad is a synthesized BOUND (or there are no pads at
        all) — DRC over a dimensionally-plausible guess is meaningless,
        the same reasoning ``gerber.export_fab``'s ``SynthesizedPadError``
        already applies at export time.

        **Now builds board furniture (fiducials/title block/silkscreen)
        too**, via the SAME :meth:`_board_furniture` :meth:`_render_gerber`
        and the fab SVG already call — this view used to check ONLY
        component pads/copper, so a fiducial (an ordinary flashed pad on
        the real fab set) had no representation here at all: a track
        routed near an outline corner could ship a gerber with a 1mm
        copper dot sitting on top of it while ``view='drc'`` read clean,
        because the dot was never in the model this view checked. The
        furniture is COMPUTED fresh on every call, never persisted and
        read back — see :meth:`Store.pcb_drc_findings_latest`'s own
        docstring for why a persisted-then-read DRC result makes "no rows"
        and "no problems" the same value, the exact trap a silk census
        computed here (not on write) avoids."""
        design = self.store.pcb_load(ref_id)
        if design["board"] is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        ran = self._drc_run(ref_id, design)
        if ran is None:
            return Response(
                body="no realized copper yet\n\nNext: put(kind='pcb', "
                "id='slug', args={'op':'route'}) to realize copper, then "
                "re-check this view."
            )
        run_id, findings, pads_only = ran
        n_error = sum(1 for f in findings if f.severity == "error")
        n_warn = len(findings) - n_error
        head = f"# DRC — run {run_id[:8]} — {n_error} error(s), {n_warn} warn(s)"
        n_synth = sum(1 for f in findings if f.rule == "synthesized_footprint")
        if n_synth:
            head += (
                f"\n{n_synth} part(s) have no cached footprint — checked at a "
                "synthesized bound (see the synthesized_footprint finding(s) "
                "below); their DRC results are not a verdict"
            )
        if pads_only:
            # State the scope EXPLICITLY (round-4 contract) so a clean
            # pads-only pass — pad geometry only, no router output yet
            # (e.g. every net is unrouted by construction) — is never
            # mistaken for a full pass over realized copper.
            head += "\n(pads-only DRC — no routed copper yet)"
        if not findings:
            return Response(body=head + "\n— no findings ✓")
        rows = [
            {
                "severity": f.severity,
                "rule": f.rule,
                "where": f.where,
                "margin_mm": "" if f.margin_mm is None else f"{f.margin_mm:+.3f}",
                "detail": f.detail,
            }
            for f in findings
        ]
        return Response(
            body=head
            + "\n"
            + render_agent_table(
                rows, schema=["severity", "rule", "where", "margin_mm", "detail"]
            )
        )

    def _drc_rule_inputs(
        self,
        design: dict[str, Any],
        courtyard_local: dict[str, list[tuple[float, float]]],
        capability: CapabilityRow,
    ) -> tuple[
        list[pcb_drc.Courtyard],
        dict[str, bool],
        dict[str, NetRules],
        dict[str, float],
    ]:
        """``(courtyards, courtyard_bottom, net_rules, net_voltages)`` — the
        per-design inputs the geometric rules take besides the copper model.
        Shared by :meth:`_drc_run` and :meth:`_validity_findings` so the two
        cannot judge a board by different courtyards or class rules."""
        # The part's own courtyard POLYGON (see :meth:`_drc_geometry`),
        # placed into board coordinates through the SAME affine path its
        # pads and its silkscreen travel — a courtyard that rotated by a
        # different convention would reserve space where the part's own
        # copper is not, and look plausible doing it. A refdes the IR
        # somehow didn't carry falls back to the flat
        # ``DEFAULT_COURTYARD_RADIUS_MM`` square: a safety net, not the
        # normal path, and deliberately still SOMETHING rather than
        # nothing, since a part checked against no shape is a part the
        # rule cannot see.
        _flat = pcb_drc.DEFAULT_COURTYARD_RADIUS_MM
        _fallback = [(-_flat, -_flat), (_flat, -_flat), (_flat, _flat), (-_flat, _flat)]
        courtyards: list[pcb_drc.Courtyard] = [
            (
                str(i["refdes"]),
                place_points(
                    courtyard_local.get(str(i["refdes"])) or _fallback,
                    cx=float(i["x"]),
                    cy=float(i["y"]),
                    rot_deg=float(i.get("rot") or 0.0),
                ),
            )
            for i in design["instances"]
            if i["x"] is not None and i["y"] is not None
        ]
        # gr341516 — a courtyard reservation says nothing about which side
        # of the board it sits on; two parts on OPPOSITE sides, one
        # directly beneath the other (an EWOD sink grid's whole point,
        # `generators.py`'s own module docstring), are not colliding.
        # `padplace.is_bottom_instance` is the SAME predicate `_drc_pads`'s
        # own pad source now reads (`ir.py::from_graph` ->
        # `PcbIR.inst_bottom`) -- one parse of `pcb_instances.layer`, not a
        # second one narrower than it.
        courtyard_bottom = {
            str(i["refdes"]): padplace.is_bottom_instance(i)
            for i in design["instances"]
        }
        net_classes = design.get("net_classes") or {}
        net_rules: dict[str, NetRules] = {
            str(n["name"]): resolve_net_rules(
                str(n.get("net_class") or ""),
                # Clearance (the only field check_clearance reads off this
                # map) doesn't depend on layer -- an arbitrary True is fine
                # here; realize.py is the caller that resolves per-layer.
                layer_is_outer=True,
                fab_caps=capability,
                overrides=net_classes.get(n.get("net_class") or ""),
                current_a=n.get("est_current_a"),
            )
            for n in design["nets"]
        }
        # §E-1's PAIRWISE voltage term. Only annotated nets are in the map —
        # a net missing here is "not annotated", never 0 V, and
        # `check_clearance` reports the difference rather than inventing a
        # potential (`pcb-missing-constraint-classes.md` §E-1).
        net_voltages = {
            str(n["name"]): float(n["working_voltage_v"])
            for n in design["nets"]
            if n.get("working_voltage_v") is not None
        }
        return courtyards, courtyard_bottom, net_rules, net_voltages

    def _drc_run(
        self, ref_id: int, design: dict[str, Any]
    ) -> tuple[str, list[pcb_drc.DrcFinding], bool] | None:
        """One geometric DRC run over ``design`` (which must have a board):
        ``(run_id, findings, pads_only)``, findings persisted under
        ``run_id``. ``None`` when there is nothing real to check — no
        realized copper and every placed pad a synthesized bound.

        Split out of :meth:`_render_drc` so ``view='gerber'`` asks the SAME
        question the DRC view answers before it hands over a fab bundle,
        rather than a second, narrower one."""
        board = design["board"]
        stackup = board["stackup"]
        try:
            capability = capability_for(pcb_drc.process_for_stackup(stackup))
        except ValueError as exc:
            raise BadInput(f"pcb: {exc}") from exc
        layer_names = [str(layer.get("name")) for layer in stackup]
        # Pads come from the IR, which is the SAME source the router and the
        # realizer use (precis.pcb.landpattern via ir.pin_point). A second
        # pad source is how this build's recurring defect works — one rule,
        # two call sites, drifted — and connectivity is precisely the check
        # that would be fooled by pads in the wrong place.
        pads, courtyard_local = self._drc_geometry(ref_id, layer_names)

        copper = self.store.pcb_copper_list(int(board["board_id"]))
        # Round-4 contract change (docs/backlog/pcb-ewod-multitile.md's
        # decisions log): this used to bail whenever `copper` (router
        # output) was empty, full stop — but a board whose every net is
        # fanout-1 (an `ewod_pad_array` standalone board is the motivating
        # case: the array's escape geometry is footprint-PAD copper, never
        # something a router touches) then NEVER gets a single
        # `pcb_copper` row, `op='route'` or not, so `view='drc'` could
        # never actually run one rule against it — "a check you did not
        # run is not a check that passed" (repo doctrine). The new rule:
        # run geometric DRC whenever ANY placed pad is REAL (authored or
        # cached, `synthesized=False`), even with zero realized copper —
        # the response says so explicitly (`_pads_only` below) so a
        # partial (pads-only) pass is never mistaken for a full one. The
        # bail stays ONLY when every placed pad is a synthesized BOUND
        # (or there are no pads at all): DRC over a dimensionally-plausible
        # guess, not real geometry, is meaningless (same reasoning
        # `gerber.export_fab`'s `SynthesizedPadError` already applies at
        # export time). This reaches every board in the kind, not just
        # EWOD ones — an ordinary placed-but-unrouted board with cached
        # real footprints (e.g. `esp32c3_reference`) now gets pads-only
        # findings here too, instead of "not yet".
        has_real_pads = any(not p.get("synthesized") for p in pads)
        if not copper and not has_real_pads:
            return None
        pads_only = not copper

        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        graph = self.store.pcb_graph(ref_id)
        ir = self._build_ir(ref_id, graph)
        instance_sides = {
            str(i.get("refdes")): str(i.get("layer") or "top")
            for i in design["instances"]
        }
        vias = [c for c in copper if c.get("ctype") == "via"]
        outline = self._outline_from_features(ref_id)
        # `pads` below is REPLACED with `_board_furniture`'s own return, not
        # merely extended — that call already folds the component pads
        # passed in here together with the fiducial flashes it mints
        # (module docstring above: this is the fix for fiducial copper
        # being invisible to this view). Component pads never travel
        # through a second path once this line runs.
        # `furniture_warnings` (fiducial/title-block/S/N-patch drop prose)
        # is discarded here on purpose: those three are board-level
        # furniture with no per-instance `SilkPlacement` row to check
        # against (silk.py's own module docstring — fiducials/title block
        # sit outside `build_silk`'s per-instance census entirely), so
        # `run_geometric_drc` has no structured rule for them yet; the
        # gerber/fab-SVG render paths still surface this same prose to a
        # human inspecting those artifacts.
        # `clearance_mm` reused straight off the `capability` row already
        # resolved above rather than a second `capability_for` call — see
        # `_furniture_clearance_mm`'s own docstring for why this is the
        # SAME figure `plane_pours` itself used to cut this board's other
        # antipads at realize time.
        pads, silk_draws, _furniture_warnings, silk_census, copper = (
            self._board_furniture(
                ir,
                pads,
                vias=vias,
                instance_sides=instance_sides,
                polarized=self._polarized_refdes(design),
                copper=copper,
                outline=outline,
                layer_names=layer_names,
                slug=slug,
                clearance_mm=resolve_net_rules(
                    "", layer_is_outer=True, fab_caps=capability
                ).clearance_mm,
                capability=capability,
                date=_export_date(),
            )
        )
        model = {
            "layers": layer_names,
            "copper": copper,
            "pads": pads,
            "drills": self._drc_drills(ref_id),
            "silkscreen": silk_draws,
            "soldermask_expansion_mm": pcb_silk.soldermask_expansion_mm(capability),
        }
        courtyards, courtyard_bottom, net_rules, net_voltages = self._drc_rule_inputs(
            design, courtyard_local, capability
        )
        findings = pcb_drc.run_geometric_drc(
            model,
            capability=capability,
            outline=outline,
            courtyards=courtyards,
            courtyard_bottom=courtyard_bottom,
            net_rules=net_rules,
            net_voltages=net_voltages,
            unrouted=[
                {"net": r["name"], "note": r.get("note")}
                for r in self.store.pcb_route_status(ref_id)
                if r["status"] != "realized"
            ],
            census=silk_census,
            holes=[
                (
                    f"hole @ ({h.x:g}, {h.y:g})",
                    pcb_optimize.mounting_hole_keepout_polygon(h),
                    h.part,
                )
                for h in ir.mounting_holes
            ],
        )
        run_id = uuid.uuid4().hex
        self.store.pcb_write_drc_findings(
            int(board["board_id"]), run_id, [f.to_row() for f in findings]
        )
        return run_id, list(findings), pads_only

    def _validity_findings(self, ref_id: int) -> list[pcb_drc.DrcFinding]:
        """The findings of the geometric-validity rules on the board as
        the store (or the open :meth:`Store.pcb_judged_tx`) holds it now,
        persisting nothing, errors AND warnings (a warning is a shortfall
        against the house or net-class margin; only :meth:`_judged_mutation`
        decides which of them gate): copper clearance (with class rules and net
        voltages), trace width, annular ring, NPTH clearance, via/pad and
        via/via keep-out, board edge, plus courtyard overlap, courtyard vs
        mounting hole and outline containment. Not silkscreen, unrouted/
        connectivity (routedness, not validity), synthesized footprints or
        board furniture.

        Router (non-``fixed``) copper rows carry ``derived: True`` so a
        finding names which side yields (:func:`precis.pcb.session.
        router_nets_of`). ``[]`` for a board with no placed instance: an
        unplaced netlist has no geometry to violate. A part whose footprint
        is only a synthesized bound (no cached or authored pad geometry)
        contributes no pads and no courtyard: a verdict on a guess would
        refuse boards for geometry nobody drew."""
        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is None or not any(
            i["x"] is not None and i["y"] is not None for i in design["instances"]
        ):
            return []
        try:
            capability = capability_for(pcb_drc.process_for_stackup(board["stackup"]))
        except ValueError as exc:
            raise BadInput(f"pcb: {exc}") from exc
        layer_names = [str(layer.get("name")) for layer in board["stackup"]]
        pads, courtyard_local = self._drc_geometry(ref_id, layer_names)
        # A part with no real footprint is checked at a guessed bound, which
        # `view='drc'` itself calls "not a verdict" — never gate on a guess.
        guessed = {str(p.get("refdes")) for p in pads if p.get("synthesized")}
        pads = [p for p in pads if str(p.get("refdes")) not in guessed]
        courtyards, courtyard_bottom, net_rules, net_voltages = self._drc_rule_inputs(
            design, courtyard_local, capability
        )
        courtyards = [c for c in courtyards if c[0] not in guessed]
        copper = [
            {**row, "net": row.get("net") or ""}
            if row.get("fixed")
            else {**row, "derived": True}
            for row in self.store.pcb_copper_list(int(board["board_id"]))
        ]
        model = {
            "layers": layer_names,
            "copper": copper,
            "pads": pads,
            "drills": self._drc_drills(ref_id),
        }
        outline = self._outline_from_features(ref_id)
        ir = self._build_ir(ref_id, self.store.pcb_graph(ref_id))
        findings: list[pcb_drc.DrcFinding] = [
            *pcb_drc.check_clearance(
                model, capability, net_rules=net_rules, net_voltages=net_voltages
            ),
            *pcb_drc.check_trace_width(model, capability),
            *pcb_drc.check_annular_ring(model, capability),
            *pcb_drc.check_npth_clearance(model, capability),
            *pcb_drc.check_via_pad_keepout(model, capability),
            *pcb_drc.check_via_via_keepout(model, capability),
            *pcb_drc.check_board_edge_clearance(model, capability, outline=outline),
            *pcb_drc.check_outline_containment(
                model, outline=outline, courtyards=courtyards
            ),
        ]
        if courtyards:
            findings += pcb_drc.check_courtyard_overlap(
                courtyards, bottom_by_refdes=courtyard_bottom
            )
            findings += pcb_drc.check_courtyard_hole(
                courtyards,
                [
                    (
                        f"hole @ ({h.x:g}, {h.y:g})",
                        pcb_optimize.mounting_hole_keepout_polygon(h),
                        h.part,
                    )
                    for h in ir.mounting_holes
                ],
            )
        return findings

    def _judged_mutation[T](
        self,
        ref_id: int | None,
        mutate: Callable[[Any], T],
        *,
        ref_id_of: Callable[[T], int] | None = None,
        refuse: bool = True,
    ) -> tuple[T, JudgeReport]:
        """Run ``mutate(conn)`` in one transaction and judge the board it
        leaves, as a DELTA against the board before it (legality is a hard
        gate; incompleteness is the only permitted failure):

        - a validity finding the change adds, or deepens by more than
          :data:`_GROUP_MOVE_MARGIN_EPS_MM`, that names ROUTER copper rips
          that copper's net (it goes unrouted, listed in the report) — at
          any severity, so a tightened net class that the stored copper no
          longer meets (a warning-tier shortfall) yields too;
        - an ERROR that names only pads and authored copper is a problem,
          and any problem raises :class:`BadInput` and rolls the whole
          change back (a warning between pads is a margin, not legality);
        - an error the board already had, not worsened, is only counted.

        ``ref_id`` is ``None`` for a design the mutation creates (nothing
        before); ``ref_id_of(result)`` then names it. A board with no placed
        instance is never judged (:meth:`_validity_findings`).

        ``refuse=False`` turns the refusal into a report: the problems are
        kept (``JudgeReport.visible``) and the change commits (the seam for
        a mutation whose new facts must win, e.g. a pulled footprint)."""
        with self.store.pcb_judged_tx() as conn:
            before = self._validity_findings(ref_id) if ref_id is not None else []
            result = mutate(conn)
            rid = ref_id_of(result) if ref_id_of is not None else ref_id
            if rid is None:
                raise ValueError("_judged_mutation: no ref id after the change")
            after = self._validity_findings(rid)
            worse, standing = _margin_delta(
                [(_finding_identity(f), f.margin_mm or 0.0) for f in before],
                [(_finding_identity(f), f.margin_mm or 0.0, f) for f in after],
            )
            report = JudgeReport(
                standing=sum(1 for f in standing if f.severity == "error")
            )
            problems: list[str] = []
            for f, margin, old_margin in worse:
                nets = pcb_session.router_nets_of(f)
                if nets:
                    for net in sorted(nets):
                        report.ripped.setdefault(net, f.rule)
                    continue
                if f.severity != "error":
                    report.margins.append(f"{f.rule} {f.where}")
                    continue
                line = f"{f.rule}: {f.where}"
                if old_margin is not None:
                    line += f" (worse: {margin:.4f}mm vs {old_margin:.4f}mm before)"
                problems.append(line)
            if problems and not refuse:
                report.visible = problems
            elif problems:
                shown = "; ".join(problems[:8])
                more = f" (+{len(problems) - 8} more)" if len(problems) > 8 else ""
                raise BadInput(
                    f"pcb: this change would leave an invalid board: {shown}{more}",
                    next=(
                        "omit x/y for those parts and run put(args={'op':"
                        "'place'}), or give poses clear of the named parts. "
                        "Nothing was changed."
                    ),
                )
            for net in report.ripped:
                self.store.pcb_rip_route(rid, net, conn=conn)
        return result, report

    def _gerber_drc_banner(self, ref_id: int, slug: str) -> tuple[str, int]:
        """What ``view='gerber'`` says about DRC, and the error count.

        Reto's ruling 2026-10-01: a DRC-red board still EXPORTS — refusing
        would take away the bundle a person needs to look at a broken
        board — but it must be impossible to miss. So the fab view runs
        the same DRC ``view='drc'`` does and leads its response with this:
        a block banner with the count, the per-rule breakdown and the
        first findings when there are errors, one line when it is clean.
        A DRC that could not run is said out loud too; before this the
        export mentioned DRC nowhere, on a board with 116 errors."""
        design = self.store.pcb_load(ref_id)
        try:
            ran = self._drc_run(ref_id, design)
        except BadInput as exc:
            return f"⚠️  DRC NOT RUN — {exc}", 0
        if ran is None:
            return (
                "⚠️  DRC NOT RUN — no realized copper and no real pad geometry "
                "to check; this bundle is unverified",
                0,
            )
        run_id, findings, pads_only = ran
        errors = [f for f in findings if f.severity == "error"]
        scope = " (pads-only — no routed copper yet)" if pads_only else ""
        if not errors:
            n_warn = len(findings)
            return (
                f"DRC: 0 errors, {n_warn} warn(s) — run {run_id[:8]}{scope} ✓",
                0,
            )
        by_rule: dict[str, int] = {}
        for f in errors:
            by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        bar = "█" * 72
        lines = [
            bar,
            f"██  DRC FAILED — {len(errors)} ERROR(S){scope}",
            "██  THIS BOARD IS NOT MANUFACTURABLE AS EXPORTED. DO NOT SEND IT TO A FAB.",
            f"██  run {run_id[:8]} · "
            + " · ".join(
                f"{rule} {n}"
                for rule, n in sorted(by_rule.items(), key=lambda kv: -kv[1])
            ),
            bar,
            "first errors:",
        ]
        lines += [
            f"  {f.rule}: {f.where}"
            + ("" if f.margin_mm is None else f"  ({f.margin_mm:+.3f}mm)")
            for f in errors[:_GERBER_BANNER_FINDINGS]
        ]
        if len(errors) > _GERBER_BANNER_FINDINGS:
            lines.append(f"  … {len(errors) - _GERBER_BANNER_FINDINGS} more")
        lines.append(f"Full list: get(kind='pcb', id='{slug}', view='drc')")
        return "\n".join(lines), len(errors)

    # ── SVG render (pcb-svg-render) ─────────────────────────────────────
    def _render_svg(self, ref_id: int, args: dict[str, Any]) -> Response:
        """Publication-quality vector figure (:mod:`precis.pcb.svg`).
        ``args.level='board'`` (default) — realized copper (L5): outline +
        tracks/vias/pours off ``pcb_copper``, the same model shape
        :mod:`precis.pcb.gerber` writes from. **Pads and silkscreen are
        never included here** — the store has no data source for either
        yet (no resolved, instance-transformed pad geometry anywhere in
        this codebase, and no silkscreen table at all; see
        :mod:`precis.pcb.svg`'s module docstring). ``args.level='sketch'``
        — the L3 rubber-band sketch (placed components + straight
        connections, layer-coloured wherever a persisted
        ``pcb_routes.layer_assign`` says so). ``args.layers`` restricts a
        board render to a layer-name subset; ``args.include`` further
        restricts to a subset of :data:`precis.pcb.svg.DEFAULT_INCLUDE`.
        ``args.level='fab'`` renders the board FROM ITS GERBERS with a
        layer selector (:meth:`_render_fab_svg`) — the view that shows pads
        and mask, and the only one that can be trusted about what a fab
        will image. Returns the raw SVG text — never written to disk (this
        is an inline "eye", not an exporter)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        level = str(args.get("level") or "board").strip().lower()

        if level == "sketch":
            graph = self.store.pcb_graph(ref_id)
            if not graph["instances"]:
                return Response(
                    body="no parts on this design yet — nothing to sketch\n\n"
                    "Next: put(kind='pcb', id='slug', args={'components':[...]})"
                )
            ir = self._build_ir(ref_id, graph)
            # Same order as the pcb_route job: stored sketch keys are
            # endpoint pins of the POST-swap IR.
            pcb_session.apply_pin_swap_overrides(
                ir, self.store.pcb_pin_swaps_list(ref_id)
            )
            unmatched = pcb_session.apply_route_overrides(
                ir, self.store.pcb_routes_get(ref_id)
            )
            svg_text = pcb_svg.render_sketch(ir, title=f"{slug} — sketch (L3)")
            if unmatched:
                # The body is raw SVG, so the note rides as an XML comment
                # right after the (optional) XML declaration.
                note = (
                    f"<!-- warning: {unmatched} stored sketch "
                    f"entr{'y' if unmatched == 1 else 'ies'} matched no segment "
                    "(netlist changed since the sketch was saved) -->\n"
                )
                if svg_text.startswith("<?xml"):
                    head, _, rest = svg_text.partition("?>")
                    svg_text = f"{head}?>\n{note}{rest.lstrip()}"
                else:
                    svg_text = note + svg_text
            return Response(body=svg_text)
        if level == "fab":
            return self._render_fab_svg(ref_id, slug)
        if level != "board":
            raise BadInput(
                f"view='svg' args.level={level!r} not recognized",
                options=["board", "sketch", "fab"],
            )

        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        layer_names = [str(layer.get("name")) for layer in board["stackup"]]
        model: dict[str, Any] = {
            "layers": layer_names,
            "outline": self._outline_from_features(ref_id),
            "copper": self.store.pcb_copper_list(int(board["board_id"])),
            # Mounting holes have no copper around them, so without this
            # the board render simply omits every one of them — the figure
            # looks clean rather than incomplete, which is the worst way
            # for a render to be wrong.
            "drills": self._drc_drills(ref_id),
        }
        raw_layers = args.get("layers")
        raw_include = args.get("include")
        svg_text = pcb_svg.render_board(
            model,
            layers=[str(x) for x in raw_layers] if raw_layers else None,
            include={str(x) for x in raw_include} if raw_include else None,
            title=f"{slug} — board (L5)",
        )
        return Response(body=svg_text)

    def _render_capability(self, ref_id: int, args: dict[str, Any]) -> Response:
        """A generator's capability map (pcb-ewod-multitile Slice 2 pt 2):
        usable vs unusable/reserved pads, plaza slot allocation, pin
        naming and computed sizing — read straight off the
        ``pcb_generators`` ledger a ``generators:[...]`` apply already
        stored (:meth:`Store.pcb_generators_for`), no re-expansion.
        ``args.format='svg'`` (default) renders
        :func:`precis.pcb.svg.render_capability_map`; ``args.format=
        'ledger'`` renders the same data as agent-facing tables (pads +
        plaza slots) — "the same as a machine-readable ledger" the spec
        asks for, without inventing a second on-disk shape: the ledger
        dict IS already machine-readable, this just tabulates it.
        ``args.name`` selects which generator when a design authors more
        than one; a lone generator is the default so the common case
        needs no extra arg."""
        gens = self.store.pcb_generators_for(ref_id)
        if not gens:
            raise BadInput(
                "pcb: no generators on this design — view='capability' needs "
                "a generators:[...] call (e.g. 'ewod_pad_array') applied first",
                next="put(kind='pcb', id='slug', args={'generators':[{'name':"
                "'ARR1','generator':'ewod_pad_array','params':{'grid':[9,9]}}]})",
            )
        name = str(args.get("name") or "").strip()
        if not name:
            if len(gens) > 1:
                raise BadInput(
                    f"pcb: {len(gens)} generators on this design — pass "
                    f"args={{'name': ...}}, one of {sorted(gens)}",
                )
            name = next(iter(gens))
        elif name not in gens:
            raise BadInput(f"pcb: no generator named {name!r} — known: {sorted(gens)}")
        row = gens[name]
        fmt = str(args.get("format") or "svg").strip().lower()
        if fmt not in ("svg", "ledger"):
            raise BadInput(
                f"view='capability' args.format={fmt!r} not recognized",
                options=["svg", "ledger"],
            )
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        ledger = row["ledger"] or {}
        if fmt == "svg":
            return Response(
                body=pcb_svg.render_capability_map(
                    ledger, row["params"] or {}, title=f"{slug}#{name} — capability map"
                )
            )
        pad_rows = [
            {
                "pin": pin,
                "row": info.get("row"),
                "col": info.get("col"),
                "usable": info.get("usable", True),
                "reason": info.get("reason", ""),
                "plaza": info.get("plaza", ""),
            }
            for pin, info in sorted((ledger.get("pads") or {}).items())
        ]
        slot_rows = [
            {
                "plaza": slot_key,
                "direction": direction,
                "status": slot.get("status"),
                "pin": slot.get("pin", ""),
            }
            for slot_key, plaza in sorted((ledger.get("plazas") or {}).items())
            for direction, slot in sorted((plaza.get("slots") or {}).items())
        ]
        summary = ledger.get("summary") or {}
        head = (
            f"# {slug}#{name} — capability map ({row['generator']}, "
            f"grid {ledger.get('grid')}, variant {ledger.get('variant')})\n"
            f"{summary.get('pads_usable', 0)}/{summary.get('pads_total', 0)} pads "
            f"usable, {summary.get('plazas', 0)} plaza(s)"
        )
        body = (
            head
            + "\n\n## pads\n"
            + render_agent_table(
                pad_rows, schema=["pin", "row", "col", "usable", "reason", "plaza"]
            )
        )
        if slot_rows:
            body += "\n\n## plaza slots\n" + render_agent_table(
                slot_rows, schema=["plaza", "direction", "status", "pin"]
            )
        # docs/backlog/pcb-pre-place-route-blocks.md Slice 2 -- the escape
        # fabric's own per-tile emitted/refused/suppressed counts, off the
        # SAME ledger dict this view already renders (no re-expansion,
        # same as every other section here). Absent for a generator that
        # emits no ``copper`` of its own.
        fabric = ledger.get("fabric") or {}
        fabric_tiles = fabric.get("tiles") or {}
        if fabric_tiles:
            totals = fabric.get("totals") or {}
            body += (
                "\n\n## fabric\n"
                f"{totals.get('emitted', 0)} emitted, {totals.get('refused', 0)} "
                f"refused, {totals.get('suppressed', 0)} suppressed "
                f"(fan-out: {fabric.get('fan', '?')})\n"
                + render_agent_table(
                    [
                        {"tile": tile, **counts}
                        for tile, counts in sorted(fabric_tiles.items())
                    ],
                    schema=["tile", "emitted", "refused", "suppressed"],
                )
            )
        return Response(body=body)

    def _render_fab_svg(self, ref_id: int, slug: str) -> Response:
        """``level='fab'`` — the board rendered FROM ITS GERBERS, with a
        layer selector.

        Not a prettier ``level='board'``. That one draws the model; this
        one exports the fab set and reads it back, so what you are looking
        at is what a fab's own reader will see. The difference is not
        theoretical: ``svg.render_board`` applies a per-layer dash pattern
        as a layer cue, which made continuous B.Cu copper look broken and
        cost a session proving it was not. A view that is stylistically
        different from the artefact cannot verify the artefact.

        It is also the only view that shows pads, soldermask openings and
        drill hits, because those exist in the fab set and nowhere in the
        copper table — plus generated silk (:mod:`precis.pcb.silk`), the
        SAME builder :meth:`_render_gerber` calls, so this preview matches
        what a real export would carry.
        """
        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        layer_names = [str(layer.get("name")) for layer in board["stackup"]]
        pads = self._drc_pads(ref_id, layer_names)
        graph = self.store.pcb_graph(ref_id)
        ir = self._build_ir(ref_id, graph)
        instance_sides = {
            str(i.get("refdes")): str(i.get("layer") or "top")
            for i in design["instances"]
        }
        copper = self.store.pcb_copper_list(int(board["board_id"]))
        vias = [c for c in copper if c.get("ctype") == "via"]
        outline = self._outline_from_features(ref_id) or []
        capability = self._stackup_capability(board["stackup"])
        pads, silk_draws, furniture_warnings, _silk_census, copper = (
            self._board_furniture(
                ir,
                pads,
                vias=vias,
                instance_sides=instance_sides,
                polarized=self._polarized_refdes(design),
                copper=copper,
                outline=outline,
                layer_names=layer_names,
                slug=slug,
                clearance_mm=self._furniture_clearance_mm(board["stackup"]),
                capability=capability,
                date=_export_date(),
            )
        )
        model: dict[str, Any] = {
            "layers": layer_names,
            "outline": outline,
            "copper": copper,
            "pads": pads,
            # Mounting-hole drills — without them this preview showed a
            # solder-nut ring with no hole through it while the real
            # bundle (`_render_gerber`, same IR source) drills one; the
            # two views disagreeing about the artefact is exactly what
            # this level='fab' view exists to prevent. Via drills ride in
            # from `copper` (excellon_files' own via pass), so this key
            # carries only the board-config holes.
            "drills": [
                {"x": h.x, "y": h.y, "dia_mm": h.drill_mm, "plated": h.plated}
                for h in ir.mounting_holes
            ],
            "silkscreen": silk_draws,
            "soldermask_expansion_mm": pcb_silk.soldermask_expansion_mm(capability),
            "mask_open_regions": self._mask_open_regions(ref_id),
        }
        # `synthesized_by_refdes` off `pads` BEFORE export_fab -- the gerber
        # round-trip loses this flag entirely (gerber has no "this pad is a
        # bound, not a measurement" concept, only net/refdes/pin X2
        # attributes), so it can only be read here, from the model, not
        # from the fab set `render_fab_svg` parses back (gripe gr341532).
        synthesized_by_refdes: dict[str, int] = {}
        for pad in pads:
            # `part_lcsc` scoping (gr341532 fix, same as
            # `drc.py::check_synthesized_footprint`) -- a design-local pad
            # (no LCSC C-number) is synthesized by construction and can
            # never be cached, so it is not this note's business.
            if pad.get("synthesized") and pad.get("part_lcsc"):
                refdes = str(pad.get("refdes") or "")
                if refdes:
                    synthesized_by_refdes[refdes] = (
                        synthesized_by_refdes.get(refdes, 0) + 1
                    )
        # allow_synthesized, because this is a picture and not an order.
        # export_fab's refusal exists to stop a land-pattern BOUND reaching
        # a fab; looking at one is exactly how you notice it is a bound.
        files = pcb_gerber.export_fab(model, name=slug, allow_synthesized=True)
        try:
            body = gerber_view.render_fab_svg(files, title=slug)
            # **A dropped fiducial or title block must not be invisible on the
            # surface a human INSPECTS the board with.** This path used to
            # discard these: a silently-missing title block looked identical
            # to one nobody asked for, and that cost a real diagnosis. The
            # gerber path prints them; here the SVG body is the whole
            # response, so they ride along inside the document.
            #
            # **`<desc>`, not an XML comment.** The first cut used a comment
            # and every renderer rejected the whole file: these messages are
            # full of `--` ("-- dropped", "-- skipped") and an XML comment
            # may not contain a double hyphen. That turned a diagnostic into
            # a corrupted artefact, and only opening the SVG revealed it —
            # the honesty mechanism broke the thing it was reporting on.
            # `<desc>` is a legal child of `<svg>`, ignored by renderers,
            # read by screen readers, and greppable.
            if furniture_warnings:
                notes = "\n".join(
                    "  " + w.replace("&", "&amp;").replace("<", "&lt;")
                    for w in furniture_warnings
                )
                desc = f"<desc>pcb render warnings:\n{notes}\n</desc>\n"
                body = body.replace("</svg>", f"{desc}</svg>", 1)
            if synthesized_by_refdes:
                # A dashed-stroke/hatch per-pad treatment would need the
                # gerber round-trip to carry a per-flash "synthesized" flag
                # (it doesn't -- see the comment above `synthesized_by_
                # refdes` -- and gerber.py/gerber_view.py are outside this
                # change's remit), so this stays a legend NOTE rather than a
                # distinct visual treatment on the copper itself.
                lines = "\n".join(
                    "  synthesized footprint: "
                    f"{refdes.replace('&', '&amp;').replace('<', '&lt;')} "
                    f"({n} pins) — geometry is a bound, not the part"
                    for refdes, n in sorted(synthesized_by_refdes.items())
                )
                desc = f"<desc>synthesized footprints (not real geometry):\n{lines}\n</desc>\n"
                body = body.replace("</svg>", f"{desc}</svg>", 1)
            return Response(body=body)
        except gerber_view.UnsupportedGerber as exc:
            raise BadInput(f"pcb: cannot render this fab set — {exc}") from exc

    # ── delete ───────────────────────────────────────────────────────
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='pcb') requires id= (the design slug)")
        ref = resolve_live_slug_ref(self.store, kind="pcb", id=str(id).strip())
        counts = self.store.pcb_delete(ref.id)
        n = counts.get("pcb_instances", 0)
        return Response(body=f"retired pcb design {ref.slug} ({n} instance(s))")

    # ── search ───────────────────────────────────────────────────────
    def search(
        self,
        *,
        q: str | None = None,
        mode: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='pcb') requires q=",
                next="search(kind='pcb', q='I2C sensor node')",
            )
        q = str(q)
        triples = self._card_search(q, query_vec=None, mode=mode, page_size=page_size)
        if not triples:
            return Response(body=f"no pcb designs match {q!r}")
        rows = []
        for _block, ref, _score in triples:
            handle = handle_registry.try_format("pcb", ref.id, chunk=False) or "—"
            rows.append({"handle": handle, "design": ref.slug, "title": ref.title})
        return Response(
            body=f"# {len(triples)} pcb design(s) for {q!r}\n"
            + render_agent_table(rows, schema=["handle", "design", "title"])
        )

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        page_size: int = 10,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        triples = self._card_search(
            q, query_vec=query_vec, mode=mode, page_size=page_size
        )
        self.store.chunks.bump_salience([b.id for b, _r, _s in triples])
        out: list[SearchHit] = []
        for block, ref, score in triples:
            text = (getattr(block, "text", "") or "").strip()
            preview = text if len(text) <= 200 else text[:199].rstrip() + "…"
            out.append(
                SearchHit(
                    score=float(score),
                    kind="pcb",
                    title=ref.title or ref.slug or "",
                    preview=preview,
                    slug=ref.slug,
                    ref_id=ref.id,
                    dedupe_key=f"pcb:{ref.slug or ref.id}",
                    uhandle=handle_registry.try_format("pcb", ref.id, chunk=False),
                )
            )
        return out

    def _card_search(
        self,
        q: str,
        *,
        query_vec: list[float] | None,
        mode: str | None,
        page_size: int,
    ) -> list[Any]:
        if not (q and q.strip()):
            return []
        if (mode or "").strip().lower() == "lexical":
            query_vec = None
        elif query_vec is None:
            query_vec = embed_query(self.embedder, q)
        return self.store.chunks.search_chunks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="pcb",
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            card_kinds=("card_combined",),
        )

    # ── rendering helpers ────────────────────────────────────────────
    def _render_list(self) -> Response:
        designs = self.store.list_refs(kind="pcb", order_by="id_desc", limit=50)
        if not designs:
            return Response(
                body="no pcb designs yet\n\nNext: put(kind='pcb', id='sensor-node', "
                "args={'components': [...], 'nets': [...], 'connections': [...]})"
            )
        rows = [{"design": r.slug, "title": r.title} for r in designs]
        return Response(
            body=f"# {len(designs)} pcb design(s)\n"
            + render_agent_table(rows, schema=["design", "title"])
        )

    def _toc(self, design: dict[str, Any]) -> str:
        parts = []
        board = design.get("board")
        if board is not None:
            parts.append(
                f"## board: {board['name']} — {self._stackup_summary(board['stackup'])}"
            )
        irows = [
            {
                "refdes": i["refdes"],
                "part": i["label"],
                "lcsc": i["part_lcsc"] or "—",
                "layer": i["layer"],
                "pose": self._pose(i),
                "roles": ",".join(i["roles"]) or "—",
            }
            for i in design["instances"]
        ]
        parts.append(
            "## parts\n"
            + render_agent_table(
                irows, schema=["refdes", "part", "lcsc", "layer", "pose", "roles"]
            )
        )
        nrows = [
            {
                "net": n["name"],
                "class": n["net_class"] or "—",
                "fanout": n["fanout"],
                "I": "" if n["est_current_a"] is None else f"{n['est_current_a']:g}A",
                "w": "" if n["width_mm"] is None else f"{n['width_mm']:g}mm",
            }
            for n in design["nets"]
        ]
        parts.append(
            "## nets\n"
            + render_agent_table(nrows, schema=["net", "class", "fanout", "I", "w"])
        )
        net_classes = design.get("net_classes") or {}
        if net_classes:
            crows = [
                {"class": name, "rules": json.dumps(rules)}
                for name, rules in net_classes.items()
            ]
            parts.append(
                "## net classes\n"
                + render_agent_table(crows, schema=["class", "rules"])
            )
        n_nets = len(design["nets"])
        route_status = design.get("route_status") or {}
        if n_nets:
            if route_status:
                summary = format_route_summary(route_status)
            else:
                summary = f"{n_nets} net(s): {n_nets} unrouted"
            parts.append(f"## route status: {summary}")
        return "\n".join(parts)

    def _stackup_summary(self, stackup: Any) -> str:
        """``4 layers: F.Cu/In1.Cu(GND)/In2.Cu/B.Cu`` — the plane_net (if
        any) parenthesised after its layer name."""
        layers = list(stackup or [])
        names = [
            f"{layer.get('name')}({layer['plane_net']})"
            if layer.get("plane_net")
            else str(layer.get("name"))
            for layer in layers
        ]
        return f"{len(layers)} layers: {'/'.join(names)}"

    def _pose(self, i: dict[str, Any]) -> str:
        if i["x"] is None or i["y"] is None:
            return "unplaced"
        pose = f"@{i['x']:g},{i['y']:g}"
        if i["rot"]:
            pose += f" r{i['rot']:g}"
        if i["fixed"]:
            pose += f" 📌{i['fixed']}"
        return pose

    def _render_pinout(self, ref: Any, refdes: str) -> Response:
        design = self.store.pcb_load(ref.id)
        instance = next((i for i in design["instances"] if i["refdes"] == refdes), None)
        if instance is None:
            raise NotFound(
                f"pcb instance {refdes!r} not found in this design",
                next=f"get(kind='pcb', id={ref.slug!r})",
            )
        lcsc = instance.get("part_lcsc")
        key = str(lcsc or instance.get("footprint") or "")
        if lcsc:
            footprint = self.store.part_footprint_get(key)
            source = (
                f"catalog-cache; source={(footprint or {}).get('source') or 'unknown'}"
            )
        else:
            footprint = self.store.pcb_local_footprints_for(ref.id).get(key)
            source = "design-local-authored"
        head = (
            f"# {ref.slug}#{refdes} — pinout\n"
            f"footprint: {key or 'unavailable'}; {source}\n"
            f"instance: x_mm={instance.get('x')} y_mm={instance.get('y')} "
            f"side={'bottom' if padplace.is_bottom_instance(instance) else 'top'} "
            f"rotation_deg={instance.get('rot') or 0}\n"
            "orientation: footprint-local +X right / +Y up; board top-view, "
            "CW-positive rotation; bottom mirrors local X before rotate/translate.\n"
            "Stored geometry/mapping, not vendor or mating-orientation verification.\n"
        )
        if not footprint or not footprint.get("pads"):
            hint = (
                f"put(kind='pcb', id={ref.slug!r}, "
                f"args={{'op':'footprint', 'part':{key!r}}})"
                if lcsc
                else "get(kind='skill', id='precis-pcb-help') — author footprints[] "
                f"for {key or 'this instance'}"
            )
            return Response(
                body=head + f"geometry: unavailable (no stored pads)\nnext: {hint}"
            )
        neighbors = self.store.pcb_instance_neighbors(ref.id, refdes)
        stackup = (design.get("board") or {}).get("stackup") or []
        layers = [str(layer["name"]) for layer in stackup]
        result = eyes.pinout(
            instance, footprint, (neighbors or {}).get("pins", []), layers
        )
        rows = []
        for pad in result["rows"]:
            row = dict(pad)
            for field in (
                "local_x_mm",
                "local_y_mm",
                "board_x_mm",
                "board_y_mm",
                "pad_rotation_deg",
            ):
                value = row[field]
                row[field] = "unavailable" if value is None else f"{value:.4f}"
            for field in (
                "board_layers",
                "pin_names",
                "net_names",
                "mapping_sources",
                "duplicate_indices",
                "notes",
            ):
                row[field] = json.dumps(row[field], ensure_ascii=False, sort_keys=True)
            rows.append(row)
        body = head + f"geometry: stored; physical pads: {len(rows)}\n"
        if not result["placed"]:
            body += "board coordinates: unavailable (unplaced/invalid pose)\n"
        body += render_agent_table(
            rows,
            schema=[
                "pad_index",
                "pad_number",
                "local_x_mm",
                "local_y_mm",
                "board_x_mm",
                "board_y_mm",
                "pad_layer",
                "board_layers",
                "pad_rotation_deg",
                "footprint_pin",
                "pin_names",
                "net_names",
                "mapping_sources",
                "mapping_state",
                "duplicate_indices",
                "geometry",
                "notes",
            ],
        )
        if result["unmatched"]:
            body += "\nunmatched declared pins:\n" + render_agent_table(
                result["unmatched"]
            )
        return Response(body=body)

    def _render_instance(self, ref_id: int, refdes: str) -> Response:
        nb = self.store.pcb_instance_neighbors(ref_id, refdes)
        if nb is None:
            raise NotFound(f"pcb instance {refdes!r} not found in this design")
        rows = [
            {
                "pin": p["pin"],
                "pad": p["pad"] or "—",
                "tags": ",".join(p["tags"]) or "—",
                "net": p["net"] or "(nc)",
                "neighbors": ",".join(p["neighbors"]) or "—",
            }
            for p in nb["pins"]
        ]
        return Response(
            body=f"# {refdes} — {len(nb['pins'])} pin(s)\n"
            + render_agent_table(
                rows, schema=["pin", "pad", "tags", "net", "neighbors"]
            )
        )

    def _render_net(self, ref_id: int, name: str) -> Response:
        net = self.store.pcb_net_members(ref_id, name)
        if net is None:
            raise NotFound(f"pcb net {name!r} not found in this design")
        rows = [
            {"refdes": m["refdes"], "pin": m["pin"], "tags": ",".join(m["tags"]) or "—"}
            for m in net["members"]
        ]
        cls = net["net_class"] or "—"
        return Response(
            body=f"# net {name} (class {cls}) — {len(net['members'])} pin(s)\n"
            + render_agent_table(rows, schema=["refdes", "pin", "tags"])
        )
