"""``hexfold.join`` -- the block joiner over resolved blocks (SPEC §22.2,
`docs/backlog/hexfold-integration.md` ruling step 5, slice 1: sp2
two-block join reproducing a whole-spec fuse on the stick rung).

Every "compose two separately-resolved blocks" case is checked against
the equivalent whole-spec fuse (one ``.hx`` text, both instances, one
``build`` + ``stick``) -- the block joiner's whole job is to reproduce
that, minus the atoms it deliberately leaves frozen outside the seam
radius.
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from hexfold.build import Net, _frame, build
from hexfold.join import (
    SEAM_RADIUS,
    Block,
    _adjacency,
    _seam_subgraph,
    _shells_from,
    _stick_relaxer,
    block_from_net,
    compose,
    rank_k,
)
from hexfold.report import Severity
from hexfold.stick import (
    _angle_springs,
    _rep_pairs,
    _spring_forces,
    stick_info,
    stick_relax_pinned,
)
from hexfold.text import parse

_EXAMPLES = Path(__file__).resolve().parents[2] / "hexfold" / "examples"


def _resolved(text: str) -> Block:
    net = build(text, strict=False)
    pos, _max_force = stick_info(net)
    return block_from_net(net, pos)


def _kabsch_rmsd(x: np.ndarray, ref: np.ndarray) -> float:
    xc = x - x.mean(0)
    yc = ref - ref.mean(0)
    h = xc.T @ yc
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T))
    r = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    return float(np.sqrt(((xc @ r - yc) ** 2).sum(1).mean()))


def _whole_spec_census(whole: Net) -> dict[int, int]:
    finding = next(f for f in whole.report.findings if f.code == "seam.rings")
    return dict(dict(finding.data)["rings"])


# ---------- two-block fuse reproduces the whole-spec build ----------


@pytest.mark.parametrize(
    ("n", "m", "length", "rmsd_tol"),
    [
        # zigzag: the table radius (8 shells) leaves plenty of pinned
        # interior clear of the seam, so the composite's local re-relax
        # lands close to the whole-spec joint relax.
        (8, 0, 3, 0.05),
        # armchair: the table radius (2 shells) pins over half of this
        # short tube.  Measured: the composite's re-relax converges
        # cleanly (residual force ~1e-9 after 3000 iters, unchanged by
        # 20000 more) but to a different local minimum of the spring
        # landscape than the whole-spec joint relax -- Kabsch RMSD sits
        # at 0.073-0.085 A across len=3..8 and radius=2..10, only
        # collapsing to ~0.004 A once nearly everything is movable
        # (`seam_radius` far above the table default).  That is the same
        # trade-off `seam.leak`'s own armchair threshold documents (a
        # measured 1.41 deg angle change at the guard band, well above
        # the zigzag case's 0.011 deg): freezing this short an armchair
        # tube at r=2 is a real boundary effect of the stick rung, not a
        # bug, so the RMSD bound here is the measured value with
        # headroom rather than the tighter zigzag bound.
        (5, 5, 3, 0.1),
    ],
)
def test_two_tube_fuse_matches_whole_spec_build(
    n: int, m: int, length: int, rmsd_tol: float
) -> None:
    spec1 = f"hexfold 0.2\na: tube({n},{m}, len={length})\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    pa, pb = a.ports["out"], b.ports["in"]
    comp = compose(a, pa, b, pb, k=0)
    assert not [f for f in comp.findings if f.severity == Severity.ERROR]

    whole_text = (
        f"hexfold 0.2\na: tube({n},{m}, len={length})\n"
        f"b: tube({n},{m}, len={length})\na.out --fuse k=0--> b.in\n"
    )
    whole = build(whole_text, strict=False)
    whole_pos, _max_force = stick_info(whole)

    assert len(comp.elements) == len(whole.atoms)
    assert len(comp.bonds) == len(whole.bonds)
    assert comp.seam["rings"] == _whole_spec_census(whole)

    # ordinal correspondence: instance "a"'s own atoms get the same
    # path-sorted ordinals whether built alone or as part of the
    # two-instance whole spec (checked, not assumed -- `_assemble` sorts
    # by (instance, path), so one instance's own relative order never
    # depends on what else is in the spec); instance "b" then follows at
    # offset n_a.  So the composite and the whole-spec net's atoms are
    # the same physical atoms at the same ordinals.
    n_a = len(a.elements)
    nn = len(pa.dangling)
    seam_pairs = [(pa.dangling[i], n_a + pb.dangling[(0 - i) % nn]) for i in range(nn)]
    seam_dl = [
        abs(
            float(np.linalg.norm(comp.coords[i] - comp.coords[j]))
            - float(np.linalg.norm(whole_pos[i] - whole_pos[j]))
        )
        for i, j in seam_pairs
    ]
    assert max(seam_dl) < 0.02

    assert _kabsch_rmsd(comp.coords, whole_pos) < rmsd_tol

    # interior beyond the movable radius is bit-for-bit: a untouched, b
    # is the exact rigid image (R @ x + t), never touched by the relax.
    r, t = comp.transform
    non_movable_a = ~comp.movable[:n_a]
    assert np.array_equal(comp.coords[:n_a][non_movable_a], a.coords[non_movable_a])
    expected_b = (r @ b.coords.T).T + t
    non_movable_b = ~comp.movable[n_a:]
    assert np.array_equal(comp.coords[n_a:][non_movable_b], expected_b[non_movable_b])

    # composite ports: the two unconsumed ports, payload fields intact
    assert set(comp.ports) == {"a_in", "b_out"}
    a_in, b_out = comp.ports["a_in"], comp.ports["b_out"]
    a_in_orig, b_out_orig = a.ports["in"], b.ports["out"]
    assert len(a_in.dangling) == len(a_in_orig.dangling)
    assert a_in.rim_type == a_in_orig.rim_type
    assert len(b_out.dangling) == len(b_out_orig.dangling)
    assert b_out.rim_type == b_out_orig.rim_type

    # b_out's direction rotates by the same R placing b onto a
    c_b_before = b.coords.mean(axis=0)
    _c, dir_before = _frame(b.coords, b_out_orig.dangling, c_b_before)
    c_b_after = comp.coords[n_a:].mean(axis=0)
    _c2, dir_after = _frame(comp.coords, b_out.dangling, c_b_after)
    assert np.allclose(dir_after, r @ dir_before, atol=1e-6)


# ---------- adapter: pure-z onto pure-a, equal N ----------


def test_adapter_matches_whole_spec_and_rank_k_agrees_with_build_k_fit() -> None:
    a = _resolved("hexfold 0.2\na: tube(12,0, len=2)\n")
    b = _resolved("hexfold 0.2\nb: tube(6,6, len=2)\n")
    pa, pb = a.ports["out"], b.ports["in"]
    assert len(pa.dangling) == len(pb.dangling) == 12

    comp = compose(a, pa, b, pb, k=0)
    assert not [f for f in comp.findings if f.severity == Severity.ERROR]
    assert comp.seam["motif"] == "adapter"
    adapters = [f for f in comp.findings if f.code == "seam.adapter"]
    assert len(adapters) == 1
    assert dict(adapters[0].data)["a_type"] == ["z", 12]
    assert dict(adapters[0].data)["b_type"] == ["a", 12]

    whole_text = (
        "hexfold 0.2\na: tube(12,0, len=2)\nb: tube(6,6, len=2)\n"
        "a.out --fuse k=0--> b.in\n"
    )
    whole = build(whole_text, strict=False)
    assert comp.seam["rings"] == _whole_spec_census(whole)

    # k="fit" (rank_k) picks the same phase build's own k=fit resolves,
    # over the identical two-instance spec (the internal k=-1 fit marker
    # has no text syntax -- SPEC 12.1's own `to_text` docstring -- so the
    # AST is built directly, as `hexfold.options`/menus.py do).
    spec = parse(whole_text)
    spec_fit = replace(spec, connects=(replace(spec.connects[0], k=-1),))
    whole_fit = build(spec_fit, strict=False)
    fit_finding = next(
        f for f in whole_fit.report.findings if f.code == "fit.alternatives"
    )
    applied = dict(fit_finding.data)["applied"]
    assert rank_k(a, pa, b, pb)[0][0] == applied


# ---------- N mismatch ----------


def test_n_mismatch_reports_port_mismatch_and_mints_no_geometry() -> None:
    a = _resolved("hexfold 0.2\na: tube(12,0, len=2)\n")
    b = _resolved("hexfold 0.2\nb: tube(8,0, len=2)\n")
    comp = compose(a, a.ports["out"], b, b.ports["in"], k=0)
    assert [f.code for f in comp.findings] == ["port.mismatch"]
    assert comp.findings[0].severity == Severity.ERROR
    assert comp.elements == ()
    assert comp.coords.shape == (0, 3)
    assert comp.bonds == ()
    assert comp.ports == {}


# ---------- seam_radius override: leak fires below the table radius ----------


def test_seam_radius_override_fires_leak_silent_at_table_radius() -> None:
    spec1 = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    pa, pb = a.ports["out"], b.ports["in"]

    default = compose(a, pa, b, pb, k=0)
    assert [f for f in default.findings if f.code == "seam.leak"] == []

    tight = compose(a, pa, b, pb, k=0, seam_radius={"a": 1})
    leaks = [f for f in tight.findings if f.code == "seam.leak"]
    assert len(leaks) == 1
    assert dict(leaks[0].data)["block"] == "a"
    assert dict(leaks[0].data)["r"] == 1


def test_seam_leak_names_the_breached_measure_and_its_threshold() -> None:
    """A leak WARN has to say WHICH of the two measures tripped and what
    it tripped against.  Measured in the 2026-09-29 prod dogfood: the
    adapter join reported `max |dl| 0.0000 A, max |dtheta| 0.060 deg`,
    where the bond term was below its own threshold and formatted to a
    bare zero, so a genuine angle breach (0.060 against the zigzag
    0.025) read as a false alarm."""
    spec1 = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    tight = compose(a, a.ports["out"], b, b.ports["in"], k=0, seam_radius={"a": 1})
    leak = next(f for f in tight.findings if f.code == "seam.leak")
    data = dict(leak.data)

    # the breached measure is named, and only the breached one
    assert data["breached"], data
    for name, value, key in (
        ("|dl|", data["max_dl"], "thresh_dl"),
        ("|dtheta|", data["max_dtheta"], "thresh_dtheta"),
    ):
        assert (name in data["breached"]) == (value >= data[key]), (name, data)

    # ... and the message carries the threshold beside the value, so the
    # reader can size the breach without looking up the table
    assert " over threshold " in leak.message, leak.message
    for name in data["breached"]:
        assert name in leak.message, (name, leak.message)
    assert f"{data['thresh_dtheta']:.3f} deg" in leak.message, leak.message


def test_seam_sigma_names_both_bond_lengths_and_which_one_wins() -> None:
    """gr456212 (2026-09-29 prod dogfood): a sigma=1.75 A block joined to
    a sigma=1.42 A one was accepted -- strain roughly tripled and
    `seam.leak` fired on both sides, but nothing said why. `compose`
    only ever consults `a.sigma` for placement/relax, so the finding
    must name both values and say which one the seam actually used."""
    spec = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec)
    b = replace(_resolved(spec), sigma=1.75)
    composite = compose(a, a.ports["out"], b, b.ports["in"], k=0)
    finding = next(f for f in composite.findings if f.code == "seam.sigma")
    assert finding.severity == Severity.WARN
    data = dict(finding.data)
    assert data["a_sigma"] == pytest.approx(a.sigma)
    assert data["b_sigma"] == pytest.approx(1.75)
    assert f"{a.sigma:.4f}" in finding.message
    assert "1.7500" in finding.message


def test_seam_sigma_silent_when_equal() -> None:
    """Two blocks off the same lattice must not spuriously fire -- the
    tolerance has to be tight enough not to trip on ordinary float
    round-trip noise between two independently-built copies."""
    spec = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec)
    b = _resolved(spec)
    composite = compose(a, a.ports["out"], b, b.ports["in"], k=0)
    assert not [f for f in composite.findings if f.code == "seam.sigma"]


def test_seam_element_names_both_rims() -> None:
    """gr456212's second half: the sigma check landed in 2026-09 and the
    element one never did, so a rim of one element fused to a rim of
    another in silence. The seam bonds `pa.dangling` to `pb.dangling` by
    geometry and port size alone -- no element comparison exists anywhere
    in the join path, unlike bind/generate/propose, which all check
    `port.expected_element` against the atom."""
    spec = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec)
    b0 = _resolved(spec)
    b = replace(b0, elements=("N",) * len(b0.elements))
    composite = compose(a, a.ports["out"], b, b.ports["in"], k=0)
    finding = next(f for f in composite.findings if f.code == "seam.element")
    # WARN, matching seam.sigma: JOINERS is keyed on a lattice *pair* so a
    # heterojunction entry can exist later, so refusing would pre-empt the
    # design. Promoting to ERROR is a product call.
    assert finding.severity == Severity.WARN
    data = dict(finding.data)
    assert data["a_elements"] == ["C"]
    assert data["b_elements"] == ["N"]
    assert "'out'" in finding.message and "'in'" in finding.message


def test_seam_element_silent_when_both_rims_match() -> None:
    """The load-bearing negative: every ordinary carbon join goes through
    this check, so a wrong comparison would warn on all of them."""
    spec = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec)
    b = _resolved(spec)
    composite = compose(a, a.ports["out"], b, b.ports["in"], k=0)
    assert not [f for f in composite.findings if f.code == "seam.element"]


# ---------- second join: a composite is itself a Block ----------


def test_second_join_onto_a_composite_port() -> None:
    spec1 = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    comp1 = compose(a, a.ports["out"], b, b.ports["in"], k=0)
    assert not [f for f in comp1.findings if f.severity == Severity.ERROR]
    assert set(comp1.ports) == {"a_in", "b_out"}

    composite_block = Block(
        elements=comp1.elements,
        coords=comp1.coords,
        bonds=comp1.bonds,
        rings=comp1.rings,
        ports=comp1.ports,
        sigma=a.sigma,
    )
    c = _resolved(spec1)
    # real block names, not the positional default -- what the se side
    # passes so a chain of joins doesn't collide on "a"/"b"
    comp2 = compose(
        composite_block,
        composite_block.ports["b_out"],
        c,
        c.ports["in"],
        k=0,
        prefix_a="ab",
        prefix_b="c",
    )
    assert not [f for f in comp2.findings if f.severity == Severity.ERROR]
    assert [f for f in comp2.findings if f.code == "seam.leak"] == []
    assert len(comp2.elements) == len(comp1.elements) + len(c.elements)
    assert set(comp2.ports) == {"ab_a_in", "c_out"}


# ---------- compose() keeps the old positional/keyword contract ----------


def test_compose_default_prefixes_are_backwards_compatible() -> None:
    spec1 = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    # no prefix_a/prefix_b: every pre-existing caller's port names unchanged
    comp = compose(a, a.ports["out"], b, b.ports["in"], k=0)
    assert set(comp.ports) == {"a_in", "b_out"}


# ---------- re-relax only touches the seam sub-graph ----------


def test_relax_receives_only_the_seam_subgraph() -> None:
    # a tube long enough that the table radius (8 shells) plus its guard
    # band (2 more) does not reach the far port -- the len=3 blocks used
    # elsewhere in this file are entirely consumed by shells 0..10, so
    # |S| == N there and this property would be invisible.
    spec1 = "hexfold 0.2\na: tube(8,0, len=10)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    pa, pb = a.ports["out"], b.ports["in"]
    n_a = len(a.elements)
    total_n = n_a + len(b.elements)
    n = len(pa.dangling)
    seam_pairs = [(pa.dangling[i], n_a + pb.dangling[(0 - i) % n]) for i in range(n)]
    dist_a = _shells_from(set(pa.atoms), _adjacency(a.bonds))
    dist_b = _shells_from(set(pb.atoms), _adjacency(b.bonds))
    r = SEAM_RADIUS["z"]
    movable = np.zeros(total_n, dtype=bool)
    for o, d in dist_a.items():
        if d <= r:
            movable[o] = True
    for o, d in dist_b.items():
        if d <= r:
            movable[n_a + o] = True
    expected_s = _seam_subgraph(movable, dist_a, dist_b, n_a, r, r, seam_pairs)
    assert len(expected_s) < total_n  # the reduction is real for this block

    seen: dict[str, int] = {}

    def counting_relax(
        elements: list[str],
        coords: np.ndarray,
        bonds: list[tuple[int, int, int]],
        rings: list[tuple[int, ...]],
        pinned_mask: np.ndarray,
    ) -> np.ndarray:
        seen["n"] = len(coords)
        return _stick_relaxer(a.sigma)(elements, coords, bonds, rings, pinned_mask)

    comp = compose(a, pa, b, pb, k=0, relax=counting_relax)
    assert not [f for f in comp.findings if f.severity == Severity.ERROR]
    assert seen["n"] == len(expected_s)
    assert seen["n"] < total_n


# ---------- seam.strain carries a bond-angle term too ----------


def test_seam_strain_reports_bond_and_angle_terms() -> None:
    spec1 = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    comp = compose(a, a.ports["out"], b, b.ports["in"], k=0)
    strain = next(f for f in comp.findings if f.code == "seam.strain")
    data = dict(strain.data)
    for key in ("rms_dl", "max_dl", "rms_dtheta", "max_dtheta", "bonds", "vertices"):
        assert key in data, data
        assert math.isfinite(data[key]), (key, data)
    assert data["max_dl"] < 0.1
    assert data["bonds"] > 0
    assert data["vertices"] > 0


# ---------- seam.terminated: a non-carbon atom inside S is not silent ----------


def test_seam_terminated_flags_non_carbon_atoms_in_the_subgraph() -> None:
    spec1 = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    pa, pb = a.ports["out"], b.ports["in"]

    comp_clean = compose(a, pa, b, pb, k=0)
    assert [f for f in comp_clean.findings if f.code == "seam.terminated"] == []

    # synthetic: relabel one of the fused rim's own dangling atoms as a
    # termination cap, as `terminate` would leave behind nearby -- a real
    # terminated tube has no port left to fuse through once its own rim
    # is capped, so this is the faithful way to put a non-carbon element
    # inside S without needing a third port.
    elements = list(a.elements)
    elements[pa.dangling[0]] = "H"
    a_terminated = replace(a, elements=tuple(elements))
    comp = compose(a_terminated, pa, b, pb, k=0)
    terms = [f for f in comp.findings if f.code == "seam.terminated"]
    assert len(terms) == 1
    assert dict(terms[0].data)["elements"] == ["H"]


# ---------- stick.py pin-mask refactor: byte-identical to the original ----------


_DT = 0.05
_K_BOND = 1.0
_K_ANGLE = 0.5
_K_REP = 0.1
_REP_CUT = 1.3
_REFRESH = 20
_REP_MARGIN = 2.0


def _reference_relax(
    pos: np.ndarray,
    bonds: np.ndarray,
    brest: np.ndarray,
    springs: np.ndarray,
    sigma: float,
    iters: int,
) -> np.ndarray:
    """The pre-refactor ``stick_info`` loop, copied verbatim (not via
    ``stick_relax_pinned``) so this test proves the refactor against an
    independent reimplementation, not against itself."""
    n = len(pos)
    bonded = np.zeros((n, n), dtype=bool)
    bonded[bonds[:, 0], bonds[:, 1]] = True
    bonded[bonds[:, 1], bonds[:, 0]] = True
    si = springs[:, 0].astype(np.int64)
    sj = springs[:, 1].astype(np.int64)
    bonded[si, sj] = True
    bonded[sj, si] = True
    srest = springs[:, 2]
    pairs = _rep_pairs(pos, bonded, _REP_MARGIN * _REP_CUT * sigma)
    f = np.zeros_like(pos)
    for it in range(iters):
        if it % _REFRESH == 0:
            pairs = _rep_pairs(pos, bonded, _REP_MARGIN * _REP_CUT * sigma)
        f = _spring_forces(pos, bonds[:, 0], bonds[:, 1], brest, _K_BOND)
        f += _spring_forces(pos, si, sj, srest, _K_ANGLE)
        if len(pairs):
            pi, pj = pairs[:, 0], pairs[:, 1]
            d = pos[pj] - pos[pi]
            r = np.linalg.norm(d, axis=1)
            near = r < _REP_CUT * sigma
            pi, pj, d = pi[near], pj[near], d[near]
            r = r[near]
            r = np.where(r == 0.0, 1e-9, r)
            rf = -_K_REP * (_REP_CUT * sigma - r)[:, None] * d / r[:, None]
            np.add.at(f, pi, rf)
            np.add.at(f, pj, -rf)
        pos = pos + _DT * f
    return pos


@pytest.mark.parametrize("example", ["pillar.hx", "nanobud_87.hx"])
def test_stick_relax_pinned_matches_reference_algorithm(example: str) -> None:
    text = (_EXAMPLES / example).read_text(encoding="utf-8")
    net = build(text, strict=False)
    assert net.seed3 is not None
    pos0 = np.array(net.seed3, dtype=np.float64)
    sig = net.lattice.sigma_A
    bonds = np.array([(i, j) for i, j, _ in net.bonds], dtype=np.int64)
    lat_el = set(net.lattice.elements)
    brest = np.array(
        [
            sig
            if net.atoms[i].element in lat_el and net.atoms[j].element in lat_el
            else net.lattice.sigma_CH_A
            for i, j, _ in net.bonds
        ],
        dtype=np.float64,
    )
    springs = np.array(_angle_springs(net), dtype=np.float64)
    # 50 iterations spans >2 repulsion-pair refresh cycles (_REFRESH=20),
    # enough to exercise every branch without the full 3000-iteration
    # cost of a converged stick() call.
    ref = _reference_relax(pos0.copy(), bonds, brest, springs, sig, iters=50)
    got, _max_force = stick_relax_pinned(
        pos0.copy(), bonds, brest, springs, sig, iters=50
    )
    assert np.array_equal(ref, got)
