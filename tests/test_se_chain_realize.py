"""``realize_chain`` and ``view='export'`` — the nucleic-acid domain's
third slice (:mod:`precis_se.chain.atoms`, :mod:`precis_se.chain.export`).

Theorem-style, like the other chain tests: every geometric assertion is
recomputed from the atoms the op actually stored, never from the numbers
the templates were transcribed with. Two of the item's acceptance numbers
are restated here, with the reason, rather than asserted as written:

- intra-strand P–P is **6.26 Å**, not the item's "6.6–7.2": the Arnott
  fibre model's phosphate radius (8.97 Å) at the motif's 10.5 bp/turn
  step gives exactly that (crystal B-DNA is 6.5–7.0 because its phosphates
  sit further out). The window below is the model's, 6.2–7.2.
- "21 bp regains phase" is the frame at offset **21** against offset 0
  (twenty-one 34.29° steps = 720°), not the twenty-first unit.
"""

from __future__ import annotations

import itertools
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import precis_se
from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis_chain.pdb import read_trace
from precis_se import persist
from precis_se.atomic.apply import HANDLER_LEVEL_OPS
from precis_se.chain import nucleic
from precis_se.chain.atoms import (
    BACKBONE_ATOMS,
    PlacedUnit,
    UnitOccupant,
    build_region,
    helix_handedness,
)
from precis_se.chain.export import to_cadnano, to_oxdna, to_scadnano
from precis_se.chain.layout import helix_geometry
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops
from precis_web.design_turn import _SE_STORE_AWARE_SIGNATURES, dry_run_se
from tests.test_se_chain_origami import _rectangle_tree

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

SEQ22 = "GCGAATTCGCGATCGCGAATTG"
_COMPLEMENT = {"A": "T", "T": "A", "G": "C", "C": "G"}


def _rc(seq: str) -> str:
    return "".join(_COMPLEMENT[c] for c in reversed(seq))


def _seed_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _seed_se_migrations(store)
    return SeHandler(hub=hub)


def _duplex_ops(n: int = 22, seq: str = SEQ22) -> list[dict[str, Any]]:
    """A straight, lattice-free ``n``-bp duplex (so the motif is the plain
    10.5 bp/turn B-DNA) with a forward and a reverse strand, laid out —
    segment ``h.s0`` covers units 0–20, ``h.s1`` unit 21."""
    length = (n - 1) * nucleic.B_DNA_RISE_M
    return [
        {"op": "add_block", "name": "h"},
        {
            "op": "declare_helix",
            "block": "h",
            "n_units": n,
            "path": {
                "waypoints": [["0 m", "0 m", "0 m"], ["0 m", "0 m", f"{length} m"]]
            },
        },
        {"op": "add_block", "name": "fwd"},
        {"op": "declare_strand", "block": "fwd", "sequence": seq},
        {"op": "add_block", "name": "rev"},
        {"op": "declare_strand", "block": "rev", "sequence": _rc(seq)},
        {
            "op": "add_domain",
            "strand": "fwd",
            "helix": "h",
            "start": 0,
            "end": n,
            "forward": True,
        },
        {
            "op": "add_domain",
            "strand": "rev",
            "helix": "h",
            "start": 0,
            "end": n,
            "forward": False,
        },
        {"op": "layout_chain"},
    ]


def _hairpin_ops() -> list[dict[str, Any]]:
    """The item's hairpin, ``GGGGAAAACCCC``, authored as one honeycomb helix
    with two antiparallel domains and a 4-nt loop (the same records
    ``fold_layout`` writes, without needing ViennaRNA in the venv)."""
    return [
        {"op": "add_block", "name": "stem"},
        {
            "op": "declare_helix",
            "block": "stem",
            "n_units": 4,
            "lattice": "honeycomb",
            "row": 0,
            "col": 0,
        },
        {"op": "add_block", "name": "hp"},
        {"op": "declare_strand", "block": "hp", "sequence": "GGGGAAAACCCC"},
        {
            "op": "add_domain",
            "strand": "hp",
            "helix": "stem",
            "start": 0,
            "end": 4,
            "forward": True,
        },
        {
            "op": "add_domain",
            "strand": "hp",
            "helix": "stem",
            "start": 0,
            "end": 4,
            "forward": False,
            "loop_before_nt": 4,
        },
        {"op": "layout_chain"},
    ]


