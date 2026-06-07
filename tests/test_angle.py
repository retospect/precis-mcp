"""Anchor sampler for the ``angle`` spray (pure math, no DB).

Pins the one invariant the whole feature rests on: every anchor sits
at cosine ``angle`` from the seed (docs/design/dreaming.md, §The
``angle`` spray), anchors are unit length and (in high-d) mutually
diverse, and the sampler is deterministic under a seeded RNG.
"""

from __future__ import annotations

import math
import random

import pytest

from precis.utils.angle import angle_anchors, normalize


def _cos(a: list[float], b: list[float]) -> float:
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(y * y for y in b))
    return dot / (na * nb)


def _seed(dim: int = 64) -> list[float]:
    rng = random.Random(0)
    return normalize([rng.gauss(0, 1) for _ in range(dim)])


@pytest.mark.parametrize("angle", [1.0, 0.8, 0.5, 0.0, -0.5, -1.0])
def test_anchors_sit_at_requested_cosine(angle: float) -> None:
    v = _seed()
    anchors = angle_anchors(v, angle, n=12, rng=random.Random(42))
    for w in anchors:
        assert _cos(w, v) == pytest.approx(angle, abs=1e-9)


def test_anchors_are_unit_length() -> None:
    v = _seed()
    for w in angle_anchors(v, 0.5, n=10, rng=random.Random(1)):
        assert math.sqrt(math.fsum(x * x for x in w)) == pytest.approx(1.0, abs=1e-9)


def test_angle_one_returns_the_seed_direction() -> None:
    v = _seed()
    (w,) = angle_anchors(v, 1.0, n=1, rng=random.Random(7))
    assert _cos(w, v) == pytest.approx(1.0, abs=1e-9)


def test_angle_minus_one_returns_opposite_pole() -> None:
    v = _seed()
    (w,) = angle_anchors(v, -1.0, n=1, rng=random.Random(7))
    assert _cos(w, v) == pytest.approx(-1.0, abs=1e-9)


def test_high_dim_anchors_are_mutually_diverse() -> None:
    # In high dimensions random perpendicular directions are near-
    # orthogonal, so distinct anchors spread on their own (the design's
    # "no diversify flag" claim). Pairwise cosines cluster near angle^2.
    v = _seed(dim=256)
    angle = 0.5
    anchors = angle_anchors(v, angle, n=8, rng=random.Random(3))
    for i in range(len(anchors)):
        for j in range(i + 1, len(anchors)):
            # cos(w_i, w_j) = angle^2 + (1-angle^2)*cos(u_i, u_j); the
            # second term is small for near-orthogonal u's.
            assert _cos(anchors[i], anchors[j]) < 0.6


def test_deterministic_under_seeded_rng() -> None:
    v = _seed()
    a = angle_anchors(v, 0.3, n=5, rng=random.Random(99))
    b = angle_anchors(v, 0.3, n=5, rng=random.Random(99))
    assert a == b


def test_rejects_bad_angle_and_n() -> None:
    v = _seed()
    with pytest.raises(ValueError, match="angle"):
        angle_anchors(v, 1.5, n=4)
    with pytest.raises(ValueError, match="n must"):
        angle_anchors(v, 0.5, n=0)


def test_normalize_rejects_zero_vector() -> None:
    with pytest.raises(ValueError, match="zero vector"):
        normalize([0.0, 0.0, 0.0])
