"""Diverse-cone sampler for the ``angle`` spray on ``search``.

Given a unit seed vector ``v`` and a target cosine ``angle`` in
``[-1, 1]``, produce ``n`` unit anchors that each sit at *exactly*
cosine ``angle`` from ``v`` but point in different directions. The
caller snaps each anchor to its nearest real chunk via ANN; high
dimensions make the random perpendicular components near-orthogonal,
so the snapped items spread on their own — no ``diversify`` flag
(docs/design/dreaming.md, §The ``angle`` spray).

**One formula covers every angle.** For a unit ``v`` and a random unit
vector ``u`` drawn orthogonal to ``v``::

    w = angle*v + sqrt(1 - angle^2)*u      # cosine(w, v) == angle

``angle=1`` gives ``w = v`` (plain nearest-neighbour); ``angle=-1``
gives ``w = -v``; ``angle=0`` is orthogonal.

**Pure Python on purpose.** ``numpy`` is an *optional* dependency
(transitive via the ``embed`` extra); the torch-free dream worker that
drives this sampler must run without it. The math is a handful of dot
products over a 1024-d vector times ``n`` (~8) anchors — trivial cost.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def _norm(v: Sequence[float]) -> float:
    return math.sqrt(math.fsum(x * x for x in v))


def normalize(v: Sequence[float]) -> list[float]:
    """Return ``v`` scaled to unit L2 length.

    Raises ``ValueError`` on a zero vector — there is no meaningful
    direction to seed a spray from.
    """
    n = _norm(v)
    if n == 0.0:
        raise ValueError("cannot normalize a zero vector")
    return [float(x) / n for x in v]


def angle_anchors(
    seed: Sequence[float],
    angle: float,
    n: int,
    *,
    rng: random.Random | None = None,
) -> list[list[float]]:
    """Build ``n`` unit anchors at cosine ``angle`` from ``seed``.

    Each anchor is ``angle*v + sqrt(1-angle^2)*u`` for a fresh random
    unit ``u`` drawn orthogonal to the normalized seed ``v`` (Gram-
    Schmidt against ``v``). The exact cosine to ``v`` is ``angle`` by
    construction (up to float error).

    ``rng`` is injectable so callers/tests get deterministic anchors;
    defaults to a fresh, entropy-seeded :class:`random.Random`.

    Raises ``ValueError`` for ``angle`` outside ``[-1, 1]``, ``n < 1``,
    or a zero/empty seed.
    """
    if not -1.0 <= angle <= 1.0:
        raise ValueError(f"angle must be in [-1, 1], got {angle}")
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    rng = rng or random.Random()

    v = normalize(seed)
    dim = len(v)
    perp_scale = math.sqrt(max(0.0, 1.0 - angle * angle))

    anchors: list[list[float]] = []
    for _ in range(n):
        u = _random_orthogonal_unit(v, dim, rng)
        w = [angle * vi + perp_scale * ui for vi, ui in zip(v, u, strict=True)]
        anchors.append(w)
    return anchors


def _random_orthogonal_unit(
    v: list[float], dim: int, rng: random.Random
) -> list[float]:
    """A random unit vector orthogonal to unit vector ``v``.

    Draws an isotropic Gaussian, subtracts its component along ``v``,
    and renormalizes. Retries a few times on the measure-zero chance
    the residual is degenerate (drew (anti)parallel to ``v``); falls
    back to a zero perpendicular (anchor collapses to ``angle*v``) if
    every draw degenerates, which only the ``dim==1`` case can force.
    """
    for _ in range(8):
        u = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        dot = math.fsum(ui * vi for ui, vi in zip(u, v, strict=True))
        u = [ui - dot * vi for ui, vi in zip(u, v, strict=True)]
        norm = _norm(u)
        if norm > 1e-9:
            return [x / norm for x in u]
    return [0.0] * dim