def _realize(block: str, start: int, end: int, **extra: Any) -> dict[str, Any]:
    return {"op": "realize_chain", "block": block, "start": start, "end": end, **extra}


def _put(handler: SeHandler, slug: str, ops: list[dict[str, Any]]) -> str:
    return handler.put(id=slug, text=json.dumps({"ops": ops})).body


def _loaded(store: Store, slug: str) -> SeTree:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(store, ref.id)


def _atoms(store: Store, slug: str) -> tuple[np.ndarray, dict[str, Any]]:
    """``(coords Å (n, 3), chain_atoms meta)`` of a minted structure."""
    ref = store.get_ref(kind="structure", id=slug)
    assert ref is not None, f"no structure {slug!r}"
    scene, _handles = store.structure_load(ref.id)
    coords = np.array([scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()])
    return coords, dict(ref.meta["chain_atoms"])


def _select(
    coords: np.ndarray, meta: dict[str, Any], chain: str, name: str
) -> np.ndarray:
    rows = [
        i
        for i in range(coords.shape[0])
        if meta["chain_ids"][i] == chain and meta["names"][i] == name
    ]
    return coords[rows]


# ── the roster and the web dry-run ──────────────────────────────────────


def test_realize_chain_is_handler_level_and_a_web_proposal(store: Store) -> None:
    assert "realize_chain" in HANDLER_LEVEL_OPS
    assert "realize_chain" in _SE_STORE_AWARE_SIGNATURES
    tree = SeTree()
    apply_ops(tree, _duplex_ops())
    # The dry-run runs the pure half (so a bad region is reported) and
    # mints nothing.
    assert dry_run_se(store, tree, [_realize("h", 0, 21)], design_slug="dry") is None
    assert store.get_ref(kind="structure", id="dry-h.s0") is None
    error = dry_run_se(store, tree, [_realize("h", 0, 30)], design_slug="dry")
    assert error is not None and "not inside helix" in error


# ── the 21-bp theorems ──────────────────────────────────────────────────


