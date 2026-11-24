"""``import_trace`` — a PDB / mmCIF C-alpha trace as helix records and a tube
(:mod:`precis_se.chain.protein`).

Theorem-style: the fixture is an *ideal* alpha-helix built from the textbook
numbers, and every assertion is a consequence of those numbers — the bend
radius of a helix of radius r and pitch p, residues per turn, containment in
the tube — never a snapshot of the importer's own output.
"""

from __future__ import annotations

import math
from unittest.mock import Mock

import numpy as np
import pytest

from precis.store import Store
from precis_chain import bend_radius, units_per_turn
from precis_se.atomic.apply import apply_ops_with_atomic
from precis_se.chain import drc, nucleic, protein
from precis_se.chain.layout import helix_geometry, segment_capsule
from precis_se.chain.relax import op_relax_chain
from precis_se.chain.vocab import build_domain, validate_chain
from precis_se.ops import OpError, SeTree, apply_ops

_A = 1e-10


def _ideal_helix_m(n: int) -> np.ndarray:
    """C-alpha trace of an ideal right-handed alpha-helix about +z, metres."""
    k = np.arange(n, dtype=float)
    phi = k * protein.ALPHA_TWIST_RAD
    return np.column_stack(
        [
            protein.ALPHA_RADIUS_M * np.cos(phi),
            protein.ALPHA_RADIUS_M * np.sin(phi),
            k * protein.ALPHA_RISE_M,
        ]
    )


def _cif(ca_m: np.ndarray, chain: str = "A", *more: tuple[str, np.ndarray]) -> str:
    head = (
        "data_x\nloop_\n_atom_site.group_PDB\n_atom_site.id\n"
        "_atom_site.label_atom_id\n_atom_site.label_asym_id\n"
        "_atom_site.label_seq_id\n_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\n"
    )
    rows = [
        f"ATOM {i + 1} CA {cid} {i + 1} {p[0] / _A:.3f} {p[1] / _A:.3f} {p[2] / _A:.3f}"
        for i, (cid, p) in enumerate(
            [(chain, p) for p in ca_m] + [(c, p) for c, trace in more for p in trace]
        )
    ]
    return head + "\n".join(rows) + "\n#\n"


def _import(text: str, **kw: object) -> SeTree:
    tree = SeTree()
    apply_ops(tree, [{"op": "import_trace", "name": "p", "source": text, **kw}])
    return tree


