"""Geometric DRC on realized copper (L5) — pcb-guided-place-route Slice 8.

**The DRC split** (see ``ir.py``'s module docstring for its half): two
engines at the IR level boundary. ``ir.py`` owns **graph feasibility**
(L0-L4, no geometry, no shapely), inside the optimizer's constraint set.
This module owns **geometric DRC** (L5, on realized copper) — class-rule
clearance, trace width, annular ring, via-to-pad keep-out, via-to-via
keep-out, courtyard overlap, board-edge clearance, silk rendering/
printability (:func:`check_silk_missing`/:func:`check_silk_printability`,
off :mod:`precis.pcb.silk`'s own ``SilkPlacement`` census) — the *final*
check, not the main event. Both emit
``pcb_drc_findings`` rows and share ``view='drc'`` (``handlers/pcb.py``);
``eyes.drc_lite`` is superseded and retired.

Two NETLIST-level rules report on the same surface since Slice 2 of
docs/backlog/pcb-lazy-netlist-and-checks.md (§1g: one check surface, not
two — an ERC finding is the same row with ``margin_mm`` empty):
:func:`check_unconnected_pins`, phase-aware through
:func:`derive_phase`/``PHASES``, and :func:`check_undeclared_pads`, the
declare-all completion's safety net.

**Input model**: the same copper-model dict :mod:`precis.pcb.realize`
produces (:func:`precis.pcb.realize.to_gerber_model`) that
:mod:`precis.pcb.gerber` consumes — ``{"layers": [...], "outline": [...],
"copper": [{"ctype": "track"|"via"|"pour", ...}]}`` (exact per-``ctype``
shape: that module's docstring). Never a second geometry representation
for the same board.

``realize.py`` emits ``ctype='via'`` copper wherever a track's routed
layer differs from its pads' layer, always carrying ``span``/``layers``,
never a scalar ``layer`` (:class:`~precis.pcb.realize.RealizedVia`'s
docstring: why that distinction is load-bearing here) — real input for
:func:`check_annular_ring` and the via halves of
:func:`check_clearance`/:func:`check_npth_clearance`.

**Two-tier margin, not bare pass/fail.** Every rule reads both tiers off
:mod:`precis.pcb.capabilities`: ``jlc_min`` (fab's published,
unmanufacturable-below floor) and ``house_default`` (our margin above
it). A finding fires **error** below ``jlc_min`` (unmanufacturable) or
**warn** clearing ``jlc_min`` but eating ``house_default`` margin — either
way ``margin_mm``/``detail`` name the exact numbers, never a bare boolean.
A ``None`` capability field (JLC publishes none for that process/field)
means the rule doesn't apply — silently skipped, never a violated zero.

**Class-rule clearance reads both the fab capability row and a
``pcb_net_classes`` override, through ONE resolver.** ``trace_spacing_mm``
is JLC's name for copper-to-copper clearance, keyed by process
(2-layer/4-layer/aluminum — the "class"), same precedent as
:func:`precis.pcb.escape.compute_gaps`; that stays the absolute floor. A
net whose class carries an authored ``pcb_net_classes.rules.clearance_mm``
(or whose current annotation implies a wider IPC-2221 trace, hence more
room) resolves through :func:`precis.pcb.rules.resolve_net_rules` — the
SAME resolver :mod:`precis.pcb.realize` uses for track width and
:mod:`precis.pcb.cost` uses for ``thermal_rise``. :func:`check_clearance`'s
caller supplies the resolved :class:`~precis.pcb.rules.NetRules` per net
name via ``net_rules=``; ``None`` (default) keeps capability-only
behaviour.

**Board-edge clearance is two fields**: ``board_edge_clearance_routed_mm``
vs. ``..._vcut_mm``. Unknown panel type → :func:`check_board_edge_clearance`
uses the V-cut figure (the conservative one, per ``capabilities.py``).

**The O(n^2) reference oracle — the highest-value code in this module.**
:func:`clearance_violations_naive` computes every same-layer,
different-net track/via pair's exact copper-to-copper gap using ONLY
closed-form circle/segment math (no shapely, no spatial index, no shared
code with the accelerated path below the primitive-flattening step) and is
asserted equal to :func:`check_clearance`'s STRtree-accelerated engine over
many randomized layouts (``tests/test_pcb_drc.py``). This build has
already shipped four silent-but-fatal bugs that crashed nothing and failed
no type check (an inverted hardening penalty, a schedule-mismatched
acceptance test, a temperature decayed below eligibility, an estimator
that was provably always zero) — a spatial-index bug that silently misses
a neighbour is the same family, and produces a clean DRC pass on a shorted
board. Pour polygons — and, since 2026-08-29, PAD polygons
(:func:`clearance_pairs_indexed` now folds ``model["pads"]`` into the same
per-layer check every other copper item gets; see its own docstring) — are
checked by the accelerated engine only, not cross-validated by the
dependency-free oracle, which is deliberately restricted to the
circle/capsule primitives that make closed-form math possible (a
rect/obround pad is neither). A stated, not silent, gap in oracle
coverage — and note it is the SAME gap pours already had: the oracle
agreeing with the accelerated engine on tracks/vias was never evidence
that either one was checking pads or pours at all.

**A reference oracle only checks the inputs it's fed**: ``clearance_
violations_naive`` was correct against every synthetic fixture and still
shipped a real bug (its own docstring: compared only each group's FIRST
primitive when deciding two items shared a layer, so a multi-layer via
with a partially-overlapping span could be silently skipped). Agreement
with a synthetic-fixture oracle proves agreement on the shapes tested,
not correctness against production geometry.

**Findings have an identity, not just a present** (docs/backlog/
finding-stable-identity.md). :func:`finding_key` hashes ``(rule,
canonicalised participants, layer)`` — never coordinates, the margin or
list position — so the same pair at a different margin is the same
finding across runs; :func:`delta_against` pairs one run's findings with
the previous run's persisted rows by that key (rank for rank by margin
when several share one) into new / still / gone. The handler's
``view='drc'`` renders that delta; the group-move judge in
``handlers/pcb.py`` keys on the same :func:`finding_identity`, so the two
agree on what "the same finding" means.

**STRtree is used only for the clearance rule**: copper-to-copper
clearance is the one genuinely O(n^2) rule (every different-net pair per
layer is a candidate); trace width, annular ring, NPTH clearance and
board-edge clearance are O(n) or O(n·holes) per-item, no spatial index
needed. Courtyard overlap is also pairwise and uses the same STRtree
machinery, at instance rather than copper-item scale.
"""

from __future__ import annotations

import collections
import dataclasses
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# shapely ships no py.typed marker in this environment (mypy wants the
# separate `types-shapely` stub package, a pyproject/dev-dep decision
# outside this module's remit) -- silence the import-untyped noise here
# rather than let it mask real errors elsewhere in the file (tiling.py
# sets the same precedent).
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from precis.finding_id import finding_hash
from precis.pcb import (
    CAP_DIELECTRIC_RANK,
    PASSIVE_CAP_VOLTAGE_FACTOR,
    PASSIVE_CAP_VOLTAGE_FACTOR_DEFAULT,
    PASSIVE_MARGIN_POLICY,
    PASSIVE_MARGIN_REVISION,
    PASSIVE_RESISTOR_POWER_FRACTION,
    PIN_CURRENT_ROLES,
    PIN_ETYPES,
)
from precis.pcb.capabilities import CapabilityRow, conductor_spacing_mm
from precis.pcb.catalog import parse_passive_description
from precis.pcb.geom import _orient, dist_point_to_segment
from precis.pcb.geom import dist as _dist
from precis.pcb.ir import COURTYARD_MITRE_LIMIT
from precis.pcb.rules import NetRules
from precis.pcb.silk import SilkPlacement

Coord = tuple[float, float]

#: Tolerance for two-tier comparisons and shapely distance queries — well
#: below any real manufacturing figure (mm), just float noise absorption.
_EPS = 1e-9

#: Tolerance for :func:`_two_tier` — a figure within 0.1 µm of a floor is
#: AT the floor. Geometry imported from EasyEDA arrives in mil (0.0001-mil
#: source quantum, rounded to the nanometre on the way to mm), so a ring
#: drawn at exactly JLC's 0.150 mm reads 0.1499997 mm; against ``_EPS``
#: that was 48 "0.000 mm short" errors on one real board (2026-10-02).
#: Four orders below any fab tolerance, so nothing real hides in it.
_TIER_EPS = 1e-4

#: A generic courtyard fallback radius for an instance with no real
#: footprint courtyard data supplied — the same honest fallback
#: :class:`precis.pcb.realize.RealizeConfig`'s ``default_obstacle_radius_mm``
#: uses, for the same reason (no per-footprint courtyard wired in yet).
DEFAULT_COURTYARD_RADIUS_MM = 1.0

#: How finely an arc segment is flattened for geometry construction (both
#: the STRtree-accelerated path and the reference oracle use this SAME
#: tessellation, via :func:`_arc_points` — so a disagreement between the
#: two engines can only be about the distance QUERY, never about differing
#: input geometry).
_ARC_MAX_SEG_DEG = 15.0

#: shapely's ``buffer()`` approximates a round cap/join as a many-sided
#: polygon; the default resolution (8 segments/quarter-circle) leaves a
#: sub-micron systematic gap versus the exact closed-form capsule the
#: reference oracle computes. Raised well past where it could ever be
#: mistaken for a real spatial-index miss (see the module docstring's
#: oracle section) rather than loosening the property test's tolerance to
#: paper over it.
_BUFFER_QUAD_SEGS = 64


# ── findings ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DrcFinding:
    """One geometric DRC finding — the row shape both a caller renders as a
    TOON digest and persists to ``pcb_drc_findings`` (board_id/run_id are
    added by the caller, not this module, which knows nothing about a
    board's identity). ``margin_mm`` is always signed NEGATIVE when a
    finding fires — how far below the tier's own threshold the measured
    value sits (module docstring's two-tier rule) — so "how bad" is a
    number, never just a severity string."""

    rule: str
    severity: str  # "error" | "warn"
    where: str
    detail: str
    objects: tuple[dict[str, Any], ...] = ()
    margin_mm: float | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "objects": list(self.objects),
            "detail": self.detail,
            "finding_key": finding_key(self),
            "margin_mm": self.margin_mm,
            "location": self.where,
        }


# ── finding identity ──────────────────────────────────────────────────────
#
# docs/backlog/finding-stable-identity.md: a finding's identity is
# ``(rule, participants)``, never a row id, a list position or a
# coordinate. The participants are the durable things the finding is ABOUT
# — the two nets of a clearance hit, the pad and the via of a keep-out,
# the two parts of a courtyard overlap — canonicalised (sorted, so an
# unordered pair keys the same either way) and independent of the measured
# value. ``R0C2 <-> RESV at -0.114mm`` and the same pair at ``-0.090mm``
# after a nudge are ONE finding with a changed margin. Coordinates and the
# margin are payload: they are how you find the finding on the board and
# how bad it is, not what it is. Position would be the tempting key and is
# wrong — every participant here moves; keying on where it sat would report
# resolved-and-recreated on every placement nudge, the exact noise this
# removes.


def _object_identity(o: dict[str, Any], prefix: str = "") -> str:
    """Coordinate-free identity of one object of a finding: a pad is
    ``refdes/pin``, authored fixed copper its ``fixed_id``. An object with
    neither is a courtyard finding's ``a``/``b`` pair (``part:<refdes>``, or
    ``hole:<label>``); a silk draw is ``role:refdes:side``; a bare part is
    ``part:<refdes>``; anything else falls back to ``ctype:net:layer``.
    ``prefix`` reads the ``a_``/``b_``-style halves of a paired object."""
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
    if refdes and prefix == "" and o.get("kind") is not None:
        # silk_missing / silk_printability: several findings per component,
        # one per silk element (pin1 marker, courtyard, refdes label), and a
        # stroke-width vs. cap-height printability finding on the same label.
        # The refdes label keeps the bare ``part:<refdes>`` it always had
        # (existing keys stay put); any other element (with its side) or the
        # stroke-width measure is appended so the findings stop sharing a key.
        base = f"part:{refdes}"
        kind = str(o["kind"])
        if kind != "refdes":
            base += f"#{kind}:{o.get('side')}"
        if "stroke_width_mm" in o:
            base += "#stroke"
        return base
    if refdes:
        # outline_containment of a whole part (courtyard): no pin.
        return f"part:{refdes}"
    return (
        f"{o.get(prefix + 'ctype')}:{o.get(prefix + 'net')}:{o.get(prefix + 'layer')}"
    )


def finding_identity(f: DrcFinding) -> tuple[str, tuple[str, ...], str | None]:
    """``(rule, sorted participant identities, layer)`` — no coordinates,
    so a finding between two members of a rigidly moved group keeps its
    key, and the same pair at a different margin is the same finding.

    Paired-object rules whose single ``objects`` entry carries both halves
    under prefixes (``via_pad_keepout``: ``via_*``/``pad_*``;
    ``via_via_keepout``: ``a_*``/``b_*``) are split into their two
    participants here; every other rule's participants are its ``objects``
    one by one through :func:`_object_identity`."""
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
    if f.rule == "via_via_keepout" and f.objects:
        o = f.objects[0]
        pair = (f"via:{o.get('a_net')}", f"via:{o.get('b_net')}")
        return (f.rule, tuple(sorted(pair)), None)
    ids = tuple(sorted(_object_identity(o) for o in f.objects))
    layer = f.where.rsplit(" on ", 1)[-1] if " on " in f.where else None
    return (f.rule, ids, layer)


def finding_key(f: DrcFinding) -> str:
    """The persisted form of :func:`finding_identity`: 12 hex chars of a
    SHA-1 over ``rule|layer|participant+participant``. Deterministic and
    recomputable from the finding alone — no counter, sequence, run id or
    list position — so two runs of the same check over an unchanged board
    produce identical key sets, and ``pcb_drc_findings.finding_key`` can
    be joined across runs. Several findings may legitimately share a key
    (many clearance hits between the same two nets on one layer):
    :func:`delta_against` pairs those rank for rank by margin rather than
    pretending the key is unique."""
    rule, ids, layer = finding_identity(f)
    return finding_hash(rule, ids, layer)


#: A standing finding whose margin fell by more than this is reported as
#: worse, not merely still — float noise on a re-measured, unchanged board
#: must never read as a regression.
WORSE_MARGIN_EPS_MM = 1e-4


@dataclass(frozen=True, slots=True)
class FindingDelta:
    """One run against the previous run on the same board, by
    :func:`finding_key`. ``still`` carries ``(finding, previous margin)``
    so "same finding, now -0.114 instead of -0.090" is reportable (the
    spec's acceptance: a move that changes a margin without changing
    participants is ``still``, never ``gone`` + ``new``). ``gone`` holds
    the previous run's rows (``rule``/``location``/``margin_mm``/
    ``finding_key``) that have no counterpart now. ``previous_run_id`` is
    ``None`` when there was nothing keyed to compare against — the first
    run after 0191, or a previous run written before it — and then every
    finding sits in ``still`` with no previous margin, deliberately NOT in
    ``new``: "we cannot tell" must not be rendered as "all new"."""

    previous_run_id: str | None
    new: list[DrcFinding] = dataclasses.field(default_factory=list)
    still: list[tuple[DrcFinding, float | None]] = dataclasses.field(
        default_factory=list
    )
    gone: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def worse(self) -> list[tuple[DrcFinding, float]]:
        """The ``still`` findings whose margin fell by more than
        :data:`WORSE_MARGIN_EPS_MM`, with the previous margin."""
        return [
            (f, prev)
            for f, prev in self.still
            if prev is not None
            and f.margin_mm is not None
            and f.margin_mm < prev - WORSE_MARGIN_EPS_MM
        ]

    def label(self, f: DrcFinding) -> str:
        """The per-row delta cell: ``new`` / ``still`` / ``still, worse
        (was -0.090)`` / ``still, better (was -0.114)``; empty when there
        was no previous run to compare against."""
        if self.previous_run_id is None:
            return ""
        if any(f is g for g in self.new):
            return "new"
        for g, prev in self.still:
            if f is g:
                if prev is None or f.margin_mm is None:
                    return "still"
                if f.margin_mm < prev - WORSE_MARGIN_EPS_MM:
                    return f"still, worse (was {prev:+.3f})"
                if f.margin_mm > prev + WORSE_MARGIN_EPS_MM:
                    return f"still, better (was {prev:+.3f})"
                return "still"
        return ""


def _margin_rank(m: float | None) -> float:
    return math.inf if m is None else m


def delta_against(
    previous_run_id: str | None,
    previous: Iterable[dict[str, Any]],
    findings: Sequence[DrcFinding],
) -> FindingDelta:
    """Pair ``findings`` with the ``previous`` run's persisted rows by
    :func:`finding_key`, most negative margin first within a key, so one
    new hit among old ones between the same two nets is the one reported
    as new. Extra current findings under a key are ``new``, extra previous
    rows are ``gone``, matched ranks are ``still`` with the old margin.

    A previous run with ANY keyless row (written before 0191) is treated
    as absent: half a comparison would render some findings as new and
    others as still on no evidence. A clean previous run (a run id, no
    rows) is a real comparison — everything now is new."""
    prev_rows = list(previous)
    if previous_run_id is None or any(not r.get("finding_key") for r in prev_rows):
        return FindingDelta(None, still=[(f, None) for f in findings])
    old: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in prev_rows:
        old[str(r["finding_key"])].append(r)
    for rows in old.values():
        rows.sort(key=lambda r: _margin_rank(r.get("margin_mm")))
    now: dict[str, list[DrcFinding]] = collections.defaultdict(list)
    for f in findings:
        now[finding_key(f)].append(f)
    delta = FindingDelta(previous_run_id)
    for key, items in now.items():
        items.sort(key=lambda f: _margin_rank(f.margin_mm))
        was = old.pop(key, [])
        for i, f in enumerate(items):
            if i < len(was):
                m = was[i].get("margin_mm")
                delta.still.append((f, None if m is None else float(m)))
            else:
                delta.new.append(f)
        delta.gone.extend(was[len(items) :])
    for rows in old.values():
        delta.gone.extend(rows)
    # Report in the caller's finding order, not grouped by key.
    order = {id(f): i for i, f in enumerate(findings)}
    delta.new.sort(key=lambda f: order[id(f)])
    delta.still.sort(key=lambda fp: order[id(fp[0])])
    return delta


def process_for_stackup(stackup: list[dict[str, Any]]) -> str:
    """The capability-table process row for a board's layer COUNT — v1 is
    4-layer-only (backlog decision); 2-layer is kept as a hedge since the
    capability table already carries that row. Raises rather than
    silently guessing for a layer count with no row (:func:`precis.pcb.
    capabilities.capability_for`'s own "raise, don't default" precedent)."""
    n = len(stackup)
    if n == 2:
        return "2layer"
    if n == 4:
        return "4layer"
    raise ValueError(
        f"no DRC capability row for a {n}-layer stackup — v1 checks 2- or "
        "4-layer boards only"
    )


def _two_tier(
    value_mm: float, jlc_min: float | None, house_default: float | None
) -> tuple[str, float] | None:
    """(severity, margin_mm) if ``value_mm`` fails the two-tier bar, else
    ``None`` (clean — no finding). ``jlc_min=None`` means the process/field
    combination is genuinely inapplicable (JLC publishes nothing for it):
    never a finding, never an invented number (capabilities.py's own
    discipline). ``margin_mm`` is always negative when a finding fires —
    the deficit below whichever tier's threshold was crossed."""
    if jlc_min is None:
        return None
    if value_mm < jlc_min - _TIER_EPS:
        return "error", value_mm - jlc_min
    if house_default is not None and value_mm < house_default - _TIER_EPS:
        return "warn", value_mm - house_default
    return None


def _margin_detail(
    label: str,
    value_mm: float,
    capability: CapabilityRow,
    field: str,
    severity: str,
    margin_mm: float,
) -> str:
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    assert jlc_min is not None  # _two_tier already checked this
    if severity == "error":
        return (
            f"{label} {value_mm:.3f}mm < JLC min {jlc_min:.3f}mm "
            f"({capability.process}) — {abs(margin_mm):.3f}mm short of "
            "manufacturable"
        )
    assert house is not None  # a "warn" only fires when house_default exists
    return (
        f"{label} {value_mm:.3f}mm — JLC min {jlc_min:.3f}mm, house default "
        f"{house:.3f}mm ({capability.process}) — spends {abs(margin_mm):.3f}mm "
        "of headroom"
    )


# ── shared geometry: arc flattening (both engines use this identically) ───


def _arc_points(
    seg: dict[str, Any], *, max_seg_deg: float = _ARC_MAX_SEG_DEG
) -> list[Coord]:
    """A gerber-shaped ``{"shape": "arc", ...}`` segment flattened into a
    polyline (start..end inclusive) — the SAME sweep-direction convention
    :mod:`precis.pcb.realize`'s own arcs are generated with and
    ``tests/test_pcb_realize.py``'s sampler already verifies: recompute the
    raw modular angle difference, then take the ``cw`` flag literally
    rather than assuming every arc is the "short way" (defensive: correct
    for either a short or a long arc, not just this codebase's own
    producer's convention)."""
    cx, cy = float(seg["center"][0]), float(seg["center"][1])
    sx, sy = float(seg["start"][0]), float(seg["start"][1])
    ex, ey = float(seg["end"][0]), float(seg["end"][1])
    r = math.hypot(sx - cx, sy - cy)
    a1 = math.atan2(sy - cy, sx - cx)
    a2 = math.atan2(ey - cy, ex - cx)
    diff = (a2 - a1) % (2 * math.pi)
    sweep = diff if not seg.get("cw", True) else diff - 2 * math.pi
    n = max(1, math.ceil(abs(math.degrees(sweep)) / max_seg_deg))
    return [
        (cx + r * math.cos(a1 + sweep * k / n), cy + r * math.sin(a1 + sweep * k / n))
        for k in range(n + 1)
    ]


def _flatten_segments(segments: list[dict[str, Any]]) -> list[Coord]:
    """Every segment (line or arc) of one track, start to end, as one
    polyline — shared prep for both :func:`_copper_item_polygon` (the
    accelerated engine) and, indirectly, :func:`_copper_primitives` (the
    reference oracle builds capsules straight off ``segments`` instead, but
    an arc's flattening comes from the same :func:`_arc_points`)."""
    pts: list[Coord] = []
    for seg in segments:
        if not pts:
            pts.append((float(seg["start"][0]), float(seg["start"][1])))
        if seg.get("shape") == "arc":
            pts.extend(_arc_points(seg)[1:])
        else:
            pts.append((float(seg["end"][0]), float(seg["end"][1])))
    return pts


