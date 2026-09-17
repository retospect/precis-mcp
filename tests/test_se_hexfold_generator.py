"""The ``hexfold`` generator — :mod:`precis_se.atomic.generators.hexfold_spec`'s
adapter between the topology-only ``.hx`` notation package and the
``generate`` op's ``GeneratedBlock`` contract.

hexfold is vendored at ``src/hexfold/`` — no import skip needed.
"""

from __future__ import annotations

import pytest

from precis.cad import dsl as cad_dsl
from precis.store import Store
from precis_se.atomic.generate import prepare_generate
from precis_se.atomic.generators import GENERATORS, GeneratorError
from precis_se.atomic.generators._types import GeneratedBlock
from precis_se.ops import SeTree

TUBE_SPEC = """\
hexfold 0.1

lattice: element=C sigma=1.42

origin post
post: tube(5,5,len=4)
"""

#: [2+2] cycloaddition bud (Nasibulin 2007): two authored bonds from the
#: C60 6-6 bond onto an intact armchair host — 4 atoms go sp3.
NANOBUD_SPEC = """\
hexfold 0.1

lattice: element=C sigma=1.42

origin h
h: tube(10,10, len=10)
b: fullerene(C60)
b @ h/(4,0,A):0 [2+2]
"""


def _build(spec: str, **params: object) -> GeneratedBlock:
    return GENERATORS["hexfold"]({"spec": spec, **params})


def test_hexfold_tube_counts_and_ports() -> None:
    block = _build(TUBE_SPEC)
    assert len(block.elements) == 80
    assert len(block.bonds) == 110
    assert len(block.ports) == 2
    assert block.hybridizations is not None
    assert set(block.hybridizations) == {"sp2"}
    assert len(block.hybridizations) == 80
    # Every remaining port carries its whole dangling ring.
    assert all(p.atoms is not None and len(p.atoms) == 10 for p in block.ports)
    cad_dsl.parse(block.envelope, require_units=True)


def test_hexfold_nanobud_sp3_bonds_are_order_one() -> None:
    block = _build(NANOBUD_SPEC)
    assert block.hybridizations is not None
    sp3 = {i for i, h in enumerate(block.hybridizations) if h == "sp3"}
    assert len(sp3) == 4
    for i, j, order, _kind in block.bonds:
        if i in sp3 or j in sp3:
            assert order == 1.0


def test_hexfold_fidelity_check_returns_report_and_mints_nothing() -> None:
    block = _build(TUBE_SPEC, fidelity="check")
    assert block.dry_run is True
    assert "euler.chi" in block.provenance
    assert block.elements == [] and block.bonds == [] and block.ports == []
    assert block.envelope == ""


def test_hexfold_fidelity_stick_builds_atoms() -> None:
    block = _build(TUBE_SPEC, fidelity="stick")
    assert block.dry_run is False
    assert len(block.elements) == 80
    assert len(block.bonds) == 110
    assert "fidelity=stick" in block.provenance


def test_hexfold_fidelity_defaults_to_stick() -> None:
    # No fidelity/dry_run at all → the old non-dry-run behavior.
    block = _build(TUBE_SPEC)
    assert block.dry_run is False
    assert len(block.elements) == 80


def test_hexfold_unsupported_fidelity_raises() -> None:
    with pytest.raises(GeneratorError, match="not wired yet"):
        _build(TUBE_SPEC, fidelity="geo")
    with pytest.raises(GeneratorError, match="not wired yet"):
        _build(TUBE_SPEC, fidelity="emt")
    with pytest.raises(GeneratorError, match="not wired yet"):
        _build(TUBE_SPEC, fidelity="ml")
    with pytest.raises(GeneratorError, match="fidelity must be one of"):
        _build(TUBE_SPEC, fidelity="not-a-real-tier")


def test_hexfold_dry_run_alias_maps_to_fidelity() -> None:
    # dry_run=True ⇒ fidelity="check": same report-only, mint-nothing shape.
    checked = _build(TUBE_SPEC, dry_run=True)
    assert checked.dry_run is True
    assert "euler.chi" in checked.provenance
    # dry_run=False ⇒ fidelity="stick": same build-and-mint shape.
    stuck = _build(TUBE_SPEC, dry_run=False)
    assert stuck.dry_run is False
    assert len(stuck.elements) == 80
    # Both given and agreeing is fine either way.
    _build(TUBE_SPEC, dry_run=True, fidelity="check")
    _build(TUBE_SPEC, dry_run=False, fidelity="stick")
    # Both given and disagreeing raises.
    with pytest.raises(GeneratorError, match="disagree"):
        _build(TUBE_SPEC, dry_run=True, fidelity="stick")
    with pytest.raises(GeneratorError, match="disagree"):
        _build(TUBE_SPEC, dry_run=False, fidelity="check")


def test_hexfold_bad_spec_raises_generator_error() -> None:
    # A spec whose check fires ERROR findings → BuildError → GeneratorError
    # carrying the rendered report.
    with pytest.raises(GeneratorError, match="port.mismatch"):
        _build(
            "hexfold 0.1\n"
            "a: tube(5,0,len=3)\n"
            "b: tube(6,0,len=3)\n"
            "a.out --fuse k=0--> b.in\n"
        )
    # Unparseable text → ParseError → GeneratorError.
    with pytest.raises(GeneratorError):
        _build("not a spec at all")


def test_hexfold_determinism() -> None:
    a, b = _build(NANOBUD_SPEC), _build(NANOBUD_SPEC)
    assert a.coords.tobytes() == b.coords.tobytes()
    assert a.topology["canonical_json"] == b.topology["canonical_json"]


# ── adapter round-trip through prepare_generate ───────────────────────


def test_prepare_generate_binds_atoms_and_ports(store: Store) -> None:
    tree = SeTree()
    echo, pending = prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": TUBE_SPEC},
            "name": "tube",
        },
        "hx-design",
    )
    assert pending is not None
    assert "80 atom(s)" in echo and "110 bond(s)" in echo
    assert len(pending.scene.atoms) == 80
    assert len(pending.scene.bonds) == 110
    assert len(pending.ports_map) == 2
    # Ports are bound to real atom labels in the minted scene.
    assert all(label in pending.scene.atoms for label in pending.ports_map.values())
    # Per-atom hybridization made it onto the scene atoms.
    assert {a.hybridization for a in pending.scene.atoms.values()} == {"sp2"}
    # The block landed in the tree with ports.
    node = tree.blocks["tube"]
    assert set(node.ports) == {"in", "out"}


def test_prepare_generate_fidelity_check_returns_none_pending(store: Store) -> None:
    tree = SeTree()
    echo, pending = prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": TUBE_SPEC, "fidelity": "check"},
            "name": "probe",
        },
        "hx-design",
    )
    assert pending is None
    assert "euler.chi" in echo
    assert "probe" not in tree.blocks


def test_prepare_generate_dry_run_alias_returns_none_pending(store: Store) -> None:
    tree = SeTree()
    echo, pending = prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": TUBE_SPEC, "dry_run": True},
            "name": "probe",
        },
        "hx-design",
    )
    assert pending is None
    assert "euler.chi" in echo
    assert "probe" not in tree.blocks
