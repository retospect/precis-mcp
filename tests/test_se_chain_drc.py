"""The pure ``chain_*`` DRC rules (:mod:`precis_se.chain.drc`).

Theorem-style: every assertion recomputes the property from the values the
code under test returns (the kernel's own contour/reach/gap arithmetic, the
:mod:`precis_se.chain.nucleic` constants), never from a golden blob. A
finding is asserted *with* the number that produced it, so a test that
passes for the wrong reason is visible.

The geometry fixtures are built straight over :class:`~precis_se.ops.SeTree`
through :func:`~precis_se.ops.apply_ops` — no store, since this whole pass
is pure over the tree.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from precis_chain.loop import contour
from precis_chain.register import commensurate
from precis_se.chain import nucleic
from precis_se.chain.drc import LOOP_SLACK_FRACTION, findings
from precis_se.chain.layout import helix_geometry, register_offsets
from precis_se.chain.pairing import (
    PAIRED,
    SINGLE,
    OffsetOccupancy,
    Pairing,
    derive_pairing,
)
from precis_se.ops import OpError, SeTree, apply_ops

#: The square lattice's design twist per base pair — 3 turns / 32 bp, the
#: number ``lattice_motif`` retunes a B-DNA helix to.
SQUARE_TWIST = 2.0 * math.pi * 3 / 32
#: The square lattice's crossover period, and therefore the first offset at
#: which a helix's forward backbone can face its ``+x`` neighbour when
#: ``phase0`` is set to put it there.
XOVER = 7
#: ``phase0`` for every helix in the ribbon: the value that makes offset
#: :data:`XOVER` register-correct for a forward→reverse crossover to the
#: ``+x`` neighbour. The rule is ``phase0 + k * twist == azimuth + pi/2``
#: (:func:`precis_se.chain.nucleic.crossover_phase_rad`) with the ``+x``
#: neighbour at azimuth 0.
JUNCTION_PHASE0 = math.pi / 2 - XOVER * SQUARE_TWIST


def _rules(tree: SeTree) -> list[str]:
    return [f.rule for f in findings(tree)]


def _by_rule(tree: SeTree, rule: str) -> list[Any]:
    return [f for f in findings(tree) if f.rule == rule]


def _errors(tree: SeTree) -> list[Any]:
    return [f for f in findings(tree) if f.severity == "error"]


# ── the 4-way junction ──────────────────────────────────────────────────


def _junction(shift: int = 0) -> SeTree:
    """A four-helix square-lattice ribbon: four helices, four strands, eight
    domains, four zero-nt crossovers at register-correct offsets — authored
    in ONE ops list (the acceptance criterion's "one ``put``").

    The register arithmetic the fixture is built on, and the reason the
    offsets are what they are: with every helix's ``phase0`` set to
    :data:`JUNCTION_PHASE0`, the two backbones of a helix and of its
    ``col + 1`` neighbour come as close as the duplex geometry lets them at
    every offset ``≡ XOVER (mod 32)`` — 32 being the square lattice's
    3-turn repeat, so offsets 7 and 39 both qualify and nothing in between
    does. Each strand is one crossover: two 8-unit antiparallel domains on
    a neighbouring pair.

    ``shift`` displaces ONE crossover's landing offset by that many base
    pairs — the acceptance criterion's "shifting one crossover by 1 bp".
    """
    tree = SeTree()
    ops: list[dict[str, Any]] = []
    for col in range(4):
        name = f"h{col}"
        ops.append({"op": "add_block", "name": name})
        ops.append(
            {
                "op": "declare_helix",
                "block": name,
                "n_units": 64,
                "lattice": "square",
                "row": 0,
                "col": col,
                "phase0": f"{JUNCTION_PHASE0} rad",
            }
        )
    # (strand, helix it leaves, helix it lands on, base offset of both
    # 8-unit domains) — three crossovers along the ribbon at offset 7, and a
    # fourth one repeat further along at offset 39.
    crossovers = [
        ("sA", "h0", "h1", 0, 0),
        ("sB", "h1", "h2", 0, 0),
        ("sC", "h2", "h3", 0, 0),
        ("sD", "h0", "h1", 32, shift),
    ]
    for strand, first, second, base, offset in crossovers:
        ops.append({"op": "add_block", "name": strand})
        ops.append({"op": "declare_strand", "block": strand})
        ops.append(
            {
                "op": "add_domain",
                "strand": strand,
                "helix": first,
                "start": base,
                "end": base + 8,
                "forward": True,
            }
        )
        ops.append(
            {
                "op": "add_domain",
                "strand": strand,
                "helix": second,
                "start": base + offset,
                "end": base + offset + 8,
                "forward": False,
                "loop_before_nt": 0,
            }
        )
    apply_ops(tree, ops)
    return tree


def test_four_way_junction_is_clean_and_its_crossovers_reach() -> None:
    tree = _junction()
    assert len(tree.blocks) == 8  # 4 helices + 4 strands
    assert len(tree.domains) == 8
    assert sum(1 for d in tree.domains if d.loop_before_nt == 0) == 4

    # No chain_* error anywhere (info-tier single-stranded notes are fine).
    assert [f.rule for f in _errors(tree)] == []
    # ... and in particular the 64-unit runs are in register on the square
    # lattice, so no warn either.
    assert "chain_twist_register" not in _rules(tree)
    assert "chain_clash" not in _rules(tree)

    # The two helices with both a forward and a reverse domain over the same
    # offsets are PAIRED there — derived, not declared.
    pairing = derive_pairing(tree)
    assert pairing.conflicts == []
    assert len(pairing.pairs) == 16
    assert all(
        getattr(pairing.at(helix, offset), "status", None) == PAIRED
        for helix in ("h1", "h2")
        for offset in range(8)
    )

    # The reason there is no chain_loop_short: each crossover's two backbone
    # exits are within one bond PLUS the groove-asymmetry allowance of each
    # other. Recomputed from the kernel and the motif's own delta.
    base = nucleic.MOTIFS["B-DNA"]
    geoms = {n: helix_geometry(tree.blocks[n]) for n in ("h0", "h1")}
    reach = contour(0, nucleic.SS_CONTOUR_PER_NT_M) + 2 * (
        nucleic.backbone_frustration_m(base)
    )
    #: The closest two neighbouring duplexes can bring facing backbones:
    #: centre spacing less ``2 r sin(delta/2)``. NOT ``2.5 - 2 x 1.0`` —
    #: that is the antipodal answer, and the difference between the two is
    #: exactly the allowance above.
    floor = nucleic.HELIX_SPACING_M - 2 * base.radius * math.sin(
        nucleic.minor_groove_span_rad(base) / 2
    )
    assert floor > nucleic.HELIX_SPACING_M - 2 * base.radius
    for offset in (XOVER, XOVER + 32):
        gap = float(
            np.linalg.norm(
                geoms["h0"].exit(offset, True) - geoms["h1"].exit(offset, False)
            )
        )
        assert gap < reach, (offset, gap, reach)
        # Register-correct means the gap sits ON that floor.
        assert gap == pytest.approx(floor, rel=1e-9)


def test_shifting_one_crossover_by_one_bp_fires_loop_short() -> None:
    tree = _junction(shift=1)
    short = _by_rule(tree, "chain_loop_short")
    assert len(short) == 1, [f.subject for f in short]
    assert short[0].severity == "error"
    assert short[0].subject.startswith("sD#")

    # The number behind the finding: one base pair of displacement rolls the
    # landing backbone 3/32 of a turn round the duplex and a rise along it,
    # which is past the single bond (plus allowance) a 0-nt crossover has.
    base = nucleic.MOTIFS["B-DNA"]
    h0 = helix_geometry(tree.blocks["h0"])
    h1 = helix_geometry(tree.blocks["h1"])
    gap = float(np.linalg.norm(h0.exit(XOVER + 32, True) - h1.exit(XOVER + 33, False)))
    reach = contour(0, nucleic.SS_CONTOUR_PER_NT_M) + 2 * (
        nucleic.backbone_frustration_m(base)
    )
    assert gap > reach, (gap, reach)
    # Re-derived, not remembered. At a register-correct offset each exit
    # misses pointing at the other helix by ``a = (pi - delta)/2`` — equal
    # and opposite, so the two errors cancel in the transverse direction.
    # Moving the LANDING offset by one bp rolls only that exit, by one
    # unit's twist, so the cancellation breaks and the helix also rises.
    a = (math.pi - nucleic.minor_groove_span_rad(base)) / 2
    e_leave, e_land = a, -a + SQUARE_TWIST
    r, s = base.radius, nucleic.HELIX_SPACING_M
    expected = math.hypot(
        math.hypot(
            s - r * math.cos(e_leave) - r * math.cos(e_land),
            r * (math.sin(e_leave) + math.sin(e_land)),
        ),
        h1.motif.rise,
    )
    assert gap == pytest.approx(expected, rel=1e-9)
    # The finding names the landing offsets that WOULD reach, so an agent
    # does not have to binary-search 32 of them.
    assert "Register-correct landing offsets on h1" in short[0].detail
    assert "(authored 40)" in short[0].detail


# ── the grooves ─────────────────────────────────────────────────────────


def _channels(geom: Any, *, ref: int = 24, span: int = 20) -> list[tuple[float, float]]:
    """The duplex's two helical channels, measured from the model's own
    backbone exits: ``[(width, relative azimuth), …]``, narrower first.

    A channel is the trench between the two backbone strands, and its width
    is the shortest distance from one strand to the other. So: hold the
    forward strand's exit at offset ``ref`` and walk the reverse strand's
    exits two turns either side. The distance is a **local minimum** once
    per turn, wherever the two strands come round to the same azimuth (the
    axial term pulls each minimum in from there), and consecutive minima
    are the two different channels. The reverse exit's azimuth relative to
    the forward one says which: inside the arc that holds the frame normal
    (``0 < rel < delta``) is the **minor** groove, by the convention
    :func:`precis_se.chain.nucleic.strand_azimuth_rad` sets. The relative
    azimuth comes back in ``[0, 2 pi)``, not ``(-pi, pi]``, because A-RNA's
    delta is itself over pi and a half-open-at-pi wrap would cut its minor
    arc in two.
    """
    p = geom.exit(ref, True)
    base = geom.azimuth(ref, True)
    walk = []
    for offset in range(ref - span, ref + span + 1):
        if offset == ref:
            continue
        rel = geom.azimuth(offset, False) - base
        walk.append(
            (
                float(np.linalg.norm(geom.exit(offset, False) - p)),
                rel % (2 * math.pi),
            )
        )
    minima = [
        walk[i]
        for i in range(1, len(walk) - 1)
        if walk[i - 1][0] > walk[i][0] < walk[i + 1][0]
    ]
    minima.sort()
    return minima[:2]


def _free_helix(motif_name: str, n_units: int = 64) -> Any:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": n_units,
                "motif": motif_name,
                "nucleic": "RNA" if motif_name == "A-RNA" else "DNA",
                "path": {
                    "waypoints": [["0 nm", "0 nm", "0 nm"], ["0 nm", "0 nm", "40 nm"]]
                },
            },
        ],
    )
    return helix_geometry(tree.blocks["h"])


def test_the_two_grooves_are_different_widths_and_the_minor_one_is_named() -> None:
    """The test the antipodal azimuth pair could not pass.

    At ``delta = pi`` the two backbones are half a turn out of phase, so
    ``f(m) = 2 r^2 (1 + cos(m * twist)) + (m * rise)^2`` is EVEN in ``m``
    and the two channels are equal by construction — B-DNA with no minor
    groove. That is what this excludes.
    """
    for name, minor_is_narrow in (("B-DNA", True), ("A-RNA", False)):
        motif = nucleic.MOTIFS[name]
        delta = nucleic.minor_groove_span_rad(motif)
        geom = _free_helix(name)
        (d0, rel0), (d1, rel1) = _channels(geom)
        # Minor = the channel whose reverse exit sits inside the arc that
        # holds the frame normal, i.e. the arc of width delta.
        minor, major = (d0, d1) if 0 < rel0 < delta else (d1, d0)
        assert 0 < (rel0 if 0 < rel0 < delta else rel1) < delta
        assert minor != pytest.approx(major, rel=1e-6)
        assert (minor < major) is minor_is_narrow, (name, minor, major)
        # A-form's minor groove is the wide shallow one, which is the whole
        # reason this is keyed on "minor" and not on "narrow".
        assert (delta < math.pi) is minor_is_narrow


def test_the_cited_span_agrees_with_the_one_the_groove_widths_imply() -> None:
    """Two independent routes to δ, which must not drift apart.

    B-DNA's 144° is cited outright (Kornyshev & Leikin 2000; Allahyarov et
    al. 2003). The groove widths are a different measurement entirely, and
    :func:`precis_se.chain.nucleic.minor_groove_span_from_widths` turns them
    into 142.8°. Agreement to about a degree is what makes the coded value
    trustworthy; edit either input and this fails.
    """
    implied = nucleic.minor_groove_span_from_widths(*nucleic.GROOVE_WIDTH_M["B-DNA"])
    cited = nucleic.MINOR_GROOVE_SPAN_RAD["B-DNA"]
    assert cited == pytest.approx(math.radians(144.0))
    assert abs(math.degrees(cited - implied)) < 1.5
    # A-RNA has no cited value — its entry IS the width-ratio inference, and
    # it is past pi, which is the A form's reversed groove order.
    assert nucleic.MINOR_GROOVE_SPAN_RAD["A-RNA"] == pytest.approx(
        nucleic.minor_groove_span_from_widths(*nucleic.GROOVE_WIDTH_M["A-RNA"])
    )
    assert nucleic.MINOR_GROOVE_SPAN_RAD["A-RNA"] > math.pi


def test_the_modelled_groove_widths_recover_the_tabulated_ones() -> None:
    """δ is a *checked* number: put the phosphates where the fibre models
    put them and the model's own channels reproduce the tabulated widths.

    The production exits sit on the duplex surface (1.0 nm), not at the
    phosphorus (0.89 nm), so this rebuilds the motif at
    :data:`precis_se.chain.nucleic.P_RADIUS_M` — the one place the two
    radii are compared. B-DNA lands +2.7 % / +0.0 %; A-RNA +2.7 % / +5.3 %
    off a phosphorus radius that is itself an estimate, which is why its
    delta carries the wider uncertainty.
    """
    from dataclasses import replace

    for name, tol in (("B-DNA", 0.04), ("A-RNA", 0.08)):
        base = nucleic.MOTIFS[name]
        p_motif = replace(base, radius=nucleic.P_RADIUS_M[name])
        geom = _free_helix(name)
        object.__setattr__(geom, "motif", p_motif)
        object.__setattr__(geom, "base_motif", p_motif)
        delta = nucleic.minor_groove_span_rad(p_motif)
        (d0, rel0), (d1, rel1) = _channels(geom)
        minor, major = (d0, d1) if 0 < rel0 < delta else (d1, d0)
        want_minor, want_major = nucleic.GROOVE_WIDTH_M[name]
        assert minor == pytest.approx(
            want_minor + nucleic.PHOSPHATE_VDW_DIAMETER_M, rel=tol
        ), (name, minor)
        assert major == pytest.approx(
            want_major + nucleic.PHOSPHATE_VDW_DIAMETER_M, rel=tol
        ), (name, major)


# ── register ────────────────────────────────────────────────────────────


def test_register_correct_offsets_recur_at_the_lattice_repeat_per_neighbour() -> None:
    """The per-neighbour register table, recomputed from the exits.

    The claim this replaces ("the model is 4× stricter than caDNAno, two
    positions in 32 against one every 8 bp") was a counting error: the
    lattice's documented crossover period is the **aggregate over all
    neighbours**. Per neighbour it is one offset per lattice repeat, and
    ``repeat / neighbours`` is exactly the documented period.
    """
    for kind, spec in nucleic.LATTICES.items():
        motif = nucleic.lattice_motif(nucleic.MOTIFS["B-DNA"], kind)
        n = 3 * spec.pitch_units
        tree = SeTree()
        ops: list[dict[str, Any]] = []
        for i, (row, col) in enumerate(((0, 0), (0, 1))):
            ops.append({"op": "add_block", "name": f"g{i}"})
            ops.append(
                {
                    "op": "declare_helix",
                    "block": f"g{i}",
                    "n_units": n,
                    "lattice": kind,
                    "row": row,
                    "col": col,
                    "phase0": "0 rad",
                }
            )
        apply_ops(tree, ops)
        geom = helix_geometry(tree.blocks["g0"])
        reach = contour(0, motif.contour_per_unit) + 2 * (
            nucleic.backbone_frustration_m(nucleic.MOTIFS["B-DNA"])
        )
        per_neighbour = []
        for k in range(len(spec.neighbours)):
            offsets = register_offsets(geom, k, forward_to_reverse=True, n_units=n)
            assert offsets, (kind, k)
            gaps = {offsets[i + 1] - offsets[i] for i in range(len(offsets) - 1)}
            assert gaps == {spec.pitch_units}, (kind, k, offsets)
            per_neighbour.append(offsets[0])
        # Aggregated over the neighbours, one crossover site every
        # ``repeat / neighbours`` offsets — the lattice's own number.
        sites = sorted(o % spec.pitch_units for o in per_neighbour)
        steps = {sites[i + 1] - sites[i] for i in range(len(sites) - 1)}
        assert steps == {spec.crossover_period}, (kind, sites)
        assert spec.pitch_units // len(spec.neighbours) == spec.crossover_period

        # And the analytic rule agrees with the exits themselves: the +x
        # neighbour of g0 is g1, and the offsets where their two backbones
        # come within reach are exactly the rule's.
        other = helix_geometry(tree.blocks["g1"])
        index = spec.neighbours.index((1.0, 0.0)) if kind == "square" else None
        if index is not None:
            brute = [
                offset
                for offset in range(n)
                if float(
                    np.linalg.norm(geom.exit(offset, True) - other.exit(offset, False))
                )
                <= reach
            ]
            assert brute == register_offsets(
                geom, index, forward_to_reverse=True, n_units=n
            )


def test_the_two_crossover_directions_are_no_longer_one_condition() -> None:
    """With ``delta != pi`` a forward→reverse crossover and a
    reverse→forward one no longer admit the same offsets — they are half a
    turn apart, and on the honeycomb (an odd number of turns per repeat)
    only the forward→reverse family lands on integers at all, which is why
    it is the one that coincides with caDNAno's 0/7/14."""
    assert nucleic.crossover_phase_rad(True) - nucleic.crossover_phase_rad(
        False
    ) == pytest.approx(math.pi)
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": 42,
                "lattice": "honeycomb",
                "row": 0,
                "col": 0,
                "phase0": "0 rad",
            },
        ],
    )
    geom = helix_geometry(tree.blocks["h"])
    spec = nucleic.LATTICES["honeycomb"]
    scaffold = sorted(
        register_offsets(geom, k, forward_to_reverse=True)[0] % spec.pitch_units
        for k in range(len(spec.neighbours))
    )
    assert scaffold == [0, 7, 14]
    for k in range(len(spec.neighbours)):
        staple = register_offsets(geom, k, forward_to_reverse=False)
        assert staple
        assert set(staple).isdisjoint(
            register_offsets(geom, k, forward_to_reverse=True)
        )


