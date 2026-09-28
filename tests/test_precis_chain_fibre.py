"""precis_chain.fibre — per-unit placement and backbone exits.

Theorems recomputed from the returned arrays:

- Unit ``k`` sits at arc length ``k * rise``; consecutive unit origins are
  exactly ``rise`` apart on a straight path.
- The frame field carries the motif's twist: the summed per-step roll over 21
  units at 10.5 units/turn is two whole turns.
- A backbone exit sits exactly ``motif.radius`` off the axis, perpendicular to
  it, and two exits at azimuths ``theta`` and ``theta + pi`` are ``2 * radius``
  apart — the duplex's two strands.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis_chain.fibre import backbone_exit, unit_frames
from precis_chain.frames import accumulated_twist
from precis_chain.motif import Motif
from precis_chain.path import Path, polyline

_MOTIF = Motif(
    name="test-duplex",
    rise=0.334,
    twist=math.radians(360.0 / 10.5),
    radius=1.0,
    min_bend_radius=10.0,
    persistence_length=50.0,
    contour_per_unit=0.63,
)


def _straight(length: float, n: int = 500) -> Path:
    z = np.linspace(0.0, length, n)
    return polyline(np.stack([np.zeros_like(z), np.zeros_like(z), z], axis=1))


def test_unit_origins_are_one_rise_apart_along_the_axis() -> None:
    path = _straight(30.0)
    placed = unit_frames(path, _MOTIF, 21)
    assert len(placed) == 21
    assert placed.origins.shape == (21, 3)
    assert placed.frames.shape == (21, 3, 3)
    assert np.allclose(placed.s, np.arange(21) * _MOTIF.rise)
    gaps = np.linalg.norm(np.diff(placed.origins, axis=0), axis=1)
    assert np.allclose(gaps, _MOTIF.rise, atol=1e-9)
    assert np.allclose(placed.frames[:, :, 0], np.tile([0.0, 0.0, 1.0], (21, 1)))


def test_placed_frames_carry_two_whole_turns_over_21_units() -> None:
    placed = unit_frames(_straight(30.0), _MOTIF, 21)
    assert accumulated_twist(placed.frames) == pytest.approx(
        20 * _MOTIF.twist, abs=1e-9
    )
    # 21 unit ORIGINS span 20 steps; the 21-unit run itself is two turns.
    assert 21 * _MOTIF.twist == pytest.approx(4.0 * math.pi, abs=1e-9)


def test_phase0_rotates_every_unit_by_the_same_offset() -> None:
    base = unit_frames(_straight(30.0), _MOTIF, 11)
    shifted = unit_frames(_straight(30.0), _MOTIF, 11, phase0=math.pi / 2.0)
    for k in range(11):
        # +pi/2 about the tangent takes the normal onto the old binormal.
        assert np.allclose(shifted.frames[k, :, 1], base.frames[k, :, 2], atol=1e-12)


def test_explicit_r0_pins_the_absolute_azimuth() -> None:
    placed = unit_frames(_straight(30.0), _MOTIF, 5, r0=np.array([1.0, 0.0, 0.0]))
    assert np.allclose(placed.frames[0, :, 1], [1.0, 0.0, 0.0], atol=1e-12)


def test_backbone_exit_is_radius_off_the_axis_and_perpendicular_to_it() -> None:
    placed = unit_frames(_straight(30.0), _MOTIF, 21)
    for azimuth in (0.0, 1.0, math.pi, -2.5):
        offsets = backbone_exit(placed.frames, _MOTIF, azimuth)
        assert offsets.shape == (21, 3)
        assert np.allclose(np.linalg.norm(offsets, axis=1), _MOTIF.radius)
        assert np.allclose(
            np.einsum("ij,ij->i", offsets, placed.frames[:, :, 0]), 0.0, atol=1e-12
        )
    points = backbone_exit(placed.frames, _MOTIF, 0.0, placed.origins)
    assert np.allclose(
        points - placed.origins, backbone_exit(placed.frames, _MOTIF, 0.0)
    )


def test_two_antiparallel_strand_azimuths_are_a_diameter_apart() -> None:
    placed = unit_frames(_straight(30.0), _MOTIF, 21)
    strand_a = backbone_exit(placed.frames, _MOTIF, 0.4, placed.origins)
    strand_b = backbone_exit(placed.frames, _MOTIF, 0.4 + math.pi, placed.origins)
    assert np.allclose(np.linalg.norm(strand_a - strand_b, axis=1), 2.0 * _MOTIF.radius)


def test_backbone_exit_accepts_a_single_frame() -> None:
    placed = unit_frames(_straight(30.0), _MOTIF, 3)
    single = backbone_exit(placed.frames[1], _MOTIF, 0.7)
    assert single.shape == (3,)
    assert np.allclose(single, backbone_exit(placed.frames, _MOTIF, 0.7)[1])
    with_origin = backbone_exit(placed.frames[1], _MOTIF, 0.7, placed.origins[1])
    assert np.allclose(with_origin, placed.origins[1] + single)


def test_a_curved_path_still_places_units_at_the_right_arc_length() -> None:
    radius = 20.0
    t = np.linspace(0.0, math.pi / 2.0, 4000)
    arc = polyline(
        np.stack([radius * np.cos(t), radius * np.sin(t), np.zeros_like(t)], axis=1)
    )
    placed = unit_frames(arc, _MOTIF, 30)
    # Origins lie on the circle and advance by the rise in arc length.
    assert np.allclose(np.linalg.norm(placed.origins, axis=1), radius, atol=1e-4)
    angles = np.arctan2(placed.origins[:, 1], placed.origins[:, 0])
    assert np.allclose(np.diff(angles), _MOTIF.rise / radius, rtol=1e-3)


def test_placement_validation() -> None:
    assert len(unit_frames(_straight(1.0), _MOTIF, 0)) == 0
    with pytest.raises(ValueError, match="n_units must be >= 0"):
        unit_frames(_straight(30.0), _MOTIF, -1)
    with pytest.raises(ValueError, match="need"):
        unit_frames(_straight(1.0), _MOTIF, 21)
    # Exactly (n-1) rises of path is enough — the last unit's ORIGIN fits.
    exact = _straight(20 * _MOTIF.rise)
    assert len(unit_frames(exact, _MOTIF, 21)) == 21
    with pytest.raises(ValueError, match=r"frames must be"):
        backbone_exit(np.zeros((2, 2)), _MOTIF, 0.0)
    placed = unit_frames(_straight(30.0), _MOTIF, 4)
    with pytest.raises(ValueError, match="origins has"):
        backbone_exit(placed.frames, _MOTIF, 0.0, np.zeros((2, 3)))