def _via_layer_names(item: dict[str, Any], all_layers: list[str]) -> list[str]:
    """Copper layers a via flashes on — mirrors :func:`precis.pcb.gerber.
    _via_layers` (duplicated, not imported: that helper is private to its
    own module, and this one is small enough that owning it here beats
    coupling to another module's internal). An explicit ``layers``
    override, else a blind/buried ``span``, else every layer (through)."""
    if item.get("layers"):
        return list(item["layers"])
    span = item.get("span")
    if span:
        i0, i1 = all_layers.index(span[0]), all_layers.index(span[1])
        lo, hi = min(i0, i1), max(i0, i1)
        return all_layers[lo : hi + 1]
    return list(all_layers)


# ── the STRtree-accelerated engine's geometry (per copper ITEM) ───────────


def _copper_item_polygon(item: dict[str, Any]) -> BaseGeometry | None:
    """The physical copper shape of one copper-bearing item as a single
    shapely polygon — a track's full polyline buffered by its half-width in
    one shot (round caps/joins are the physically correct shape for a
    routed trace), a via as a buffered point, a pour as its exterior
    polygon WITH its ``holes`` cut out as interior rings, a PAD (``model
    ["pads"]``, tagged ``ctype="pad"`` by every caller that mixes it into a
    copper-item list — pads carry no ``ctype`` of their own on
    ``model["pads"]``) as its real ``shape`` (circle/rect/obround), not a
    circle stand-in. ``None`` for a degenerate item (a track with < 2
    points, or a pad/via with zero size).

    **Pads are copper, not a lesser class of it — this is the ONE shape
    function.** Every other geometric rule that needs "what does this pad
    physically occupy" (:func:`clearance_pairs_indexed` via
    :func:`check_clearance`) reads it from here, not a second circle/rect
    approximation — that divergence (two functions independently deciding
    "what shape is this thing", answering differently) is this
    subsystem's own most-repeated defect (see :func:`pads_for_ir`'s own
    docstring in :mod:`precis.pcb.realize` for the pad-geometry half of
    the same lesson). ``check_via_pad_keepout`` (gr346004) and
    ``check_outline_containment`` (gr450064/gr449709) both now read a pad's
    real outline through this SAME function too — the disc either used
    before over-stated a non-square pad's reach past its actual land,
    firing on vias/off-board findings against copper that, on the real
    board, clears them by a wide margin. :mod:`precis.pcb.connectivity`'s
    own pad-touch test (:func:`~precis.pcb.connectivity._pad_poly`) reads
    its rect/obround outline from here for the same reason, though it
    keeps its own polygon-vertex machinery rather than sharing shapely
    geometry across module boundaries.

    **A pour's ``holes`` are antipads, not decoration.** :mod:`precis.pcb.
    planes` (:func:`~precis.pcb.planes.plane_pours`, its own docstring)
    punches a hole around every foreign-net via/track that passes through a
    poured layer specifically so the fill does not short it — the same
    reason :mod:`precis.pcb.gerber`'s ``_emit_region`` images a hole's
    interior as clear-polarity copper rather than solid. Reading only
    ``polygon`` here made every consumer of this function see a pour as a
    SOLID sheet where the real copper has a hole cut in it — so a trace
    correctly antipadded inside that hole reported a clearance violation
    against copper that, on the real board, is not there at all. Now that
    copper fill can share a layer with routing (``ir.layer_is_routable``/
    ``layer_is_pourable``), every antipadded feature on a filled layer hit
    this, not just a rare edge case."""
    ctype = item.get("ctype")
    if ctype == "track":
        pts = _flatten_segments(item.get("segments") or [])
        if len(pts) < 2:
            return None
        r = float(item.get("width_mm", 0.0)) / 2.0
        geom = LineString(pts)
        return geom.buffer(r, quad_segs=_BUFFER_QUAD_SEGS) if r > 0 else geom
    if ctype == "via":
        r = float(item.get("dia_mm", 0.0)) / 2.0
        return Point(float(item["x"]), float(item["y"])).buffer(
            r, quad_segs=_BUFFER_QUAD_SEGS
        )
    if ctype == "pour":
        poly = item.get("polygon") or []
        if len(poly) < 3:
            return None
        holes = [
            [(float(p[0]), float(p[1])) for p in hole]
            for hole in item.get("holes") or []
            if len(hole) >= 3
        ]
        return Polygon([(float(p[0]), float(p[1])) for p in poly], holes)
    if ctype == "pad":
        shape = item.get("shape", "circle")
        if shape == "polygon":
            poly = item.get("poly") or []
            if len(poly) < 3:
                return None
            return Polygon([(float(p[0]), float(p[1])) for p in poly])
        x, y = float(item["x"]), float(item["y"])
        w = float(item.get("w", 0.0))
        h = float(item.get("h", w))
        if w <= 0 or h <= 0:
            return None
        if shape == "circle":
            return Point(x, y).buffer(w / 2.0, quad_segs=_BUFFER_QUAD_SEGS)
        if shape == "rect":
            hw, hh = w / 2.0, h / 2.0
            return Polygon(
                [(x - hw, y - hh), (x + hw, y - hh), (x + hw, y + hh), (x - hw, y + hh)]
            )
        if shape == "obround":
            # A capsule -- the same "buffer a segment" construction a track
            # uses above -- with the rounded ends on the LONGER of w/h (the
            # conventional "stadium" reading of an oval pad) and a straight
            # zero-length degenerate case at w == h so this never divides
            # by an undefined axis.
            if w == h:
                return Point(x, y).buffer(w / 2.0, quad_segs=_BUFFER_QUAD_SEGS)
            if w > h:
                half_len, r = (w - h) / 2.0, h / 2.0
                line = LineString([(x - half_len, y), (x + half_len, y)])
            else:
                half_len, r = (h - w) / 2.0, w / 2.0
                line = LineString([(x, y - half_len), (x, y + half_len)])
            return line.buffer(r, quad_segs=_BUFFER_QUAD_SEGS)
        raise ValueError(f"unknown pad shape {shape!r}")
    return None


