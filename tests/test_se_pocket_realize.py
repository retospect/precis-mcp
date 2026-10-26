"""pocket-from-molecule slice 4 (docs/backlog/pocket-from-molecule.md):
the realize loop — cluster cut + H-capping helpers, the pick → join →
check scoring over three hand-built wall patches, ranking, cap,
provenance, and the ``pocket_realize`` op's vetting.

tblite is stubbed (``_FakeTBLite`` — deterministic ±0.06 e charges and
zero forces, so every relax "converges" in place), matching
``tests/test_se_pocket_from_molecule.py``.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import numpy as np
import pytest
from ase.calculators.calculator import Calculator, all_changes

from precis.dispatch import Hub
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
    _run_migration("0174_taxon_seed.sql")
    _run_migration("0182_se_measurand_seed.sql")
    _run_migration("0228_se_measurands_esp_binding.sql")
    return store


class _FakeTBLite(Calculator):
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
    setattr(fake, "TBLite", _FakeTBLite)  # noqa: B010
    monkeypatch.setitem(sys.modules, "tblite.ase", fake)
    monkeypatch.setattr(_FakeTBLite, "seen", [])


def _save_structure(
    store: Store, slug: str, elements: list[str], xyz: np.ndarray
) -> None:
    cell = Cell.from_lengths_angles(120, 120, 120, pbc=(False, False, False))
    scene = StructScene(cell=cell)
    for el, p in zip(elements, xyz, strict=True):
        label = scene.next_label(el)
        scene.atoms[label] = StructAtom(label, el, cell.cart_to_frac(np.asarray(p)))
    store.structure_save(
        slug=slug, title=slug, scene=scene, version=1, card_text="test."
    )


#: The wall block's pose (Å): candidate structures are stored this far
#: under their world position, so the realize loop's resolver scene
#: only round-trips if it holds the candidate's own frame coords and
#: the wall pose is applied on read — not a cell-centred copy.
_WALL_POSE_A = 10.0

#: The posed NO sits at world (50, 50, 56)–(51.155, 50, 56) Å (stored
#: at the origin, posed [5 5 5.6] nm): the candidates below are carbon
#: rings centred under it at world z≈52.
_CENTRE = np.array([50.0, 50.0, 52.0])
_RING_R = 1.42  # Å — graphene's C–C
_RING = np.array(
    [
        _CENTRE + _RING_R * np.array([np.cos(t), np.sin(t), 0.0])
        for t in np.linspace(0, 2 * np.pi, 6, endpoint=False)
    ]
    + [_CENTRE]
)


def _wall(kind: str) -> tuple[list[str], np.ndarray]:
    """One hand-built wall patch: bare graphene ring, the same with the
    centre atom N-doped, or the ring plus an OH pendant at its rim."""
    if kind == "graphene":
        return ["C"] * 7, _RING
    if kind == "n-doped":
        return ["C"] * 6 + ["N"], _RING
    # OH at the rim: O on ring atom 0's outward bond direction, its H
    # pointing up into the pocket.
    o = _RING[0] + np.array([_RING_R * 0.85, 0.0, 0.6])
    h = o + np.array([0.0, 0.0, 0.97])
    return ["C"] * 7 + ["O", "H"], np.concatenate([_RING, [o, h]])
    raise AssertionError(kind)


_OPS: list[dict[str, Any]] = [
    {
        "op": "add_block",
        "name": "wall",
        "envelope": "sphere:r2e-9",
        "pose": "[1 1 1] nm",
    },
    {"op": "bind_structure", "block": "wall", "design": "wall-struct"},
    {
        "op": "add_block",
        "name": "mol",
        "envelope": "sphere:r5e-10",
        "pose": "[5 5 5.6] nm",
    },
    {"op": "bind_structure", "block": "mol", "design": "mol-struct"},
]


@pytest.fixture
def posed(store: Store, taxonomy: Store, monkeypatch: pytest.MonkeyPatch) -> SeHandler:
    """NO posed over the graphene wall's z window; tblite stubbed. The
    pocket is written by ``pocket_from_molecule`` (slice 2b) so the
    regions and measures are the real ones."""
    _stub_tblite(monkeypatch)
    _save_structure(store, "wall-struct", ["C"] * 7, _RING - _WALL_POSE_A)
    _save_structure(store, "mol-struct", ["N", "O"], _NO)
    for kind in ("graphene", "n-doped", "oh-rim"):
        elements, xyz = _wall(kind)
        _save_structure(store, f"cand-{kind}", elements, xyz - _WALL_POSE_A)
    se = SeHandler(hub=Hub(store=store))
    se.put(id="realize", text=json.dumps({"ops": _OPS}))
    se.edit(
        id="realize",
        ops=[
            {
                "op": "pocket_from_molecule",
                "molecule": "mol",
                "wall": "wall",
                # wall frame: world z minus the 10 Å pose → the patch
                # centroids sit at ~46 Å
                "z_window": [4.0, 5.0],
            }
        ],
    )
    return se


def _tree(handler: SeHandler) -> Any:
    from precis_se import persist

    ref = handler.store.get_ref(kind="se", id="realize")
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


def _pocket(tree: Any) -> Any:
    (pocket,) = tree.blocks["wall"].pockets.values()
    return pocket


def test_cluster_cut_and_cap_dangling() -> None:
    """The helper takes atoms inside the radius only, and one H lands
    per severed bond at the X–H covalent distance."""
    from precis_se.atomic.clearance import cap_dangling, cluster_cut

    chain = np.array([[i * 1.4, 0.0, 0.0] for i in range(6)])
    cut = cluster_cut(chain, np.array([[4.2, 0.0, 0.0]]), 1.6)
    assert list(cut) == [2, 3, 4]
    elements, coords, anchors = cap_dangling(["C"] * 6, chain, cut)
    # atoms 2 and 4 each lost one neighbour across the cut
    assert elements == ["C", "C", "C", "H", "H"]
    assert sorted(anchors) == [0, 2]
    dist = np.linalg.norm(coords[3:] - coords[[0, 2]], axis=1)
    assert np.allclose(dist, 1.07, atol=0.02)


def test_loop_ranks_all_three_with_provenance(posed: SeHandler) -> None:
    """The spec's acceptance fixture: three hand-built walls against
    the NO pocket — all three ranked inside the cap, the OH-rim wall
    (the only donor carrier) first, every result carrying its
    provenance."""
    from precis_se.pocket_realize import WallCandidate, realize_pocket

    tree = _tree(posed)
    pocket = _pocket(tree)
    candidates = []
    for kind in ("graphene", "n-doped", "oh-rim"):
        elements, xyz = _wall(kind)
        candidates.append(
            WallCandidate(
                name=f"cand-{kind}",
                source=f"test:{kind}",
                labels=elements,
                elements=elements,
                coords_A=xyz,
                formal=[0] * len(elements),
            )
        )
    result = realize_pocket(tree, "wall", pocket, candidates, cap=3)
    assert result.evaluated == 3 and result.unevaluated == 0
    assert len(result.results) == 3
    by_name = {r.name: r for r in result.results}
    assert result.results[0].name == "cand-oh-rim"
    # the OH-rim wall is the only donor carrier: it meets the donor
    # band of the patch its OH faces, and ranks strictly ahead of the
    # C/N walls on unmet hard measures (the stubbed charges leave the
    # mirrored ESP bands unmet everywhere, so nobody is solved)
    best = result.results[0]
    assert any(
        s.measurand == "h-bond-donor-count" and s.met is True for s in best.scores
    )
    assert best.unmet_hard < by_name["cand-graphene"].unmet_hard
    assert best.unmet_hard < by_name["cand-n-doped"].unmet_hard
    assert by_name["cand-graphene"].unmet_hard > 0
    assert by_name["cand-n-doped"].unmet_hard > 0
    for loser in (by_name["cand-graphene"], by_name["cand-n-doped"]):
        assert loser.failing, "a loser names its failing regions"
        assert any("don" in f for f in loser.failing)
    for r in result.results:
        assert r.error is None
        assert r.provenance["class_hash"] == pocket.class_hash
        assert r.provenance["source"].startswith("test:")
        assert r.provenance["iteration"] in (0, 1, 2)
        assert r.binding_eV is not None and r.e_complex_eV is not None
        assert r.cluster_atoms > 0 and r.relax_converged is not None


def test_cap_bounds_the_evaluations(posed: SeHandler) -> None:
    """cap=2 evaluates the first two candidates only; the third is
    reported left over the cap."""
    from precis_se.pocket_realize import WallCandidate, realize_pocket

    tree = _tree(posed)
    pocket = _pocket(tree)
    candidates = [
        WallCandidate(
            name=k,
            source="test",
            labels=["C"],
            elements=["C"],
            coords_A=_RING[:1],
            formal=[0],
        )
        for k in ("a", "b", "c")
    ]
    result = realize_pocket(tree, "wall", pocket, candidates, cap=2)
    assert result.evaluated == 2 and result.unevaluated == 1
    assert [r.name for r in result.results] == ["a", "b"] or all(
        r.name in ("a", "b") for r in result.results
    )


def test_op_runs_end_to_end_and_vets_args(posed: SeHandler) -> None:
    """The handler-level op ranks the three stored structures in its
    echo and refuses bad args by name."""
    from precis.errors import BadInput

    out = posed.edit(
        id="realize",
        ops=[
            {
                "op": "pocket_realize",
                "wall": "wall",
                "candidates": ["cand-graphene", "cand-n-doped", "cand-oh-rim"],
            }
        ],
    )
    echo = out.body if hasattr(out, "body") else str(out)
    assert "pocket_realize" in echo
    assert "cand-oh-rim" in echo
    first_oh = echo.index("cand-oh-rim")
    first_bad = min(
        (echo.index(n) for n in ("cand-graphene", "cand-n-doped") if n in echo),
        default=10**9,
    )
    assert first_oh < first_bad or "1. cand-oh-rim" in echo
    with pytest.raises(BadInput, match="unknown key"):
        posed.edit(
            id="realize",
            ops=[{"op": "pocket_realize", "wall": "wall", "nope": 1}],
        )
    with pytest.raises(BadInput, match="candidates"):
        posed.edit(
            id="realize",
            ops=[{"op": "pocket_realize", "wall": "wall", "candidates": []}],
        )
    with pytest.raises(BadInput, match="no structure"):
        posed.edit(
            id="realize",
            ops=[
                {
                    "op": "pocket_realize",
                    "wall": "wall",
                    "candidates": ["gone"],
                }
            ],
        )