def _honeycomb_helix(n_units: int) -> SeTree:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": n_units,
                "lattice": "honeycomb",
                "row": 0,
                "col": 0,
            },
        ],
    )
    return tree


def test_honeycomb_21_bp_is_in_register_and_22_is_not() -> None:
    good = _honeycomb_helix(21)
    assert "chain_twist_register" not in _rules(good)
    bad = _honeycomb_helix(22)
    fired = _by_rule(bad, "chain_twist_register")
    assert len(fired) == 1
    assert fired[0].severity == "warn"

    # The property, recomputed: 21 units on the honeycomb lattice closes on
    # a whole number of turns AND is a whole repeat; 22 is neither.
    motif = helix_geometry(good.blocks["h"]).motif
    spec = nucleic.LATTICES["honeycomb"]
    assert commensurate(motif, 21, spec.pitch_units)
    assert not commensurate(motif, 22, spec.pitch_units)
    # 21 units × two turns' worth of twist is exactly 2 turns per 10.5 bp —
    # the reason B_DNA_TWIST_RAD is 2π/10.5 and not the rounded 34.3°.
    assert 21 * motif.twist == pytest.approx(4.0 * math.pi, abs=1e-9)
    assert pytest.approx(math.radians(34.2857), abs=1e-5) == nucleic.B_DNA_TWIST_RAD


