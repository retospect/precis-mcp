"""The pure ``chain_*`` DRC rules — docs/backlog/se-nucleic-acid.md slice 1.

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
from precis_se.chain.layout import helix_geometry
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
    ``-XOVER * twist``, a forward strand's backbone faces world ``+x`` (its
    ``col + 1`` neighbour) and a reverse strand's faces ``-x`` (its
    ``col - 1`` neighbour) at every offset ``≡ XOVER (mod 32)`` — 32 being
    the square lattice's 3-turn repeat, so offsets 7 and 39 both qualify and
    nothing in between does. Each strand is one crossover: two 8-unit
    antiparallel domains on a neighbouring pair.

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
                "phase0": f"{-XOVER * SQUARE_TWIST} rad",
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
    # exits are within ONE bond of each other. Recomputed from the kernel.
    geoms = {n: helix_geometry(tree.blocks[n]) for n in ("h0", "h1")}
    reach = contour(0, nucleic.SS_CONTOUR_PER_NT_M)
    for offset in (XOVER, XOVER + 32):
        gap = float(
            np.linalg.norm(
                geoms["h0"].exit(offset, True) - geoms["h1"].exit(offset, False)
            )
        )
        assert gap < reach, (offset, gap, reach)
        # The gap IS the lattice's surface separation: 2.5 nm of centre
        # spacing less the two 1.0 nm duplex radii.
        assert gap == pytest.approx(
            nucleic.HELIX_SPACING_M - 2 * nucleic.B_DNA_RADIUS_M, rel=1e-9
        )


def test_shifting_one_crossover_by_one_bp_fires_loop_short() -> None:
    tree = _junction(shift=1)
    short = _by_rule(tree, "chain_loop_short")
    assert len(short) == 1, [f.subject for f in short]
    assert short[0].severity == "error"
    assert short[0].subject.startswith("sD#")

    # The number behind the finding: one base pair of displacement rolls the
    # landing backbone 3/32 of a turn round the duplex and a rise along it,
    # which is past the single bond a 0-nt crossover has.
    h0 = helix_geometry(tree.blocks["h0"])
    h1 = helix_geometry(tree.blocks["h1"])
    gap = float(np.linalg.norm(h0.exit(XOVER + 32, True) - h1.exit(XOVER + 33, False)))
    reach = contour(0, nucleic.SS_CONTOUR_PER_NT_M)
    assert gap > reach, (gap, reach)
    assert gap == pytest.approx(0.932e-9, rel=5e-3)


# ── register ────────────────────────────────────────────────────────────


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

    ``phase0 = pi`` on both puts the relevant exits on the inward face at
    offset 0 — the reverse domain on ``h0`` leaves at its ``start`` and the
    forward one on ``h1`` enters at its ``start``, so both are unit 0 and
    the loop has no axial component at all.
    """
    tree = SeTree()
    ops: list[dict[str, Any]] = []
    for i, x in enumerate((0.0, 5.0)):
        ops.append({"op": "add_block", "name": f"h{i}"})
        ops.append(
            {
                "op": "declare_helix",
                "block": f"h{i}",
                "n_units": 8,
                "phase0": f"{math.pi} rad",
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
    n_strands: int, *, sequences: list[str] | None = None, geometry: str | None = None
) -> SeTree:
    """``n_strands`` strands all occupying offset 0 of one helix."""
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
        },
    ]
    for i in range(n_strands):
        name = f"s{i}"
        ops.append({"op": "add_block", "name": name})
        declare: dict[str, Any] = {"op": "declare_strand", "block": name}
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
        if geometry is not None and i == 0:
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


def test_derive_pairing_refuses_an_unhonoured_state_kwarg() -> None:
    tree = _one_offset_tree(1)
    assert derive_pairing(tree, state=None).offsets  # the no-op default
    with pytest.raises(NotImplementedError, match="reserved hook"):
        derive_pairing(tree, state={"walker": "s1"})


def test_layout_chain_needs_a_helix() -> None:
    tree = _one_offset_tree(1)
    with pytest.raises(OpError, match="not a helix"):
        apply_ops(tree, [{"op": "layout_chain", "block": "s0"}])
