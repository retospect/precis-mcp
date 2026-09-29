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

Rule names are flat snake_case, the house convention
(docs/backlog/se-nucleic-acid.md's 2026-09-28 decision — the dotted
spelling in earlier drafts was an artefact).

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
  are the designer's answer.
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
- ``chain_malformed`` (error) — a stored ``chain`` record that does not fit
  the schema at all. Not in the spec's list; it is the same
  defence-in-depth every other stored jsonb payload in se gets
  (``malformed_joint``), and without it a hand-corrupted record would
  crash the read path instead of surfacing.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np

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
    units_per_segment,
)
from precis_se.chain.pairing import (
    CROWDED,
    PAIRED,
    PARALLEL,
    Pairing,
    derive_pairing,
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


def _register_findings(
    geoms: dict[str, HelixGeometry], findings: list[ValidationIssue]
) -> None:
    for name in sorted(geoms):
        geom = geoms[name]
        if geom.lattice is None:
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


def _floppy_findings(
    pairing: Pairing,
    loops: list[tuple[DomainSpec, DomainSpec, float, float]],
    findings: list[ValidationIssue],
) -> None:
    """``chain_floppy`` — one row per single-stranded span longer than the
    **coded** ssDNA persistence length. The handler-side pass drops these
    and re-emits them against a ``material`` Lp row when the design has
    one, so exactly one row per span exists either way."""
    c = nucleic.SS_CONTOUR_PER_NT_M
    lp = nucleic.LP_SSDNA_M
    for helix, first, last in pairing.single_runs():
        n_nt = last - first + 1
        span = n_nt * c
        if span <= lp:
            continue
        findings.append(
            ValidationIssue(
                rule="chain_floppy",
                subject=f"{helix}[{first}:{last + 1}]",
                detail=(
                    f"{n_nt} nt of single-stranded span = {_len(span)} of "
                    f"contour, past ssDNA's coded persistence length "
                    f"{_len(lp)} — this stretch has no defined shape "
                    "(coded default; a material Lp row with conditions "
                    "overrides it)"
                ),
                severity="info",
            )
        )
    for _before, after, _gap, reach in loops:
        n = after.loop_before_nt or 0
        if n == 0 or reach <= lp:
            continue
        findings.append(
            ValidationIssue(
                rule="chain_floppy",
                subject=f"{after.strand}#{after.ord} loop",
                detail=(
                    f"{n}-nt loop has {_len(reach)} of contour, past ssDNA's "
                    f"coded persistence length {_len(lp)} — its shape is an "
                    "ensemble, not a curve (coded default; a material Lp row "
                    "with conditions overrides it)"
                ),
                severity="info",
            )
        )


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


def _geometry_findings(pairing: Pairing, findings: list[ValidationIssue]) -> None:
    for key in sorted(pairing.offsets):
        occ = pairing.offsets[key]
        if occ.geometry is None or occ.status != PAIRED:
            continue
        a, b = occ.occupants[0].letter, occ.occupants[1].letter
        if not a or not b:
            continue  # unsequenced or 'N' — unverifiable, not wrong
        if nucleic.pair_allowed(occ.geometry, a, b):
            continue
        family = nucleic.canonical_geometry(occ.geometry) or occ.geometry
        allowed = sorted(
            f"{x}·{y}" for x, y in nucleic.ALLOWED_PAIRS.get(family, frozenset())
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
    _register_findings(geoms, out)
    _route_findings(tree, tables, geoms, out)
    loops = _loops(tables, geoms)
    _loop_findings(tables, geoms, loops, out)
    if geoms:
        _clash_findings(geoms, loops, out)
    pairing = derive_pairing(tree)
    _occupancy_findings(pairing, out)
    _geometry_findings(pairing, out)
    _floppy_findings(pairing, loops, out)
    return out
