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


def test_hexfold_ports_carry_the_lattice_tag_and_rim_payload() -> None:
    """The port *type* seam (GeneratedPort.lattice/payload): every hexfold
    port is an ``sp2-hex`` rim and its payload is the SPEC 10 rim instance
    -- word, dangling count and rim type -- mirrored into
    ``topology.ports`` for consumers that read the block, not the ports."""
    block = _build(TUBE_SPEC)
    assert {p.lattice for p in block.ports} == {"sp2-hex"}
    for p in block.ports:
        assert p.payload is not None
        assert p.payload["kind"] == "rim"
        assert p.payload["N"] == 10
        assert p.payload["type"] == "a10"  # tube(5,5): armchair, N = 2n
        assert p.payload["word"]
        mirrored = block.topology["ports"][p.name]
        assert mirrored["lattice"] == "sp2-hex"
        assert mirrored["type"] == "a10"
    # the pre-existing single-atom generators leave both unset
    cnt = GENERATORS["cnt"]({"n": 6, "m": 6, "length_A": 8.0})
    assert all(p.lattice is None and p.payload is None for p in cnt.ports)


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


def test_prepare_generate_multi_instance_ports_are_undotted(store: Store) -> None:
    """A spec with several instances gets hexfold-qualified rim names
    (``h.in``); se's ``add_port`` reserves the dot for ``block.port``
    endpoints, so the adapter maps them to ``h_in`` and keeps the hexfold
    path under ``topology.ports[<name>].hx`` (found by the dev-DB dogfood,
    docs/backlog/hexfold-integration.md step 4)."""
    tree = SeTree()
    echo, pending = prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": NANOBUD_SPEC},
            "name": "bud",
        },
        "hx-design",
    )
    assert pending is not None
    # the echo names the topology facts, it does not dump them (regions
    # and ports carry every atom ordinal; the spec and canonical JSON
    # are kilobytes)
    assert len(echo) < 1500, len(echo)
    assert "ports={h_in,h_out}" in echo and "canonical_json=" in echo
    node = tree.blocks["bud"]
    assert set(node.ports) == {"h_in", "h_out"}
    assert not any("." in p for p in node.ports)
    block = _build(NANOBUD_SPEC)
    assert {p.name for p in block.ports} == set(node.ports)
    hx_paths = {p: v["hx"] for p, v in block.topology["ports"].items()}
    assert hx_paths == {"h_in": "h.in", "h_out": "h.out"}


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


# ── generated measures (hexfold-integration.md step 3) ────────────────

SHEET_A_SPEC = """\
hexfold 0.2

lattice: element=C sigma=1.42

s: sheet(25A, 12)
"""


def test_hexfold_block_carries_length_anchors_with_snap_bands() -> None:
    """A sheet authored in Å snaps to whole cells (``extent.snap``) and the
    block declares the realised extent as a measure whose band is the
    snap cell; tubes declare ``len`` (band = snap period) and ``R`` (a
    point)."""
    block = _build(SHEET_A_SPEC)
    codes = [f["code"] for f in block.topology["report"]["findings"]]
    assert "extent.snap" in codes
    by_name = {m.name: m for m in block.measures}
    assert set(by_name) == {"s_W", "s_H"}
    w = by_name["s_W"]
    a = 1.42 * 3**0.5
    assert w.value_A == pytest.approx(10 * a, abs=1e-6)
    assert w.min_A == pytest.approx(9.5 * a, abs=1e-6)
    assert w.max_A == pytest.approx(10.5 * a, abs=1e-6)
    assert w.reason and "snap cell" in w.reason
    assert [m["name"] for m in block.topology["measures"]] == ["s_W", "s_H"]

    tube = _build(TUBE_SPEC)
    names = {m.name for m in tube.measures}
    assert names == {"post_len", "post_R"}
    r = next(m for m in tube.measures if m.name == "post_R")
    assert r.min_A is None and r.max_A is None
    assert r.value_A == pytest.approx(a * (75**0.5) / (2 * 3.141592653589793), abs=1e-4)


def test_prepare_generate_lands_measures_and_stackup_uses_them(store: Store) -> None:
    """The generated anchors are ordinary se measures: ``prepare_generate``
    mints them in metres on the block, and a user relation onto
    ``sheet.s_W`` is evaluated by ``stackup`` -- agreeing within the
    relation's tolerance, or a ``mismatch`` beyond it."""
    from precis_se.measures import stackup
    from precis_se.ops import apply_ops

    tree = SeTree()
    _echo, pending = prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": SHEET_A_SPEC},
            "name": "sheet",
        },
        "hx-design",
    )
    assert pending is not None
    got = {m.name: m for m in tree.measures if m.block == "sheet"}
    assert set(got) == {"s_W", "s_H"}
    w = got["s_W"]
    a_m = 1.42e-10 * 3**0.5
    assert w.unit == "m" and w.strength == "gauge"
    assert w.value == pytest.approx(10 * a_m, rel=1e-9)
    assert w.min_value == pytest.approx(9.5 * a_m, rel=1e-9)
    assert w.max_value == pytest.approx(10.5 * a_m, rel=1e-9)
    assert not stackup(tree.measures)  # clean anchors produce no rows

    # the user's requested 25 Å, related to the realised extent with a
    # half-Å tolerance: the 0.405 Å snap delta is inside it
    apply_ops(
        tree,
        [
            {
                "op": "add_measure",
                "block": "sheet",
                "name": "width_req",
                "value": 25e-10,
                "relation": {"source": "sheet.s_W", "offset": 0.0, "tol": 0.5e-10},
            }
        ],
    )
    rows = {r.measure: r for r in stackup(tree.measures)}
    assert rows["sheet.width_req"].problem is None
    assert rows["sheet.width_req"].derived == pytest.approx(10 * a_m, rel=1e-9)

    # a tighter tolerance than the snap delta is a stack-up mismatch
    apply_ops(
        tree,
        [
            {
                "op": "add_measure",
                "block": "sheet",
                "name": "width_tight",
                "value": 25e-10,
                "relation": {"source": "sheet.s_W", "offset": 0.0, "tol": 0.1e-10},
            }
        ],
    )
    rows = {r.measure: r for r in stackup(tree.measures)}
    assert rows["sheet.width_tight"].problem_kind == "mismatch"


def test_hexfold_check_mode_surfaces_parse_errors_as_text() -> None:
    """``fidelity='check'`` is the path a caller iterates a spec on, so a
    ParseError must come back as ``line:col: message`` in a GeneratorError
    -- not escape as an internal error (dogfood 2026-09-27: every guess at
    the fuse grammar read ``internal error in put: ParseError``)."""
    with pytest.raises(GeneratorError, match=r"^\d+:\d+: "):
        _build(
            "hexfold 0.2\nt: tube(5,5,len=3)\nfuse t.out --> t.in k=0\n",
            fidelity="check",
        )


def test_hexfold_internal_crash_is_named_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-hexfold exception out of the compiler is a hexfold bug; the
    caller gets told so with the exception text, in both fidelities."""
    from precis_se.atomic.generators import hexfold_spec as mod

    def boom(*_a: object, **_k: object) -> object:
        raise ValueError("invalid literal for int() with base 10: 'fit'")

    monkeypatch.setattr(mod, "check", boom)
    with pytest.raises(GeneratorError, match="hexfold internal error.*invalid literal"):
        _build(TUBE_SPEC, fidelity="check")
    monkeypatch.setattr(mod, "build", boom)
    with pytest.raises(GeneratorError, match="hexfold internal error.*File a gripe"):
        _build(TUBE_SPEC)
