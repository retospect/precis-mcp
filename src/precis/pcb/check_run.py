"""The pcb check-surface run — DRC + ERC over one design.

One call (:func:`run`) judges a design at a phase and returns the findings
with the delta against the board's previous run of the same phase. With
``record=True`` it also persists them under a fresh ``run_id`` (a
``pcb_drc_runs`` row even when clean); without, it writes nothing
(gr491591). ``view='drc'``, ``view='check'``, ``view='gerber'``'s
pre-flight and the ``pcb_review`` job all ask the SAME question through it.
:func:`reuse` answers it from the last recorded run instead when that
run's design-revision stamp still matches (gr496242) — opt-in per call
site, so the worker's "run the check itself" rule is untouched.

This was ``PcbHandler._drc_run`` and its helpers until Reto's 2026-10-16
ruling (3a): ``pcb_review`` runs the check itself rather than trusting
whichever check the caller last ran, and a worker may not import
:mod:`precis.handlers` (the "workers import store and utils, never
handlers" import-linter contract). The handler keeps one-line delegators
(``_drc_run``, ``_build_ir``, ``_outline_from_features``, ``_drc_geometry``,
``_drc_drills``, ``_drc_rule_inputs``, ``_design_phase``) so its other
views and their tests call the same bodies.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from precis.errors import BadInput
from precis.pcb import cost as pcb_cost
from precis.pcb import drc as pcb_drc
from precis.pcb import edge_mate as pcb_edge_mate
from precis.pcb import furniture as pcb_furniture
from precis.pcb import geom as pcb_geom
from precis.pcb import ir as pcb_ir
from precis.pcb import optimize as pcb_optimize
from precis.pcb import padplace
from precis.pcb import planes as pcb_planes
from precis.pcb import profiles as pcb_profiles
from precis.pcb import realize as pcb_realize
from precis.pcb import resolved as pcb_resolved
from precis.pcb import route_state as pcb_route_state
from precis.pcb import session as pcb_session
from precis.pcb import silk as pcb_silk
from precis.pcb.capabilities import CapabilityRow, coating_extent_mm
from precis.pcb.landpattern import place_points
from precis.pcb.rules import NetRules, resolve_net_rules

if TYPE_CHECKING:
    from precis.store import Store

__all__ = [
    "ERC_RULES",
    "DrcRun",
    "build_ir",
    "design_phase",
    "drills",
    "erc_findings",
    "export_date",
    "finding_from_row",
    "geometry",
    "outline_from_features",
    "record_run",
    "reuse",
    "rule_inputs",
    "run",
    "unsupplied_client_findings",
]


def export_date() -> str:
    """Today, UTC, for the silkscreen title block.

    :func:`precis.pcb.silk.build_title_block` refuses to invent a date and
    requires the caller to supply one, which is right for a library: a
    date on a board is a claim about that artwork. The caller is the
    layer that legitimately holds one — this IS the export, so the export
    date is a fact it has rather than a guess.

    There is no stored design revision to pair it with (``pcb_boards``
    carries ``board_id``/``name``/``stackup``/``fold_lines`` and no
    version or timestamp column), so the block renders name + date and
    omits the revision rather than fabricating one.
    """
    return datetime.now(UTC).date().isoformat()


class DrcRun(NamedTuple):
    """What :func:`run` hands its callers. ``run_id`` is ``None`` for a
    read (gr491591: ``view='check'``/``'drc'``/``'gerber'`` record
    nothing); only a ``record=True`` run has an id."""

    run_id: str | None
    findings: list[pcb_drc.DrcFinding]
    pads_only: bool
    delta: pcb_drc.FindingDelta
    #: :meth:`Store.pcb_drc_run_freshness` of ``delta``'s baseline, taken
    #: BEFORE this run records (recording prunes superseded stamps). For
    #: a :func:`reuse` result it is that run's own freshness.
    baseline: dict[str, Any] | None = None
    #: ``True`` when this is the board's last RECORDED run served back
    #: unchanged (gr496242: its design-revision stamp still matches), not
    #: a run just computed. Callers say so in their ack.
    reused: bool = False

    @property
    def diags(self) -> list[Any]:
        """``findings`` as structured Diags — ``pcb.<rule>``-coded
        records with the finding's participant refs (gr522027, S1b).
        DRC and ERC findings share the :class:`DrcFinding` shape, so
        ``erc_findings`` output converts identically."""
        from precis.pcb import diag as pcb_diag

        return [pcb_diag.drc_to_diag(f) for f in self.findings]


def outline_from_features(store: Store, ref_id: int) -> list[list[float]] | None:
    """The board outline, as a plain point polygon — the single
    parse point every view/export reads (gerber, board/fab SVG, DRC,
    silk/fiducial furniture) so an ``outline`` feature's optional
    ``corner_radius_mm`` (fillet + polygonize, see
    :func:`precis.pcb.geom.rounded_polygon`) applies exactly once and
    every downstream consumer inherits the rounded corners for free —
    none of them special-case a radius, they just get more points.
    ``corner_radius_mm`` absent or ``<= 0`` returns the sharp-corner
    path unchanged, byte for byte."""
    for f in store.pcb_features_list(ref_id):
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


def effective_graph(store: Store, ref_id: int) -> dict[str, Any]:
    """The design's EFFECTIVE netlist: the authored graph with the
    latest resolved snapshot laid over it (Slice 7). Every reader that
    names which pin carries which net (pads, ERC, ratsnest, exports)
    takes this, so it agrees with the routed copper. Idempotent:
    :func:`build_ir` applying the snapshot again changes nothing."""
    return pcb_resolved.apply(
        store.pcb_graph(ref_id), store.pcb_netlist_snapshot_latest(ref_id)
    )


def build_ir(
    store: Store,
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

    The IR is built from the EFFECTIVE netlist: the authored graph
    with the latest resolved snapshot laid over it
    (``pcb_netlist_snapshots``, :func:`precis.pcb.resolved.apply`,
    pcb-lazy-netlist-and-checks Slice 7). A view that read the
    authored nets alone would disagree with the routed copper about
    which supply pin carries which electrode.

    ``with_fixed_copper`` adds the authored vias the placer keeps solder
    lands off (``pcb_place``'s own hydration) — for a pose check.
    ``fixed_copper`` supplies that list directly instead (a HYPOTHETICAL
    board: ``op='move'`` of a generator group judges the carried copper
    at its new pose before anything is written)."""
    board_id = (graph.get("board") or {}).get("board_id")
    ir = pcb_session.build_ir(
        pcb_resolved.apply(graph, store.pcb_netlist_snapshot_latest(ref_id)),
        mounting_holes=pcb_session.mounting_holes_from_features(
            store.pcb_features_list(ref_id)
        ),
        footprints_by_lcsc=store.pcb_footprints_for(ref_id),
        local_footprints_by_name=store.pcb_local_footprints_for(ref_id),
        fixed_copper=(
            fixed_copper
            if fixed_copper is not None
            else store.pcb_fixed_copper_list(int(board_id))
            if with_fixed_copper and board_id is not None
            else None
        ),
    )
    return ir