# ── clash ───────────────────────────────────────────────────────────────


def _parallel_pair(spacing_nm: float, n_units: int = 30) -> SeTree:
    """Two straight, parallel free-path helices ``spacing_nm`` apart."""
    tree = SeTree()
    ops: list[dict[str, Any]] = []
    for i, x in enumerate((0.0, spacing_nm)):
        ops.append({"op": "add_block", "name": f"h{i}"})
        ops.append(
            {
                "op": "declare_helix",
                "block": f"h{i}",
                "n_units": n_units,
                "path": {
                    "waypoints": [
                        [f"{x} nm", "0 nm", "0 nm"],
                        [f"{x} nm", "0 nm", "10 nm"],
                    ]
                },
            }
        )
    apply_ops(tree, ops)
    return tree


def test_clash_fires_at_2nm_and_is_clean_at_lattice_spacing() -> None:
    tight = _parallel_pair(2.0)
    fired = _by_rule(tight, "chain_clash")
    assert len(fired) >= 1
    assert all(f.severity == "warn" for f in fired)

    clear = _parallel_pair(2.5)
    assert "chain_clash" not in _rules(clear)

    # The threshold, recomputed: the default gap tolerance is the surface
    # separation at the LOW end of the measured spacing range (2.4 − 2 × 1.0
    # nm), so a pair at the nominal 2.5 nm clears it by 0.1 nm rather than
    # sitting on a knife-edge float comparison, while 2.0 nm of centre
    # spacing (zero surface gap) is well inside it.
    motif = nucleic.MOTIFS["B-DNA"]
    tol = nucleic.default_min_gap_m(motif)
    assert tol == pytest.approx(0.4e-9)
    assert 2.0e-9 - 2 * motif.radius < tol
    assert not (2.5e-9 - 2 * motif.radius < tol)
    # The margin is real, not luck: the nominal-spacing gap is 25 % over the
    # tolerance.
    assert (2.5e-9 - 2 * motif.radius) / tol == pytest.approx(1.25)