def test_realize_chain_binds_the_segment_and_the_atoms_are_b_dna(
    handler: SeHandler, store: Store
) -> None:
    echo = _put(handler, "duplex", [*_duplex_ops(), _realize("h", 0, 21, sites=[5])])
    assert "on segment 'h.s0'" in echo and "structure 'duplex-h.s0'" in echo
    tree = _loaded(store, "duplex")
    seg = tree.blocks["h.s0"]
    assert seg.bound_kind == "structure" and seg.bound == "duplex-h.s0"
    assert tree.blocks["h"].bound_kind is None  # the helix parent carries none
    assert tree.blocks["h.s1"].bound_kind is None

    coords, meta = _atoms(store, "duplex-h.s0")
    assert meta["chains"] == {"A": "fwd", "B": "rev"}
    assert meta["fidelity"] == "allatom"
    n = 21
    p_a = _select(coords, meta, "A", "P")
    p_b = _select(coords, meta, "B", "P")
    c1_a = _select(coords, meta, "A", "C1'")
    c1_b = _select(coords, meta, "B", "C1'")
    assert p_a.shape == (n, 3) and p_b.shape == (n, 3)

    # P–P along a strand: the model's 6.26 Å (module docstring).
    pp = np.linalg.norm(np.diff(p_a, axis=0), axis=1)
    assert pp.min() >= 6.2 and pp.max() <= 7.2, pp
    # rise 3.34 ± 0.02 Å along the segment's own axis (local z).
    rise = np.diff(c1_a[:, 2])
    assert np.all(np.abs(rise - 3.34) <= 0.02), rise
    # C1'–C1' across a pair: chain B runs 3'→5' down the same offsets.
    across = [np.linalg.norm(c1_a[k] - c1_b[n - 1 - k]) for k in range(n)]
    assert min(across) >= 10.4 and max(across) <= 10.8, across
    # Grooves as the shortest inter-strand P···P on either axial side:
    # 11.5 / 17.5 ± 1 Å (the item's restated criterion).
    minor: list[float] = []
    major: list[float] = []
    for i in range(4, n - 4):
        d = np.linalg.norm(p_b - p_a[i], axis=1)
        minor.append(float(d[p_b[:, 2] < p_a[i, 2]].min()))
        major.append(float(d[p_b[:, 2] > p_a[i, 2]].min()))
    assert all(10.5 <= v <= 12.5 for v in minor), minor
    assert all(16.5 <= v <= 18.5 for v in major), major
    # 21 bp regains phase: the frame at offset 21 against offset 0.
    frames = helix_geometry(tree.blocks["h"]).units.frames
    assert float(np.dot(frames[21][:, 1], frames[0][:, 1])) >= 0.98
    # Right-handed, measured from the stored phosphates alone: successive
    # forward-strand phosphates turn counter-clockwise about the segment's
    # own +z axis.
    turns = [
        math.atan2(float(a[0] * b[1] - a[1] * b[0]), float(np.dot(a[:2], b[:2])))
        for a, b in itertools.pairwise(p_a)
    ]
    assert all(t > 0 for t in turns), turns

    # No bond_length_sanity, envelope_fit clean.
    body = handler.get(id="duplex", view="validate").body
    assert "bond_length_sanity" not in body
    assert "envelope_fit" not in body

    # Ports: measured 5p rot, the listed site only.
    ports = seg.ports
    assert ports["5p"].rot is not None and ports["5p"].rot_source == "bound"
    assert ports["5p"].pose is not None and ports["5p"].pose_source == "bound"
    # layout_chain's backbone-exit pose (gr458316) gave way to the atom:
    # its marker and outward direction are gone with it.
    assert "pose_from" not in ports["5p"].annotations
    assert ports["5p"].direction is None and ports["3p"].direction is None
    assert ports["5p"].bound_design == "duplex-h.s0"
    assert {"5p", "3p", "r5p", "r3p"} <= set(ports)
    assert {"n5_c5m", "n5_maj", "n5_min"} <= set(ports)
    assert not any(p.startswith("n6_") for p in ports)


def test_backbone_fidelity_keeps_three_atoms_and_the_same_theorems(
    handler: SeHandler, store: Store
) -> None:
    _put(handler, "trace", [*_duplex_ops(), _realize("h", 0, 21, fidelity="backbone")])
    coords, meta = _atoms(store, "trace-h.s0")
    assert set(meta["names"]) == set(BACKBONE_ATOMS)
    p_a = _select(coords, meta, "A", "P")
    pp = np.linalg.norm(np.diff(p_a, axis=0), axis=1)
    assert pp.min() >= 6.2 and pp.max() <= 7.2
    c1_a = _select(coords, meta, "A", "C1'")
    assert np.all(np.abs(np.diff(c1_a[:, 2]) - 3.34) <= 0.02)
    # An attachment site needs base atoms.
    with pytest.raises(BadInput, match="fidelity='allatom'"):
        _put(
            handler,
            "trace2",
            [*_duplex_ops(), _realize("h", 0, 21, fidelity="backbone", sites=[3])],
        )


def test_a_later_failing_op_leaves_no_structure_row(
    handler: SeHandler, store: Store
) -> None:
    """The mint is deferred past the WHOLE op list (``finish_generate``'s
    rule): a later op failing validation means nothing was minted. The
    item's stronger wording — "a forced ``save_tree`` failure leaves no
    structure row" — is not what this path guarantees: ``structure_save``
    commits in its own transaction immediately before ``save_tree``, so a
    hard crash between the two strands a real, unreferenced structure
    design (the residual :func:`precis_se.atomic.generate.finish_generate`
    documents, shared deliberately rather than solved for one op)."""
    with pytest.raises(BadInput):
        _put(
            handler,
            "orphan",
            [*_duplex_ops(), _realize("h", 0, 21), {"op": "set_pose", "block": "nope"}],
        )
    assert store.get_ref(kind="structure", id="orphan-h.s0") is None
    assert store.get_ref(kind="se", id="orphan") is None