def _clearance_items(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Every copper-bearing item :func:`clearance_pairs_indexed` /
    :func:`check_clearance` reason about, as ONE list: ``model["copper"]``
    followed by ``model["pads"]`` (each pad shallow-copied with
    ``ctype="pad"`` tacked on so :func:`_copper_item_polygon`'s existing
    ``ctype`` dispatch — and every finding's ``where``/``objects`` text —
    handles a pad exactly like a track/via/pour, never a special case).
    Both functions below build this list the SAME way so an index computed
    by one always means the same item to the other — mirrors :mod:`precis.
    pcb.connectivity`'s ``_pad_primitives(model, start_group=len(model
    ["copper"]))`` offset convention, the precedent already set for
    concatenating pads onto a copper-item index space."""
    return [*(model.get("copper") or []), *_tagged_pads(model)]


def _tagged_pads(model: dict[str, Any]) -> list[dict[str, Any]]:
    """``model["pads"]`` with ``ctype="pad"`` added (shallow copy — never
    mutates the caller's pad dicts) so a pad can sit in the same item list
    as ``model["copper"]``'s tracks/vias/pours."""
    return [{**pad, "ctype": "pad"} for pad in model.get("pads") or []]


def clearance_pairs_indexed(
    model: dict[str, Any], *, required_mm: float
) -> list[tuple[int, int, float, str]]:
    """``(item_i, item_j, gap_mm, layer)`` for every same-layer, different-net
    copper-item pair (tracks/vias/pours/PADS) whose true edge-to-edge gap
    is below ``required_mm`` — the STRtree-accelerated engine. Indices are
    positions in :func:`_clearance_items`'s combined list —
    ``model["copper"]`` followed by ``model["pads"]`` — NOT
    ``model["copper"]`` alone; a pad-involving pair's index can land past
    ``len(model["copper"])``. A per-layer STRtree prunes candidates
    (``predicate='dwithin'``); the reported gap itself is shapely's own
    exact polygon-to-polygon ``distance()``, not an estimate — the
    acceleration is entirely in *which pairs get checked*, never in the
    number reported for a pair that IS checked.

    **Pads used to be invisible here entirely** — this function iterated
    ``model["copper"]`` only, so a pad (a separate top-level model key,
    ``to_gerber_model``'s/``_render_drc``'s own shape) was never a
    candidate on EITHER side of a pair: pad-vs-pour, pad-vs-track,
    pad-vs-via and pad-vs-pad clearance all went unchecked, on a board
    whose pads are exactly the copper you solder to. Fixed by folding
    ``model["pads"]`` into the same per-layer STRtree pass as every other
    copper item (:func:`_clearance_items`), not a parallel pad-only pass —
    a pad is a same-net-exempt, different-net-checked copper item like any
    other, not a second kind of question.

    **A via has no single ``item["layer"]``** — it flashes on every layer
    :func:`_via_layer_names` says it spans, exactly like
    :func:`_copper_primitives` already handles for the reference oracle.
    One source item can therefore contribute an entry to SEVERAL per-layer
    STRtrees at once (``entries`` below is keyed by ``(source_idx, layer)``,
    not by ``source_idx`` alone) — an earlier version of this function
    read ``item.get("layer")`` unconditionally, which silently bucketed
    every via under a nonexistent ``""`` layer and dropped it out of every
    real layer's candidate set. Caught by :func:`clearance_violations_naive`
    disagreeing on exactly this case (property test,
    ``tests/test_pcb_drc.py``) — the oracle doing its job.

    **The reference oracle does NOT cross-check a pad pair.**
    :func:`clearance_violations_naive` is restricted to the circle/capsule
    primitive alphabet (module docstring); a rect/obround pad is neither,
    so pad clearance — like pour clearance before it — is validated by
    this accelerated engine alone, a stated gap in oracle coverage, not a
    silent one.

    **A drilled (through-hole) pad has no single ``item["layer"]`` either**
    (gr341516) — its plated land is real copper on every board layer, not
    only the one side :func:`precis.pcb.realize.pads_for_ir` reports for
    it (that field means "this pad's OUTER flash", see that function's own
    docstring), exactly the physical fact :func:`check_annular_ring`
    already treats a drilled pad and a router via as the same construct
    for. Handled the SAME way a via's multi-layer span is here: one entry
    per layer, deduplicated at the SOURCE-pair level below so a THT pad
    close to a foreign net on several layers still reports ONE finding,
    not one per layer."""
    items = _clearance_items(model)
    all_layers = list(model.get("layers") or [])
    # (source_idx, net, layer, polygon) — one entry per (item, layer it's on).
    entries: list[tuple[int, str, str, BaseGeometry]] = []
    for idx, item in enumerate(items):
        poly = _copper_item_polygon(item)
        if poly is None or poly.is_empty:
            continue
        net = str(item.get("net", ""))
        if item.get("ctype") == "via" or (
            item.get("ctype") == "pad" and item.get("drill")
        ):
            for layer in _via_layer_names(item, all_layers):
                entries.append((idx, net, layer, poly))
        else:
            entries.append((idx, net, str(item.get("layer", "")), poly))

    by_layer: dict[str, list[int]] = {}
    for local_i, (_src, _net, layer, _poly) in enumerate(entries):
        by_layer.setdefault(layer, []).append(local_i)

    out: list[tuple[int, int, float, str]] = []
    seen_src: set[tuple[int, int]] = set()
    for layer, locals_ in by_layer.items():
        if len(locals_) < 2:
            continue
        layer_polys = [entries[i][3] for i in locals_]
        tree = STRtree(layer_polys)
        seen_local: set[tuple[int, int]] = set()
        for local_i in locals_:
            src_i, net_i, _layer_i, poly_i = entries[local_i]
            for c in tree.query(poly_i, predicate="dwithin", distance=required_mm):
                local_j = locals_[int(c)]
                src_j, net_j, _layer_j, poly_j = entries[local_j]
                if src_i == src_j or net_i == net_j:
                    continue
                key_local = (min(local_i, local_j), max(local_i, local_j))
                if key_local in seen_local:
                    continue
                seen_local.add(key_local)
                src_key = (min(src_i, src_j), max(src_i, src_j))
                if src_key in seen_src:
                    continue  # a through via already matched this source pair on another layer
                gap = poly_i.distance(poly_j)
                if gap < required_mm - _EPS:
                    seen_src.add(src_key)
                    out.append((src_key[0], src_key[1], gap, layer))
    return out


def _clearance_detail(
    value_mm: float,
    jlc_min: float,
    required_mm: float | None,
    process: str,
    severity: str,
    margin_mm: float,
    floor_mm: float | None = None,
) -> str:
    """Same two-tier wording as :func:`_margin_detail`, but takes the
    per-PAIR required clearance directly rather than pulling a single
    figure off ``capability.house_default`` — the ``check_clearance``
    threshold is now per-net (``net_rules``), not always the fab's generic
    house default (module docstring). ``floor_mm`` is the pair's actual
    error threshold — above ``jlc_min`` when a class's ``min_clearance_mm``
    / ``working_voltage_v`` hard floor binds (Slice 4b), so the message
    names the floor that was crossed, never a figure that wasn't."""
    if severity == "error":
        floor = floor_mm if floor_mm is not None else jlc_min
        if floor > jlc_min:
            return (
                f"copper clearance {value_mm:.3f}mm < class floor "
                f"{floor:.3f}mm (net-class min_clearance_mm; JLC min "
                f"{jlc_min:.3f}mm, {process}) — {abs(margin_mm):.3f}mm short "
                "of the creepage/voltage requirement"
            )
        return (
            f"copper clearance {value_mm:.3f}mm < JLC min {jlc_min:.3f}mm "
            f"({process}) — {abs(margin_mm):.3f}mm short of manufacturable"
        )
    assert required_mm is not None  # a "warn" only fires when a tier exists
    return (
        f"copper clearance {value_mm:.3f}mm — JLC min {jlc_min:.3f}mm, required "
        f"{required_mm:.3f}mm ({process}) — spends {abs(margin_mm):.3f}mm of headroom"
    )


def _clearance_label(item: dict[str, Any]) -> str:
    """A pad named by its PART and pin, never by its net alone — the
    :func:`check_via_pad_keepout` fix (gr451052) applied to its sibling.
    On ``ewod-dogfood-6`` ``pad[ARR1_R7C0]`` read as "the R7C0 electrode"
    when it was ``ARR1_SINK_0.HVOUT24``, the driver's land carrying that
    electrode's escape net through a pin swap; it produced two wrong
    diagnoses of a fabrication-fatal defect (2026-09-30)."""
    ctype, net = item.get("ctype"), item.get("net")
    refdes, pin = item.get("refdes"), item.get("pin")
    if ctype == "pad" and refdes and pin:
        return f"pad[{refdes}/{pin} " + (f"net {net}]" if net else "no net]")
    return f"{ctype}[{net}]"


def _nearest_xy(
    a: dict[str, Any], b: dict[str, Any]
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """The point on each shape nearest the other — where to look."""
    pa, pb = _copper_item_polygon(a), _copper_item_polygon(b)
    if pa is None or pb is None:
        return None, None
    na, nb = nearest_points(pa, pb)
    return (float(na.x), float(na.y)), (float(nb.x), float(nb.y))


def _derived_tag(item: dict[str, Any]) -> dict[str, Any]:
    """``{"derived": True}`` for a ROUTER-drawn copper row, else ``{}``.

    A caller that must tell router copper from authored (``fixed``) copper in
    a finding's ``objects`` (:func:`precis.pcb.session.
    strip_drc_violating_nets`) tags the model's router rows ``derived: True``;
    this carries that tag through to the finding. Absent for every other
    caller, so existing finding shapes are unchanged."""
    return {"derived": True} if item.get("derived") else {}


def _clearance_object(
    item: dict[str, Any], near: tuple[float, float] | None
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "ctype": item.get("ctype"),
        "net": item.get("net"),
        "layer": item.get("layer"),
        **_derived_tag(item),
    }
    if item.get("fixed_id") is not None:
        obj["fixed_id"] = item["fixed_id"]
    if item.get("ctype") == "pad" and item.get("refdes"):
        obj["refdes"], obj["pin"] = item.get("refdes"), item.get("pin")
    if near is not None:
        obj["x"], obj["y"] = round(near[0], 4), round(near[1], 4)
    return obj


def check_clearance(
    model: dict[str, Any],
    capability: CapabilityRow,
    *,
    net_rules: dict[str, NetRules] | None = None,
    net_voltages: dict[str, float] | None = None,
) -> list[DrcFinding]:
    """Copper-to-copper clearance, different nets — the class-rule check
    (module docstring: "class" = fab process, per JLC's own naming, read
    off ``capabilities.py``, house-default tier). STRtree-accelerated; see
    :func:`clearance_violations_naive` for the independent reference this
    is checked against (pad pairs are NOT part of that comparison — see
    :func:`clearance_pairs_indexed`'s own docstring).

    **Pads are checked here too** (:func:`_clearance_items`) — a pad is
    the copper you solder to, and is exempt from this rule on the same
    terms as any other same-net copper (a trace legitimately lands on its
    own pad) and checked on the same terms otherwise (a foreign pad, pour,
    track or via too close is exactly what this rule exists to catch).

    ``net_rules`` (net NAME -> :class:`~precis.pcb.rules.NetRules`, the
    same resolved rules :mod:`precis.pcb.realize` used to draw the copper)
    supplies a PER-NET required clearance in place of the flat
    ``house_default`` — a pair's actual threshold is the STRICTER
    (larger) of its two nets' own resolved clearance, since a net that
    wants more room never gets less just because its neighbour wants
    less. A net absent from ``net_rules`` (or ``net_rules=None``
    entirely) falls back to the generic ``house_default`` tier, today's
    behaviour unchanged.

    ``net_voltages`` (net NAME -> peak working voltage, ``pcb_nets.
    working_voltage_v``) adds the PAIRWISE term
    ``docs/backlog/pcb-missing-constraint-classes.md`` §E-1 specifies:
    required spacing is a function of ``|V_a - V_b|`` (IPC-2221B Table 6-1,
    via :func:`precis.pcb.capabilities.conductor_spacing_mm`), a property
    of the *pair*, not of either net — a 20 V net beside another 20 V net
    needs nothing special, the same net beside ground needs the full
    spacing. It is folded into the same ``max`` as the per-net floors,
    never replacing them.

    **Both nets of a pair must be annotated for the term to apply.** A
    missing annotation is not 0 V — inferring one would invent the very
    number the rule turns on — so a half-annotated pair is checked on the
    per-net floors alone, and the run emits one ``voltage_spacing_unknown``
    warning naming the unannotated nets involved. Silence there would be
    the dangerous direction.

    Coating: ``coated=False`` (the B2 column, the larger requirement).
    Nothing in the schema records whether a board is conformal-coated, so
    the uncoated figure is the honest default rather than a guess that
    relaxes a safety rule."""
    field = "trace_spacing_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    if jlc_min is None:
        return []
    if net_rules:
        resolved_values = [r.clearance_mm for r in net_rules.values()]
        query_radius = (
            max(resolved_values)
            if resolved_values
            else (house if house is not None else jlc_min)
        )
    else:
        query_radius = house if house is not None else jlc_min
    outer_layers = _outer_layers(model)
    items = _clearance_items(model)
    # The two coverage reports are computed off the NET SET, before the
    # STRtree prune — never inside the pair loop. A pair that the prune
    # already dropped is exactly the pair whose annotation gap matters most
    # (it was dropped because no rule demanded that much room, which is the
    # thing a missing voltage would have changed), so a report keyed on
    # surviving pairs would go quiet precisely when it should speak.
    unannotated: set[str] = set()
    over_range: set[str] = set()
    if net_voltages:
        copper_nets = {
            str(item.get("net")) for item in items if item.get("net") not in (None, "")
        }
        unannotated = copper_nets - set(net_voltages)
        values = [float(v) for v in net_voltages.values() if v is not None]
        if (
            len(values) >= 2
            and _voltage_spacing_mm(max(values) - min(values), outer=True) is None
        ):
            over_range = {
                name
                for name, v in net_voltages.items()
                if v is not None and float(v) in (max(values), min(values))
            }
        # The STRtree prunes by this radius, so a pair that only the voltage
        # term would have failed has to survive the prune first — widen to
        # the worst spacing any annotated pair could demand, or the term
        # silently never fires on the pairs it exists for.
        query_radius = max(query_radius, _max_voltage_spacing_mm(net_voltages))
    pairs = clearance_pairs_indexed(model, required_mm=query_radius)
    findings: list[DrcFinding] = []
    for i, j, gap, layer in pairs:
        a, b = items[i], items[j]
        required = house
        if net_rules:
            candidates = [
                r.clearance_mm
                for r in (
                    net_rules.get(str(a.get("net"))),
                    net_rules.get(str(b.get("net"))),
                )
                if r is not None
            ]
            if candidates:
                required = max(candidates)
        if net_voltages:
            v_a = net_voltages.get(str(a.get("net")))
            v_b = net_voltages.get(str(b.get("net")))
            # Both or neither: a missing annotation is not 0 V (the
            # coverage report above already named it), and a difference
            # past the table's top band yields None rather than an
            # extrapolation.
            if v_a is not None and v_b is not None:
                spacing = _voltage_spacing_mm(v_a - v_b, outer=layer in outer_layers)
                if spacing is not None:
                    required = spacing if required is None else max(required, spacing)
        # Slice 4b (ewod-controller-and-hv-supply.md): a net's resolved
        # ``min_clearance_mm`` is a HARD floor — below it is an ERROR, the
        # same tier the fab minimum owns, because a creepage requirement
        # reported as a warning is not acceptable. The pair floor is the
        # stricter of the two nets', never below the fab's own.
        floor = jlc_min
        if net_rules:
            floors = [
                r.min_clearance_mm
                for r in (
                    net_rules.get(str(a.get("net"))),
                    net_rules.get(str(b.get("net"))),
                )
                if r is not None and r.min_clearance_mm is not None
            ]
            if floors:
                floor = max(floor, *floors)
        # Two pads of ONE footprint sit at the part's vendor-fixed pitch
        # (a 0.65mm-pitch HV sink's VPP and DIOB lands): placement and
        # routing cannot widen that gap, so a class floor above the fab
        # minimum between them is a WARN, not a put-blocking error. The
        # fab minimum itself stays an error, and pads of DIFFERENT
        # footprints keep the full class floor.
        if (
            floor > jlc_min
            and jlc_min - _TIER_EPS <= gap < floor - _TIER_EPS
            and _same_footprint_pads(a, b)
        ):
            findings.append(_part_fixed_clearance_finding(a, b, gap, floor, layer))
            continue
        result = _two_tier(gap, floor, required)
        if result is None:
            continue
        severity, margin = result
        # The layer the pair actually met on — never `a["layer"]`: a via
        # (or THT pad) spans several layers and its row's nominal `layer`
        # is F.Cu regardless, so a via against a bottom-side pad used to
        # read "on F.Cu" while via_pad_keepout said B.Cu for the same
        # pair (gr345858).
        where = f"{_clearance_label(a)} <-> {_clearance_label(b)} on {layer}"
        # Only flagged pairs reach this line, so the nearest-point pass costs
        # nothing on the pairs the STRtree pass clears.
        near_a, near_b = _nearest_xy(a, b)
        findings.append(
            DrcFinding(
                rule="clearance",
                severity=severity,
                where=where,
                detail=_clearance_detail(
                    gap,
                    jlc_min,
                    required,
                    capability.process,
                    severity,
                    margin,
                    floor_mm=floor,
                )
                + (
                    f" — nearest points ({near_a[0]:.3f}, {near_a[1]:.3f}) / "
                    f"({near_b[0]:.3f}, {near_b[1]:.3f})"
                    if near_a and near_b
                    else ""
                ),
                objects=(
                    _clearance_object(a, near_a),
                    _clearance_object(b, near_b),
                ),
                margin_mm=margin,
            )
        )
    if unannotated:
        named = sorted(unannotated)
        findings.append(
            DrcFinding(
                rule="voltage_spacing_unknown",
                severity="warn",
                where=f"{len(named)} net(s) with no working_voltage_v",
                detail=(
                    "pairwise IPC-2221B voltage spacing was NOT applied to "
                    "pairs involving these nets — a missing annotation is "
                    "not 0 V, so the pair was checked on the per-net "
                    "clearance floors alone: "
                    + ", ".join(named[:8])
                    + ("…" if len(named) > 8 else "")
                    + ". Set working_voltage_v on them (0 is a valid "
                    "annotation for a net tied to chassis/ground)."
                ),
                objects=tuple({"net": n} for n in named[:8]),
            )
        )
    if over_range:
        named = sorted(over_range)
        findings.append(
            DrcFinding(
                rule="voltage_spacing_out_of_table",
                severity="warn",
                where=f"{len(named)} net(s) past IPC-2221B Table 6-1",
                detail=(
                    "the widest potential difference on this board is past "
                    "IPC-2221B Table 6-1's 500 V top band — per-volt "
                    "extrapolation above a table this coarse is out of "
                    "scope, so any PAIR whose own difference exceeds it gets "
                    "no voltage spacing (pairs within the band are still "
                    "checked normally). The extremes are: "
                    + ", ".join(named[:8])
                    + ("…" if len(named) > 8 else "")
                    + ". Supply an explicit clearance_mm on their net class "
                    "instead."
                ),
                objects=tuple({"net": n} for n in named[:8]),
            )
        )
    return findings


def _same_footprint_pads(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Both items are pads of the same placed part (same refdes)."""
    return (
        a.get("ctype") == "pad"
        and b.get("ctype") == "pad"
        and bool(a.get("refdes"))
        and a.get("refdes") == b.get("refdes")
    )


def _part_fixed_clearance_finding(
    a: dict[str, Any],
    b: dict[str, Any],
    gap: float,
    floor: float,
    layer: str,
) -> DrcFinding:
    """The WARN :func:`check_clearance` emits for a pad pair inside one
    footprint that is below its net-class floor but at or above the fab
    minimum: names the footprint, both pads, the actual and required
    clearance, and that the pitch is fixed by the part. Default chosen
    pending Reto (2026-10-16); an error plus a per-footprint waiver is
    the possible follow-up."""
    near_a, near_b = _nearest_xy(a, b)
    return DrcFinding(
        rule="clearance",
        severity="warn",
        where=f"{_clearance_label(a)} <-> {_clearance_label(b)} on {layer}",
        detail=(
            f"footprint {a.get('refdes')}: pads {a.get('pin')} and "
            f"{b.get('pin')} are {gap:.3f}mm apart, below the net-class "
            f"floor {floor:.3f}mm (min_clearance_mm) — the pad pitch is "
            "fixed by the part, so placement and routing cannot meet it; "
            "check that the part's own voltage rating covers this pair"
        )
        + (
            f" — nearest points ({near_a[0]:.3f}, {near_a[1]:.3f}) / "
            f"({near_b[0]:.3f}, {near_b[1]:.3f})"
            if near_a and near_b
            else ""
        ),
        objects=(_clearance_object(a, near_a), _clearance_object(b, near_b)),
        margin_mm=gap - floor,
    )


def _outer_layers(model: dict[str, Any]) -> frozenset[str]:
    """The stackup's two outermost copper layer NAMES — the IPC-2221B
    external/internal split. An unknown or degenerate stackup yields the
    empty set, which reads every layer as internal: the SMALLER B1
    requirement, so this must never be the silent path on a real board.
    Every caller that threads ``net_voltages`` also supplies ``layers``."""
    layers = [str(name) for name in (model.get("layers") or [])]
    if len(layers) < 2:
        return frozenset(layers)
    return frozenset({layers[0], layers[-1]})


def _voltage_spacing_mm(delta_v: float, *, outer: bool) -> float | None:
    """IPC-2221B Table 6-1 spacing for a potential difference, or ``None``
    when the table cannot answer.

    :func:`precis.pcb.capabilities.conductor_spacing_mm` RAISES above its
    500 V top band rather than extrapolating (its own documented rule).
    That is right for a generator computing one electrode gap and wrong
    here: a single mis-typed annotation would take the whole ``view='drc'``
    down with a ValueError instead of reporting a board. So the refusal is
    caught and surfaced as a finding by the caller."""
    try:
        return conductor_spacing_mm(
            abs(delta_v), layer="external" if outer else "internal", coated=False
        )
    except ValueError:
        return None


def _max_voltage_spacing_mm(net_voltages: dict[str, float]) -> float:
    """The largest spacing any annotated pair on this board could demand —
    the widest |V_a - V_b| read at the *external* (larger) column. Used
    only to widen the STRtree query radius, never as a threshold."""
    values = [float(v) for v in net_voltages.values() if v is not None]
    if len(values) < 2:
        return 0.0
    return _voltage_spacing_mm(max(values) - min(values), outer=True) or 0.0


# ── the O(n^2) reference oracle — no shapely, no spatial index ────────────
#
# A brute-force, obviously-correct implementation over the same primitive
# alphabet realize.py's closed-form geometry already uses (circles for
# vias, capsules — a line segment with radius — for track segments): every
# pairwise clearance is computed by exact point/segment math, not shapely.
# This is the strongest oracle available against a spatial-index bug
# precisely BECAUSE it cannot share a bug with the STRtree engine above —
# it doesn't import shapely at all.


@dataclass(frozen=True, slots=True)
class _Prim:
    """One circle (``b=None``) or capsule (line segment ``a``-``b``,
    radius ``r``) — ``group`` is the parent ``model["copper"]`` index (two
    primitives of the SAME group, e.g. two segments of one track, never
    count as a clearance pair against each other, mirroring
    :func:`precis.pcb.geom.sweep_line_crossings`'s ``group_id`` exclusion)."""

    a: Coord
    b: Coord | None
    r: float
    group: int
    net: str
    layer: str


def _copper_primitives(model: dict[str, Any]) -> list[_Prim]:
    """Track/via items only, flattened to circles/capsules — pours AND
    pads are deliberately excluded (module docstring: the oracle's scope
    is the closed-form-representable primitives; a rect/obround pad no
    more fits a circle/capsule than a pour polygon does, so pad clearance
    — like pour clearance before it — is validated by the accelerated
    engine alone)."""
    prims: list[_Prim] = []
    for idx, item in enumerate(model.get("copper") or []):
        ctype = item.get("ctype")
        net = str(item.get("net", ""))
        if ctype == "track":
            r = float(item.get("width_mm", 0.0)) / 2.0
            layer = str(item.get("layer", ""))
            for seg in item.get("segments") or []:
                if seg.get("shape") == "arc":
                    pts = _arc_points(seg)
                    prims.extend(
                        _Prim(pts[k], pts[k + 1], r, idx, net, layer)
                        for k in range(len(pts) - 1)
                    )
                else:
                    a = (float(seg["start"][0]), float(seg["start"][1]))
                    b = (float(seg["end"][0]), float(seg["end"][1]))
                    prims.append(_Prim(a, b, r, idx, net, layer))
        elif ctype == "via":
            r = float(item.get("dia_mm", 0.0)) / 2.0
            x, y = float(item["x"]), float(item["y"])
            layers = _via_layer_names(item, list(model.get("layers") or []))
            for layer in layers:
                prims.append(_Prim((x, y), None, r, idx, net, layer))
    return prims


def _on_segment(p: Coord, q: Coord, r: Coord, *, eps: float = 1e-9) -> bool:
    """``q`` assumed collinear with ``p``/``r``: is it between them?"""
    return (
        min(p[0], r[0]) - eps <= q[0] <= max(p[0], r[0]) + eps
        and min(p[1], r[1]) - eps <= q[1] <= max(p[1], r[1]) + eps
    )


def _segments_intersect(p1: Coord, p2: Coord, p3: Coord, p4: Coord) -> bool:
    """Classic orientation-test segment intersection, collinear-overlap and
    touching-endpoint cases included — needed because two SEGMENTS
    (unlike two points) can cross at an interior point that is neither
    endpoint, which no point-to-segment distance below would ever find as
    zero (see :func:`_capsule_capsule_gap`'s docstring)."""
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    eps = 1e-9
    if abs(d1) < eps and _on_segment(p3, p1, p4):
        return True
    if abs(d2) < eps and _on_segment(p3, p2, p4):
        return True
    if abs(d3) < eps and _on_segment(p1, p3, p2):
        return True
    return bool(abs(d4) < eps and _on_segment(p1, p4, p2))


def _capsule_capsule_gap(x: _Prim, y: _Prim) -> float:
    """Exact edge-to-edge gap between two circles/capsules, closed form.

    Circle-circle and circle-capsule reduce to a single point/segment
    distance. Segment-segment is the one non-trivial case: the minimum
    distance between two DISJOINT segments is always achieved at an
    endpoint of one projected onto the other (a standard computational-
    geometry fact — the min of the 4 endpoint-to-opposite-segment
    distances), but if the segments actually cross, that minimum is 0 at
    an INTERIOR point neither endpoint distance would find, so crossing is
    checked explicitly first via :func:`_segments_intersect`."""
    if x.b is None and y.b is None:
        center = _dist(x.a, y.a)
    elif x.b is None:
        assert y.b is not None  # the first branch already excluded "both circles"
        center = dist_point_to_segment(x.a, y.a, y.b)
    elif y.b is None:
        center = dist_point_to_segment(y.a, x.a, x.b)
    elif _segments_intersect(x.a, x.b, y.a, y.b):
        center = 0.0
    else:
        center = min(
            dist_point_to_segment(x.a, y.a, y.b),
            dist_point_to_segment(x.b, y.a, y.b),
            dist_point_to_segment(y.a, x.a, x.b),
            dist_point_to_segment(y.b, x.a, x.b),
        )
    return max(0.0, center - x.r - y.r)


def clearance_violations_naive(
    model: dict[str, Any], *, required_mm: float
) -> list[tuple[int, int, float]]:
    """The O(n^2) reference oracle (backlog, verbatim): every same-layer,
    different-net pair of track/via copper ITEMS whose exact minimum
    edge-to-edge gap (the min over every constituent circle/capsule sub-
    primitive pair sharing a layer — see :func:`_capsule_capsule_gap`) is
    below ``required_mm``. Pure closed-form math; no shapely, no
    dependency. Compare against :func:`check_clearance` / :func:`clearance_
    pairs_indexed` — the property test in ``tests/test_pcb_drc.py`` asserts
    the two agree on both WHICH pairs violate and the gap number itself,
    over many randomized track/via-only layouts.

    **Checks every (sub-primitive, sub-primitive) pair for a SHARED
    layer directly**, taking the minimum gap over only those pairs that
    share one, rather than gating the whole group pair on one
    primitive's layer. Comparing only each GROUP's FIRST primitive's
    ``.layer`` is not enough to decide "do these two items share a layer
    at all": that's correct for a track (every sub-primitive on the same
    single layer) or a single-layer via, but a real multi-layer via
    (:mod:`precis.pcb.realize`'s own
    :class:`~precis.pcb.realize.RealizedVia`, e.g. a blind via spanning
    F.Cu..In1.Cu) breaks that shortcut: two vias with only a PARTIALLY
    overlapping span (say F.Cu..In1.Cu and In1.Cu..B.Cu) can have first
    primitives on different layers even though they DO share In1.Cu and
    DO clash there."""
    prims = _copper_primitives(model)
    by_group: dict[int, list[_Prim]] = {}
    for p in prims:
        by_group.setdefault(p.group, []).append(p)
    groups = sorted(by_group)
    out: list[tuple[int, int, float]] = []
    for gi in range(len(groups)):
        for gj in range(gi + 1, len(groups)):
            i, j = groups[gi], groups[gj]
            group_i, group_j = by_group[i], by_group[j]
            if group_i[0].net == group_j[0].net:
                continue
            best_gap: float | None = None
            for px in group_i:
                for py in group_j:
                    if px.layer != py.layer:
                        continue
                    gap = _capsule_capsule_gap(px, py)
                    if best_gap is None or gap < best_gap:
                        best_gap = gap
            if best_gap is None:
                continue  # these two items share no layer at all
            if best_gap < required_mm - _EPS:
                out.append((i, j, best_gap))
    return out


# ── trace width ─────────────────────────────────────────────────────────


def check_trace_width(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    field = "trace_width_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    findings: list[DrcFinding] = []
    for item in model.get("copper") or []:
        if item.get("ctype") != "track":
            continue
        width = float(item.get("width_mm", 0.0))
        result = _two_tier(width, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        net, layer = item.get("net"), item.get("layer")
        findings.append(
            DrcFinding(
                rule="trace_width",
                severity=severity,
                where=f"{net} on {layer}",
                detail=_margin_detail(
                    "trace width", width, capability, field, severity, margin
                ),
                objects=({"net": net, "layer": layer, **_derived_tag(item)},),
                margin_mm=margin,
            )
        )
    return findings


# ── annular ring (vias) ────────────────────────────────────────────────


def _pad_boundary_edges(
    pad: dict[str, Any],
) -> list[tuple[float, float, float, float]]:
    """The pad's land outline, board frame, as boundary segments — its
    ``poly`` ring when it carries one (a polygon pad, or the true rotated
    outline :func:`precis.pcb.realize.pads_for_ir` emits for an oblique
    land), else the w×h rect rotated by ``oblique_rot`` when the pad
    recorded one (``oblique_rot`` is instance+pad rotation, so for a
    mirrored bottom pad whose pad-local ``rot`` was nonzero the rect here
    is the nearest available outline, not the exact one — the poly path
    above is what the DRC model actually feeds for those). An obround pad
    is read on its bbox: the stadium's side walls ARE the bbox edges, so
    a lateral measurement is exact everywhere but right at the end arcs,
    where it errs a hair generous."""
    poly = pad.get("poly")
    if poly:
        pts = [(float(p[0]), float(p[1])) for p in poly]
    else:
        w = float(pad.get("w", 0.0))
        h = float(pad.get("h", w))
        hw, hh = w / 2.0, h / 2.0
        rot = math.radians(float(pad.get("oblique_rot") or 0.0))
        c, s = math.cos(rot), math.sin(rot)
        cx, cy = float(pad["x"]), float(pad["y"])
        pts = [
            (cx + px * c - py * s, cy + px * s + py * c)
            for px, py in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
        ]
    return [
        (
            pts[i][0],
            pts[i][1],
            pts[(i + 1) % len(pts)][0],
            pts[(i + 1) % len(pts)][1],
        )
        for i in range(len(pts))
    ]


def _slot_lateral_ring_mm(pad: dict[str, Any], slot: Any, drill: float) -> float:
    """The copper left BESIDE a plated slot — the annular ring a fab floor
    can actually hold a slotted pad to. Measured across the slot's minor
    axis: the pad boundary's nearest approach to the slot's end-centre
    line within the slot's own span, minus the cutter radius (``drill``
    is already the slot's minor dimension, the round cutter width — the
    same convention :func:`precis.pcb.padplace._slot_centre_line`
    authors).

    The ring at the slot's ENDS is deliberately not part of this figure.
    A slot that pierces its pad's edge along the long axis is an authored
    open slot — taz15-blank's four USB-C shell GND slots are exactly one
    (pad 1.1×1.6mm, the 0.6mm slot spanning the pad's full short axis, so
    the ring the old per-axis ``min`` read at the ends was 0.000mm on
    every one — gr484771) — and the fab plates it as drawn. What must
    still meet the floor is the LATERAL ring, the copper wrapping the
    slot's long edges."""
    (sx1, sy1), (sx2, sy2) = slot
    dx, dy = sx2 - sx1, sy2 - sy1
    span = math.hypot(dx, dy)
    if span < 1e-9:
        # A degenerate slot line carries no axis; judge it as the round
        # hole it degenerates to, against the pad's narrow side.
        w = float(pad.get("w", 0.0))
        h = float(pad.get("h", w))
        dia = float(pad.get("land_min_mm") or min(w, h))
        return (dia - drill) / 2.0
    ux, uy = dx / span, dy / span
    mx, my = (sx1 + sx2) / 2.0, (sy1 + sy2) / 2.0
    half = span / 2.0
    nearest = math.inf
    for ex1, ey1, ex2, ey2 in _pad_boundary_edges(pad):
        # In slot coordinates (u along the slot, v across it, origin the
        # span midpoint) keep only the boundary facing the slot's SIDES:
        # the strip |a| <= half. Edges past the slot's ends never enter
        # it, so an open end contributes nothing by construction.
        a1 = (ex1 - mx) * ux + (ey1 - my) * uy
        t1 = -(ex1 - mx) * uy + (ey1 - my) * ux
        a2 = (ex2 - mx) * ux + (ey2 - my) * uy
        t2 = -(ex2 - mx) * uy + (ey2 - my) * ux
        da = a2 - a1
        if da == 0.0:
            if not -half <= a1 <= half:
                continue
            ta, tb = t1, t2
        else:
            s0, s1 = (-half - a1) / da, (half - a1) / da
            if s0 > s1:
                s0, s1 = s1, s0
            s0, s1 = max(s0, 0.0), min(s1, 1.0)
            if s0 > s1:
                continue
            ta = t1 + (t2 - t1) * s0
            tb = t1 + (t2 - t1) * s1
        # t is linear over the clipped interval, so its |.| bottoms out
        # at whichever end sits nearer the slot line — or exactly 0 where
        # the edge crosses it (the slot's side is outside the pad).
        nearest = min(nearest, 0.0 if ta * tb <= 0 else min(abs(ta), abs(tb)))
    if nearest is math.inf:
        # No pad boundary beside the slot at all: nothing wraps its
        # sides, so there is no ring — a certain violation.
        return -drill / 2.0
    return nearest - drill / 2.0


def _drilled_pad_ring_items(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Every drilled THT footprint pad's own physical hole, deduplicated to
    ONE entry per hole. :func:`precis.pcb.padplace.place_footprint_pads`
    flashes a drilled pad once PER COPPER LAYER (a real annular ring runs
    the full stack, that module's own docstring) — the SAME hole would
    otherwise be ring-checked once per layer here, an inflated finding
    count for a board with >2 copper layers rather than a wrong one, but
    inflated all the same. Deduped by ``(x, y, drill)`` — the physical
    identity of one drilled hole, independent of which layer's flash we
    happened to see first."""
    seen: dict[tuple[float, float, float], dict[str, Any]] = {}
    for pad in model.get("pads") or []:
        drill = pad.get("drill")
        if not drill:
            continue
        key = (
            round(float(pad["x"]), 4),
            round(float(pad["y"]), 4),
            round(float(drill), 4),
        )
        seen.setdefault(key, pad)
    return list(seen.values())


def check_annular_ring(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    """A plated hole's annular ring, checked for BOTH constructs this
    codebase drills a hole through the board for: a router-placed
    ``model["copper"]`` via, and an authored THT footprint pad
    (``pad["drill"]`` set — :mod:`precis.pcb.generators`'s EWOD plaza via
    is exactly this: a real, fabricable drilled pad, never a ``copper``
    via row by design — see that module's docstring). Before
    pcb-ewod-multitile Slice 2 this rule only ever saw router vias, so a
    board's OWN drilled footprint pads (a plaza via, or any ordinary
    catalog THT part) had their annular ring computed but never validated
    against the fab floor — a check that could not fire on a whole class
    of drilled hole, silently. A rect/obround THT pad's ring is measured
    on its NARROW axis (``min(w, h)``, not the ``max`` a keepout-radius
    check like :func:`check_via_pad_keepout` conservatively uses) — the
    narrow axis is where an oblong pad's own ring is thinnest, and that is
    the dimension a manufacturability floor must be checked against."""
    field = "annular_ring_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    findings: list[DrcFinding] = []
    for item in model.get("copper") or []:
        if item.get("ctype") != "via":
            continue
        dia, drill = float(item.get("dia_mm", 0.0)), float(item.get("drill_mm", 0.0))
        ring = (dia - drill) / 2.0
        result = _two_tier(ring, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        net = item.get("net")
        where = f"via[{net}] @ ({item.get('x')}, {item.get('y')})"
        findings.append(
            DrcFinding(
                rule="annular_ring",
                severity=severity,
                where=where,
                detail=_margin_detail(
                    "via annular ring", ring, capability, field, severity, margin
                ),
                objects=(
                    {
                        "net": net,
                        "x": item.get("x"),
                        "y": item.get("y"),
                        **_derived_tag(item),
                    },
                ),
                margin_mm=margin,
            )
        )
    for pad in _drilled_pad_ring_items(model):
        w = float(pad.get("w", 0.0))
        h = float(pad.get("h", w))
        # An oblique pad's w/h are its rotated bbox; `land_min_mm` is the
        # land's real narrow side (realize.pads_for_ir).
        dia = float(pad.get("land_min_mm") or min(w, h))
        drill = float(pad["drill"])
        slot = pad.get("slot")
        if slot:
            # A plated slot's floor is the LATERAL ring — the copper
            # beside its long edges — not an end ring the pad never
            # promised (:func:`_slot_lateral_ring_mm`; gr484771).
            ring = _slot_lateral_ring_mm(pad, slot, drill)
        else:
            ring = (dia - drill) / 2.0
        result = _two_tier(ring, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        net = pad.get("net")
        where = f"pad[{net}] @ ({pad.get('x')}, {pad.get('y')})"
        findings.append(
            DrcFinding(
                rule="annular_ring",
                severity=severity,
                where=where,
                detail=_margin_detail(
                    "THT pad annular ring", ring, capability, field, severity, margin
                ),
                objects=({"net": net, "x": pad.get("x"), "y": pad.get("y")},),
                margin_mm=margin,
            )
        )
    return findings


# ── NPTH copper clearance (a distinct field from via annular ring) ───────


def check_npth_clearance(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    """Copper must clear a non-plated hole by ``npth_annular_ring_mm`` (JLC
    needs bare copper cleared around an NPTH for the sealing film — a
    DIFFERENT field from a via's plated annular ring, per
    ``capabilities.py``'s own module docstring). Checked against every
    layer's copper (a mechanical hole passes through the whole board, so
    the conservative check is against all of it, same "unknown ⇒
    conservative" instinct as the board-edge V-cut default)."""
    field = "npth_annular_ring_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    if jlc_min is None:
        return []
    holes = [d for d in (model.get("drills") or []) if not d.get("plated", True)]
    if not holes:
        return []
    prims = _copper_primitives(model)
    findings: list[DrcFinding] = []
    for h in holes:
        hx, hy = float(h["x"]), float(h["y"])
        hr = float(h.get("dia_mm", 0.0)) / 2.0
        best = math.inf
        nearest: _Prim | None = None
        for p in prims:
            d = (
                _dist(p.a, (hx, hy))
                if p.b is None
                else dist_point_to_segment((hx, hy), p.a, p.b)
            )
            if d - hr - p.r < best:
                best, nearest = d - hr - p.r, p
        if best is math.inf:
            continue
        result = _two_tier(best, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        findings.append(
            DrcFinding(
                rule="npth_clearance",
                severity=severity,
                where=f"NPTH @ ({hx}, {hy})",
                detail=_margin_detail(
                    "NPTH copper clearance", best, capability, field, severity, margin
                ),
                objects=(
                    {
                        "hole_x": hx,
                        "hole_y": hy,
                        # The nearest copper item is the one that violates
                        # (it is the minimum), so name it: a strip pass
                        # otherwise cannot tell whose copper sits on the hole.
                        **(
                            {
                                "net": nearest.net,
                                **_derived_tag(model["copper"][nearest.group]),
                            }
                            if nearest is not None
                            else {}
                        ),
                    },
                ),
                margin_mm=margin,
            )
        )
    return findings


# ── via-to-pad keep-out (a via is a drilled hole, not a trace) ──────────


def check_via_pad_keepout(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    """A via must clear every pad — including a pad on its OWN net — by
    the fab's minimum copper isolation. Closest analogue is
    :func:`check_npth_clearance` (drill-versus-copper, same closed-form
    circle math, no shapely): a via is likewise a drilled hole, just a
    plated one, and this rule is that same "hole must clear copper"
    question asked of vias against pads specifically.

    **Why this cannot be expressed by tightening :func:`check_clearance`.**
    That rule deliberately EXEMPTS same-net copper (module docstring's
    two-tier section) — correctly: same-net copper touching is how a
    trace joins a pad. A via is not a trace. It is a hole drilled through
    the board; landing its annulus ON a pad — even the via's own net's
    pad — wicks solder down the barrel and starves the joint, or on a
    through-hole pad drills a second hole through the first. Widening the
    clearance exemption to cover this would make the wrong case legal
    (same-net trace-into-pad is fine; same-net via-onto-pad is not), so
    the two questions need two rules over the same geometry — the same
    relationship :func:`check_connectivity` has to :func:`check_clearance`:
    a different question, not a variant of the same one.

    **A same-net FIXED (authored) via is exempted** (docs/backlog/
    pcb-pre-place-route-blocks.md geometry residues) — a NARROWER
    carve-out than the paragraph above argues against, and deliberately
    scoped to ``item.get("fixed")`` (:meth:`precis.store._pcb_ops.
    PcbMixin.pcb_fixed_copper_list`'s own "authored rows carry
    ``fixed: True```, absent/``False`` on derived ones" marker) so it
    NEVER reaches a router-PLACED via: that argument above is about a via
    genuinely landing ON a pad's solder land, which stays an error
    regardless of net for anything the ROUTER puts there (see
    :func:`check_via_via_keepout`'s own net-blind stance, unaffected by
    this, and ``tests/test_pcb_drc.py::
    test_check_via_pad_keepout_fires_on_a_real_realized_via``, which stays
    red on a router-placed via-in-pad). What actually reached this rule
    is a plaza via against its OWN net's electrode BODY — an AUTHORED
    fixed-copper row, that electrode's own designed drop point, placed
    just outside the body by :mod:`precis.pcb.generators`'s own derived
    spacing (:func:`~precis.pcb.generators._plaza_capacity`) — and the
    circumscribed-circle pad approximation this rule used to use (not the
    electrode's real crenellated/chamfered outline) over-stated how close
    the body actually reaches there, a false positive on real geometry,
    not a wicking risk on it (this is what gr346004 later confirmed by
    arithmetic on prod ``ewod-dogfood-2``, and what this rule's geometry —
    now read off the pad's real polygon, see just below — no longer does).
    This carve-out stays regardless: it is a narrower, net-scoped policy
    call about an authored drop point, not a stand-in for the geometry fix.
    A FOREIGN net's via, or an ordinary (non-fixed) via on the SAME net, is
    unaffected: only ``item["fixed"] and via_net == pad_net`` (both
    non-empty) skips.

    The margin is DERIVED, not invented: ``trace_spacing_mm`` is already
    this project's figure for how close two independent copper features
    may legally sit (module docstring, and :func:`check_clearance`'s own
    field) — a via's plated annulus and a pad's solder land are
    independent features by this rule's whole premise, so the same jlc_min
    floor applies, net or no net. Severity is always ``error`` (no warn
    tier): a via drilled into a land is a manufacturing defect, not a
    margin the house_default tier would grade.

    **A drilled (through-hole) pad is never excluded by the via's own
    layer span** (gr341516) — same reasoning as
    :func:`clearance_pairs_indexed`'s drilled-pad handling: its land is
    real copper on every board layer, so a via landing near it is a
    keepout violation regardless of which particular layers the via
    itself spans.
    """
    field = "trace_spacing_mm"
    required = capability.jlc_min[field]
    if required is None:
        return []
    all_layers = list(model.get("layers") or [])
    pads = model.get("pads") or []
    findings: list[DrcFinding] = []

    # Pad polygons built ONCE, then a per-layer STRtree over them — the same
    # shape :func:`clearance_pairs_indexed` already uses (gr458087). This
    # rule used to rebuild every pad's polygon inside the per-via loop and
    # test every via against every pad, measured at 1.9 s of a 1.9 s
    # :func:`run_geometric_drc` on ONE 8x8 EWOD tile and 30 s at four tiles,
    # which is what made "run geometric DRC at every board mutation"
    # (docs/backlog/pcb-always-valid-board-invariant.md) unaffordable.
    #
    # The index is a PRUNE, not a different question: this rule's own
    # condition ``gap < required`` with ``gap = pad_poly.distance(centre) -
    # vr`` is exactly ``pad_poly.distance(centre) < required + vr``, so a
    # ``dwithin`` query at that distance can only drop pads the linear scan
    # would have cleared. Candidates are re-sorted into pad order before the
    # per-pad body runs, so the findings list stays identical — same
    # elements, same order — to the unindexed version.
    pad_polys: list[BaseGeometry | None] = []
    for pad in pads:
        poly = _copper_item_polygon({**pad, "ctype": "pad"})
        pad_polys.append(None if poly is None or poly.is_empty else poly)

    # A drilled pad's land is copper on EVERY layer, so it stays a candidate
    # whatever the via spans (gr341516). Indexed in its own tree rather than
    # duplicated into each layer's, so it is still reached by a via whose
    # layer span is empty — which the linear scan also checked, since its
    # ``not pad.get("drill")`` guard let drilled pads through the layer test.
    # Degenerate pads (no shapeable polygon) keep the circumscribed-circle
    # fallback below, so they are carried as unconditional candidates.
    by_layer: dict[str, list[int]] = {}
    drilled: list[int] = []
    degenerate: list[int] = []
    for idx, pad in enumerate(pads):
        if pad_polys[idx] is None:
            degenerate.append(idx)
        elif pad.get("drill"):
            drilled.append(idx)
        else:
            by_layer.setdefault(str(pad.get("layer", "")), []).append(idx)
    layer_trees = {
        layer: (idxs, STRtree([pad_polys[i] for i in idxs]))
        for layer, idxs in by_layer.items()
    }
    drilled_tree = STRtree([pad_polys[i] for i in drilled]) if drilled else None

    for item in model.get("copper") or []:
        if item.get("ctype") != "via":
            continue
        vx, vy = float(item["x"]), float(item["y"])
        vr = float(item.get("dia_mm", 0.0)) / 2.0
        via_net = item.get("net")
        via_fixed = bool(item.get("fixed"))
        via_layers = set(_via_layer_names(item, all_layers))
        vx_y = Point(vx, vy)
        reach = required + vr
        cand: set[int] = set(degenerate)
        for layer in via_layers:
            entry = layer_trees.get(layer)
            if entry is None:
                continue
            layer_idxs, layer_tree = entry
            for c in layer_tree.query(vx_y, predicate="dwithin", distance=reach):
                cand.add(layer_idxs[int(c)])
        if drilled_tree is not None:
            for c in drilled_tree.query(vx_y, predicate="dwithin", distance=reach):
                cand.add(drilled[int(c)])
        for pad_idx in sorted(cand):
            pad = pads[pad_idx]
            if pad.get("layer") not in via_layers and not pad.get("drill"):
                continue
            pad_net_raw = pad.get("net")
            if via_fixed and via_net and pad_net_raw and via_net == pad_net_raw:
                continue
            pad_poly = pad_polys[pad_idx]
            if pad_poly is not None:
                # The pad's REAL outline (:func:`_copper_item_polygon` — the
                # module docstring's ONE shape function, already the source
                # of truth for a pad's physical reach in
                # :func:`check_clearance`), not a circumscribed circle
                # (gr346004): a non-square pad's circumscribed disc reaches
                # well past its real land, so a via that clears the actual
                # copper by a wide margin still read as a keepout violation
                # against ground that, on the real board, is not there. A
                # via is itself round, so its own shape stays a disc — only
                # the PAD side of this pair gets the true-shape upgrade;
                # ``gap`` is the exact edge-to-edge distance (0 the instant
                # the via's centre enters the pad), same contract the old
                # circle/circle formula kept.
                gap = pad_poly.distance(vx_y) - vr
            else:
                px, py = float(pad["x"]), float(pad["y"])
                w = float(pad.get("w", 0.0))
                h = float(pad.get("h", w))
                # Degenerate pad geometry only (zero-size, or a malformed
                # polygon with < 3 vertices) — the same circumscribed-circle
                # fallback this rule always used, kept here only so a pad
                # that :func:`_copper_item_polygon` cannot shape at all
                # still gets SOME check rather than none.
                pr = max(w, h) / 2.0
                gap = _dist((vx, vy), (px, py)) - vr - pr
            if gap >= required - _EPS:
                continue
            pad_net, pad_layer = pad.get("net"), pad.get("layer")
            # Name the pad by its PART, not only by its net (gr451052).
            # A net name is not a location: a net with several members has
            # a pad per member, in different places, and reporting only the
            # net invites reading `pad[ARR1_R3C4]` as "the R3C4 electrode"
            # when it is the driver IC's own land that happens to sit on
            # that electrode's channel. That misreading cost a whole
            # investigation, which concluded the check was broken when it
            # was right. `pads_for_ir` already puts `refdes`/`pin` on every
            # pad, synthesized ones included, so this is free.
            pad_refdes, pad_pin = pad.get("refdes"), pad.get("pin")
            pad_id = (
                f"{pad_refdes}/{pad_pin} (net {pad_net})"
                if pad_refdes and pad_pin
                else f"pad[{pad_net}]"
            )
            findings.append(
                DrcFinding(
                    rule="via_pad_keepout",
                    severity="error",
                    where=(
                        f"via[{via_net}] @ ({vx}, {vy}) <-> {pad_id} on {pad_layer}"
                    ),
                    detail=(
                        f"via clears {pad_id} by {gap:.3f}mm, needs "
                        f"{required:.3f}mm (JLC min {field}, "
                        f"{capability.process}) — a via drilled into a solder "
                        "land starves the joint regardless of net"
                    ),
                    objects=(
                        {
                            "via_net": via_net,
                            **({"via_derived": True} if item.get("derived") else {}),
                            **(
                                {"via_fixed_id": item["fixed_id"]}
                                if item.get("fixed_id") is not None
                                else {}
                            ),
                            "via_x": vx,
                            "via_y": vy,
                            "pad_net": pad_net,
                            "pad_refdes": pad_refdes,
                            "pad_pin": pad_pin,
                            "pad_layer": pad_layer,
                        },
                    ),
                    margin_mm=gap - required,
                )
            )
    return findings


# ── via-to-via keep-out (two drilled holes, not a clearance pair) ───────


def check_via_via_keepout(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    """Two vias must not overlap — COPPER (the plated barrel) and the
    DRILLED HOLE alike, net-blind, same tier of defect as
    :func:`check_via_pad_keepout` (that function's own docstring already
    makes the argument this one inherits: :func:`check_clearance`
    deliberately exempts same-net copper because a trace legally lands on
    its own pad, and that exemption never covered a via — a via is a hole
    drilled through the board, not a trace, and this is that same
    "hole must clear copper" question restricted to another via's barrel
    and, more fundamentally, its DRILL).

    **Two distinct geometries, two distinct margins:**

    - **copper-to-copper** (barrel annulus vs. barrel annulus): the SAME
      ``trace_spacing_mm`` jlc_min :func:`check_via_pad_keepout` already
      reads for a via's annulus against independent copper — two via
      barrels are exactly that, independent copper features, net or no
      net.
    - **hole-to-hole** (drilled circle vs. drilled circle): this
      capability table publishes no hole-to-hole spacing figure, and none
      is invented here (``capabilities.py``'s own "never carry a figure
      across" discipline, and this codebase's live-JLC-page verification
      standard for every OTHER figure in that table — a number this
      module cannot check against a live page has no business claiming
      that provenance). The threshold used instead is the physical
      definition of two DISTINCT holes: their drilled circles must not
      intersect (required = 0, i.e. centre distance >= the sum of the two
      drill radii). Two holes that overlap at all are, physically,
      already one larger hole or an unintended slot — true regardless of
      any fab's published tolerance, so no published figure is needed to
      state it. This is the SAME "categorical, not a manufacturability
      margin" treatment :func:`check_courtyard_overlap` already gives
      physical overlap; there is no ``house_default`` tier for either.

    **A correctly-spread stitched group stays quiet.**
    :mod:`precis.pcb.realize` (``_route_pass``) spreads a same-net
    ampacity-sized via group along a pitch of ``via_dia_mm +
    clearance_mm`` — a copper gap of exactly ``clearance_mm`` (the
    resolved net/house clearance, which is always at or above this rule's
    ``trace_spacing_mm`` floor by construction) and a hole gap of
    ``via_dia_mm + clearance_mm - drill_mm`` (strictly positive, since a
    via's annular ring — ``(dia_mm - drill_mm) / 2`` — is itself positive
    by construction): neither margin trips for a group spread by that
    code path.

    **Two vias at the EXACT SAME coordinate, on purpose, is a DIFFERENT
    construct this rule cannot tell apart from the defect it exists to
    catch.** Nothing in the realized copper model marks "this pair is one
    deliberate stack" — same net, same net, same (x, y) is indistinguishable
    from a real duplicate-via bug (two placement/route passes silently
    emitting the same via twice), and this codebase's own discipline is to
    report a real geometric collision rather than silently assume intent
    (module docstring: a spatial-index bug that silently misses a
    neighbour "produces a clean DRC pass on a shorted board" — inventing a
    same-coordinate exemption here is the identical failure mode aimed at
    a different rule). Stated here, not guessed past: if this codebase
    grows an intentional same-spot via-stack construct, it needs its own
    marker in the model for this rule to key off, not a same-net/
    same-coordinate heuristic.

    Severity is always ``error`` — like :func:`check_via_pad_keepout`, a
    categorical manufacturing defect, not a margin the ``house_default``
    tier would grade."""
    field = "trace_spacing_mm"
    copper_required = capability.jlc_min[field]
    if copper_required is None:
        return []
    all_layers = list(model.get("layers") or [])
    vias = [item for item in model.get("copper") or [] if item.get("ctype") == "via"]
    findings: list[DrcFinding] = []
    for a_i, via_a in enumerate(vias):
        ax, ay = float(via_a["x"]), float(via_a["y"])
        a_dia = float(via_a.get("dia_mm", 0.0))
        a_drill = float(via_a.get("drill_mm", 0.0))
        a_net = via_a.get("net")
        a_layers = set(_via_layer_names(via_a, all_layers))
        for b_i in range(a_i + 1, len(vias)):
            via_b = vias[b_i]
            b_layers = set(_via_layer_names(via_b, all_layers))
            if not (a_layers & b_layers):
                continue  # no shared copper layer -- not the same physical barrel
            bx, by = float(via_b["x"]), float(via_b["y"])
            b_dia = float(via_b.get("dia_mm", 0.0))
            b_drill = float(via_b.get("drill_mm", 0.0))
            b_net = via_b.get("net")
            center = _dist((ax, ay), (bx, by))
            where = f"via[{a_net}] @ ({ax}, {ay}) <-> via[{b_net}] @ ({bx}, {by})"
            objects = (
                {
                    "a_net": a_net,
                    "a_x": ax,
                    "a_y": ay,
                    "b_net": b_net,
                    "b_x": bx,
                    "b_y": by,
                    **({"a_derived": True} if via_a.get("derived") else {}),
                    **({"b_derived": True} if via_b.get("derived") else {}),
                },
            )

            copper_gap = center - (a_dia / 2.0 + b_dia / 2.0)
            if copper_gap < copper_required - _EPS:
                findings.append(
                    DrcFinding(
                        rule="via_via_keepout",
                        severity="error",
                        where=where,
                        detail=(
                            f"via barrels clear each other by {copper_gap:.3f}mm, "
                            f"needs {copper_required:.3f}mm (JLC min {field}, "
                            f"{capability.process}) — independent via copper "
                            "regardless of net"
                        ),
                        objects=objects,
                        margin_mm=copper_gap - copper_required,
                    )
                )

            hole_gap = center - (a_drill / 2.0 + b_drill / 2.0)
            if hole_gap < -_EPS:
                findings.append(
                    DrcFinding(
                        rule="via_via_keepout",
                        severity="error",
                        where=where,
                        detail=(
                            f"drilled holes overlap by {abs(hole_gap):.3f}mm "
                            f"(centres {center:.3f}mm apart, drills "
                            f"{a_drill:.3f}mm/{b_drill:.3f}mm) — two distinct "
                            "holes cannot occupy the same space; a broken bit "
                            "or an unintended slot, regardless of net"
                        ),
                        objects=objects,
                        margin_mm=hole_gap,
                    )
                )
    return findings


# ── synthesized footprint (gripe gr341532) ───────────────────────────────


def check_synthesized_footprint(model: dict[str, Any]) -> list[DrcFinding]:
    """One finding per REFDES that is a real catalog part (a ``part_lcsc``
    C-number, carried on the pad since gr341532's scoping fix — see
    :func:`precis.pcb.realize.pads_for_ir`) with any ``synthesized=True``
    pad — a catalog part with no cached footprint, silently DRC'd at
    :mod:`precis.pcb.landpattern`'s fabricated ``_quad``/``_dual_row``/etc.
    bound instead of its real geometry (:func:`precis.pcb.session.
    apply_real_pin_offsets` no-ops for that instance; every pin of it then
    falls back through :func:`precis.pcb.landpattern.offsets_for`).

    **Scoped to catalog parts only.** A design-local/hand-authored pad
    (a generator's own copper, a mounting hole, a wire) is ALSO
    ``synthesized=True`` — :mod:`precis.pcb.landpattern` sizes it the same
    way — but it has no LCSC C-number and no cache to ever fill, so it can
    never stop being "synthesized"; flagging it produced a permanent,
    unfixable finding on every generic/generated board (the ESP32C3
    reference fixture, every EWOD array) with nothing to cache toward.
    Only a pad carrying ``part_lcsc`` (real catalog part, cache genuinely
    empty) is actionable, so only those pads count here.

    Before this rule, that fallback was silent everywhere ``view='drc'``
    could see: :func:`precis.pcb.realize.pads_for_ir` sets
    ``pad["synthesized"]`` but nothing in this module read it, so a whole
    part placed at a fabricated bound produced clearance/connectivity
    findings that READ as real shorts — on the prod dogfood board, 196 of
    them, for one uncached LCSC part. :func:`precis.pcb.gerber.export_fab`
    already refuses a ``view='gerber'`` export over synthesized geometry
    (:class:`~precis.pcb.gerber.SynthesizedPadError`); this is that same
    refusal's ``view='drc'`` counterpart — not a refusal (a DRC view
    reports, it does not block), but a finding loud enough that the
    findings list around it is read correctly: every other rule's verdict
    on this refdes is against a guess, not a measurement.

    Severity is always ``error`` — a synthesized bound is not a
    manufacturability margin to grade, it is an instruction to go cache
    the part's real footprint before trusting anything else this run said
    about it.

    **A pad marked ``pin_unmatched`` is a different defect** and gets its
    own ``pin_name_mismatch`` finding: the part's footprint IS cached, but
    the declared pin name matches none of its pads, so caching cannot fix
    it — renaming the pin can. Reporting it as "no real footprint" sent
    the EWOD sink's 56 misnamed pins (``OUT0`` vs the part's ``HVOUT1``)
    looking for a cache row that already existed. ``view='footprints'``
    names the footprint's own pins beside the declared ones."""
    by_refdes: dict[str, int] = {}
    unmatched: dict[str, list[str]] = {}
    for pad in model.get("pads") or []:
        refdes = str(pad.get("refdes") or "")
        if not refdes or not pad.get("synthesized"):
            continue
        if pad.get("pin_unmatched"):
            names = unmatched.setdefault(refdes, [])
            if str(pad.get("pin")) not in names:
                names.append(str(pad.get("pin")))
        elif pad.get("part_lcsc"):
            by_refdes[refdes] = by_refdes.get(refdes, 0) + 1
    findings: list[DrcFinding] = []
    for refdes in sorted(unmatched):
        names = unmatched[refdes]
        shown = ", ".join(names[:8]) + (" …" if len(names) > 8 else "")
        findings.append(
            DrcFinding(
                rule="pin_name_mismatch",
                severity="error",
                where=f"part {refdes}",
                detail=(
                    f"{len(names)} declared pin name(s) of {refdes} match no pad "
                    f"on its cached footprint ({shown}) — checked at a "
                    "synthesized bound; view='footprints' lists the "
                    "footprint's own names"
                ),
                objects=({"refdes": refdes, "pins": tuple(names)},),
            )
        )
    for refdes in sorted(by_refdes):
        n = by_refdes[refdes]
        findings.append(
            DrcFinding(
                rule="synthesized_footprint",
                severity="error",
                where=f"part {refdes}",
                detail=(
                    f"{n} pin(s) of {refdes} have no real footprint — checked "
                    "at a synthesized bound; DRC on this part is not a verdict"
                ),
                objects=({"refdes": refdes, "n_pins": n},),
            )
        )
    return findings


# ── courtyard overlap ──────────────────────────────────────────────────


#: One instance's courtyard as DRC receives it: refdes plus the polygon in
#: BOARD coordinates. Was ``(refdes, x, y, radius_mm)`` until 2026-08-30 —
#: a circle could not stand in for the real shape, over-reserving an edge
#: connector eightfold while UNDER-reserving a SOIC-8, so a rule built on
#: it enforced a boundary that was not the one the placer respected or the
#: one the silkscreen showed. See ``docs/backlog/pcb-courtyard-polygon.md``.
Courtyard = tuple[str, list[tuple[float, float]]]
#: ``(label, keep-out polygon, owner refdes or None)`` for
#: :func:`check_courtyard_hole`.
HoleKeepout = tuple[str, list[tuple[float, float]], str | None]


def check_courtyard_overlap(
    courtyards: list[Courtyard],
    *,
    bottom_by_refdes: dict[str, bool] | None = None,
    clearance_mm: float | None = None,
    pads: Sequence[dict[str, Any]] | None = None,
) -> list[DrcFinding]:
    """Two parts' courtyards overlapping is an ERROR when their land
    patterns themselves collide, and a WARNING when only the placer's
    routing-corridor margin is short.

    ``pads`` (``model["pads"]``, each carrying ``refdes``) refines the
    error tier further: a courtyard is the CONVEX hull of a part's pads,
    an over-approximation of what the part occupies, so two hulls
    intersecting proves nothing about the lands (docs/backlog/
    pcb-lazy-netlist-and-checks.md §1g-bis — an over-approximation is
    sound for proving a collision ABSENT, not present). With pads given,
    the error is kept only when a real land of one part intersects a real
    land of the other; hulls that overlap with every land clear are a
    warning naming the land gap (taz15's L2 inductor pad reaching into
    the TO-263's hull between its leads, 1.06 mm "deep" by hull, 0 mm by
    land). Bodies are not modelled; that is said in the finding.

    ``clearance_mm`` is the offset the caller inflated each courtyard by
    (:data:`precis.pcb.ir.COURTYARD_CLEARANCE_MM` for every placer/DRC
    caller). Given, the rule deflates both polygons by it to recover the
    pad hulls and grades the pair on THOSE: hulls that intersect are two
    parts physically on top of each other (error); hulls that clear each
    other are two parts closer than the placer's reservation, which is a
    tuning pin for the router's escape corridor (that constant's own
    docstring: "a pin, not an optimum"), not a fab figure. taz15 (2026-10-15)
    is the case: the manufactured source board has 30 neighbour pairs with
    land gaps of 0.58–0.79 mm against a 2 x 0.40 mm reservation, and the
    categorical error refused every route of a board that was built and
    works. Different-net lands closer than the fab floor are
    :func:`check_clearance`'s finding, not this rule's. Left ``None``, every
    overlap is the categorical error it always was (the courtyards' own
    offset is unknown, so nothing can be deflated).

    The polygons are :func:`precis.pcb.ir.instance_courtyard_polygon`
    placed into board coordinates — **the same objects the placer reserves
    and the silkscreen draws.** That identity is the point of this
    signature: DRC on circles while the placer went polygon would recreate
    exactly the drift the courtyard work exists to remove, with the added
    trap that the drift only shows on parts whose aspect ratio is far from
    square.

    Reported with two numbers rather than one, because a polygon overlap
    has no single "depth": the intersection AREA (exact, unambiguous) in
    the detail, and ``margin_mm`` as the negative of the overlap's
    depth — the shorter side of the overlap region
    (:func:`_overlap_depth_mm`), i.e. how far the parts must move apart
    along the easier axis to separate. A pair that overlaps only near a corner and a pair
    that is half-buried report very different second numbers, which is the
    thing a reader wants and a bare flag cannot give.

    ``bottom_by_refdes`` (gr341516, refdes -> "is this instance mounted on
    the bottom of the board") is optional — ``None`` (the default) keeps
    every existing caller's behaviour exactly as before: two 2-D polygons
    overlapping is an overlap regardless of side, the reading that was
    correct back when every part was implicitly top-side. Supplied, a pair
    on OPPOSITE sides is never flagged — a bottom-side sink placed directly
    beneath a top-side array (the physically correct, intentional layout
    for e.g. an EWOD sink grid) is not two parts colliding, it is two parts
    facing each other across the board's own thickness. A refdes absent
    from the map (an instance the caller never resolved a side for)
    defaults to top, same as :func:`precis.pcb.padplace.is_bottom_instance`
    itself defaults an unset ``layer`` to top."""
    if len(courtyards) < 2:
        return []
    geoms = [Polygon(poly) if len(poly) >= 3 else None for _, poly in courtyards]
    indexed = [(i, g) for i, g in enumerate(geoms) if g is not None and not g.is_empty]
    if len(indexed) < 2:
        return []
    tree = STRtree([g for _, g in indexed])
    lands = _land_polygons_by_refdes(pads)
    findings: list[DrcFinding] = []
    seen: set[tuple[int, int]] = set()
    for pos, (i, gi) in enumerate(indexed):
        for c in tree.query(gi, predicate="intersects"):
            if int(c) == pos:
                continue
            j, gj = indexed[int(c)]
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            seen.add(key)
            if bottom_by_refdes is not None:
                bottom_i = bottom_by_refdes.get(courtyards[i][0], False)
                bottom_j = bottom_by_refdes.get(courtyards[j][0], False)
                if bottom_i != bottom_j:
                    # Opposite sides of the board -- physically facing
                    # each other, not colliding (docstring's sink-under-
                    # array example).
                    continue
            overlap = gi.intersection(gj)
            if overlap.is_empty or overlap.area <= _EPS:
                # A shared edge or a single touching vertex: STRtree's
                # `intersects` counts it, zero area means it is not one.
                continue
            severity = "error"
            detail = (
                f"{courtyards[i][0]} and {courtyards[j][0]} courtyards "
                f"overlap over {overlap.area:.4f}mm^2, "
                f"{_overlap_depth_mm(overlap):.3f}mm deep"
            )
            # Only a courtyard built from real lands can be deflated back
            # to them: with `pads` supplied (non-empty), a part that has no land in it
            # (a pinless mounting hole or fiducial, whose courtyard is the
            # caller's bare fallback square, never inflated) keeps the
            # categorical reading rather than a shrunken square's.
            deflatable = not pads or (
                bool(lands.get(courtyards[i][0])) and bool(lands.get(courtyards[j][0]))
            )
            land_gap = _land_hull_gap_mm(gi, gj, clearance_mm) if deflatable else None
            if land_gap is not None and land_gap > _EPS:
                severity = "warn"
                detail += (
                    f" — the land patterns themselves clear by {land_gap:.3f}mm; "
                    f"short of the {clearance_mm:.2f}mm courtyard margin the "
                    "placer reserves for routing, not of a fab floor"
                )
            elif land_gap is not None:
                pad_gap = _pad_gap_mm(
                    lands.get(courtyards[i][0]), lands.get(courtyards[j][0])
                )
                if pad_gap is not None and pad_gap > _EPS:
                    severity = "warn"
                    detail += (
                        f" — the pad hulls overlap but no land touches another "
                        f"(nearest lands {pad_gap:.3f}mm apart); the hull is a "
                        "convex over-approximation and part bodies are not "
                        "modelled, so this is not a proven collision"
                    )
            findings.append(
                DrcFinding(
                    rule="courtyard_overlap",
                    severity=severity,
                    where=f"{courtyards[i][0]} <-> {courtyards[j][0]}",
                    detail=detail,
                    objects=({"a": courtyards[i][0], "b": courtyards[j][0]},),
                    margin_mm=-_overlap_depth_mm(overlap),
                )
            )
    return findings


def _land_hull_gap_mm(
    gi: Polygon, gj: Polygon, clearance_mm: float | None
) -> float | None:
    """The edge-to-edge gap between two courtyards' LAND hulls — each
    polygon deflated by the ``clearance_mm`` it was inflated by (mitred,
    the inverse of :func:`precis.pcb.ir.instance_courtyard_polygon`'s
    offset). ``None`` when no clearance is known or a deflated hull
    vanishes (a pinless fallback square smaller than the offset): the
    caller then keeps the categorical reading. ``0.0`` when the hulls
    touch or overlap."""
    if clearance_mm is None or clearance_mm <= 0:
        return None
    hi = gi.buffer(-clearance_mm, join_style="mitre", mitre_limit=COURTYARD_MITRE_LIMIT)
    hj = gj.buffer(-clearance_mm, join_style="mitre", mitre_limit=COURTYARD_MITRE_LIMIT)
    if hi.is_empty or hj.is_empty:
        return None
    return float(hi.distance(hj))


def _land_polygons_by_refdes(
    pads: Sequence[dict[str, Any]] | None,
) -> dict[str, list[BaseGeometry]]:
    """Each part's real land polygons (:func:`_copper_item_polygon` over
    its ``model["pads"]`` rows), keyed by refdes — the exact geometry
    :func:`check_courtyard_overlap` grades a hull overlap against."""
    out: dict[str, list[BaseGeometry]] = {}
    for pad in pads or ():
        refdes = pad.get("refdes")
        if not refdes:
            continue
        poly = _copper_item_polygon({**pad, "ctype": "pad"})
        if poly is not None and not poly.is_empty:
            out.setdefault(str(refdes), []).append(poly)
    return out


def _pad_gap_mm(
    a: list[BaseGeometry] | None, b: list[BaseGeometry] | None
) -> float | None:
    """The smallest edge-to-edge distance between any land of ``a`` and
    any land of ``b`` — ``0.0`` when two lands touch or overlap, ``None``
    when either part has no land geometry to judge by."""
    if not a or not b:
        return None
    return min(float(pa.distance(pb)) for pa in a for pb in b)


def check_courtyard_hole(
    courtyards: list[Courtyard], holes: Sequence[HoleKeepout]
) -> list[DrcFinding]:
    """A part's courtyard overlapping a mounting hole's keep-out is a hard
    error, on either side of the board — the hole and its hardware go
    through it. ``holes`` are ``(label, polygon, owner)`` triples, the
    polygon built by
    :func:`precis.pcb.optimize.mounting_hole_keepout_polygon`, the shape the
    placer's ``_placement_is_legal`` already rejects against, so the placer
    and DRC hold the same rule. Without this a part whose courtyard covers a
    plated solder-nut hole while its pads miss it was DRC-clean:
    :func:`check_npth_clearance` tests copper, and only against unplated
    holes. ``owner`` is the refdes whose footprint the hole is cut from
    (:attr:`precis.pcb.ir.MountingHole.part`): that part's own courtyard
    covers it by design and is skipped; every other part is still
    checked."""
    hole_geoms = [
        (label, Polygon(poly), owner) for label, poly, owner in holes if len(poly) >= 3
    ]
    if not hole_geoms or not courtyards:
        return []
    tree = STRtree([g for _, g, _ in hole_geoms])
    findings: list[DrcFinding] = []
    for refdes, poly in courtyards:
        if len(poly) < 3:
            continue
        gc = Polygon(poly)
        for c in tree.query(gc, predicate="intersects"):
            label, gh, owner = hole_geoms[int(c)]
            if owner is not None and owner == refdes:
                continue
            overlap = gc.intersection(gh)
            if overlap.is_empty or overlap.area <= _EPS:
                continue
            depth = _overlap_depth_mm(overlap)
            findings.append(
                DrcFinding(
                    rule="courtyard_hole",
                    severity="error",
                    where=f"{refdes} <-> {label}",
                    detail=(
                        f"{refdes} courtyard overlaps the {label} keep-out "
                        f"over {overlap.area:.4f}mm^2, {depth:.3f}mm deep"
                    ),
                    objects=({"a": refdes, "b": label},),
                    margin_mm=-depth,
                )
            )
    return findings


def _overlap_depth_mm(overlap: BaseGeometry) -> float:
    """How far two courtyards have to move apart to separate, along the
    easier axis — the SHORTER side of the overlap region's bounding box.

    Not the minimum translation distance (the textbook penetration depth,
    which needs a full Minkowski difference), and deliberately not "how
    far is the deepest vertex buried", which was the first cut here and
    returned **zero** for the commonest case there is: two axis-aligned
    courtyards overlapping in a band, where every vertex of one lies
    exactly ON the other's edge and shapely's ``contains`` — correctly —
    excludes a boundary point. A depth measure that reads 0.000mm on a
    real 1mm² overlap is worse than no number, because it looks like a
    near-miss.

    The shorter bbox side has none of that fragility and keeps the
    intuition a reader needs: a wide shallow band and a deep narrow one
    report differently, and the number falls to zero only when the
    overlap really is degenerate.

    **It is a LOWER BOUND, exact only when both courtyards share an
    axis-aligned orientation** — which is the common case, and the case
    the constant was chosen for. Two long thin parts rotated to +45 and
    -45 degrees and crossing near their centres measure ~0.85mm by this
    reading while genuinely needing ~14mm of travel to separate: the
    intersection's bbox is a small diamond, but backing either part out
    means sliding it the length of the other. The overlap AREA in the
    same finding does not have that blind spot, which is why the detail
    carries both numbers and this one is never the whole report."""
    if overlap.is_empty:
        return 0.0
    x0, y0, x1, y1 = overlap.bounds
    return min(x1 - x0, y1 - y0)


# ── board-edge clearance ───────────────────────────────────────────────


def check_board_edge_clearance(
    model: dict[str, Any],
    capability: CapabilityRow,
    *,
    outline: list[list[float]] | None,
    panel_type: str | None = None,
) -> list[DrcFinding]:
    """Copper-to-board-edge clearance. ``board_edge_clearance_mm`` is TWO
    fields (module docstring) — ``panel_type='routed'`` selects the
    mechanically-routed-edge figure; anything else (including ``None``,
    the "don't know yet" default) selects V-cut, the conservative one.

    **Pads used to be invisible here** — this rule iterated
    ``model["copper"]`` only, so a pad (a separate top-level model key) was
    never a candidate: a pad sitting inside the outline but nearer the edge
    than the fab can manufacture went unreported, on a board whose pads are
    exactly the copper you solder to. Fixed by reading
    :func:`_clearance_items` (copper items followed by ``model["pads"]``,
    :func:`_copper_item_polygon`'s exact ``ctype == "pad"`` branch) instead
    of ``model["copper"]`` alone — the same fix already made to
    :func:`clearance_pairs_indexed`, for the same reason. This is a
    DIFFERENT question from :func:`check_outline_containment` (that
    function's own docstring): containment asks whether a pad is on the
    board at all (binary, no margin); this rule asks whether a pad that IS
    on the board sits too near the edge to manufacture (two-tier margin).
    A pad can fail one, the other, both, or neither."""
    field = (
        "board_edge_clearance_routed_mm"
        if panel_type == "routed"
        else "board_edge_clearance_vcut_mm"
    )
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    if jlc_min is None or not outline or len(outline) < 3:
        return []
    ring_pts = [(float(p[0]), float(p[1])) for p in outline]
    if ring_pts[0] != ring_pts[-1]:
        ring_pts.append(ring_pts[0])
    boundary = LineString(ring_pts)
    findings: list[DrcFinding] = []
    for item in _clearance_items(model):
        geom = _copper_item_polygon(item)
        if geom is None or geom.is_empty:
            continue
        gap = boundary.distance(geom)
        result = _two_tier(gap, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        net, layer, ctype = item.get("net"), item.get("layer"), item.get("ctype")
        # Carry the offending item's own coordinates when it has them.
        # Without these the finding names a CLASS of object ("a via on
        # VCC3V3") and not an object, and a board carries many of those --
        # an investigation then cannot get from the finding to the geometry
        # without instrumenting a run, which is how a 10um shortfall on this
        # very rule sat unattributed. Tracks and pours have no single point
        # and simply omit them.
        obj: dict[str, Any] = {
            "net": net,
            "layer": layer,
            "ctype": ctype,
            **_derived_tag(item),
        }
        if item.get("x") is not None and item.get("y") is not None:
            obj["x"], obj["y"] = float(item["x"]), float(item["y"])
        findings.append(
            DrcFinding(
                rule="board_edge_clearance",
                severity=severity,
                where=f"{ctype}[{net}] on {layer}",
                detail=_margin_detail(
                    "board-edge clearance", gap, capability, field, severity, margin
                ),
                objects=(obj,),
                margin_mm=margin,
            )
        )
    return findings


def check_outline_containment(
    model: dict[str, Any],
    *,
    outline: list[list[float]] | None,
    courtyards: list[Courtyard] | None = None,
) -> list[DrcFinding]:
    """Copper, pads and parts must be ON the board.

    **A different question from :func:`check_board_edge_clearance`, and the
    reason that one cannot answer it.** Edge clearance measures
    ``boundary.distance(geom)`` — the distance to the outline as a *line*.
    That is unsigned, so it is symmetric about the edge: it fires on copper
    1mm inside and copper 1mm outside alike, and stays silent on copper
    20mm outside, because 20mm is not a small gap. The check is about
    proximity to a boundary and says nothing about which side of it you are
    on.

    Measured on the reference design with the outline shrunk to a board the
    parts cannot fit in: at 20mm square, 24 of 29 parts, 48 of 81 pads and
    70 copper items lay outside the board and DRC reported 10 errors. At
    2mm square — every pad outside, the whole design off the board — it
    reported **nine**. The count went DOWN as the board got more absurd,
    which is exactly what a proximity check does when the geometry walks
    away from the boundary entirely.

    There is no two-tier margin here and no capability field to read. A
    fab images what is inside the profile; copper outside it is not
    marginal, it does not exist on the delivered board. Severity is always
    ``error``.
    """
    if not outline or len(outline) < 3:
        return []
    ring = [(float(p[0]), float(p[1])) for p in outline]
    board = Polygon(ring)
    if not board.is_valid:
        board = board.buffer(0)
    if board.is_empty:
        return []

    findings: list[DrcFinding] = []

    def outside(geom: BaseGeometry, rule_where: str, obj: dict[str, Any]) -> None:
        if board.covers(geom):
            return
        # How far out, and whether any of it is on the board at all — the
        # two things a reader needs to tell "a pad hanging over the edge"
        # from "this part was never placed on the board".
        over = geom.difference(board)
        wholly = not board.intersects(geom)
        gap = board.distance(geom) if wholly else 0.0
        if not wholly:
            # A partial overhang's depth: the farthest any vertex of the
            # shape lies outside the outline (floored just above zero so a
            # finding never reads as "no shortfall").
            gap = max(
                (
                    board.distance(Point(c))
                    for g in getattr(geom, "geoms", [geom])
                    for c in getattr(g, "exterior", g).coords
                ),
                default=0.0,
            )
            gap = max(gap, 1e-6)
        detail = (
            f"{rule_where} lies {'entirely' if wholly else 'partly'} outside "
            "the board outline"
        )
        detail += (
            f" — {gap:.3f}mm beyond the edge at the nearest point"
            if wholly
            else f" — {over.area:.4f}mm² of it overhangs"
        )
        findings.append(
            DrcFinding(
                rule="outline_containment",
                severity="error",
                where=rule_where,
                detail=detail + "; a fab images only what is inside the profile",
                objects=(obj,),
                margin_mm=-gap,
            )
        )

    for item in model.get("copper") or []:
        geom = _copper_item_polygon(item)
        if geom is None or geom.is_empty:
            continue
        net, layer, ctype = item.get("net"), item.get("layer"), item.get("ctype")
        outside(
            geom,
            f"{ctype}[{net}] on {layer}",
            {"net": net, "layer": layer, "ctype": ctype, **_derived_tag(item)},
        )

    for pad in model.get("pads") or []:
        # The pad's real shape (circle/rect/obround/polygon), same as every
        # other copper item above — this loop's own circumscribed-circle
        # stand-in (gr346004 left it, deliberately, as the one remaining
        # pre-existing pad approximation) is fixed under gr450064/gr449709:
        # a non-square pad's circumscribed disc over-states its reach past
        # the real land, so a pad that cleared the outline by a wide margin
        # could still read as a containment violation against board that,
        # on the real design, it is not actually off of.
        geom = _copper_item_polygon({**pad, "ctype": "pad"})
        if geom is None or geom.is_empty:
            continue
        net, layer = pad.get("net"), pad.get("layer")
        outside(
            geom,
            f"pad[{net}] on {layer}",
            {
                "net": net,
                "layer": layer,
                "refdes": pad.get("refdes"),
                "pin": pad.get("pin"),
            },
        )

    for refdes, poly in courtyards or []:
        if len(poly) < 3:
            continue  # a pinless part the caller gave no fallback shape
        outside(Polygon(poly), f"part {refdes}", {"refdes": refdes})

    # Silkscreen ink is imaged too — a refdes label or a courtyard drawn
    # past the outline is exactly as absent from the delivered board as
    # copper would be, and until this loop existed `view='drc'` never saw
    # it: `precis.pcb.silk.build_silk` checked a candidate against pads/
    # vias/committed silk but never against the board itself (silk.py's
    # own module docstring). `_silk_item_polygon` reads the SAME
    # ``model["silkscreen"]`` shape :func:`check_silk_missing` already
    # consumes, so a caller building one model gets both checks over it
    # for free.
    for side, draws in (model.get("silkscreen") or {}).items():
        for draw in draws:
            geom = _silk_item_polygon(draw)
            if geom is None or geom.is_empty:
                continue
            role = str(draw.get("role") or "")
            refdes = str(draw.get("refdes") or "")
            outside(
                geom,
                f"silk {role}[{refdes}] on {side}",
                {"role": role, "refdes": refdes, "side": side},
            )

    return findings


def check_edge_mating(
    edge_mates: Sequence[Any] | None,
    *,
    outline: list[list[float]] | None,
) -> list[DrcFinding]:
    """A side-insertion connector must sit ON the outline, mating face out
    (gr451356) — the opposite polarity of :func:`check_board_edge_clearance`
    (copper stays AWAY from the edge) and :func:`check_outline_containment`
    (parts stay INSIDE it), neither of which can say a USB receptacle 10mm
    inside the board is unbuildable: the cable cannot reach it.

    ``edge_mates`` is :func:`precis.pcb.edge_mate.records` — one placed
    courtyard + outward direction per part that authored an ``edge_mate``;
    ``None``/empty (every board without one) and no outline are silent.
    ``require='required'`` is an ``error``, ``'preferred'`` a ``warn``.
    ``margin_mm`` is the negative distance the face sits from the edge
    (interior), or ``-EDGE_TOL`` for a part on the edge but facing in.
    """
    from precis.pcb import edge_mate as em

    edges = em.outline_edges(outline)
    if not edges or not edge_mates:
        return []
    findings: list[DrcFinding] = []
    for rec in edge_mates:
        v = em.evaluate(rec, edges)
        if v.state == "ok":
            continue
        if v.state == "interior":
            detail = (
                f"{rec.refdes} mates by side insertion but its mating face "
                f"is {v.distance_mm:.2f}mm from the nearest board edge "
                f"(allowed {em.EDGE_TOL_MM:g}mm); a cable cannot reach it"
            )
            margin = -(v.distance_mm - em.EDGE_TOL_MM)
        else:
            detail = (
                f"{rec.refdes} sits on the board edge but its mating face "
                f"points {v.angle_deg:.0f} degrees off the outward normal "
                f"(allowed {em.FACING_TOL_DEG:g}); it faces into the board"
            )
            margin = -em.EDGE_TOL_MM
        findings.append(
            DrcFinding(
                rule="edge_mating",
                severity="error" if rec.require == "required" else "warn",
                where=f"part {rec.refdes}",
                detail=detail,
                objects=({"refdes": rec.refdes, "require": rec.require},),
                margin_mm=margin,
            )
        )
    return findings


# ── silkscreen: a label/courtyard that never rendered is a DRC error ─────
#
# `precis.pcb.silk.build_silk` DROPS a refdes label, a courtyard outline or
# a pin-1 tick outright when it cannot be placed without colliding with a
# pad/via/other silk. Before this section that fact lived ONLY in
# `SilkResult.dropped`/`.relocated` -- human-readable prose nothing checked
# -- so a board could ship with unlabelled parts and read as DRC-clean.
# `SilkPlacement` (silk.py) is the structured census this reads instead of
# re-parsing that prose.


def _silk_item_polygon(item: dict[str, Any]) -> BaseGeometry | None:
    """One ``model["silkscreen"]`` draw's physical ink footprint.

    ``precis.pcb.silk._draw`` emits ``segments``/``width_mm`` in exactly
    the shape :func:`_copper_item_polygon`'s ``ctype == "track"`` branch
    already reads (a polyline buffered by its half-width, round caps) —
    so this is that branch, not a second implementation of "what shape is
    a stroke": a silk draw carries no ``ctype`` of its own, and adding one
    just to dispatch through the same function beats writing the buffer
    arithmetic twice."""
    return _copper_item_polygon({**item, "ctype": "track"})


#: A LEGIBILITY judgement, not a fab spec: below this cap height a human
#: reading an assembled board's silkscreen struggles to make a refdes out
#: at arm's length (a soldering-iron-and-tweezers distance, not a
#: magnifier). `capabilities.py`'s `silk_width_mm` is a PRINTABILITY floor
#: -- can the fab's silkscreen process resolve a line that thin at all --
#: and is checked separately, per-item, against the real fab-capability
#: table below; this number is never dressed up as one of that table's
#: entries (capabilities.py's own "checked directly against live JLCPCB
#: capability pages" standard does not, and cannot, apply to a readability
#: opinion this codebase is stating for itself).
SILK_LEGIBILITY_HEIGHT_MM = 0.8

#: `precis.pcb.silk._draw`'s own `role=` convention, inverted: which
#: `SilkPlacement.kind` a given `model["silkscreen"]` draw's `role` proves
#: was actually rendered. `"title"`/`"sn-text"`/`"sn-box"` (board-level
#: furniture -- title block, S/N patch) are deliberately absent: they carry
#: no per-instance census row to cross-check against (silk.py's own module
#: docstring: fiducials/title block are board-level, not part of the
#: per-instance loop `build_silk` returns a census for).
_ROLE_TO_SILK_KIND = {"outline": "courtyard", "pin1": "pin1", "refdes": "refdes"}


def check_silk_missing(
    census: Sequence[SilkPlacement], model: dict[str, Any]
) -> list[DrcFinding]:
    """An error per :class:`~precis.pcb.silk.SilkPlacement` the builder
    never rendered (``outcome == "dropped"``) -- a refdes nobody can read
    off the assembled board, a courtyard silently absent, or a pin-1 tick
    that never got drawn is exactly as real a defect as a clearance
    violation, and stayed invisible to every DRC run before this rule
    existed.

    **The cross-check this rule ALSO runs is not optional (task brief,
    verbatim: "this is the guard; do not skip it").** A dropped item
    reported here is a REPORTING channel — it repeats what
    :func:`~precis.pcb.silk.build_silk` already knew when it built the
    census, so a bug that makes the census claim success when nothing was
    actually drawn would sail straight through it. So this function
    independently reads ``model["silkscreen"]`` (the same
    ``{"top": [...], "bottom": [...]}`` shape :func:`build_silk` itself
    returns, and what a real gerber/SVG render is built from) and asserts
    that every census row claiming ``"placed"``/``"relocated"`` has a
    matching draw there — by ``(refdes, kind)`` identity, never a bare
    COUNT (this subsystem's own fixture-symmetry lesson: a count-based
    check cannot tell "the right N items" from "some other N items", and a
    census claiming success over an empty silk layer must produce a
    finding, not agree with it because the totals happen to match)."""
    findings: list[DrcFinding] = []
    for c in census:
        if c.outcome != "dropped":
            continue
        findings.append(
            DrcFinding(
                rule="silk_missing",
                severity="error",
                where=f"{c.refdes} ({c.kind}, {c.side})",
                detail=f"{c.kind} silk for {c.refdes} was not rendered: {c.reason}",
                objects=({"refdes": c.refdes, "kind": c.kind, "side": c.side},),
            )
        )

    silkscreen = model.get("silkscreen") or {}
    drawn: set[tuple[str, str]] = set()
    for side_draws in silkscreen.values():
        for draw in side_draws:
            kind = _ROLE_TO_SILK_KIND.get(str(draw.get("role") or ""))
            refdes = str(draw.get("refdes") or "")
            if kind is not None and refdes:
                drawn.add((refdes, kind))
    for c in census:
        if c.outcome not in ("placed", "relocated"):
            continue
        if (c.refdes, c.kind) in drawn:
            continue
        findings.append(
            DrcFinding(
                rule="silk_missing",
                severity="error",
                where=f"{c.refdes} ({c.kind}, {c.side})",
                detail=(
                    f"census claims {c.kind} silk for {c.refdes} was "
                    f"{c.outcome}, but no matching draw appears in "
                    "model['silkscreen'] -- the census does not describe the "
                    "board actually being shipped"
                ),
                objects=({"refdes": c.refdes, "kind": c.kind, "side": c.side},),
            )
        )
    return findings


def check_silk_printability(
    census: Sequence[SilkPlacement], capability: CapabilityRow
) -> list[DrcFinding]:
    """Two independent findings over the same census, both restricted to
    items that were actually drawn (``outcome in ("placed", "relocated")``
    -- a dropped item's silk never reaches the fab at all, and is already
    covered by :func:`check_silk_missing`, so checking its would-be width
    here would just be a second finding for the same one defect):

    - **error** when ``stroke_width_mm`` is below the fab's declared
      minimum printable silk width (``capabilities.py``'s
      ``silk_width_mm``, ``jlc_min`` tier) — a line the process cannot
      physically resolve, the exact gap this rule closes: ``silk_width_mm``
      was declared in :data:`precis.pcb.capabilities.FIELDS` and read by
      nothing (see that field's own docstring), while :mod:`precis.pcb.silk`
      carried its own ``stroke_width_mm`` default and never consulted it.
    - **warn** when a refdes label's ``height_mm`` is below
      :data:`SILK_LEGIBILITY_HEIGHT_MM` — a READABILITY judgement, not a
      fab limit (see that constant's own docstring for why the two must
      never be presented as the same kind of number).
    """
    findings: list[DrcFinding] = []
    min_width = capability.jlc_min.get("silk_width_mm")
    if min_width is not None:
        for c in census:
            if c.outcome not in ("placed", "relocated"):
                continue
            if c.stroke_width_mm >= min_width - _EPS:
                continue
            findings.append(
                DrcFinding(
                    rule="silk_printability",
                    severity="error",
                    where=f"{c.refdes} ({c.kind}, {c.side})",
                    detail=(
                        f"{c.kind} silk for {c.refdes} uses a "
                        f"{c.stroke_width_mm:.3f}mm stroke, below "
                        f"{capability.process}'s {min_width:.3f}mm minimum "
                        "printable silk width -- the fab cannot resolve a "
                        "thinner line"
                    ),
                    objects=(
                        {
                            "refdes": c.refdes,
                            "kind": c.kind,
                            "stroke_width_mm": c.stroke_width_mm,
                        },
                    ),
                    margin_mm=c.stroke_width_mm - min_width,
                )
            )

    for c in census:
        if c.outcome not in ("placed", "relocated") or c.height_mm is None:
            continue
        if c.height_mm >= SILK_LEGIBILITY_HEIGHT_MM - _EPS:
            continue
        findings.append(
            DrcFinding(
                rule="silk_printability",
                severity="warn",
                where=f"{c.refdes} ({c.kind}, {c.side})",
                detail=(
                    f"refdes label for {c.refdes} has a {c.height_mm:.3f}mm "
                    f"cap height, below the {SILK_LEGIBILITY_HEIGHT_MM:.3f}mm "
                    "legibility floor -- a human reading the assembled board "
                    "may struggle to make it out (a readability judgement, "
                    "not a fab limit)"
                ),
                objects=(
                    {"refdes": c.refdes, "kind": c.kind, "height_mm": c.height_mm},
                ),
                margin_mm=c.height_mm - SILK_LEGIBILITY_HEIGHT_MM,
            )
        )
    return findings


#: Float-noise tolerance for the 45-degree direction test — the DRC-side
#: twin of :data:`precis.pcb.realize._OCTILINEAR_EPS_MM` (kept as a local
#: constant: this module deliberately imports no realizer). A genuinely
#: octilinear segment misses by rounding only (~1e-14 at board scale);
#: an off-angle emitter misses by whole hundredths of a millimetre.
_OCTILINEAR_EPS_MM = 1e-6


def check_octilinear(model: dict[str, Any]) -> list[DrcFinding]:
    """Every drawn copper LINE segment must run at a multiple of 45
    degrees — axis-aligned or a true diagonal (|dx| == |dy|) — the
    octilinear discipline every emitter on the maze-router path in
    :mod:`precis.pcb.realize` guarantees since 2026-08-31 (user
    requirement: every wire on every layer fully 90/45).

    Scope: ``segments`` entries with ``shape == "line"`` on
    ``model["copper"]`` — the drawn wires, dogbone stubs included. Arcs
    are exempt (a corner fillet between two octilinear runs is still an
    octilinear layout, and an arc has no single direction to grade);
    pour rims are exempt (a pour follows its blockers, it is not a
    wire); silkscreen is ink, not copper. No capability field and no
    two-tier margin — a fab can image any angle, so this is a HOUSE
    style rule; severity is ``error`` so the acceptance fixtures'
    copper-class hard zero holds it board-wide."""
    findings: list[DrcFinding] = []
    for item in model.get("copper") or []:
        for seg in item.get("segments") or []:
            if seg.get("shape") != "line":
                continue
            x1, y1 = (float(v) for v in seg["start"])
            x2, y2 = (float(v) for v in seg["end"])
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            if (
                dx <= _OCTILINEAR_EPS_MM
                or dy <= _OCTILINEAR_EPS_MM
                or abs(dx - dy) <= _OCTILINEAR_EPS_MM
            ):
                continue
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            net, layer = item.get("net"), item.get("layer")
            findings.append(
                DrcFinding(
                    rule="octilinear",
                    severity="error",
                    where=f"track[{net}] on {layer}",
                    detail=(
                        f"segment ({x1:.3f},{y1:.3f})->({x2:.3f},{y2:.3f}) "
                        f"on {layer} runs at {angle:.1f}deg; every drawn "
                        f"wire must be a multiple of 45deg"
                    ),
                    objects=({"net": net, "layer": layer, "x": x1, "y": y1},),
                )
            )
    return findings


def check_silk_edge_clearance(
    model: dict[str, Any],
    capability: CapabilityRow,
    *,
    outline: list[list[float]] | None,
) -> list[DrcFinding]:
    """Silk-to-board-edge clearance — the same two-tier bar
    :func:`check_board_edge_clearance` applies to copper, applied here to
    silkscreen ink, off the same ``board_edge_clearance_vcut_mm`` field
    and the same :func:`_two_tier`/:func:`_margin_detail` machinery.

    ``precis.pcb.silk.build_silk`` already keeps a caller-supplied
    ``outline`` clear of this margin at CANDIDATE time
    (``silk._silk_edge_margin_mm``) for the refdes label and the pin-1
    dot — but only for those two, and only when it was given an outline
    at all. This rule is the independent DRC-time check of the same fact,
    over EVERY item in ``model["silkscreen"]``: the courtyard ring and the
    corner tick, which the generator deliberately never relocates (silk.py's
    own module docstring — their footprint is the part's own, not a
    candidate choice), a render built with no ``outline`` passed to
    :func:`~precis.pcb.silk.build_silk` at all, and a genuine regression in
    the generator itself. A candidate that this module's own generation
    side already rejected should never fire this rule; one that does is
    the two disagreeing about the one board-edge number the module
    docstring's edge-margin note says they must not."""
    field = "board_edge_clearance_vcut_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    if jlc_min is None or not outline or len(outline) < 3:
        return []
    ring_pts = [(float(p[0]), float(p[1])) for p in outline]
    if ring_pts[0] != ring_pts[-1]:
        ring_pts.append(ring_pts[0])
    boundary = LineString(ring_pts)
    findings: list[DrcFinding] = []
    for side, draws in (model.get("silkscreen") or {}).items():
        for draw in draws:
            geom = _silk_item_polygon(draw)
            if geom is None or geom.is_empty:
                continue
            gap = boundary.distance(geom)
            result = _two_tier(gap, jlc_min, house)
            if result is None:
                continue
            severity, margin = result
            role = str(draw.get("role") or "")
            refdes = str(draw.get("refdes") or "")
            findings.append(
                DrcFinding(
                    rule="silk_edge_clearance",
                    severity=severity,
                    where=f"silk {role}[{refdes}] on {side}",
                    detail=_margin_detail(
                        "silk-to-board-edge clearance",
                        gap,
                        capability,
                        field,
                        severity,
                        margin,
                    ),
                    objects=({"role": role, "refdes": refdes, "side": side},),
                    margin_mm=margin,
                )
            )
    return findings


# ── orchestrator ────────────────────────────────────────────────────────


def check_connectivity(model: dict[str, Any]) -> list[DrcFinding]:
    """Every net's copper must be ONE connected component.

    The rule that makes "zero DRC errors" mean something. Every other rule
    here checks that copper is not too close, too thin, or too near an
    edge — all of which a board with a severed net passes. See
    :mod:`precis.pcb.connectivity` for the two shipped defects this catches
    and why they were invisible to the rest of this module.
    """
    # Lazy, to break a genuine cycle rather than paper over one: the
    # connectivity module deliberately reuses THIS module's primitive
    # alphabet and gap arithmetic (two notions of "touching" between
    # clearance and connectivity would be a defect generator of its own),
    # so it imports drc and drc cannot import it at module scope. Same
    # idiom, same reason, as handlers/_paper_search.py.
    from precis.pcb.connectivity import net_islands

    # A net with no copper at all is the ``unrouted`` question (gr488909):
    # its pads are each "a piece", so asking here too reported every
    # multi-pad net of a pads-only board as a connectivity error per pad —
    # one defect under two names.
    copper_nets = {str(item.get("net", "")) for item in model.get("copper") or []}
    return [
        DrcFinding(
            rule="connectivity",
            severity="error",
            where=f"net {island.net}",
            detail=(
                f"net {island.net} copper is in {island.components} disconnected "
                "pieces; a net's copper must be one connected component. "
                "Witnesses (one point per piece): "
                + "; ".join(
                    f"({x:.3f},{y:.3f}) on {layer}"
                    for x, y, layer in island.witnesses[:6]
                )
            ),
            objects=({"net": island.net, "components": island.components},),
        )
        for island in net_islands(model)
        if island.net in copper_nets
    ]


def check_unrouted(
    unrouted: list[dict[str, Any]] | None,
) -> list[DrcFinding]:
    """An unrouted connection is a DRC error, not a side channel.

    It was reported only in ``RealizeResult.unrouted`` and a ``failed`` row
    while ``view='drc'`` said zero errors — so "DRC clean" did not mean
    "board is finished". That is the same trap as reaching zero errors by
    routing nothing, relocated: the number a reader trusts stays silent
    about the thing that matters.

    Each entry needs a ``net``; ``from``/``to`` name the two endpoints when
    the caller knows them (the realizer does — it works per connection) and
    are omitted when it only knows the net (the DRC view reads per-net
    ``pcb_routes`` status). The finding says which it got rather than
    printing ``?`` for a fact nobody claimed to have.
    """
    out: list[DrcFinding] = []
    for item in unrouted or []:
        net = str(item.get("net", "?"))
        a, b = item.get("from"), item.get("to")
        what = f"connection {a} -> {b}" if a and b else "at least one connection"
        note = f" ({item['note']})" if item.get("note") else ""
        out.append(
            DrcFinding(
                rule="unrouted",
                severity="error",
                where=f"net {net}",
                detail=(
                    f"{what} on net {net} has no route{note}; the board is not finished"
                ),
                objects=(dict(item),),
            )
        )
    return out


# ── netlist-level rules on the same check surface ──────────────────────
#
# docs/backlog/pcb-lazy-netlist-and-checks.md §1g ("one check surface, not
# two") + §1j (intent markers): the rules below need no geometry — they
# judge the netlist — but they report on THIS surface, as the same
# ``DrcFinding`` row (``margin_mm=None``), because splitting the surface
# would move the schematic/layout division into the checker rather than
# remove it.


#: Design phases for phase-aware severity (§1g's "severity is a function
#: of ``(rule, phase)``"), cheapest-to-latest. ``fab`` is never derived —
#: it is the author's claim the design is tape-out ready (Slice 8's
#: ``op='phase'``); :func:`derive_phase` only ever reports the three the
#: store can see.
PHASES = ("netlist", "placed", "routed", "fab")


def derive_phase(*, placed: bool, routed: bool) -> str:
    """The design's phase from what the store can observe (§1g): nothing
    placed → ``netlist``; placed but the router never ran → ``placed``;
    else ``routed``. ``fab`` is never derived — a check that
    escalates at ``fab`` can only reach that tier when the author (Slice
    8's ``op='phase'``) says so."""
    if not placed:
        return "netlist"
    return "routed" if routed else "placed"


#: Severity as a function of ``(rule, phase)`` — §1g's one extra axis on
#: the ``jlc_min``/``house_default`` two-tier machinery. ``unrouted``
#: while the design is still being built is a work item, not a defect:
#: "the board is not finished" is true by construction before routing
#: ever ran, and reporting it at ``error`` for a netlist-phase board is
#: the noise that trains a reader to ignore the check. At ``routed``/``fab``
#: it stays its base severity — the router has run (or the author claims
#: tape-out) and a net without copper IS the defect then.
#:
#: ``todo`` is a PRESENTED tier only: :attr:`DrcFinding.severity` keeps
#: the base tier (``error``/``warn`` — ``pcb_drc_findings.severity``'s
#: CHECK accepts nothing else) and the check view renders
#: :func:`severity_at`'s value. Nothing stored ever claims "todo", so a
#: persisted run's severity stays the rule's own verdict, not the phase
#: it happened to be judged under.
PHASE_SEVERITY: dict[str, dict[str, str]] = {
    "unrouted": {"netlist": "todo", "placed": "todo"},
}


def severity_at(rule: str, severity: str, phase: str) -> str:
    """The tier ``view='check'`` shows for ``rule`` at ``phase`` —
    :data:`PHASE_SEVERITY` when it names the pair, the rule's base
    severity otherwise. (Rules whose phase-response is intrinsic, like
    ``unconnected_pin``'s warn-before-fab/error-at-fab, already take
    ``phase`` themselves and are not listed here.)"""
    return PHASE_SEVERITY.get(rule, {}).get(phase, severity)


# ``PIN_ETYPES`` (the closed §1i pin etype vocabulary) is imported at
# the top from ``precis.pcb`` — ``ir.py`` reads it too, and
# ``ir → drc → rules → ir`` would be a cycle — and re-exported here in
# ``__all__`` since the ERC rules below are its primary reader.


def _pin_marker(pin: dict[str, Any]) -> str | None:
    """The §1j intent marker a netless pin carries — ``"nc"`` /
    ``"must_terminate"`` / ``"unused"`` / ``None``. Each reads the pin's
    meta bool (``_pcb_apply`` writes it for the ``nc: true`` /
    ``unused: true`` / ``must_terminate: true`` decl keys) OR the same word
    in ``tags``, so a footprint ``pin_map`` tag — the part-level authoring
    surface ``nc``/``must_terminate`` belong to — reaches the pin through
    completion untouched. Resolution order is the spec's: ``nc`` and
    ``unused`` silence the complaint; ``must_terminate`` overrides
    ``unused`` but not ``nc`` (a no-internal-connection pad cannot be
    terminated — both set is contradictory authoring that resolves the
    physical fact's way)."""
    tags = {str(t) for t in pin.get("tags") or []}
    meta = pin.get("meta") or {}
    if "nc" in tags or meta.get("nc"):
        return "nc"
    if "must_terminate" in tags or meta.get("must_terminate"):
        return "must_terminate"
    if "unused" in tags or meta.get("unused"):
        return "unused"
    return None


def check_unconnected_pins(
    unconnected: Iterable[dict[str, Any]] | None, *, phase: str = "netlist"
) -> list[DrcFinding]:
    """A declared pin with no net and no stated reason is a complaint —
    phase-aware (§1j): ``warn`` while the board is still being worked
    (``netlist``/``placed``/``routed`` — "did you mean to wire this?" is a
    TODO then, not a defect), ``error`` at ``fab``, where an unwired pin
    the author never accounted for is a dead-board class bug.

    The §1j markers are why this can exist at all — declaring every
    footprint pad as a pin (Slice 2) without them would emit ~24
    intentional "unconnected" complaints per EWOD sink and train the
    reader to ignore the rule. ``nc`` (part-level: the datasheet says no
    internal connection) never complains. ``unused`` (design-level: this
    board does not use a real pin) never complains about being netless —
    the pad keeps full geometry and stays matcher-available. The marker
    that pays is ``must_terminate`` (part-level: the pin must not float —
    a CMOS input, /OE, /CS, the HV507's BL/POL): it errors whenever it is
    netless, **even if also marked ``unused``**, at every phase.

    Aggregation: dozens of unmarked spares on one MCU would otherwise be
    dozens of identical warnings — the same "train the reader to ignore
    it" failure the markers exist to prevent. Below ``fab`` the rule
    therefore emits ONE warn per instance listing the pin names; at
    ``fab`` each pin is its own error row, because the bundle review reads
    one line per defect. ``must_terminate`` stays per-pin at every phase —
    a floating no-float pin is never bulk-summarized.

    ``unconnected`` entries are ``pcb_graph``'s ``unconnected`` rows:
    ``{refdes, pin, tags, meta}``. A pin with a net never reaches this
    rule — it is about absence of a net, nothing else."""
    findings: list[DrcFinding] = []
    spare_by_refdes: dict[str, list[str]] = {}
    for pin in unconnected or []:
        marker = _pin_marker(pin)
        if marker in ("nc", "unused"):
            continue
        refdes, name = str(pin.get("refdes")), str(pin.get("pin"))
        if marker == "must_terminate":
            findings.append(
                DrcFinding(
                    rule="unconnected_pin",
                    severity="error",
                    where=f"pin {refdes}/{name}",
                    detail=(
                        f"pin {refdes}/{name} is marked must_terminate and has "
                        "no net — a pin that must not float is floating "
                        "(the `unused` marker does not exempt it)"
                    ),
                    objects=({"refdes": refdes, "pin": name, "marker": marker},),
                )
            )
            continue
        if phase == "fab":
            findings.append(
                DrcFinding(
                    rule="unconnected_pin",
                    severity="error",
                    where=f"pin {refdes}/{name}",
                    detail=(
                        f"pin {refdes}/{name} has no net and no intent marker — "
                        "wire it, or mark it `nc` (part: no internal "
                        "connection) / `unused` (this board does not use it) / "
                        "`must_terminate` (it must not float — and then it may "
                        "NOT stay netless)"
                    ),
                    objects=({"refdes": refdes, "pin": name},),
                )
            )
            continue
        spare_by_refdes.setdefault(refdes, []).append(name)
    for refdes in sorted(spare_by_refdes):
        names = sorted(spare_by_refdes[refdes])
        shown = ", ".join(names[:12]) + (
            f" … +{len(names) - 12}" if len(names) > 12 else ""
        )
        findings.append(
            DrcFinding(
                rule="unconnected_pin",
                severity="warn",
                where=f"part {refdes}",
                detail=(
                    f"{len(names)} spare pad(s) on {refdes} netless and unmarked "
                    f"({shown}) — mark them `unused` (this board does not use "
                    "them; full geometry, still matcher-available) or `nc` "
                    "(part has no internal connection), or wire them"
                ),
                objects=({"refdes": refdes, "pins": tuple(names)},),
            )
        )
    return findings


def check_undeclared_pads(
    model: dict[str, Any], *, unchecked: Sequence[str] = ()
) -> list[DrcFinding]:
    """A footprint pad no declared pin claims — the Slice-2 safety net.

    ``_pcb_apply`` completes pin declarations from the cached footprint's
    ``pin_map``, so a pad reaches ``model["pads"]`` carrying
    ``undeclared`` (:func:`precis.pcb.realize._footprint_pad_rows`) only
    when completion could not run: the footprint was cached AFTER the
    component was applied (``op='footprint'`` — a re-put completes the
    declarations), or the design predates declare-all. The pad is still
    real geometry — it is checked for clearance like any pad — but the
    netlist has no name for it, which is what this rule complains about:
    warn, never silent, never a clean pass.

    ``unchecked`` names refdes whose footprint is NOT cached: the rule
    cannot tell whether those packages have undeclared pads at all, so it
    says so — "a check that cannot fire must say so", the same contract
    ``synthesized_footprint`` keeps."""
    findings: list[DrcFinding] = []
    for pad in model.get("pads") or []:
        if not pad.get("undeclared"):
            continue
        refdes = str(pad.get("refdes") or "")
        label = str(pad.get("pin") or "")
        number = pad.get("pad_number")
        findings.append(
            DrcFinding(
                rule="undeclared_pad",
                severity="warn",
                where=f"pad {refdes}/{number or label}",
                detail=(
                    f"pad {number or label} on {refdes} is real footprint "
                    "copper with no declared pin — it is checked for "
                    "geometry but the netlist cannot name it; re-put the "
                    "component so _pcb_apply completes its pin declarations "
                    "from the cached footprint"
                ),
                objects=(
                    {
                        "refdes": refdes,
                        "pin": label,
                        "pad_number": number,
                        "layer": pad.get("layer"),
                    },
                ),
            )
        )
    if unchecked:
        names = ", ".join(sorted(str(r) for r in unchecked))
        findings.append(
            DrcFinding(
                rule="undeclared_pad",
                severity="warn",
                where="footprint cache",
                detail=(
                    f"{names}: no cached footprint — whether these parts "
                    "have pads no pin declares CANNOT be checked "
                    "(op='footprint' or a catalog fetch completes them)"
                ),
                objects=tuple(
                    {"refdes": str(r), "not_checked": True} for r in sorted(unchecked)
                ),
            )
        )
    return findings


def run_geometric_drc(
    model: dict[str, Any],
    *,
    capability: CapabilityRow,
    outline: list[list[float]] | None = None,
    courtyards: list[Courtyard] | None = None,
    courtyard_bottom: dict[str, bool] | None = None,
    panel_type: str | None = None,
    net_rules: dict[str, NetRules] | None = None,
    net_voltages: dict[str, float] | None = None,
    unrouted: list[dict[str, Any]] | None = None,
    census: tuple[SilkPlacement, ...] | None = None,
    holes: Sequence[HoleKeepout] = (),
    edge_mates: Sequence[Any] | None = None,
    unconnected: Iterable[dict[str, Any]] | None = None,
    phase: str = "netlist",
    undeclared_unchecked: Sequence[str] = (),
    courtyard_clearance_mm: float | None = None,
) -> list[DrcFinding]:
    """Every geometric DRC rule over one realized board, in one call — what
    ``view='drc'`` and the ``netlist_drc_clean`` gate evaluator both run.
    ``courtyard_clearance_mm`` is the offset ``courtyards`` were inflated
    by, threaded to :func:`check_courtyard_overlap`'s land-hull grading.
    ``net_rules`` (net name -> resolved :class:`~precis.pcb.rules.NetRules`)
    threads the per-net clearance override into :func:`check_clearance`
    only — the other rules stay capability-only (module docstring: they
    check the fab's own hard limits, not an authored class preference).
    ``net_voltages`` (net name -> ``pcb_nets.working_voltage_v``) rides the
    same single seam, for the same reason: §E-1's spacing rule is pairwise,
    and :func:`check_clearance` is the only rule that already reasons about
    a pair of nets.

    ``unrouted`` is the realizer's list of connections it could not route.
    It is an argument rather than something derived from ``model`` because
    a model cannot distinguish "not routed" from "not attempted" — absence
    of copper is not evidence of failure, and guessing here would either
    invent errors or hide them.

    ``census`` is :func:`precis.pcb.silk.build_silk`'s own per-item
    placement record (:class:`~precis.pcb.silk.SilkPlacement`) — an
    argument for the exact same reason ``unrouted`` is one: this module
    cannot derive "was label X actually rendered, and if not, why" from
    ``model["silkscreen"]`` alone, since an absent draw there is
    indistinguishable from "never attempted" versus "dropped for a stated
    reason" without the record :func:`~precis.pcb.silk.build_silk` already
    produced while deciding. ``census=None`` (the default) keeps every
    existing caller — none of which built a census before this rule
    existed — DRC-clean on this axis exactly as before, the identical
    "silent about a thing this module was never told" contract
    ``unrouted=None`` already has.

    ``courtyard_bottom`` (gr341516) threads straight into
    :func:`check_courtyard_overlap`'s own ``bottom_by_refdes`` — see that
    function's docstring; ``None`` (the default) keeps every existing
    caller's courtyard-overlap behaviour unchanged.

    ``holes`` (mounting-hole keep-out polygons) feeds
    :func:`check_courtyard_hole`; empty by default.

    ``edge_mates`` (:func:`precis.pcb.edge_mate.records`) feeds
    :func:`check_edge_mating`; ``None`` keeps a caller that does not pass it
    silent on that axis.

    ``unconnected`` + ``phase`` + ``undeclared_unchecked`` feed the
    netlist-level rules (§1g's one check surface):
    :func:`check_unconnected_pins` (the ``pcb_graph`` ``unconnected`` rows,
    markers read off their ``tags``/``meta``, severity set by ``phase``)
    and :func:`check_undeclared_pads` (``undeclared`` pad flags already on
    ``model["pads"]``, plus a not-checked note for the refdes
    ``undeclared_unchecked`` names). ``None``/empty keeps a caller that
    never built them silent on that axis — same contract as ``unrouted``.
    """
    findings: list[DrcFinding] = []
    findings += check_synthesized_footprint(model)
    findings += check_clearance(
        model, capability, net_rules=net_rules, net_voltages=net_voltages
    )
    findings += check_trace_width(model, capability)
    findings += check_annular_ring(model, capability)
    findings += check_npth_clearance(model, capability)
    findings += check_via_pad_keepout(model, capability)
    findings += check_via_via_keepout(model, capability)
    findings += check_board_edge_clearance(
        model, capability, outline=outline, panel_type=panel_type
    )
    findings += check_silk_edge_clearance(model, capability, outline=outline)
    findings += check_octilinear(model)
    findings += check_outline_containment(model, outline=outline, courtyards=courtyards)
    findings += check_edge_mating(edge_mates, outline=outline)
    findings += check_connectivity(model)
    findings += check_unrouted(unrouted)
    findings += check_unconnected_pins(unconnected, phase=phase)
    findings += check_undeclared_pads(model, unchecked=undeclared_unchecked)
    if courtyards:
        findings += check_courtyard_overlap(
            courtyards,
            bottom_by_refdes=courtyard_bottom,
            clearance_mm=courtyard_clearance_mm,
            pads=model.get("pads"),
        )
        findings += check_courtyard_hole(courtyards, holes)
    findings += check_silk_missing(census or (), model)
    findings += check_silk_printability(census or (), capability)
    return findings


def check_coating_extent(
    outline: Sequence[Sequence[float]] | None,
    *,
    ewod_components: Sequence[str],
    extent_mm: tuple[float, float] | None,
) -> list[DrcFinding]:
    """Parylene-coater size check, EWOD boards only (gr414481).

    Only a board carrying an ``ewod_pad_array`` is coated, so
    ``ewod_components`` (the generator component names) empty means no
    finding either way. ``extent_mm`` is
    :func:`precis.pcb.capabilities.coating_extent_mm`; ``None`` is UNKNOWN,
    never "no limit", so it yields a ``warn`` that the board size has NOT
    been screened. With a limit, the outline bounding box must fit in either
    orientation, else an ``error`` whose ``margin_mm`` is the (negative)
    smaller per-axis overshoot. No outline -> nothing to measure.
    """
    if not ewod_components:
        return []
    names = ", ".join(sorted(ewod_components))
    objs = tuple({"component": n} for n in sorted(ewod_components))
    if extent_mm is None:
        return [
            DrcFinding(
                rule="coating_extent_unknown",
                severity="warn",
                where="board",
                detail=(
                    f"EWOD component(s) {names}: the parylene coater's max "
                    "board width/height is not configured, so the board size "
                    "has NOT been screened against the coating step. Supply "
                    "the coater's size (fill coating_extent_mm in "
                    "pcb_capabilities.json)."
                ),
                objects=objs,
            )
        ]
    if not outline:
        return []
    xs = [float(pt[0]) for pt in outline]
    ys = [float(pt[1]) for pt in outline]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    mw, mh = extent_mm
    over_direct = max(w - mw, h - mh)
    over_rot = max(w - mh, h - mw)
    over = min(over_direct, over_rot)
    if over <= 0:
        return []
    return [
        DrcFinding(
            rule="coating_extent_exceeded",
            severity="error",
            where="board",
            detail=(
                f"board outline {w:.2f} x {h:.2f}mm exceeds the coater's "
                f"{mw:g} x {mh:g}mm (either orientation) by {over:.2f}mm; "
                f"EWOD component(s) {names} need the parylene step."
            ),
            objects=objs,
            margin_mm=-over,
        )
    ]


# ── ERC: the §1h rules over §1i pin semantics (Slice 5) ──────────────
#
# These are netlist rules — they read the graph's ``nets`` (members
# carrying ``tags``/``meta`` straight off ``pcb_pins``), never geometry —
# but they report on THIS surface as the same ``DrcFinding`` row with
# ``margin_mm=None``. Every one of them has an explicit CANNOT-FIRE path:
# a design that declares none of the vocabulary a rule needs gets a
# ``not_checked`` warn naming the gap, never a silent pass — a rule that
# cannot see is not a rule that passed.


def _pin_etype(pin: dict[str, Any]) -> str | None:
    """The pin's §1i electrical role — the ``PIN_ETYPES`` word in its
    tags (``_pcb_apply`` validates + stores it there; a footprint
    ``pin_map`` tag lands the same way through completion)."""
    tags = {str(t) for t in pin.get("tags") or []}
    return next((t for t in PIN_ETYPES if t in tags), None)


def _pin_obj(pin: dict[str, Any]) -> dict[str, Any]:
    return {"refdes": pin.get("refdes"), "pin": pin.get("pin")}


def _erc_not_checked(rule: str, why: str) -> DrcFinding:
    """The rule's cannot-fire path: one warn naming what it would have
    needed — the same "CANNOT be checked" phrasing
    ``check_undeclared_pads``'s NOT-CHECKED note established."""
    return DrcFinding(
        rule=rule,
        severity="warn",
        where="netlist",
        detail=(
            f"{rule} CANNOT be checked — {why}; the check is a "
            "not-checked, not a clean pass"
        ),
        objects=({"not_checked": True, "rule": rule},),
    )


#: Etypes that exclusively DRIVE a net — two of them tied together is a
#: push-pull contention (``pin_conflict``). ``open_drain``/``tristate``
#: are shareable by design (wired-OR, buses); ``bidi`` drives AND reads
#: so it satisfies ``no_driver`` but two bidis on one net is legal.
_EXCLUSIVE_DRIVERS = frozenset({"output", "power_out"})
_DRIVERS = _EXCLUSIVE_DRIVERS | {"bidi", "open_drain", "tristate"}
_CONSUMERS = frozenset({"input", "power_in"})


def check_pin_conflicts(
    nets: Iterable[dict[str, Any]],
) -> list[DrcFinding]:
    """Two exclusive drivers (``output``/``power_out``) on one net — the
    classic ERC error two outputs tied together hides as. Shareable
    drivers (``open_drain``, ``tristate``, ``bidi``) never conflict.

    ``nets`` are the graph's net dicts (``members`` carry ``tags``/
    ``meta``). Cannot fire — and says so — when no pin anywhere declares
    an etype."""
    if not any(_pin_etype(m) for n in nets for m in n.get("members") or []):
        return [
            _erc_not_checked("pin_conflict", "no pin in the design declares an etype")
        ]
    out: list[DrcFinding] = []
    for net in nets:
        drivers = [
            m for m in net.get("members") or [] if _pin_etype(m) in _EXCLUSIVE_DRIVERS
        ]
        if len(drivers) < 2:
            continue
        out.append(
            DrcFinding(
                rule="pin_conflict",
                severity="error",
                where=f"net {net['name']}",
                detail=(
                    f"net {net['name']} has {len(drivers)} exclusive "
                    "drivers ("
                    + ", ".join(f"{m['refdes']}/{m['pin']}" for m in drivers)
                    + ") — two outputs tied together contend"
                ),
                objects=tuple(_pin_obj(m) for m in drivers),
            )
        )
    return out


def check_no_driver(
    nets: Iterable[dict[str, Any]],
) -> list[DrcFinding]:
    """A net with a declared consumer (``input``/``power_in``) but no
    pin that can drive it — the net floats by construction. Advisory
    (``warn``): a connector pin awaiting a second board is the same
    shape."""
    if not any(_pin_etype(m) for n in nets for m in n.get("members") or []):
        return [_erc_not_checked("no_driver", "no pin in the design declares an etype")]
    out: list[DrcFinding] = []
    for net in nets:
        members = net.get("members") or []
        consumers = [m for m in members if _pin_etype(m) in _CONSUMERS]
        if not consumers:
            continue
        if any(_pin_etype(m) in _DRIVERS for m in members):
            continue
        out.append(
            DrcFinding(
                rule="no_driver",
                severity="warn",
                where=f"net {net['name']}",
                detail=(
                    f"net {net['name']} has input(s) "
                    + ", ".join(f"{m['refdes']}/{m['pin']}" for m in consumers)
                    + " but nothing that drives them"
                ),
                objects=tuple(_pin_obj(m) for m in consumers),
            )
        )
    return out


def check_unsupplied_client(
    clients: Sequence[Any],
    supplies: Sequence[Any],
    roles: Mapping[str, Any],
) -> list[DrcFinding]:
    """A ``client_of`` pin no supplier terminal can serve: runs the same
    capacity/voltage gate the route job uses (distance-free
    :func:`precis.pcb.matching.solve`) and errors once per unsupplied
    client. No clients -> no findings."""
    from precis.pcb import matching

    if not clients:
        return []
    try:
        res = matching.solve(clients, supplies, roles, lambda _r, _p: (0.0, 0.0))
    except ValueError as exc:
        return [_erc_not_checked("unsupplied_client", str(exc))]
    by_pin = {
        (c.instance, p): c
        for c in clients
        for p in (c.members if c.members is not None else (c.pin,))
    }
    n_clients: dict[str, int] = {}
    n_terms: dict[str, int] = {}
    for c in clients:
        n_clients[c.role] = n_clients.get(c.role, 0) + 1
    for s in supplies:
        n_terms[s.role] = n_terms.get(s.role, 0) + len(s.blocks)
    out: list[DrcFinding] = []
    for refdes, pin in res.unsupplied:
        role = by_pin[(refdes, pin)].role
        out.append(
            DrcFinding(
                rule="unsupplied_client",
                severity="error",
                where=f"pin {refdes}/{pin}",
                detail=(
                    f"pin {refdes}/{pin} is a client of role {role} but no "
                    f"supplier of role {role} has a free admissible terminal "
                    f"({n_clients[role]} clients vs {n_terms.get(role, 0)} terminals)"
                ),
                objects=(_pin_obj({"refdes": refdes, "pin": pin}),),
            )
        )
    return out


def _erc_unknown_for_net(
    rule: str,
    net: dict[str, Any],
    pins: list[dict[str, Any]],
    what: str,
) -> DrcFinding:
    """A per-net cannot-fire row (pcb-datasheet-review.md slice 2, Reto's
    ruling): pins on this net declare a bound (``max_voltage_v`` /
    ``max_current_a``) but the net declares no voltage/current to compare
    it against. The net's value is NEVER guessed — the finding asks the
    user instead. Renders ``info`` like the rule-level not-checked row."""
    return DrcFinding(
        rule=rule,
        severity="warn",
        where=f"net {net['name']}",
        detail=(
            f"{what} unknown for net {net['name']}: ask the user — pin(s) "
            + ", ".join(f"{p['refdes']}/{p['pin']}" for p in pins)
            + " declare a bound nothing on the net answers"
        ),
        objects=(
            {"not_checked": True, "rule": rule, "net": net["name"]},
            *(_pin_obj(p) for p in pins),
        ),
    )


def _net_voltage(net: dict[str, Any], class_rules: dict[str, Any]) -> float | None:
    """A net's working voltage: ``pcb_nets.working_voltage_v`` when
    authored, else its class's ``rules['working_voltage_v']`` (the Slice
    4c net-class key — one key, two+ consumers: ``check_over_voltage``,
    ``check_passive_rating``). ``None`` when nobody declared it — the
    never-guess ruling leaves the caller to emit an ask-the-user row."""
    v = net.get("working_voltage_v")
    if v is None:
        cls = class_rules.get(str(net.get("net_class") or ""))
        v = (cls or {}).get("working_voltage_v") if isinstance(cls, dict) else None
    return None if v is None else float(v)


def check_over_voltage(
    nets: Iterable[dict[str, Any]],
    net_class_rules: dict[str, Any] | None = None,
) -> list[DrcFinding]:
    """A pin on a net whose working voltage exceeds the pin's declared
    ``max_voltage_v`` — a 20 V rail on a 12 V-tolerant input.

    The net's working voltage is ``pcb_nets.working_voltage_v`` when
    authored, else its class's ``rules['working_voltage_v']`` (the Slice
    4c net-class key — one key, two consumers). Cannot fire — and says
    so — when nothing declares either side (no net voltage AND no pin
    bound). A bound pin on a net whose voltage nobody declared is a
    per-net not-checked row (``voltage unknown for net X: ask the
    user``) — an imported board's rails are never guessed."""
    class_rules = net_class_rules or {}
    any_voltage = False
    any_bound = False
    out: list[DrcFinding] = []
    for net in nets:
        v = _net_voltage(net, class_rules)
        if v is not None:
            any_voltage = True
        unbounded: list[dict[str, Any]] = []
        for m in net.get("members") or []:
            bound = (m.get("meta") or {}).get("max_voltage_v")
            if bound is None:
                continue
            any_bound = True
            if v is None:
                unbounded.append(m)
                continue
            # Absolute values — a negative rail (−12 V against a 5 V pin
            # bound) is just as over.
            if abs(float(bound)) >= abs(float(v)):
                continue
            out.append(
                DrcFinding(
                    rule="over_voltage",
                    severity="error",
                    where=f"net {net['name']}",
                    detail=(
                        f"net {net['name']} works at {float(v):g} V but "
                        f"{m['refdes']}/{m['pin']} is rated "
                        f"{float(bound):g} V max"
                    ),
                    objects=(_pin_obj(m),),
                )
            )
        if unbounded:
            out.append(_erc_unknown_for_net("over_voltage", net, unbounded, "voltage"))
    if not out and not (any_voltage and any_bound):
        return [
            _erc_not_checked(
                "over_voltage",
                "no net carries working_voltage_v or no pin carries max_voltage_v",
            )
        ]
    return out


def check_over_current(
    nets: Iterable[dict[str, Any]],
) -> list[DrcFinding]:
    """A net's declared ``est_current_a`` exceeds a pin's declared
    ``max_current_a`` — a 3 A rail through a pin rated 1 A
    (pcb-datasheet-review.md slice 2; the ratings-validation half of
    ``pcb-component-model.md``'s power section).

    Whether a pin sits IN the current path is ``meta['current_role']``
    (``series``/``shunt``/``signal``, authored via ``op='pins'``): only
    ``shunt`` is exempt — a shunt device taps the net without carrying
    its through current, so its rating is not the net's budget. A pin
    that declares ``max_current_a`` with no role is still compared: the
    bound is the authored fact, the role only exempts.

    Cannot fire — and says so — when nothing declares either side. A
    bound pin on a net with no ``est_current_a`` is a per-net
    not-checked row (``current unknown for net X: ask the user``), the
    same never-guess ruling as :func:`check_over_voltage`."""
    any_current = False
    any_bound = False
    out: list[DrcFinding] = []
    for net in nets:
        i_net = net.get("est_current_a")
        if i_net is not None:
            any_current = True
        unknown: list[dict[str, Any]] = []
        for m in net.get("members") or []:
            meta = m.get("meta") or {}
            bound = meta.get("max_current_a")
            if bound is None:
                continue
            any_bound = True
            # ``shunt`` is the one exemption (a tap, not the through
            # path); every other declared role — and none — compares.
            if str(meta.get("current_role") or "").strip() == "shunt":
                continue
            if i_net is None:
                unknown.append(m)
                continue
            if abs(float(i_net)) <= abs(float(bound)):
                continue
            out.append(
                DrcFinding(
                    rule="over_current",
                    severity="error",
                    where=f"net {net['name']}",
                    detail=(
                        f"net {net['name']} carries {float(i_net):g} A but "
                        f"{m['refdes']}/{m['pin']} is rated "
                        f"{float(bound):g} A max"
                    ),
                    objects=(_pin_obj(m),),
                )
            )
        if unknown:
            out.append(_erc_unknown_for_net("over_current", net, unknown, "current"))
    if not out and not (any_current and any_bound):
        return [
            _erc_not_checked(
                "over_current",
                "no net carries est_current_a or no pin carries max_current_a",
            )
        ]
    return out


def _passive_not_checked(refdes: str, why: str, *objects: dict[str, Any]) -> DrcFinding:
    """``check_passive_rating``'s per-part cannot-fire row — one warn
    naming the part and the gap, the ``not_checked`` object marker the
    surface renders ``info`` (same contract as
    :func:`_erc_unknown_for_net`, keyed to the part not the net)."""
    return DrcFinding(
        rule="passive_rating",
        severity="warn",
        where=f"part {refdes}",
        detail=f"passive_rating CANNOT check {refdes} — {why}",
        objects=(
            {"not_checked": True, "rule": "passive_rating", "refdes": refdes},
            *objects,
        ),
    )


def check_passive_rating(
    nets: Iterable[dict[str, Any]],
    instances: Iterable[dict[str, Any]],
    net_class_rules: dict[str, Any] | None = None,
) -> list[DrcFinding]:
    """A catalog passive whose parsed rating fails the margin policy —
    pcb-datasheet-review.md slice 3 (in-scope 4's parse + 5's
    ``passive_rating``): ``C1525``'s description parses to
    ``100nF 16V X7R`` (:func:`precis.pcb.catalog.parse_passive_description`)
    and the named policy (``PASSIVE_MARGIN_POLICY`` r
    ``PASSIVE_MARGIN_REVISION``) then asks rated ≥ factor × the voltage
    across it, dielectric rank ≥ any ``min_dielectric`` a member pin
    declares, and a resistor's dissipation ≤ 50 % of its rated power.

    ``instances`` are the graph's instance dicts — the check needs only
    ``refdes`` + ``part_description`` (the ``parts`` catalog join
    ``pcb_graph`` hoists). A capacitor's working voltage is the spread
    between the nets it joins (``_net_voltage`` — authored or class
    ``working_voltage_v``); an unannotated one produces the ask-the-user
    row, never a guess. ``min_dielectric`` is the design-layer stand-in
    for the slice-5 part fact's ``dielectric ≥``: declared on a pin via
    ``op='pins'``, it binds every parsed capacitor on the pin's rail
    (pin ``meta.rail`` → all nets sharing that net-level ``meta.rail``;
    no rail means the pin's own net) — the same join
    ``check_no_decoupling`` uses to find the caps serving a rail.

    Cannot fire — and says so — when no placed part parses to a rated
    passive; a parse that lacks the field a check needs, or a net whose
    voltage is unknown, is a per-part not-checked info row, not a pass.
    """
    nets = list(nets)
    class_rules = net_class_rules or {}
    specs: dict[str, dict[str, Any]] = {}
    for inst in instances:
        spec = parse_passive_description(
            inst.get("part_description")
            if isinstance(inst.get("part_description"), str)
            else None
        )
        if spec is not None:
            specs[str(inst.get("refdes") or "")] = spec

    member_nets: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    net_rails: dict[str, Any] = {}
    for net in nets:
        net_rails[str(net["name"])] = (net.get("meta") or {}).get("rail")
        for m in net.get("members") or []:
            member_nets.setdefault(str(m.get("refdes") or ""), []).append((net, m))

    # Dielectric requirements: each pin's meta.min_dielectric, scoped by
    # its rail join — a declared rail widens the requirement to every net
    # on that rail (a bypass cap may sit on a neighbouring net of the
    # same rail), otherwise just the pin's own net.
    reqs: list[dict[str, Any]] = []
    for net in nets:
        for m in net.get("members") or []:
            meta = m.get("meta") or {}
            need = meta.get("min_dielectric")
            if need is None:
                continue
            cls = str(need).strip().lower()
            if cls not in CAP_DIELECTRIC_RANK:
                continue  # unreachable — op='pins' validates the vocabulary
            rail = meta.get("rail") or net_rails.get(str(net["name"]))
            scope = (
                {
                    name
                    for name, r in net_rails.items()
                    if r is not None and str(r) == str(rail)
                }
                if rail is not None
                else set()
            )
            # The pin's own net is always in scope — a rail the nets do
            # not repeat shrinks the requirement to just it, never to
            # nothing.
            scope.add(str(net["name"]))
            reqs.append(
                {"pin": m, "cls": cls, "rank": CAP_DIELECTRIC_RANK[cls], "nets": scope}
            )

    out: list[DrcFinding] = []
    evaluated = False
    for refdes in sorted(specs):
        spec = specs[refdes]
        entries = member_nets.get(refdes) or []
        if not entries:
            continue  # unconnected passive — check_unconnected owns it
        volts = {str(n["name"]): _net_voltage(n, class_rules) for n, _m in entries}
        unknown = sorted(name for name, v in volts.items() if v is None)
        known = [v for v in volts.values() if v is not None]
        if spec["kind"] == "capacitor":
            for req in reqs:
                hit = sorted(set(volts) & req["nets"])
                if not hit:
                    continue
                mpin = next(m for n, m in entries if str(n["name"]) in req["nets"])
                rank = CAP_DIELECTRIC_RANK.get(str(spec.get("dielectric") or ""))
                if rank is None:
                    out.append(
                        _passive_not_checked(
                            refdes,
                            f"rail serving net {hit[0]} requires dielectric "
                            f"≥ {req['cls']} ({req['pin']['refdes']}/"
                            f"{req['pin']['pin']}) but the catalog "
                            "description names no ranked dielectric — "
                            "cannot confirm",
                            _pin_obj(mpin),
                        )
                    )
                elif rank > req["rank"]:
                    evaluated = True
                    out.append(
                        DrcFinding(
                            rule="passive_rating",
                            severity="error",
                            where=f"part {refdes}",
                            detail=(
                                f"{refdes} is {spec.get('dielectric')!s} "
                                f"but net {hit[0]} requires ≥ {req['cls']} "
                                f"({req['pin']['refdes']}/{req['pin']['pin']})"
                            ),
                            objects=(_pin_obj(mpin), _pin_obj(req["pin"])),
                        )
                    )
                else:
                    evaluated = True
            rated_v = spec.get("rated_v")
            if rated_v is None:
                out.append(
                    _passive_not_checked(
                        refdes,
                        "the catalog description names no voltage rating — "
                        "the margin policy has nothing to compare",
                        *(_pin_obj(m) for _n, m in entries),
                    )
                )
            elif unknown:
                out.append(
                    _passive_not_checked(
                        refdes,
                        "; ".join(
                            f"voltage unknown for net {name}: ask the user"
                            for name in unknown
                        ),
                        *(_pin_obj(m) for _n, m in entries),
                    )
                )
            else:
                dv = (max(known) - min(known)) if known else 0.0
                factor = PASSIVE_CAP_VOLTAGE_FACTOR.get(
                    str(spec.get("dielectric") or ""),
                    PASSIVE_CAP_VOLTAGE_FACTOR_DEFAULT,
                )
                need = factor * dv
                evaluated = True
                if float(rated_v) < need - 1e-9:
                    out.append(
                        DrcFinding(
                            rule="passive_rating",
                            severity="error",
                            where=f"part {refdes}",
                            detail=(
                                f"{refdes} is rated {float(rated_v):g} V "
                                f"across {dv:g} V working — policy "
                                f"{PASSIVE_MARGIN_POLICY} "
                                f"r{PASSIVE_MARGIN_REVISION} wants ≥ "
                                f"{factor:g}× = {need:g} V"
                            ),
                            objects=tuple(_pin_obj(m) for _n, m in entries),
                        )
                    )
        else:  # resistor
            ohm = spec.get("ohm")
            rated_w = spec.get("rated_w")
            if not ohm or float(ohm) <= 0 or rated_w is None:
                out.append(
                    _passive_not_checked(
                        refdes,
                        "the catalog description names no usable "
                        "resistance or power rating — dissipation cannot "
                        "be derived",
                        *(_pin_obj(m) for _n, m in entries),
                    )
                )
            elif unknown:
                # Voltage drop is not derivable; a ``series`` pin on a
                # net with declared ``est_current_a`` still gives I²R.
                currents = [
                    float(n["est_current_a"])
                    for n, m in entries
                    if str((m.get("meta") or {}).get("current_role") or "") == "series"
                    and n.get("est_current_a") is not None
                ]
                if not currents:
                    out.append(
                        _passive_not_checked(
                            refdes,
                            "; ".join(
                                f"voltage unknown for net {name}: ask the user"
                                for name in unknown
                            ),
                            *(_pin_obj(m) for _n, m in entries),
                        )
                    )
                else:
                    watts = max(currents) ** 2 * float(ohm)
                    evaluated = True
                    if watts > float(rated_w) * PASSIVE_RESISTOR_POWER_FRACTION:
                        out.append(
                            DrcFinding(
                                rule="passive_rating",
                                severity="error",
                                where=f"part {refdes}",
                                detail=(
                                    f"{refdes} dissipates ~{watts:g} W at "
                                    f"{max(currents):g} A — policy "
                                    f"{PASSIVE_MARGIN_POLICY} "
                                    f"r{PASSIVE_MARGIN_REVISION} allows ≤ "
                                    f"{PASSIVE_RESISTOR_POWER_FRACTION:g} × "
                                    f"{float(rated_w):g} W rated"
                                ),
                                objects=tuple(_pin_obj(m) for _n, m in entries),
                            )
                        )
            else:
                dv = (max(known) - min(known)) if known else 0.0
                watts = dv * dv / float(ohm)
                evaluated = True
                if watts > float(rated_w) * PASSIVE_RESISTOR_POWER_FRACTION:
                    out.append(
                        DrcFinding(
                            rule="passive_rating",
                            severity="error",
                            where=f"part {refdes}",
                            detail=(
                                f"{refdes} dissipates ~{watts:g} W across "
                                f"{dv:g} V — policy {PASSIVE_MARGIN_POLICY} "
                                f"r{PASSIVE_MARGIN_REVISION} allows ≤ "
                                f"{PASSIVE_RESISTOR_POWER_FRACTION:g} × "
                                f"{float(rated_w):g} W rated"
                            ),
                            objects=tuple(_pin_obj(m) for _n, m in entries),
                        )
                    )
    if not out and not evaluated:
        return [
            _erc_not_checked(
                "passive_rating",
                (
                    "no placed part parses to a rated passive — no catalog "
                    "description (part_lcsc join) yields a capacitor/resistor"
                    if not specs
                    else "no parsed passive could be checked — its nets "
                    "carry no voltage/current and no pin declared a "
                    "dielectric minimum"
                ),
            )
        ]
    return out


def check_rail_crossing(
    nets: Iterable[dict[str, Any]],
    roles_by_refdes: dict[str, Any] | None = None,
) -> list[DrcFinding]:
    """Pins of two DIFFERENT rails joined on one net — a 3.3 V-domain
    output directly driving a 5 V-domain input. Clean when a member's
    component is a declared translator (``roles: ['level_shifter']`` —
    the part that spans two rails on purpose).

    A pin's rail is ``pcb_pins.meta['rail']``; pins that declare none
    take no part in the comparison. Cannot fire — and says so — when no
    pin declares a rail."""
    roles_by_refdes = roles_by_refdes or {}
    if not any(
        (m.get("meta") or {}).get("rail") for n in nets for m in n.get("members") or []
    ):
        return [
            _erc_not_checked("rail_crossing", "no pin in the design declares a rail")
        ]
    out: list[DrcFinding] = []
    for net in nets:
        members = net.get("members") or []
        by_rail: dict[str, dict[str, Any]] = {}
        for m in members:
            rail = (m.get("meta") or {}).get("rail")
            if rail is not None and str(rail) not in by_rail:
                by_rail[str(rail)] = m
        if len(by_rail) < 2:
            continue
        if any(
            "level_shifter" in (roles_by_refdes.get(str(m.get("refdes"))) or [])
            for m in members
        ):
            continue
        out.append(
            DrcFinding(
                rule="rail_crossing",
                severity="error",
                where=f"net {net['name']}",
                detail=(
                    f"net {net['name']} joins pins of rails "
                    + ", ".join(
                        f"{r!r} ({m['refdes']}/{m['pin']})"
                        for r, m in sorted(by_rail.items())
                    )
                    + " with no level_shifter between them"
                ),
                objects=tuple(_pin_obj(m) for m in by_rail.values()),
            )
        )
    return out


#: How far from an IC's ``power_in`` pin a rail capacitor may sit before
#: ``no_decoupling`` warns — a rule parameter's house default.
#: ``SOURCE NEEDED`` (the spec's own marker): the µs-scale instinct is
#: "as close as the package allows"; 5 mm is the round figure a 0402
#: can share a pour within. Tune per design via the ``max_distance_mm``
#: argument.
DEFAULT_DECOUPLING_DISTANCE_MM = 5.0


def check_no_decoupling(
    nets: Iterable[dict[str, Any]],
    positions: dict[tuple[str, str], tuple[float, float]] | None = None,
    *,
    max_distance_mm: float = DEFAULT_DECOUPLING_DISTANCE_MM,
    roles_by_refdes: dict[str, Any] | None = None,
) -> list[DrcFinding]:
    """Every ``power_in`` pin needs a capacitor on its rail within
    ``max_distance_mm`` — the bypass cap that keeps a switching IC's
    supply stiff. A capacitor here is a member pin of a ``^C\\d+$``
    refdes (the schematic convention), or of a component carrying the
    ``capacitor`` role — a ``passive`` etype alone does NOT count (a
    resistor or inductor on the rail within reach must not silence it),
    and neither does a connector-style refdes (``CN1``, ``CAM1``).

    ``positions`` maps ``(refdes, pin)`` to board mm — the pad geometry
    the run already built; ``roles_by_refdes`` is the instance's
    ``roles`` list. Cannot fire — and says so — when no pin declares
    ``power_in``, or when the board is not yet placed (nothing has a
    position)."""
    nets = list(nets)
    positions = positions or {}
    power_in = [
        (net, m)
        for net in nets
        for m in net.get("members") or []
        if _pin_etype(m) == "power_in"
    ]
    if not power_in:
        return [
            _erc_not_checked(
                "no_decoupling", "no pin in the design declares etype power_in"
            )
        ]
    if not positions:
        return [
            _erc_not_checked(
                "no_decoupling",
                "the board is not placed — pin positions do not exist",
            )
        ]

    def _rail(net: dict[str, Any], pin: dict[str, Any]) -> str | None:
        rail = (pin.get("meta") or {}).get("rail")
        if rail is None:
            rail = (net.get("meta") or {}).get("rail")
        return None if rail is None else str(rail)

    # Rail -> member pins a capacitor could be. Strictly: the C<digits>
    # refdes convention or a `capacitor` component role — a passive-
    # etype resistor/inductor on the rail does not decouple anything.
    roles_by_refdes = roles_by_refdes or {}
    rail_caps: dict[str, list[dict[str, Any]]] = {}
    for net in nets:
        net_rail = str((net.get("meta") or {}).get("rail") or "")
        if not net_rail:
            continue
        for m in net.get("members") or []:
            refdes = str(m.get("refdes") or "")
            if re.fullmatch(r"C\d+", refdes) or "capacitor" in (
                roles_by_refdes.get(refdes) or []
            ):
                rail_caps.setdefault(net_rail, []).append(m)

    out: list[DrcFinding] = []
    unrailed: list[dict[str, Any]] = []
    for net, pin in power_in:
        rail = _rail(net, pin)
        pos = positions.get((str(pin["refdes"]), str(pin["pin"])))
        if rail is None:
            unrailed.append(pin)
            continue
        if pos is None:
            continue  # unplaced pin — covered by the not-checked row below
        nearest: float | None = None
        for c in rail_caps.get(rail, []):
            cpos = positions.get((str(c["refdes"]), str(c["pin"])))
            if cpos is None:
                continue
            d = math.hypot(cpos[0] - pos[0], cpos[1] - pos[1])
            if nearest is None or d < nearest:
                nearest = d
        if nearest is not None and nearest <= max_distance_mm:
            continue
        out.append(
            DrcFinding(
                rule="no_decoupling",
                severity="warn",
                where=f"pin {pin['refdes']}/{pin['pin']}",
                detail=(
                    f"power_in pin {pin['refdes']}/{pin['pin']} (rail "
                    f"{rail!r}) has no capacitor on its rail within "
                    f"{max_distance_mm:g}mm"
                    + (
                        f" — nearest rail capacitor is {nearest:.2f}mm away"
                        if nearest is not None
                        else " — no capacitor on that rail at all"
                    )
                ),
                objects=(_pin_obj(pin),),
            )
        )
    if unrailed:
        out.append(
            _erc_not_checked(
                "no_decoupling",
                "power_in pin(s) "
                + ", ".join(f"{p['refdes']}/{p['pin']}" for p in unrailed)
                + " declare no rail — which rail a capacitor must sit on "
                "cannot be told",
            )
        )
    return out


def run_placement_drc(
    model: dict[str, Any],
    *,
    capability: CapabilityRow,
    outline: list[list[float]] | None = None,
    courtyards: list[Courtyard] | None = None,
    courtyard_bottom: dict[str, bool] | None = None,
    net_rules: dict[str, NetRules] | None = None,
    edge_mates: Sequence[Any] | None = None,
    courtyard_clearance_mm: float | None = None,
) -> list[DrcFinding]:
    """The PLACEMENT-only subset of :func:`run_geometric_drc` — every rule
    that can fire before a single trace is routed, over a model carrying
    only placed pads and AUTHORED fixed copper (no router-derived copper,
    which does not exist yet at end-of-place / start-of-route).

    **Why a separate entry point rather than a flag on
    :func:`run_geometric_drc`.** These are different callers with a
    different contract. ``run_geometric_drc`` is the FINAL check over
    realized copper (``view='drc'``, and the ``netlist_drc_clean`` gate).
    This is the EARLY check the place and route jobs run so an
    already-unmanufacturable placement is flagged UP FRONT rather than
    silently routed and only noticed when a human later asks for the DRC
    view — the exact seam ``handlers/pcb.py``'s own "a check you did not
    RUN is not a check that passed" comment names. The motivating case
    (gr451052): nine ``via_pad_keepout`` errors where authored escape vias
    sit 0.008–0.225mm from the HV507 driver IC's own solder lands against a
    0.090mm floor — pad-and-via geometry, present before routing, that
    nothing ran a check against because
    :func:`run_geometric_drc`'s one caller was the DRC view.

    Deliberately the placement-geometry rules the report names — pad/pad +
    copper/copper clearance (:func:`check_clearance`), pad/via keep-out
    (:func:`check_via_pad_keepout`), via/via keep-out
    (:func:`check_via_via_keepout`), courtyard overlap
    (:func:`check_courtyard_overlap`) and outline containment
    (:func:`check_outline_containment`) — and NONE of the rules that need
    router output (trace width, annular ring, connectivity, unrouted, silk)
    or a drill census this seam does not carry. Every finding it returns is
    therefore a SUBSET of what :func:`run_geometric_drc` would report over
    the same board once routed, never one that view would not also fire —
    so acting on it early can only agree with the later full run.

    ``net_rules`` is threaded into :func:`check_clearance` exactly as
    :func:`run_geometric_drc` threads it (``None`` → capability-floor only).
    A caller gating on ERROR severity needs it for nothing: the ERROR tier
    is ``jlc_min``, which no per-net override ever lowers — an override only
    ever RAISES the required clearance, so the floor errors this returns are
    identical with or without it.
    """
    findings: list[DrcFinding] = []
    findings += check_clearance(model, capability, net_rules=net_rules)
    findings += check_via_pad_keepout(model, capability)
    findings += check_via_via_keepout(model, capability)
    findings += check_outline_containment(model, outline=outline, courtyards=courtyards)
    findings += check_edge_mating(edge_mates, outline=outline)
    if courtyards:
        findings += check_courtyard_overlap(
            courtyards,
            bottom_by_refdes=courtyard_bottom,
            clearance_mm=courtyard_clearance_mm,
            pads=model.get("pads"),
        )
    return findings


__all__ = [
    "DEFAULT_COURTYARD_RADIUS_MM",
    "DEFAULT_DECOUPLING_DISTANCE_MM",
    "PHASES",
    "PHASE_SEVERITY",
    "PIN_CURRENT_ROLES",
    "PIN_ETYPES",
    "SILK_LEGIBILITY_HEIGHT_MM",
    "WORSE_MARGIN_EPS_MM",
    "DrcFinding",
    "FindingDelta",
    "check_annular_ring",
    "check_board_edge_clearance",
    "check_clearance",
    "check_connectivity",
    "check_courtyard_hole",
    "check_courtyard_overlap",
    "check_edge_mating",
    "check_no_decoupling",
    "check_no_driver",
    "check_npth_clearance",
    "check_octilinear",
    "check_outline_containment",
    "check_over_current",
    "check_over_voltage",
    "check_pin_conflicts",
    "check_rail_crossing",
    "check_silk_edge_clearance",
    "check_silk_missing",
    "check_silk_printability",
    "check_synthesized_footprint",
    "check_trace_width",
    "check_unconnected_pins",
    "check_undeclared_pads",
    "check_unrouted",
    "check_via_pad_keepout",
    "check_via_via_keepout",
    "clearance_pairs_indexed",
    "clearance_violations_naive",
    "delta_against",
    "derive_phase",
    "finding_identity",
    "finding_key",
    "process_for_stackup",
    "run_geometric_drc",
    "run_placement_drc",
    "severity_at",
]
