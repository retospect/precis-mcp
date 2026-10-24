"""The ``jiggle`` relax rung (dev-loop-scratch spec D0) — a seeded
Gaussian perturbation with a fabricated-but-consistent convergence
report, for testing how the system tolerates moved/"relaxed" geometry.

DB-free: exercises :func:`precis.structure.relax` directly.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.structure import (
    RelaxUnsupported,
    Scene,
    apply_ops,
    relax,
)
from precis.structure.cell import Cell
from precis.structure.relax import _JIGGLE_SIGMA, JiggleDied
from precis.structure.scene import FIX_ALL, FIX_X


def _scene(*, n: int = 5, a: float = 30.0) -> Scene:
    """n carbons spread through a big non-periodic box (no wrap tricks)."""
    scene = Scene(cell=Cell.from_lengths_angles(a, a, a, pbc=(False, False, False)))
    apply_ops(
        scene,
        [
            {
                "op": "add_atom",
                "element": "C",
                "frac": [0.5 + (i - n // 2) * 0.1, 0.5, 0.5],
            }
            for i in range(n)
        ],
    )
    return scene


def _fracs(scene: Scene) -> dict[str, np.ndarray]:
    return {la: a.frac.copy() for la, a in scene.atoms.items()}


def _cart_displacement(scene: Scene, before: dict[str, np.ndarray]) -> np.ndarray:
    """Per-atom Cartesian displacement since ``before``."""
    return np.array(
        [
            scene.cell.frac_to_cart(scene.atoms[la].frac - before[la])
            for la in scene.atoms
        ]
    )


def test_jiggle_report_shape() -> None:
    scene = _scene()
    res = relax(scene, fidelity="jiggle", steps=40, seed=7)
    assert res.rung == "jiggle"
    assert res.converged
    assert res.n_steps == 40
    assert len(res.curve) == 40
    # monotone non-increasing, ending under the rungs' usual floor
    assert all(b <= a for a, b in zip(res.curve, res.curve[1:]))
    assert res.curve[-1] <= 0.05
    assert res.energy is not None and res.max_force is not None
    assert res.max_force == res.curve[-1]
    assert res.model == "jiggle"
    # honestly labelled a fabrication, never a calculator's numbers
    assert res.forces_approx is True
    assert res.forces_source == "jiggle"
    assert "fabricated" in (res.note or "")


def test_jiggle_moves_atoms_seeded_deterministic() -> None:
    s1, s2, s3 = _scene(), _scene(), _scene()
    relax(s1, fidelity="jiggle", steps=10, seed=42)
    relax(s2, fidelity="jiggle", steps=10, seed=42)
    relax(s3, fidelity="jiggle", steps=10, seed=43)
    for la in s1.atoms:
        assert np.allclose(s1.atoms[la].frac, s2.atoms[la].frac)
        assert not np.allclose(s1.atoms[la].frac, s3.atoms[la].frac)
    moved = any(
        not np.allclose(s1.atoms[la].frac, _scene().atoms[la].frac) for la in s1.atoms
    )
    assert moved


def test_jiggle_unseeded_varies() -> None:
    s1, s2 = _scene(), _scene()
    relax(s1, fidelity="jiggle", steps=5)
    relax(s2, fidelity="jiggle", steps=5)
    assert any(not np.allclose(s1.atoms[la].frac, s2.atoms[la].frac) for la in s1.atoms)


def test_jiggle_respects_fixed() -> None:
    scene = _scene()
    scene.atoms["aC1"].fixed = FIX_ALL
    scene.atoms["aC2"].fixed = FIX_X
    before = _fracs(scene)
    relax(scene, fidelity="jiggle", steps=10, seed=1)
    assert np.allclose(scene.atoms["aC1"].frac, before["aC1"])
    d2 = scene.cell.frac_to_cart(scene.atoms["aC2"].frac - before["aC2"])
    assert d2[0] == pytest.approx(0.0)
    assert not np.allclose(d2, 0.0)


def test_jiggle_caps_at_three_sigma() -> None:
    """A draw beyond 3σ is rescaled to 3σ — with a fat sigma and enough
    atoms to guarantee an outlier, no atom lands further than 3σ."""
    scene = _scene(n=20)
    before = _fracs(scene)
    relax(scene, fidelity="jiggle", steps=5, sigma=0.5, seed=3)
    disp = _cart_displacement(scene, before)
    assert np.linalg.norm(disp, axis=1).max() <= 1.5 + 1e-9


def test_jiggle_sigma_scales_the_displacement() -> None:
    small, big = _scene(), _scene()
    b_s, b_b = _fracs(small), _fracs(big)
    relax(small, fidelity="jiggle", steps=5, sigma=0.02, seed=9)
    relax(big, fidelity="jiggle", steps=5, sigma=0.2, seed=9)
    d_s = np.linalg.norm(_cart_displacement(small, b_s), axis=1)
    d_b = np.linalg.norm(_cart_displacement(big, b_b), axis=1)
    # same seed ⇒ same normalised draw; displacement scales with sigma
    assert np.allclose(d_b, d_s * 10, rtol=1e-6)


def test_jiggle_die_at_step_raises_mid_run() -> None:
    scene = _scene()
    with pytest.raises(JiggleDied, match="die_at_step"):
        relax(scene, fidelity="jiggle", steps=10, seed=7, die_at_step=5)


def test_jiggle_die_leaves_a_mid_run_geometry() -> None:
    """Dying at step k leaves (k-1)/steps of the displacement — the
    checkpoint model: completed steps landed, step k's work never ran."""
    died, full = _scene(), _scene()
    b_d, b_f = _fracs(died), _fracs(full)
    with pytest.raises(JiggleDied):
        relax(died, fidelity="jiggle", steps=10, seed=7, die_at_step=5)
    relax(full, fidelity="jiggle", steps=10, seed=7)
    for la in died.atoms:
        d_died = died.cell.frac_to_cart(died.atoms[la].frac - b_d[la])
        d_full = full.cell.frac_to_cart(full.atoms[la].frac - b_f[la])
        assert np.allclose(d_died, d_full * 0.4)