def test_region_refusals_name_the_fix(handler: SeHandler, store: Store) -> None:
    with pytest.raises(BadInput, match="straddles segments"):
        _put(handler, "straddle", [*_duplex_ops(), _realize("h", 10, 22)])
    with pytest.raises(BadInput, match="layout_chain first"):
        _put(handler, "unlaid", [*_duplex_ops()[:-1], _realize("h", 0, 21)])
    _put(handler, "twice", [*_duplex_ops(), _realize("h", 0, 21)])
    with pytest.raises(BadInput, match="already bound"):
        handler.edit(id="twice", text=json.dumps({"ops": [_realize("h", 0, 21)]}))


# ── the hairpin: stem alone, then stem + loop after relax ───────────────


def test_hairpin_stem_realizes_and_the_loop_waits_for_relax(
    handler: SeHandler, store: Store, hub: Hub
) -> None:
    echo = _put(handler, "hp", [*_hairpin_ops(), _realize("stem", 0, 4)])
    assert "8 nucleotide(s)" in echo
    _coords, meta = _atoms(store, "hp-stem.s0")
    assert meta["chains"] == {"A": "hp"}
    assert max(meta["resseq"]) == 8

    with pytest.raises(Unsupported, match="4-nt loop of strand 'hp'.*no placed curve"):
        _put(handler, "hp-early", [*_hairpin_ops(), _realize("stem", 0, 4, loops=True)])
    assert store.get_ref(kind="se", id="hp-early") is None

    echo = _put(
        handler,
        "hp-late",
        [*_hairpin_ops(), {"op": "relax_chain"}, _realize("stem", 0, 4, loops=True)],
    )
    assert "12 nucleotide(s)" in echo
    coords, meta = _atoms(store, "hp-late-stem.s0")
    assert max(meta["resseq"]) == 12
    # One chain, bonded through the loop: every consecutive residue pair
    # has an O3'→P step under 2 Å except across the loop, where the
    # nucleotides sit at the loop's own spacing.
    assert meta["chain_ids"].count("A") == coords.shape[0]
    # The loop caps the helix end (gripe 457929): its phosphates rise
    # above the last pair's plane along the segment's own +z, instead of
    # all sitting at exactly that plane's height.
    resseq = np.asarray(meta["resseq"])
    is_p = np.asarray(meta["names"]) == "P"
    stem_top = float(coords[(resseq == 4) & is_p, 2].max())
    loop_z = coords[(resseq >= 5) & (resseq <= 8) & is_p, 2]
    assert loop_z.shape == (4,) and float(loop_z.max()) > stem_top + 2.0, loop_z

    # The PDB round-trips through the kernel's own reader.
    text = StructureHandler(hub=hub).get(id="hp-late-stem.s0", view="pdb").body
    assert text.count("\nATOM  ") + text.startswith("ATOM  ") == coords.shape[0]
    trace = read_trace(text, "P")
    assert list(trace) == ["A"] and trace["A"].shape == (12, 3)
    assert "DG" in text and "DA" in text and "DC" in text


def test_loop_residue_rows_persist_and_envelope_fit_skips_the_loop(
    handler: SeHandler, store: Store
) -> None:
    """gr457928: the loop bows out of the duplex tube by construction, so
    ``envelope_fit`` used to warn on every realized loop forever. The
    structure now carries one row per residue (offset ``None`` marks a
    loop nucleotide) and the check skips those atoms."""
    _put(
        handler,
        "hp-rows",
        [*_hairpin_ops(), {"op": "relax_chain"}, _realize("stem", 0, 4, loops=True)],
    )
    _coords, meta = _atoms(store, "hp-rows-stem.s0")
    rows = meta["residues"]
    assert len(rows) == 12
    assert [r[4] for r in rows] == [0, 1, 2, 3, None, None, None, None, 3, 2, 1, 0]
    assert "".join(r[5] for r in rows) == "GGGGAAAACCCC"
    assert {r[2] for r in rows} == {"hp"}
    body = handler.get(id="hp-rows", view="validate").body
    assert "envelope_fit" not in body, body