# ── loops ───────────────────────────────────────────────────────────────


def _loop_fixture(loop_nt: int) -> SeTree:
    """Two parallel helices 5 nm apart with one strand crossing between
    them, so the two backbone exits face each other across exactly 3 nm
    (5 nm of axis separation less the two 1 nm radii).

    Each helix gets the ``phase0`` that points the relevant exit at the
    other helix at offset 0 — the reverse domain on ``h0`` leaves at its
    ``start`` and the forward one on ``h1`` enters at its ``start``, so both
    are unit 0 and the loop has no axial component at all. The **two values
    differ**, by ``pi + delta``: with non-antipodal backbones a
    reverse→forward pair cannot have both exits facing at one shared phase
    (:func:`precis_se.chain.nucleic.crossover_phase_rad`), which is exactly
    the direction-dependence the antipodal model hid.
    """
    half = nucleic.minor_groove_span_rad(nucleic.MOTIFS["B-DNA"]) / 2
    phases = (-half, math.pi + half)
    tree = SeTree()
    ops: list[dict[str, Any]] = []
    for i, x in enumerate((0.0, 5.0)):
        ops.append({"op": "add_block", "name": f"h{i}"})
        ops.append(
            {
                "op": "declare_helix",
                "block": f"h{i}",
                "n_units": 8,
                "phase0": f"{phases[i]} rad",
                "path": {
                    "waypoints": [
                        [f"{x} nm", "0 nm", "0 nm"],
                        [f"{x} nm", "0 nm", "10 nm"],
                    ]
                },
            }
        )
    ops.append({"op": "add_block", "name": "s"})
    ops.append({"op": "declare_strand", "block": "s"})
    ops.append(
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h0",
            "start": 0,
            "end": 4,
            "forward": False,
        }
    )
    ops.append(
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h1",
            "start": 0,
            "end": 4,
            "forward": True,
            "loop_before_nt": loop_nt,
        }
    )
    apply_ops(tree, ops)
    return tree