def _dist_to_segment(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    ab = b - a
    t = 0.0 if not ab.any() else float(np.clip((p - a) @ ab / (ab @ ab), 0.0, 1.0))
    return float(np.linalg.norm(p - (a + t * ab)))


def test_ideal_helix_numbers_are_the_textbook_ones() -> None:
    motif = nucleic.MOTIFS["alpha-helix"]
    assert motif.rise == pytest.approx(0.15e-9)
    assert 2 * math.pi / motif.twist == pytest.approx(3.6)
    assert motif.radius == pytest.approx(0.23e-9)


def test_ideal_helix_ca_bend_radius_and_turn_and_tube() -> None:
    n = 20
    ca = _ideal_helix_m(n)
    tree = _import(_cif(ca), step="per-residue")
    node = tree.blocks["p"]
    geom = helix_geometry(node)
    assert geom.n_units == n

    # Bend radius of a helix r, pitch 2*pi*c: (r^2 + c^2) / r. The trace is
    # sampled every 100 deg, so the discrete circumradius sits within a few
    # percent of the continuous formula.
    r = protein.ALPHA_RADIUS_M
    c = protein.ALPHA_RISE_M / protein.ALPHA_TWIST_RAD
    formula = (r * r + c * c) / r
    imported = geom.units.origins
    measured = float(np.median(bend_radius(imported)))
    assert measured == pytest.approx(formula, rel=0.2)
    # ... and the imported trace is that trace (unit k sits on residue k).
    assert np.allclose(imported, ca, atol=1e-12)

    # 3.6 +/- 0.1 residues per turn, measured from the C-alpha azimuths.
    az = np.unwrap(np.arctan2(imported[:, 1], imported[:, 0]))
    per_turn = 2 * math.pi / float(np.mean(np.diff(az)))
    assert per_turn == pytest.approx(3.6, abs=0.1)

    # Every C-alpha inside the layout_chain tube.
    segs = [b for b in tree.blocks.values() if b.parent == "p"]
    assert segs
    caps = []
    for b in segs:
        start, end = (b.chain or {})["start"], (b.chain or {})["end"]
        caps.append((start, end, segment_capsule(geom, start, end)))
    assert sum(e - s + 1 for s, e, _ in caps) == n
    tube = protein.DEFAULT_TUBE_RADIUS_M
    for k in range(n):
        capsule = next(cap for s, e, cap in caps if s <= k <= e)
        assert capsule.r == pytest.approx(tube, rel=1e-9)
        assert _dist_to_segment(ca[k], capsule.a, capsule.b) <= capsule.r


def test_per_ss_segment_helix_run_sits_on_the_helix_motif() -> None:
    n = 20
    ca = _ideal_helix_m(n)
    tree = _import(_cif(ca), step="per-ss-segment")
    helices = [
        b for b in tree.blocks.values() if (b.chain or {}).get("motif") == "alpha-helix"
    ]
    assert len(helices) == 1
    node = helices[0]
    geom = helix_geometry(node)
    assert geom.n_units == n
    # unit k's backbone point is within the tube of residue k (and, for an
    # ideal helix, within a fraction of an angstrom of it).
    pts = np.array([geom.exit(k, True) for k in range(n)])
    assert float(np.max(np.linalg.norm(pts - ca, axis=1))) < 0.1e-9
    assert units_per_turn(geom.base_motif) == pytest.approx(3.6)


def test_per_ss_segment_fits_compressed_beta_strand() -> None:
    z = np.arange(20) * 0.30e-9
    ca = np.column_stack([np.zeros_like(z), np.zeros_like(z), z])
    tree = _import(_cif(ca), step="per-ss-segment")
    geom = helix_geometry(tree.blocks["p.ss0"])
    assert geom.base_motif.name == "beta-strand"
    assert geom.n_units == len(ca)
    assert geom.motif.rise == pytest.approx(0.30e-9, rel=1e-9)
    np.testing.assert_allclose(geom.units.origins, ca, atol=1e-15)
    capsule = segment_capsule(geom, 0, geom.n_units - 1)
    assert all(_dist_to_segment(p, capsule.a, capsule.b) <= capsule.r for p in ca)


@pytest.mark.parametrize("step", protein.STEPS)
def test_imported_trace_is_valid_for_drc(step: str) -> None:
    tree = _import(_cif(_ideal_helix_m(20)), step=step, tube_radius="0.3 nm")
    for node in tree.blocks.values():
        if node.chain is not None:
            validate_chain(node.chain)
            if node.chain["role"] == "helix":
                geom = helix_geometry(node)
                assert segment_capsule(geom, 0, 0).r == pytest.approx(0.3e-9, rel=1e-9)
    assert not any(issue.rule == "chain_malformed" for issue in drc.findings(tree))


def test_import_converged_fold() -> None:
    store = Mock(spec=Store)
    store.get_ref.side_effect = [
        Mock(id=2),
        Mock(meta={"fold": {"cif": _cif(_ideal_helix_m(8))}}),
    ]
    store.links_for.return_value = [Mock(src_ref_id=1, dst_ref_id=2)]
    tree = SeTree()
    apply_ops_with_atomic(
        store,
        tree,
        [{"op": "import_trace", "name": "p", "source": "structure:fold"}],
        design_slug="design",
    )
    assert helix_geometry(tree.blocks["p"]).n_units == 8


def test_import_trace_reads_pdb() -> None:
    ca = _ideal_helix_m(8)
    pdb = "\n".join(
        f"ATOM  {i:5d}  CA  ALA A{i:4d}    {x:8.3f}{y:8.3f}{z:8.3f}"
        for i, (x, y, z) in enumerate(ca / _A, start=1)
    )
    tree = _import(pdb, chain="A")
    assert np.allclose(helix_geometry(tree.blocks["p"]).units.origins, ca, atol=1e-13)


def test_mmcif_residue_ids_determine_trace_order() -> None:
    ca = _ideal_helix_m(8)
    lines = _cif(ca).splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("ATOM "))
    lines[first:-1] = reversed(lines[first:-1])
    tree = _import("\n".join(lines))
    assert np.allclose(helix_geometry(tree.blocks["p"]).units.origins, ca, atol=1e-13)