def test_relax_loops_chains_the_loop_backbone_with_the_duplex_pinned(
    handler: SeHandler, store: Store
) -> None:
    """``relax_loops=true``: a geometric relax over the loop nucleotides
    only. Every O3'→P step along the chain ends up a bond length, the
    duplex atoms do not move, and the echo reports the before/after."""
    ops = [*_hairpin_ops(), {"op": "relax_chain"}]
    flat_echo = _put(
        handler,
        "hp-flat",
        [*ops, _realize("stem", 0, 4, loops=True, relax_loops=False)],
    )
    assert "pass relax_loops=true to chain them" in flat_echo, flat_echo
    flat, _meta = _atoms(store, "hp-flat-stem.s0")
    # The default IS the relax (Reto's ruling on gr457928): an unqualified
    # loops=True realize chains the loop.
    echo = _put(handler, "hp-chained", [*ops, _realize("stem", 0, 4, loops=True)])
    assert "loop backbone chained by a geometric relax" in echo, echo
    coords, meta = _atoms(store, "hp-chained-stem.s0")
    resseq = np.asarray(meta["resseq"])
    names = np.asarray(meta["names"])

    def atom(r: int, n: str) -> np.ndarray:
        rows = np.flatnonzero((resseq == r) & (names == n))
        assert rows.shape == (1,)
        return coords[rows[0]]

    steps = [
        float(np.linalg.norm(atom(r + 1, "P") - atom(r, "O3'"))) for r in range(1, 12)
    ]
    report = meta["loop_relax"]
    assert report["max_step_before_A"] > 4.0
    assert max(steps) < 2.5, (steps, report)
    assert report["max_step_after_A"] < 2.5, report
    assert set(report) >= {"converged", "n_steps", "n_loop_atoms", "n_pinned_atoms"}
    duplex = (resseq <= 4) | (resseq >= 9)
    assert np.allclose(coords[duplex], flat[duplex], atol=1e-9)
    assert "envelope_fit" not in handler.get(id="hp-chained", view="validate").body


# ── the rectangle exports ───────────────────────────────────────────────


def test_scadnano_export_carries_every_domain_once() -> None:
    tree = _rectangle_tree()
    doc = json.loads(json.dumps(to_scadnano(tree, design="rect")))
    assert doc["grid"] == "square"
    assert len(doc["helices"]) == 24
    seen: list[tuple[int, bool, int, int]] = []
    scaffolds = 0
    for strand in doc["strands"]:
        scaffolds += int(strand["is_scaffold"])
        for d in strand["domains"]:
            if "loopout" in d:
                continue
            seen.append((d["helix"], d["forward"], d["start"], d["end"]))
    assert scaffolds == 1
    assert len(seen) == len(tree.domains) == len(set(seen))


def test_oxdna_export_is_one_path_per_strand() -> None:
    tree = _rectangle_tree()
    top, conf = to_oxdna(tree, design="rect")
    top_lines = top.strip().split("\n")
    n_nt, n_strands = (int(v) for v in top_lines[0].split())
    assert n_strands == 97
    rows = [line.split() for line in top_lines[1:]]
    assert len(rows) == n_nt
    total = sum(d.n_units + (d.loop_before_nt or 0) for d in tree.domains)
    assert n_nt == total
    # Follow 5' neighbours from each strand's 3' end: one path per strand
    # covering every nucleotide exactly once.
    by_strand: dict[str, list[int]] = {}
    for i, (sid, _base, _three, _five) in enumerate(rows):
        by_strand.setdefault(sid, []).append(i)
    for sid, members in by_strand.items():
        start = [i for i in members if rows[i][2] == "-1"]
        assert len(start) == 1, f"strand {sid} has {len(start)} 3' ends"
        walked = 0
        cur = start[0]
        while cur != -1:
            walked += 1
            assert rows[cur][0] == sid
            cur = int(rows[cur][3])
        assert walked == len(members)
    conf_lines = conf.strip().split("\n")
    assert len(conf_lines) == n_nt + 3
    assert conf_lines[0] == "t = 0" and conf_lines[1].startswith("b = ")