def _loop_gap(tree: SeTree) -> float:
    h0 = helix_geometry(tree.blocks["h0"])
    h1 = helix_geometry(tree.blocks["h1"])
    return float(np.linalg.norm(h1.exit(0, True) - h0.exit(0, False)))


def test_three_nt_cannot_bridge_three_nm() -> None:
    tree = _loop_fixture(3)
    gap = _loop_gap(tree)
    assert gap == pytest.approx(3.0e-9, rel=1e-9)
    reach = contour(3, nucleic.SS_CONTOUR_PER_NT_M)
    assert reach == pytest.approx(4 * 0.63e-9)
    assert gap > reach

    fired = _by_rule(tree, "chain_loop_short")
    assert len(fired) == 1 and fired[0].severity == "error"
    assert "chain_loop_slack" not in _rules(tree)


def test_twenty_nt_across_three_nm_is_slack() -> None:
    tree = _loop_fixture(20)
    gap = _loop_gap(tree)
    reach = contour(20, nucleic.SS_CONTOUR_PER_NT_M)
    assert gap < LOOP_SLACK_FRACTION * reach, (gap, reach)

    fired = _by_rule(tree, "chain_loop_slack")
    assert len(fired) == 1 and fired[0].severity == "info"
    assert "chain_loop_short" not in _rules(tree)


