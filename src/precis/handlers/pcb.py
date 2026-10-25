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
  a no-op, changed params retire-and-reinsert the previous expansion
  (carrying each generator-owned instance's committed pose forward;
  freshly-seeded secondaries are legalised — or unplaced when their own
  expansion's copper makes the seed untenable — before the always-valid
  judge reads the board, pcb-global-plan-router slice 5b).
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
  ``op='order'`` (pcb-jlc-order) dry-runs or places the JLCPCB order
  (``dry_run`` default True; the live path needs ``confirm='add-to-cart'``
  and creates an unpaid order — the API's cart equivalent, never a
  payment). See ``precis-pcb-route-help``.
- ``get``    — list designs (no id); a design's netlist TOC (``id=slug``,
  now with the board/stackup + net_classes + route-status summary); one
  instance's neighbourhood (``id='slug#U3'`` — its pins, the net on each, and
  the connected instances); a net's members (``id='slug@SCL'``); the *eyes*
  (``view='crossings'|'ratsnest'|'drc'|'trace'|'proximity'|'measures'|
  'feasibility'|'route-status'|'congestion'|'planes'|'stackup'|'channel-map'``); ``view='footprints'``
  — which catalog-part instances have a cached footprint, the read-side
  counterpart to ``op='footprint'``; ``view='svg'`` — a
  publication-quality vector figure (``args={'level':'board'|'sketch'|'fab'}``,
  see :mod:`precis.pcb.svg`); ``view='capability'`` — a generator's
  capability map (usable/reserved pads, plaza slots, pin names, computed
  sizing; ``args={'name'?,'format':'svg'|'ledger'}``, pcb-ewod-multitile
  Slice 2); or an *export*
  (``view='bom'|'cpl'|'netlist'|'dsn'|'mechanical'|'gerber'|'epro'|'fab'``
  writes a JLCPCB fab artifact — ``'gerber'`` is the full manufacturable
  bundle (gerbers + Excellon, zipped) off our own realizer/pads, never
  Freerouting/kicad-cli, and ``'fab'`` is that zip plus the BOM/CPL in
  JLC's upload formats, the package ``op='order'`` ships;
  ``view='route'`` runs the Freerouting
  place↔route round-trip, the demoted escape hatch —
  :mod:`precis.pcb.export` / :mod:`precis.pcb.route`).
- ``search`` — over design names + descriptions (the one summary card).
- ``delete`` — soft-retire a whole design.
- ``link``  — edges to other refs (``rel=`` per ``precis-relations``;
  ``view='links'`` reads them back).

The in-house topological place+route engine (:mod:`precis.pcb.optimize` /
:mod:`precis.pcb.realize`, run via the ``pcb_place``/``pcb_route`` jobs) is
the primary path; Freerouting/gerbers-via-DSN is the rented, demoted escape
hatch. Export is the one place the design leaves the relational graph.
See ``precis-pcb-help`` and ``precis-pcb-route-help``.
"""

from __future__ import annotations

import collections
import csv
import dataclasses
import difflib
import hashlib
import io
import json
import logging
import math
import tempfile
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from precis import bom_nudge
from precis.checklist.status import NOT_CHECKABLE
from precis.config import load_config
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._interpret import interpreted
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.pcb import (
    DEFAULT_STACKUP,
    PIN_ETYPES,
    PRESET_GROUND_NET,
    PRESET_SIDES,
    STACKUP_PRESETS,
    argue,
    check_run,
    eyes,
    format_route_summary,
    format_warm_start,
    gerber_view,
    padplace,
    place,
    ratsnest,
    stackup_preset,
)
from precis.pcb import catalog as pcb_catalog
from precis.pcb import connectivity as pcb_connectivity
from precis.pcb import datasheets as pcb_datasheets
from precis.pcb import drc as pcb_drc
from precis.pcb import edge_mate as pcb_edge_mate
from precis.pcb import epro_verify as pcb_epro_verify
from precis.pcb import epro_write as pcb_epro_write
from precis.pcb import export as pcb_export
from precis.pcb import furniture as pcb_furniture
from precis.pcb import generators as pcb_generators
from precis.pcb import geom as pcb_geom
from precis.pcb import gerber as pcb_gerber
from precis.pcb import ir as pcb_ir
from precis.pcb import jlc_api as pcb_jlc_api
from precis.pcb import jlc_order as pcb_jlc_order
from precis.pcb import layer_lock as pcb_layer_lock
from precis.pcb import length_ratio as pcb_length_ratio
from precis.pcb import matching as pcb_matching
from precis.pcb import mounts as pcb_mounts
from precis.pcb import optimize as pcb_optimize
from precis.pcb import planes as pcb_planes
from precis.pcb import profiles as pcb_profiles
from precis.pcb import realize as pcb_realize
from precis.pcb import resolved as pcb_resolved
from precis.pcb import review as pcb_review
from precis.pcb import route as pcb_route
from precis.pcb import route_state as pcb_route_state
from precis.pcb import selectables as pcb_selectables
from precis.pcb import session as pcb_session
from precis.pcb import session_store as pcb_session_store
from precis.pcb import silk as pcb_silk
from precis.pcb import svg as pcb_svg
from precis.pcb import topo as pcb_topo
from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.footprint import fetch_footprint
from precis.pcb.landpattern import rotate_offset
from precis.pcb.rows import (
    normalize_footprint as normalize_local_footprint,
)
from precis.pcb.rules import NetRules, clearance_rule_problems
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    require_link_target,
    validate_link_mode,
)
from precis.utils.search_merge import SearchHit
from precis.utils.units import parse_angle_deg

log = logging.getLogger(__name__)


_export_date = check_run.export_date


#: The "eyes" — analytic, computed-on-read (Slice 4/5).
_PROBE_VIEWS = (
    "crossings",
    "ratsnest",
    "drc",
    "check",
    "trace",
    "proximity",
    "measures",
    "feasibility",
)
#: Pure file exporters off the IR (Slice 6). ``gerber`` (pcb-fab-output-
#: unwired) is the odd one out here — it doesn't read ``_export_model``
#: (the netlist IR), it assembles :mod:`precis.pcb.gerber`'s copper+pad
#: model straight off the store, see :meth:`PcbHandler._render_gerber`.
#: ``fab`` (pcb-jlc-order) is the JLC upload package: the gerber zip plus
#: BOM and CPL in JLC's column formats, one directory.
_EXPORT_VIEWS = (
    "bom",
    "cpl",
    "netlist",
    "dsn",
    "mechanical",
    "gerber",
    "epro",
    "fab",
)
#: How many errors ``view='gerber'``'s DRC banner quotes before pointing at
#: ``view='drc'`` for the rest — enough to see what kind of failure it is.
_GERBER_BANNER_FINDINGS = 8
#: The rented autorouter round-trip (Slice 6, gated on Freerouting).
_ROUTE_VIEWS = ("route",)
#: pcb-guided-place-route Slice 1 — per-net route status (all-unrouted v1).
_STATUS_VIEWS = ("route-status",)
#: pcb-guided-place-route Slice 10 — the enqueued optimizer/realizer's
#: read-side: congestion warnings from the latest ``op='route'`` run, and
#: authored plane assignments. ``stackup`` (gr486893) is the board's
#: copper stackup as the router sees it — role, plane net, routable.
_SESSION_VIEWS = ("congestion", "planes", "stackup", "channel-map")

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
#: ``notes`` (pcb-argue-with-design.md) — the design's argument ledger:
#: what the user typed on the board page, verbatim, anchored by handle,
#: with the model's answer re each question (:mod:`precis.pcb.argue`).
#: ``availability`` (gr485250) — the per-BOM-line stock/library/lifecycle
#: table for an EOL sweep; rows whose catalog data is stale get one bounded
#: live jlcpcb.com refresh on the way out.
_OTHER_VIEWS = (
    "links",
    "footprints",
    "pinout",
    "pinout-preview",
    "notes",
    "availability",
    "placement",
)
_VIEWS = (
    *_PROBE_VIEWS,
    *_EXPORT_VIEWS,
    *_ROUTE_VIEWS,
    *_STATUS_VIEWS,
    *_SESSION_VIEWS,
    *_RENDER_VIEWS,
    *_OTHER_VIEWS,
)
#: ``view=`` spellings with exactly one reading (gr495607: prod callers sent
#: ``view='easyeda'`` for the EasyEDA Pro export); run as the canonical view
#: with an ``interpreted:`` line.
_VIEW_ALIASES = {"easyeda": "epro", "easyeda-pro": "epro", "epro2": "epro"}
#: Spellings with two plausible readings: refuse, naming both
#: (gr495607: ``view='meta'`` / ``view='nets'``).
_VIEW_NEAR_MISSES = {
    "meta": "the TOC (omit view=: board, stackup, tags, parts, nets)",
    "nets": (
        "the TOC (omit view=) for the net list, id='<board>@<NET>' for one "
        "net's members, or view='netlist' for the netlist file export"
    ),
}

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
    "phase",
    "profile",
    "pins",
    "pin_rename",
    "pin_remove",
    "corner_nuts",
)
#: gr341532 fix 3 — pull/author a catalog part's real footprint into the
#: ``part_footprints`` cache. A handful of small HTTP calls (EasyEDA), not
#: the router's per-board compute, so it runs inline like the edits above,
#: never as an enqueued worker job.
_FOOTPRINT_OPS = ("footprint",)
#: pcb-jlc-order — the JLCPCB order pipeline (upload → quote → create).
#: Cheap request-building plus bounded HTTP, not worker compute — inline
#: like ``footprint``, guarded by ``dry_run``/``confirm='add-to-cart'``.
_ORDER_OPS = ("order",)
#: gr495289 (Reto's ruling: option 2a) — re-import a board's OWN ``.epro2``
#: export into a new preview slug, server-side. Addresses a slug that does
#: not exist yet, so ``put`` routes it before the existing-design lookup.
_IMPORT_OPS = ("import_epro",)
_OPS = (
    *_JOB_OPS,
    *_INLINE_EDIT_OPS,
    *_FOOTPRINT_OPS,
    *_ORDER_OPS,
    *_IMPORT_OPS,
    "datasheets",
    "review",
    "check",
)

#: ``view='availability'``: catalog rows older than this (or absent) are
#: re-checked live, bounded by ``jlc_api._LIVE_BATCH_MAX`` per call.
_AVAIL_STALE_S = 6 * 3600


def _availability_lifecycle(row: dict[str, Any], live_outcome: str | None) -> str:
    """The lifecycle/EOL cell for one ``view='availability'`` row: every
    signal the catalog row carries, or the live-refresh outcome when the
    part was probed this call (a confirmed 'miss' beats 'not in catalog')."""
    tokens: list[str] = []
    params = row.get("params") or {}
    lc = params.get("_lifecycle") or {}
    if lc.get("merged"):
        tokens.append(f"merged→{lc['merged']}")
    if lc.get("alts"):
        tokens.append(f"alts {lc['alts']}")
    if lc.get("no_buy"):
        tokens.append(f"no-buy {lc['no_buy']}")
    if lc.get("idle"):
        tokens.append("idle")
    if lc.get("est"):
        tokens.append(f"est {lc['est']}")
    if lc.get("loss"):
        tokens.append(f"stock-loss {lc['loss']}")
    if row.get("discontinued"):
        tokens.append("dropped-from-dump")
    if row.get("stock") == 0:
        tokens.append("stock 0")
    if not tokens and row.get("part_lcsc") and row.get("refreshed_at") is None:
        tokens.append(
            "absent from jlcpcb.com" if live_outcome == "miss" else "not in catalog"
        )
    return "; ".join(tokens) if tokens else "ok"


#: A pre-existing finding the group move makes more negative than this is a
#: new fault, not a standing one.
_GROUP_MOVE_MARGIN_EPS_MM = 1e-4
#: ``op='move'`` kwargs that are ``pcb_instances`` columns (not pose, not
#: ``fixed``): the rigid-group path writes poses only, so these go on after.
_MOVE_COLUMN_KEYS = frozenset(
    {"layer", "max_travel_mm", "fixed_side", "group_fixed", "room_id"}
)
_MOVES_EXAMPLE = (
    "args={'op':'move','moves':[{'refdes':'U1','x':10,'y':5},"
    "{'refdes':'U2','x':20,'y':5,'rot':90}]}"
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
    ``via_pad_keepout``, ``outline`` and ``edge_mating`` are per part (hole
    id / ``via`` / ``board outline`` / ``board edge``). ``skip[refdes]`` is the part's own rigid
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
        unit = "mm" if rule in ("via_pad_keepout", "edge_mating") else "mm\u00b2"
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


#: What :meth:`PcbHandler._drc_run` hands its callers.
_DrcRun = check_run.DrcRun


#: Gone findings listed in full up to this many; past it, a count.
_DRC_GONE_SHOWN = 40


#: ``view='drc'`` adds a severity x rule tally line from this many findings.
_DRC_SUMMARY_MIN = 10


def _drc_run_label(run_id: str | None) -> str:
    """``run 1a2b3c4d`` for a recorded run, ``unrecorded run`` for a read."""
    return f"run {run_id[:8]}" if run_id else "unrecorded run"


#: The tail a read-only check view appends (gr491591, Reto 2026-10-16).
_DRC_RECORD_HINT = (
    "\n\nThis view records nothing; the delta is against the last recorded "
    "run. To record one: put(kind='pcb', id='{slug}', args={{'op':'check'}})"
)


def _rel_age(at: datetime | None) -> str:
    """``34min ago``/``2h ago``/``3d ago`` — the relative half of a reused
    run's recorded-at ack (gr496242)."""
    if at is None:
        return "unknown age"
    s = max(0, int((datetime.now(UTC) - at.astimezone(UTC)).total_seconds()))
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}min ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _drc_reused_line(ran: _DrcRun) -> str:
    """gr496242: the ack a reused recorded run carries — which run, when
    it recorded (UTC plus age), that the board is unchanged since, and
    how to force a fresh one."""
    at = (ran.baseline or {}).get("recorded_at")
    when = (
        at.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
        if at is not None
        else "unknown time"
    )
    return (
        f"\nreused {_drc_run_label(ran.run_id)} recorded {when} "
        f"({_rel_age(at)}) — the board is unchanged since; "
        "args={'fresh': true} re-runs inline"
    )


def _drc_stale_line(baseline: dict[str, Any] | None) -> str:
    """gr492096: the line under ``vs run X`` when that run is older than
    the board's current revision — both revisions and the record time, so
    the delta is never read as a comparison against the current design."""
    if not baseline or not baseline["stale"]:
        return ""
    at = baseline["recorded_at"]
    when = (
        at.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
        if at is not None
        else "unknown time"
    )
    rec = (baseline["recorded_revision"] or "unstamped")[:8]
    return (
        f"\nstale baseline: that run was recorded {when} at revision {rec}; "
        f"the board is now at revision {baseline['current_revision'][:8]}"
    )


def _drc_delta_line(delta: pcb_drc.FindingDelta) -> str:
    """The honest-count line under the DRC head: new / still (worse) /
    gone against the previous run, each counted separately and NEVER
    netted — a move that fixes three findings and creates three others
    must not read as "no change" (docs/backlog/finding-stable-identity.md).
    With no keyed previous run it says so, rather than calling everything
    new."""
    if delta.previous_run_id is None:
        return (
            "vs previous run: none to compare against (first run with finding "
            "ids on this board) — every finding below is reported without a "
            "new/still verdict"
        )
    n_worse = len(delta.worse)
    worse = f" ({n_worse} worse)" if n_worse else ""
    return (
        f"vs run {delta.previous_run_id[:8]}: {len(delta.new)} new, "
        f"{len(delta.still)} still{worse}, {len(delta.gone)} gone"
    )


def _drc_gone_block(delta: pcb_drc.FindingDelta) -> str:
    """The previous run's findings that have no counterpart now, one line
    each (``id rule where margin``) so "these two went away" is a
    statement about named findings, not a change in a count."""
    if not delta.gone:
        return ""
    lines = [
        "",
        f"## gone since run {(delta.previous_run_id or '')[:8]} ({len(delta.gone)})",
    ]
    for r in delta.gone[:_DRC_GONE_SHOWN]:
        m = r.get("margin_mm")
        margin = "" if m is None else f"  ({float(m):+.3f}mm)"
        lines.append(
            f"- {r.get('finding_key') or '-'}  {r.get('severity')}  {r.get('rule')}: "
            f"{r.get('location') or r.get('detail') or ''}{margin}"
        )
    if len(delta.gone) > _DRC_GONE_SHOWN:
        lines.append(f"- … {len(delta.gone) - _DRC_GONE_SHOWN} more")
    return "\n".join(lines)


def _finding_identity(f: pcb_drc.DrcFinding) -> tuple[Any, ...]:
    """The shared finding identity (:func:`precis.pcb.drc.finding_identity`)
    — the same ``(rule, participants, layer)`` ``view='drc'`` persists as
    ``finding_key``, so a group move and the DRC delta agree on what "the
    same finding" means."""
    return pcb_drc.finding_identity(f)


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


#: Hex digits of the sha256 a download path carries — enough that a stale
#: link cannot match a changed board by accident, short enough to paste.
EPRO_SHA_PREFIX = 16


@dataclasses.dataclass(frozen=True)
class EproBuild:
    """One in-memory ``.epro2`` export (:meth:`PcbHandler.epro_build`):
    ``model``/``warnings`` from ``_fab_model``, the writer's ``exported``
    result, and the zipped ``blob``."""

    slug: str
    model: dict[str, Any]
    warnings: list[str]
    exported: pcb_epro_write.EproExport
    blob: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.blob).hexdigest()

    @property
    def filename(self) -> str:
        return f"{pcb_epro_write.safe_stem(self.slug)}.epro2"

    def download_path(self, *, allow_synthesized: bool = False) -> str:
        """The web UI route that rebuilds these bytes; its ``sha`` makes a
        link to a since-edited board answer 409 instead of a different
        file."""
        query = f"sha={self.sha256[:EPRO_SHA_PREFIX]}"
        if allow_synthesized:
            query += "&allow_synthesized=1"
        return f"/pcb/{urllib.parse.quote(self.slug, safe='')}/export.epro2?{query}"


