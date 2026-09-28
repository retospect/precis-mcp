"""precis_chain.relax — the segment-body settle.

The four claims, each measured from the returned ``(B, 2, 3)`` array:

- Two rigid 21-unit bodies whose backbone exits start 6 nm apart, joined by a
  2-unit loop (contour ``3 * 0.63 = 1.89`` nm by the (n+1)-bond convention),
  settle to exit-to-exit within 1.9 nm while their axes stay at least ``2r``
  apart.
- An unloaded straight 8-body chain stays straight: bend radius over a
  micrometre (in fact infinite — nothing perturbs it, so the first force
  evaluation already converges).
- Excluded volume separates capsules that start interpenetrating, and resists a
  taut loop pulling two parallel bodies together. It is a penalty, not a
  projection, and the tolerances below say by how much.
- 400 bodies settle in well under 10 s.

The motif numbers are test fixtures; the kernel ships no nucleic-acid
constants.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from precis_chain.clash import segment_distance
from precis_chain.curvature import bend_radius
from precis_chain.loop import contour
from precis_chain.motif import Motif
from precis_chain.relax import (
    Attachment,
    Hinge,
    LoopSpring,
    Pin,
    hinge_stiffness,
    relax_bundle,
)

_MOTIF = Motif(
    name="test-duplex",
    rise=0.334,
    twist=math.radians(360.0 / 10.5),
    radius=1.0,
    min_bend_radius=10.0,
    persistence_length=50.0,
    contour_per_unit=0.63,
)
_SEGMENT = 21 * _MOTIF.rise  # 7.014 nm — one 21-unit segment


def _axis_gap(bodies: np.ndarray, i: int, j: int) -> float:
    """Closest distance between two bodies' axes, recomputed from the result."""
    return float(
        segment_distance(bodies[i, 0], bodies[i, 1], bodies[j, 0], bodies[j, 1])[0]
    )


def _transported(
    offset: np.ndarray, axis_before: np.ndarray, axis_after: np.ndarray
) -> np.ndarray:
    """The same rigid offset transport ``relax_bundle`` applies — recomputed
    here independently (Rodrigues from one unit axis to the other) so the exit
    positions are measured, not read back out of the kernel."""
    u0 = axis_before / np.linalg.norm(axis_before)
    u1 = axis_after / np.linalg.norm(axis_after)
    w = np.cross(u0, u1)
    k = np.array([[0.0, -w[2], w[1]], [w[2], 0.0, -w[0]], [-w[1], w[0], 0.0]])
    rot = np.eye(3) + k + (k @ k) / (1.0 + float(np.dot(u0, u1)))
    return rot @ offset