# ── single_runs — exact contiguous-run tuples, not just a count ─────────
#
# ``runs[-1][2] == occ.offset - 1`` (the contiguity test) and
# ``runs[-1][0] == occ.helix`` (the same-helix guard) both survive
# `- → +` / `== → !=` mutation with only a run-COUNT assertion in place —
# two separated stretches could merge, or one stretch could split, and the
# suite stays green. These pin the exact ``(helix, first, last)`` tuples,
# built directly on :class:`Pairing`/:class:`OffsetOccupancy` (``single_runs``
# only reads ``.helix``/``.offset`` off ``singles``, so a bare status/offset
# fixture is enough — no need to route this through ``derive_pairing``).


def _single(helix: str, offset: int) -> OffsetOccupancy:
    return OffsetOccupancy(helix=helix, offset=offset, occupants=(), status=SINGLE)


def test_single_runs_pins_exact_tuples_for_two_separated_stretches() -> None:
    pairing = Pairing()
    pairing.singles = [
        _single("h", 0),
        _single("h", 1),
        _single("h", 2),
        _single("h", 10),
        _single("h", 11),
    ]
    assert pairing.single_runs() == [("h", 0, 2), ("h", 10, 11)]


def test_single_runs_fully_contiguous_stretch_is_one_run() -> None:
    pairing = Pairing()
    pairing.singles = [_single("h", offset) for offset in range(5)]
    assert pairing.single_runs() == [("h", 0, 4)]


def test_single_runs_does_not_merge_across_a_helix_boundary() -> None:
    pairing = Pairing()
    # Adjacent offsets (5, 6) on TWO DIFFERENT helices — contiguous by
    # offset alone, but the guard must keep them as two one-offset runs.
    pairing.singles = [_single("h0", 5), _single("h1", 6)]
    assert pairing.single_runs() == [("h0", 5, 5), ("h1", 6, 6)]


# ── occupancy + declared pair geometry ──────────────────────────────────


def _one_offset_tree(
    n_strands: int,
    *,
    sequences: list[str] | None = None,
    geometry: str | None = None,
    geometries: list[str | None] | None = None,
    nucleic_name: str = "DNA",
    strand_nucleic: str | None = None,
) -> SeTree:
    """``n_strands`` strands all occupying offset 0 of one helix.

    ``geometry`` sets only the first domain's declaration (the original,
    single-declarer shape); ``geometries`` sets one per strand, ``None``
    entries skipped, for the disagreement tests.
    """
    tree = SeTree()
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "h"},
        {
            "op": "declare_helix",
            "block": "h",
            "n_units": 8,
            "lattice": "square",
            "row": 0,
            "col": 0,
            "nucleic": nucleic_name,
        },
    ]
    for i in range(n_strands):
        name = f"s{i}"
        ops.append({"op": "add_block", "name": name})
        declare: dict[str, Any] = {
            "op": "declare_strand",
            "block": name,
            "nucleic": strand_nucleic or nucleic_name,
        }
        if sequences is not None:
            declare["sequence"] = sequences[i]
        ops.append(declare)
        domain: dict[str, Any] = {
            "op": "add_domain",
            "strand": name,
            "helix": "h",
            "start": 0,
            "end": 1,
            "forward": i % 2 == 0,
        }
        if geometries is not None:
            if geometries[i] is not None:
                domain["geometry"] = geometries[i]
        elif geometry is not None and i == 0:
            domain["geometry"] = geometry
        ops.append(domain)
    apply_ops(tree, ops)
    return tree


def test_three_strands_on_one_offset_is_an_occupancy_error() -> None:
    tree = _one_offset_tree(3)
    fired = _by_rule(tree, "chain_occupancy")
    assert len(fired) == 1
    assert fired[0].severity == "error"
    assert fired[0].subject == "h[0]"
    pairing = derive_pairing(tree)
    assert len(pairing.conflicts) == 1
    assert len(pairing.conflicts[0].occupants) == 3
    # Two strands on the same offset running the same way is the OTHER
    # occupancy error, and the antiparallel pair is not an error at all.
    assert "chain_occupancy" not in _rules(_one_offset_tree(2))