def test_jiggle_emits_per_step_checkpoints() -> None:
    """``on_step`` (gr508493) fires per fabricated step — the positions
    carry that step's share of the displacement, the curve so far."""
    scene = _scene()
    seen: list[tuple[int, np.ndarray, list[float]]] = []
    relax(
        scene,
        fidelity="jiggle",
        steps=6,
        seed=5,
        on_step=lambda n, pos, curve: seen.append((n, pos.copy(), list(curve))),
    )
    assert [s[0] for s in seen] == [1, 2, 3, 4, 5, 6]
    assert all(len(s[2]) == s[0] for s in seen)
    # the last checkpoint's positions are the finished geometry
    final = np.array(
        [scene.cell.frac_to_cart(scene.atoms[la].frac) for la in scene.atoms]
    )
    assert np.allclose(seen[-1][1], final)


def test_jiggle_die_at_step_checkpoints_stop_before_the_kill() -> None:
    """A kill at step k checkpoints steps 1..k-1 — the resume trail a
    real crashed rung would leave (gr508493)."""
    scene = _scene()
    seen: list[int] = []
    with pytest.raises(JiggleDied):
        relax(
            scene,
            fidelity="jiggle",
            steps=10,
            seed=7,
            die_at_step=5,
            on_step=lambda n, pos, curve: seen.append(n),
        )
    assert seen == [1, 2, 3, 4]


def test_jiggle_die_at_step_one_leaves_the_scene_untouched() -> None:
    scene = _scene()
    before = _fracs(scene)
    with pytest.raises(JiggleDied):
        relax(scene, fidelity="jiggle", steps=10, seed=7, die_at_step=1)
    for la in scene.atoms:
        assert np.allclose(scene.atoms[la].frac, before[la])


def test_jiggle_empty_scene_converges() -> None:
    scene = Scene(cell=Cell.from_lengths_angles(10, 10, 10))
    res = relax(scene, fidelity="jiggle", steps=10, seed=1)
    assert res.converged and res.n_steps == 0


def test_jiggle_sigma_zero_is_a_noop() -> None:
    scene = _scene()
    before = _fracs(scene)
    res = relax(scene, fidelity="jiggle", steps=10, seed=1, sigma=0.0)
    assert res.converged
    for la in scene.atoms:
        assert np.allclose(scene.atoms[la].frac, before[la])


def test_jiggle_options_are_refused_on_other_rungs() -> None:
    for kw in ({"seed": 1}, {"sigma": 0.1}, {"die_at_step": 2}):
        with pytest.raises(RelaxUnsupported, match="jiggle"):
            relax(_scene(), fidelity="clean", **kw)
        with pytest.raises(RelaxUnsupported, match="jiggle"):
            relax(_scene(), fidelity="emt", **kw)


def test_jiggle_rejects_energy_rung_knobs() -> None:
    for kw in (
        {"cell": "inplane"},
        {"fmax": 0.01},
        {"dispersion": True},
        {"charge": 1},
        {"multiplicity": 2},
    ):
        with pytest.raises(RelaxUnsupported):
            relax(_scene(), fidelity="jiggle", **kw)


def test_jiggle_rejects_bad_option_values() -> None:
    with pytest.raises(RelaxUnsupported):
        relax(_scene(), fidelity="jiggle", sigma=-0.1)
    with pytest.raises(RelaxUnsupported):
        relax(_scene(), fidelity="jiggle", die_at_step=0)


def test_jiggle_default_sigma_is_the_spec_value() -> None:
    assert pytest.approx(0.05) == _JIGGLE_SIGMA


def test_jiggle_progress_lines_match_the_energy_rungs(caplog) -> None:
    import logging

    scene = _scene()
    with caplog.at_level(logging.INFO, logger="precis.structure.relax"):
        relax(scene, fidelity="jiggle", steps=30, seed=1)
    lines = [
        r.message
        for r in caplog.records
        if r.name == "precis.structure.relax" and "relax[jiggle]:" in r.message
    ]
    assert len(lines) == 3  # steps 10, 20, 30 — PROGRESS_EVERY cadence
    assert "step 10/30" in lines[0]
    assert "max force" in lines[0] and "eV/A" in lines[0]
