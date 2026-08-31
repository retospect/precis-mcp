"""precis_nm L4 mechanics ceilings — slice 4a build order (iii)
(docs/backlog/nm-kind.md "Generators — parametric block factories",
"Mechanics ceilings" section): :mod:`precis_nm.mechanics`'s closed-form
min-cut tensile / Euler buckling / harmonic strain energy, and the
handler's ``view='mechanics'``. Every figure here is advisory (never
gates) — these tests check the NUMBERS, never that anything raises or
blocks a write.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import precis_nm
from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Bond, Scene
from precis_nm import mechanics
from precis_nm.generators.sp2 import build_cnt
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


def _cell() -> Cell:
    return Cell.from_lengths_angles(30.0, 30.0, 30.0, pbc=(False, False, False))


# ── min-cut ──────────────────────────────────────────────────────────────


def test_min_cut_linear_chain_is_one() -> None:
    scene = Scene(cell=_cell())
    for lbl in "abcd":
        scene.atoms[lbl] = Atom(label=lbl, element="C", frac=np.zeros(3))
    scene.bonds = [Bond(i="a", j="b"), Bond(i="b", j="c"), Bond(i="c", j="d")]
    cut, ceiling = mechanics.min_cut(scene, "a", "d")
    assert cut == 1
    assert ceiling == pytest.approx(mechanics.RUPTURE_FORCE_NN)


def test_min_cut_two_parallel_paths_is_two() -> None:
    scene = Scene(cell=_cell())
    for lbl in "abcd":
        scene.atoms[lbl] = Atom(label=lbl, element="C", frac=np.zeros(3))
    scene.bonds = [
        Bond(i="a", j="b"),
        Bond(i="b", j="d"),
        Bond(i="a", j="c"),
        Bond(i="c", j="d"),
    ]
    cut, ceiling = mechanics.min_cut(scene, "a", "d")
    assert cut == 2
    assert ceiling == pytest.approx(2 * mechanics.RUPTURE_FORCE_NN)


def test_min_cut_disconnected_components_is_zero() -> None:
    """The demo-style "dumbbell" case (nm-kind.md's task spec): two atoms
    with no bond path between them — zero tensile ceiling, the honest
    answer for a genuinely unconnected structure, not an error."""
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    scene.atoms["b"] = Atom(label="b", element="C", frac=np.zeros(3))
    cut, ceiling = mechanics.min_cut(scene, "a", "b")
    assert cut == 0
    assert ceiling == 0.0


def test_min_cut_missing_atom_is_zero_not_an_error() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    assert mechanics.min_cut(scene, "a", "ghost") == (0, 0.0)


# ── Euler buckling ───────────────────────────────────────────────────────


def test_euler_buckling_matches_hand_computed_closed_form() -> None:
    r_A, length_A = 6.0, 40.0
    expected_nN = (
        (np.pi**2)
        * mechanics.E_MODULUS_PA
        * (np.pi * (r_A * 1e-10) ** 3 * (mechanics.TUBE_WALL_THICKNESS_A * 1e-10))
        / (length_A * 1e-10) ** 2
        * 1e9
    )
    assert mechanics.euler_buckling_ceiling_nN(r_A, length_A) == pytest.approx(
        expected_nN, rel=1e-9
    )


def test_euler_buckling_of_a_generated_cnt_matches_hand_calc_via_envelope() -> None:
    """A real generated CNT's own envelope round-trips to (radius, length)
    (module docstring's "schema-limitation workaround") closely enough
    that the buckling ceiling computed from it matches a hand calculation
    using the generator's own (pre-storage) ``topology['radius_A']`` and
    the requested length — envelope round-tripping through the cad-DSL's
    4-decimal formatting is the only source of drift, so a loose
    (0.1%) relative tolerance is still a real "matches" check."""
    block = build_cnt({"n": 8, "m": 8, "length_A": 30.0})
    geom = mechanics.tube_geometry_from_envelope(block.envelope)
    assert geom is not None
    radius_A, length_A = geom
    hand_expected = (
        (np.pi**2)
        * mechanics.E_MODULUS_PA
        * (
            np.pi
            * (block.topology["radius_A"] * 1e-10) ** 3
            * (mechanics.TUBE_WALL_THICKNESS_A * 1e-10)
        )
        / (length_A * 1e-10) ** 2
        * 1e9
    )
    assert mechanics.euler_buckling_ceiling_nN(radius_A, length_A) == pytest.approx(
        hand_expected, rel=1e-3
    )


def test_tube_geometry_from_envelope_rejects_cone() -> None:
    """A cone's tapered wall isn't a constant-radius buckling candidate
    (module docstring, point 2) — never treated as a tube."""
    assert mechanics.tube_geometry_from_envelope("cone:r5h10") is None


def test_tube_geometry_from_envelope_rejects_non_cyl_and_none() -> None:
    assert mechanics.tube_geometry_from_envelope(None) is None
    assert mechanics.tube_geometry_from_envelope("sphere:r5") is None


# ── harmonic strain energy ───────────────────────────────────────────────


def _cnt_scene(block: object, coords: np.ndarray) -> Scene:
    scene = Scene(
        cell=Cell.from_lengths_angles(200, 200, 200, pbc=(False, False, False))
    )
    labels = []
    for elt, cart in zip(block.elements, coords, strict=True):  # type: ignore[attr-defined]
        lbl = scene.next_label(elt)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(np.asarray(cart)))
        scene.atoms[lbl] = Atom(label=lbl, element=elt, frac=frac, hybridization="sp2")
        labels.append(lbl)
    for i, j, order, kind in block.bonds:  # type: ignore[attr-defined]
        scene.bonds.append(Bond(i=labels[i], j=labels[j], order=order, kind=kind))
    return scene


def test_strain_energy_of_pristine_generated_cnt_is_near_zero() -> None:
    block = build_cnt({"n": 8, "m": 8, "length_A": 25.0})
    scene = _cnt_scene(block, block.coords)
    energy_eV, n_triples = mechanics.harmonic_strain_energy_eV(scene)
    assert n_triples > 0
    # near zero relative to the deliberately-bent case below, not exactly
    # zero -- a rolled sheet has a little genuine curvature-induced angle
    # deviation from the flat-sheet 120 deg ideal.
    assert energy_eV < 1.0


def test_strain_energy_of_a_deliberately_bent_cnt_is_positive_and_larger() -> None:
    block = build_cnt({"n": 8, "m": 8, "length_A": 25.0})
    pristine_e, _n = mechanics.harmonic_strain_energy_eV(
        _cnt_scene(block, block.coords)
    )
    bent_coords = block.coords.copy()
    bent_coords[0] += np.array([2.5, 2.5, 2.5])  # a genuinely large local kink
    bent_e, _n2 = mechanics.harmonic_strain_energy_eV(_cnt_scene(block, bent_coords))
    assert bent_e > 0.0
    assert bent_e > pristine_e


def test_strain_energy_no_covalent_neighbors_contributes_nothing() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    energy_eV, n_triples = mechanics.harmonic_strain_energy_eV(scene)
    assert energy_eV == 0.0
    assert n_triples == 0


# ── view='mechanics' rendering ───────────────────────────────────────────


def test_mechanics_view_renders_unfilled_for_unbound_design(handler: NmHandler) -> None:
    handler.put(
        id="mech-empty",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "scaffold", "envelope": "sphere:r3"}]}
        ),
    )
    body = handler.get(id="mech-empty", view="mechanics").body
    assert "nm mechanics" in body
    assert mechanics.HONESTY_NOTE in body
    assert "unfilled" in body
    assert "no bond connects declared" in body


def test_mechanics_view_renders_buckling_for_a_bound_cnt_block(
    handler: NmHandler, store: Store
) -> None:
    ops = [
        {
            "op": "generate",
            "generator": "cnt",
            "params": {"n": 6, "m": 6, "length_A": 15.0},
            "name": "axle",
        }
    ]
    handler.put(id="mech-cnt", text=json.dumps({"ops": ops}))
    body = handler.get(id="mech-cnt", view="mechanics").body
    assert "axle" in body
    assert "not a tube envelope" not in body
    # buckling + strain columns are populated (not "unfilled") for the
    # generated, bound block.
    lines = [line for line in body.splitlines() if line.strip().startswith("axle")]
    assert lines, body
    assert "unfilled" not in lines[0]


def test_mechanics_view_min_cut_zero_for_dumbbell_with_no_shaft(
    handler: NmHandler, structure: StructureHandler
) -> None:
    """The demo-style dumbbell topology (nm-kind.md's task spec test list):
    two heads bound to the SAME structure design, but with no bond between
    the two fragments — min-cut ceiling renders 0 and says so."""
    structure.put(
        id="mech-dumbbell-scene",
        text=json.dumps(
            {
                "cell": {"a": 30.0, "b": 30.0, "c": 30.0, "pbc": [False, False, False]},
                "ops": [
                    {
                        "op": "add_atom",
                        "element": "C",
                        "label": "a1",
                        "cart": [0, 0, 0],
                    },
                    {
                        "op": "add_atom",
                        "element": "C",
                        "label": "a2",
                        "cart": [1.5, 0, 0],
                    },
                    {"op": "add_bond", "i": "a1", "j": "a2"},
                    {
                        "op": "add_atom",
                        "element": "C",
                        "label": "b1",
                        "cart": [20, 0, 0],
                    },
                    {
                        "op": "add_atom",
                        "element": "C",
                        "label": "b2",
                        "cart": [21.5, 0, 0],
                    },
                    {"op": "add_bond", "i": "b1", "j": "b2"},
                ],
            }
        ),
    )
    ops = [
        {"op": "add_block", "name": "headA", "envelope": "sphere:r2"},
        {
            "op": "add_port",
            "block": "headA",
            "name": "p",
            "roles": ["covalent"],
            "expected_element": "C",
        },
        {"op": "add_block", "name": "headB", "envelope": "sphere:r2"},
        {
            "op": "add_port",
            "block": "headB",
            "name": "p",
            "roles": ["covalent"],
            "expected_element": "C",
        },
        {
            "op": "bind_structure",
            "block": "headA",
            "design": "mech-dumbbell-scene",
            "ports": {"p": "a1"},
        },
        {
            "op": "bind_structure",
            "block": "headB",
            "design": "mech-dumbbell-scene",
            "ports": {"p": "b1"},
        },
        {"op": "connect", "a": "headA.p", "b": "headB.p"},
    ]
    handler.put(id="mech-dumbbell", text=json.dumps({"ops": ops}))
    body = handler.get(id="mech-dumbbell", view="mechanics").body
    assert "headA.p" in body and "headB.p" in body
    tensile_lines = [line for line in body.splitlines() if "headA.p" in line]
    assert tensile_lines, body
    assert any("disconnected" in line for line in tensile_lines)


def test_mechanics_view_min_cut_positive_for_dumbbell_with_shaft(
    handler: NmHandler, structure: StructureHandler
) -> None:
    structure.put(
        id="mech-dumbbell2-scene",
        text=json.dumps(
            {
                "cell": {"a": 30.0, "b": 30.0, "c": 30.0, "pbc": [False, False, False]},
                "ops": [
                    {
                        "op": "add_atom",
                        "element": "C",
                        "label": "a1",
                        "cart": [0, 0, 0],
                    },
                    {
                        "op": "add_atom",
                        "element": "C",
                        "label": "a2",
                        "cart": [1.5, 0, 0],
                    },
                    {"op": "add_bond", "i": "a1", "j": "a2"},
                    {
                        "op": "add_atom",
                        "element": "C",
                        "label": "b1",
                        "cart": [3.0, 0, 0],
                    },
                    {
                        "op": "add_atom",
                        "element": "C",
                        "label": "b2",
                        "cart": [4.5, 0, 0],
                    },
                    {"op": "add_bond", "i": "b1", "j": "b2"},
                    {"op": "add_bond", "i": "a2", "j": "b1"},
                ],
            }
        ),
    )
    ops = [
        {"op": "add_block", "name": "headA", "envelope": "sphere:r2"},
        {
            "op": "add_port",
            "block": "headA",
            "name": "p",
            "roles": ["covalent"],
            "expected_element": "C",
        },
        {"op": "add_block", "name": "headB", "envelope": "sphere:r2"},
        {
            "op": "add_port",
            "block": "headB",
            "name": "p",
            "roles": ["covalent"],
            "expected_element": "C",
        },
        {
            "op": "bind_structure",
            "block": "headA",
            "design": "mech-dumbbell2-scene",
            "ports": {"p": "a1"},
        },
        {
            "op": "bind_structure",
            "block": "headB",
            "design": "mech-dumbbell2-scene",
            "ports": {"p": "b2"},
        },
        {"op": "connect", "a": "headA.p", "b": "headB.p"},
    ]
    handler.put(id="mech-dumbbell2", text=json.dumps({"ops": ops}))
    body = handler.get(id="mech-dumbbell2", view="mechanics").body
    assert "disconnected" not in body
    assert str(mechanics.RUPTURE_FORCE_NN) in body or "5" in body


def test_mechanics_view_cross_design_connect_renders_not_fused_not_zero(
    handler: NmHandler, structure: StructureHandler
) -> None:
    """Round-3 review finding: two ports bound to two DIFFERENT structure
    designs were never fused into one bond graph at all — rendering that
    as a bare ``0`` reads as "measured and found to be zero tensile
    capacity", indistinguishable from a genuinely shared-but-disconnected
    scene (the dumbbell-with-no-shaft case above). The unfilled-not-zero
    rule applies here too: this must render as its own distinct state."""
    for slug, label in (("mech-cross-a", "a1"), ("mech-cross-b", "b1")):
        structure.put(
            id=slug,
            text=json.dumps(
                {
                    "cell": {
                        "a": 20.0,
                        "b": 20.0,
                        "c": 20.0,
                        "pbc": [False, False, False],
                    },
                    "ops": [
                        {
                            "op": "add_atom",
                            "element": "C",
                            "label": label,
                            "cart": [0, 0, 0],
                        }
                    ],
                }
            ),
        )
    ops = [
        {"op": "add_block", "name": "headA", "envelope": "sphere:r2"},
        {
            "op": "add_port",
            "block": "headA",
            "name": "p",
            "roles": ["covalent"],
            "expected_element": "C",
        },
        {"op": "add_block", "name": "headB", "envelope": "sphere:r2"},
        {
            "op": "add_port",
            "block": "headB",
            "name": "p",
            "roles": ["covalent"],
            "expected_element": "C",
        },
        {
            "op": "bind_structure",
            "block": "headA",
            "design": "mech-cross-a",
            "ports": {"p": "a1"},
        },
        {
            "op": "bind_structure",
            "block": "headB",
            "design": "mech-cross-b",
            "ports": {"p": "b1"},
        },
        {"op": "connect", "a": "headA.p", "b": "headB.p"},
    ]
    handler.put(id="mech-cross", text=json.dumps({"ops": ops}))
    body = handler.get(id="mech-cross", view="mechanics").body
    assert "not fused" in body
    assert "disconnected" not in body
    tensile_lines = [line for line in body.splitlines() if "headA.p" in line]
    assert tensile_lines, body
    assert "0.0" not in tensile_lines[0] and " 0 " not in tensile_lines[0]