def test_parallel_pair_on_one_offset_is_an_occupancy_error() -> None:
    tree = _one_offset_tree(2)
    # Flip the second domain so both run forward.
    tree.domains[1].forward = True
    fired = _by_rule(tree, "chain_occupancy")
    assert len(fired) == 1 and fired[0].severity == "error"
    assert "SAME way" in fired[0].detail


def test_declared_geometry_over_bases_that_cannot_pair_that_way() -> None:
    tree = _one_offset_tree(2, sequences=["C", "C"], geometry="W-H-trans")
    fired = _by_rule(tree, "chain_pairing_geometry")
    assert len(fired) == 1
    assert fired[0].severity == "error"
    assert fired[0].subject == "h[0]"
    # The table it consulted says so, and the same declaration over letters
    # the family DOES accommodate is clean.
    assert not nucleic.pair_allowed("W-H-trans", "C", "C")
    assert nucleic.pair_allowed("W-H-trans", "A", "T")
    ok = _one_offset_tree(2, sequences=["A", "T"], geometry="W-H-trans")
    assert "chain_pairing_geometry" not in _rules(ok)
    # An unsequenced design is unverifiable, not wrong.
    blank = _one_offset_tree(2, geometry="W-H-trans")
    assert "chain_pairing_geometry" not in _rules(blank)


def test_undeclared_duplex_over_non_complementary_letters_is_a_mismatch() -> None:
    # No geometry declared: the co-occupancy claims a Watson–Crick pair the
    # letters cannot form. This is the check behind "do these base pairs in
    # fact match" — before it, a render was the only evidence.
    tree = _one_offset_tree(2, sequences=["A", "G"])
    fired = _by_rule(tree, "chain_pairing_mismatch")
    assert len(fired) == 1
    assert fired[0].severity == "error"
    assert fired[0].subject == "h[0]"
    assert "A·G is not a Watson–Crick pair" in fired[0].detail
    assert "s0#0 A vs s1#0 G" in fired[0].detail
    # A complementary pair is clean; so is an unsequenced one (unverifiable,
    # not wrong); so is a G·T wobble ONCE it is declared W-W-cis — the
    # undeclared duplex is strict, the declared family is the curated table.
    assert "chain_pairing_mismatch" not in _rules(
        _one_offset_tree(2, sequences=["A", "T"])
    )
    assert "chain_pairing_mismatch" not in _rules(_one_offset_tree(2))
    assert "chain_pairing_mismatch" not in _rules(
        _one_offset_tree(2, sequences=["G", "N"])
    )
    wobble = _one_offset_tree(2, sequences=["G", "T"])
    assert "chain_pairing_mismatch" in _rules(wobble)
    declared = _one_offset_tree(2, sequences=["G", "T"], geometry="W-W-cis")
    assert "chain_pairing_mismatch" not in _rules(declared)
    assert "chain_pairing_geometry" not in _rules(declared)
    # An RNA helix reads A·U as complementary (a T is refused at declare
    # time, so the RNA mismatch here is A·G).
    assert "chain_pairing_mismatch" not in _rules(
        _one_offset_tree(2, sequences=["A", "U"], nucleic_name="RNA")
    )
    assert "chain_pairing_mismatch" in _rules(
        _one_offset_tree(2, sequences=["A", "G"], nucleic_name="RNA")
    )
    # A hybrid duplex pairs across alphabets: DNA strands' T against an
    # RNA helix's A is the complement it is (the letters fold T→U before
    # the comparison), and U-bearing RNA strands on a DNA helix likewise.
    assert "chain_pairing_mismatch" not in _rules(
        _one_offset_tree(
            2, sequences=["T", "A"], nucleic_name="RNA", strand_nucleic="DNA"
        )
    )
    assert "chain_pairing_mismatch" not in _rules(
        _one_offset_tree(
            2, sequences=["U", "A"], nucleic_name="DNA", strand_nucleic="RNA"
        )
    )
    rna = _by_rule(
        _one_offset_tree(2, sequences=["A", "G"], nucleic_name="RNA"),
        "chain_pairing_mismatch",
    )
    assert "G·U wobble" in rna[0].detail


def test_pairing_geometry_message_uses_dna_lettering_on_a_dna_helix() -> None:
    # W-W-cis (the wobble family) accommodates G·U/U·G, which prints "G·T"
    # on a DNA helix, never the RNA-alphabet "U" the table is keyed in.
    tree = _one_offset_tree(2, sequences=["C", "C"], geometry="W-W-cis")
    fired = _by_rule(tree, "chain_pairing_geometry")
    assert len(fired) == 1
    assert "G·T" in fired[0].detail
    assert "U" not in fired[0].detail


def test_two_domains_declaring_different_families_fires_one_disagreement() -> None:
    tree = _one_offset_tree(2, geometries=["W-W-cis", "W-H-trans"])
    fired = _by_rule(tree, "chain_pairing_disagree")
    assert len(fired) == 1
    assert fired[0].severity == "error"
    assert fired[0].subject == "h[0]"
    assert "W-W-cis" in fired[0].detail
    assert "W-H-trans" in fired[0].detail