def test_tube_radius_is_honoured() -> None:
    tree = _import(_cif(_ideal_helix_m(8)), tube_radius="0.3 nm")
    seg = next(b for b in tree.blocks.values() if b.parent == "p")
    assert seg.envelope is not None and "r3e-10" in seg.envelope.replace(".", "")


def test_secondary_structure_call() -> None:
    assert set(protein.assign_ss(_ideal_helix_m(12))) == {"H"}
    k = np.arange(10, dtype=float)
    strand = np.column_stack([k * protein.BETA_RISE_M, (k % 2) * 0.05e-9, np.zeros(10)])
    assert set(protein.assign_ss(strand)) == {"E"}


def _mixed_ss_trace() -> np.ndarray:
    """A helix run followed by a zig-zag strand run (the i→i+2 spacing the
    secondary-structure call reads as ``E``), so ``per-ss-segment`` mints
    more than one helix block."""
    helix = _ideal_helix_m(10)
    k = np.arange(8, dtype=float)
    tail = helix[-1] + np.column_stack(
        [(k % 2) * 0.10e-9, np.zeros(8), (k + 1) * 0.31e-9]
    )
    return np.vstack([helix, tail])


def test_per_ss_segment_mints_one_helix_per_run() -> None:
    tree = _import(_cif(_mixed_ss_trace()), step="per-ss-segment")
    motifs = sorted(
        str((b.chain or {}).get("motif"))
        for b in tree.blocks.values()
        if b.parent == "p"
    )
    assert motifs == ["alpha-helix", "beta-strand"]


@pytest.mark.parametrize("step", protein.STEPS)
def test_imported_trace_has_no_self_clash(step: str) -> None:
    """A trace's own tube is one continuous envelope — contacts between
    its capsules are the fold, never ``chain_clash`` (gr695518: without
    the exemption every import self-flooded ~n warns)."""
    tree = _import(_cif(_mixed_ss_trace()), step=step)
    assert not [i for i in drc.findings(tree) if i.rule == "chain_clash"]


def test_protein_still_clashes_against_a_duplex() -> None:
    """The exemption is protein-vs-protein only: a trace overlapping a
    designed duplex is a collision worth naming."""
    tree = _import(_cif(_ideal_helix_m(12)))
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": 6,
                "path": {
                    "waypoints": [
                        ["0 nm", "0 nm", "0 nm"],
                        ["0 nm", "0 nm", "5 nm"],
                    ]
                },
            },
        ],
    )
    clashes = [i for i in drc.findings(tree) if i.rule == "chain_clash"]
    assert clashes and all("h.s0" in i.subject for i in clashes)


def test_mixed_design_preserves_duplex_clearance_in_drc_and_relax() -> None:
    tree = _import(_cif(_ideal_helix_m(12) + np.array([20e-9, 0.0, 0.0])))
    # B-DNA has a 1 nm radius and requires 0.4 nm of surface clearance.
    # These axes start 2.2 nm apart, so the tubes are clear but too close.
    for name, x in (("h0", "0 nm"), ("h1", "2.2 nm")):
        apply_ops(
            tree,
            [
                {"op": "add_block", "name": name},
                {
                    "op": "declare_helix",
                    "block": name,
                    "n_units": 6,
                    "path": {"waypoints": [[x, "0 nm", "0 nm"], [x, "0 nm", "5 nm"]]},
                },
                {"op": "layout_chain", "block": name},
            ],
        )
    assert [i.subject for i in drc.findings(tree) if i.rule == "chain_clash"] == [
        "h0.s0—h1.s0"
    ]
    op_relax_chain(None, tree, {"op": "relax_chain"})
    separation = tree.blocks["h1.s0"].pose[0] - tree.blocks["h0.s0"].pose[0]
    assert separation > 2.2e-9


