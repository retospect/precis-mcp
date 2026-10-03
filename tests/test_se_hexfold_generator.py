"""The ``hexfold`` generator — :mod:`precis_se.atomic.generators.hexfold_spec`'s
adapter between the topology-only ``.hx`` notation package and the
``generate`` op's ``GeneratedBlock`` contract.

hexfold is vendored at ``src/hexfold/`` — no import skip needed.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

from precis.cad import dsl as cad_dsl
from precis.store import Store
from precis.structure.scene import Atom as StructAtom
from precis.structure.scene import Scene as StructScene
from precis_se.atomic import validate as atomic_validate
from precis_se.atomic.generate import generated_cell, ingest_envelope, prepare_generate
from precis_se.atomic.generators import GENERATORS, GeneratorError
from precis_se.atomic.generators._types import ENVELOPE_UNIT, GeneratedBlock
from precis_se.atomic.generators.sp2 import VDW_MARGIN_A
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


def _sha(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(raw).hexdigest()


def _golden_fingerprint(block: GeneratedBlock) -> dict[str, object]:
    """The post-processed output a refactor must not move: rounded coords
    (6 dp, so last-bit float noise across CPUs cannot redden it), the
    canonical topology JSON, the envelope, port names + atom indices,
    bonds (order and kind included), the length anchors, the topology key
    set and the provenance text (both coordinate-independent)."""
    return {
        "n_atoms": len(block.elements),
        "coords": _sha(np.round(block.coords, 6).astype("<f8").tobytes()),
        "canonical_json": _sha(str(block.topology["canonical_json"])),
        "envelope": block.envelope,
        "ports": [(p.name, p.atom_index) for p in block.ports],
        "bonds": _sha(repr(block.bonds)),
        "measures": _sha(
            repr([(m.name, m.value_A, m.min_A, m.max_A) for m in block.measures])
        ),
        "topology_keys": sorted(block.topology),
        "provenance": _sha(block.provenance),
    }


_PILLAR_HX = Path(__file__).resolve().parents[1] / "hexfold" / "examples" / "pillar.hx"

_GOLDEN_SPECS = {
    "tube": TUBE_SPEC,
    "nanobud": NANOBUD_SPEC,
    "sheet": SHEET_A_SPEC,
    # A sheet hole fused to a tube: canonical frame + (terminated) ports.
    "pillar": _PILLAR_HX.read_text(encoding="utf-8"),
    # Fused multi-part with surviving ports. ``fit in {...}`` makes the
    # coordinates platform-sensitive beyond 6 dp (macOS digests differ from
    # Linux); the Linux container values are pinned.
    "sheet_tube_cap": (
        "hexfold 0.2\nlattice: element=C sigma=1.42\n\ns: sheet(25A, 12)\n"
        "t: tube(fit in {(5,5),(6,6)}, len=3)\nc: cap(5,5)\n"
        "t.out --fuse k=0--> c.in\n"
    ),
}

_GOLDEN_TOPOLOGY_KEYS = [
    "canonical_json",
    "hexfold",
    "measures",
    "n_atoms",
    "n_bonds",
    "ports",
    "regions",
    "report",
    "rings",
    "seed_kind",
    "spec",
]

_GOLDEN: dict[str, dict[str, object]] = {
    "tube": {
        "n_atoms": 80,
        "coords": "09d0a14564f9228d7d7a07efae0e05e169cc33ca4da6e8efb74d75d40a412bbc",
        "canonical_json": "101eee8e489258b025478bf4dc08143a107e425b061f54e354730412ecacae7d",
        "envelope": "cyl:r5.1492Åh11.999Å",
        "ports": [("in", 0), ("out", 79)],
        "bonds": "1f4e1bd69c50d4442ee7ecb75d8db9b512080a85ab6146929642ad01b977c6f2",
        "measures": "5285d138cf0f585d6d86e922b040fd45ad9958b950d878544d43a18f30791f33",
        "topology_keys": _GOLDEN_TOPOLOGY_KEYS,
        "provenance": "0ee0ed53bfd7b64b379c3e420d6db60924dfd02e1c54db3301cf9949196834a9",
    },
    "nanobud": {
        "n_atoms": 460,
        "coords": "e1dacd77a1008cb072253d1ef44bb7b7f7d11106208799c995963405c955850f",
        "canonical_json": "7dc5e93ae5dd55561d7ed31b9a43d25815a9b5c32146431f3a65bf7e32cacee4",
        "envelope": "cyl:r16.7786Åh16.9395Å",
        "ports": [("h_in", 60), ("h_out", 85)],
        "bonds": "cfe212fc407a8934b362a5fa64bbc027c5f73767b42249726a83dcca719389f4",
        "measures": "4cdf0586c43c1a10762d94da35c5b44e2e23ce81a69351aa3b48986473680879",
        "topology_keys": _GOLDEN_TOPOLOGY_KEYS,
        "provenance": "ea04c0818b2031d9a3b2725568fa6a1d2df06b4e6a575fb7e1be7180a3c5eafb",
    },
    "sheet": {
        "n_atoms": 240,
        "coords": "fef0d679b06c9f389ad8de80006b905e41269d9a3bc1ea34031753d643d979ef",
        "canonical_json": "1e477fe7ec10211e8fc4b010163ed279b7d406947fe42be6ceea7dba42b51f58",
        "envelope": "cyl:r23.7426Åh3.5399Å",
        "ports": [("rim", 2)],
        "bonds": "a0ad85716b042fa848d77ffd99d507d62eee86cb28c77027fe79e596c07e972a",
        "measures": "86b0d23f4b3e9be3b6bfbe8698d430367fa965924b016ab749b06610399c5ea8",
        "topology_keys": _GOLDEN_TOPOLOGY_KEYS,
        "provenance": "cbbe71cea2fca41400e01bc952ce9d67a396679ead1059f60016a4fcfc480499",
    },
    "pillar": {
        "n_atoms": 972,
        "coords": "4f5d896bf8354a545010fbd68c34ebffd56735e862f981f72500c1039843d9d8",
        "canonical_json": "398e9fe24d61191ff5dd68bf9d90acde7e8da694bc06a5ff26b52a91d36eb198",
        "envelope": "cyl:r43.0831Åh21.9105Å",
        "ports": [],
        "bonds": "d6a47a0cc4d34c5f761b724db4c0255ece9a248685fa426f3864a389ef4b5240",
        "measures": "c915f64651e03e526dc153dd76a67155f9d9807a77733f248b0f7bd4fc269265",
        "topology_keys": _GOLDEN_TOPOLOGY_KEYS,
        "provenance": "5ab02a432ee45bb27273e1c9ac0162e15a773b70f4858e7770acee15aff57d10",
    },
    "sheet_tube_cap": {
        "n_atoms": 350,
        "coords": "dbca6defb3aabf65d08588734830df2ba3d9029aa9ddf1fff52b4fd8c8e2ad92",
        "canonical_json": "a6d669121d332f7b2aca3cd30a3b3766158cc3eb05d110f8276b6c76d087019f",
        "envelope": "cyl:r13.7248Åh50.1512Å",
        "ports": [("s_rim", 52), ("t_in", 290)],
        "bonds": "9ca866d58dc55d22061d5e38e0c61355189e5e1a1c6f4fda4d3096d429c083ea",
        "measures": "ad0f997d7453cc1d15f375c85d154fe856c88e55d7c5dd829bcf5d0df030a590",
        "topology_keys": _GOLDEN_TOPOLOGY_KEYS,
        "provenance": "4d982d52a749f42a974ea412d3f6045bb527228a640a97f5444c1d577c72b246",
    },
}


# Golden: pins today's hexfold generator output (orchestrator S4b verdict,
# 2026-10-03). Re-pin only with a hexfold __version__ bump (gr464341 rule),
# never to make a refactor pass.
@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            n,
            marks=pytest.mark.skipif(
                sys.platform != "linux",
                reason=(
                    "gr464502: stick relax of this fit spec differs across "
                    "platforms; Linux values pinned"
                ),
            ),
        )
        if n == "sheet_tube_cap"
        else n
        for n in sorted(_GOLDEN_SPECS)
    ],
)
def test_hexfold_output_is_pinned_golden(name: str) -> None:
    block = _build(_GOLDEN_SPECS[name])
    assert _golden_fingerprint(block) == _GOLDEN[name]


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


_FRAME_SPECS = {
    "tube": TUBE_SPEC,
    "sheet": SHEET_A_SPEC,
    "sheet_tube_cap": (
        "hexfold 0.2\nlattice: element=C sigma=1.42\n\ns: sheet(25A, 12)\n"
        "t: tube(fit in {(5,5),(6,6)}, len=3)\nc: cap(5,5)\n"
        "t.out --fuse k=0--> c.in\n"
    ),
    "c60": "hexfold 0.2\nlattice: element=C sigma=1.42\n\nb: fullerene(C60)\n",
    "nanobud": NANOBUD_SPEC,
}


def _scene_of(block: GeneratedBlock) -> StructScene:
    """The GeneratedBlock -> Scene conversion ``prepare_generate`` does,
    so ``envelope_fit`` sees the atoms exactly as a design would."""
    scene = StructScene(cell=generated_cell(block.coords))
    for element, cart in zip(block.elements, block.coords, strict=True):
        label = scene.next_label(element)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(np.asarray(cart, dtype=float)))
        scene.atoms[label] = StructAtom(
            label=label, element=element, frac=frac, hybridization="sp2"
        )
    return scene


@pytest.mark.parametrize("name", sorted(_FRAME_SPECS))
def test_hexfold_atoms_sit_inside_their_cylinder_envelope(name: str) -> None:
    """``envelope_fit`` reads ``cyl:r<>h<>`` as the z axis, z in [0, h],
    radially centred on the origin. The generator used to describe a
    cylinder about a PCA axis but leave the atoms unmoved, so every
    non-tube block protruded (sheet 28 A, C60 1.7 A -- dogfood
    2026-09-27). Now the coordinates are moved into the envelope's frame,
    so no hexfold block protrudes -- flat, closed, fused or multi-part."""
    block = _build(_FRAME_SPECS[name])
    assert (
        atomic_validate.envelope_fit(ingest_envelope(block.envelope), _scene_of(block))
        is None
    )
    z = block.coords[:, 2]
    assert z.min() == pytest.approx(VDW_MARGIN_A, abs=1e-6)
    radial = np.linalg.norm(block.coords[:, :2], axis=1)
    spec = cad_dsl.parse(block.envelope.replace(ENVELOPE_UNIT, ""))
    assert spec.alias == "cyl"
    assert radial.max() == pytest.approx(spec.params["r"] - VDW_MARGIN_A, abs=1e-3)
    assert z.max() == pytest.approx(spec.params["h"] - VDW_MARGIN_A, abs=1e-3)


def test_generated_record_is_persisted_and_rendered_in_the_block_view(
    store: Store,
) -> None:
    """After minting, the build report must be readable: ``finish_generate``
    lands a trimmed record (spec, rings, counts, report, measures -- not
    the per-atom regions/ports) on the structure ref's ``meta['generated']``
    and ``view='block'`` renders it as ``## generated`` (dogfood
    2026-09-27: ``extent.snap``/``fit.propagated`` for a minted block were
    unreachable through every se view)."""
    from precis_se.atomic.generate import finish_generate
    from precis_se.handler import _render_block

    tree = SeTree()
    _echo, pending = prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": _FRAME_SPECS["sheet_tube_cap"]},
            "name": "blk",
        },
        "hx-rec",
    )
    assert pending is not None and pending.generated is not None
    assert set(pending.generated) >= {"generator", "spec", "report", "rings", "n_atoms"}
    assert "regions" not in pending.generated and "ports" not in pending.generated
    finish_generate(store, tree, pending)

    ref = store.get_ref(kind="structure", id=pending.struct_slug)
    assert ref is not None
    rec = (ref.meta or {})["generated"]
    codes = {f["code"] for f in rec["report"]["findings"]}
    assert {"extent.snap", "fit.propagated", "seam.rings"} <= codes

    body = _render_block(tree, tree.blocks["blk"], store, ref_id=0)
    assert "## generated (hexfold)" in body
    assert "extent.snap" in body and "fit.propagated" in body
    assert "spec (regeneration input):" in body and "t.out --fuse k=0--> c.in" in body

    # A later plain re-save (a relax would do this) keeps the record.
    store.structure_save(
        slug=pending.struct_slug,
        title=pending.title,
        scene=pending.scene,
        version=2,
        card_text=pending.card_text,
    )
    ref2 = store.get_ref(kind="structure", id=pending.struct_slug)
    assert ref2 is not None and "generated" in (ref2.meta or {})
