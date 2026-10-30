"""pocket-from-molecule slice 2 (docs/backlog/pocket-from-molecule.md):
the region computers DRC actually runs (2a: ``measure_unmet`` /
``measure_uncomputable``, realised values in ``view='pockets'``) and the
``pocket_from_molecule`` handler op with ``mol:``/``mpatch:`` tokens and
the pocket ``class_hash`` (2b).

The wall fixture is a small bound structure read through stubbed GFN2 —
``_FakeTBLite`` returns alternating ±0.06 e charges, so realised values
are deterministic.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from ase.calculators.calculator import Calculator, all_changes

from precis.dispatch import Hub
from precis.errors import Unsupported
from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom as StructAtom
from precis.structure.scene import Scene as StructScene
from precis_se.handler import SeHandler

_NO = np.array([[0.0, 0.0, 0.0], [1.155, 0.0, 0.0]])


def _run_migration(name: str) -> None:
    import psycopg

    from precis.store.migrate import _execute_dump_sql
    from tests.conftest import MIGRATIONS_DIR, _active_dsn

    sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            _execute_dump_sql(cur, sql)


@pytest.fixture
def taxonomy(store: Store) -> Store:
    """The measurand taxonomy incl. 0228's electrostatic potential."""
    _run_migration("0174_taxon_seed.sql")
    _run_migration("0182_se_measurand_seed.sql")
    _run_migration("0228_se_measurands_esp_binding.sql")
    return store


class _FakeTBLite(Calculator):
    """A TBLite stand-in: records its kwargs, returns ±0.06 e charges."""

    implemented_properties = ["energy", "free_energy", "forces", "charges"]
    seen: list[dict[str, Any]] = []

    def __init__(self, atoms=None, **kwargs: Any):
        super().__init__(atoms=atoms)
        _FakeTBLite.seen.append(kwargs)

    def calculate(
        self, atoms=None, properties=("energy",), system_changes=all_changes
    ) -> None:
        super().calculate(atoms, properties, system_changes)
        assert self.atoms is not None
        n = len(self.atoms)
        q = np.array([0.06 if i % 2 == 0 else -0.06 for i in range(n)])
        self.results = {
            "energy": -185.9,
            "free_energy": -185.9,
            "forces": np.zeros((n, 3)),
            "charges": q - q.mean(),
        }


def _stub_tblite(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("tblite.ase")
    setattr(fake, "TBLite", _FakeTBLite)
    monkeypatch.setitem(sys.modules, "tblite.ase", fake)
    monkeypatch.setattr(_FakeTBLite, "seen", [])


def _save_structure(
    store: Store, slug: str, elements: list[str], xyz: np.ndarray
) -> None:
    cell = Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False))
    scene = StructScene(cell=cell)
    for el, p in zip(elements, xyz, strict=True):
        label = scene.next_label(el)
        frac = cell.cart_to_frac(np.asarray(p) + 10.0)
        scene.atoms[label] = StructAtom(label, el, frac)
    store.structure_save(
        slug=slug, title=slug, scene=scene, version=1, card_text="test."
    )


def _bound_design(
    store: Store, slug: str, elements: list[str], xyz: np.ndarray
) -> SeHandler:
    """A design ``slug`` whose block ``wall`` binds a structure holding
    ``elements`` at ``xyz`` (Å)."""
    _save_structure(store, f"{slug}-struct", elements, xyz)
    se = SeHandler(hub=Hub(store=store))
    se.put(
        id=slug,
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "wall",
                        "envelope": "sphere:r4e-10",
                    },
                    {
                        "op": "bind_structure",
                        "block": "wall",
                        "design": f"{slug}-struct",
                    },
                ]
            }
        ),
    )
    return se


def _drc(se: SeHandler, slug: str) -> str:
    return se.get(id=slug, view="drc").body


