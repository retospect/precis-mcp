"""The pure nucleic-acid DRC pass — every ``chain_*`` finding that needs
only the tree.

Called once from :func:`precis_se.drc.drc`, the same way
:func:`precis_se.fasten.findings` is: this module owns the numbers and the
reasoning, ``drc.py`` owns the one list. Pure over the tree — no store, no
compute budget — which is why the handler-side rules of the same family
(the ``material`` Lp row that re-emits ``chain_floppy``, the ViennaRNA fold
checks) are **not** here; they are appended in
:func:`precis_se.handler._render_drc` after this pass, like
:func:`precis_se.precedent.findings`.

Rule names are flat snake_case, the house convention — the dotted spelling
in earlier drafts of this domain was an artefact.

What each rule is, and why it is at the tier it is:

- ``chain_bend`` — the centre line bends tighter than the helix tolerates
  (:func:`precis_chain.curvature.min_bend_radius_violations`). **Error**
  when the design authored its own ``min_bend_radius``; **warn** on the
  coded Lp/5 default, because that default is a stiffness estimate and a
  designer who has not spoken has not yet made a claim.
- ``chain_twist_register`` (warn) — the helix's run is not a whole number
  of lattice repeats rolled back into register
  (:func:`precis_chain.register.commensurate`). Warn, not error: an
  off-register run is strained, not impossible, and insertions/deletions
  are the designer's answer. Only a helix in a connected set of two or more
  is checked: a lone helix has no neighbour to keep register with.
- ``chain_twist_global`` (warn) — per connected set of lattice helices
  (linked by strands with domains on both), a helix whose real-twist
  residual (:func:`precis_se.chain.layout.twist_residual`: the molecule's
  winding at the base motif's twist against the lattice twist it is drawn
  at) exceeds half one base's twist — past that, one more insertion or
  deletion would bring it closer. The detail names each helix's residual and
  the insertions/deletions that would cancel it.
- ``chain_clash`` (warn) — two segments' swept tubes are closer than the
  design's ``min_gap`` (:func:`precis_chain.clash.clashes`, the kernel's
  exact broad phase). Consecutive segments of one helix and the two
  segments a crossover joins are *supposed* to touch and are skipped.
- ``chain_loop_short`` (error) — a loop cannot reach: the gap between the
  two backbone exits exceeds ``(n + 1) * c + tol``, the kernel's
  ``(n+1)``-bond contour convention plus the spec's ``tol``, which is the
  groove-asymmetry shortfall two *different* helices cannot route around
  (:func:`precis_se.chain.nucleic.backbone_frustration_m`; zero within one
  helix). **This IS the crossover register check**: a 0-nt crossover has
  one bond of reach, so it is feasible only in the narrow azimuthal window
  where both helices' backbones face the other
  (:func:`precis_se.chain.nucleic.crossover_window_rad`), and the finding
  names the landing offsets that would fit.
- ``chain_loop_slack`` (info) — the opposite end: a loop with far more
  contour than it needs. Free nucleotides, and a floppy joint where the
  design may have meant a rigid one.
- ``chain_floppy`` (info) — a single-stranded span longer than ssDNA's
  coded persistence length. The pure pass uses the coded default and says
  so; the handler-side pass re-emits against a ``material`` row when the
  design has one.
- ``chain_dangling_domain`` (error) — a domain naming a block that is not
  there (or is not a strand/helix), an offset past the helix's end, or a
  route whose ``ord`` sequence has a hole or a repeat.
- ``chain_occupancy`` (error) — two parallel occupants of one offset, or
  three or more (:mod:`precis_se.chain.pairing`; triplexes are reported,
  not modelled).
- ``chain_pairing_geometry`` (error) — a declared Leontis–Westhof family
  over two bases that family does not accommodate
  (:func:`precis_se.chain.nucleic.pair_allowed`).
- ``chain_pairing_disagree`` (error) — two domains at one offset declare
  *different* Leontis–Westhof families (:mod:`precis_se.chain.pairing`'s
  ``OffsetOccupancy.declarations``); the first-declaring occupant still wins
  for every other purpose (``geometry``), this rule only reports that a
  second domain said something else.
- ``chain_unpaired`` (info) / ``chain_unpaired_run`` (warn) — offsets the
  ``unpair`` op marked: one stays at its duplex position and is named; a
  run of two or more is a folding question, not a rigid duplex.
  ``chain_unpaired_stray`` (warn) — a mark on an offset that is no longer a
  two-strand pair, so it does nothing.
- ``chain_malformed`` (error) — a stored ``chain`` record that does not fit
  the schema at all. Not in the spec's list; it is the same
  defence-in-depth every other stored jsonb payload in se gets
  (``malformed_joint``), and without it a hand-corrupted record would
  crash the read path instead of surfacing.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from typing import Any

import numpy as np

from precis.cad.vec import rotation
from precis.utils.units import format_quantity
from precis_chain.clash import capsule_distance, clashes
from precis_chain.curvature import min_bend_radius_violations
from precis_chain.loop import contour, loop_slack_energy
from precis_chain.register import commensurate
from precis_se.chain import nucleic
from precis_se.chain.layout import (
    HelixGeometry,
    helix_geometry,
    segment_capsule,
    segment_ranges,
    twist_residual,
    units_per_segment,
)
from precis_se.chain.pairing import (
    CROWDED,
    PAIRED,
    PARALLEL,
    UNPAIRED,
    OffsetOccupancy,
    Pairing,
    derive_pairing,
    watson_crick,
)
from precis_se.chain.vocab import (
    HELIX_ROLE,
    STRAND_ROLE,
    ChainError,
    DomainSpec,
    chain_role,
    group_domains,
    validate_chain,
)
from precis_se.validate import ValidationIssue

#: A loop whose end-to-end extension is below this fraction of its own
#: contour is reported as slack. Half: at that extension a Gaussian chain
#: still has most of its conformational entropy, so the loop is genuinely
#: free rather than merely un-taut, and a crossover (extension ≈ 0.8 of
#: one bond's contour) is comfortably above it.
LOOP_SLACK_FRACTION = 0.5


def _len(value: float) -> str:
    """A metre length through the shared formatter — ``2.5 nm``, never a
    bare float (the same reason :func:`precis_se.handler._fmt_len` exists:
    a nucleic-acid design is all e-9)."""
    return format_quantity(value, "length")


def _helix_geometries(
    tree: Any, findings: list[ValidationIssue]
) -> dict[str, HelixGeometry]:
    """Realise every helix block's geometry, turning a record this pass
    cannot use into a ``chain_malformed`` finding rather than an
    exception."""
    out: dict[str, HelixGeometry] = {}
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        role = chain_role(node)
        if role is None:
            continue
        try:
            validate_chain(node.chain, what=f"{name}.chain")
        except ChainError as exc:
            findings.append(
                ValidationIssue(
                    rule="chain_malformed",
                    subject=name,
                    detail=f"stored chain record does not fit the schema: {exc}",
                    severity="error",
                )
            )
            continue
        if role != HELIX_ROLE:
            continue
        try:
            out[name] = helix_geometry(node)
        except (ChainError, KeyError, ValueError) as exc:
            findings.append(
                ValidationIssue(
                    rule="chain_malformed",
                    subject=name,
                    detail=f"helix geometry cannot be realised: {exc}",
                    severity="error",
                )
            )
    return out


def _bend_findings(
    geoms: dict[str, HelixGeometry], findings: list[ValidationIssue]
) -> None:
    for name in sorted(geoms):
        geom = geoms[name]
        bad = min_bend_radius_violations(geom.path.points, geom.min_bend_radius_m)
        if bad.size == 0:
            continue
        source = (
            "authored min_bend_radius"
            if geom.bend_authored
            else f"coded default Lp/5 = {_len(geom.min_bend_radius_m)}"
        )
        findings.append(
            ValidationIssue(
                rule="chain_bend",
                subject=name,
                detail=(
                    f"centre line bends tighter than {_len(geom.min_bend_radius_m)} "
                    f"at {bad.size} of {len(geom.path.points)} sample(s) "
                    f"({source}) — a duplex bent this tightly is paying real "
                    "elastic energy; lengthen the run or relax the waypoints"
                ),
                severity="error" if geom.bend_authored else "warn",
            )
        )


def connected_helix_sets(
    geoms: dict[str, HelixGeometry], tables: Any
) -> list[list[str]]:
    """Connected sets of **lattice** helices of two or more: helices on the
    same lattice that one strand has domains on both of. Sorted, each set
    sorted — a lone helix (the 4-bp hairpin) belongs to no set."""
    parent: dict[str, str] = {}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    for route in tables.by_strand.values():
        on_lattice: dict[str, str] = {}
        for domain in route:
            geom = geoms.get(domain.helix)
            if geom is None or geom.lattice is None:
                continue
            parent.setdefault(domain.helix, domain.helix)
            first = on_lattice.setdefault(geom.lattice, domain.helix)
            parent[find(domain.helix)] = find(first)
    groups: dict[str, list[str]] = {}
    for name in parent:
        groups.setdefault(find(name), []).append(name)
    return sorted(sorted(g) for g in groups.values() if len(g) >= 2)


def _register_findings(
    geoms: dict[str, HelixGeometry],
    tables: Any,
    findings: list[ValidationIssue],
) -> None:
    in_set = {name for group in connected_helix_sets(geoms, tables) for name in group}
    for name in sorted(geoms):
        geom = geoms[name]
        if geom.lattice is None or name not in in_set:
            continue
        spec = nucleic.LATTICES[geom.lattice]
        if commensurate(geom.motif, geom.n_units, spec.pitch_units):
            continue
        whole = geom.n_units % spec.pitch_units == 0
        why = (
            "its accumulated roll does not close on a whole number of turns"
            if whole
            else (
                f"{geom.n_units} is not a whole number of {spec.pitch_units}-unit "
                f"{spec.name} repeats ({geom.n_units % spec.pitch_units} over)"
            )
        )
        findings.append(
            ValidationIssue(
                rule="chain_twist_register",
                subject=name,
                detail=(
                    f"{geom.n_units} units on the {spec.name} lattice is out of "
                    f"register — {why}; the design is strained unless an "
                    "insertion/deletion retunes it "
                    f"(repeat: {spec.pitch_units} units = "
                    f"{spec.turns_per_pitch} turns)"
                ),
                severity="warn",
            )
        )


def _indel_phrase(residual: float, base_twist: float) -> str:
    """The insertions/deletions that would cancel ``residual`` — an
    over-wound helix (positive) sheds bases, an under-wound one gains them."""
    count = round(residual / base_twist)
    if count > 0:
        return f"{count} deletion(s)"
    if count < 0:
        return f"{-count} insertion(s)"
    return "none"


def _global_twist_findings(
    geoms: dict[str, HelixGeometry],
    tables: Any,
    findings: list[ValidationIssue],
) -> None:
    """``chain_twist_global`` — see the module docstring. The tolerance is
    half of one base-motif unit's twist: beyond it one more insertion or
    deletion would leave the helix closer to flat."""
    for group in connected_helix_sets(geoms, tables):
        offenders: list[str] = []
        for name in group:
            geom = geoms[name]
            residual = twist_residual(geom)
            tolerance = 0.5 * abs(geom.base_motif.twist)
            if abs(residual) <= tolerance:
                continue
            offenders.append(
                f"{name} {math.degrees(residual):+.1f} deg "
                f"(currently {geom.insertions} insertion(s), "
                f"{geom.deletions} deletion(s); "
                f"{_indel_phrase(residual, geom.base_motif.twist)} more "
                "would cancel it)"
            )
        if not offenders:
            continue
        shown = ", ".join(group[:4]) + (
            f" +{len(group) - 4} more" if len(group) > 4 else ""
        )
        findings.append(
            ValidationIssue(
                rule="chain_twist_global",
                subject=shown,
                detail=(
                    f"{len(offenders)} of {len(group)} connected lattice helices "
                    "wind off the lattice they are drawn on — at the real "
                    "twist each accumulates a residual over its run; the sheet "
                    "would curl in the tube unless insertions/deletions retune "
                    "it: " + "; ".join(offenders)
                ),
                severity="warn",
            )
        )


def _route_findings(
    tree: Any,
    tables: Any,
    geoms: dict[str, HelixGeometry],
    findings: list[ValidationIssue],
) -> None:
    """``chain_dangling_domain`` — the domain graph re-checked over stored
    rows, the same posture ``dangling_threading`` has."""
    for strand in sorted(tables.by_strand):
        route = tables.by_strand[strand]
        node = tree.blocks.get(strand)
        if node is None or chain_role(node) != STRAND_ROLE:
            missing = "no longer exists" if node is None else "is not a strand"
            findings.append(
                ValidationIssue(
                    rule="chain_dangling_domain",
                    subject=strand,
                    detail=(
                        f"{len(route)} domain(s) name strand {strand!r}, which "
                        f"{missing} — declare_strand it, or remove_domain the "
                        "rows"
                    ),
                    severity="error",
                )
            )
        ords = [d.ord for d in route]
        if ords != list(range(len(ords))):
            findings.append(
                ValidationIssue(
                    rule="chain_dangling_domain",
                    subject=strand,
                    detail=(
                        f"route ordinals {ords} are not 0…{len(ords) - 1} — a "
                        "hole or a repeat in a strand's 5'→3' order leaves the "
                        "loops between the gaps undefined"
                    ),
                    severity="error",
                )
            )
        for domain in route:
            geom = geoms.get(domain.helix)
            if geom is None:
                node = tree.blocks.get(domain.helix)
                why = (
                    "no longer exists"
                    if node is None
                    else "is not a helix (or its record is malformed)"
                )
                findings.append(
                    ValidationIssue(
                        rule="chain_dangling_domain",
                        subject=f"{strand}#{domain.ord}",
                        detail=(f"domain names helix {domain.helix!r}, which {why}"),
                        severity="error",
                    )
                )
                continue
            if domain.end > geom.n_units:
                findings.append(
                    ValidationIssue(
                        rule="chain_dangling_domain",
                        subject=f"{strand}#{domain.ord}",
                        detail=(
                            f"offsets [{domain.start}, {domain.end}) run past "
                            f"helix {domain.helix!r}'s {geom.n_units} units — "
                            "extend the helix or shorten the domain"
                        ),
                        severity="error",
                    )
                )


def _segment_table(
    geoms: dict[str, HelixGeometry],
) -> tuple[list[Any], dict[str, tuple[int, int]]]:
    """Capsules for every helix's segments, plus ``helix → (base index,
    units per segment)`` so an offset's capsule index is arithmetic.

    The same tiling ``layout_chain`` materialises (:func:`precis_se.chain.
    layout.segment_ranges`), computed here rather than read off the child
    blocks: the clash check must give the same answer whether or not
    anybody has run ``layout_chain`` yet.
    """
    capsules: list[Any] = []
    index: dict[str, tuple[int, int]] = {}
    for name in sorted(geoms):
        geom = geoms[name]
        per = units_per_segment(geom)
        index[name] = (len(capsules), per)
        for start, end in segment_ranges(geom.n_units, per):
            capsules.append(segment_capsule(geom, start, end))
    return capsules, index


def _capsule_of(
    index: dict[str, tuple[int, int]], helix: str, offset: int
) -> int | None:
    entry = index.get(helix)
    if entry is None:
        return None
    base, per = entry
    return base + offset // per


def _clash_findings(
    geoms: dict[str, HelixGeometry],
    loops: list[tuple[DomainSpec, DomainSpec, float, float]],
    findings: list[ValidationIssue],
) -> None:
    capsules, index = _segment_table(geoms)
    if len(capsules) < 2:
        return
    # Sanctioned contacts: consecutive segments of one helix (they share an
    # endpoint by construction) and the two segments a short loop joins.
    skip: list[tuple[int, int]] = []
    for name in sorted(geoms):
        base, per = index[name]
        count = len(segment_ranges(geoms[name].n_units, per))
        skip.extend((base + i, base + i + 1) for i in range(count - 1))
    for before, after, _gap, _reach in loops:
        i = _capsule_of(index, before.helix, before.exit_offset)
        j = _capsule_of(index, after.helix, after.entry_offset)
        if i is not None and j is not None and i != j:
            skip.append((i, j))
    # One tolerance for the whole pass: the tightest ``min_gap`` any helix
    # in the design declares. A per-pair tolerance would need the pair, and
    # the kernel's broad phase is built around a single tol — taking the
    # tightest is the conservative reading (it reports less), and a design
    # mixing tolerances is told which helix set the number.
    owner = min(geoms.values(), key=lambda g: g.min_gap_m)
    tol = owner.min_gap_m
    default_note = "" if owner.gap_authored else ", the coded lattice default"
    for i, j in clashes(capsules, skip_pairs=skip, tol=tol):
        gap = capsule_distance(capsules[i], capsules[j])
        findings.append(
            ValidationIssue(
                rule="chain_clash",
                subject=f"{_capsule_name(index, i)}—{_capsule_name(index, j)}",
                detail=(
                    f"swept tubes are {_len(gap)} apart, under the design's "
                    f"minimum gap {_len(tol)} (from helix {owner.name!r}"
                    f"{default_note}) — move a helix, or declare the contact "
                    "by widening min_gap"
                ),
                severity="warn",
            )
        )


def _capsule_name(index: dict[str, tuple[int, int]], i: int) -> str:
    """``<helix>.s<k>`` for capsule ``i`` — the same name
    ``layout_chain`` gives the child block, so a finding points at
    something a designer can address."""
    owner, base = "?", 0
    for helix, (helix_base, _per) in sorted(index.items(), key=lambda kv: kv[1][0]):
        if helix_base > i:
            break
        owner, base = helix, helix_base
    return f"{owner}.s{i - base}"


def _loop_reach(
    n: int, geom_a: HelixGeometry, geom_b: HelixGeometry
) -> tuple[float, float]:
    """``(reach, tol)`` for an ``n``-nt loop from ``geom_a`` to ``geom_b``:
    the ``(n + 1) * c`` contour, and the spec's ``tol`` term.

    ``tol`` is :func:`precis_se.chain.nucleic.backbone_frustration_m`
    summed over the two helices, and only when they are **different**
    helices: it is the shortfall a duplex's own groove asymmetry imposes on
    two *neighbouring* backbones facing each other, which no routing choice
    can remove (that function's note). A loop inside one helix — a hairpin
    — is not facing a neighbour and gets no allowance.
    """
    reach = contour(n, geom_b.motif.contour_per_unit)
    if geom_a.name == geom_b.name:
        return reach, 0.0
    tol = nucleic.backbone_frustration_m(
        geom_a.base_motif
    ) + nucleic.backbone_frustration_m(geom_b.base_motif)
    return reach + tol, tol


def _loops(
    tables: Any, geoms: dict[str, HelixGeometry]
) -> list[tuple[DomainSpec, DomainSpec, float, float]]:
    """Every loop in the design as ``(before, after, gap, reach)`` — the
    two domains it bridges, the distance between their backbone exits, and
    the contour available.

    A loop exists between every consecutive pair of a strand's domains; its
    nucleotide count is the LATER domain's ``loop_before_nt`` (``None`` is
    read as 0 — a direct crossover, which is the common case and the one
    the reach check is sharpest on).
    """
    out: list[tuple[DomainSpec, DomainSpec, float, float]] = []
    for route in tables.by_strand.values():
        for before, after in itertools.pairwise(route):
            geom_a, geom_b = geoms.get(before.helix), geoms.get(after.helix)
            if geom_a is None or geom_b is None:
                continue  # already a chain_dangling_domain finding
            if (
                before.exit_offset >= geom_a.n_units
                or after.entry_offset >= geom_b.n_units
            ):
                continue  # ditto
            p = geom_a.exit(before.exit_offset, before.forward)
            q = geom_b.exit(after.entry_offset, after.forward)
            gap = float(np.linalg.norm(q - p))
            n = after.loop_before_nt or 0
            reach, _tol = _loop_reach(n, geom_a, geom_b)
            out.append((before, after, gap, reach))
    return out


def _register_hint(
    geom_a: HelixGeometry,
    geom_b: HelixGeometry,
    before: DomainSpec,
    after: DomainSpec,
    reach: float,
) -> str:
    """The landing offsets that WOULD reach, for a 0-nt crossover that does
    not — the register rule stated as an answer rather than a rule.

    Holds the departing exit where it is and walks ``after``'s entry offset
    outward from the one authored, reporting the nearest two that fit
    inside ``reach``. On the error path only, so the scan costs nothing in
    a clean design (the dogfood's recovery took a whole tile rebuild
    because nothing named these).
    """
    entry = after.entry_offset
    p = geom_a.exit(before.exit_offset, before.forward)
    hits: list[int] = []
    for step in range(1, geom_b.n_units):
        for candidate in (entry - step, entry + step):
            if not 0 <= candidate < geom_b.n_units:
                continue
            q = geom_b.exit(candidate, after.forward)
            if float(np.linalg.norm(q - p)) <= reach:
                hits.append(candidate)
        if len(hits) >= 2:
            break
    if not hits:
        return ""
    offsets = ", ".join(str(o) for o in sorted(hits))
    return (
        f". Register-correct landing offsets on {geom_b.name} nearest this "
        f"one: {offsets} (authored {entry})"
    )


def _tether_findings(
    tree: Any,
    tables: Any,
    geoms: dict[str, HelixGeometry],
    findings: list[ValidationIssue],
) -> None:
    """A walker leg's tether — ``declare_strand(anchor='<body>.<port>',
    tether_nt=n)`` — checked like any other loop: ``chain_loop_short``
    when the ``(n + 1)·c`` contour cannot span the anchor port (at the
    body's CURRENT pose — the state's stored pose once ``args.state`` has
    applied it, else the block's default) to the foot domain's entry
    exit; ``chain_dangling_domain`` when the anchor names a block or port
    the design no longer has. A free leg (its foot lifted off every helix
    in this state, so the route is empty) has no tether to check. No
    groove-asymmetry allowance: the body is not a neighbouring duplex."""
    for strand in sorted(tables.by_strand):
        node = tree.blocks.get(strand)
        anchor = ((node.chain if node is not None else None) or {}).get("anchor")
        if not anchor:
            continue
        body = tree.blocks.get(str(anchor.get("block")))
        port = body.ports.get(str(anchor.get("port"))) if body is not None else None
        if body is None or port is None:
            findings.append(
                ValidationIssue(
                    rule="chain_dangling_domain",
                    subject=f"{strand} tether",
                    detail=(
                        f"strand is anchored at {anchor.get('block')}."
                        f"{anchor.get('port')}, which "
                        f"{'no longer exists' if body is None else 'has no such port'}"
                        " — re-declare the strand's anchor, or add the port"
                    ),
                    severity="error",
                )
            )
            continue
        if port.pose is None:
            continue  # nothing to measure from; the settle refuses it by name
        route = tables.by_strand[strand]
        if not route:
            continue
        foot = route[0]
        geom = geoms.get(foot.helix)
        if geom is None or foot.entry_offset >= geom.n_units:
            continue  # already a chain_dangling_domain finding
        rot = np.asarray(rotation(*(float(v) for v in body.rot)).R, dtype=float)
        anchor_m = np.asarray(body.pose, dtype=float).reshape(3) + rot @ np.asarray(
            port.pose, dtype=float
        ).reshape(3)
        gap = float(
            np.linalg.norm(geom.exit(foot.entry_offset, foot.forward) - anchor_m)
        )
        n = int(anchor.get("nt") or 0)
        c = geom.motif.contour_per_unit
        reach = contour(n, c)
        # Half a nucleotide of slack: the tether's length is quantised in
        # whole nucleotides, so a gap within c/2 of the reach is inside the
        # model's own resolution — and a station settle stops exactly at
        # reach (its springs are one-sided), so without this the solver's
        # residual would flag every taut, settled leg.
        if gap > reach + c / 2.0:
            needed = max(0, math.ceil(gap / c) - 1)
            findings.append(
                ValidationIssue(
                    rule="chain_loop_short",
                    subject=f"{strand} tether",
                    detail=(
                        f"{n} nt of tether cannot bridge {_len(gap)} from "
                        f"{anchor.get('block')}.{anchor.get('port')} to the foot "
                        f"domain #{foot.ord} on {foot.helix}@{foot.entry_offset}: "
                        f"reach (n+1)·c = {_len(reach)} at c = {_len(c)} per "
                        f"nucleotide. Needs at least {needed} nt, or the body "
                        "settled closer (relax_chain state={...})"
                    ),
                    severity="error",
                )
            )


def _loop_findings(
    tables: Any,
    geoms: dict[str, HelixGeometry],
    loops: list[tuple[DomainSpec, DomainSpec, float, float]],
    findings: list[ValidationIssue],
) -> None:
    del tables
    for before, after, gap, reach in loops:
        n = after.loop_before_nt or 0
        subject = f"{after.strand}#{before.ord}→#{after.ord}"
        geom_a, geom_b = geoms[before.helix], geoms[after.helix]
        c = geom_b.motif.contour_per_unit
        if gap > reach:
            _reach, tol = _loop_reach(n, geom_a, geom_b)
            allowance = (
                f" plus {_len(tol)} of groove-asymmetry allowance" if tol else ""
            )
            hint = (
                _register_hint(geom_a, geom_b, before, after, reach) if n == 0 else ""
            )
            needed = max(0, math.ceil((gap - tol) / c) - 1)
            findings.append(
                ValidationIssue(
                    rule="chain_loop_short",
                    subject=subject,
                    detail=(
                        f"{n} nt cannot bridge {_len(gap)}: the loop's reach "
                        f"is (n+1)·c{allowance} = {_len(reach)} at c = "
                        f"{_len(c)} per nucleotide. Needs at least {needed} "
                        "nt, or the two exits moved closer — for a 0-nt "
                        "crossover that means a register-correct offset (both "
                        "backbones must face the other helix)" + hint
                    ),
                    severity="error",
                )
            )
            continue
        if gap < LOOP_SLACK_FRACTION * reach:
            energy = loop_slack_energy(np.zeros(3), np.array([gap, 0.0, 0.0]), n, c)
            findings.append(
                ValidationIssue(
                    rule="chain_loop_slack",
                    subject=subject,
                    detail=(
                        f"{n} nt spans {_len(gap)} but has {_len(reach)} of "
                        f"contour ({gap / reach:.0%} extended, "
                        f"{energy:.2f} kT of stretching) — the loop is free to "
                        "flop; fewer nucleotides would pin the geometry"
                    ),
                    severity="info",
                )
            )


#: What :func:`floppy_findings` says about the persistence length it
#: measured against when the caller named no source — the pure pass's case.
CODED_LP_NOTE = "coded default; a material Lp row with conditions overrides it"


def floppy_findings(
    pairing: Pairing,
    loops: list[tuple[DomainSpec, DomainSpec, float, float]],
    *,
    lp_of: Callable[[str], tuple[float, str]] | None = None,
) -> list[ValidationIssue]:
    """``chain_floppy`` — one row per single-stranded span, and one per
    loop, longer than ssDNA's persistence length.

    ``lp_of(helix)`` supplies ``(Lp in metres, a note naming where it came
    from)`` per helix; the default is the coded
    :data:`precis_se.chain.nucleic.LP_SSDNA_M` with
    :data:`CODED_LP_NOTE`. That hook is the whole reason this function is
    public: the handler-side pass
    (:func:`precis_se.chain.findings.findings`) re-emits these rows against
    a ``material`` Lp row through **this same code**, so the two tiers can
    never measure the same span two different ways — and it supersedes the
    pure rows rather than adding to them, so a span is one finding either
    way.
    """
    reader = lp_of or (lambda _helix: (nucleic.LP_SSDNA_M, CODED_LP_NOTE))
    out: list[ValidationIssue] = []
    c = nucleic.SS_CONTOUR_PER_NT_M
    for helix, first, last in pairing.single_runs():
        lp, note = reader(helix)
        n_nt = pairing.run_nt(helix, first, last)
        span = n_nt * c
        if span <= lp:
            continue
        out.append(
            ValidationIssue(
                rule="chain_floppy",
                subject=f"{helix}[{first}:{last + 1}]",
                detail=(
                    f"{n_nt} nt of single-stranded span = {_len(span)} of "
                    f"contour, past ssDNA's persistence length "
                    f"{_len(lp)} — this stretch has no defined shape "
                    f"({note})"
                ),
                severity="info",
            )
        )
    for _before, after, _gap, reach in loops:
        lp, note = reader(after.helix)
        n = after.loop_before_nt or 0
        if n == 0 or reach <= lp:
            continue
        out.append(
            ValidationIssue(
                rule="chain_floppy",
                subject=f"{after.strand}#{after.ord} loop",
                detail=(
                    f"{n}-nt loop has {_len(reach)} of contour, past ssDNA's "
                    f"persistence length {_len(lp)} — its shape is an "
                    f"ensemble, not a curve ({note})"
                ),
                severity="info",
            )
        )
    return out


def geometry_and_loops(
    tree: Any,
) -> tuple[dict[str, HelixGeometry], list[tuple[DomainSpec, DomainSpec, float, float]]]:
    """``(helix geometries, loops)`` for ``tree`` — the two derived
    structures the handler-side passes need and must not re-derive
    differently.

    A record this pass cannot read is **dropped** here rather than
    reported: the ``chain_malformed`` finding is :func:`findings`' to make,
    and a second copy of it from the handler side would double every row.
    """
    dropped: list[ValidationIssue] = []
    geoms = _helix_geometries(tree, dropped)
    tables = group_domains(list(getattr(tree, "domains", []) or []))
    return geoms, _loops(tables, geoms)


def _occupancy_findings(pairing: Pairing, findings: list[ValidationIssue]) -> None:
    for occ in pairing.conflicts:
        who = ", ".join(
            f"{o.strand}#{o.ord} {'→' if o.forward else '←'}" for o in occ.occupants
        )
        if occ.status == PARALLEL:
            detail = (
                f"two strands occupy this offset running the SAME way ({who}) "
                "— a parallel duplex is out of scope for this cut; reverse one "
                "domain's forward, or move it"
            )
        elif occ.status == CROWDED:
            detail = (
                f"{len(occ.occupants)} strands occupy this offset ({who}) — a "
                "helix offset holds two backbones; a triplex needs a third "
                "azimuth and is out of scope for this cut"
            )
        else:  # pragma: no cover — conflicts hold exactly these two statuses
            continue
        findings.append(
            ValidationIssue(
                rule="chain_occupancy",
                subject=f"{occ.helix}[{occ.offset}]",
                detail=detail,
                severity="error",
            )
        )


def _geometry_findings(
    pairing: Pairing,
    geoms: dict[str, HelixGeometry],
    findings: list[ValidationIssue],
) -> None:
    for key in sorted(pairing.offsets):
        occ = pairing.offsets[key]
        if occ.status != PAIRED:
            continue
        a, b = occ.occupants[0].letter, occ.occupants[1].letter
        if not a or not b:
            continue  # unsequenced or 'N' — unverifiable, not wrong
        if occ.geometry is None:
            # No declared family: the duplex is Watson–Crick or it is wrong.
            # A G·T wobble is NOT accepted here — it belongs to a declared
            # W-W-cis; an undeclared duplex claims strict complementarity.
            if watson_crick(occ) is False:
                who = " vs ".join(
                    f"{o.strand}#{o.ord} {o.letter}" for o in occ.occupants
                )
                geom = geoms.get(occ.helix)
                wobble = "G·U" if geom is not None and geom.nucleic == "RNA" else "G·T"
                findings.append(
                    ValidationIssue(
                        rule="chain_pairing_mismatch",
                        subject=f"{occ.helix}[{occ.offset}]",
                        detail=(
                            f"{a}·{b} is not a Watson–Crick pair ({who}) — the "
                            "two domains co-occupy this offset, so the design "
                            "claims a base pair the letters cannot form: "
                            "change one sequence, or declare the geometry "
                            f"(a {wobble} wobble is a W-W-cis, not an "
                            "undeclared duplex)"
                        ),
                        severity="error",
                    )
                )
            continue
        if nucleic.pair_allowed(occ.geometry, a, b):
            continue
        family = nucleic.canonical_geometry(occ.geometry) or occ.geometry
        # ALLOWED_PAIRS is keyed in RNA lettering (T folded onto U); render
        # it back in the helix's own alphabet so a DNA design does not read
        # a wobble as "U·G", which pairs nowhere in a DNA design.
        geom = geoms.get(occ.helix)
        is_rna = geom is not None and geom.nucleic == "RNA"
        letters = (lambda x: x) if is_rna else (lambda x: "T" if x == "U" else x)
        allowed = sorted(
            f"{letters(x)}·{letters(y)}"
            for x, y in nucleic.ALLOWED_PAIRS.get(family, frozenset())
        )
        findings.append(
            ValidationIssue(
                rule="chain_pairing_geometry",
                subject=f"{occ.helix}[{occ.offset}]",
                detail=(
                    f"declared {family} over {a}·{b}, which that family does "
                    f"not accommodate (coded occupancy: "
                    f"{', '.join(allowed) or '(none)'}) — change the bases or "
                    "the declared geometry"
                ),
                severity="error",
            )
        )


def _disagree_findings(pairing: Pairing, findings: list[ValidationIssue]) -> None:
    """``chain_pairing_disagree`` — two occupants of one offset declared
    different Leontis–Westhof families. ``geometry``'s first-wins reading
    (:mod:`precis_se.chain.pairing`) is unchanged; this only reports that a
    later domain said something else, one row per offset."""
    for key in sorted(pairing.offsets):
        occ = pairing.offsets[key]
        families = {family for _occupant, family in occ.declarations}
        if len(families) < 2:
            continue
        winner_occupant, winner_family = occ.declarations[0]
        who = ", ".join(
            f"{occupant.strand}#{occupant.ord} declares {family!r}"
            for occupant, family in occ.declarations
        )
        findings.append(
            ValidationIssue(
                rule="chain_pairing_disagree",
                subject=f"{occ.helix}[{occ.offset}]",
                detail=(
                    f"{who} — one offset cannot be two families. "
                    f"{winner_occupant.strand}#{winner_occupant.ord}'s "
                    f"{winner_family!r} is what every other check uses "
                    "(first declaration in domain order wins), so the other "
                    "declaration is being ignored — change one of them to "
                    "agree, per-position via overrides[offset] if the two "
                    "domains need different families elsewhere"
                ),
                severity="error",
            )
        )


def _unpaired_findings(pairing: Pairing, findings: list[ValidationIssue]) -> None:
    """``chain_unpaired`` / ``chain_unpaired_run`` / ``chain_unpaired_stray``
    — the ``unpair`` op's marks, read back. A lone unpaired offset is
    modelled at its duplex position (owner ruling 2026-10-01); two or more
    consecutive are not a duplex shape at all and are a folding question."""
    for key in sorted(pairing.offsets):
        occ = pairing.offsets[key]
        if occ.unpaired_by and occ.status != UNPAIRED:
            findings.append(
                ValidationIssue(
                    rule="chain_unpaired_stray",
                    subject=f"{occ.helix}[{occ.offset}]",
                    detail=(
                        "declared unpaired but not a two-strand pair "
                        f"(status {occ.status}), so the mark does nothing — "
                        "clear it with unpair(clear=true)"
                    ),
                    severity="warn",
                )
            )
    runs: list[list[OffsetOccupancy]] = []
    for occ in sorted(pairing.unpaired, key=lambda o: (o.helix, o.offset)):
        prev = runs[-1][-1] if runs else None
        if (
            prev is not None
            and prev.helix == occ.helix
            and prev.offset == occ.offset - 1
        ):
            runs[-1].append(occ)
        else:
            runs.append([occ])
    for run in runs:
        first = run[0]
        if len(run) == 1:
            who = " vs ".join(
                f"{o.strand}#{o.ord} {o.letter or '?'}" for o in first.occupants
            )
            findings.append(
                ValidationIssue(
                    rule="chain_unpaired",
                    subject=f"{first.helix}[{first.offset}]",
                    detail=(
                        f"declared unpaired ({who}) — modelled at its duplex "
                        "position, which a single unpaired base keeps"
                    ),
                    severity="info",
                )
            )
            continue
        findings.append(
            ValidationIssue(
                rule="chain_unpaired_run",
                subject=f"{first.helix}[{first.offset}:{run[-1].offset + 1}]",
                detail=(
                    f"{len(run)} consecutive unpaired offsets are placed at "
                    "duplex positions, which is not their shape — a run this "
                    "long is a folding question (fold_layout's ViennaRNA fold, "
                    "or oxDNA), not a rigid duplex"
                ),
                severity="warn",
            )
        )


def findings(tree: Any) -> list[ValidationIssue]:
    """Every pure ``chain_*`` finding for ``tree``. Empty when the design
    declares no chain at all (the overwhelmingly common case — this pass
    costs one dict lookup per block then)."""
    out: list[ValidationIssue] = []
    has_chain = any(chain_role(node) is not None for node in tree.blocks.values())
    domains = list(getattr(tree, "domains", []) or [])
    if not has_chain and not domains:
        return out
    geoms = _helix_geometries(tree, out)
    tables = group_domains(domains)
    _bend_findings(geoms, out)
    _register_findings(geoms, tables, out)
    _global_twist_findings(geoms, tables, out)
    _route_findings(tree, tables, geoms, out)
    loops = _loops(tables, geoms)
    _loop_findings(tables, geoms, loops, out)
    _tether_findings(tree, tables, geoms, out)
    if geoms:
        _clash_findings(geoms, loops, out)
    pairing = derive_pairing(tree)
    _occupancy_findings(pairing, out)
    _geometry_findings(pairing, geoms, out)
    _disagree_findings(pairing, out)
    _unpaired_findings(pairing, out)
    out.extend(floppy_findings(pairing, loops))
    return out