def test_protein_helix_refuses_strand_routing() -> None:
    """A protein helix's units are C-alpha residues — no strand's domain
    may route onto one, at write time and in the stored-row check."""
    tree = _import(_cif(_ideal_helix_m(8)))
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "s"},
            {"op": "declare_strand", "block": "s", "sequence": "ACGT"},
            {"op": "add_block", "name": "h"},
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": 6,
                "lattice": "honeycomb",
                "row": 0,
                "col": 0,
            },
        ],
    )
    domain = {
        "op": "add_domain",
        "strand": "s",
        "helix": "p",
        "start": 0,
        "end": 2,
        "forward": True,
    }
    with pytest.raises(OpError, match="protein trace"):
        apply_ops(tree, [domain])
    apply_ops(tree, [{**domain, "helix": "h"}])
    with pytest.raises(OpError, match="protein trace"):
        apply_ops(
            tree,
            [{"op": "set_domain", "strand": "s", "ord": 0, "helix": "p"}],
        )
    # A row stored anyway (a hand edit) is a chain_dangling_domain error.
    tree.domains.append(
        build_domain(
            {"start": 0, "end": 2, "forward": True},
            strand="s",
            helix="p",
            ord_=1,
        )
    )
    dangling = [i for i in drc.findings(tree) if i.rule == "chain_dangling_domain"]
    assert any("protein trace" in i.detail for i in dangling)


def test_trace_window_splits_around_a_break() -> None:
    """A chain break is refused, and ``start=``/``end=`` import each
    contiguous piece as its own block."""
    ca = _ideal_helix_m(8)
    gap = ca.copy()
    gap[4:] += np.array([0.0, 0.0, 2e-9])
    text = _cif(gap)
    with pytest.raises(OpError, match="start=/end="):
        _import(text)
    first = _import(text, start=0, end=4)
    assert np.allclose(
        helix_geometry(first.blocks["p"]).units.origins, ca[:4], atol=1e-12
    )
    tree = SeTree()
    apply_ops(tree, [{"op": "import_trace", "name": "p", "source": text, "start": 4}])
    assert np.allclose(
        helix_geometry(tree.blocks["p"]).units.origins, gap[4:], atol=1e-12
    )
    with pytest.raises(OpError, match="outside"):
        _import(_cif(ca), start=6, end=2)


def test_max_seg_len_coarsens_the_trace_tube() -> None:
    """``layout_chain max_seg_len=`` applies to a protein-coil trace too —
    the per-bond default is only the DEFAULT."""
    tree = _import(_cif(_ideal_helix_m(8)))
    assert sum(1 for b in tree.blocks.values() if b.parent == "p") == 8
    apply_ops(tree, [{"op": "layout_chain", "block": "p", "max_seg_len": "1.5 nm"}])
    segs = sorted(b.name for b in tree.blocks.values() if b.parent == "p")
    assert segs == ["p.s0", "p.s1", "p.s2"]


@pytest.mark.parametrize("step", protein.STEPS)
def test_trace_along_the_seed_axis_imports(step: str) -> None:
    """A trace whose first tangent is parallel to the ``+x`` frame seed
    falls back to a non-parallel seed rather than failing the import."""
    k = np.arange(6, dtype=float)
    ca = np.column_stack([k * protein.CA_CA_M, np.zeros(6), np.zeros(6)])
    tree = _import(_cif(ca), step=step)
    helix = next(
        b for b in tree.blocks.values() if (b.chain or {}).get("role") == "helix"
    )
    geom = helix_geometry(helix)
    assert geom.n_units == len(ca)
    cap = segment_capsule(geom, 0, geom.n_units - 1)
    for p in ca:
        assert _dist_to_segment(p, cap.a, cap.b) <= cap.r


def test_refusals() -> None:
    ca = _ideal_helix_m(8)
    with pytest.raises(OpError, match="structure design stores"):
        _import("structure:abc")
    with pytest.raises(OpError, match="no C-alpha atoms in chain 'B'"):
        _import(_cif(ca), chain="B")
    gap = ca.copy()
    gap[4:] += np.array([0.0, 0.0, 2e-9])
    with pytest.raises(OpError, match="chain break"):
        _import(_cif(gap))
    with pytest.raises(OpError, match="step must be"):
        _import(_cif(ca), step="per-atom")
    two = _cif(ca, "A", ("B", ca))
    with pytest.raises(OpError, match="chain= is required"):
        _import(two)