def _straight_chain(n: int, wobble: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    bodies = np.zeros((n, 2, 3))
    for i in range(n):
        bodies[i, 0] = [i * _SEGMENT, wobble * math.sin(i), 0.0]
        bodies[i, 1] = [(i + 1) * _SEGMENT, wobble * math.sin(i + 1), 0.0]
    return bodies, np.full(n, _MOTIF.radius)


def test_two_helices_joined_by_a_two_unit_loop_settle_to_the_contour() -> None:
    # Axes 8 nm apart, exits facing each other at radius 1 nm => 6 nm apart.
    bodies = np.array(
        [
            [[0.0, 0.0, 0.0], [_SEGMENT, 0.0, 0.0]],
            [[0.0, 8.0, 0.0], [_SEGMENT, 8.0, 0.0]],
        ]
    )
    offset_a = (0.0, 1.0, 0.0)
    offset_b = (0.0, -1.0, 0.0)
    exit_a = np.array(offset_a) * _MOTIF.radius
    exit_b = np.array(offset_b) * _MOTIF.radius
    start_gap = np.linalg.norm((bodies[1, 1] + exit_b) - (bodies[0, 1] + exit_a))
    assert start_gap == pytest.approx(6.0)

    rest = contour(2, _MOTIF.contour_per_unit)
    assert rest == pytest.approx(1.89)
    result = relax_bundle(
        bodies,
        [],
        [
            LoopSpring(
                Attachment(0, 1.0, offset_a),
                Attachment(1, 1.0, offset_b),
                rest,
                1.0,
            )
        ],
        [Pin(0, 0, (0.0, 0.0, 0.0)), Pin(0, 1, (_SEGMENT, 0.0, 0.0))],
        np.array([_MOTIF.radius, _MOTIF.radius]),
        3000,
    )
    assert result.converged
    settled = result.bodies
    axis0 = np.array([_SEGMENT, 0.0, 0.0])
    point_a = settled[0, 1] + _transported(exit_a, axis0, settled[0, 1] - settled[0, 0])
    point_b = settled[1, 1] + _transported(exit_b, axis0, settled[1, 1] - settled[1, 0])
    assert float(np.linalg.norm(point_b - point_a)) <= 1.9
    assert _axis_gap(settled, 0, 1) >= 2.0 * _MOTIF.radius
    # The pinned body has not moved and neither body has stretched.
    assert np.allclose(settled[0], bodies[0], atol=1e-9)
    assert float(np.linalg.norm(settled[1, 1] - settled[1, 0])) == pytest.approx(
        _SEGMENT, rel=1e-3
    )


def test_unloaded_straight_chain_stays_straight() -> None:
    bodies, radii = _straight_chain(8)
    stiffness = hinge_stiffness(_MOTIF, _SEGMENT)
    result = relax_bundle(
        bodies, [Hinge(i, i + 1, stiffness) for i in range(7)], [], [], radii, 500
    )
    assert result.converged
    joints = np.concatenate([result.bodies[:, 0], result.bodies[-1:, 1]])
    assert float(np.min(bend_radius(joints))) > 1000.0  # > 1 um in nm units
    assert np.allclose(result.bodies, bodies, atol=1e-12)


def test_a_bent_chain_straightens_toward_its_hinge_rest_angle() -> None:
    bodies, radii = _straight_chain(6)
    # Kink the far half by rotating every body after the third about +z.
    angle = math.radians(25.0)
    rot = np.array(
        [
            [math.cos(angle), -math.sin(angle), 0.0],
            [math.sin(angle), math.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    pivot = bodies[3, 0].copy()
    for i in range(3, 6):
        bodies[i] = (bodies[i] - pivot) @ rot.T + pivot
    before = float(np.min(bend_radius(np.concatenate([bodies[:, 0], bodies[-1:, 1]]))))
    stiffness = hinge_stiffness(_MOTIF, _SEGMENT)
    result = relax_bundle(
        bodies,
        [Hinge(i, i + 1, stiffness) for i in range(5)],
        [],
        [],
        radii,
        3000,
    )
    after = float(
        np.min(
            bend_radius(np.concatenate([result.bodies[:, 0], result.bodies[-1:, 1]]))
        )
    )
    assert after > before * 2.0


def test_excluded_volume_separates_bodies_that_start_interpenetrating() -> None:
    bodies = np.array(
        [
            [[0.0, 0.0, 0.0], [_SEGMENT, 0.0, 0.0]],
            [[0.0, 1.0, 0.0], [_SEGMENT, 1.0, 0.0]],
        ]
    )
    radii = np.array([_MOTIF.radius, _MOTIF.radius])
    assert _axis_gap(bodies, 0, 1) < 2.0 * _MOTIF.radius
    result = relax_bundle(bodies, [], [], [], radii, 2000)
    assert result.converged
    assert _axis_gap(result.bodies, 0, 1) >= 2.0 * _MOTIF.radius


def test_excluded_volume_resists_a_taut_loop_to_within_a_few_percent() -> None:
    # A rest-length-zero loop is the hardest case there is: it pulls the two
    # axes onto each other and only excluded volume opposes it. EV is a penalty
    # spring, so the settle interpenetrates a little — measured at ~1% here.
    bodies = np.array(
        [
            [[0.0, 0.0, 0.0], [_SEGMENT, 0.0, 0.0]],
            [[0.0, 6.0, 0.0], [_SEGMENT, 6.0, 0.0]],
        ]
    )
    result = relax_bundle(
        bodies,
        [],
        [LoopSpring(Attachment(0, 0.5), Attachment(1, 0.5), 0.0, 1.0)],
        [Pin(0, 0, (0.0, 0.0, 0.0)), Pin(0, 1, (_SEGMENT, 0.0, 0.0))],
        np.array([_MOTIF.radius, _MOTIF.radius]),
        4000,
    )
    assert result.converged
    gap = _axis_gap(result.bodies, 0, 1)
    assert gap >= 0.97 * 2.0 * _MOTIF.radius
    assert gap <= 2.0 * _MOTIF.radius


def test_hinged_pairs_are_exempt_from_excluded_volume() -> None:
    # Two consecutive segments of one helix touch by construction; if EV applied
    # to them the chain could never stay welded.
    bodies, radii = _straight_chain(2)
    stiffness = hinge_stiffness(_MOTIF, _SEGMENT)
    result = relax_bundle(bodies, [Hinge(0, 1, stiffness)], [], [], radii, 500)
    assert result.converged
    assert np.allclose(result.bodies, bodies, atol=1e-9)


def test_soft_pin_pulls_without_freezing_and_hard_pin_freezes() -> None:
    bodies, radii = _straight_chain(1)
    target = (0.0, 3.0, 0.0)
    soft = relax_bundle(bodies, [], [], [Pin(0, 0, target, stiffness=2.0)], radii, 3000)
    assert np.linalg.norm(soft.bodies[0, 0] - np.array(target)) < 0.05
    hard = relax_bundle(bodies, [], [], [Pin(0, 0, target)], radii, 3000)
    assert np.allclose(hard.bodies[0, 0], target)


def test_hinge_stiffness_is_the_worm_like_chain_constant() -> None:
    assert hinge_stiffness(_MOTIF, _SEGMENT) == pytest.approx(
        _MOTIF.persistence_length / (2.0 * _SEGMENT)
    )
    # Shorter segmentation, stiffer hinge: the same bend through less material.
    assert hinge_stiffness(_MOTIF, 1.0) > hinge_stiffness(_MOTIF, 10.0)
    with pytest.raises(ValueError, match="segment_length"):
        hinge_stiffness(_MOTIF, 0.0)


def test_relax_is_pure_and_validates_indices() -> None:
    bodies, radii = _straight_chain(3)
    snapshot = bodies.copy()
    relax_bundle(bodies, [Hinge(0, 1, 1.0)], [], [], radii, 10)
    assert np.allclose(bodies, snapshot)
    assert relax_bundle(np.zeros((0, 2, 3)), [], [], [], np.zeros(0), 10).converged
    with pytest.raises(ValueError, match=r"bodies must be"):
        relax_bundle(np.zeros((3, 3)), [], [], [], radii, 10)
    with pytest.raises(ValueError, match="radii must be"):
        relax_bundle(bodies, [], [], [], np.zeros(2), 10)
    with pytest.raises(ValueError, match="hinge body index"):
        relax_bundle(bodies, [Hinge(0, 9, 1.0)], [], [], radii, 10)
    with pytest.raises(ValueError, match="loop attachment body"):
        relax_bundle(
            bodies, [], [LoopSpring(Attachment(0), Attachment(9), 1.0)], [], radii, 10
        )
    with pytest.raises(ValueError, match="pin body"):
        relax_bundle(bodies, [], [], [Pin(9, 0, (0.0, 0.0, 0.0))], radii, 10)
    with pytest.raises(ValueError, match="pin end"):
        relax_bundle(bodies, [], [], [Pin(0, 2, (0.0, 0.0, 0.0))], radii, 10)


@pytest.mark.slow
def test_four_hundred_bodies_settle_in_under_ten_seconds() -> None:
    bodies, radii = _straight_chain(400, wobble=0.02)
    stiffness = hinge_stiffness(_MOTIF, _SEGMENT)
    hinges = [Hinge(i, i + 1, stiffness) for i in range(399)]
    started = time.perf_counter()
    result = relax_bundle(bodies, hinges, [], [], radii, 500)
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0, f"400-body settle took {elapsed:.1f}s"
    assert result.bodies.shape == (400, 2, 3)
    assert np.all(np.isfinite(result.bodies))

    # The wobble is mostly gone: the chain is straighter than it started.
    def kink(b: np.ndarray) -> float:
        return float(np.min(bend_radius(np.concatenate([b[:, 0], b[-1:, 1]]))))

    assert kink(result.bodies) > kink(bodies)
