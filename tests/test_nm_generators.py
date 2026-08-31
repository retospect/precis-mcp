"""precis_nm generators — slice 4a round 1 (docs/backlog/nm-kind.md
"Generators — parametric block factories"): the pure ``cnt``/``fullerene``
builders (:mod:`precis_nm.generators.sp2`) and the handler-intercepted
``generate`` op (:meth:`precis_nm.handler.NmHandler._generate`).

Geometry assertions are independent of the generators' own internal
bookkeeping wherever practical — bond lengths and the fullerene ring
census are recomputed straight from ``coords``/``bonds`` here, not read
back off ``topology``, so a bug that only shows up in the *realized atoms*
(not the declared topology dict) still fails a test.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

import precis_nm
from precis.cad import dsl as cad_dsl
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis_nm.generators import GENERATORS, GeneratedBlock, GeneratorError
from precis_nm.generators.sp2 import build_cnt, build_fullerene
from precis_nm.handler import NmHandler

_MIGRATIONS_DIR = Path(precis_nm.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> NmHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return NmHandler(hub=hub)


@pytest.fixture
def structure(store: Store) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


def _assert_no_error_findings(validate_body: str) -> None:
    """``view='validate'`` renders "✓ no validator findings" when the
    finding list is empty outright, or a "# N error(s), M warning(s)"
    header otherwise (:meth:`precis_nm.handler.NmHandler._render_validate`)
    — both are "clean" as long as N is 0; a generated+bound block is
    expected to carry warn-tier findings (e.g. ``unconnected_port`` on an
    as-yet-unwired rim port), never error-tier ones."""
    assert "✓ no validator findings" in validate_body or "# 0 error(s)" in validate_body


# ── shared geometry helpers (recompute from coords/bonds, no trust in the
#    generator's own declared topology) ────────────────────────────────


def _degrees(n_atoms: int, bonds: list[tuple[int, int, float]]) -> list[int]:
    deg = [0] * n_atoms
    for i, j, _order in bonds:
        deg[i] += 1
        deg[j] += 1
    return deg


def _bond_lengths(
    coords: np.ndarray, bonds: list[tuple[int, int, float]]
) -> list[float]:
    return [float(np.linalg.norm(coords[i] - coords[j])) for i, j, _order in bonds]


def _ring_census(
    coords: np.ndarray, bonds: list[tuple[int, int, float]]
) -> Counter[int]:
    """Face sizes of the bond graph, via rotation-system face tracing.

    Every atom's neighbors are cyclically ordered by angle in the local
    tangent plane (using the atom's own position as the "up" direction —
    valid because a fullerene's atoms sit on a near-perfect shell), then
    each face is traced by always turning to the *next* neighbor in that
    order at each step (the standard combinatorial-map face-tracing
    algorithm) — a purely graph+geometry check, independent of the
    generator's own pentagon/hexagon bookkeeping.
    """
    n = len(coords)
    adj: dict[int, list[int]] = {i: [] for i in range(n)}
    for i, j, _order in bonds:
        adj[i].append(j)
        adj[j].append(i)

    def cyclic_order(v: int) -> list[int]:
        center = coords[v]
        normal = center / np.linalg.norm(center)
        arbitrary = np.array([1.0, 0.0, 0.0])
        if abs(np.dot(arbitrary, normal)) > 0.9:
            arbitrary = np.array([0.0, 1.0, 0.0])
        e1 = np.cross(normal, arbitrary)
        e1 = e1 / np.linalg.norm(e1)
        e2 = np.cross(normal, e1)
        angles = []
        for nb in adj[v]:
            rel = coords[nb] - center
            rel = rel - np.dot(rel, normal) * normal
            angles.append(np.arctan2(np.dot(rel, e2), np.dot(rel, e1)))
        return [nb for _a, nb in sorted(zip(angles, adj[v], strict=True))]

    rot = {v: cyclic_order(v) for v in range(n)}
    visited: set[tuple[int, int]] = set()
    faces: list[list[int]] = []
    for u in range(n):
        for v in adj[u]:
            if (u, v) in visited:
                continue
            face = []
            cu, cv = u, v
            while (cu, cv) not in visited:
                visited.add((cu, cv))
                face.append(cu)
                order = rot[cv]
                nxt = order[(order.index(cu) - 1) % len(order)]
                cu, cv = cv, nxt
            faces.append(face)
    return Counter(len(f) for f in faces)


# ── cnt geometry ─────────────────────────────────────────────────────────


def test_cnt_10_10_radius_matches_closed_form() -> None:
    block = build_cnt({"n": 10, "m": 10, "length_A": 20.0})
    assert abs(block.topology["radius_A"] - 6.78) < 0.01
    assert block.topology["chiral_index"] == [10, 10]
    assert block.topology["pentagons"] == 0


def test_cnt_10_10_bond_lengths_in_range() -> None:
    block = build_cnt({"n": 10, "m": 10, "length_A": 20.0})
    lengths = _bond_lengths(block.coords, block.bonds)
    assert lengths  # sanity: some bonds exist
    assert all(1.38 <= x <= 1.46 for x in lengths)


def test_cnt_10_10_non_rim_atoms_are_3_coordinate() -> None:
    block = build_cnt({"n": 10, "m": 10, "length_A": 20.0})
    deg = _degrees(len(block.elements), block.bonds)
    rim_indices = {i for i, d in enumerate(deg) if d < 3}
    for i, d in enumerate(deg):
        if i not in rim_indices:
            assert d == 3, f"atom {i} has degree {d}, expected 3 (non-rim)"
    # every rim (open-valence) atom got a port, one-to-one
    assert {p.atom_index for p in block.ports} == rim_indices
    assert len(block.ports) == len(rim_indices)
    for p in block.ports:
        assert p.roles == ["covalent", "sp2-rim"]
        assert p.expected_element == "C"


def test_cnt_10_10_atoms_inside_envelope() -> None:
    block = build_cnt({"n": 10, "m": 10, "length_A": 20.0})
    spec = cad_dsl.parse(block.envelope)
    assert spec.alias == "cyl"
    r, h = spec.params["r"], spec.params["h"]
    radial = np.linalg.norm(block.coords[:, :2], axis=1)
    assert np.all(radial <= r + 1e-6)
    assert np.all(block.coords[:, 2] >= -1e-6)
    assert np.all(block.coords[:, 2] <= h + 1e-6)


# ── cnt param rejection ────────────────────────────────────────────────


def test_cnt_m_greater_than_n_rejected() -> None:
    with pytest.raises(GeneratorError, match="0 <= m <= n"):
        build_cnt({"n": 5, "m": 8, "length_A": 10.0})


def test_cnt_n_zero_rejected() -> None:
    with pytest.raises(GeneratorError, match="positive integer"):
        build_cnt({"n": 0, "m": 0, "length_A": 10.0})


def test_cnt_absurd_length_rejected() -> None:
    with pytest.raises(GeneratorError, match="length_A"):
        build_cnt({"n": 10, "m": 10, "length_A": 100000.0})


# ── fullerene geometry ───────────────────────────────────────────────────


def test_fullerene_60_atom_and_bond_counts() -> None:
    block = build_fullerene({"atoms": 60})
    assert len(block.elements) == 60
    assert len(block.coords) == 60
    assert len(block.bonds) == 90
    assert block.ports == []
    assert block.topology == {"pentagons": 12, "hexagons": 20}


def test_fullerene_60_every_atom_3_coordinate() -> None:
    block = build_fullerene({"atoms": 60})
    deg = _degrees(60, block.bonds)
    assert deg == [3] * 60


def test_fullerene_60_ring_census_12_pentagons_20_hexagons() -> None:
    block = build_fullerene({"atoms": 60})
    sizes = _ring_census(block.coords, block.bonds)
    assert sizes[5] == 12
    assert sizes[6] == 20
    assert set(sizes) == {5, 6}


def test_fullerene_60_bond_lengths_cluster_at_experimental_values() -> None:
    block = build_fullerene({"atoms": 60})
    lengths = _bond_lengths(block.coords, block.bonds)
    short = [x for x in lengths if x < 1.43]
    long_ = [x for x in lengths if x >= 1.43]
    assert len(short) == 30  # 6:6 bonds
    assert len(long_) == 60  # 5:6 bonds
    assert all(abs(x - 1.401) < 0.01 for x in short)
    assert all(abs(x - 1.458) < 0.01 for x in long_)


def test_fullerene_70_unsupported() -> None:
    with pytest.raises(GeneratorError, match="60"):
        build_fullerene({"atoms": 70})


# ── registry ─────────────────────────────────────────────────────────────


def test_registry_has_round_1_generators() -> None:
    assert set(GENERATORS) == {"cnt", "fullerene"}
    for builder in GENERATORS.values():
        assert callable(builder)


def test_unknown_generator_op_raises(handler: NmHandler) -> None:
    ops = json.dumps(
        {
            "ops": [
                {
                    "op": "generate",
                    "generator": "nanohorn",
                    "params": {},
                    "name": "axle",
                }
            ]
        }
    )
    with pytest.raises(BadInput, match="unknown generator") as exc_info:
        handler.put(id="gen-unknown", text=ops)
    assert "cnt" in str(exc_info.value)
    assert "fullerene" in str(exc_info.value)


# ── generate op end-to-end ────────────────────────────────────────────


def test_generate_cnt_end_to_end(handler: NmHandler, store: Store) -> None:
    ops = [
        {
            "op": "generate",
            "generator": "cnt",
            "params": {"n": 6, "m": 6, "length_A": 10.0},
            "name": "axle",
        }
    ]
    resp = handler.put(id="gentube", text=json.dumps({"ops": ops}))
    assert "gentube-axle" in resp.body
    assert "chiral_index" in resp.body

    block = handler.get(id="gentube", view="block", args={"name": "axle"})
    assert "bound_design: gentube-axle" in block.body
    assert "sp2-rim" in block.body

    struct_ref = store.get_ref(kind="structure", id="gentube-axle")
    assert struct_ref is not None

    validate = handler.get(id="gentube", view="validate")
    _assert_no_error_findings(validate.body)


def test_generate_fullerene_end_to_end(handler: NmHandler, store: Store) -> None:
    ops = [
        {
            "op": "generate",
            "generator": "fullerene",
            "params": {"atoms": 60},
            "name": "cage",
        }
    ]
    resp = handler.put(id="genball", text=json.dumps({"ops": ops}))
    assert "genball-cage" in resp.body
    assert "pentagons=12" in resp.body
    assert "hexagons=20" in resp.body

    struct_ref = store.get_ref(kind="structure", id="genball-cage")
    assert struct_ref is not None
    scene, _handles = store.structure_load(struct_ref.id)
    assert len(scene.atoms) == 60
    assert len(scene.bonds) == 90

    block = handler.get(id="genball", view="block", args={"name": "cage"})
    assert "bound_design: genball-cage" in block.body

    validate = handler.get(id="genball", view="validate")
    _assert_no_error_findings(validate.body)


def test_generate_duplicate_block_name_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle", "envelope": "sphere:r2"},
        {
            "op": "generate",
            "generator": "fullerene",
            "params": {"atoms": 60},
            "name": "axle",
        },
    ]
    with pytest.raises(BadInput, match="duplicate"):
        handler.put(id="gendup", text=json.dumps({"ops": ops}))


# ── generate defers its store write past the whole ops list (no orphan on
#    a later op's failure — reviewer round-1 finding) ───────────────────


def test_generate_followed_by_failing_op_creates_no_orphan(
    handler: NmHandler, store: Store
) -> None:
    ops = [
        {
            "op": "generate",
            "generator": "fullerene",
            "params": {"atoms": 60},
            "name": "cage",
        },
        # deliberately invalid — 'ghost'/'ghost2' were never added, so this
        # op fails validation AFTER the generate op already ran its
        # (pure, in-memory) half; the mint must never have happened.
        {"op": "connect", "a": "ghost.p1", "b": "ghost2.p2"},
    ]
    with pytest.raises(BadInput):
        handler.put(id="genfail", text=json.dumps({"ops": ops}))
    assert store.get_ref(kind="structure", id="genfail-cage") is None
    # the whole put failed before the ref-insert transaction ever opened —
    # the nm design itself was never persisted either.
    assert store.get_ref(kind="nm", id="genfail") is None


def test_generate_collides_with_existing_structure_design_rejected(
    handler: NmHandler, structure: StructureHandler, store: Store
) -> None:
    # A hand-authored structure design already lives at the exact slug
    # generate would compute for block 'axle' under nm design 'gencollide'
    # ('{design}-{block name}') — generate must reject loudly rather than
    # silently retiring this design's atoms via structure_save's
    # create-or-replace semantics.
    structure.put(
        id="gencollide-axle",
        text=json.dumps(
            {
                "cell": {
                    "a": 20.0,
                    "b": 20.0,
                    "c": 20.0,
                    "pbc": [False, False, False],
                },
                "ops": [{"op": "add_atom", "element": "N", "cart": [0.0, 0.0, 0.0]}],
            }
        ),
    )
    ops = [
        {
            "op": "generate",
            "generator": "cnt",
            "params": {"n": 4, "m": 4, "length_A": 8.0},
            "name": "axle",
        }
    ]
    with pytest.raises(BadInput, match="gencollide-axle"):
        handler.put(id="gencollide", text=json.dumps({"ops": ops}))

    struct_ref = store.get_ref(kind="structure", id="gencollide-axle")
    assert struct_ref is not None
    scene, _handles = store.structure_load(struct_ref.id)
    assert len(scene.atoms) == 1
    (only_atom,) = scene.atoms.values()
    assert only_atom.element == "N"


def test_generate_block_is_a_generated_block_type() -> None:
    # sanity: the dataclass shape the handler relies on stays as documented
    block = build_fullerene({"atoms": 60})
    assert isinstance(block, GeneratedBlock)
    assert block.provenance