def geometry(
    store: Store, ref_id: int, layers: list[str]
) -> tuple[
    list[dict[str, Any]],
    dict[str, list[tuple[float, float]]],
    dict[str, list[tuple[float, float]]],
    dict[str, dict[str, Any]],
]:
    """DRC's pads (the same IR-sourced geometry :meth:`PcbHandler._drc_pads`
    builds, off ONE ``build_ir`` call here rather than two independent
    ones) plus each instance's courtyard POLYGON in local-frame
    coordinates, keyed by refdes, plus each instance's zero-clearance
    pad-union BODY polygon in the same local frame (gr529963 — what the
    edge-clearance and hole keep-out rules measure), plus the
    refdes-keyed footprint map the pad build consumed (Slice 2's
    ``undeclared_pad`` rule needs it to tell "footprint cached, every
    pad declared" from "footprint never cached — cannot check").

    **The shape is not computed here, and since 2026-08-30 there is
    only one of it.** :func:`precis.pcb.ir.instance_courtyard_polygon`
    — the union of the part's own pad outlines, offset outward —
    is what the placer reserves
    (``OptimizeEngine._keepout_poly``/``seed_placement``), what
    ``courtyard_overlap`` and ``outline_containment`` check, and what
    :mod:`precis.pcb.silk` draws. Three consumers, one definition,
    which is the whole point of the courtyard-polygon work.

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
    graph = effective_graph(store, ref_id)
    ir = build_ir(store, ref_id, graph)
    footprints = pcb_session.footprints_by_refdes(
        ir,
        store.pcb_footprints_for(ref_id),
        local_footprints_by_name=store.pcb_local_footprints_for(ref_id),
        local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
    )
    pads = pcb_realize.pads_for_ir(ir, layers, footprints) if graph.get("nets") else []
    polys = pcb_ir.instance_courtyard_polygons(
        ir,
        clearance_mm=pcb_ir.COURTYARD_CLEARANCE_MM,
        fallback_half_extent_mm=pcb_cost.COURTYARD_MIN_SEPARATION_MM / 2.0,
    )
    # gr529963 (ruling Reto 2026-10-28): the same pad-union shape at ZERO
    # clearance — the body the edge-clearance and hole keep-out rules
    # measure (the fab bar), where the courtyard above is the
    # between-parts reservation. Same call shape, same fallback, only the
    # offset differs.
    body_polys = pcb_ir.instance_courtyard_polygons(
        ir,
        clearance_mm=0.0,
        fallback_half_extent_mm=pcb_cost.COURTYARD_MIN_SEPARATION_MM / 2.0,
    )
    return (
        pads,
        {str(ir.instance_refdes[i]): polys[i] for i in range(ir.n_instances)},
        {str(ir.instance_refdes[i]): body_polys[i] for i in range(ir.n_instances)},
        footprints,
    )


def drills(ir: pcb_ir.PcbIR) -> list[dict[str, Any]]:
    """``model['drills']`` for :func:`precis.pcb.drc.
    check_npth_clearance` — the IR's mounting holes (``mounting_hole``
    features plus every instance's footprint holes at its current
    pose), the mechanical holes that check exists to protect (a screw hole is never a
    soldered lead, so it is unplated by construction). Before this,
    ``PcbHandler._render_drc`` never populated ``drills`` at all, so
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
    ONE source — see :meth:`PcbHandler._render_drc`'s own comment) carries each
    pad's ``drill``/``slot`` on the pad itself, where the drilled-pad
    rules already read it (:func:`precis.pcb.drc.check_annular_ring`'s
    ``_drilled_pad_ring_items``, ``clearance_pairs_indexed``'s
    "spans every layer" signal). ``check_npth_clearance`` — this
    list's consumer — only wants the unplated holes anyway. The
    plated-hole side for the FAB model is :meth:`PcbHandler._pad_drills`."""
    return [
        {"x": h.x, "y": h.y, "dia_mm": h.drill_mm, "plated": h.plated}
        for h in ir.mounting_holes
    ]


def rule_inputs(
    design: dict[str, Any],
    courtyard_local: dict[str, list[tuple[float, float]]],
    capability: CapabilityRow,
    body_local: dict[str, list[tuple[float, float]]] | None = None,
) -> tuple[
    list[pcb_drc.Courtyard],
    dict[str, bool],
    dict[str, NetRules],
    dict[str, float],
    list[pcb_drc.Courtyard] | None,
]:
    """``(courtyards, courtyard_bottom, net_rules, net_voltages, bodies)``
    — the per-design inputs the geometric rules take besides the copper
    model. Shared by :func:`run` and :meth:`PcbHandler._validity_findings`
    so the two cannot judge a board by different courtyards or class rules.
    ``body_local`` (gr529963) is :func:`geometry`'s zero-clearance
    pad-union body per refdes, placed through the SAME affine path as the
    courtyard below; ``None`` keeps the edge/hole rules measuring
    courtyards, exactly as before (the "silent about a thing this module
    was never told" contract the drc.py runners document)."""
    # The part's own courtyard POLYGON (see :func:`geometry`),
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
                mirrored=padplace.is_bottom_instance(i),
            ),
        )
        for i in design["instances"]
        if i["x"] is not None and i["y"] is not None
    ]
    # The zero-clearance body (gr529963), placed through the identical
    # affine path — a body rotated by a different convention would clear
    # an edge its own copper crowds.
    bodies: list[pcb_drc.Courtyard] | None = None
    if body_local is not None:
        bodies = [
            (
                str(i["refdes"]),
                place_points(
                    body_local.get(str(i["refdes"])) or _fallback,
                    cx=float(i["x"]),
                    cy=float(i["y"]),
                    rot_deg=float(i.get("rot") or 0.0),
                    mirrored=padplace.is_bottom_instance(i),
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
        str(i["refdes"]): padplace.is_bottom_instance(i) for i in design["instances"]
    }
    net_rules = pcb_session.clearance_net_rules(design, capability)
    # §E-1's PAIRWISE voltage term. Only annotated nets are in the map —
    # a net missing here is "not annotated", never 0 V, and
    # `check_clearance` reports the difference rather than inventing a
    # potential (`pcb-missing-constraint-classes.md` §E-1).
    net_voltages = {
        str(n["name"]): float(n["working_voltage_v"])
        for n in design["nets"]
        if n.get("working_voltage_v") is not None
    }
    return courtyards, courtyard_bottom, net_rules, net_voltages, bodies


def design_phase(design: dict[str, Any]) -> str:
    """The phase the check surface judges at (Slice 8): the author's
    claim (``pcb_boards.meta['phase']`` via ``op='phase'``) when one is
    recorded, else :func:`precis.pcb.drc.derive_phase`'s read of what
    exists. ``fab`` can ONLY come from the authored claim — nothing
    observable distinguishes "all routed" from "tape-out ready"."""
    board = design.get("board") or {}
    authored = str((board.get("meta") or {}).get("phase") or "")
    if authored in pcb_drc.PHASES:
        return authored
    return pcb_drc.derive_phase(
        placed=any(
            i.get("x") is not None and i.get("y") is not None
            for i in design["instances"]
        ),
        routed=pcb_route_state.started(design.get("route_status") or {}),
    )


def _topo_pin_assign(store: Store, ref_id: int) -> dict[tuple[str, str], str]:
    """The newest topo-place run's pin assignment, ``(refdes, pin) ->
    slot name`` — for :func:`precis.pcb.drc.check_unconnected_pins`'
    ``topo_assign`` (gr520797). Read off the newest ``placer='topo'``
    job's ``meta.topo_run.files['pins.json']['pins']`` (``{name:
    {refdes, pin}}``; a numeric pin lands back as a str). ``{}`` when no
    stored run assigns anything."""
    for job in store.recent_refs(["job"], parent_id=ref_id, limit=50):
        topo_run = (job.meta or {}).get("topo_run") or {}
        if not topo_run:
            continue
        pins = ((topo_run.get("files") or {}).get("pins.json") or {}).get("pins") or {}
        return {
            (str(row.get("refdes")), str(row.get("pin"))): str(name)
            for name, row in pins.items()
            if isinstance(row, dict)
        }
    return {}


def _baseline_freshness(
    store: Store, board_id: int, run_id: str | None
) -> dict[str, Any] | None:
    if run_id is None:
        return None
    ref_id = store.pcb_board_ref_id(board_id)
    return store.pcb_drc_run_freshness(ref_id, run_id)


#: Phases whose CLAIMED eval narrows the run: a claimed ``netlist`` eval
#: never reads ``pcb_copper`` or the geometric rules, a claimed ``placed``
#: eval checks pads only. Recorded runs are stamped with the scope their
#: claim produced (``meta['drc_scopes']``) so :func:`reuse` never answers
#: a full-pass question with a narrowed run — or the reverse.
_SCOPED_PHASES = ("netlist", "placed")


def _run_scope(claimed_phase: str | None) -> str:
    """``meta['drc_scopes']`` value for a run: the claimed phase when it
    narrows the run, else ``"full"`` — ``routed``/``fab`` claims and an
    unclaimed eval all run the full pass."""
    return claimed_phase if claimed_phase in _SCOPED_PHASES else "full"


def record_run(
    store: Store,
    board_id: int,
    findings: list[pcb_drc.DrcFinding],
    pads_only: bool,
    phase: str | None = None,
    *,
    record: bool = False,
    claimed_phase: str | None = None,
) -> DrcRun:
    """Diff one run against the last recorded one and, with ``record``,
    persist it — the tail of :func:`run` shared by every phase-scoped
    branch. Without ``record`` nothing is written and ``run_id`` is
    ``None`` (gr491591: a get is read-only).

    ``phase`` keys the delta BASELINE: the run diffs only against the
    previous run of the same phase and becomes that phase's baseline —
    a ``netlist`` eval's zero copper findings must never read to the
    next full run as "everything resolved".

    ``claimed_phase`` is the claim that produced the run (:func:`run`'s
    ``claimed_phase``): with ``record`` it is stamped into
    ``meta['drc_scopes']`` so a later :func:`reuse` can tell a narrowed
    run from a full one under the same phase key."""
    prev_run_id, prev_rows = store.pcb_drc_previous_run(board_id, phase=phase)
    baseline = _baseline_freshness(store, board_id, prev_run_id)
    run_id: str | None = None
    if record:
        run_id = uuid.uuid4().hex
        store.pcb_write_drc_findings(
            board_id,
            run_id,
            [f.to_row() for f in findings],
            pads_only=pads_only,
            phase=phase,
            scope=_run_scope(claimed_phase),
        )
    delta = pcb_drc.delta_against(prev_run_id, prev_rows, findings)
    return DrcRun(run_id, list(findings), pads_only, delta, baseline)


#: The rules :func:`erc_findings` adds — the §1h ERC netlist rules,
#: ``view='check'``'s ``erc=True`` superset over ``view='drc'``. Every
#: recording path today (``op='check'``, ``pcb_review``) runs ``erc=True``,
#: so a recorded run is always the superset; :func:`reuse` strips these
#: for a caller that asked without them.
ERC_RULES = frozenset(
    {
        "pin_conflict",
        "no_driver",
        "over_voltage",
        "rail_crossing",
        "no_decoupling",
        "over_current",
        "unsupplied_client",
        "passive_rating",
    }
)


def erc_findings(
    design: dict[str, Any],
    graph: dict[str, Any],
    pads: list[dict[str, Any]],
) -> list[pcb_drc.DrcFinding]:
    """Slice 5's netlist ERC — the §1h rules over the §1i pin
    semantics, one finding list with the geometric run's. Pin
    positions come from the pads the run already placed (real
    footprint geometry where cached), not instance centroids."""
    roles_by_refdes = {
        str(i.get("refdes")): list(i.get("roles") or []) for i in design["instances"]
    }
    positions = {
        (str(p["refdes"]), str(p["pin"])): (float(p["x"]), float(p["y"]))
        for p in pads
        if p.get("refdes") and p.get("pin") is not None
    }
    nets = graph.get("nets") or []
    return (
        pcb_drc.check_pin_conflicts(nets)
        + pcb_drc.check_no_driver(
            nets,
            instances=graph.get("instances") or [],
            roles_by_refdes=roles_by_refdes,
        )
        + pcb_drc.check_over_voltage(nets, design.get("net_classes"))
        + pcb_drc.check_rail_crossing(nets, roles_by_refdes)
        + pcb_drc.check_no_decoupling(nets, positions, roles_by_refdes=roles_by_refdes)
        + pcb_drc.check_over_current(nets)
        + unsupplied_client_findings(graph)
        + pcb_drc.check_passive_rating(
            nets, graph.get("instances") or [], design.get("net_classes")
        )
    )


def unsupplied_client_findings(graph: dict[str, Any]) -> list[pcb_drc.DrcFinding]:
    from precis.pcb import matching as pcb_matching

    clients, supplies, roles = pcb_matching._matching_model(graph, [])
    return pcb_drc.check_unsupplied_client(clients, supplies, roles)


def finding_from_row(row: dict[str, Any]) -> pcb_drc.DrcFinding:
    """Rehydrate one persisted ``pcb_drc_findings`` row into the
    :class:`~precis.pcb.drc.DrcFinding` it was written from — the inverse
    of :meth:`~precis.pcb.drc.DrcFinding.to_row` (``location`` is
    ``where``). ``finding_key`` recomputes to the row's own key, so a
    delta of these findings against the run they came from reads all
    ``still``."""
    return pcb_drc.DrcFinding(
        rule=str(row["rule"]),
        severity=str(row["severity"]),
        where=str(row.get("location") or ""),
        detail=str(row.get("detail") or ""),
        objects=tuple(dict(o) for o in row.get("objects") or ()),
        margin_mm=(None if row.get("margin_mm") is None else float(row["margin_mm"])),
    )


def reuse(
    store: Store,
    ref_id: int,
    design: dict[str, Any],
    *,
    eval_phase: str | None = None,
    eval_phase_claimed: bool = True,
    erc: bool = False,
) -> DrcRun | None:
    """The board's last recorded run served back when it still describes
    this design (gr496242): the phase baseline ``run`` would have been
    recorded under, at the scope this eval's claim would produce, whose
    design-revision stamp still matches the live design
    (:meth:`Store.pcb_drc_run_freshness`). ``None`` on any miss — no run,
    a stale one, or a scope mismatch — and the caller falls through to
    :func:`run`. Read-only: records nothing (gr491591).

    The delta is the stored run against itself — ``0 new, N still, 0
    resolved`` IS the claim "the board is unchanged since that run"."""
    board = design.get("board")
    if board is None:
        return None
    phase = eval_phase or design_phase(design)
    authored = str((board.get("meta") or {}).get("phase") or "")
    if authored not in pcb_drc.PHASES:
        authored = ""
    claimed = (eval_phase if eval_phase_claimed else None) or authored
    run_id, rows = store.pcb_drc_previous_run(int(board["board_id"]), phase=phase)
    if run_id is None:
        return None
    fresh = store.pcb_drc_run_freshness(ref_id, run_id)
    if (
        fresh is None
        or fresh["stale"]
        or (fresh.get("scope") or "full") != _run_scope(claimed)
    ):
        return None
    if not erc:
        rows = [r for r in rows if r.get("rule") not in ERC_RULES]
    findings = [finding_from_row(r) for r in rows]
    delta = pcb_drc.delta_against(run_id, rows, findings)
    return DrcRun(
        run_id,
        findings,
        bool(fresh.get("pads_only")),
        delta,
        fresh,
        reused=True,
    )


def run(
    store: Store,
    ref_id: int,
    design: dict[str, Any],
    *,
    eval_phase: str | None = None,
    eval_phase_claimed: bool = True,
    erc: bool = False,
    record: bool = False,
) -> DrcRun | None:
    """One check-surface run over ``design`` (which must have a board):
    with ``record`` the findings persist under a fresh ``run_id`` (a
    ``pcb_drc_runs`` row even when clean) — only the explicit paths
    pass it (``op='check'``, ``op='review'``; gr491591, Reto
    2026-10-16) — plus the delta against the board's last recorded
    run (:func:`precis.pcb.drc.delta_against`, by ``finding_key``).
    ``None`` when there is nothing real to check — no realized copper
    and every placed pad a synthesized bound.

    ``eval_phase`` is ``view='check'``'s phase axis (Slice 8): the
    phase to judge AT — ``args.phase``'s explicit override, or the
    authored ``op='phase'`` claim, or the derived default when neither
    caller supplies one. ``netlist`` runs only the netlist/flag rules
    and never touches ``pcb_copper``; ``placed`` is the pads-only run
    (pad geometry, no copper); ``routed``/``fab`` the full geometric
    pass. ``view='drc'`` calls this with ``None``: same run it has
    always done.

    ``eval_phase_claimed=False`` marks ``eval_phase`` as the derived
    default (``view='check'`` with no ``args.phase``): it tiers
    severities and keys the delta, but never scopes the run — only an
    ``args.phase`` or authored claim makes ``placed`` pads-only
    (gr488909: taz15's 749 imported tracks went unchecked at its
    derived ``placed``).

    ``erc=True`` adds Slice 5's netlist ERC rules — ``view='check'``
    passes it (the check surface is the superset); ``view='drc'`` is
    the preset and does not.

    Split out of :meth:`PcbHandler._render_drc` so ``view='gerber'`` asks the SAME
    question the DRC view answers before it hands over a fab bundle,
    rather than a second, narrower one."""
    board = design["board"]
    stackup = board["stackup"]
    board_profile_name = pcb_profiles.board_profile(board.get("meta"))
    board_profile_rules = pcb_profiles.profile_rules(board_profile_name)
    try:
        capability = pcb_session.board_capability(
            stackup, board_profile_name, meta=board.get("meta")
        )
    except ValueError as exc:
        refusal = BadInput(f"pcb: {exc}")
        from precis.pcb import diag as pcb_diag

        pcb_diag._stamp(
            refusal,
            pcb_diag.error_diag(refusal, op="check", code="pcb.check-capability"),
        )
        raise refusal from exc
    layer_names = [str(layer.get("name")) for layer in stackup]
    # Pads come from the IR, which is the SAME source the router and the
    # realizer use (precis.pcb.landpattern via ir.pin_point). A second
    # pad source is how this build's recurring defect works — one rule,
    # two call sites, drifted — and connectivity is precisely the check
    # that would be fooled by pads in the wrong place.
    pads, courtyard_local, body_local, footprints = geometry(store, ref_id, layer_names)

    # The phase this board is judged at: an explicit ``eval_phase``,
    # the authored claim, or the derived default — in that order.
    phase = eval_phase or design_phase(design)
    undeclared_unchecked = [
        str(i.get("refdes"))
        for i in design["instances"]
        if (i.get("part_lcsc") or i.get("footprint"))
        and str(i.get("refdes")) not in footprints
    ]

    graph = effective_graph(store, ref_id)
    topo_assign = _topo_pin_assign(store, ref_id)
    board_id = int(board["board_id"])
    authored_phase = str((board.get("meta") or {}).get("phase") or "")
    if authored_phase not in pcb_drc.PHASES:
        authored_phase = ""
    # The phase a caller CLAIMED — args.phase or the authored
    # op='phase' value — is what may suppress work ("judge as if at
    # this phase"). A merely derived phase never hides copper that
    # exists: a board seeded with tracks but nothing placed still
    # gets them checked.
    claimed_phase = (eval_phase if eval_phase_claimed else None) or authored_phase
    if claimed_phase == "netlist":
        # Phase gate (Slice 8): netlist phase was ASKED for (args or
        # an authored claim), so only the rules needing no geometry
        # and no copper run — ``pcb_copper`` is never even read.
        # ``unrouted`` joins the set here: its input is
        # ``pcb_routes`` status (a route row, not copper) and at this
        # phase it reports as `todo`, a work item. A merely DERIVED
        # netlist board (nothing placed yet) does not take this
        # branch — it may still carry seeded copper to check.
        bail_model = {"layers": layer_names, "pads": pads}
        findings = (
            pcb_drc.check_unconnected_pins(
                graph.get("unconnected"), phase=phase, topo_assign=topo_assign
            )
            + pcb_drc.check_undeclared_pads(bail_model, unchecked=undeclared_unchecked)
            + pcb_drc.check_synthesized_footprint(bail_model)
            + pcb_drc.check_unrouted(
                [
                    {"net": r["name"], "note": r.get("note")}
                    for r in pcb_route_state.route_state(
                        store, ref_id, drc=False
                    ).pending
                ]
            )
        )
        # The §1h ERC rules are netlist rules — at netlist phase they
        # are exactly what runs.
        if erc:
            findings += erc_findings(design, graph, pads)
        # A claimed netlist eval ALWAYS answers with a run — even with
        # zero findings it is "0 findings at netlist" (and, recorded,
        # the phase's fresh baseline), never "nothing could run".
        return record_run(
            store,
            board_id,
            findings,
            True,
            phase="netlist",
            record=record,
            claimed_phase=claimed_phase,
        )

    copper = [] if claimed_phase == "placed" else store.pcb_copper_list(board_id)
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
        # No geometry worth checking — but Slice 2's netlist-level
        # rules need none: a board whose only signal is an unmarked
        # unconnected pin, a floating `must_terminate`, or an
        # uncached footprint gets a real run recording exactly that
        # rather than "no realized copper yet" hiding the only
        # complaint there is. `check_synthesized_footprint` runs too:
        # its synthesized/pin_unmatched pad flags also need no real
        # geometry, and a join-failed or uncached part's
        # `pin_name_mismatch`/`synthesized_footprint` is exactly the
        # complaint this bail used to hide.
        bail_model = {"layers": layer_names, "pads": pads}
        findings = (
            pcb_drc.check_unconnected_pins(
                graph.get("unconnected"), phase=phase, topo_assign=topo_assign
            )
            + pcb_drc.check_undeclared_pads(bail_model, unchecked=undeclared_unchecked)
            + pcb_drc.check_synthesized_footprint(bail_model)
        )
        # Slice 5 ERC — netlist rules, need no geometry either.
        if erc:
            findings += erc_findings(design, graph, pads)
        if not findings:
            # ``view='drc'`` keeps its "no realized copper yet" answer;
            # a check run (an explicit phase eval) still returns a run
            # — a run that ran clean is a verdict, not an absence.
            if eval_phase is None:
                return None
            return record_run(
                store,
                board_id,
                findings,
                True,
                phase=phase,
                record=record,
                claimed_phase=claimed_phase,
            )
        return record_run(
            store,
            board_id,
            findings,
            True,
            phase=phase,
            record=record,
            claimed_phase=claimed_phase,
        )
    pads_only = not copper

    ref = store.get_ref(kind="pcb", id=ref_id)
    slug = ref.slug if ref is not None and ref.slug else str(ref_id)
    ir = build_ir(store, ref_id, graph)
    instance_sides = {
        str(i.get("refdes")): str(i.get("layer") or "top") for i in design["instances"]
    }
    vias = [c for c in copper if c.get("ctype") == "via"]
    outline = outline_from_features(store, ref_id)
    # A board whose copper is all authored (``--copper fixed`` import,
    # gr492989) was never realized, so nothing poured its plane and
    # pour-carried nets: their pads reach each other only through the
    # fill, and a model without it reads each such net as split. Pour
    # them the way realize would, before the furniture cuts its antipads.
    # A fresh IR: plane promotion is L1 state the route job re-applies
    # (:func:`precis.pcb.session.apply_plane_assignments`), and ``ir``
    # above stays the unpromoted one every other reader here was built on.
    fixed = [c for c in copper if c.get("fixed")]
    if fixed and outline and not any(c.get("ctype") == "pour" for c in copper):
        pour_ir = build_ir(store, ref_id, graph)
        pour_ir.outline = [(float(x), float(y)) for x, y in outline]
        pcb_session.apply_plane_assignments(pour_ir, store.pcb_planes_list(ref_id))
        copper = copper + pcb_realize.fixed_copper_pours(
            pour_ir,
            pcb_realize.RealizeConfig(
                fab_caps=capability,
                class_rules=graph.get("net_classes"),
                thermal=pcb_planes.thermal_spec(board_profile_rules),
            ),
            footprints,
            fixed,
        )
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
        pcb_furniture.board_furniture(
            ir,
            pads,
            vias=vias,
            instance_sides=instance_sides,
            polarized=pcb_session.polarized_refdes(design),
            unpolarized=pcb_session.unpolarized_refdes(design),
            copper=copper,
            outline=outline,
            layer_names=layer_names,
            slug=slug,
            clearance_mm=resolve_net_rules(
                "", layer_is_outer=True, fab_caps=capability
            ).clearance_mm,
            capability=capability,
            date=export_date(),
            profile=board_profile_name,
        )
    )
    model = {
        "layers": layer_names,
        "copper": copper,
        "pads": pads,
        "drills": drills(ir),
        "silkscreen": silk_draws,
        "soldermask_expansion_mm": pcb_silk.soldermask_expansion_mm(capability),
        # Bend zones the builder authored (gr520329) — `check_bend_zones`
        # reads them plus the placed `instances` for the part-in-zone rule.
        "bend_zones": pcb_session.fab_layers_from_features(
            store.pcb_features_list(ref_id)
        ).get("bend_zones", []),
        "instances": design["instances"],
        # Pours the router will make but has not (fixed source copper
        # imported without them): connectivity counts them (gr492989).
        "pending_pours": [
            {"net": a.net, "layer": a.layer, "region": a.region}
            for a in pcb_planes.resolve_assignments(
                stackup,
                store.pcb_planes_list(ref_id),
                [str(n["name"]) for n in graph.get("nets") or []],
            )[0]
        ],
    }
    courtyards, courtyard_bottom, net_rules, net_voltages, bodies = rule_inputs(
        design, courtyard_local, capability, body_local
    )
    # Fixed source copper routes a net with no ``pcb_routes`` row
    # (gr492989): judge "unrouted" by the copper, not the status alone.
    # A net with fewer than 2 pins is nothing (Reto 2026-10-18): a route run
    # writes it 'realized', but a fixed-copper import never runs one, so the
    # same ``net_member_counts`` rule drops it here, with no finding.
    pending = pcb_route_state.route_state(store, ref_id, drc=False).pending
    dangling = {
        str(ir.net_name[i]) for i, n in pcb_ir.net_member_counts(ir).items() if n < 2
    }
    settled = (pcb_drc.nets_routed_by_copper(model) if pending else set()) | dangling
    findings = pcb_drc.run_geometric_drc(
        model,
        capability=capability,
        outline=outline,
        courtyards=courtyards,
        courtyard_bottom=courtyard_bottom,
        net_rules=net_rules,
        net_voltages=net_voltages,
        bodies=bodies,
        unrouted=[
            {"net": r["name"], "note": r.get("note")}
            for r in pending
            if r["name"] not in settled
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
        edge_mates=pcb_edge_mate.records(ir),
        unconnected=graph.get("unconnected"),
        keepouts=pcb_session.keepouts_from_features(store.pcb_features_list(ref_id)),
        phase=phase,
        topo_assign=topo_assign,
        courtyard_clearance_mm=pcb_ir.COURTYARD_CLEARANCE_MM,
        undeclared_unchecked=undeclared_unchecked,
        profile_rules=board_profile_rules,
    )
    # Parylene-coater size (gr414481): drc.py knows no components, so
    # the EWOD gate (an `ewod_pad_array` generator) is decided here.
    findings += pcb_drc.check_coating_extent(
        outline,
        ewod_components=[
            str(n)
            for n, g in store.pcb_generators_for(ref_id).items()
            if g.get("generator") == "ewod_pad_array"
        ],
        extent_mm=coating_extent_mm(),
    )
    if erc:
        findings += erc_findings(design, graph, pads)
    # Read the previous run BEFORE writing this one — it is what this
    # run is compared against; afterwards it would be this run itself.
    return record_run(
        store,
        board_id,
        findings,
        pads_only,
        phase=phase,
        record=record,
        claimed_phase=claimed_phase,
    )
