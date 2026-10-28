"""gr464502: the rim-to-rim fuse twist must not depend on rounding noise.

When the two rims' dangling lists wind opposite ways the angular offsets are
spread evenly round the circle, their circular mean is 0/0, and ``atan2`` of
the rounding noise used to pick a different twist per host (and per CI run).
"""

from __future__ import annotations

import math

import numpy as np

from hexfold.build import _fuse_transform


def _two_rims(n: int = 6) -> tuple[np.ndarray, tuple[int, ...], tuple[int, ...]]:
    ang = [2 * math.pi * i / n for i in range(n)]
    p = [(3 * math.cos(a), 3 * math.sin(a), 0.0) for a in ang]
    q = [(3 * math.cos(a), 3 * math.sin(a), 10.0) for a in ang]
    pos = np.array(p + q)
    return pos, tuple(range(n)), tuple(range(n, 2 * n))


def test_degenerate_twist_is_stable_under_rounding_noise() -> None:
    pos, p_dang, q_dang = _two_rims()
    inst_p = np.array([0.0, 0.0, -5.0])
    inst_q = np.array([0.0, 0.0, 15.0])
    ref_r, ref_t = _fuse_transform(pos, p_dang, q_dang, 0, 1.42, inst_p, inst_q)
    rng = np.random.default_rng(0)
    for _ in range(25):
        noisy = pos + rng.normal(size=pos.shape) * 1e-13
        r, t = _fuse_transform(noisy, p_dang, q_dang, 0, 1.42, inst_p, inst_q)
        assert np.abs(r - ref_r).max() < 1e-9
        assert np.abs(t - ref_t).max() < 1e-9