def test_wc_alias_and_its_canonical_spelling_are_not_a_disagreement() -> None:
    # 'WC' and 'W-W-cis' canonicalise to the same family — no disagreement.
    tree = _one_offset_tree(2, geometries=["WC", "W-W-cis"])
    assert "chain_pairing_disagree" not in _rules(tree)


# ── dangling + malformed ────────────────────────────────────────────────


def test_domain_past_the_helix_end_dangles() -> None:
    tree = _one_offset_tree(1)
    tree.domains[0].end = 99
    fired = _by_rule(tree, "chain_dangling_domain")
    assert len(fired) == 1 and fired[0].severity == "error"
    assert "99" in fired[0].detail


def test_removing_a_helix_takes_its_domains_with_it() -> None:
    tree = _one_offset_tree(1)
    apply_ops(tree, [{"op": "remove_block", "block": "h"}])
    assert tree.domains == []
    assert findings(tree) == []


def test_removing_a_strand_takes_its_domains_in_memory() -> None:
    """The strand-side sibling of the helix removal above, asserted the
    same way — on the in-memory tree ``remove_block`` just mutated, not a
    store round-trip. ``persist.save_tree``/``load_tree`` retires and
    re-inserts ``se_topology`` domain rows straight off ``tree.domains``
    with no filter of its own, so a store round-trip would happily persist
    and reload whatever the in-memory list already (wrongly) says — it does
    not independently clean up a dangling domain a broken retention
    comprehension failed to drop. This is the one place ``ops.py``'s own
    cascade, not a downstream store detail, is on the hook."""
    tree = _one_offset_tree(1)
    apply_ops(tree, [{"op": "remove_block", "block": "s0"}])
    assert tree.domains == []


def test_a_malformed_stored_record_is_a_finding_not_a_crash() -> None:
    tree = _one_offset_tree(1)
    tree.blocks["h"].chain = {"role": "helix", "motif": "Z-DNA", "n_units": 8}
    fired = _by_rule(tree, "chain_malformed")
    assert len(fired) == 1 and fired[0].severity == "error"


def _malformed_then_real_finding_tree() -> SeTree:
    """Two helices, named so ``sorted(tree.blocks)`` visits the malformed
    one FIRST: ``h0`` carries a stored record that fails ``validate_chain``
    (the same "Z-DNA" corruption as the test above), ``h1`` is well-formed
    but 22 honeycomb units — out of register, so it fires its own
    ``chain_twist_register``. Both live in ``_helix_geometries``'s single
    scan, so a ``continue → break`` on the malformed arm would stop the
    scan at ``h0`` and never even reach ``h1``."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {
                "op": "add_block",
                "name": "h0",
            },
            {
                "op": "declare_helix",
                "block": "h0",
                "n_units": 8,
                "lattice": "honeycomb",
                "row": 0,
                "col": 0,
            },
            {"op": "add_block", "name": "h1"},
            {
                "op": "declare_helix",
                "block": "h1",
                "n_units": 22,
                "lattice": "honeycomb",
                "row": 0,
                "col": 1,
            },
        ],
    )
    tree.blocks["h0"].chain = {"role": "helix", "motif": "Z-DNA", "n_units": 8}
    return tree


def test_a_malformed_block_does_not_stop_the_scan_at_later_blocks() -> None:
    tree = _malformed_then_real_finding_tree()
    malformed = _by_rule(tree, "chain_malformed")
    assert len(malformed) == 1 and malformed[0].subject == "h0"
    # ... and h1, sorting AFTER the malformed block, still gets its own
    # geometry realised and its own finding — which a ``break`` would have
    # silently dropped along with every other later helix's.
    register = _by_rule(tree, "chain_twist_register")
    assert len(register) == 1 and register[0].subject == "h1"


def test_no_chain_declaration_means_no_findings_and_no_work() -> None:
    tree = SeTree()
    apply_ops(tree, [{"op": "add_block", "name": "plain", "envelope": "box:w1d1h1"}])
    assert findings(tree) == []


def test_derive_pairing_honours_an_occupancy_state() -> None:
    tree = _one_offset_tree(1)
    assert derive_pairing(tree, state=None).offsets  # the rows as declared
    domain = tree.domains[0]
    key = f"{domain.strand}.{domain.ord}"
    # A freed leg (null) pairs nowhere in that state …
    assert not derive_pairing(tree, state={key: None}).offsets
    # … and the rows themselves are untouched by the read.
    assert tree.domains[0] is domain and not domain.free
    # A leg placed where it already is reads exactly as declared.
    same = derive_pairing(tree, state={key: f"{domain.helix}@{domain.start}"})
    assert same.offsets == derive_pairing(tree).offsets


def test_layout_chain_needs_a_helix() -> None:
    tree = _one_offset_tree(1)
    with pytest.raises(OpError, match="not a helix"):
        apply_ops(tree, [{"op": "layout_chain", "block": "s0"}])
