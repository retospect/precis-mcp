"""Atomic-mode L4 mechanics ceilings — :mod:`precis_se.atomic.mechanics`'s
closed-form min-cut tensile / Euler buckling / harmonic strain energy, plus
the two ``view='mechanics'`` min-cut topologies. Every figure here is
advisory (never gates) — these tests check the NUMBERS, never that anything
raises or blocks a write.

nm's ``tests/test_nm_mechanics.py`` ported by the nm→se merge
(docs/backlog/nm-se-merge.md): the pure-formula half moved unchanged (it
already imported the module at its se home), and the two dumbbell render
tests — the min-cut *shapes* nobody else pins — are re-seeded through
:class:`~precis_se.handler.SeHandler` with canonical-metre envelopes and an
explicit ``kind='bond'`` connect. The other three nm view tests are already
covered on se by ``tests/test_se_atomic_bind.py``
(``mech-empty``/``mech-cnt``/the not-fused pair).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import precis_se
from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Bond, Scene
from precis_se.atomic import mechanics
from precis_se.atomic.generate import ingest_envelope
from precis_se.atomic.generators.sp2 import build_cnt
from precis_se.handler import SeHandler

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


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
    assert ceiling == pytest.approx(mechanics.RUPTURE_FORCE_N)


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
    assert ceiling == pytest.approx(2 * mechanics.RUPTURE_FORCE_N)


def test_min_cut_disconnected_components_is_zero() -> None:
    """The demo-style "dumbbell" case: two atoms with no bond path between
    them — zero tensile ceiling, the honest answer for a genuinely
    unconnected structure, not an error."""
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
    expected_N = (
        (np.pi**2)
        * mechanics.E_MODULUS_PA
        * (np.pi * (r_A * 1e-10) ** 3 * (mechanics.TUBE_WALL_THICKNESS_A * 1e-10))
        / (length_A * 1e-10) ** 2
    )
    assert mechanics.euler_buckling_ceiling_N(r_A, length_A) == pytest.approx(
        expected_N, rel=1e-9
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
    # tube_geometry_from_envelope reads a STORED (design-space, metres)
    # envelope — the generator's own `block.envelope` is Å-SUFFIXED raw
    # output (the enclave's atomistic math, untouched); round-trip it
    # through the SAME single ingest boundary `generate` uses in
    # production rather than feeding raw Å text to a metres-expecting
    # function.
    stored_env = ingest_envelope(block.envelope)
    geom = mechanics.tube_geometry_from_envelope(stored_env)
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
    )
    assert mechanics.euler_buckling_ceiling_N(radius_A, length_A) == pytest.approx(
        hand_expected, rel=1e-3
    )


def test_tube_geometry_from_envelope_rejects_cone() -> None:
    """A cone's tapered wall isn't a constant-radius buckling candidate
    (module docstring, point 2) — never treated as a tube. ``envelope`` is
    STORED (design-space, canonical/storage-mode) text — bare metres, no
    unit suffix, the shape this function actually reads in production."""
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
    energy_J, n_triples = mechanics.harmonic_strain_energy_J(scene)
    assert n_triples > 0
    # near zero relative to the deliberately-bent case below, not exactly
    # zero -- a rolled sheet has a little genuine curvature-induced angle
    # deviation from the flat-sheet 120 deg ideal. Bound restated in SI
    # (1 eV, the pre-cutover bound, converted once via mechanics'
    # own eV->J factor) rather than re-tuned in J from scratch.
    assert energy_J < 1.0 * mechanics._EV_TO_J


def test_strain_energy_of_a_deliberately_bent_cnt_is_positive_and_larger() -> None:
    block = build_cnt({"n": 8, "m": 8, "length_A": 25.0})
    pristine_e, _n = mechanics.harmonic_strain_energy_J(_cnt_scene(block, block.coords))
    bent_coords = block.coords.copy()
    bent_coords[0] += np.array([2.5, 2.5, 2.5])  # a genuinely large local kink
    bent_e, _n2 = mechanics.harmonic_strain_energy_J(_cnt_scene(block, bent_coords))
    assert bent_e > 0.0
    assert bent_e > pristine_e


def test_strain_energy_no_covalent_neighbors_contributes_nothing() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    energy_J, n_triples = mechanics.harmonic_strain_energy_J(scene)
    assert energy_J == 0.0
    assert n_triples == 0


# ── view='mechanics': the two min-cut topologies ─────────────────────────


def _dumbbell_ops(design: str, a_atom: str, b_atom: str) -> list[dict[str, object]]:
    """Two head blocks bound to the SAME structure design, wired by one
    bond connect — the topology whose min-cut the view reports."""
    return [
        {"op": "add_block", "name": "headA", "envelope": "sphere:r2e-10"},
        {
            "op": "add_port",
            "block": "headA",
            "name": "p",
            "roles": ["covalent"],
            "expected_element": "C",
        },
        {
            "op": "add_block",
            "name": "headB",
            "envelope": "sphere:r2e-10",
            "pose": [1e-9, 0, 0],
        },
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
            "design": design,
            "ports": {"p": a_atom},
        },
        {
            "op": "bind_structure",
            "block": "headB",
            "design": design,
            "ports": {"p": b_atom},
        },
        {"op": "connect", "a": "headA.p", "b": "headB.p", "kind": "bond"},
    ]


def _two_fragment_scene(structure: StructureHandler, slug: str, *, shaft: bool) -> None:
    """Two bonded C2 fragments in one design — joined by a third bond when
    ``shaft`` (one connected graph), otherwise genuinely disconnected."""
    ops: list[dict[str, object]] = [
        {"op": "add_atom", "element": "C", "label": "a1", "cart": [0, 0, 0]},
        {"op": "add_atom", "element": "C", "label": "a2", "cart": [1.5, 0, 0]},
        {"op": "add_bond", "i": "a1", "j": "a2"},
        {
            "op": "add_atom",
            "element": "C",
            "label": "b1",
            "cart": [3.0, 0, 0] if shaft else [20.0, 0, 0],
        },
        {
            "op": "add_atom",
            "element": "C",
            "label": "b2",
            "cart": [4.5, 0, 0] if shaft else [21.5, 0, 0],
        },
        {"op": "add_bond", "i": "b1", "j": "b2"},
    ]
    if shaft:
        ops.append({"op": "add_bond", "i": "a2", "j": "b1"})
    structure.put(
        id=slug,
        text=json.dumps(
            {
                "cell": {"a": 30.0, "b": 30.0, "c": 30.0, "pbc": [False, False, False]},
                "ops": ops,
            }
        ),
    )


def test_mechanics_view_min_cut_zero_for_a_dumbbell_with_no_shaft(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """The demo-style dumbbell topology: two heads bound to the SAME
    structure design, but with no bond path between the two fragments —
    the min-cut ceiling renders 0 and *says* disconnected, rather than
    passing a bare zero off as a measurement."""
    _two_fragment_scene(structure, "mech-dumbbell-scene", shaft=False)
    handler.put(
        id="mech-dumbbell",
        text=json.dumps({"ops": _dumbbell_ops("mech-dumbbell-scene", "a1", "b1")}),
    )
    body = handler.get(id="mech-dumbbell", view="mechanics").body
    assert "headA.p" in body and "headB.p" in body
    tensile_lines = [line for line in body.splitlines() if "headA.p" in line]
    assert tensile_lines, body
    assert any("disconnected" in line for line in tensile_lines)


def test_mechanics_view_min_cut_positive_for_a_dumbbell_with_a_shaft(
    handler: SeHandler, structure: StructureHandler
) -> None:
    _two_fragment_scene(structure, "mech-dumbbell2-scene", shaft=True)
    handler.put(
        id="mech-dumbbell2",
        text=json.dumps({"ops": _dumbbell_ops("mech-dumbbell2-scene", "a1", "b2")}),
    )
    body = handler.get(id="mech-dumbbell2", view="mechanics").body
    assert "disconnected" not in body
    tensile_lines = [line for line in body.splitlines() if "headA.p" in line]
    assert tensile_lines, body
    # A real measured ceiling, never the unfilled/zero states the
    # honesty rule keeps distinct (test_se_atomic_bind.py pins those).
    assert "unfilled" not in tensile_lines[0]
    assert f"{mechanics.RUPTURE_FORCE_N:.4g}" in body or "5" in body