def test_computed_measure_outside_band_reports_measure_unmet(
    store: Store, taxonomy: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Region computers run at read: an N-N wall's stub charges sum to 0 e,
    so a required net partial charge of +0.4 e reads measure_unmet —
    never measurand_unchecked."""
    _stub_tblite(monkeypatch)
    se = _bound_design(store, "wall-chg", ["N", "O"], _NO)
    se.edit(
        id="wall-chg",
        ops=[
            {
                "op": "add_measure",
                "block": "wall",
                "name": "q",
                "measurand": "net partial charge",
                "datum": "atoms:wall[0-1]",
                "min": 0.3,
                "max": 0.5,
            }
        ],
    )
    body = _drc(se, "wall-chg")
    assert "measure_unmet" in body and "wall.q" in body
    assert "measurand_unchecked" not in body
    assert "measure_uncomputable" not in body


def test_computed_measure_inside_band_reports_nothing(
    store: Store, taxonomy: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A band covering the realised 0 e is met silently — no finding."""
    _stub_tblite(monkeypatch)
    se = _bound_design(store, "wall-ok", ["N", "O"], _NO)
    se.edit(
        id="wall-ok",
        ops=[
            {
                "op": "add_pocket",
                "block": "wall",
                "name": "p",
                "regions": [{"selector": "atoms:wall[0-1]"}],
            },
            {
                "op": "add_measure",
                "block": "wall",
                "name": "q",
                "measurand": "net partial charge",
                "datum": "atoms:wall[0-1]",
                "min": -0.1,
                "max": 0.1,
            },
        ],
    )
    body = _drc(se, "wall-ok")
    assert "measure_unmet" not in body
    assert "measure_uncomputable" not in body
    assert "measurand_unchecked" not in body
    pockets = se.get(id="wall-ok", view="pockets").body
    assert re.search(r"realised: -?\d", pockets)


def test_uncomputable_measure_names_the_reason(
    store: Store, taxonomy: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A region naming atoms beyond the structure resolves to an error —
    the computer returns None and DRC carries the reason."""
    _stub_tblite(monkeypatch)
    se = _bound_design(store, "wall-gone", ["N", "O"], _NO)
    se.edit(
        id="wall-gone",
        ops=[
            {
                "op": "add_measure",
                "block": "wall",
                "name": "q",
                "measurand": "net partial charge",
                "datum": "atoms:wall[42]",
            }
        ],
    )
    body = _drc(se, "wall-gone")
    assert "measure_uncomputable" in body and "wall.q" in body
    assert "has 2 atoms" in body
    assert "measurand_unchecked" not in body


def test_no_backend_measure_is_uncomputable_not_unchecked(
    store: Store, taxonomy: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GFN2 failure is carried as the measure_uncomputable reason, not
    a traceback."""
    se = _bound_design(store, "wall-nobe", ["N", "O"], _NO)
    se.edit(
        id="wall-nobe",
        ops=[
            {
                "op": "add_measure",
                "block": "wall",
                "name": "q",
                "measurand": "net partial charge",
                "datum": "atoms:wall[0-1]",
            }
        ],
    )
    import precis_se.properties.surface as se_surface

    def _boom(*a: Any, **k: Any) -> Any:
        raise Unsupported("no GFN2 backend", next="install tblite")

    monkeypatch.setattr(se_surface, "gfn2_single_point", _boom)
    body = _drc(se, "wall-nobe")
    assert "measure_uncomputable" in body and "GFN2" in body
    assert "measurand_unchecked" not in body


# ── slice 2b: the posed-NO fixture, the op, class hash ───────────────────

_EXAMPLES = Path(__file__).resolve().parents[1] / "hexfold" / "examples"


def _generated_structure(store: Store, slug: str, block: Any) -> None:
    """Persist a ``GeneratedBlock`` as a structure design. The authored
    coords centre near the origin, so they are stored offset to the
    middle of a 100 Å non-periodic box (keeps frac coords positive)."""
    scene = StructScene(cell=Cell.from_lengths_angles(100, 100, 100, pbc=(False,) * 3))
    for el, cart in zip(block.elements, block.coords, strict=True):
        label = scene.next_label(el)
        frac = scene.cell.wrap(
            scene.cell.cart_to_frac(np.asarray(cart, dtype=float) + 50.0)
        )
        scene.atoms[label] = StructAtom(label, el, frac)
    store.structure_save(
        slug=slug, title=slug, scene=scene, version=1, card_text="hexfold wall."
    )


_POSED_OPS: list[dict[str, Any]] = [
    {"op": "add_block", "name": "wall", "envelope": "sphere:r2e-9"},
    {"op": "bind_structure", "block": "wall", "design": "wall-struct"},
    {
        "op": "add_block",
        "name": "mol",
        "envelope": "sphere:r5e-10",
        # NO is stored at +10 Å in a 20 Å box; the wall sits centred at
        # +50 Å in its 100 Å box, so this pose lands NO inside the tube
        # bore at (50, 50, 56) Å
        "pose": "[4 4 4.6] nm",
    },
    {"op": "bind_structure", "block": "mol", "design": "mol-struct"},
]

#: z-window (nm) covering the molecule's stored z (≈ 55–57 Å in the
#: unposed wall frame): [50, 60] Å.
_Z_WINDOW = [5.0, 6.0]


@pytest.fixture
def posed(store: Store, taxonomy: Store, monkeypatch: pytest.MonkeyPatch) -> SeHandler:
    """NO posed inside the capped_tube hexfold wall (ruling 8): blocks
    ``mol`` (bound to an NO structure, posed in the tube bore) and
    ``wall`` (bound to the built capped tube), tblite stubbed."""
    _stub_tblite(monkeypatch)
    from precis_se.atomic.generators.hexfold_spec import build_hexfold

    spec = (_EXAMPLES / "capped_tube.hx").read_text(encoding="utf-8")
    wall_block = build_hexfold({"spec": spec})
    _generated_structure(store, "wall-struct", wall_block)
    _save_structure(store, "mol-struct", ["N", "O"], _NO)
    se = SeHandler(hub=Hub(store=store))
    se.put(id="posed", text=json.dumps({"ops": _POSED_OPS}))
    return se


def _tree(handler: SeHandler, slug: str) -> Any:
    from precis_se import persist

    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


def _tree_blocks_pockets(tree: Any) -> list[Any]:
    return [p for node in tree.blocks.values() for p in node.pockets.values()]


def test_pocket_from_molecule_writes_and_round_trips(
    posed: SeHandler,
) -> None:
    """The op writes one pocket on the wall with mpatch regions and
    mirrored-ESP / donor / hydrophobicity measures; regions round-trip
    through view='pockets' and view='ops'."""
    posed.edit(
        id="posed",
        ops=[
            {
                "op": "pocket_from_molecule",
                "molecule": "mol",
                "wall": "wall",
                "z_window": _Z_WINDOW,
            }
        ],
    )
    tree = _tree(posed, "posed")
    (pocket,) = _tree_blocks_pockets(tree)
    assert pocket.shape == "mol:mol@0.0"
    assert pocket.class_hash and len(pocket.class_hash) == 64
    assert len(pocket.regions) >= 2  # N/O end patches split on ESP sign
    assert all(r.startswith("mpatch:mol:") for r in pocket.regions)

    body = posed.get(id="posed", view="pockets").body
    for r in pocket.regions:
        assert r in body
    ops_body = posed.get(id="posed", view="ops").body
    exported = json.loads(ops_body.split("```json")[1].split("```")[0])["ops"]
    posed2 = SeHandler(hub=Hub(store=posed.store))
    posed2.put(id="posed-copy", text=json.dumps({"ops": exported}))
    copy_tree = _tree(posed2, "posed-copy")
    (copy_pocket,) = _tree_blocks_pockets(copy_tree)
    assert copy_pocket.regions == pocket.regions
    assert copy_pocket.class_hash == pocket.class_hash
    stored = json.dumps({"regions": copy_pocket.regions})
    assert "nonpolar" not in stored


def test_every_acceptor_patch_faces_a_donor_requirement(
    posed: SeHandler,
) -> None:
    """NO reads as acceptor throughout (the surface test pins it), so
    every region carries the 'H-bond donor count ≥ 1' measure."""
    posed.edit(
        id="posed",
        ops=[
            {
                "op": "pocket_from_molecule",
                "molecule": "mol",
                "wall": "wall",
                "z_window": _Z_WINDOW,
            }
        ],
    )
    tree = _tree(posed, "posed")
    (pocket,) = _tree_blocks_pockets(tree)
    from precis_se.pockets import region_measures

    for region in pocket.regions:
        donor = [
            m
            for m in region_measures(tree, "wall", region)
            if m.measurand == "h-bond-donor-count"
        ]
        assert donor, f"region {region} has no donor requirement"
        assert donor[0].min_value == 1
    # and the ESP requirement mirrors the patch band in sign
    esp = [
        m for m in tree.measures if m.measurand == "electrostatic-potential" and m.datum
    ]
    assert len(esp) == len(pocket.regions)


def test_same_pose_rerun_yields_the_same_class_hash(posed: SeHandler) -> None:
    """Patches rank by (z, θ) in the wall frame — same seed, same pose,
    same hash. The rerun is a second design replayed through the same
    ops, so the pockets differ only in which design owns them."""
    op = {
        "op": "pocket_from_molecule",
        "molecule": "mol",
        "wall": "wall",
        "z_window": _Z_WINDOW,
    }
    posed.edit(id="posed", ops=[op])
    first = _tree_blocks_pockets(_tree(posed, "posed"))[0].class_hash
    posed.put(id="posed-2", text=json.dumps({"ops": _POSED_OPS}))
    posed.edit(id="posed-2", ops=[op])
    second = _tree_blocks_pockets(_tree(posed, "posed-2"))[0].class_hash
    assert first and second == first


def test_drc_reports_unmet_requirements_on_the_wall(posed: SeHandler) -> None:
    """The capped-tube wall presents no H-bond donors, so the acceptor
    patches' donor requirement reads measure_unmet — and nothing reads
    measurand_unchecked for the three computed measurands."""
    posed.edit(
        id="posed",
        ops=[
            {
                "op": "pocket_from_molecule",
                "molecule": "mol",
                "wall": "wall",
                "z_window": _Z_WINDOW,
            }
        ],
    )
    body = _drc(posed, "posed")
    assert "measure_unmet" in body
    # the computed measurands never read measurand_unchecked; the only
    # measurand that may is the categorical hydrophobicity index — it has
    # no computer by design
    unchecked = [l for l in body.splitlines() if "measurand_unchecked" in l]
    assert all("hydrophobicity" in l for l in unchecked)


def test_mol_and_mpatch_parse_canonicalise_and_refuse() -> None:
    """Both tokens parse strictly, canonicalise, and refuse malformed
    text and negative clearance by name."""
    from precis_se.datums import _selector_text, parse_selector
    from precis_se.measures import MeasureError

    sel = parse_selector("mpatch:mol:3")
    assert (sel.kind, sel.instance, sel.patch_id) == ("mpatch", "mol", 3)
    assert _selector_text(sel) == "mpatch:mol:3"
    shape = parse_selector("mol:mol@1.5")
    assert (shape.kind, shape.instance) == ("mol", "mol")
    assert shape.clearance == pytest.approx(1.5e-10)
    assert _selector_text(shape) == "mol:mol@1.5"
    with pytest.raises(MeasureError, match="clearance"):
        parse_selector("mol:mol@-0.5")
    with pytest.raises(MeasureError, match="mpatch"):
        parse_selector("mpatch:mol")
    with pytest.raises(MeasureError, match="mpatch"):
        parse_selector("mpatch:mol:x")


def test_pocket_from_molecule_vets_its_args(posed: SeHandler) -> None:
    from precis.errors import BadInput

    base = {
        "op": "pocket_from_molecule",
        "molecule": "mol",
        "wall": "wall",
        "z_window": _Z_WINDOW,
    }
    with pytest.raises(BadInput, match="clearance_A"):
        posed.edit(id="posed", ops=[{**base, "clearance_A": -1}])
    with pytest.raises(BadInput, match="unknown key"):
        posed.edit(id="posed", ops=[{**base, "mystery": 1}])
    with pytest.raises(BadInput, match="z_window"):
        posed.edit(id="posed", ops=[{**base, "z_window": [5, 1]}])
    with pytest.raises(BadInput, match="no block"):
        posed.edit(id="posed", ops=[{**base, "molecule": "nope"}])


def test_fractional_window_name_is_dot_free_integer_angstrom(
    posed: SeHandler,
) -> None:
    posed.edit(
        id="posed",
        ops=[
            {
                "op": "pocket_from_molecule",
                "molecule": "mol",
                "wall": "wall",
                "z_window": [0.45, 0.95],
            }
        ],
    )
    names = [n for b in _tree(posed, "posed").blocks.values() for n in b.pockets]
    assert names == ["wall-mol@z5-10"]  # 4.5 Å rounds half-up; no dot


def test_same_window_second_placement_refuses_with_window_hint(
    posed: SeHandler,
) -> None:
    from precis.errors import BadInput

    op = {
        "op": "pocket_from_molecule",
        "molecule": "mol",
        "wall": "wall",
        "z_window": _Z_WINDOW,
    }
    posed.edit(id="posed", ops=[op])
    with pytest.raises(BadInput) as ei:
        posed.edit(id="posed", ops=[op])
    assert "already exists" in str(ei.value)
    assert "z window" in (ei.value.next or "")


_CO2 = np.array([[-1.16, 0.0, 0.0], [0.0, 0.0, 0.0], [1.16, 0.0, 0.0]])


def _co2_design(posed: SeHandler, slug: str = "posed") -> None:
    """Add ``end``/``side``/``up`` CO2 blocks to design ``slug`` (created
    from the NO ops when it is not ``posed``)."""
    _save_structure(posed.store, "co2-struct", ["O", "C", "O"], _CO2)
    if slug != "posed":
        posed.put(id=slug, text=json.dumps({"ops": _POSED_OPS}))
    ops: list[dict[str, Any]] = []
    for name, rot in [
        ("end", "[0 0 0] deg"),
        ("side", "[0 0 90] deg"),
        ("up", "[0 -90 0] deg"),
    ]:
        ops += [
            {
                "op": "add_block",
                "name": name,
                "envelope": "sphere:r5e-10",
                "pose": "[4 4 4.6] nm",
                "rot": rot,
            },
            {"op": "bind_structure", "block": name, "design": "co2-struct"},
        ]
    posed.edit(id=slug, ops=ops)


def _pfm(molecule: str) -> dict[str, Any]:
    return {
        "op": "pocket_from_molecule",
        "molecule": molecule,
        "wall": "wall",
        "z_window": _Z_WINDOW,
    }


def test_pose_changes_the_name_and_names_are_dot_free(posed: SeHandler) -> None:
    _co2_design(posed)
    for mol in ("end", "side", "up"):
        posed.edit(id="posed", ops=[_pfm(mol)])
    tree = _tree(posed, "posed")
    names = sorted(n for b in tree.blocks.values() for n in b.pockets)
    # measure names are scoped by pocket: none repeats on the one wall
    mnames = [m.name for m in tree.measures if m.name.startswith("wall-")]
    assert mnames and len(mnames) == len(set(mnames))
    assert all(m.split("/")[0] in names for m in mnames)
    assert _drc(posed, "posed")  # DRC runs over all three pockets
    assert names == [
        "wall-end@z50-60",  # unrotated canonical pose: no r suffix
        "wall-side@z50-60r0-0-90",
        "wall-up@z50-60r0-m90-0",
    ]
    assert all("." not in n for n in names)


def test_translation_only_move_collides_and_names_the_window(
    posed: SeHandler,
) -> None:
    from precis.errors import BadInput

    _co2_design(posed)
    posed.edit(id="posed", ops=[_pfm("end")])
    posed.edit(
        id="posed", ops=[{"op": "set_pose", "block": "end", "pose": "[4.1 4 4.6] nm"}]
    )
    with pytest.raises(BadInput) as ei:
        posed.edit(id="posed", ops=[_pfm("end")])
    assert "z window" in (ei.value.next or "")


#: class_hash of the NO pose in ``posed`` over ``_Z_WINDOW``; pinned from the
#: code before measure names were scoped by pocket (the hash digests
#: selectors and measure tuples, never names).
_NO_CLASS_HASH = "3ab9f58d963fffa615b98efac3a4e275a312c69b082c9682da5e79d7fb7a5d23"


def test_class_hash_of_a_pose_is_pinned(posed: SeHandler) -> None:
    posed.edit(id="posed", ops=[_pfm("mol")])
    (pocket,) = _tree_blocks_pockets(_tree(posed, "posed"))
    assert pocket.class_hash == _NO_CLASS_HASH