def test_cadnano_export_is_lattice_only() -> None:
    tree = _rectangle_tree()
    doc = to_cadnano(tree, design="rect")
    assert len(doc["vstrands"]) == 24
    assert all(len(v["scaf"]) == 256 for v in doc["vstrands"])
    scaf_used = sum(
        1 for v in doc["vstrands"] for b in v["scaf"] if b != [-1, -1, -1, -1]
    )
    assert scaf_used == 24 * 256
    # A waypoint helix has no row/col.
    arc = SeTree()
    apply_ops(
        arc,
        [
            {"op": "add_block", "name": "bent"},
            {
                "op": "declare_helix",
                "block": "bent",
                "n_units": 30,
                "path": {
                    "waypoints": [
                        ["0 m", "0 m", "0 m"],
                        ["0.5 nm", "0 m", "5 nm"],
                        ["0 m", "0 m", "10 nm"],
                    ]
                },
            },
            {"op": "add_block", "name": "s"},
            {"op": "declare_strand", "block": "s"},
            {
                "op": "add_domain",
                "strand": "s",
                "helix": "bent",
                "start": 0,
                "end": 30,
                "forward": True,
            },
        ],
    )
    with pytest.raises(Unsupported, match="helix 'bent'"):
        to_cadnano(arc, design="arc")


def test_export_view_takes_only_format(handler: SeHandler, store: Store) -> None:
    _put(handler, "rect-view", [*_duplex_ops(), _realize("h", 0, 21)])
    body = handler.get(id="rect-view", view="export", args={"format": "scadnano"}).body
    assert json.loads(body)["grid"] == "none"
    pdb = handler.get(id="rect-view", view="export", args={"format": "pdb"}).body
    # One PDB chain per (segment, strand): the duplex region's two strands
    # are two chains (dogfood 2026-09-30: a per-segment chain id merged
    # them into one chain with duplicate residue numbers).
    assert pdb.count("TER") == 2 and "ATOM" in pdb
    assert " DG A   1 " in pdb and " B   1 " in pdb
    with pytest.raises(BadInput):
        handler.get(id="rect-view", view="export", args={"fmt": "pdb"})
    with pytest.raises(Unsupported, match="one of"):
        handler.get(id="rect-view", view="export", args={"format": "xyz"})


# ── the pure geometry helper ────────────────────────────────────────────


def test_build_region_is_right_handed_and_bonds_the_backbone() -> None:
    from precis_chain.fibre import unit_frames
    from precis_chain.path import polyline

    motif = nucleic.MOTIFS["B-DNA"]
    n = 12
    path = polyline(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, (n - 1) * motif.rise]]))
    frames = unit_frames(path, motif, n, 0.0, r0=np.array([1.0, 0.0, 0.0]))
    units = [
        PlacedUnit(
            k,
            frames.origins[k] * 1e10,
            frames.frames[k],
            (
                UnitOccupant("s", 0, True, SEQ22[k]),
                UnitOccupant("t", 0, False, _COMPLEMENT[SEQ22[k]]),
            ),
        )
        for k in range(n)
    ]
    region = build_region(units)
    assert math.degrees(helix_handedness(region)) > 0
    inter = [
        float(np.linalg.norm(region.coords_A[i] - region.coords_A[j]))
        for i, j in region.bonds
        if region.resseq[i] != region.resseq[j]
    ]
    assert len(inter) == 2 * (n - 1)
    assert all(1.45 <= d <= 1.65 for d in inter), inter


def test_layouts_own_5p_3p_ports_get_the_realizers_chemistry(
    handler: SeHandler, store: Store
) -> None:
    """``layout_chain`` pre-mints ``5p``/``3p`` as bare backbone anchors;
    after ``realize_chain`` they carry the same expected element and
    annotations as the ports the realizer mints itself (gripe 457930)."""
    _put(handler, "duplex", [*_duplex_ops(), _realize("h", 0, 21)])
    ports = _loaded(store, "duplex").blocks["h.s0"].ports
    for name, element in (("5p", "P"), ("3p", "O"), ("r5p", "P"), ("r3p", "O")):
        port = ports[name]
        assert port.expected_element == element, name
        assert port.annotations["strand"] == ("fwd" if name in ("5p", "3p") else "rev")
        assert {"strand", "end", "offset", "atoms"} <= set(port.annotations), name
        assert len(port.annotations["atoms"]) == 1
        assert port.bound_atom == port.annotations["atoms"][0]
    # The layout's slot survives (its role), it was not re-minted bare.
    assert ports["5p"].roles == ["backbone"]