class PcbHandler(Handler):
    #: args= keys each verb reads out of ``args`` (gr475332); dispatch
    #: rejects any other key. Keep in step with the verb bodies.
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "put": ArgsSpec(
            {
                "op",
                "components",
                "nets",
                "connections",
                "measures",
                "features",
                "footprints",
                "generators",
                "autoplace",
                "meta",
                "net_classes",
                "roles",
                "iters",
                "seed",
                "warm_start",
                "router",
                "negotiate",
                "corridor",
                "layer_cost",
                "off_axis",
                "cleanup",
                "place",
                "escape",
                "groups",
                # Opt-in field polish on place/route
                # (OptimizeConfig.field_polish, off until the silk gate
                # lands).
                "field_polish",
                "loop",
                "moves",
                "refdes",
                "x",
                "y",
                "rot",
                "rot_deg",
                "fixed",
                "max_travel_mm",
                "fixed_side",
                "group_fixed",
                "room_id",
                # op='move' refdes label spot override (gr496552).
                "label_spot",
                "priority",
                "snap",
                "net",
                "a",
                "b",
                "side",
                "layer",
                "name",
                "rules",
                "layers",
                # op='stackup' presets (pcb-stackup-regions slice 3).
                "preset",
                "ground",
                "part",
                "parts",
                "footprint",
                "force",
                "phase",
                "profile",
                "pins",
                # op='corner_nuts' (_op_corner_nuts); gr496392.
                "radius_mm",
                "hole_dia_mm",
                "ring_dia_mm",
                "body_dia_mm",
                "clearance_mm",
                "source",
                "item",
                "limit",
                # op='pin_rename' / 'pin_remove': pad number or pin name;
                # `to` is the new net name (pin_rename only).
                "pin",
                "to",
                # op='check': force the inline run past reuse (gr496242).
                "fresh",
                # op='order' (_op_order / _order_dry_run); gr495025.
                "achieve_date",
                "billing_address",
                "billing_address_flag",
                "city",
                "confirm",
                "country",
                "dry_run",
                "order_type",
                "pcb_param",
                "postcode",
                "qty",
                "reorder",
                "shipping_address",
                "shipping_method",
                "smt_stencil",
                "tax_or_vat",
                "thickness_mm",
            }
        ),
        "get": ArgsSpec(
            {
                "a",
                "b",
                "net",
                "dir",
                "allow_synthesized",
                "verify",
                "max_passes",
                "iters",
                "level",
                "layers",
                "include",
                "name",
                "format",
                "pins",
                "phase",
                "rule",
                "severity",
                # view='check'/'drc': force the inline run past reuse
                # (gr496242).
                "fresh",
            }
        ),
    }

    #: netlists, footprints and layout ops: the agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True
    spec: ClassVar[KindSpec] = KindSpec(
        kind="pcb",
        title="PCB",
        can_own_jobs=True,
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
            "'congestion'|'planes'|'stackup'|'channel-map'), per-BOM-line stock/library/lifecycle "
            "for an EOL sweep (view='availability', live-refreshes stale "
            "C-numbers), which catalog parts have a cached "
            "footprint (view='footprints', one row per catalog-part "
            "instance), one instance's actual physical pinout "
            "(id='slug#J1', view='pinout', read-only), an unsaved explicit mapping "
            "preview (same selector, view='pinout-preview', "
            "args={'pins':[{name,pad}]}), a vector figure (view='svg', "
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
            "zipped) and 'fab' is that zip plus JLC-format BOM+CPL in one "
            "dir; view='epro' writes an EasyEDA Pro .epro2 (outline, "
            "parts, pads, nets, copper, pours; no schematic; "
            "args={'verify':true} re-reads it and diffs it against the "
            "board; put(id=<new slug>, args={'op':'import_epro','source':"
            "'export:<slug>'}) re-imports it, dry run by default); "
            "view='route' runs "
            "the demoted Freerouting escape hatch), all with args={...}; "
            "put(args={'op':'place'}) / args={'op':'route'} ENQUEUE a worker "
            "job (never inline; idempotent per design+op+content-hash) — "
            "args={'autoplace':{...}} is a deprecated alias for op='place'; "
            "put(args={'op':'move'|'rip'|'pin_side'|'plane_net'|'class_rules'|"
            "'phase'|'profile'|'pins'}) "
            "are cheap inline edits; put(args={'op':'footprint','part':"
            "'C639448'}) (or parts=[...], force=True to re-pull, or "
            "footprint={...} to author one directly under part=) pulls/"
            "authors a catalog part's real pad geometry into the "
            "part_footprints cache -- fixes the synthesized_footprint DRC "
            "finding; put(args={'op':'order'}) dry-runs a JLCPCB order "
            "(dry_run default True; confirm='add-to-cart' + dry_run=False "
            "creates the unpaid order — never a payment); "
            "view='notes' is the argument ledger the board page's "
            "text box writes (handles REFDES | REFDES.PIN | net:NAME) — "
            "put(kind='job', job_type='pcb_argue', parent_id=<this ref>, "
            "params={'pcb_ref_id':N}) answers every open question in "
            "place; put(args={'op':'review'}) queues a pcb_review job (it runs "
            "its own fresh check) over the unchecked/stale pcb-tapeout items "
            "(precis-pcb-review-help); "
            "search over names; delete soft-retires; link/unlink "
            "edges to other refs (rel= per precis-relations — e.g. a "
            "board 'serves' the quest it feeds). "
            "Postgres-canonical; routing/gerbers are downstream export. "
            "See precis-pcb-help and precis-pcb-route-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_search=True,
        supports_search_hits=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        views=_VIEWS,
    )

    def __init__(
        self, *, hub: Hub, live_refresh: Callable[..., Any] | None = None
    ) -> None:
        if hub.store is None:
            raise InitError("pcb: store required")
        self.hub = hub
        self.store = hub.store
        self.embedder = hub.embedder
        #: Test seam for ``view='availability'``'s bounded live refresh —
        #: ``(store, lcscs) -> {lcsc: 'hit'|'miss'|'error: …'}``; defaults
        #: to :func:`precis.pcb.jlc_api.live_refresh_many`.
        self._live_refresh = live_refresh
        #: ``view='congestion'``'s capacity map, one entry per ref keyed
        #: by the board's ``content_hash`` — the strict flow-graph build
        #: costs seconds on a real board, and every input the map reads
        #: is inside that digest. Scoped to the handler instance: a
        #: module-level cache survives across boards (and tests) whose
        #: ref ids collide.
        self._capacity_map_cache: dict[int, tuple[str, str]] = {}

    # ── put ──────────────────────────────────────────────────────────
    def put(
        self,
        *,
        id: str | int | None = None,
        title: str | None = None,
        args: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
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
            if op in _IMPORT_OPS:
                return self._op_import_epro(slug, args)
            ref = resolve_live_slug_ref(self.store, kind="pcb", id=slug)
            resp = self._dispatch_op(ref, str(op), args)
            if tags or untags:
                resp = dataclasses.replace(
                    resp, body=resp.body + self._put_tags(ref.id, tags, untags)
                )
            return resp
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
        roles = args.get("roles")
        ttl = (title or slug).strip() or slug
        profile = (
            self._parse_profile(args["profile"])
            if args.get("profile") is not None
            else None
        )

        self._reject_non_electrical(nets)
        for comp in components:
            if isinstance(comp, dict) and comp.get("max_travel_mm") is not None:
                try:
                    self._parse_max_travel(comp["max_travel_mm"])
                except BadInput as exc:
                    raise BadInput(
                        f"component {comp.get('refdes')!r}: {exc}",
                        next="max_travel_mm: a number of mm >= 0, or omit it",
                    ) from None

        try:
            # One transaction spans pcb_apply + the net_classes upsert (both
            # accept an external conn=) so a validation error in either half
            # (e.g. a blank net_class name) rolls back the whole put — never
            # a committed design followed by a BadInput implying nothing
            # happened (gr — pcb-guided-place-route Slice 1 review).
            existing = self.store.get_ref(kind="pcb", id=slug)
            if isinstance(net_classes, dict) and net_classes:
                self._refuse_bad_clearance_rules(
                    existing.id if existing is not None else None, net_classes
                )
            tag_counts: dict[str, Any] = {}
            mapped: list[str] = []

            def apply(conn: Any) -> tuple[Any, bool, dict[str, int], int]:
                seeded: list[str] = []
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
                    seeded=seeded,
                    mapped=mapped,
                )
                if profile is not None:
                    self.store.pcb_set_board_profile(ref.id, profile, conn=conn)
                n_classes = 0
                if isinstance(net_classes, dict) and net_classes:
                    n_classes = self.store.pcb_upsert_net_classes(
                        ref.id, net_classes, conn=conn
                    )
                if roles:
                    self.store.pcb_upsert_roles(ref.id, roles, conn=conn)
                if seeded:
                    reposed, unplaced = self._legalize_generator_seeds(
                        ref.id, seeded, conn=conn
                    )
                    counts["seeds_legalized"] = reposed
                    counts["seeds_unplaced"] = unplaced
                if tags or untags:
                    # D3 shortcut, same door as the tag verb — inside the
                    # judged transaction so a bad tag rolls the write back.
                    tag_counts["v"] = apply_tag_ops(
                        self.store,
                        "pcb",
                        ref.id,
                        tags=tags,
                        untags=untags,
                        conn=conn,
                    )
                return ref, created, counts, n_classes

            (ref, created, counts, n_classes), judged = self._judged_mutation(
                existing.id if existing is not None else None,
                apply,
                ref_id_of=lambda r: int(r[0].id),
            )
        except BadInput:
            # BadInput is also a ValueError (gr514202); keep its own hint.
            raise
        except ValueError as exc:
            raise BadInput(
                f"pcb: {exc}",
                next="correct the named argument and retry the same put — "
                "nothing was stored",
            ) from exc

        # After the commit, never inside the judged transaction: queue the
        # datasheet pulls + extractions (jobs; nothing is fetched here).
        queued, facts_queued = self._queue_datasheets(ref.id)

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

        tag_line = ""
        if tag_counts.get("v"):
            tag_line = f"\ntags: +{tag_counts['v'][0]}, -{tag_counts['v'][1]}"

        verb = "created" if created else "extended"
        design = self.store.pcb_load(ref.id)
        extra = f", +{counts['measures']} measure(s)" if counts["measures"] else ""
        if counts["features"]:
            extra += f", +{counts['features']} feature(s)"
        if counts.get("outline_replaced"):
            extra += ", replaced the existing outline (a board has one)"
        if counts.get("footprints"):
            extra += f", +{counts['footprints']} footprint(s)"
        if counts.get("generators"):
            extra += f", {counts['generators']} generator(s) applied"
        if counts.get("seeds_legalized"):
            extra += (
                f", {counts['seeds_legalized']} generator-seeded part(s) "
                "legalised to a valid pose"
            )
        if counts.get("seeds_unplaced"):
            extra += (
                f", {counts['seeds_unplaced']} generator-seeded part(s) left "
                "unplaced (their seed pose was invalid; op='place' owns "
                "finding one)"
            )
        # An annotation pass re-puts nets that already exist, so +0 net(s)
        # is the NORMAL result and says nothing about whether the edit
        # landed. Report the patch separately or it reads as a no-op
        # (gr457053).
        if counts.get("nets_patched"):
            extra += f", {counts['nets_patched']} net(s) patched"
        if counts.get("pins_completed"):
            extra += (
                f", +{counts['pins_completed']} pin(s) completed from the "
                "cached footprint (Slice 2 declare-all)"
            )
        if n_classes:
            extra += f", +{n_classes} net_class(es)"
        mapped_line = (
            f"{len(mapped)} connection pin(s) given as pad numbers were wired "
            "to the footprint's pin names (gr495291): " + ", ".join(mapped) + "\n"
            if mapped
            else ""
        )
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
            + (tag_line + "\n" if tag_line else "")
            + mapped_line
            + "".join(n + "\n" for n in notes)
            + (f"{queued} datasheet pull(s) queued\n" if queued else "")
            + (
                f"{facts_queued} datasheet facts extraction(s) queued\n"
                if facts_queued
                else ""
            )
            + self._put_echo(ref.id)
            + self._toc(design)
        )

    def _put_tags(
        self, ref_id: int, tags: list[str] | None, untags: list[str] | None
    ) -> str:
        """``put(tags=/untags=)`` on the op path — the same D3 shortcut the
        batch write applies inside its transaction; the op has already
        committed here, so the ops run in their own."""
        n_add, n_rm = apply_tag_ops(self.store, "pcb", ref_id, tags=tags, untags=untags)
        return f"\ntags: +{n_add}, -{n_rm}"

    def _put_echo(self, ref_id: int) -> str:
        """What the put left stored that the TOC does not show: the §1b
        supply roles (declared ``roles``, each part's ``supplies``, a client
        count from pins' ``client_of``) and a one-line netlist summary —
        so the caller sees the effect without a second get (gr491374)."""
        g = self.store.pcb_graph(ref_id)
        suppliers: dict[str, list[str]] = {}
        for inst in g["instances"]:
            for s in inst.get("supplies") or []:
                n_blocks = len(s.get("blocks") or [])
                suppliers.setdefault(str(s.get("role")), []).append(
                    f"{inst['refdes']}({n_blocks} block(s))"
                )
        clients: collections.Counter[str] = collections.Counter()
        pins = [m for n in g["nets"] for m in n["members"]] + g["unconnected"]
        for p in {(p["refdes"], p["pin"]): p for p in pins}.values():
            client_of = (p.get("meta") or {}).get("client_of")
            if isinstance(client_of, dict) and client_of.get("role"):
                clients[str(client_of["role"])] += 1
        roles = g["roles"]
        out = ""
        names = sorted(set(roles) | set(suppliers) | set(clients))
        if names:
            rows = [
                {
                    "role": name,
                    "voltage_v": (
                        "undeclared"
                        if name not in roles
                        else "—"
                        if roles[name]["voltage_v"] is None
                        else f"{roles[name]['voltage_v']:g}"
                    ),
                    "fanout": (
                        "—"
                        if roles.get(name, {}).get("fanout") is None
                        else str(roles[name]["fanout"])
                    ),
                    "supplied_by": ",".join(suppliers.get(name, [])) or "—",
                    "clients": str(clients[name]),
                }
                for name in names
            ]
            out += (
                "## supply roles\n"
                + render_agent_table(
                    rows,
                    schema=["role", "voltage_v", "fanout", "supplied_by", "clients"],
                )
                + "\n"
            )
        marked = sum(
            pcb_drc._pin_marker(p) in ("nc", "unused") for p in g["unconnected"]
        )
        by_class = collections.Counter(n["net_class"] or "—" for n in g["nets"])
        classes = ", ".join(f"{c}:{k}" for c, k in sorted(by_class.items()))
        out += (
            f"## netlist: {len(g['nets'])} net(s)"
            + (f" (by class {classes})" if classes else "")
            + f", {len(g['unconnected'])} netless pin(s)"
            + (f" ({marked} marked nc/unused)" if marked else "")
            + "\n"
        )
        return out

    def _queue_datasheets(self, ref_id: int, *, force: bool = False) -> tuple[int, int]:
        """Enqueue a ``datasheet_pull`` job per C-number on the board that has
        no datasheet and no recorded attempt, plus a ``datasheet_facts``
        extraction per part that has (or is pulling) a datasheet but no
        fact block yet (:mod:`precis.pcb.datasheets`); returns
        ``(pulls_queued, facts_queued)``."""
        job = self.hub.sibling("job")
        return (
            pcb_datasheets.enqueue_pulls(self.store, job, ref_id, force=force),
            pcb_datasheets.enqueue_facts(self.store, job, ref_id),
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
        if view in ("pinout", "pinout-preview"):
            selector = str(id or "").strip()
            if selector.count("#") != 1 or "@" in selector:
                raise BadInput(
                    f"{view} requires one board#REFDES selector",
                    next=f"get(kind='pcb', id='<board>#<REFDES>', view={view!r})",
                )
            slug, refdes = (part.strip() for part in selector.split("#", 1))
            if not slug or not refdes:
                raise BadInput(
                    f"{view} requires a nonempty board and REFDES",
                    next=f"get(kind='pcb', id='<board>#<REFDES>', view={view!r})",
                )
            ref = resolve_live_slug_ref(self.store, kind="pcb", id=slug)
            proposals = self._preview_pins(args) if view == "pinout-preview" else None
            return self._render_pinout(ref, refdes, proposals=proposals)
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
            canon = _VIEW_ALIASES.get(view.strip().lower())
            readings = [f"view={view!r} read as view={canon!r}"] if canon else []
            return interpreted(
                self._render_view(ref.id, canon or view, args or {}), readings
            )
        design = self.store.pcb_load(ref.id)
        head = (
            f"# {ref.slug} — {len(design['instances'])} part(s), "
            f"{len(design['nets'])} net(s)"
        )
        tags = self.store.tags_for(ref.id)
        if tags:
            head += "\ntags: " + " ".join(str(t) for t in tags)
        return Response(body=head + "\n" + self._toc(design))

    # ── auto-place ─────────────────────────────────────
    def _place_and_store(
        self,
        ref_id: int,
        *,
        iters: int,
        seed: int,
        measures: list[dict[str, Any]],
        legal_only: bool = False,
    ) -> tuple[place.PlaceResult, int]:
        """Anneal over the current graph and persist the placement + the
        ``last_place`` meta stamp. Shared by the put-time autoplace and the
        route round-trip's per-pass re-place (one copy of the store logic).
        With ``legal_only`` the anneal's poses are judged by
        :meth:`_refuse_illegal_placement` first and nothing is written
        when any conflicts."""
        graph = pcb_session.sorted_graph(self._effective_graph(ref_id))
        res = place.autoplace(
            graph["instances"],
            graph["nets"],
            measures=measures,
            iters=iters,
            seed=seed,
            features=self.store.pcb_features_list(ref_id),
        )
        if legal_only:
            self._refuse_illegal_placement(ref_id, graph, res.positions)
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

    def _refuse_illegal_placement(
        self,
        ref_id: int,
        graph: dict[str, Any],
        positions: dict[str, tuple[float, float]],
    ) -> None:
        """Refuse a whole-board placement any re-placed part of which
        :meth:`precis.pcb.optimize.OptimizeEngine.pose_conflicts` rejects
        (courtyard on a part or a mounting hole, solder land on an
        authored via) — the rule ``op='place'`` and ``op='move'`` hold, on
        an engine built over the board as ``positions`` leave it. The
        legacy annealer behind ``view='route'`` (:func:`place.autoplace`)
        does not check these, so its poses are judged here before the
        store sees them. As with ``op='move'``, a conflict the stored board
        already has may stand; only a pair the re-place introduces is
        refused, each named once."""

        def conflicts(g: dict[str, Any]) -> dict[tuple[str, str, str], str]:
            ir = self._build_ir(ref_id, g, with_fixed_copper=True)
            ir.outline = self._move_outline(ref_id)
            engine = pcb_optimize.OptimizeEngine(ir, pcb_optimize.OptimizeConfig())
            found: dict[tuple[str, str, str], str] = {}
            for idx, refdes in enumerate(ir.instance_refdes):
                x, y = float(ir.inst_x[idx]), float(ir.inst_y[idx])
                if str(refdes) not in positions or math.isnan(x) or math.isnan(y):
                    continue
                for rule, other in engine.pose_conflicts(idx, x, y):
                    a, b = sorted((str(refdes), other))
                    found.setdefault((rule, a, b), f"{rule} {refdes} with {other}")
            return found

        placed = {
            **graph,
            "instances": [
                {**i, "x": positions[rd][0], "y": positions[rd][1]}
                if (rd := str(i["refdes"])) in positions
                else i
                for i in graph["instances"]
            ],
        }
        standing = conflicts(graph)
        new = [msg for key, msg in conflicts(placed).items() if key not in standing]
        if new:
            raise BadInput(
                "pcb: the re-place would leave an invalid board, so nothing "
                "was written: " + "; ".join(new),
                next=(
                    "re-run view='route' with another iters, add area, or run "
                    "put(args={'op':'place'}) and route in-tree with "
                    "put(args={'op':'route'})"
                ),
            )

    def _legalize_generator_seeds(
        self, ref_id: int, seeded: list[str], *, conn: Any
    ) -> tuple[int, int]:
        """docs/backlog/pcb-global-plan-router.md slice 5b (gr481705): a
        generator-emitted x/y/rot is a SEED for the placer, not placement
        truth — but until now the always-valid gate judged that seed raw,
        so an ``ewod_pad_array`` sink whose centroid lands on the array's
        own plaza vias made the whole put refuse with
        ``invariant:no_via_in_pad`` even though a legal pose exists (the
        anneal finds one — the legal islands can be fractions of a
        lattice step wide on a dense via field, which the ring search
        cannot land on). The seed can't compute that itself: the
        expansion is part-agnostic and never sees the sink's real lands.

        So a freshly-seeded instance gets, BEFORE the judge runs, the
        same :meth:`precis.pcb.optimize.OptimizeEngine.legalize_start`
        slide the anneal opens with — over the board as this put left it,
        authored vias hydrated so ``via_pad_keepout`` binds. A seed the
        ring search cannot repair is then UNPLACED (x/y cleared) when the
        surviving conflict is the generator's own authored vias — the
        seed can never verify that pose, so deferring it to
        ``op='place'``'s seed+legalise+anneal (which owns finding poses)
        is the honest degrade: unplaced is the one incomplete-but-valid
        state the gate permits. A courtyard/hole conflict instead lands
        for the judge to refuse as before — a pose-vs-other-parts clash
        is authored arrangement, not seed blindness. And a part that
        ASSERTS its pose (``fixed`` xy) is never touched here either —
        the judged wrapper refuses it with the invariant's own witness,
        as ruled.

        Only the just-seeded refdes are written back (plus any
        rigid-group member a seeded instance shares its body with — a
        group moves as one, persisting a slice would tear it). A foreign
        part ``legalize_start`` also shifted stays hypothetical; a
        carried-forward pose is committed placement and is never touched
        here. And a seed whose footprint is not yet cached is left alone
        entirely: under synthesized lands a ``via_pad_keepout`` conflict
        is a guess, and sliding or unplacing a pose the judge accepts on
        a guess is a regression, not a legalisation (the ring-fixture
        route-op test's seed reads as conflicting until its footprint
        lands). Returns ``(reposed, unplaced)`` counts."""
        graph = self.store.pcb_graph(ref_id)
        ir = self._build_ir(ref_id, graph, with_fixed_copper=True)
        ir.outline = self._move_outline(ref_id)
        # gr491326: a seed must clear a foreign-net authored via by the
        # pair's net floor, the ERROR tier the judge applies right after
        # this: on an HV EWOD board the hv class's min_clearance_mm, which
        # the generator resolves from the coated (parylene, IPC B4)
        # column. Same-net pairs owe it nothing.
        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is not None:
            try:
                capability = pcb_session.board_capability(
                    board["stackup"], pcb_profiles.board_profile(board.get("meta"))
                )
            except ValueError as exc:
                raise BadInput(
                    f"pcb: {exc}",
                    next="args={'op':'stackup','preset':'4l-two-sided'} — v1 "
                    "supports 2- or 4-layer boards",
                ) from exc
            ir.net_clearance_floor_mm = pcb_session.net_clearance_floors(
                design, capability
            )
        idx = {str(r): i for i, r in enumerate(ir.instance_refdes)}
        seed_set = set(seeded)
        # Per-instance: did ``land_rects_by_instance`` hydrate REAL pads,
        # or the pin-derived synthesized bound? The same ``by_refdes``
        # drives ``apply_real_pin_offsets``, so a synthesized pin offset
        # is the honest signal. Mask synthesized-lands seeds out of the
        # legalise search — ``inst_fixed_xy`` is exactly the "leave this
        # pose alone" bit ``legalize_start`` honours, and the unplace
        # fallback below skips it for the same reason.
        for i in range(ir.n_instances):
            pins = ir.pin_instance == i
            if not bool(pins.any()) or bool(ir.pin_offsets_synthesized[pins].all()):
                ir.inst_fixed_xy[i] = True
        engine = pcb_optimize.OptimizeEngine(ir, pcb_optimize.OptimizeConfig())
        moved = set(engine.legalize_start())
        groups = {
            int(ir.inst_group[idx[rd]])
            for rd in moved & seed_set
            if int(ir.inst_group[idx[rd]]) >= 0
        }
        persist = {
            rd
            for rd in moved
            if rd in idx and (rd in seed_set or int(ir.inst_group[idx[rd]]) in groups)
        }
        for rd in persist:
            i = idx[rd]
            self.store.pcb_move_instance(
                ref_id,
                rd,
                x=float(ir.inst_x[i]),
                y=float(ir.inst_y[i]),
                rot=float(ir.inst_rot[i]),
                conn=conn,
            )
        unplaced = 0
        for rd in seed_set:
            si = idx.get(rd)
            if si is None:
                continue
            x, y = float(ir.inst_x[si]), float(ir.inst_y[si])
            if math.isnan(x) or math.isnan(y) or bool(ir.inst_fixed_xy[si]):
                continue
            if any(
                rule == "via_pad_keepout" for rule, _ in engine.pose_conflicts(si, x, y)
            ):
                conn.execute(
                    "UPDATE pcb_instances SET x = NULL, y = NULL "
                    "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL",
                    (ref_id, rd),
                )
                unplaced += 1
        return len(persist), unplaced

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
        if op == "phase":
            return self._op_phase(ref, args)
        if op == "profile":
            return self._op_profile(ref, args)
        if op == "pins":
            return self._op_pins(ref, args)
        if op in ("pin_rename", "pin_remove"):
            return self._op_pin_edit(ref, op, args)
        if op == "corner_nuts":
            return self._op_corner_nuts(ref, args)
        if op == "stackup":
            return self._op_stackup(ref, args)
        if op == "footprint":
            return self._op_footprint(ref, args)
        if op == "order":
            return self._op_order(ref, args)
        if op == "review":
            return self._op_review(ref, args)
        if op == "check":
            # gr491591: the one explicit way to RECORD a check run — the
            # get views compute the same run and write nothing.
            check_args = {k: v for k, v in args.items() if k != "op"}
            return self._render_check(ref.id, check_args, record=True)
        if op == "datasheets":
            n, nf = self._queue_datasheets(ref.id, force=bool(args.get("force")))
            return Response(
                body=f"{ref.slug}: {n} datasheet pull(s) queued, "
                f"{nf} facts extraction(s) queued"
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
        snap = pcb_session_store.load_snapshot(self.store, ref.id)
        graph = snap.graph
        stackup = (graph.get("board") or {}).get("stackup") or []
        if len(stackup) != 4:
            raise BadInput(
                f"pcb: {ref.slug!r} has a {len(stackup)}-layer stackup — v1 "
                "place/route only supports the default 4-layer board",
                next="a non-4-layer stackup is out of v1 scope (2-layer "
                "boards route power like signals, a different problem)",
            )
        if op == "route":
            # Reto 2026-10-20: a board must have a closed outline,
            # otherwise nothing goes.
            defect = pcb_session.outline_defect(self._outline_from_features(ref.id))
            if defect is not None:
                raise BadInput(
                    f"pcb: refusing to route {ref.slug!r} — {defect}",
                    next=pcb_session.OUTLINE_FIX_HINT.replace("<slug>", ref.slug),
                )
            # gr480010: a stale expansion used to ride along with only the
            # warning below — job 479998 routed ewod-dogfood-6 over ARR1
            # copper stored at v3 while the code emitted v4, and committed
            # the degraded result (10→76 DRC errors, 4→38 failed nets).
            # Routing a board whose expansion is behind the code is now
            # refused outright; re-putting the entry re-expands it at the
            # current version.
            stale_rows = pcb_generators.stale_generators(
                self.store.pcb_generators_for(ref.id)
            )
            if stale_rows:
                raise BadInput(
                    f"pcb: refusing to route {ref.slug!r} — generator "
                    "expansion(s) are stale; the board carries copper an "
                    "older build emitted:\n"
                    + "\n".join(
                        f"- {name} ({gen}): stored version {stored}, code "
                        f"version {current}"
                        for name, gen, stored, current in stale_rows
                    ),
                    next=(
                        "put the same generators entry again — the version "
                        "mismatch re-expands it at the current code (its "
                        "placement and routing are redone) — then re-route"
                    ),
                )
        params: dict[str, Any] = {"pcb_ref_id": ref.id}
        if opts.get("iters") is not None:
            params["iters"] = int(opts["iters"])
        if opts.get("seed") is not None:
            params["seed"] = int(opts["seed"])
        if op == "place" and opts.get("snap") is not None:
            params["snap"] = bool(opts["snap"])
        # Opt-in support-passive organisation (optimize.OptimizeConfig.
        # support_groups): pull-up banks, per-IC decoupling rows and
        # series pairs re-laid as organised rows post-anneal.
        if op in ("place", "route") and opts.get("groups") is not None:
            params["groups"] = bool(opts["groups"])
        # Opt-in field polish (optimize.OptimizeConfig.field_polish, off
        # until the silk gate lands): forwarded only when given, so the
        # config default stays the one switch.
        if op in ("place", "route") and opts.get("field_polish") is not None:
            params["field_polish"] = bool(opts["field_polish"])
        if op == "route" and opts.get("warm_start") is not None:
            warm_start = opts["warm_start"]
            if warm_start not in ("distance", "radial"):
                raise BadInput(
                    "pcb: warm_start must be distance or radial",
                    options=["distance", "radial"],
                    next="put(args={'op':'route','warm_start':'distance'})",
                )
            params["warm_start"] = warm_start
        # Removed grid-router knobs (negotiate/corridor/escape/layer_cost/
        # off_axis/cleanup): still forwarded — the job collects them into
        # the run's `ignored` list rather than refusing a stale caller.
        if op == "route":
            for knob in (
                "negotiate",
                "corridor",
                "escape",
                "layer_cost",
                "off_axis",
            ):
                if opts.get(knob) is not None:
                    params[knob] = opts[knob]
        # Router selector — 'vector' (the only router); anything else
        # fails at the job with a legible input error.
        if op == "route" and opts.get("router") is not None:
            params["router"] = str(opts["router"])
        # Opt-in route -> penalise -> anneal -> route outer loop
        # (optimize.anneal_route_loop); off unless asked for.
        if op == "route" and opts.get("loop") is not None:
            loop = int(opts["loop"])
            cap = pcb_optimize.LOOP_MAX_ROUNDS
            if not 0 <= loop <= cap:
                raise BadInput(
                    f"pcb: loop={loop} is out of range; give 0 (off) "
                    f"to {cap} route rounds.",
                    next=f"put(args={{'op':'route','loop':{cap}}})",
                )
            params["loop"] = loop
        # Authored expansion bounds for the loop's priced moves ride the
        # design's own meta (put(args={'meta': {'expansion_caps': {...}}})
        # — see precis-pcb-route-help). Forwarded verbatim into the job
        # params so the dedup hash sees them; the job validates the shape.
        if op == "route" and params.get("loop"):
            caps = (ref.meta or {}).get("expansion_caps")
            if caps is not None:
                params["expansion_caps"] = caps
        # Override for the pre-route placement DRC gate; absent unless True.
        if op in ("place", "route") and opts.get("force") is not None:
            if not isinstance(opts["force"], bool):
                raise BadInput(
                    "pcb: force must be a boolean (True or False)",
                    next="put(args={'op':'route','force':True})",
                )
            if opts["force"]:
                params["force"] = True
        # Route clean-up post-pass (RealizeConfig.route_cleanup, gr495397);
        # on unless turned off, so only `False` is forwarded.
        if op == "route" and opts.get("cleanup") is not None:
            if not isinstance(opts["cleanup"], bool):
                raise BadInput(
                    "pcb: cleanup must be a boolean (True or False)",
                    next="put(args={'op':'route','cleanup':False})",
                )
            if not opts["cleanup"]:
                params["cleanup"] = False
        # Route-only run (gr495397): `place=False` keeps every pose and the
        # stored sketch and only routes; absent/True = the joint place+route.
        if op == "route" and opts.get("place") is not None:
            if not isinstance(opts["place"], bool):
                raise BadInput(
                    "pcb: place must be a boolean (True or False)",
                    next="put(args={'op':'route','place':False})",
                )
            if not opts["place"]:
                if opts.get("loop"):
                    raise BadInput(
                        "pcb: place=False cannot combine with loop (the loop "
                        "re-anneals between rounds)",
                        next="put(args={'op':'route','place':False})",
                    )
                params["place"] = False
        # Raw store rows, verbatim -- `content_hash` itself does the
        # narrowing (job-written-field exclusion, authored-vs-derived
        # filtering) so there is exactly one place that can get it wrong.
        # The resolved netlist snapshot is job-written (Slice 7): like
        # derived pin swaps before it, it never enters the digest, so
        # `load_snapshot` does not carry it. The workers read the same
        # `load_snapshot`, so the hashed list and the read list are one.
        digest = pcb_session.content_hash(
            graph, params, session_state=snap.session_state()
        )
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
        if self._claiming_worker_live():
            job_line = job_resp.body
            drain_note = (
                f"Runs on the cluster worker's code, not necessarily this "
                f"session's ({code_stamp()}) — the job's `ran_on:` line records "
                "which build actually ran.\n"
            )
        else:
            # A scratch/test store: the job's numeric id is a local row
            # nothing else can poll — `get(kind='job', id=N)` would hit
            # the session's own store, not this one (gr513874). Name it
            # a ref_id and let the drain note say how it runs.
            verb = "existing" if job_resp.reused else "created"
            job_line = (
                f"{verb} job ref_id={job_resp.ref_id} "
                f"(job_type='pcb_{op}', queued on this store)"
            )
            drain_note = (
                "No live worker on this store — nothing claims the job until "
                "something drains it in-process "
                "(precis.workers.executors.job_inproc.run_job_inproc_pass), "
                "which runs the draining process's own code.\n"
            )
        return (
            f"# {op} {ref.slug} — enqueued\n{job_line}\n\n"
            + (stale + "\n\n" if stale else "")
            + drain_note
            + f"Next: get(kind='pcb', id='{ref.slug}', view='{status_view}') "
            "to check progress once the job lands."
        )

    def _claiming_worker_live(self) -> bool:
        """True when some host's ``host_heartbeat`` row is fresh AND
        advertises a minted ``meta.boot_ids`` entry — i.e. a real claiming
        worker process is alive, so the job may run on a different build
        than this session's (gr513874). A local/scratch/test store has no
        heartbeats at all: nothing but an in-process
        ``run_job_inproc_pass`` will ever drain the job there, and the
        "cluster worker's code" caveat would be wrong."""
        from precis.workers.nursery import HOST_DARK_SILENCE_MIN

        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM host_heartbeat "
                "WHERE ts > now() - make_interval(mins => %s) "
                "AND meta->'boot_ids' <> '{}'::jsonb LIMIT 1",
                (HOST_DARK_SILENCE_MIN,),
            ).fetchone()
        return row is not None

    def _op_review(self, ref: Any, args: dict[str, Any]) -> Response:
        """``put(args={'op':'review', 'item'?, 'limit'?})`` — pcb-datasheet-
        review slice 7: enqueue a ``pcb_review`` job over the
        ``pcb-tapeout`` judgment items that are not checked or stale. The
        job runs the check surface fresh itself (:mod:`precis.pcb.
        check_run`, persisted, ERC included) and quotes the run by id; this
        op runs no check. Nothing queued → no job: a settled design costs
        no model call."""
        design = self.store.pcb_load(ref.id)
        if design["board"] is None:
            raise BadInput(
                f"pcb: {ref.slug!r} has no board yet — nothing to review",
                next="put(kind='pcb', id='slug', args={'components':[...],"
                "'nets':[...]}) to create the design",
            )
        item = str(args.get("item") or "").strip() or None
        params: dict[str, Any] = {"pcb_ref_id": ref.id}
        if item is not None:
            params["item"] = item
        if args.get("limit") is not None:
            limit = int(args["limit"])
            if not 1 <= limit <= 60:
                raise BadInput(
                    "pcb: review limit must be 1..60 items",
                    next="put(args={'op':'review','limit':20})",
                )
            params["limit"] = limit
        q = pcb_review.queue(self.store, ref.id, item=item)
        if q.reason is not None:
            raise BadInput(
                f"pcb: {ref.slug}: {q.reason}",
                next=[
                    f"edit(kind='checklist', id='{pcb_review.CHECKLIST}', "
                    f"op='assign', target='pcb:{ref.slug}')",
                    "if the checklist itself is missing it is created by the "
                    "checklist_sync job — file a gripe if it stays absent",
                ],
            )
        status_next = (
            f"Next: get(kind='checklist', id='{pcb_review.CHECKLIST}', "
            f"target='pcb:{ref.slug}')"
        )
        if item is not None and not any(st.name == item for st in q.statuses):
            raise BadInput(
                f"pcb: {item!r} is not an item on {pcb_review.CHECKLIST}",
                options=[st.name for st in q.statuses],
            )
        work = list(q.todo)
        skipped = sum(1 for st in q.statuses if st.status == NOT_CHECKABLE)
        if not work:
            return Response(
                body=f"# review {ref.slug} — nothing to review\n"
                f"every judgment item is settled ({skipped} not checkable at "
                "the current inputs); no job queued.\n" + status_next
            )
        # Idempotent per (design, queue state): re-submitting while the
        # same items are pending collapses onto the in-flight job.
        queued = sorted(f"{st.name}@{st.item['rev']}:{st.status}" for st in work)
        digest = pcb_session.content_hash(
            self.store.pcb_graph(ref.id), {**params, "queue": queued}
        )
        job_resp = self.hub.sibling("job").put(
            job_type="pcb_review",
            executor="job_inproc",
            parent_id=ref.id,
            params=params,
            idem_key=f"pcb_review:{ref.id}:{digest}",
        )
        return Response(
            body=f"# review {ref.slug} — enqueued ({len(work)} item(s) not "
            f"checked or stale)\n{job_resp.body}\n\n"
            "The job runs a fresh check (DRC + ERC) first and quotes it by id.\n"
            f"{status_next} once the job lands — see precis-pcb-review-help."
        )

    @staticmethod
    def _pose_kwargs(src: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        for k in ("x", "y"):
            if src.get(k) is not None:
                kwargs[k] = float(src[k])
        for k in ("rot_deg", "rot"):  # degrees, or an explicit-unit string
            if src.get(k) is not None:
                kwargs["rot"] = parse_angle_deg(src[k], arg_name=k, bare="degrees")
                break
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
        if "max_travel_mm" in args:
            # explicit null clears the soft pin (the column -> NULL)
            kwargs["max_travel_mm"] = self._parse_max_travel(args["max_travel_mm"])
        if "label_spot" in args:
            # gr496552: the refdes label spot override lives on instance
            # meta ("we can move it"); explicit null clears it.
            spot = args["label_spot"]
            if spot is not None and str(spot) not in pcb_silk.LABEL_SPOT_ORDER:
                raise BadInput(
                    f"pcb: label_spot must be one of "
                    f"{', '.join(pcb_silk.LABEL_SPOT_ORDER)} (got {spot!r}); "
                    "null clears the override",
                    next=f"args={{'op':'move','refdes':'{refdes}',"
                    "'label_spot':'top-right'}}",
                )
            kwargs["meta"] = {"label_spot": None if spot is None else str(spot)}
        kwargs.update(self._move_lock_kwargs(ref, refdes, args))
        if not kwargs:
            raise BadInput(
                "op='move' needs at least one of x / y / rot / fixed / "
                "max_travel_mm / side / fixed_side / group_fixed / room_id / "
                "label_spot",
                next="args={'op':'move','refdes':'U1','fixed':'xy'}",
            )
        if {"x", "y", "rot"} & kwargs.keys() or "layer" in kwargs:
            group_body = (
                self._move_poses(ref, [(refdes, kwargs)], single_form=True)
                if {"x", "y", "rot"} & kwargs.keys()
                else None
            )
            if group_body is not None:
                # the rigid-group path writes poses via pcb_move_groups,
                # which carries `fixed` as `lock` but has no arm for the
                # lock columns — the soft pin, side and the other columns
                # apply here.
                extra = {k: v for k, v in kwargs.items() if k in _MOVE_COLUMN_KEYS}
                if "meta" in kwargs:
                    extra["meta"] = kwargs["meta"]
                if extra:
                    self.store.pcb_move_instance(ref.id, refdes, **extra)
                return Response(body=group_body)
            standing = self._refuse_illegal_pose(ref, refdes, kwargs)
        else:
            standing = []
        ok = self.store.pcb_move_instance(ref.id, refdes, **kwargs)
        if not ok:
            raise NotFound(
                f"pcb instance {refdes!r} not found in {ref.slug!r}",
                next=f"get(kind='pcb', id='{ref.slug}') lists this design's refdes",
            )
        body = f"# {refdes} moved — {kwargs}"
        if standing:
            body += (
                f"\nboard still has {len(standing)} pre-existing DRC error(s) the "
                "move did not add: " + "; ".join(standing[:5])
            )
        return Response(body=body)

    @staticmethod
    def _parse_max_travel(pin: Any) -> float | None:
        """The soft pin as mm: a finite number >= 0, or ``None`` (clear)."""
        if pin is None:
            return None
        if isinstance(pin, bool):
            raise BadInput(
                f"pcb: max_travel_mm must be a number of mm >= 0 (got {pin!r})",
                next="max_travel_mm: a number of mm >= 0, or null to clear the pin",
            )
        try:
            pin_mm = float(pin)
        except (TypeError, ValueError):
            raise BadInput(
                f"pcb: max_travel_mm must be a number of mm >= 0 (got {pin!r})",
                next="max_travel_mm: a number of mm >= 0, or null to clear the pin",
            ) from None
        if not math.isfinite(pin_mm) or pin_mm < 0:
            raise BadInput(
                f"pcb: max_travel_mm must be a number of mm >= 0 "
                f"(got {pin!r}); null clears the pin",
                next="max_travel_mm: a number of mm >= 0, or null to clear the pin",
            )
        return pin_mm

    def _move_lock_kwargs(
        self, ref: Any, refdes: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate ``op='move'``'s side / lock arguments (``side``,
        ``fixed_side``, ``group_fixed``, ``room_id``) and return the
        ``pcb_move_instance`` kwargs they map to (``layer`` for ``side``).
        Raises before anything is written."""
        out: dict[str, Any] = {}
        for key in ("fixed_side", "group_fixed"):
            if key in args:
                if not isinstance(args[key], bool):
                    raise BadInput(
                        f"pcb: {key} must be a boolean, true or false "
                        f"(got {args[key]!r})",
                        next=f"args={{'op':'move','refdes':'{refdes}','{key}':True}}",
                    )
                out[key] = args[key]
        if "side" in args:
            side = args["side"]
            if side not in ("top", "bottom"):
                raise BadInput(
                    f"pcb: side must be 'top' or 'bottom' (got {side!r})",
                    options=["top", "bottom"],
                    next=f"args={{'op':'move','refdes':'{refdes}','side':'bottom'}}",
                )
            out["layer"] = side
        if "room_id" in args:
            room = args["room_id"]
            if room is None:
                out["room_id"] = None
            else:
                if isinstance(room, bool) or not isinstance(room, int):
                    raise BadInput(
                        f"pcb: room_id must be an integer feature id or null "
                        f"(got {room!r})",
                        next="room_id: the integer feature_id of a 'room' "
                        "feature, or null to unassign",
                    )
                feature = next(
                    (
                        f
                        for f in self.store.pcb_features_list(ref.id)
                        if f["feature_id"] == room
                    ),
                    None,
                )
                if feature is None:
                    raise BadInput(
                        f"pcb: no live feature {room} on this board",
                        next="author a room first: put(args={'features':"
                        "[{'ftype':'room',...}]})",
                    )
                if feature["ftype"] != "room":
                    raise BadInput(
                        f"pcb: feature {room} is a {feature['ftype']!r}, not a room",
                        next="room_id must name a feature with ftype='room' — "
                        "author one via put(args={'features':[{'ftype':'room',...}]})",
                    )
                out["room_id"] = room
        if "side" in args:
            inst = next(
                (
                    i
                    for i in self.store.pcb_load(ref.id)["instances"]
                    if i["refdes"] == refdes
                ),
                None,
            )
            if inst is None:
                raise NotFound(
                    f"pcb instance {refdes!r} not found in {ref.slug!r}",
                    next=f"get(kind='pcb', id='{ref.slug}') lists this design's refdes",
                )
            if self._is_through_hole(ref, inst):
                raise BadInput(
                    f"pcb: {refdes} is a through-hole part (its footprint has "
                    "drilled pads, which exist on both sides); side= only "
                    "applies to surface-mount parts",
                    next=f"omit 'side' for {refdes} — a through-hole part is "
                    "already on both sides",
                )
            gens = self.store.pcb_generators_for(ref.id)
            gi: dict[str, Any] = next(
                (
                    i
                    for i in self.store.pcb_graph(ref.id)["instances"]
                    if i["refdes"] == refdes
                ),
                {},
            )
            in_generator = any(
                refdes == g.get("refdes") or refdes.startswith(n + "_")
                for n, g in gens.items()
            )
            if gi.get("group") or in_generator:
                raise BadInput(
                    f"pcb: {refdes} belongs to a "
                    f"{'generator' if in_generator else 'rigid'} group; a group "
                    "side flip is not supported yet (pcb-stackup-regions slice 5)",
                    next=f"omit 'side' for {refdes} — flipping a whole group "
                    "is not supported",
                )
            unlocking = out.get("fixed_side") is False
            if (
                inst["fixed_side"]
                and not unlocking
                and str(inst["layer"] or "top") != args["side"]
            ):
                raise BadInput(
                    f"pcb: {refdes} is side-locked (fixed_side=true); pass "
                    "fixed_side=false to unlock it",
                    next=f"args={{'op':'move','refdes':'{refdes}','side':"
                    f"'{args['side']}','fixed_side':False}}",
                )
        return out

    def _is_through_hole(self, ref: Any, inst: dict[str, Any]) -> bool:
        """True when the instance's footprint (the design-local row, else
        the cached catalog one — the order :func:`pcb_session.
        footprints_by_refdes` uses) has a drilled pad. A part with no
        footprint on file reads as surface-mount: nothing to refuse on."""
        fp = None
        name = inst.get("footprint")
        if name:
            fp = self.store.pcb_local_footprints_for(ref.id).get(str(name))
        if fp is None and inst.get("part_lcsc"):
            fp = self.store.pcb_footprints_for(ref.id).get(str(inst["part_lcsc"]))
        return any(pad.get("drill") for pad in (fp or {}).get("pads") or [])

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
                raise NotFound(
                    f"pcb instance {refdes!r} not found in {ref.slug!r}",
                    next=f"get(kind='pcb', id='{ref.slug}') lists this design's refdes",
                )
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
                gboard = graph.get("board") or {}
                return pcb_session.board_capability(
                    gboard["stackup"], pcb_profiles.board_profile(gboard.get("meta"))
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise BadInput(
                    f"pcb: cannot judge {label}'s copper on this board — {exc}",
                    next="args={'op':'stackup','preset':'4l-two-sided'} — v1 "
                    "supports 2- or 4-layer boards",
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
            raise NotFound(
                str(exc),
                next=f"get(kind='pcb', id='{ref.slug}') lists this design's "
                "live refdes — retry with one of those",
            ) from exc
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
        after_engine = engine
        new_layer = pose.get("layer")
        if new_layer is not None and (new_layer == "bottom") != bool(
            ir.inst_bottom[idx]
        ):
            # A side flip: judge the new pose over a board where this part
            # sits on the new layer (its courtyard/keep-outs mirror there).
            flipped = {
                **graph,
                "instances": [
                    {**i, "layer": new_layer} if i["refdes"] == refdes else i
                    for i in graph["instances"]
                ],
            }
            ir_after = self._build_ir(ref.id, flipped, with_fixed_copper=True)
            ir_after.outline = ir.outline
            after_engine = pcb_optimize.OptimizeEngine(
                ir_after, pcb_optimize.OptimizeConfig()
            )
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
            [((after_engine, idx, refdes), (x, y, 0.0 if math.isnan(rot) else rot))],
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
            raise NotFound(
                f"net {net!r} not found in {ref.slug!r}",
                next=f"get(kind='pcb', id='{ref.slug}') lists this design's nets",
            )
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
        # One net per REGION of a layer (pcb-stackup-regions slice 4): an
        # authored plane covers the whole board, so it cannot share a
        # layer with another net's whole-board plane — a hard constraint,
        # not a price. Without this, a second op='plane_net' on the same
        # layer was accepted, and planes.plane_pours' "lowest net id wins"
        # tie-break then silently dropped one of them: the caller's
        # instruction was stored, agreed to, and never appeared on the
        # board. A row with a region (a derived power region, an imported
        # fill) shares the layer by priority and is no conflict.
        conflict = next(
            (
                row
                for row in self.store.pcb_planes_list(ref.id)
                if row["layer"] == layer
                and row["net"] != net
                and row.get("region") is None
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
        priority = args.get("priority")
        if priority is not None and (
            isinstance(priority, bool) or not isinstance(priority, int)
        ):
            raise BadInput(
                f"pcb: priority must be an integer (got {priority!r})",
                next="args={'op':'plane_net','layer':'In1.Cu','net':'GND',"
                "'priority':10}",
            )
        plane_id = self.store.pcb_assign_plane(ref.id, layer, net, priority=priority)
        if not plane_id:
            raise NotFound(
                f"net {net!r} not found in {ref.slug!r}",
                next=f"get(kind='pcb', id='{ref.slug}') lists this design's nets",
            )
        return Response(
            body=f"# {net} assigned to plane layer {layer}\n"
            "Takes effect on the next put(args={'op':'route'}) — its pins "
            "fan out (dog-bone) instead of routing."
        )

    def _refuse_bad_clearance_rules(
        self, ref_id: int | None, batch: dict[str, Any]
    ) -> None:
        """Refuse ``clearance_by_layer``/``pair_rules`` that name a layer
        the board's stackup lacks or a class the design does not define
        (vector-router slice 1, P7). ``batch`` is ``{class: rules}``; its
        own names count as defined. A design not created yet is judged
        against the stackup it will get."""
        if not any(
            isinstance(r, dict) and ("clearance_by_layer" in r or "pair_rules" in r)
            for r in batch.values()
        ):
            return
        if ref_id is not None:
            design = self.store.pcb_load(ref_id)
            stackup = (design.get("board") or {}).get("stackup") or DEFAULT_STACKUP
            known = set(design.get("net_classes") or {})
        else:
            stackup, known = DEFAULT_STACKUP, set()
        layers = [str(e.get("name")) for e in stackup]
        classes = known | {str(n) for n in batch}
        problems = [
            line
            for name, rules in batch.items()
            if isinstance(rules, dict)
            for line in clearance_rule_problems(
                str(name), rules, layers=layers, classes=classes
            )
        ]
        if problems:
            raise BadInput(
                "net-class clearance rules refused: " + "; ".join(problems),
                next=(
                    "name copper layers of this board's stackup and net "
                    "classes the design defines; entry keys are "
                    "clearance_mm / min_clearance_mm (+ with / layers in "
                    "pair_rules) — nothing was changed"
                ),
                options=layers,
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
        self._refuse_bad_clearance_rules(ref.id, {name: rules})
        named = rules.get("layers")
        if isinstance(named, list | tuple) and named:
            stackup = (self.store.pcb_load(ref.id)["board"]).get("stackup") or []
            routable = [
                str(e.get("name")) for e in stackup if pcb_ir.layer_is_routable(e)
            ]
            unroutable = [str(n) for n in named if str(n) not in routable]
            if stackup and unroutable:
                raise BadInput(
                    f"class {name!r} names layer(s) {unroutable} that this "
                    f"board's stackup does not route on (routable: {routable}); "
                    "the router would silently drop them",
                    next=(
                        "put(args={'op':'stackup',...}) to make the layer a "
                        "signal layer first, or name only routable layers"
                    ),
                    options=routable,
                )
        try:
            _, judged = self._judged_mutation(
                ref.id,
                lambda conn: self.store.pcb_set_class_rules(
                    ref.id, name, rules, conn=conn
                ),
            )
        except BadInput:
            raise
        except ValueError as exc:
            # A rules value the table cannot honour (e.g. working_voltage_v
            # past IPC-2221B's top band) is bad input, not a judge verdict.
            raise BadInput(
                str(exc),
                next="bring the named rule value into range and retry the "
                "same put — nothing was changed",
            ) from exc
        return Response(
            body=f"# net class {name!r} rules set: {rules}\n" + judged.lines()
        )

    def _op_phase(self, ref: Any, args: dict[str, Any]) -> Response:
        """``put(args={'op':'phase','phase':'netlist|placed|routed|fab'})``
        — the authored design phase (Slice 8), stored on the board's meta
        and read back by :meth:`_design_phase` ahead of the derived
        default. ``fab`` can ONLY be claimed here — no board state derives
        it — and claiming it is what makes fab-tier severities (an
        unmarked netless pin, an unrouted net) report as errors.
        ``phase: null`` clears the claim."""
        raw = args.get("phase")
        if raw is None:
            self.store.pcb_set_board_phase(ref.id, None)
            return Response(
                body=(
                    f"{ref.slug}: phase claim cleared — the check surface "
                    "derives it (netlist/placed/routed from board state; "
                    "fab is never derived)"
                )
            )
        phase = str(raw).strip().lower()
        if phase not in pcb_drc.PHASES:
            raise BadInput(
                f"op='phase' phase={phase!r} not recognized",
                options=list(pcb_drc.PHASES),
            )
        self.store.pcb_set_board_phase(ref.id, phase)
        return Response(
            body=(
                f"{ref.slug}: phase = {phase} (authored — "
                "pcb_boards.meta['phase'])\nNext: get(kind='pcb', "
                f"id='{ref.slug}', view='check')"
            )
        )

    def _op_corner_nuts(self, ref: Any, args: dict[str, Any]) -> Response:
        """``put(args={'op':'corner_nuts'[, 'radius_mm':5.0, ...]})`` — four
        SMT mounting nuts, one per corner of the rectangular outline, each
        centred on the arc that rounds its corner (``corner_radius_mm`` =
        the nut's edge distance). Replaces the outline and any earlier
        corner nuts; other mounting holes stay. See
        :mod:`precis.pcb.mounts`."""
        features = self.store.pcb_features_list(ref.id)
        outline = next(
            (
                f
                for f in reversed(features)
                if f["ftype"] == "outline"
                and isinstance((f.get("geom") or {}).get("path"), list)
            ),
            None,
        )
        if outline is None:
            raise BadInput(
                "op='corner_nuts' needs a rectangular outline feature",
                next="put(args={'features':[{'ftype':'outline','geom':"
                "{'path':[[0,0],[40,0],[40,30],[0,30]]}}]}) first",
            )
        sizes: dict[str, float] = {}
        for key in (
            "radius_mm",
            "hole_dia_mm",
            "ring_dia_mm",
            "body_dia_mm",
            "clearance_mm",
        ):
            if args.get(key) is not None:
                try:
                    sizes[key] = float(args[key])
                except (TypeError, ValueError):
                    raise BadInput(
                        f"op='corner_nuts' {key}={args[key]!r} is not a number",
                        next=f"{key}: a number of mm, or omit it",
                    ) from None
        try:
            new = pcb_mounts.corner_nut_features(outline["geom"]["path"], **sizes)
        except ValueError as exc:
            raise BadInput(
                f"op='corner_nuts': {exc}",
                next="radius_mm / ring_dia_mm / body_dia_mm: mm sizes that fit "
                "the board; the outline must be a plain rectangle",
            ) from None
        old_nuts = [
            f["feature_id"]
            for f in features
            if f["ftype"] == "mounting_hole"
            and (f.get("geom") or {}).get("style") == pcb_mounts.NUT_STYLE
        ]
        self.store.pcb_features_retire(ref.id, old_nuts)
        self.store.pcb_apply(
            slug=ref.slug,
            title=ref.title or ref.slug,
            components=[],
            nets=[],
            connections=[],
            features=new,
        )
        radius = new[0]["geom"]["corner_radius_mm"]
        return Response(
            body=(
                f"{ref.slug}: 4 corner nuts at ({radius:g} mm, {radius:g} mm) "
                f"from each corner, corner_radius_mm = {radius:g} "
                f"({len(old_nuts)} earlier corner nut(s) replaced)\nNext: "
                f"get(kind='pcb', id='{ref.slug}', view='check')"
            )
        )

    @staticmethod
    def _parse_profile(raw: Any) -> str:
        """A board profile name, or ``BadInput`` listing the valid ones."""
        name = str(raw).strip().lower()
        if name not in pcb_profiles.profile_names():
            raise BadInput(
                f"profile={name!r} not recognized",
                options=list(pcb_profiles.profile_names()),
            )
        return name

    def _op_profile(self, ref: Any, args: dict[str, Any]) -> Response:
        """``put(args={'op':'profile','profile':'prod|prototype'})`` — the
        board profile, stored on ``pcb_boards.meta['profile']`` (absent ⇒
        ``prod``). ``prototype`` tunes the layout for hand assembly and
        rework (precis.pcb.profiles); ``prod`` is today's behaviour."""
        if args.get("profile") is None:
            raise BadInput(
                "op='profile' needs args.profile",
                options=list(pcb_profiles.profile_names()),
            )
        profile = self._parse_profile(args["profile"])
        self.store.pcb_set_board_profile(ref.id, profile)
        return Response(
            body=(
                f"{ref.slug}: profile = {profile} — "
                f"{pcb_profiles.profile_rules(profile).label} (authored — "
                "pcb_boards.meta['profile'])\nNext: get(kind='pcb', "
                f"id='{ref.slug}', view='check')"
            )
        )

    def _op_pins(self, ref: Any, args: dict[str, Any]) -> Response:
        """``put(args={'op':'pins','refdes':'U7','pins':[{...}]})`` —
        annotate pin SEMANTICS on an existing instance
        (docs/backlog/pcb-datasheet-review.md slice 2): the ``etype`` /
        ``rail`` / ``max_voltage_v`` / ``vih`` / ``vil`` /
        ``max_current_a`` / ``current_role`` (``series``|``shunt``|
        ``signal``) / ``nc`` / ``unused`` / ``must_terminate`` /
        ``min_dielectric`` / ``source``
        facts the ERC rules read. This is the write path an imported
        board needs: a re-put is create-or-extend and never edits pins,
        so nothing could feed the check surface after import until now.
        Per-pin names are validated against the component's real pins
        (a bad one refuses the call and lists them), the result is
        idempotent, and ``source`` records the origin. ``args.source``
        is the default origin for pins that don't name their own."""
        refdes = str(args.get("refdes") or "").strip()
        pins = args.get("pins")
        if not refdes or not isinstance(pins, list) or not pins:
            raise BadInput(
                "op='pins' needs args.refdes and a non-empty args.pins list",
                next="args={'op':'pins','refdes':'U7','pins':[{'name':'VDD',"
                "'etype':'power_in','rail':'3v3','max_voltage_v':3.6,"
                "'source':'datasheet p.12'}]}",
            )
        source = args.get("source")
        if source is not None:
            pins = [
                {**p, "source": str(source)} if p.get("source") is None else p
                for p in pins
                if isinstance(p, dict)
            ]
        try:
            result = self.store.pcb_patch_pins(ref.id, refdes, pins)
        except ValueError as exc:
            raise BadInput(
                f"pcb: {exc}",
                next="fix the named pin or field and retry — the message "
                "lists the component's real pins",
            ) from exc
        changed, unchanged = result["changed"], result["unchanged"]
        body = f"# {refdes}: {len(changed)} pin(s) annotated"
        if unchanged:
            body += f", {len(unchanged)} already as declared"
        body += "\n"
        if changed:
            body += "changed: " + ", ".join(changed) + "\n"
        return Response(
            body=body + f"Next: get(kind='pcb', id='{ref.slug}', view='check', "
            "args={'phase':'netlist'}) — the ERC rules now read this pin."
        )

    def _op_pin_edit(self, ref: Any, op: str, args: dict[str, Any]) -> Response:
        """``put(args={'op':'pin_rename','refdes':'U1','pin':'8','to':'GND'})``
        and ``put(args={'op':'pin_remove','refdes':'U1','pin':'8'})`` —
        the way out for a pin declared under the wrong name (gr495291: a
        pad-number connection minted ghost pin '8' and only deleting the
        design removed it). Rename keeps the pin's connection; remove
        retires the pin and drops its connection. Both are idempotent."""
        refdes = str(args.get("refdes") or "").strip()
        pin = str(args.get("pin") or "").strip()
        to = str(args.get("to") or "").strip()
        if not refdes or not pin or (op == "pin_rename" and not to):
            raise BadInput(
                f"op={op!r} needs args.refdes and args.pin"
                + (" and args.to" if op == "pin_rename" else ""),
                next=(
                    "args={'op':'pin_rename','refdes':'U1','pin':'8','to':'GND'}"
                    if op == "pin_rename"
                    else "args={'op':'pin_remove','refdes':'U1','pin':'8'}"
                ),
            )
        try:
            if op == "pin_rename":
                result = self.store.pcb_pin_rename(ref.id, refdes, pin, to)
            else:
                result = self.store.pcb_pin_remove(ref.id, refdes, pin)
        except ValueError as exc:
            raise BadInput(
                f"pcb: {exc}",
                next="fix the named pin and retry; nothing was changed",
            ) from exc
        net = result.get("net")
        if result["status"] == "unchanged":
            head = f"# {refdes}: no change, {op} of {pin} is already applied"
        elif op == "pin_rename":
            head = f"# {refdes}: pin {pin} renamed to {to}" + (
                f" (net {net})" if net else ""
            )
        else:
            head = f"# {refdes}: pin {pin} removed" + (
                f", its connection to {net} dropped" if net else ""
            )
        return Response(
            body=head + f"\nNext: get(kind='pcb', id='{ref.slug}', view='footprints')"
            " — the pin-name warning no longer lists it."
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

        Concretely: the old default made In1.Cu/In2.Cu pure planes, so only
        F.Cu/B.Cu routed. An author who wants a different split says so —
        ``In1.Cu`` plane/GND, ``In2.Cu`` signal — instead of waiting for the
        engine to change its mind.

        ``preset=`` (pcb-stackup-regions slice 3) writes a named stackup
        (:data:`precis.pcb.STACKUP_PRESETS`: ``4l-two-sided``, the hybrid
        default; ``4l-top-only``; ``2l``) instead of ``layers``; ``ground=``
        renames its GND for a board whose ground net is called otherwise.
        """
        layers = args.get("layers")
        preset = args.get("preset")
        preset_name: str | None = None
        if preset is not None:
            preset_name = str(preset).strip()
            if layers is not None:
                raise BadInput(
                    "op='stackup': give preset= or layers=, not both",
                    next=f"args={{'op':'stackup','preset':'{preset_name}'}}",
                )
            if preset_name not in STACKUP_PRESETS:
                raise BadInput(
                    f"op='stackup': unknown preset {preset_name!r}",
                    options=sorted(STACKUP_PRESETS),
                )
            ground = str(args.get("ground") or PRESET_GROUND_NET).strip()
            layers = stackup_preset(preset_name, ground=ground)
        elif args.get("ground") is not None:
            raise BadInput(
                "op='stackup': ground= renames a preset's ground net; it needs preset=",
                next="args={'op':'stackup','preset':'4l-two-sided','ground':'VSS'}",
            )
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

        # The stackup's plane_net/pour_nets declarations reach pcb_planes
        # through the same checked apply the batch put path reuses
        # (gr515869): check first so a rejected declaration writes
        # nothing, then store the stackup, then assign.
        declared = self.store.pcb_check_stackup_planes(
            ref.id,
            board_id,
            stackup,
            preset_name=preset_name,
            slug=ref.slug,
        )
        self.store.pcb_set_stackup(
            board_id,
            stackup,
            preset=preset_name,
            sides=PRESET_SIDES.get(preset_name or ""),
        )
        self.store.pcb_apply_stackup_planes(ref.id, declared)
        routable = [e["name"] for e in stackup if pcb_ir.layer_is_routable(e)]
        return Response(
            body=f"# {ref.slug} stackup set — {len(stackup)} layers"
            + (f" (preset {preset_name})" if preset_name else "")
            + "\n"
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
                            f"fill={e['fill']}" if "fill" in e else None,
                            "power regions" if e.get("power_regions") else None,
                            f"dir={e['direction']}" if "direction" in e else None,
                            (
                                f"cost={e['route_cost']:g}x"
                                if "route_cost" in e
                                else None
                            ),
                            (
                                f"pours={','.join(str(n) for n in e['pour_nets'])}"
                                if e.get("pour_nets")
                                else None
                            ),
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
        (:func:`precis.pcb.rows.normalize_footprint`) the
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
            lcsc = pcb_catalog.lcsc_id(parts[0]) or parts[0].upper()
            try:
                _, data = normalize_local_footprint({**footprint_data, "name": lcsc})
            except ValueError as exc:
                raise BadInput(
                    f"pcb: {exc}",
                    next="fix the field the message names — the footprint "
                    "dict takes the same {pads: [...]} shape as a put "
                    "'footprints' entry",
                ) from exc
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
            lcsc = pcb_catalog.lcsc_id(raw) or raw.upper()
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

    # ── JLCPCB order pipeline (docs/backlog/pcb-jlc-order.md) ────────────
    @staticmethod
    def _order_dry_run(args: dict[str, Any]) -> bool:
        """``dry_run`` defaults ON — the live path needs it explicitly off.
        Anything but a literal ``False``/``'false'``/``'0'``/``'no'``
        (including ``None``, a missing key, or a typo'd string) stays a dry
        run: ambiguity resolves toward NOT creating an order."""
        raw = args.get("dry_run", True)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("false", "0", "no")

    def _op_order(self, ref: Any, args: dict[str, Any]) -> Response:
        """``put(args={'op':'order', ...})`` — build (and optionally place)
        the JLCPCB order for this board.

        The API has no cart endpoint (docs/backlog/pcb-jlc-order.md): the
        documented chain is ``uploadGerber`` → ``pcb/calculate`` →
        ``pcb/create``, and ``create`` produces the unpaid order a human
        pays on jlcpcb.com — that IS the cart step, and the terminal one
        this surface reaches. Guards, in order:

        * ``dry_run`` defaults True — builds the exact ``pcb/create``
          payload, prints it, and attempts the read-only ``pcb/calculate``
          quote (only when credentials resolve; its failure reports
          inline, never raises).
        * ``dry_run=False`` still does nothing without
          ``confirm='add-to-cart'`` verbatim — and ``create_order``
          re-checks the same token at the client boundary.
        * A board already carrying ``meta.jlc_order`` needs
          ``reorder=True`` too — a silent second order is the footgun.

        Requires an exportable board (``export_fab``'s synthesized-pad
        refusal applies) and, for the live call, a
        ``args.shipping_address`` dict (JLC's OrderAddressData fields:
        firstName/lastName/streetAddress/city/country/postalCode/
        cellOrMobileNumber/companyName/addressLine2)."""
        slug = ref.slug if ref.slug else str(ref.id)
        built = self._fab_zip(ref.id, slug)
        if built is None:
            raise BadInput(
                f"pcb: {slug} has no board yet",
                next="put(kind='pcb', id='slug', args={'components':[...],"
                "'nets':[...]}) to create the design",
            )
        blob, warnings, files, model = built
        layer_names = [str(l) for l in model["layers"]]
        width_mm, length_mm = pcb_jlc_order.board_dimensions_mm(model["outline"])
        overrides = args.get("pcb_param")
        param = pcb_jlc_order.pcb_param(
            layers=len(layer_names),
            width_mm=width_mm,
            length_mm=length_mm,
            qty=int(args.get("qty") or 5),
            thickness_mm=float(
                args.get("thickness_mm") or pcb_export.DEFAULT_THICKNESS_MM
            ),
            overrides=overrides if isinstance(overrides, dict) else None,
        )
        order_type = int(args.get("order_type") or 1)
        shipping_address = args.get("shipping_address")
        quote_req = pcb_jlc_order.build_quote_request(
            pcb=param,
            order_type=order_type,
            country=str(args["country"]) if args.get("country") else None,
            post_code=str(args["postcode"]) if args.get("postcode") else None,
            city=str(args["city"]) if args.get("city") else None,
            shipping_method=(
                str(args["shipping_method"]) if args.get("shipping_method") else None
            ),
            smt_stencil=args.get("smt_stencil"),
        )
        create_req = pcb_jlc_order.build_order_request(
            pcb=param,
            order_type=order_type,
            shipping_address=(
                dict(shipping_address) if isinstance(shipping_address, dict) else None
            ),
            billing_address=(
                dict(args["billing_address"])
                if isinstance(args.get("billing_address"), dict)
                else None
            ),
            billing_address_flag=(
                int(args["billing_address_flag"])
                if args.get("billing_address_flag") is not None
                else None
            ),
            tax_or_vat=(str(args["tax_or_vat"]) if args.get("tax_or_vat") else None),
            shipping_method=quote_req.get("shippingMethod"),
            smt_stencil=args.get("smt_stencil"),
            achieve_date=(
                int(args["achieve_date"]) if args.get("achieve_date") else None
            ),
        )
        preview = dict(create_req)
        preview["fileKey"] = "<uploaded at confirm>"

        client = pcb_jlc_order.JlcOrderClient(store=self.store)
        if self._order_dry_run(args):
            lines = [
                f"# order {slug} — DRY RUN (nothing created at JLCPCB)",
                f"fab bundle: {len(files)} file(s), {len(blob):,} bytes",
                "",
                "create-order payload (pcbParam craft codes are "
                "JLC-internal ints — verify against the console docs "
                "before confirming):",
                "```",
                json.dumps(preview, indent=2, sort_keys=True),
                "```",
            ]
            if warnings:
                lines.append("\n".join(f"⚠️  {w}" for w in warnings))
            if client.available:
                try:
                    quote = client.quote(quote_req)
                except Exception as exc:
                    # Any quote failure is a report line, never a raise —
                    # dry_run exists to *show* what would happen.
                    lines.append(f"quote: unavailable ({type(exc).__name__}: {exc})")
                else:
                    lines.append(
                        "quote:\n```\n"
                        + json.dumps(quote, indent=2, sort_keys=True)
                        + "\n```"
                    )
            else:
                lines.append("quote: skipped — JLCPCB credentials do not resolve here")
            lines.append(
                "\nNothing was uploaded or created. To place it (unpaid "
                "order — the API has no cart or pay endpoint): "
                "put(kind='pcb', id='" + slug + "', args={'op':'order','dry_run':False,"
                "'confirm':'add-to-cart','shipping_address':{...},"
                " ...same params})"
            )
            return Response(body="\n".join(lines))

        # ── live path ────────────────────────────────────────────────
        confirm = args.get("confirm")
        if confirm != pcb_jlc_order.CONFIRM_ADD_TO_CART:
            raise BadInput(
                "pcb: op='order' with dry_run=False requires "
                "confirm='add-to-cart' verbatim — nothing was sent",
                next=f"put(kind='pcb', id='{slug}', args={{'op':'order',"
                "'dry_run':False,'confirm':'add-to-cart','shipping_address'"
                ":{...}}})",
            )
        existing = self.store.pcb_load(ref.id)["board"] or {}
        prior = (existing.get("meta") or {}).get("jlc_order")
        if prior and not args.get("reorder"):
            raise BadInput(
                f"pcb: {slug} already carries a JLCPCB order "
                f"(batch {prior.get('batch_num')}) — pass reorder=True to "
                "place ANOTHER one; nothing was sent",
                next="retry the same call with 'reorder':True added",
            )
        if not isinstance(shipping_address, dict) or not shipping_address:
            raise BadInput(
                "pcb: op='order' needs args.shipping_address — a dict of "
                "JLC's OrderAddressData fields (firstName, lastName, "
                "streetAddress, city, country, postalCode, "
                "cellOrMobileNumber; companyName/addressLine2 optional)",
                next="add 'shipping_address':{firstName:...,lastName:...,"
                "streetAddress:...,city:...,country:...,postalCode:...,"
                "cellOrMobileNumber:...} to the same args",
            )
        if not client.available:
            raise BadInput(
                "pcb: JLCPCB credentials do not resolve here "
                "(JLCPCB_APP_ID/ACCESS_KEY/SECRET_KEY live in the prod "
                "vault) — nothing was sent",
                next="run the same call with 'dry_run':True for the payload "
                "preview, or place the order on jlcpcb.com by hand",
            )
        file_key = client.upload_gerber(
            f"{slug}-fab.zip", blob, meta={"fileName": f"{slug}-fab.zip"}
        )
        quote_req["fileKey"] = file_key
        try:
            quote = client.quote(quote_req)
        except Exception as exc:
            raise BadInput(
                f"pcb: gerber uploaded (fileKey {file_key}) but the quote "
                f"failed — order NOT created: {type(exc).__name__}: {exc}",
                next="retry the same put — the upload succeeded; no order was created",
            ) from exc
        create_req["fileKey"] = file_key
        created = client.create_order(
            create_req, confirm=pcb_jlc_order.CONFIRM_ADD_TO_CART
        )
        ids = pcb_jlc_order.extract_order_ids(created)
        record = {
            **ids,
            "file_key": file_key,
            "qty": param["qty"],
            "layers": param["layer"],
            "size_mm": [width_mm, length_mm],
            "quote": quote.get("data") if isinstance(quote, dict) else None,
            "added_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "state": "cart",
        }
        self.store.pcb_merge_board_meta(ref.id, {"jlc_order": record})
        head = (
            f"# order {slug} — ADDED TO CART (unpaid order at JLCPCB)\n"
            f"batch: {ids.get('batch_num', '?')}  "
            f"order: {ids.get('order_id') or ids.get('order_uuid') or '?'}\n"
            "Payment is a human's step on jlcpcb.com — the API has no pay "
            "endpoint. Attach the BOM/CPL (view='fab') at checkout for "
            "assembly."
        )
        if warnings:
            head += "\n" + "\n".join(f"⚠️  {w}" for w in warnings)
        head += f"\n\nquote:\n```\n{json.dumps(quote, indent=2, sort_keys=True)}\n```"
        return Response(body=head)

    def _render_notes(self, ref_id: int) -> Response:
        """The argument ledger (``pcb_notes``), oldest first. Anchors that no
        longer resolve are reported per note, never raised — se_notes' rule."""
        notes = self.store.pcb_notes_list(ref_id)
        if not notes:
            return Response(
                body="# notes — none yet\n"
                "Argue with the design on its board page (/pcb/<slug>): click "
                "parts and pads to drop their handles into the text box."
            )
        valid = self.store.pcb_handles(ref_id)
        lines = [f"# notes — {len(notes)} ({len(valid['parts'])} live part(s))"]
        for n in notes:
            ts = n["created_at"]
            when = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ") if ts else ""
            head = f"## {n['name']} · {n['kind']} · {n['origin']} · {when}"
            if n["re"]:
                head += f" · re {n['re']}"
            lines.append(head)
            if n["about"]:
                lines.append("about: " + " ".join(n["about"]))
            gone = argue.dangling(n["about"], valid)
            if gone:
                lines.append("dangling (no longer on the board): " + " ".join(gone))
            lines.append(n["body"])
            lines.append("")
        return Response(body="\n".join(lines).rstrip() + "\n")

    # ── the eyes ───────────────────────────────────────
    def _render_view(self, ref_id: int, view: str, args: dict[str, Any]) -> Response:
        if view not in _VIEWS:
            near = _VIEW_NEAR_MISSES.get(view.strip().lower())
            guess = near or "; ".join(
                f"view={m!r}"
                for m in difflib.get_close_matches(view.lower(), _VIEWS, n=3)
            )
            raise BadInput(
                f"unknown pcb view {view!r}",
                options=list(_VIEWS),
                next=(
                    (f"did you mean {guess}? " if guess else "")
                    + f"view= one of {list(_VIEWS)}, or omit for the netlist TOC"
                ),
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
        if view == "stackup":
            return self._render_stackup(ref_id)
        if view == "channel-map":
            return self._render_channel_map(ref_id, args)
        if view == "drc":
            return self._render_drc(ref_id, args)
        if view == "check":
            return self._render_check(ref_id, args)
        if view == "svg":
            return self._render_svg(ref_id, args)
        if view == "capability":
            return self._render_capability(ref_id, args)
        if view == "schematic":
            from precis.pcb import schematic

            ref = self.store.fetch_refs_by_ids([ref_id]).get(ref_id)
            slug = (ref.slug if ref is not None else None) or "pcb"
            focus = str((args or {}).get("focus") or "").strip() or None
            sheet = str((args or {}).get("sheet") or "").strip() or None
            return Response(
                body=schematic.render_schematic_svg(
                    self.store.pcb_graph(ref_id), title=slug, focus=focus, sheet=sheet
                )
            )
        if view == "footprints":
            return self._render_footprints_view(ref_id)
        if view == "availability":
            return self._render_availability(ref_id)
        if view == "placement":
            return self._render_placement(ref_id)
        if view == "notes":
            return self._render_notes(ref_id)
        if view == "links":
            # Graph-completeness audit item 1 (OPEN-ITEMS.md 🕸️) — sweep of
            # every Handler-direct kind alongside the paper fix.
            from precis.handlers._links_render import render_links_view

            ref = self.store.fetch_refs_by_ids([ref_id]).get(ref_id)
            if ref is None:
                raise NotFound(
                    f"pcb id={ref_id} not found",
                    next="re-resolve the design: get(kind='pcb', id='slug', "
                    "view='links')",
                )
            return render_links_view(self.store, ref, sense="pcb")
        graph = pcb_session.sorted_graph(self._effective_graph(ref_id))
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
                raise BadInput(
                    str(exc),
                    next="get(kind='pcb', id='slug') lists this design's refdes",
                ) from exc
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
                raise BadInput(
                    str(exc),
                    next="get(kind='pcb', id='slug') lists this design's net names",
                ) from exc
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
            graph,
            measures,
            self.store.pcb_features_list(ref_id),
            fabric=self._measures_fabric(ref_id, measures),
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

    def _measures_fabric(
        self, ref_id: int, measures: list[dict[str, Any]]
    ) -> dict | None:
        """The pads/copper/generators bundle that generator-declared
        invariant metrics and any ``strength='invariant'`` row evaluate
        against (gr345859 §F — the SAME bundle ``_validity_findings``
        judges, so the view and the gate cannot disagree on a verdict).
        ``None`` when no stored measure needs it, so a plain measures view
        never pays the pad-geometry build. On a design with no board row,
        the param-only metrics (``hv_pitch_floor``) still evaluate."""
        needs_fabric = any(
            str(m.get("metric") or "").strip().lower()
            in pcb_generators.INVARIANT_METRICS
            or str(m.get("strength") or "").strip().lower() == "invariant"
            for m in measures
        )
        if not needs_fabric:
            return None
        fabric: dict[str, Any] = {
            "generators": self.store.pcb_generators_for(ref_id),
            "pads": [],
            "copper": [],
            "layers": [],
        }
        board = self.store.pcb_load(ref_id)["board"]
        if board is not None:
            layer_names = [str(layer.get("name")) for layer in board["stackup"]]
            fabric["layers"] = layer_names
            fabric["pads"] = self._drc_pads(ref_id, layer_names)
            fabric["copper"] = [
                {**row, "net": row.get("net") or ""}
                if row.get("fixed")
                else {**row, "derived": True}
                for row in self.store.pcb_copper_list(int(board["board_id"]))
            ]
        return fabric

    # ── exporters ──────────────────────────────────────
    def _effective_graph(self, ref_id: int) -> dict[str, Any]:
        """The design's EFFECTIVE netlist: the authored graph with the
        latest resolved snapshot laid over it (Slice 7). Every reader that
        names which pin carries which net (pads, ERC, ratsnest, exports)
        takes this, so it agrees with the routed copper. Idempotent:
        :meth:`_build_ir` applying the snapshot again changes nothing.
        Delegates to :func:`precis.pcb.check_run.effective_graph`."""
        return check_run.effective_graph(self.store, ref_id)

    def _export_model(self, ref_id: int) -> dict[str, Any]:
        """The normalised export IR (placement detail + net membership),
        plus ``board_features`` — the instances that are etched copper, not
        parts, which the BOM/CPL writers leave out. Net membership is the
        effective netlist (the latest resolved snapshot over the authored
        graph), so the exported netlist matches the routed copper."""
        model = pcb_export.export_model(
            self.store.pcb_load(ref_id), self._effective_graph(ref_id)
        )
        model["board_features"] = pcb_export.board_feature_refdes(
            model["instances"], self.store.pcb_local_footprints_for(ref_id)
        )
        return model

    def _export_dir(self, slug: str, args: dict[str, Any] | None = None) -> Path:
        """Where artifacts land: ``<PRECIS_CORPUS_DIR>/pcb/<slug>/`` when the
        corpus root is set, else a temp dir. Override per-call with
        args={'dir': '...'} — resolved (symlinks followed) and confined to
        the export root: under the corpus ``pcb/`` dir or the system temp
        dir, anything else refuses rather than writing wherever the serving
        process can (an agent-supplied absolute path is not a license to
        write outside the root)."""
        root = load_config().corpus_dir
        base = Path(root) / "pcb" if root else Path(tempfile.gettempdir())
        raw = (args or {}).get("dir")
        if not raw:
            out = base / slug
            out.mkdir(parents=True, exist_ok=True)
            return out
        out = Path(str(raw)).expanduser().resolve()
        allowed = [Path(tempfile.gettempdir()).resolve()]
        if root:
            allowed.append(base.resolve())
        if not any(out == a or a in out.parents for a in allowed):
            raise BadInput(
                f"dir {str(raw)!r} resolves to {out}, outside the export "
                f"root ({', '.join(str(a) for a in allowed)})",
                next="omit 'dir' to export under the default root, or pass "
                "a directory inside it",
            )
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _outline_from_features(self, ref_id: int) -> list[list[float]] | None:
        """Delegates to :func:`precis.pcb.check_run.outline_from_features` —
        the single parse point every view/export reads the outline from."""
        return check_run.outline_from_features(self.store, ref_id)

    def _build_ir(
        self,
        ref_id: int,
        graph: dict[str, Any],
        *,
        with_fixed_copper: bool = False,
        fixed_copper: list[dict[str, Any]] | None = None,
    ) -> pcb_ir.PcbIR:
        """Delegates to :func:`precis.pcb.check_run.build_ir` — one wrapper
        so no view can build an IR that forgot the mounting holes,
        footprint caches or the resolved netlist snapshot."""
        return check_run.build_ir(
            self.store,
            ref_id,
            graph,
            with_fixed_copper=with_fixed_copper,
            fixed_copper=fixed_copper,
        )

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
        """Delegates to :func:`precis.pcb.session.furniture_clearance_mm` —
        the figure lives in the session layer so the ``pcb_argue`` worker
        resolves the SAME number (the workers->handlers layering contract
        forbids it this import)."""
        return pcb_session.furniture_clearance_mm(stackup)

    def _stackup_capability(
        self, stackup: list[dict[str, Any]], profile: str | None = None
    ) -> CapabilityRow | None:
        """Delegates to :func:`precis.pcb.session.stackup_capability` —
        the lookup lives in the session layer so the ``pcb_argue`` worker
        shares it."""
        return pcb_session.stackup_capability(stackup, profile)

    def _polarized_refdes(self, design: dict[str, Any]) -> frozenset[str]:
        """Delegates to :func:`precis.pcb.session.polarized_refdes` —
        the label/flag determination lives in the session layer so the
        ``pcb_argue`` worker shares it."""
        return pcb_session.polarized_refdes(design)

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
        label_spots: dict[str, str] | None = None,
        profile: str | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, list[dict[str, Any]]],
        list[str],
        tuple[pcb_silk.SilkPlacement, ...],
        list[dict[str, Any]],
    ]:
        """Fiducials, title block and silkscreen, assembled ONCE —
        delegates to :func:`precis.pcb.furniture.board_furniture`, where
        the assembly lives so the ``pcb_argue`` worker (which may not
        import this handler — the layering contract forbids it) builds
        the SAME catalogue model the renders do."""
        return pcb_furniture.board_furniture(
            ir,
            pads,
            vias=vias,
            instance_sides=instance_sides,
            copper=copper,
            outline=outline,
            layer_names=layer_names,
            slug=slug,
            clearance_mm=clearance_mm,
            capability=capability,
            date=date,
            polarized=polarized,
            label_spots=label_spots,
            profile=profile,
        )

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
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, list[tuple[float, float]]],
        dict[str, dict[str, Any]],
    ]:
        """Delegates to :func:`precis.pcb.check_run.geometry` — DRC's pads,
        courtyard polygons and the refdes-keyed footprint map."""
        return check_run.geometry(self.store, ref_id, layers)

    def _drc_drills(self, ir: pcb_ir.PcbIR) -> list[dict[str, Any]]:
        """Delegates to :func:`precis.pcb.check_run.drills` (the unplated
        mounting holes ``check_npth_clearance`` protects; a through-hole
        pad's own drill is :meth:`_pad_drills`)."""
        return check_run.drills(ir)

    @staticmethod
    def _pad_drills(pads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``model['drills']`` entries for every drilled component pad —
        the plated through-holes ``model['drills']`` otherwise lacks on
        the IR-sourced pad path (:meth:`_drc_pads` /
        :func:`precis.pcb.realize.pads_for_ir`), whose pads carry
        ``drill``/``slot`` on themselves instead of emitting a second
        drill list the way
        :func:`precis.pcb.padplace.place_footprint_pads` does.

        Re-shapes what is already on the pad, so it cannot disagree with
        the drawn land. Deduped on the hole's physical identity
        ``(x, y, dia)`` — the same :func:`precis.pcb.drc.
        _drilled_pad_ring_items` uses — so a pad list that ever carries a
        hole twice (``place_footprint_pads`` flashes one per copper layer)
        still writes one Excellon coordinate."""
        out: list[dict[str, Any]] = []
        seen: set[tuple[float, float, float]] = set()
        for pad in pads:
            drill = pad.get("drill")
            if not drill:
                continue
            key = (
                round(float(pad["x"]), 4),
                round(float(pad["y"]), 4),
                round(float(drill), 4),
            )
            if key in seen:
                continue
            seen.add(key)
            hole: dict[str, Any] = {
                "x": float(pad["x"]),
                "y": float(pad["y"]),
                "dia_mm": float(drill),
                "plated": True,
            }
            # ``slot`` on a placed pad is already the board-frame
            # end-centre pair ``_excellon_file``'s G85 row and
            # ``gerber_view``'s slot render both consume.
            if pad.get("slot"):
                hole["slot"] = pad["slot"]
            out.append(hole)
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
        if view == "fab":
            return self._render_fab_package(ref_id, args)
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        model = self._export_model(ref_id)
        out_dir = self._export_dir(slug, args)

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
                        model,
                        self.store.pcb_features_list(ref_id),
                        holes=[
                            {"x": h.x, "y": h.y, "diameter": h.drill_mm}
                            for h in self._build_ir(
                                ref_id, self.store.pcb_graph(ref_id)
                            ).mounting_holes
                        ],
                    ),
                    indent=2,
                ),
            ),
        }
        ext, build = builders[view]
        content = build()

        # BOM and CPL are both .csv: a shared `{slug}.csv` let one export
        # silently overwrite the other in the same directory.
        safe = pcb_epro_write.safe_stem(slug)
        stem = f"{safe}-{view}" if view in ("bom", "cpl") else safe
        path = out_dir / f"{stem}.{ext}"
        path.write_text(content, encoding="utf-8")
        warns = self._export_warnings(model, view)
        head = f"# exported {slug} → {view.upper()}\n{path}  ({len(content):,} bytes)"
        if warns:
            head += "\n" + "\n".join(f"⚠️  {w}" for w in warns)
        # Echo a short preview so the agent sees the shape without re-reading.
        preview = pcb_export.csv_preview(content)
        if view == "bom":
            # Not a column of the file (JLCPCB's BOM upload has fixed
            # headers): the datasheet state of each C-number, beside it.
            head += self._datasheet_status_block(ref_id)
        body = head + "\n\n```\n" + preview + "\n```"
        if view == "bom":
            # The consolidation nudge rides the RESPONSE, never the file —
            # the exported CSV is a JLCPCB artifact and stays byte-exact.
            footer = bom_nudge.pcb_footer(
                self.store,
                root_ref_id=ref_id,
                root_meta=(ref.meta or {}) if ref is not None else {},
            )
            if footer:
                body += "\n\n" + "\n".join(footer)
        return Response(body=body)

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
            ex = pcb_export.excluded_features(model)
            if ex:
                out.append(
                    f"{len(ex)} board feature(s) excluded (etched copper, "
                    f"no pads to solder): {', '.join(ex[:8])}"
                    + ("…" if len(ex) > 8 else "")
                )
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
        graph = self._effective_graph(ref_id)
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
        # Local-first, same precedence as footprints_by_refdes / board_pads:
        # an imported .epro2 part carries BOTH its C-number and the board's
        # own footprint — the local row is what its pads come from.
        has_pads = {
            i["refdes"]
            for i in placed
            if (local_footprints.get(str(i.get("footprint") or "")) or {}).get("pads")
            or (footprints.get(str(i.get("part_lcsc") or "")) or {}).get("pads")
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
        board_profile_name = pcb_profiles.board_profile(board.get("meta"))
        capability = self._stackup_capability(board["stackup"], board_profile_name)
        pads, silk_draws, furniture_warnings, silk_census, copper = (
            self._board_furniture(
                ir,
                pads,
                vias=vias,
                instance_sides=instance_sides,
                polarized=self._polarized_refdes(design),
                label_spots=pcb_session.label_spots(design),
                copper=copper,
                outline=outline,
                layer_names=layer_names,
                slug=slug,
                clearance_mm=self._furniture_clearance_mm(board["stackup"]),
                capability=capability,
                date=_export_date(),
                profile=board_profile_name,
            )
        )
        warnings.extend(furniture_warnings)
        # Where each DRAWN refdes label sits (silk.SilkPlacement x/y): the
        # anchor an editable-text export (.epro2 ATTR) uses so the
        # retypeable designator lands where the stroked one prints. The
        # gerber writer ignores the key (its model docstring is the
        # contract); additive, like `instances`.
        silk_labels = [
            {
                "refdes": c.refdes,
                "side": c.side,
                "x": c.x_mm,
                "y": c.y_mm,
                "angle": c.angle_deg,
                "height_mm": c.height_mm,
                "stroke_width_mm": c.stroke_width_mm,
            }
            for c in silk_census
            if c.kind == "refdes" and c.x_mm is not None and c.y_mm is not None
        ]

        # Pin electrical semantics for the .epro2 writer's "Pin Types"
        # write-back — the §1i etype/nc tags pins imported or were edited
        # to. Keyed by pin NAME: the writer stamps each footprint pad's
        # ``num`` with the resolved pin name, and that num is what the
        # re-imported pin's ``pad`` reads — the join key that survives.
        # Gerber ignores it.
        pin_sem: dict[str, dict[str, dict[str, Any]]] = {}
        with self.store.pool.connection() as conn:
            for refdes, name, pad, tags, meta in conn.execute(
                "SELECT i.refdes, p.name, p.pad, p.tags, p.meta "
                "FROM pcb_instances i "
                "JOIN pcb_pins p ON p.component_id = i.component_id "
                "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                "  AND p.retired_at IS NULL",
                (ref_id,),
            ).fetchall():
                tag_list = list(tags or [])
                pin_meta = dict(meta or {})
                etype = next((t for t in tag_list if t in PIN_ETYPES), None)
                ent: dict[str, Any] = {}
                # "nc" is etype AND the marker word — emitting it as etype
                # would flip a mark-only pin into etype on re-import; the
                # nc marker is the meaning that must survive.
                if etype and etype != "nc":
                    ent["etype"] = etype
                if pin_meta.get("nc") or "nc" in tag_list:
                    ent["nc"] = True
                if ent:
                    pin_sem.setdefault(str(refdes), {})[str(name)] = ent

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
            "instances": [
                (
                    {**i, "pin_sem": pin_sem[str(i["refdes"])]}
                    if str(i["refdes"]) in pin_sem
                    else i
                )
                for i in design["instances"]
            ],
            "silk_labels": silk_labels,
            # Every net name on the board, including the empty ones (a
            # schematic net with no board members — EasyEDA keeps those
            # declared in the netlist; dropping the NET record would drop
            # the net). The writer unions this with pad/copper nets.
            "nets": [str(n["name"]) for n in graph["nets"]],
            # {layer name: plane net} from the stackup — the .epro2
            # writer covers a plane that has no authored pour left over
            # with an outline-sized zone so its net survives; the gerber
            # writer realizes its own fill and ignores the key. A layer's
            # `fill` net counts too (gr495395: the default stackup pours
            # GND as a fill on both inner layers, not as a plane_net).
            "planes": {
                str(e["name"]): str(e["plane_net"])
                if e.get("role") == "plane" and e.get("plane_net")
                else str(e["fill"])
                for e in board["stackup"]
                if (e.get("role") == "plane" and e.get("plane_net")) or e.get("fill")
            },
        }
        return model, warnings

    def _render_gerber(self, ref_id: int, args: dict[str, Any]) -> Response:
        """Write the manufacturable fab bundle — gerbers + Excellon, zipped
        (:mod:`precis.pcb.gerber`) — off :meth:`_fab_model`, closing
        ``docs/backlog/pcb-fab-output-unwired.md``'s "the export tail is
        unwired" gap. ``args={'allow_synthesized': true}`` exports a
        PREVIEW bundle over synthesized pad bounds — named
        ``{slug}-preview.zip`` and headed PREVIEW so it can never pass
        for the order upload (gr513872)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        allow_synthesized = bool(args.get("allow_synthesized"))
        built = self._fab_zip(ref_id, slug, allow_synthesized=allow_synthesized)
        if built is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        blob, warnings, files, model = built
        pads, drills, copper = model["pads"], model["drills"], model["copper"]

        out_dir = self._export_dir(slug, args)
        stem = pcb_epro_write.safe_stem(slug)
        fname = f"{stem}-preview.zip" if allow_synthesized else f"{stem}-fab.zip"
        path = out_dir / fname
        path.write_bytes(blob)
        banner, _n_drc_errors = self._gerber_drc_banner(ref_id, slug)

        preview = ""
        if allow_synthesized:
            n_synth = len(
                {str(p.get("net", "?")) for p in pads if p.get("synthesized")}
            )
            preview = f"PREVIEW - {n_synth} synthesized pad net(s), not fabricable\n"
        head = (
            f"{preview}{banner}\n\n"
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

    def _render_fab_package(self, ref_id: int, args: dict[str, Any]) -> Response:
        """``view='fab'`` — the JLC order package in one directory:
        ``{slug}-fab.zip`` (the SAME :func:`export_fab` bundle
        ``view='gerber'`` writes), plus ``{slug}-bom.csv`` and
        ``{slug}-cpl.csv`` in JLC's upload column formats off
        :meth:`_export_model`. The zip is the order upload
        (:meth:`_op_order`); the BOM/CPL are the assembly attachments a
        human adds at jlcpcb.com checkout — the order API has no BOM/CPL
        endpoints (docs/backlog/pcb-jlc-order.md)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        built = self._fab_zip(ref_id, slug)
        if built is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        blob, warnings, _files, _model = built
        export_model = self._export_model(ref_id)
        bom = pcb_export.bom_csv(export_model)
        cpl = pcb_export.cpl_csv(export_model)

        out_dir = self._export_dir(slug, args)
        stem = pcb_epro_write.safe_stem(slug)
        written = []
        for fname, text in (
            (f"{stem}-fab.zip", None),
            (f"{stem}-bom.csv", bom),
            (f"{stem}-cpl.csv", cpl),
        ):
            path = out_dir / fname
            if text is None:
                path.write_bytes(blob)
            else:
                path.write_text(text, encoding="utf-8")
            written.append(f"{path}  ({path.stat().st_size:,} bytes)")

        warns = list(warnings)
        for w in self._export_warnings(export_model, "bom") + self._export_warnings(
            export_model, "cpl"
        ):
            warns.append(w)
        seen: set[str] = set()
        unique_warns: list[str] = []
        for w in warns:
            if w not in seen:
                seen.add(w)
                unique_warns.append(w)
        warns = unique_warns
        head = f"# exported {slug} → FAB PACKAGE\n" + "\n".join(written)
        if warns:
            head += "\n" + "\n".join(f"⚠️  {w}" for w in warns)
        head += (
            "\n\nNext: put(kind='pcb', id='" + slug + "', args={'op':'order'}) "
            "dry-runs the JLCPCB order; the BOM/CPL attach at checkout."
        )
        return Response(body=head)

    def _fab_zip(
        self, ref_id: int, slug: str, *, allow_synthesized: bool = False
    ) -> tuple[bytes, list[str], dict[str, str], dict[str, Any]] | None:
        """The gerber+drill bundle as in-memory bytes, plus its warnings,
        file map and assembled model — the one assembly
        :meth:`_render_gerber`, :meth:`_render_fab_package` and
        :meth:`_op_order` share so the uploaded bytes and the previewed
        file can never drift. ``None`` when the design has no board row,
        same contract as :meth:`_fab_model`.

        ``allow_synthesized`` is :meth:`_render_gerber`'s preview switch
        ONLY — every other caller (``view='fab'``, ``fab_package_zip``,
        ``op='order'``) feeds a real JLC order upload and must stay on
        the strict default."""
        built = self._fab_model(ref_id, slug=slug)
        if built is None:
            return None
        model, warnings = built
        try:
            files = pcb_gerber.export_fab(
                model, name=slug, allow_synthesized=allow_synthesized
            )
        except pcb_gerber.SynthesizedPadError as exc:
            raise BadInput(
                f"pcb: {exc} For a preview-only export: get(kind='pcb', "
                f"id='{slug}', view='gerber', "
                "args={'allow_synthesized': True}); view='fab' and "
                "op='order' never accept it — they feed a real order. "
                "view='svg' args={'level':'fab'} inspects the board as "
                "gerbers without exporting it.",
                next=f"cache the real footprint first: put(kind='pcb', "
                f"id='{slug}', args={{'op':'footprint','part':'<lcsc>'}})",
            ) from exc
        return pcb_gerber.zip_fab(files), warnings, files, model

    def fab_package_zip(self, ref_id: int) -> tuple[bytes, str] | None:
        """The ``view='fab'`` package as ONE zip a browser can download
        (gr496699): ``{slug}-fab.zip`` (the gerber+drill bundle
        :meth:`_fab_zip` builds, kept whole for JLC's gerber upload),
        ``{slug}-bom.csv`` and ``{slug}-cpl.csv`` — the same three
        artifacts :meth:`_render_fab_package` writes to a directory,
        assembled in memory with nothing stored. ``None`` when the
        design has no board row, the same contract as
        :meth:`_fab_zip`."""
        import zipfile

        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        built = self._fab_zip(ref_id, slug)
        if built is None:
            return None
        blob, _warnings, _files, _model = built
        export_model = self._export_model(ref_id)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{slug}-fab.zip", blob)
            zf.writestr(f"{slug}-bom.csv", pcb_export.bom_csv(export_model))
            zf.writestr(f"{slug}-cpl.csv", pcb_export.cpl_csv(export_model))
        return out.getvalue(), f"{slug}-fab-package.zip"

    def epro_build(
        self, ref_id: int, *, allow_synthesized: bool = False
    ) -> EproBuild | None:
        """The ``.epro2`` bytes for a board, in memory — ``None`` when it has
        no board yet. The one build :meth:`_render_epro` (the MCP view),
        ``op='import_epro'`` and the web UI's ``GET /pcb/{slug}/export.epro2``
        all share: the writer is deterministic, so the same board state
        gives the same bytes and sha256 on every surface, with nothing
        stored (docs/backlog/pcb-epro-mcp-roundtrip.md, Reto's ruling)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        built = self._fab_model(ref_id, slug=slug)
        if built is None:
            return None
        model, warnings = built
        # The verbatim passthroughs the import stored on the ref: the rule
        # table (meta.epro_rules) and the geometry-free board records
        # (meta.epro_board — physical stackup, title) ride back out
        # unchanged. A board never imported from epro has neither.
        meta = (ref.meta or {}) if ref is not None else {}
        if meta.get("epro_rules"):
            model["epro_rules"] = meta["epro_rules"]
        if meta.get("epro_board"):
            model["epro_board"] = meta["epro_board"]
        # The authored pour zones the source carried (meta.epro_pours,
        # board coords): an inner one IS the layer's plane, an outer one
        # has no other home — either way the authored outline goes back
        # out verbatim rather than precis rebuilding a zone of its own.
        for pour in meta.get("epro_pours") or []:
            model["copper"].append({"ctype": "pour", **pour})
        # Footprint-owned non-plated holes per refdes (authored-local) —
        # they travel inside the shared FOOTPRINT documents, not as
        # board-level pads (the drill row stays the hole's board position).
        footprints = self.store.pcb_footprints_for(ref_id)
        local_footprints = self.store.pcb_local_footprints_for(ref_id)
        holes_by_refdes: dict[str, list[dict[str, Any]]] = {}
        for i in model["instances"]:
            # local-first: the same precedence board_pads applies — an
            # imported part keeps its own footprint even with a C-number.
            fp = local_footprints.get(str(i.get("footprint") or ""))
            if fp is None:
                lcsc = str(i.get("part_lcsc") or "")
                fp = footprints.get(lcsc) if lcsc else None
            if fp and fp.get("holes"):
                holes_by_refdes[str(i["refdes"])] = list(fp["holes"])
        if holes_by_refdes:
            model["footprint_holes"] = holes_by_refdes
        try:
            exported = pcb_epro_write.epro_files(
                model, slug=slug, allow_synthesized=allow_synthesized
            )
        except pcb_gerber.SynthesizedPadError as exc:
            raise BadInput(
                f"pcb: {exc} Use view='svg' args={{'level':'fab'}} to inspect "
                "the board without exporting it.",
                next=f"put(kind='pcb', id='{slug}', args={{'op':'footprint', "
                "'parts':[...]}}) caches the real footprints",
            ) from exc
        except ValueError as exc:  # a name the record separator cannot carry
            raise BadInput(
                f"pcb: cannot write .epro2: {exc}",
                next=f"rename the net or part the message names: "
                f"put(kind='pcb', id='{slug}', args={{'nets':[...]}})",
            ) from exc
        return EproBuild(
            slug=slug,
            model=model,
            warnings=warnings,
            exported=exported,
            blob=pcb_epro_write.zip_epro(exported.files),
        )

    def _op_import_epro(self, slug: str, args: dict[str, Any]) -> Response:
        """``put(id=<new slug>, args={'op':'import_epro',
        'source':'export:<board>'})`` — rebuild ``<board>``'s ``.epro2``
        server-side (:meth:`epro_build`) and run the CLI's own importer
        (:func:`precis.ingest.pcb_epro.import_epro`) on those bytes into a
        new preview slug. A dry run unless ``'dry_run': False``.

        Reto's ruling (gr495289, option 2a): the only source is precis's
        own export, so no caller-supplied bytes or URLs reach the reader —
        no base64, no fetch, no upload. Real ``.epro2`` files stay on the
        CLI. Never ``--update``: the target must be a new slug, and a
        synthesized-pad board is refused (its bounds would import as real
        footprints)."""
        from precis.ingest.pcb_epro import EproImportError, import_epro
        from precis.pcb import copper_report
        from precis.pcb.epro import EproError

        source = str(args.get("source") or "").strip()
        src_slug = source.removeprefix("export:").strip()
        example = (
            f"put(kind='pcb', id='{slug}', args={{'op':'import_epro', "
            "'source':'export:<existing board slug>'}})"
        )
        if not source.startswith("export:") or not src_slug:
            raise BadInput(
                f"op='import_epro' needs source='export:<board slug>', got {source!r}",
                next=example,
                options=["export:<board slug>"],
            )
        if src_slug == slug:
            raise BadInput(
                f"op='import_epro' writes a NEW board; id={slug!r} is its source",
                next=f"put(kind='pcb', id='{slug}-reimport', args={{'op':'import_epro', "
                f"'source':'export:{src_slug}'}})",
            )
        src_ref = resolve_live_slug_ref(self.store, kind="pcb", id=src_slug)
        build = self.epro_build(int(src_ref.id))
        if build is None:
            raise BadInput(
                f"pcb {src_slug!r} has no board yet; there is nothing to export",
                next=f"get(kind='pcb', id='{src_slug}') shows what it holds",
            )
        dry_run = args.get("dry_run") is not False
        try:
            result = import_epro(
                self.store,
                build.blob,
                slug=slug,
                title=f"{build.slug} re-import",
                source_name=f"export:{build.slug}",
                dry_run=dry_run,
            )
        except EproError as exc:
            raise BadInput(
                f"the export of {src_slug!r} does not read back: {exc}",
                next=f"get(kind='pcb', id='{src_slug}', view='epro', "
                "args={'verify': True}) names what the file loses",
            ) from exc
        except EproImportError as exc:
            # A taken slug or a board precis cannot hold (e.g. its layer
            # count); the importer's own message names which.
            raise BadInput(
                f"op='import_epro': {exc}",
                next=f"a taken slug needs a new id=, e.g. put(kind='pcb', "
                f"id='{slug}-2', args={{'op':'import_epro', "
                f"'source':'export:{src_slug}'}})",
            ) from exc

        queued = facts = 0
        if not dry_run and result.ref_id:
            # The import writes through pcb_apply, not this put's batch
            # path, so it queues the datasheet work itself (as the CLI does).
            queued, facts = self._queue_datasheets(result.ref_id)
        s = result.stats
        verb = "would import" if dry_run else "imported"
        lines = [
            f"# import_epro export:{build.slug} → {slug}: {verb} "
            f"{s['components']} component(s), {s['footprints']} footprint(s), "
            f"{s['nets']} net(s), {s['connections']} connection(s), "
            f"{s['mounting_holes']} mounting hole(s)",
            f"source sha256: {build.sha256} (the bytes view='epro' writes)",
            "stackup: " + ", ".join(str(layer.get("name")) for layer in result.stackup),
        ]
        if result.planes:
            lines.append(
                "planes: "
                + ", ".join(f"{k}={v}" for k, v in sorted(result.planes.items()))
            )
        # Every warning, never a count standing in for them: each is a thing
        # the re-imported board does NOT carry.
        lines += [f"⚠️  {w}" for w in result.warnings]
        lines += result.dropped_lines()
        if result.copper is not None:
            lines += copper_report.render(result.copper)
        if queued or facts:
            lines.append(
                f"{queued} datasheet pull(s), {facts} facts extraction(s) queued"
            )
        if dry_run:
            lines.append(
                "dry run — nothing was written. Next: the same put with "
                "'dry_run': False writes it."
            )
        else:
            lines.append(
                f"Next: get(kind='pcb', id='{slug}'). Locked parts stay locked; "
                "op='route' re-places the unlocked ones first."
            )
        return Response(body="\n".join(lines))

    def _render_epro(self, ref_id: int, args: dict[str, Any]) -> Response:
        """Write an EasyEDA Pro ``.epro2`` (:mod:`precis.pcb.epro_write`)
        off :meth:`_fab_model` — parts, pads, nets and the outline, so a
        colleague can open the board and keep working. Refuses synthesized
        pads unless ``args={'allow_synthesized': true}``. ``args={'verify':
        true}`` appends :func:`precis.pcb.epro_verify.verify_export`'s diff
        of the written bytes against the model. The response names the web
        UI download path, which rebuilds the same bytes on request."""
        allow_synthesized = bool(args.get("allow_synthesized"))
        build = self.epro_build(ref_id, allow_synthesized=allow_synthesized)
        if build is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        slug, model, warnings = build.slug, build.model, build.warnings
        exported, blob = build.exported, build.blob

        out_dir = self._export_dir(slug, args)
        path = out_dir / f"{pcb_epro_write.safe_stem(slug)}.epro2"
        path.write_bytes(blob)

        st = exported.stats
        if st["lines"] or st["arcs"] or st["vias"] or st["pours"]:
            copper_line = (
                f"copper: {st['lines']} line(s), {st['arcs']} arc(s), "
                f"{st['vias']} via(s) on {st['tracks']} track(s), "
                f"{st['pours']} pour zone(s) with cached fill where the "
                "source had it (rebuild copper in Pro after edits)."
            )
        else:
            copper_line = (
                "copper: none — the file carries the outline, parts, pads and "
                "nets only (run put(args={'op':'route'}) first for routing)."
            )
        head = (
            f"# exported {slug} → EPRO2 (EasyEDA Pro)\n{path}  "
            f"({len(blob):,} bytes zipped)\n"
            f"sha256: {build.sha256}\n"
            f"download (web UI, a human's browser): "
            f"{build.download_path(allow_synthesized=allow_synthesized)}\n"
            f"components: {st['components']}  pads: {st['pads']}  "
            f"nets: {st['nets']}\n"
            "No schematic is included, on purpose: Pro will report the import "
            "succeeded but show NO schematic pages — that is expected, this "
            "file carries the board only. Do NOT run 'Update PCB from "
            "schematic' in Pro — it would rewrite the netlist and destroy the "
            "board.\n"
            f"{copper_line}\n"
            "Verified: Reto opened a file from this writer in EasyEDA Pro "
            "on 2026-10-14 — import clean, parts and board content all "
            "present. Pour zones carry the source's cached fill; run "
            "rebuild copper after any edits."
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
        if args.get("verify"):
            # The file stays on the serving host, so the round trip an
            # agent can run is this one: the same bytes back through the
            # importer's reader, diffed against the model (gr495289).
            verdict = pcb_epro_verify.verify_export(model, blob, stats=st)
            head += "\n\n" + verdict.render()
        else:
            head += (
                "\nThe file is on the server, not readable through the MCP; "
                "args={'verify': true} re-reads it and diffs it against the board, "
                f"and put(kind='pcb', id='<new-slug>', args={{'op': 'import_epro', "
                f"'source': 'export:{slug}'}}) re-imports it (a dry run unless "
                "'dry_run': False)."
            )
        listing = "\n".join(sorted(exported.files))
        return Response(body=head + "\n\n```\n" + listing + "\n```")

    def _render_route(self, ref_id: int, args: dict[str, Any]) -> Response:
        """The §9 place↔route round-trip via Freerouting headless. Re-places
        (escalating annealing) and re-routes until the route completes or
        ``max_passes`` is hit. Degrades to a single ``.dsn``-only pass when no
        router is installed (the gate is at this step only). Every re-place
        passes the placer's legality rule before it is stored
        (:meth:`_refuse_illegal_placement`); kept as the external baseline
        the in-tree router (``op='route'``) is benchmarked against."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        # max(1,…): '0' is truthy, and 0 passes would report a .dsn that was
        # never written (the write lives inside the pass loop).
        max_passes = max(1, int(args.get("max_passes") or 3))
        base_iters = int(args.get("iters") or 1500)
        out_dir = self._export_dir(slug, args)

        measures = self.store.pcb_measures_list(ref_id)

        def place_fn(iters: int, seed: int) -> dict[str, Any]:
            res, _moved = self._place_and_store(
                ref_id, iters=iters, seed=seed, measures=measures, legal_only=True
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
            name=pcb_epro_write.safe_stem(slug),
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
        rs = pcb_route_state.route_state(self.store, ref_id, drc=False)
        rows, counts = rs.nets, rs.counts
        if not rows:
            return Response(body="no nets on this design yet")
        try:
            lengths = self._net_length_ratios(ref_id)
        except Exception:  # diagnostic column only; never break route-status
            log.warning("route-status len_ratio failed ref=%s", ref_id, exc_info=True)
            lengths = {}
        table_rows = [
            {
                "net": r["name"],
                "class": r["net_class"] or "—",
                "domain": r["domain"] or "electrical",
                "status": r["status"] + (f" ({r['note']})" if r.get("note") else ""),
                "len_ratio": lengths.get(r["name"], "—"),
            }
            for r in rows
        ]
        schema = ["net", "class", "domain", "status"]
        if lengths:
            schema.append("len_ratio")
        body = f"# route status — {format_route_summary(counts)}\n" + (
            render_agent_table(table_rows, schema=schema)
        )
        if lengths:
            body += (
                "\nlen_ratio = routed track mm / pad-to-pad spanning-tree mm "
                "(~1 = direct; large = detour, gr451277)"
            )
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        body += self._client_state_text(ref_id)
        last_route = (
            ((ref.meta or {}).get("last_route") or {}) if ref is not None else {}
        )
        # What the supply/client matching bought per role (gr491373):
        # the stored warm_start cost pair, same lines the job summary prints.
        for line in format_warm_start(last_route.get("warm_start")):
            body += f"\n{line}"
        loop_meta = last_route.get("loop")
        if loop_meta:
            # The loop run's one-line verdict; the full conflict digest
            # text stays in meta.last_route.loop.digest.
            first = str(loop_meta.get("digest") or "").split("\n", 1)[0]
            body += f"\n{first} (full digest: meta.last_route.loop)"
        return Response(body=body)

    def _net_length_ratios(self, ref_id: int) -> dict[str, str]:
        """``{net: 'N.NN'}`` routed-length ratio for nets with routed copper
        (:func:`precis.pcb.length_ratio.net_lengths`); empty when the board
        has none."""
        board = self.store.pcb_load(ref_id)["board"]
        if board is None:
            return {}
        copper = self.store.pcb_copper_list(int(board["board_id"]))
        if not any(c.get("ctype") == "track" for c in copper):
            return {}
        layers = [str(layer.get("name")) for layer in board["stackup"]]
        found = pcb_length_ratio.net_lengths(copper, self._drc_pads(ref_id, layers))
        return {n: f"{v.ratio:.2f}" for n, v in found.items() if v.ratio is not None}

    def _render_congestion(self, ref_id: int) -> Response:
        """The latest ``op='route'`` run's congestion digest — per-gap
        capacity warnings stamped onto ``refs.meta.last_route`` by the
        ``pcb_route`` job (backlog acceptance criterion: a routing failure
        must name the blocking gap, the participants, and the clearance
        arithmetic, not just report "unrouted")."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        if ref is None:
            raise NotFound(
                f"pcb id={ref_id} not found",
                next="re-resolve the design: get(kind='pcb', id='slug', "
                "view='congestion')",
            )
        last_route = (ref.meta or {}).get("last_route")
        if not last_route:
            return Response(
                body="no route run yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'op':'route'}) then re-check this view."
                + self._capacity_map_text(ref_id)
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
                str(r["status"])
                for r in pcb_route_state.route_state(self.store, ref_id, drc=False).nets
            )
            head += (
                f"\n⚠️ STALE — {ripped} net(s) ripped since that run; stored now: "
                f"{status.get('realized', 0)} realized, "
                f"{status.get('unrouted', 0)} unrouted (view='route-status')"
            )
            return Response(
                body=head
                + "".join(f"\n- {w}" for w in warnings)
                + self._capacity_map_text(ref_id)
            )
        if not warnings:
            # No tick under failed nets: "no over-capacity gaps" only rules
            # out ONE cause, and read beside "40 failed" it reads as a pass.
            if int(last_route.get("failed") or 0):
                return Response(
                    body=head + "\n(no over-capacity gaps — the failures have "
                    "another cause; per-net status: view='route-status')"
                    + self._capacity_map_text(ref_id)
                )
            return Response(
                body=head
                + "\n(no over-capacity gaps ✓)"
                + self._capacity_map_text(ref_id)
            )
        return Response(
            body=head
            + "\n"
            + "\n".join(f"- {w}" for w in warnings)
            + self._capacity_map_text(ref_id)
        )

    def _capacity_map_text(self, ref_id: int) -> str:
        """The capacity evidence for ``view='congestion'`` — since the
        grid router's removal this is the CDT portal map alone
        (``docs/backlog/pcb-vector-router.md`` slice 5): per signal layer,
        free triangles, portals, portals that take no track, and the
        capacity spread, measured on the pre-route static copper (pads +
        fixed copper). Empty string when the board has nothing to
        measure.

        The build costs seconds on a real board, so the rendered text is
        cached per ref in ``self._capacity_map_cache`` keyed on the
        board's ``content_hash`` — the digest is built from exactly the
        rows the map reads (graph, features, the latest netlist snapshot,
        planes, fixed_copper), so a changed board always rebuilds and an
        unchanged board's repeat reads are free. Routes are not passed
        at all: the map describes the pre-route static board, so neither
        realized copper nor authored route topology can stale it."""
        try:
            graph = self.store.pcb_graph(ref_id)
            if not graph.get("nets"):
                return ""
            board = graph.get("board") or {}
            board_id = board.get("board_id")
            fixed = (
                self.store.pcb_fixed_copper_list(int(board_id))
                if board_id is not None
                else []
            )
            features = self.store.pcb_features_list(ref_id)
            resolved = self.store.pcb_netlist_snapshot_latest(ref_id)
            graph = pcb_resolved.apply(graph, resolved)
            planes = self.store.pcb_planes_list(ref_id)
            key = pcb_session.content_hash(
                graph,
                {"netlist_snapshot": resolved and resolved["snapshot_id"]},
                session_state={
                    "features": features,
                    "planes": planes,
                    "fixed_copper": fixed,
                },
            )
            hit = self._capacity_map_cache.get(ref_id)
            if hit is not None and hit[0] == key:
                return hit[1]
            ir = pcb_session.build_ir(
                graph,
                outline=pcb_session.outline_from_features(features),
                mounting_holes=pcb_session.mounting_holes_from_features(features),
                footprints_by_lcsc=self.store.pcb_footprints_for(ref_id),
                local_footprints_by_name=self.store.pcb_local_footprints_for(ref_id),
                fixed_copper=fixed,
            )
            pcb_session.apply_plane_assignments(ir, planes)
            footprints = pcb_session.footprints_by_refdes(
                ir,
                self.store.pcb_footprints_for(ref_id),
                local_footprints_by_name=self.store.pcb_local_footprints_for(ref_id),
                local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(
                    graph
                ),
            )
            capability = self._stackup_capability(
                ir.stackup,
                pcb_profiles.board_profile((graph.get("board") or {}).get("meta")),
            ) or capability_for("4layer")
            config = pcb_realize.RealizeConfig(
                fab_caps=capability, class_rules=graph.get("net_classes")
            )
            ctx = pcb_realize._route_context(
                ir, list(range(ir.n_segments)), config, footprints, fixed
            )
            if ctx is None or not ctx.route_ids:
                return ""
            (track_mm, clearance_mm), _n = collections.Counter(
                (
                    ctx.rules_by_net[int(ir.seg_net[s])].track_width_mm,
                    ctx.rules_by_net[int(ir.seg_net[s])].clearance_mm,
                )
                for s in ctx.route_ids
            ).most_common(1)[0]
            outline = pcb_session.outline_from_features(features)
            if not outline:
                bbox = pcb_realize._outline_bbox(ir)
            else:
                bbox = None
            if not outline and bbox is None:
                # No authored outline at all (the old grid fabricated its
                # extent from the cell bounds): measure around the pins —
                # a diagnostic view, not a routing constraint.
                pts = [
                    p
                    for pid in range(ir.n_pins)
                    if (p := pcb_ir.pin_point(ir, pid)) is not None
                ]
                if not pts:
                    return ""
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                m = 5.0
                bbox = (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)
            if not outline:
                assert bbox is not None
                outline = [
                    [bbox[0], bbox[1]],
                    [bbox[2], bbox[1]],
                    [bbox[2], bbox[3]],
                    [bbox[0], bbox[3]],
                ]
            text = self._portal_map_text(
                [str(ir.stackup[li].get("name")) for li in ctx.signal_layers],
                pcb_session._copper_model(
                    ir,
                    [{**row, "net": row.get("net") or ""} for row in fixed],
                    footprints=footprints,
                ),
                outline,
                track_mm=track_mm,
                clearance_mm=clearance_mm,
            )
            if text:
                text = "\n\n# capacity map — portal model\n" + text.lstrip()
            self._capacity_map_cache[ref_id] = (key, text)
            return text
        except Exception as exc:  # the digest above still renders without it
            return f"\n\n# capacity map — unavailable ({exc})"

    @staticmethod
    def _portal_map_text(
        layers: list[str],
        model: dict[str, Any],
        outline: list[list[float]],
        *,
        track_mm: float,
        clearance_mm: float,
    ) -> str:
        """The CDT portal map under the tile map (``docs/backlog/
        pcb-vector-router.md`` slice 5, a probe): per signal layer, free
        triangles, portals, portals that take no track, and the capacity
        spread, measured on the same pre-route copper (pads + fixed
        copper). Its own failure says so without hiding the tile map."""
        try:
            graphs = {
                lay: pcb_topo.build_region_graph(
                    pcb_topo.board_obstacles(model, lay),
                    outline,
                    lay,
                    track_mm=track_mm,
                    clearance_mm=clearance_mm,
                )
                for lay in layers
            }
            summary = pcb_topo.summarize(graphs, [])
        except Exception as exc:
            return f"\n\n# portal map — unavailable ({exc})"
        lines = [
            "\n\n# portal map — CDT of free space (probe: kernel bodies "
            f"drawn outward {pcb_topo.DELTA_MM * 1000:.0f} µm, modal-class lanes)"
        ]
        for lay, s in summary["layers"].items():
            caps = [c for c, n in s["cap_hist"].items() for _ in range(n)]
            spread = f", capacity median {sorted(caps)[len(caps) // 2]}" if caps else ""
            lines.append(
                f"- {lay}: {s['triangles']} triangles, {s['portals']} portals, "
                f"{s['zero_cap']} take no track{spread}"
            )
        return "\n".join(lines)

    def _render_planes(self, ref_id: int) -> Response:
        """Plane assignments (``pcb_planes``) — which nets are
        plane-served on which layer, so the LLM can see the
        fanout-vs-route split before running ``op='route'``. Shows
        ``source`` so a reader can tell a human's explicit
        ``op='plane_net'`` instruction (``authored``) apart from the
        anneal's own settled decision (``derived``, written back by the
        ``pcb_route`` job, gr267526) — an authored row is never touched
        by a later route run; a derived one is replaced each run.

        The table is every EFFECTIVE assignment the router will pour
        (:func:`precis.pcb.planes.resolve_assignments`, pcb-stackup-regions
        slices 3-4): the rows, plus the stackup's own ``plane_net``
        (``stackup``) and ``fill`` (``fill``) declarations, each with its
        region, priority and region area; a declaration that pours nothing
        gets a note. The pour-quality table then covers every stored pour,
        every layer (gr495395)."""
        rows = self.store.pcb_planes_list(ref_id)
        design = self.store.pcb_load(ref_id)
        board = design["board"]
        assignments, notes = pcb_planes.resolve_assignments(
            list((board or {}).get("stackup") or []),
            rows,
            [str(n["name"]) for n in design["nets"]],
        )
        if not assignments:
            return Response(
                body="no plane assignments yet"
                + "".join(f"\nnote: {n}" for n in notes)
                + "\n\nNext: put(kind='pcb', id='slug', args={'op':'stackup',"
                "'preset':'4l-two-sided'}) or args={'op':'plane_net',"
                "'layer':'In1.Cu','net':'GND'}"
            )

        def area_text(region: Any) -> str:
            poly = pcb_planes.region_polygon(region)
            return "—" if poly is None else f"{poly.area:.0f}"

        table_rows = [
            {
                "layer": a.layer,
                "net": a.net,
                "source": a.source,
                "region": self._region_text(a.region),
                "priority": a.priority,
                "area_mm2": area_text(a.region),
            }
            for a in sorted(assignments, key=lambda a: (a.layer, -a.priority, a.net))
        ]
        body = f"# planes — {len(assignments)} assignment(s)\n" + render_agent_table(
            table_rows,
            schema=["layer", "net", "source", "region", "priority", "area_mm2"],
        )
        body += "".join(f"\nnote: {n}" for n in notes)
        # Pour quality on the stored pours (pcb-stackup-regions slice 1):
        # pieces per (net, layer), the largest piece's area share and the
        # narrowest neck, so a cut-up plane shows before the DRC does.
        pours = (
            [
                row
                for row in self.store.pcb_copper_list(int(board["board_id"]))
                if row.get("ctype") == "pour" and not row.get("fixed")
            ]
            if board is not None
            else []
        )
        quality = pcb_planes.pour_quality(pours)
        if quality:
            body += "\n\n## pour quality\n" + render_agent_table(
                [
                    {
                        "net": q.net,
                        "layer": q.layer,
                        "pieces": q.fragments,
                        "largest": f"{q.largest_share:.0%}",
                        "neck_mm": (
                            f">{pcb_planes.NECK_PROBE_MAX_MM:g}"
                            if q.min_neck_mm is None
                            else f"{q.min_neck_mm:.2f}"
                        ),
                        "area_mm2": f"{q.area_mm2:.0f}",
                    }
                    for q in quality
                ],
                schema=["net", "layer", "pieces", "largest", "neck_mm", "area_mm2"],
            )
        else:
            body += "\n\nno pours stored yet — run op='route' to pour the planes"
        return Response(body=body)

    @staticmethod
    def _region_text(region: Any) -> str:
        """``whole board`` for NULL, else ``polygon N pts`` (``+K holes``)."""
        if region is None:
            return "whole board"
        pts = region.get("points") if isinstance(region, dict) else region
        holes = region.get("holes") if isinstance(region, dict) else None
        if isinstance(pts, list):
            return f"polygon {len(pts)} pts" + (
                f" +{len(holes)} holes" if holes else ""
            )
        return "polygon"

    def _client_channel_rows(
        self, ref_id: int
    ) -> tuple[pcb_matching.MatchModel, list[pcb_matching.ChannelRow]]:
        """``((clients, supplies, roles), channel rows)`` for a design's
        ``client_of`` pins under the current assignment; rows are empty when
        it declares no clients. Shared by ``channel-map`` and
        ``route-status``."""
        from precis.workers.job_types import pcb_route as pcb_route_job

        graph = self.store.pcb_graph(ref_id)
        model = pcb_route_job._matching_model(graph, [])
        clients, supplies, _roles = model
        if not clients:
            return model, []
        ir = self._build_ir(ref_id, graph)
        pin_nets: dict[tuple[str, str], str | None] = {}
        for p in range(ir.n_pins):
            net = int(ir.pin_net[p])
            pin_nets[
                (str(ir.instance_refdes[int(ir.pin_instance[p])]), str(ir.pin_label[p]))
            ] = str(ir.net_name[net]) if net >= 0 else None
        return model, pcb_matching.channel_map(clients, supplies, pin_nets)

    def _client_state_text(self, ref_id: int) -> str:
        """The ``clients: N (a bound, b unresolved, c unmatched)`` block for
        ``route-status``; empty for a design with no ``client_of`` pins.
        unmatched = the capacity/voltage gate cannot serve the client (an
        error); unresolved = servable but its net carries no supplier
        terminal yet (a phase fact, the solve has not bound it)."""
        (clients, supplies, roles), rows = self._client_channel_rows(ref_id)
        if not clients:
            return ""
        try:
            gate = pcb_matching.solve(
                clients, supplies, roles, lambda _r, _p: (0.0, 0.0)
            ).unsupplied
        except ValueError:  # invalid model: view='check' says not-checked
            gate = ()
        unmatched = {f"{inst}/{pin}" for inst, pin in gate}
        bound = 0
        unresolved: list[str] = []
        unmatched_pins: list[str] = []
        for r in rows:
            pin = f"{r.client[0]}/{r.client[1]}"
            if pin in unmatched:
                unmatched_pins.append(pin)
            elif r.supplier is None:
                unresolved.append(pin)
            else:
                bound += 1
        out = (
            f"\nclients: {len(rows)} ({bound} bound, {len(unresolved)} "
            f"unresolved, {len(unmatched_pins)} unmatched)"
        )
        for label, pins, note in (
            ("unmatched", unmatched_pins, "error: no admissible supplier terminal"),
            ("unresolved", unresolved, "no supplier terminal on its net yet"),
        ):
            if pins:
                shown = ", ".join(pins[:10])
                more = f" (+{len(pins) - 10} more)" if len(pins) > 10 else ""
                out += f"\n  {label} ({note}): {shown}{more}"
        return out

    def _render_channel_map(self, ref_id: int, args: dict[str, Any]) -> Response:
        """``view='channel-map'`` — which supply terminal each ``client_of``
        pin is wired to under the current (swap-applied) assignment, as CSV
        or JSON. Derived and regenerated on read, never stored."""
        fmt = str(args.get("format") or "csv").lower()
        if fmt not in ("csv", "json"):
            raise BadInput(
                f"unknown channel-map format {fmt!r}", next="format='csv'|'json'"
            )
        (clients, _supplies, _roles), rows = self._client_channel_rows(ref_id)
        if not clients:
            return Response(
                body="channel-map: no clients - declare client_of on pins "
                "and supplies on the supplying part's components entry"
            )
        bound = sum(1 for r in rows if r.supplier is not None)

        def cell(pin: tuple[str, str] | None) -> str:
            return f"{pin[0]}/{pin[1]}" if pin else ""

        recs = [
            {
                "role": r.role,
                "client": cell(r.client),
                "net": r.net or "",
                "supplier": cell(r.supplier),
            }
            for r in rows
        ]
        head = (
            "# channel-map - derived from the current assignment, regenerated on "
            f"read; {len(rows)} clients, {bound} bound, {len(rows) - bound} unbound\n"
        )
        if fmt == "json":
            return Response(body=head + json.dumps(recs, indent=1))
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["role", "client", "net", "supplier"])
        for rec in recs:
            w.writerow([rec["role"], rec["client"], rec["net"], rec["supplier"]])
        return Response(body=head + buf.getvalue())

    def _render_stackup(self, ref_id: int) -> Response:
        """The board's copper stackup (``pcb_boards.stackup``) — one row
        per layer in stack order, as the ROUTER reads it. gr486893: the
        write side (``op='stackup'``/``op='plane_net'``, the ``.epro2``
        import) has existed; the read side was a prod-psql detour.

        ``routable``/``pourable`` come from
        :func:`precis.pcb.ir.layer_is_routable`/``layer_is_pourable``,
        not the stored ``role`` — a ``routable: true`` plane layer shows
        ``yes``; both yes renders ``mixed``. ``plane`` is the layer's
        effective plane (:func:`precis.pcb.planes.resolve_assignments`):
        a live ``pcb_planes`` row (``src`` = authored/derived), else the
        entry's own ``plane_net`` (``src=stackup``) — which the router
        pours without a row since pcb-stackup-regions slice 3 (gr495287:
        it used to be a dead declaration on every board born on the old
        default). ``fill``, ``dir``, ``cost`` and ``regions`` columns
        appear only when some layer carries the key, so an older board's
        table reads as it did; the header names the preset, if any.

        The closing line names the nets the router will skip — a pour
        carries them, their pads drop to it by via instead of routing
        end-to-end. It is the same resolution the ``pcb_route`` job
        applies into ``ir.net_plane_layers``; a declaration that pours
        nothing (no such net, or another net's whole-board plane on the
        layer) gets a note so the view never reports a skip the router
        will not make. The same discipline covers ``pour_nets`` (gr486958
        — fills the source carries a net by, on still-signal layers): the
        ``pours`` column shows the record per layer, and recorded nets
        with no assignment row get a note saying the router still routes
        them — several pour nets on one sheet are recorded but assigned to
        none, so a mixed layer must not read as more skipped than it is."""
        design = self.store.pcb_load(ref_id)
        board = design.get("board") or {}
        stackup = list(board.get("stackup") or [])
        if not stackup:
            return Response(
                body="no stackup stored for this board\n\nNext: "
                "put(kind='pcb', id='slug', args={'op':'stackup','layers':[...]})"
            )
        planes = self.store.pcb_planes_list(ref_id)
        assignments, notes = pcb_planes.resolve_assignments(
            stackup, planes, [str(n["name"]) for n in design["nets"]]
        )
        by_layer: dict[str, list[Any]] = {}
        for a in assignments:
            if a.source != "fill":
                by_layer.setdefault(a.layer, []).append(a)
        # Physical columns (copper weight, dielectric, …) are render-only:
        # ``validate_stackup`` admits none today, but a stored row may
        # carry them (the ``pcb_boards.stackup`` jsonb is shaped for it),
        # so show whatever keys exist rather than a fixed schema.
        extra = sorted({k for e in stackup for k in e} - pcb_ir.STACKUP_LAYER_KEYS)
        # Known keys get a column only when some layer carries them,
        # keeping a board without them reading exactly as it did.
        has = {k: any(k in e for e in stackup) for k in pcb_ir.STACKUP_LAYER_KEYS}
        has_pours = any(e.get("pour_nets") for e in stackup)
        schema = ["layer", "role", "plane", "src"]
        if has["fill"]:
            schema.append("fill")
        if has_pours:
            schema.append("pours")
        schema += ["routable", "pourable"]
        for key, col in (
            ("direction", "dir"),
            ("route_cost", "cost"),
            ("power_regions", "regions"),
            ("reference", "ref"),
        ):
            # The reference has a default (the nearest poured layer), so it
            # shows wherever the stackup speaks the slice-3 keys at all.
            if has[key] or (key == "reference" and has["direction"]):
                schema.append(col)
        schema += extra
        rows = []
        for i, entry in enumerate(stackup):
            name = str(entry.get("name"))
            routable = pcb_ir.layer_is_routable(entry)
            pourable = pcb_ir.layer_is_pourable(entry)
            role = "mixed" if routable and pourable else str(entry.get("role") or "—")
            assigned = by_layer.get(name) or []
            recorded = [str(n) for n in entry.get("pour_nets") or []]
            row = {
                "layer": name,
                "role": role,
                "plane": ",".join(a.net for a in assigned) or "—",
                "src": ",".join(a.source for a in assigned) or "—",
                "fill": str(entry.get("fill") or "—"),
                "pours": ",".join(recorded) if recorded else "—",
                "routable": "yes" if routable else "no",
                "pourable": "yes" if pourable else "no",
                "dir": str(entry.get("direction", "—")),
                "cost": f"{pcb_ir.layer_route_cost(entry):g}x",
                "regions": "power" if entry.get("power_regions") else "—",
                "ref": str(pcb_ir.reference_layer(stackup, i) or "—"),
            }
            row.update({k: entry.get(k, "—") for k in extra})
            rows.append(row)
        meta = board.get("meta") or {}
        preset = meta.get("stackup_preset")
        head = (
            f"# stackup — {board.get('name', 'main')}, {len(stackup)} layer(s)"
            + (
                f", preset {preset} (sides: {meta.get('placement_sides', 'both')})"
                if preset
                else ""
            )
            + "\n"
        )
        skip = ", ".join(
            f"{a.net} ({a.layer}, {a.source})"
            for a in sorted(assignments, key=lambda a: (a.net, a.layer))
        )
        skip_line = (
            "router skips (a plane/pour carries them): " + skip
            if skip
            else "router skips: none — every net routes as traces"
        )
        if notes:
            skip_line += "\nnote: " + "; ".join(notes)
        # Recorded but unassigned pour nets: the router reads pcb_planes
        # rows, so a pour_nets entry without one is still routed — the
        # mixed case (several nets on one sheet, assigned to none) lands
        # here by design, and the view must not imply a skip it won't
        # make.
        unassigned_pours = []
        for e in stackup:
            recorded = [str(n) for n in e.get("pour_nets") or []]
            live = {a.net for a in assignments if a.layer == str(e.get("name"))}
            missing = [n for n in recorded if n not in live]
            if missing:
                unassigned_pours.append(f"{e['name']} pour_nets={','.join(missing)}")
        if unassigned_pours:
            skip_line += (
                "\nnote: "
                + ", ".join(unassigned_pours)
                + " — recorded in the stackup but no assignment row, so the "
                "router still routes them"
            )
        return Response(
            body=head + render_agent_table(rows, schema=schema) + "\n" + skip_line
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

    def _render_availability(self, ref_id: int) -> Response:
        """``view='availability'`` (gr485250): one row per component TYPE —
        refdes set, qty, C-number, library class, stock, unit price and
        every lifecycle/discontinued signal the catalog row carries
        (``params._lifecycle`` written by the live listing:
        merged/alt successor, no-buy reason, idle, restock estimate;
        plus ``part_availability.discontinued``). Parts rows older than
        ``_AVAIL_STALE_S`` — or absent from ``parts`` entirely — get a
        bounded live refresh from the JLCPCB Open API on the way out."""
        rows = self.store.pcb_bom_availability(ref_id)
        if not rows:
            return Response(body="no components on this board")
        now = datetime.now(UTC)
        stale = sorted(
            {
                str(r["part_lcsc"])
                for r in rows
                if r["part_lcsc"]
                and (
                    r["refreshed_at"] is None
                    or (now - r["refreshed_at"]).total_seconds() > _AVAIL_STALE_S
                )
            }
        )
        head = f"# availability — {len(rows)} BOM line(s)"
        outcomes: dict[str, str] = {}
        if stale:
            fn = self._live_refresh or pcb_jlc_api.live_refresh_many
            outcomes = fn(self.store, stale)
            hits = sum(1 for v in outcomes.values() if v == "hit")
            miss = sum(1 for v in outcomes.values() if v == "miss")
            errs = sorted(k for k, v in outcomes.items() if v.startswith("error"))
            skipped = len(stale) - len(outcomes)
            head += (
                f"\nlive refresh ({len(stale)} stale): {hits} re-catalogued, "
                f"{miss} absent from the JLCPCB catalog"
                + (f", {len(errs)} failed: {', '.join(errs)}" if errs else "")
                + (f", {skipped} skipped (bounded)" if skipped else "")
            )
            if hits:
                rows = self.store.pcb_bom_availability(ref_id)

        def _age(ts: Any) -> str:
            if ts is None:
                return "—"
            s = (now - ts).total_seconds()
            if s < 3600:
                return f"{max(0, int(s // 60))}m"
            if s < 86400:
                return f"{int(s // 3600)}h"
            return f"{int(s // 86400)}d"

        table = []
        for r in rows:
            lcsc = str(r["part_lcsc"]).strip().upper() if r["part_lcsc"] else None
            price = pcb_catalog.min_unit_price(r.get("price"))
            refdes = r["refdes"]
            table.append(
                {
                    "refdes": ",".join(refdes[:6]) + ("…" if len(refdes) > 6 else ""),
                    "qty": r["qty"],
                    "lcsc": lcsc or "—",
                    "mfr_part": r["mfr_part"] or "—",
                    "lib": (
                        "—"
                        if lcsc is None or r["basic"] is None
                        else ("basic" if r["basic"] else "ext")
                    ),
                    "stock": r["stock"] if r["stock"] is not None else "—",
                    "$ea": f"{price:.4g}" if price is not None else "—",
                    "lifecycle": _availability_lifecycle(r, outcomes.get(lcsc or "")),
                    "fetched": _age(r["refreshed_at"]),
                }
            )
        n_uncatalogued = sum(1 for r in rows if r["part_lcsc"] is None)
        if n_uncatalogued:
            head += f"\n{n_uncatalogued} line(s) have no LCSC number (not orderable at JLCPCB)"
        return Response(
            body=head
            + "\n"
            + render_agent_table(
                table,
                schema=[
                    "refdes",
                    "qty",
                    "lcsc",
                    "mfr_part",
                    "lib",
                    "stock",
                    "$ea",
                    "lifecycle",
                    "fetched",
                ],
            )
        )

    def _render_drc(self, ref_id: int, args: dict[str, Any] | None = None) -> Response:
        """Geometric DRC (pcb-guided-place-route Slice 8, :mod:`precis.pcb.
        drc`) — the L5 check. Superseded ``eyes.drc_lite`` (graph-shape
        sanity only, no geometry); the graph-feasibility half of DRC
        (``ir.py``, L0-L4) stays inside the optimizer, not this view.
        Read-only (gr491591, Reto 2026-10-16): the view computes the run
        and diffs it against the last recorded one, but writes nothing.
        A durable run — what ``netlist_drc_clean`` and a reviewer read
        afterward — is recorded only by ``put(args={'op':'check'})`` (or
        ``op='review'``).

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
        computed here (not on write) avoids.

        ``args={'severity'?: 'error'|'warn', 'rule'?: 'a'|'a,b'}`` filter
        the TABLE only (gr477797); the header counts stay board-wide so a
        filtered page is never read as a clean board. Rows sort errors
        first (stable within a severity, so run order is kept). Past
        ``_DRC_SUMMARY_MIN`` findings a ``by rule:`` line gives the
        severity x rule counts.

        An unchanged board is answered from its last recorded run
        (gr496242 — the run's design-revision stamp still matches); the
        ack says which run and when. ``args={'fresh': true}`` forces the
        inline re-run."""
        args = args or {}
        severity = str(args.get("severity") or "").strip().lower()
        if severity and severity not in ("error", "warn"):
            raise BadInput(
                f"view='drc' args.severity={severity!r} not recognized",
                options=["error", "warn"],
            )
        rules = {r.strip() for r in str(args.get("rule") or "").split(",") if r.strip()}
        design = self.store.pcb_load(ref_id)
        if design["board"] is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        ran = self._drc_run(ref_id, design, reuse=not args.get("fresh"))
        if ran is None:
            return Response(
                body="no realized copper yet\n\nNext: put(kind='pcb', "
                "id='slug', args={'op':'route'}) to realize copper, then "
                "re-check this view."
            )
        run_id, findings, pads_only, delta, baseline, reused = ran
        n_error = sum(1 for f in findings if f.severity == "error")
        n_warn = len(findings) - n_error
        head = (
            f"# DRC — {_drc_run_label(run_id)} — {n_error} error(s), {n_warn} warn(s)"
        )
        head += "\n" + _drc_delta_line(delta) + _drc_stale_line(baseline)
        if reused:
            head += _drc_reused_line(ran)
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
        gone = _drc_gone_block(delta) + self._drc_record_hint(ref_id, run_id)
        if not findings:
            return Response(body=head + "\n— no findings ✓" + gone)
        if len(findings) >= _DRC_SUMMARY_MIN:
            tally: dict[tuple[str, str], int] = {}
            for f in findings:
                tally[(f.rule, f.severity)] = tally.get((f.rule, f.severity), 0) + 1
            head += "\nby rule: " + ", ".join(
                f"{rule} {n} {sev}"
                for (rule, sev), n in sorted(
                    tally.items(), key=lambda kv: (kv[0][1] != "error", -kv[1], kv[0])
                )
            )
        all_rules = {f.rule for f in findings}
        if rules - all_rules:
            raise BadInput(
                f"view='drc' args.rule={sorted(rules - all_rules)!r} matches "
                "no finding in this run",
                options=sorted(all_rules),
            )
        shown = [
            f
            for f in findings
            if (not severity or f.severity == severity)
            and (not rules or f.rule in rules)
        ]
        if len(shown) != len(findings):
            head += f"\nfiltered: showing {len(shown)} of {len(findings)} finding(s)"
        if not shown:
            return Response(body=head + "\n— no findings match the filter" + gone)
        shown.sort(key=lambda f: f.severity != "error")
        row_ids = {
            id(f): rid
            for f, rid in zip(findings, pcb_drc.finding_display_ids(findings))
        }
        findings = shown
        rows = [
            {
                "id": row_ids[id(f)],
                "severity": f.severity,
                "rule": f.rule,
                "where": f.where,
                "margin_mm": "" if f.margin_mm is None else f"{f.margin_mm:+.3f}",
                "delta": delta.label(f),
                "detail": f.detail,
            }
            for f in findings
        ]
        schema = ["id", "severity", "rule", "where", "margin_mm", "delta", "detail"]
        if delta.previous_run_id is None:
            schema.remove("delta")
        return Response(
            body=head + "\n" + render_agent_table(rows, schema=schema) + gone
        )

    def _render_check(
        self,
        ref_id: int,
        args: dict[str, Any] | None = None,
        *,
        record: bool = False,
    ) -> Response:
        """The ONE check surface (pcb-lazy-netlist-and-checks.md §1g, Slice
        8) — geometric, netlist and ERC rules report on the same finding
        list, filterable on ``rule``/``severity``/``phase``. ``view='drc'``
        stays as this surface's preset for continuity (the run it shows,
        in the table it has always rendered); this view is the surface
        itself.

        ``args.phase`` judges the board AT that phase — overriding the
        authored ``op='phase'`` claim and the derived default — and it is
        a real gate, not a label: ``netlist`` runs only the netlist/flag
        rules and never reads ``pcb_copper``; ``placed`` is the pads-only
        run; ``routed``/``fab`` the full pass. Severities are tiered by
        the eval phase (:func:`precis.pcb.drc.severity_at`): an unrouted
        net reads ``todo`` before ``routed`` and ``error`` at/after it —
        and ``unconnected_pin`` warns below ``fab``, errors at it
        (fab-tier severities need the authored claim; derive_phase never
        invents ``fab``). At ``netlist`` the per-net ``unrouted`` rows
        collapse to one head line — expected state, not a finding
        (gr485271); ``args.rule='unrouted'`` expands them.

        Read-only (gr491591, Reto 2026-10-16): the view writes no
        ``pcb_drc_runs``/``pcb_drc_findings`` rows; ``new / still /
        resolved`` below is against the board's last RECORDED run by
        ``finding_key`` — the same identity ``view='drc'`` reports.
        ``record=True`` is ``put(args={'op':'check'})``: the same run,
        persisted as the new baseline.

        An unchanged board is answered from that recorded run
        (gr496242 — its design-revision stamp still matches); the ack
        names the run and its record time. ``args={'fresh': true}``
        forces the inline re-run — and, for ``op='check'``, a new
        recorded run."""
        args = args or {}
        phase_arg = str(args.get("phase") or "").strip().lower()
        if phase_arg and phase_arg not in pcb_drc.PHASES:
            raise BadInput(
                f"view='check' args.phase={phase_arg!r} not recognized",
                options=list(pcb_drc.PHASES),
            )
        severity = str(args.get("severity") or "").strip().lower()
        if severity and severity not in ("error", "warn", "todo", "info"):
            raise BadInput(
                f"view='check' args.severity={severity!r} not recognized",
                options=["error", "warn", "todo", "info"],
            )
        rules = {r.strip() for r in str(args.get("rule") or "").split(",") if r.strip()}
        design = self.store.pcb_load(ref_id)
        if design["board"] is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        phase = phase_arg or self._design_phase(design)
        ran = self._drc_run(
            ref_id,
            design,
            eval_phase=phase,
            eval_phase_claimed=bool(phase_arg),
            erc=True,
            record=record,
            reuse=not args.get("fresh"),
        )
        if ran is None:
            return Response(
                body=f"# check — phase {phase}\n— nothing the rules at this "
                "phase can check (no pins, pads or copper yet)"
            )
        run_id, findings, pads_only, delta, baseline, reused = ran

        # Not-checked rows present as ``info`` — the same ruling the se
        # completeness findings use: "we could not run to a verdict" is
        # honest information, not a warn-tier complaint, so a clean board
        # reads clean and the delta table isn't buried in them. The row
        # still PERSISTS at its base severity (the findings column's
        # CHECK is error|warn) so the finding_key delta stays stable.
        def _sev(f: pcb_drc.DrcFinding) -> str:
            return pcb_drc.display_severity(f, phase)

        tiered = [(f, _sev(f)) for f in findings]
        # gr485271: at netlist phase an unrouted net is the EXPECTED
        # state, not a finding to read — the per-net rows (151 on a
        # blank board) bury the real netlist findings, so the TABLE
        # collapses them into one head line. The rows still persist in
        # the run and stay in its delta as ``still``: dropping them from
        # the persisted set would report every standing unrouted finding
        # ``gone`` on each netlist run, and ``netlist_drc_clean`` reads
        # the latest run's rows. An explicit ``args.rule='unrouted'``
        # still expands them.
        n_unrouted = 0
        if phase == "netlist" and "unrouted" not in rules:
            n_unrouted = sum(1 for f, _s in tiered if f.rule == "unrouted")
            tiered = [(f, s) for f, s in tiered if f.rule != "unrouted"]
        n_error = sum(1 for _f, s in tiered if s == "error")
        n_warn = sum(1 for _f, s in tiered if s == "warn")
        # The collapsed unrouted rows are still todos (gr488911): the head
        # tally is the same with or without the expanding rule filter.
        n_todo = sum(1 for _f, s in tiered if s == "todo") + n_unrouted
        n_info = sum(1 for _f, s in tiered if s == "info")
        authored = str((design["board"].get("meta") or {}).get("phase") or "")
        phase_src = (
            "args"
            if phase_arg
            else ("authored" if authored in pcb_drc.PHASES else "derived")
        )
        head = (
            f"# check — {_drc_run_label(run_id)} — phase {phase} ({phase_src}) — "
            f"{n_error} error(s), {n_warn} warn(s)"
            + (f", {n_todo} todo" if n_todo else "")
        )
        if delta.previous_run_id is None:
            head += (
                "\nvs previous run: none to compare against (first run with "
                "finding ids on this board) — every finding below is reported "
                "without a new/still verdict"
            )
        else:
            n_worse = len(delta.worse)
            head += (
                f"\nvs run {delta.previous_run_id[:8]}: {len(delta.new)} new, "
                f"{len(delta.still)} still"
                + (f" ({n_worse} worse)" if n_worse else "")
                + f", {len(delta.gone)} resolved"
            ) + _drc_stale_line(baseline)
        if reused:
            head += _drc_reused_line(ran)
        n_synth = sum(1 for f in findings if f.rule == "synthesized_footprint")
        if n_synth:
            head += (
                f"\n{n_synth} part(s) have no cached footprint — checked at a "
                "synthesized bound (see the synthesized_footprint finding(s) "
                "below); their results are not a verdict"
            )
        if pads_only:
            head += "\n(pads-only check — no routed copper in scope)"
        if n_info:
            head += (
                f"\nnot fully checked — {n_info} rule(s) could not reach a "
                "verdict (info rows)"
            )
        if n_unrouted:
            head += (
                f"\n{n_unrouted} net(s) unrouted — expected before routing "
                "(args={'rule':'unrouted'} lists them)"
            )
        # An unknown rule is refused BEFORE the zero-findings answer — a
        # filter that names nothing that ran is an input error, never a
        # quiet "no findings".
        all_rules = {f.rule for f in findings}
        if rules - all_rules:
            raise BadInput(
                f"view='check' args.rule={sorted(rules - all_rules)!r} matches "
                "no finding in this run",
                options=sorted(all_rules),
            )
        gone = _drc_gone_block(delta) + self._drc_record_hint(ref_id, run_id)
        if not findings:
            return Response(body=head + "\n— no findings ✓" + gone)
        shown = [
            (f, sev)
            for f, sev in tiered
            if (not severity or sev == severity) and (not rules or f.rule in rules)
        ]
        if len(shown) != len(tiered):
            head += f"\nfiltered: showing {len(shown)} of {len(tiered)} finding(s)"
        if not shown:
            # Empty WITHOUT a filter can only be the netlist unrouted
            # collapse — the board is clean, not "no findings match".
            tail = (
                "— no findings match the filter"
                if (severity or rules)
                else "— no findings ✓"
            )
            return Response(body=head + "\n" + tail + gone)
        # Explicit severity ladder, stable within a tier: error > warn >
        # todo (the presentation tier) > info.
        shown.sort(
            key=lambda fs: {"error": 0, "warn": 1, "todo": 2, "info": 3}.get(fs[1], 4)
        )
        row_ids = {
            id(f): rid
            for f, rid in zip(findings, pcb_drc.finding_display_ids(findings))
        }
        rows = [
            {
                "id": row_ids[id(f)],
                "severity": sev,
                "rule": f.rule,
                "where": f.where,
                "margin_mm": "" if f.margin_mm is None else f"{f.margin_mm:+.3f}",
                "delta": delta.label(f),
                "detail": f.detail,
            }
            for f, sev in shown
        ]
        schema = ["id", "severity", "rule", "where", "margin_mm", "delta", "detail"]
        if delta.previous_run_id is None:
            schema.remove("delta")
        return Response(
            body=head + "\n" + render_agent_table(rows, schema=schema) + gone
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
        """Delegates to :func:`precis.pcb.check_run.rule_inputs`; shared by
        the check run and :meth:`_validity_findings`."""
        return check_run.rule_inputs(design, courtyard_local, capability)

    @staticmethod
    def _clearance_net_rules(
        design: dict[str, Any], capability: CapabilityRow
    ) -> dict[str, NetRules]:
        """Net name -> resolved :class:`NetRules` for the clearance checks:
        what :meth:`_drc_rule_inputs` judges by and what
        :meth:`_legalize_generator_seeds` places seeds against, one source."""
        return pcb_session.clearance_net_rules(design, capability)

    def _design_phase(self, design: dict[str, Any]) -> str:
        """Delegates to :func:`precis.pcb.check_run.design_phase`."""
        return check_run.design_phase(design)

    def _drc_record_hint(self, ref_id: int, run_id: str | None) -> str:
        """The record-a-run pointer under a read-only check view; empty
        when the run was recorded."""
        if run_id is not None:
            return ""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else "slug"
        return _DRC_RECORD_HINT.format(slug=slug)

    def _drc_run(
        self,
        ref_id: int,
        design: dict[str, Any],
        *,
        eval_phase: str | None = None,
        eval_phase_claimed: bool = True,
        erc: bool = False,
        record: bool = False,
        reuse: bool = False,
    ) -> _DrcRun | None:
        """Delegates to :func:`precis.pcb.check_run.run` — the check
        surface lives in :mod:`precis.pcb` so the ``pcb_review`` worker
        runs it itself (it may not import this handler). ``record``
        persists the run (only ``op='check'``/``op='review'``;
        gr491591).

        ``reuse`` (gr496242) first asks :func:`precis.pcb.check_run.reuse`
        for the board's last recorded run at this eval's phase and scope —
        served back verbatim when its design-revision stamp still matches,
        so an unchanged board's ``view='check'``/``'drc'`` never pays the
        inline run twice. Only the get views pass it (and ``op='check'``,
        which then returns the still-fresh run instead of recording a
        duplicate); ``args.fresh``, the gerber banner and the worker run
        inline as before."""
        if reuse:
            reused = check_run.reuse(
                self.store,
                ref_id,
                design,
                eval_phase=eval_phase,
                eval_phase_claimed=eval_phase_claimed,
                erc=erc,
            )
            if reused is not None:
                return reused
        return check_run.run(
            self.store,
            ref_id,
            design,
            eval_phase=eval_phase,
            eval_phase_claimed=eval_phase_claimed,
            erc=erc,
            record=record,
        )

    def _validity_findings(self, ref_id: int) -> list[pcb_drc.DrcFinding]:
        """The findings of the geometric-validity rules on the board as
        the store (or the open :meth:`Store.pcb_judged_tx`) holds it now,
        persisting nothing, errors AND warnings (a warning is a shortfall
        against the house or net-class margin; only :meth:`_judged_mutation`
        decides which of them gate): copper clearance (with class rules and net
        voltages), trace width, annular ring, NPTH clearance, via/pad and
        via/via keep-out, board edge, plus courtyard overlap, courtyard vs
        mounting hole, outline containment and edge mating. Not silkscreen, unrouted/
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
            capability = pcb_session.board_capability(
                board["stackup"], pcb_profiles.board_profile(board.get("meta"))
            )
        except ValueError as exc:
            raise BadInput(
                f"pcb: {exc}",
                next="args={'op':'stackup','preset':'4l-two-sided'} — v1 "
                "supports 2- or 4-layer boards",
            ) from exc
        layer_names = [str(layer.get("name")) for layer in board["stackup"]]
        pads, courtyard_local, _footprints = self._drc_geometry(ref_id, layer_names)
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
        graph = self.store.pcb_graph(ref_id)
        ir = self._build_ir(ref_id, graph)
        model = {
            "layers": layer_names,
            "copper": copper,
            "pads": pads,
            "drills": self._drc_drills(ir),
        }
        outline = self._outline_from_features(ref_id)
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
            *pcb_drc.check_edge_mating(
                [r for r in pcb_edge_mate.records(ir) if r.refdes not in guessed],
                outline=outline,
            ),
        ]
        if courtyards:
            findings += pcb_drc.check_courtyard_overlap(
                courtyards,
                bottom_by_refdes=courtyard_bottom,
                clearance_mm=pcb_ir.COURTYARD_CLEARANCE_MM,
                pads=pads,
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
        # gr345859 §F — measures at `strength='invariant'` (the generator-
        # declared fabric rules) gate beside the geometric ones: a violated
        # one is `rule 'invariant:<metric>'`, state not delta — the after-
        # state either honours it or the put refuses (see _judged_mutation).
        findings += pcb_session.invariant_measure_findings(
            graph=graph,
            measures=self.store.pcb_measures_list(ref_id),
            features=self.store.pcb_features_list(ref_id),
            generators_rows=self.store.pcb_generators_for(ref_id),
            pads=pads,
            copper=copper,
            layers=layer_names,
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
            mated_before = (
                {
                    str(i["refdes"])
                    for i in self.store.pcb_graph(ref_id)["instances"]
                    if i.get("edge_mate") is not None
                }
                if ref_id is not None
                else set()
            )
            result = mutate(conn)
            rid = ref_id_of(result) if ref_id_of is not None else ref_id
            if rid is None:
                raise ValueError("_judged_mutation: no ref id after the change")
            after = self._validity_findings(rid)

            # `invariant:<metric>` findings (gr345859 §F) are STATE, not
            # delta: the board either honours every declared invariant
            # after the change or the change refuses — a pre-existing
            # violation is not "standing, not caused by this change", it
            # IS the board being invalid. They never enter _margin_delta.
            def is_invariant(f: pcb_drc.DrcFinding) -> bool:
                return f.rule.startswith("invariant:")

            worse, standing = _margin_delta(
                [
                    (_finding_identity(f), f.margin_mm or 0.0)
                    for f in before
                    if not is_invariant(f)
                ],
                [
                    (_finding_identity(f), f.margin_mm or 0.0, f)
                    for f in after
                    if not is_invariant(f)
                ],
            )
            report = JudgeReport(
                standing=sum(1 for f in standing if f.severity == "error")
            )
            problems: list[str] = []
            for f in [f for f in after if is_invariant(f)]:
                nets = pcb_session.router_nets_of(f)
                if nets:
                    for net in sorted(nets):
                        report.ripped.setdefault(net, f.rule)
                    continue
                problems.append(f"{f.rule}: {f.where}")
            for f, margin, old_margin in worse:
                # A constraint authored on a part already placed off the
                # edge is a request for op='place' to snap it, not an
                # invalid edit: shown, not refused. Moving an edge-mated
                # part off its edge is refused like any other error.
                if f.rule == "edge_mating" and not mated_before & {
                    str(o.get("refdes")) for o in f.objects
                }:
                    report.visible.append(f"{f.rule}: {f.where}")
                    continue
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
        the same check ``view='check'`` answers at ``fab`` (ERC included)
        and leads its response with this:
        a block banner with the count, the per-rule breakdown and the
        first findings when there are errors, one line when it is clean.
        A DRC that could not run is said out loud too; before this the
        export mentioned DRC nowhere, on a board with 116 errors.

        The banner always judges AT ``fab``: an authored ``phase=placed``
        claim on a routed board is a statement about progress, not a
        licence to export quietly — a fab bundle leaving with live
        clearance/connectivity errors behind a "0 errors" banner is the
        exact hole ``view='check'``'s phase axis exists to avoid."""
        design = self.store.pcb_load(ref_id)
        try:
            # erc=True: the banner answers the SAME question view='check'
            # does at phase=fab — geometric AND netlist/ERC findings — so
            # the two surfaces' error counts come from one run shape
            # (gr495288: the banner used to skip ERC and undercount).
            ran = self._drc_run(ref_id, design, eval_phase="fab", erc=True)
        except BadInput as exc:
            return f"⚠️  DRC NOT RUN — {exc}", 0
        if ran is None:
            return (
                "⚠️  DRC NOT RUN — no realized copper and no real pad geometry "
                "to check; this bundle is unverified",
                0,
            )
        run_id, findings, pads_only, _delta, _baseline, _reused = ran
        # Judged AT fab — severities tier at fab too, so a netless pin a
        # placed board merely warns about is the error it is at tape-out.
        errors = [f for f in findings if pcb_drc.display_severity(f, "fab") == "error"]
        scope = " (pads-only — no routed copper yet)" if pads_only else ""
        if not errors:
            # A board that could not be fully checked never reads clean:
            # "0 errors, N warn(s) ✓" over an uncached-footprint board
            # reports the part nobody looked at as verified copper.
            unchecked = sorted(
                {
                    str(o.get("refdes"))
                    for f in findings
                    if f.rule == "undeclared_pad"
                    for o in f.objects
                    if o.get("not_checked")
                }
            )
            if unchecked:
                shown = ", ".join(unchecked[:8]) + (" …" if len(unchecked) > 8 else "")
                return (
                    f"⚠️  DRC: not fully checked — {len(unchecked)} "
                    f"footprint(s) uncached ({shown}) — {_drc_run_label(run_id)}"
                    f"{scope}",
                    0,
                )
            n_warn = sum(
                1 for f in findings if pcb_drc.display_severity(f, "fab") == "warn"
            )
            return (
                f"DRC: 0 errors, {n_warn} warn(s) — {_drc_run_label(run_id)}{scope} ✓",
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
            f"██  {_drc_run_label(run_id)} · "
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
        lines.append(
            f"Full list: get(kind='pcb', id='{slug}', view='check', "
            "args={'phase': 'fab'})"
        )
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
            # `_build_ir` lays the resolved snapshot over the graph, as the
            # pcb_route job does: stored sketch keys are endpoint pins of
            # the effective netlist.
            ir = self._build_ir(ref_id, graph)
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
            return self._render_fab_svg(
                ref_id, slug, flip=bool((args or {}).get("flip"))
            )
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
            "drills": self._drc_drills(
                self._build_ir(ref_id, self.store.pcb_graph(ref_id))
            ),
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
                    options=sorted(gens),
                    next="get(kind='pcb', id='slug', view='capability', "
                    "args={'name':'<generator name>'})",
                )
            name = next(iter(gens))
        elif name not in gens:
            raise BadInput(
                f"pcb: no generator named {name!r} — known: {sorted(gens)}",
                options=sorted(gens),
            )
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

    def _fab_svg_model(
        self, ref_id: int, slug: str
    ) -> tuple[dict[str, Any], list[str]] | None:
        """The board model :meth:`_render_fab_svg` exports to gerbers, plus
        the furniture warnings — ONE assembly shared with
        :meth:`selection_objects`, so the click catalogue is built from the
        very model the picture is. ``None`` when the design has no board."""
        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is None:
            return None
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
        board_profile_name = pcb_profiles.board_profile(board.get("meta"))
        capability = self._stackup_capability(board["stackup"], board_profile_name)
        pads, silk_draws, furniture_warnings, _silk_census, copper = (
            self._board_furniture(
                ir,
                pads,
                vias=vias,
                instance_sides=instance_sides,
                polarized=self._polarized_refdes(design),
                label_spots=pcb_session.label_spots(design),
                copper=copper,
                outline=outline,
                layer_names=layer_names,
                slug=slug,
                clearance_mm=self._furniture_clearance_mm(board["stackup"]),
                capability=capability,
                date=_export_date(),
                profile=board_profile_name,
            )
        )
        model: dict[str, Any] = {
            "layers": layer_names,
            "outline": outline,
            "copper": copper,
            "pads": pads,
            # Two hole populations, both required or the preview lies
            # about the artefact:
            # - mounting/footprint holes off the IR — without them this
            #   preview showed a solder-nut ring with no hole through it
            #   while the real bundle (`_render_gerber`, same IR source)
            #   drills one (measured on the round-3 nano render).
            # - component pad drills off the pads themselves — the IR pad
            #   source carries `drill`/`slot` ON each pad but emits no
            #   drill list, so without this half the PTH file held vias
            #   alone and every THT part (a switch, every pin header)
            #   rendered as a solid pad with no hole — the defect that
            #   surfaced on taz15-blank (2026-10-13). `_fab_model` gets
            #   the same holes from `board_pads`' own drill return; the
            #   two stay consistent because both read `pad["drill"]`.
            # Via drills ride in from `copper` (excellon_files' own via
            # pass), so this key carries no via rows.
            "drills": [
                *self._pad_drills(pads),
                *(
                    {"x": h.x, "y": h.y, "dia_mm": h.drill_mm, "plated": h.plated}
                    for h in ir.mounting_holes
                ),
            ],
            "silkscreen": silk_draws,
            "soldermask_expansion_mm": pcb_silk.soldermask_expansion_mm(capability),
            "mask_open_regions": self._mask_open_regions(ref_id),
        }
        return model, furniture_warnings

    @staticmethod
    def _selection_rows(manifest: dict[str, Any]) -> dict[str, Any]:
        rows = manifest["rows"]
        return {
            "instances": rows["instances"],
            "pins": rows["pins"],
            "nets": rows["nets"],
            "components": rows["components"],
            "identities": manifest.get("instances"),
        }

    def selection_objects(
        self, ref_id: int, slug: str, manifest: dict[str, Any]
    ) -> list[pcb_selectables.SelectObject]:
        """The selectable-object catalogue of the board the viewer shows,
        built from :meth:`_fab_svg_model` — the same model the SVG overlay
        is. ``manifest`` carries the raw relational rows (``rows``) and the
        per-instance identities (``instances``); see
        :mod:`precis.pcb.selectables`. Raises ``ValueError`` when there is
        no board to build it from."""
        assembled = self._fab_svg_model(ref_id, slug)
        if assembled is None:
            raise ValueError("PCB board is not renderable")
        return pcb_selectables.build(assembled[0], **self._selection_rows(manifest))

    def _render_fab_svg(
        self,
        ref_id: int,
        slug: str,
        *,
        selection_manifest: dict[str, Any] | None = None,
        flip: bool = False,
    ) -> Response:
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
        assembled = self._fab_svg_model(ref_id, slug)
        if assembled is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        model, furniture_warnings = assembled
        pads = model["pads"]
        if selection_manifest is not None and "rows" in selection_manifest:
            selection_manifest = {
                **selection_manifest,
                "objects": pcb_selectables.build(
                    model, **self._selection_rows(selection_manifest)
                ),
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
            body = gerber_view.render_fab_svg(
                files,
                title=slug,
                selection_manifest=selection_manifest,
                # `feature:outline` on the Edge_Cuts group — only when the
                # board's outline is a pcb_features row (`model["outline"]`
                # comes from `_outline_from_features` and nowhere else).
                outline_handle=(
                    argue.feature_handle("outline") if model.get("outline") else None
                ),
                flip=flip,
            )
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
            raise BadInput(
                f"pcb: cannot render this fab set — {exc}",
                next=[
                    "get(kind='pcb', id='slug', view='svg') renders from the "
                    "IR without the gerber round-trip",
                    "file a gripe — our own export produced gerbers the "
                    "viewer cannot parse",
                ],
            ) from exc

    # ── link ─────────────────────────────────────────────────────────
    def link(
        self,
        *,
        id: str | int | None = None,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add or remove a link from this design to another ref
        (gr485159 — a board could not ``serve`` the quest that owns it;
        the quest-side ``served-by`` writes the edge backwards, so the
        verb belongs here). The shared :func:`apply_link_ops` door —
        the same one every other slug kind uses — carries the relation
        vocabulary, the constraint table (``serves`` is acyclic) and
        lazy target minting; ``rel=None`` adds ``related-to``."""
        if id is None:
            raise BadInput(
                "pcb link needs id= (the design slug)",
                next="link(kind='pcb', id='slug', target='<kind:id>')",
            )
        target = require_link_target("pcb", target)
        validate_link_mode(mode)
        ref = resolve_live_slug_ref(self.store, kind="pcb", id=str(id).strip())
        n_added, n_removed = apply_link_ops(
            self.store,
            ref.id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.spec.kind,
                ref_label=str(ref.slug),
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── delete ───────────────────────────────────────────────────────
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "delete(kind='pcb') requires id= (the design slug)",
                next="delete(kind='pcb', id='slug')",
            )
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
            card_kinds=("card_combined", "card_description"),
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

    @staticmethod
    def _lock_level(i: dict[str, Any]) -> str:
        """``locked`` (``fixed`` set) / ``leash N mm`` / ``free``."""
        if i.get("fixed"):
            return "locked"
        if i.get("max_travel_mm") is not None:
            return f"leash {i['max_travel_mm']:g} mm"
        return "free"

    @staticmethod
    def _lock_flags(i: dict[str, Any]) -> str:
        flags = []
        if i.get("fixed_side"):
            flags.append("side-locked")
        if i.get("group_fixed"):
            flags.append("group-fixed")
        if i.get("room_id") is not None:
            flags.append(f"room {i['room_id']}")
        return ",".join(flags) or "—"

    def _render_placement(self, ref_id: int) -> Response:
        """``view='placement'`` — per part: pose, side, lock level and the
        lock flags (``side-locked`` / ``group-fixed`` / ``room N``)."""
        design = self.store.pcb_load(ref_id)
        rows = [
            {
                "refdes": i["refdes"],
                "pose": self._pose(i),
                "side": i["layer"],
                "lock": self._lock_level(i),
                "flags": self._lock_flags(i),
            }
            for i in design["instances"]
        ]
        return Response(
            body=f"# placement — {len(rows)} part(s)\n"
            + render_agent_table(
                rows, schema=["refdes", "pose", "side", "lock", "flags"]
            )
        )

    def _toc(self, design: dict[str, Any]) -> str:
        parts = []
        board = design.get("board")
        if board is not None:
            line = (
                f"## board: {board['name']} — {self._stackup_summary(board['stackup'])}"
            )
            profile = pcb_profiles.board_profile(board.get("meta"))
            if profile != pcb_profiles.DEFAULT_PROFILE:
                line += f"\nprofile: {profile}"
            parts.append(line)
        irows = [
            {
                "refdes": i["refdes"],
                "part": i["label"],
                "lcsc": i["part_lcsc"] or "—",
                "layer": i["layer"],
                "pose": self._pose(i),
                # A locked source component is "do not move this" — the
                # fact must be readable where the parts are listed
                # (pcb-datasheet-review.md slice 1).
                "fixed": i.get("fixed") or "—",
                "roles": ",".join(i["roles"]) or "—",
            }
            for i in design["instances"]
        ]
        schema = ["refdes", "part", "lcsc", "layer", "pose", "fixed", "roles"]
        if any(
            i.get("max_travel_mm") is not None
            or i.get("fixed_side")
            or i.get("group_fixed")
            or i.get("room_id") is not None
            for i in design["instances"]
        ):
            # Only a board that uses the 0219 locks pays for the columns.
            for row, i in zip(irows, design["instances"], strict=True):
                row["lock"] = self._lock_level(i)
                row["flags"] = self._lock_flags(i)
            schema += ["lock", "flags"]
        parts.append("## parts\n" + render_agent_table(irows, schema=schema))
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
        if n_nets:
            summary = pcb_route_state.summary_line(
                n_nets, design.get("route_status") or {}
            )
            parts.append(f"## route status: {summary}")
        return "\n".join(parts)

    def _stackup_summary(self, stackup: Any) -> str:
        """``4 layers: F.Cu/In1.Cu(GND)/In2.Cu/B.Cu`` — the plane_net (if
        any) parenthesised after its layer name — then, when the stackup
        has them (pcb-stackup-regions slice 3), the layers each ``fill``
        net pours and where power regions are derived, e.g. ``, GND fill
        on F.Cu/In2.Cu/B.Cu, power regions on In2.Cu``."""
        layers = list(stackup or [])
        names = [
            f"{layer.get('name')}({layer['plane_net']})"
            if layer.get("plane_net")
            else str(layer.get("name"))
            for layer in layers
        ]
        out = f"{len(layers)} layers: {'/'.join(names)}"
        fills: dict[str, list[str]] = {}
        for layer in layers:
            if layer.get("fill"):
                fills.setdefault(str(layer["fill"]), []).append(str(layer.get("name")))
        for net, on in fills.items():
            out += f", {net} fill on {'/'.join(on)}"
        regions = [str(e.get("name")) for e in layers if e.get("power_regions")]
        if regions:
            out += f", power regions on {'/'.join(regions)}"
        return out

    def _pose(self, i: dict[str, Any]) -> str:
        if i["x"] is None or i["y"] is None:
            return "unplaced"
        pose = f"@{i['x']:g},{i['y']:g}"
        if i["rot"]:
            pose += f" r{i['rot']:g}"
        if i["fixed"]:
            pose += f" 📌{i['fixed']}"
        return pose

    @staticmethod
    def _preview_pins(args: dict[str, Any] | None) -> list[dict[str, Any]]:
        hint = (
            "get(kind='pcb', id='<board>#<REFDES>', view='pinout-preview', "
            "args={'pins':[{'name':'CLK','pad':'2'}]})"
        )
        if not isinstance(args, dict) or set(args) != {"pins"}:
            raise BadInput("preview args accepts only required pins", next=hint)
        pins = args["pins"]
        if not isinstance(pins, list) or len(pins) > 32:
            raise BadInput(
                "preview pins must be a list of at most 32 entries", next=hint
            )
        result = []
        for index, pin in enumerate(pins, 1):
            if not isinstance(pin, dict) or set(pin) != {"name", "pad"}:
                raise BadInput(
                    f"preview entry {index} requires only name and pad; "
                    "use explicit null for unknown pad",
                    next=hint,
                )
            name, pad = pin["name"], pin["pad"]
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
                raise BadInput(
                    f"preview entry {index}: name must be 1–64 characters", next=hint
                )
            if pad is not None and (
                not isinstance(pad, str) or not pad.strip() or len(pad) > 32
            ):
                raise BadInput(
                    f"preview entry {index}: pad must be an exact 1–32 character "
                    "string or null; no numbering inference",
                    next=hint,
                )
            result.append({"name": name.strip(), "pad": pad})
        return result

    def _render_pinout(
        self, ref: Any, refdes: str, *, proposals: list[dict[str, Any]] | None = None
    ) -> Response:
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
        preview = proposals is not None
        view = "pinout-preview" if preview else "pinout"
        head = (
            f"# {ref.slug}#{refdes} — {view}\n"
            f"footprint: {key or 'unavailable'}; {source}\n"
            f"instance: x_mm={instance.get('x')} y_mm={instance.get('y')} "
            f"side={'bottom' if padplace.is_bottom_instance(instance) else 'top'} "
            f"rotation_deg={instance.get('rot') or 0}\n"
            "orientation: footprint-local +X right / +Y up; board top-view, "
            "CW-positive rotation; bottom mirrors local X before rotate/translate.\n"
            "Stored geometry/mapping, not vendor or mating-orientation verification.\n"
        )
        if preview:
            head += (
                "Unsaved read-only proposal; proposed labels are not canonical pins "
                "or net assignments. Mating orientation, vendor numbering and "
                "proposed nets/NC semantics: unknown.\n"
            )
            if not proposals:
                head += "No proposed assignments (pins=[]).\n"
        missing_hint = None
        if not footprint or not footprint.get("pads"):
            hint = (
                f"put(kind='pcb', id={ref.slug!r}, "
                f"args={{'op':'footprint', 'part':{key!r}}})"
                if lcsc
                else "get(kind='skill', id='precis-pcb-help') — author footprints[] "
                f"for {key or 'this instance'}"
            )
            if not preview:
                return Response(
                    body=head + f"geometry: unavailable (no stored pads)\nnext: {hint}"
                )
            missing_hint = (
                f"get(kind='pcb', id={ref.slug!r}, view='footprints') — inspect "
                "stored cache; no automatic fetch"
                if lcsc
                else hint
            )
        footprint = footprint or {}
        if preview and len(footprint.get("pads") or []) > 64:
            raise BadInput(
                "preview exceeds 64 physical pad rows; no rows truncated",
                next=f"get(kind='pcb', id='{ref.slug}#{refdes}', view='pinout')",
            )
        neighbors = self.store.pcb_instance_neighbors(ref.id, refdes)
        stackup = (design.get("board") or {}).get("stackup") or []
        layers = [str(layer["name"]) for layer in stackup]
        stored_pins = (neighbors or {}).get("pins", [])
        result = (
            eyes.pinout_preview(instance, footprint, stored_pins, layers, proposals)
            if proposals is not None
            else eyes.pinout(instance, footprint, stored_pins, layers)
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
            if preview:
                for field in ("proposed_names", "proposal_indices", "proposal_notes"):
                    row[field] = json.dumps(
                        row[field], ensure_ascii=False, sort_keys=True
                    )
            rows.append(row)
        body = head + f"geometry: stored; physical pads: {len(rows)}\n"
        if missing_hint:
            body = (
                head + f"geometry: unavailable (no stored pads)\nnext: {missing_hint}\n"
            )
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
            ]
            + (
                [
                    "proposed_names",
                    "proposal_indices",
                    "proposal_provenance",
                    "proposal_state",
                    "proposal_notes",
                ]
                if preview
                else []
            ),
        )
        if result["holes"]:
            body += "\nfootprint holes (non-plated):\n" + render_agent_table(
                [
                    {
                        k: "unavailable" if v is None else f"{v:.4f}"
                        for k, v in hole.items()
                        if k != "hole_index"
                    }
                    | {"hole_index": hole["hole_index"]}
                    for hole in result["holes"]
                ],
                schema=[
                    "hole_index",
                    "local_x_mm",
                    "local_y_mm",
                    "board_x_mm",
                    "board_y_mm",
                    "dia_mm",
                ],
            )
        if result["unmatched"]:
            body += "\nunmatched declared pins:\n" + render_agent_table(
                result["unmatched"]
            )
        if preview:
            body += "\nunsaved proposed entries:\n" + render_agent_table(
                [
                    {**entry, "notes": json.dumps(entry["notes"], ensure_ascii=False)}
                    for entry in result["proposals"]
                ],
                schema=["entry_index", "name", "pad", "provenance", "state", "notes"],
            )
            if len(body) > 16_384:
                raise BadInput(
                    "preview exceeds 16384 text characters; no output truncated",
                    next="reduce proposals or use view='pinout' for stored evidence",
                )
        return Response(body=body)

    def _render_instance(self, ref_id: int, refdes: str) -> Response:
        nb = self.store.pcb_instance_neighbors(ref_id, refdes)
        if nb is None:
            raise NotFound(
                f"pcb instance {refdes!r} not found in this design",
                next="get(kind='pcb', id='slug') lists this design's refdes",
            )
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
        body = f"# {refdes} — {len(nb['pins'])} pin(s)\n"
        if nb.get("part_lcsc"):
            body += (
                f"catalog: {nb['part_lcsc']}"
                + (
                    f" — {nb['part_description']}"
                    if nb.get("part_description")
                    else " (no catalogue row yet)"
                )
                + "\n"
            )
        spec = pcb_catalog.parse_passive_description(nb.get("part_description"))
        if spec is not None:
            # Slice 3's derived fact, shown on #REFDES per in-scope 4:
            # the rating/dielectric the catalog description carries.
            if spec["kind"] == "capacitor":
                value = f"{spec['value_f']:g} F" if spec.get("value_f") else "?"
                rating = (
                    f"{spec['rated_v']:g} V"
                    if spec.get("rated_v") is not None
                    else "unrated"
                )
                derived = (
                    f"capacitor {value}, {rating}, "
                    f"dielectric {spec.get('dielectric') or 'unknown'}"
                )
            else:
                rating = (
                    f"{spec['rated_w']:g} W"
                    if spec.get("rated_w") is not None
                    else "unrated"
                )
                derived = f"resistor {spec['ohm']:g} Ω, {rating}"
            body += f"derived: {derived} (from catalog description)\n"
        return Response(
            body=body
            + render_agent_table(
                rows, schema=["pin", "pad", "tags", "net", "neighbors"]
            )
        )

    def _render_net(self, ref_id: int, name: str) -> Response:
        net = self.store.pcb_net_members(ref_id, name)
        if net is None:
            raise NotFound(
                f"pcb net {name!r} not found in this design",
                next="get(kind='pcb', id='slug') lists this design's net names",
            )
        rows = [
            {"refdes": m["refdes"], "pin": m["pin"], "tags": ",".join(m["tags"]) or "—"}
            for m in net["members"]
        ]
        cls = net["net_class"] or "—"
        return Response(
            body=f"# net {name} (class {cls}) — {len(net['members'])} pin(s)\n"
            + render_agent_table(rows, schema=["refdes", "pin", "tags"])
        )
