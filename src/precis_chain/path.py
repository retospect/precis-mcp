"""Chain centre-line curves: the :class:`Path` contract, polylines, splines
and arc-length reparametrisation.

A :class:`Path` is the kernel's one curve representation — sampled points,
one unit tangent per point, and the cumulative chord arc length. Everything
downstream (frames, curvature, capsules, per-unit fibre placement) consumes
that triple and nothing else, so a caller may build a path any way it likes
(analytically, from a lattice, from a solver) as long as it hands back the
three arrays.

Units are the caller's throughout (``docs/backlog/precis-chain-kernel.md``):
the arrays never know whether they hold metres, nanometres or angstroms.

Tangent provenance differs on purpose:

- :func:`polyline` differentiates the sampled points (central differences
  inside, one-sided at an open end). Tangents therefore carry the sampling's
  own ``O(h²)`` error — which is what makes a refinement test meaningful.
- :func:`catmull_rom` and :func:`hermite` use the spline's *analytic*
  derivative, so their tangents are exact for the curve they describe.
- :func:`resample_arc_length` linearly interpolates the tangents it is given
  and renormalises; it never re-differentiates.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Below this length a vector is treated as having no direction. Chosen
#: relative to nothing (the kernel is unit-agnostic), so it is an absolute
#: floor: a caller working in metres on nanometre-scale objects should scale
#: to a sane unit first, exactly as every other module here assumes.
_EPS = 1e-12


def _unit(vectors: np.ndarray) -> np.ndarray:
    """Row-normalise ``(N, 3)`` (or a bare ``(3,)``). Zero-length rows come
    back as ``+x`` rather than NaN — a degenerate tangent is a sampling
    artefact, and a silent NaN would poison every frame downstream."""
    v = np.atleast_2d(np.asarray(vectors, dtype=float))
    norms = np.linalg.norm(v, axis=1)
    out = np.empty_like(v)
    bad = norms < _EPS
    out[~bad] = v[~bad] / norms[~bad, None]
    out[bad] = np.array([1.0, 0.0, 0.0])
    return out.reshape(np.shape(vectors))


def _chord_arc_length(points: np.ndarray) -> np.ndarray:
    """``(N,)`` cumulative chord length, ``s[0] == 0``."""
    if len(points) < 2:
        return np.zeros(len(points))
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
    return np.concatenate([[0.0], np.cumsum(steps)])


@dataclass(frozen=True)
class Path:
    """A sampled centre-line curve.

    Attributes
    ----------
    points:
        ``(N, 3)`` sample positions, caller units.
    tangents:
        ``(N, 3)`` unit tangents, one per sample, pointing along increasing
        ``s``.
    s:
        ``(N,)`` cumulative arc length of the sampled polyline, ``s[0] == 0``
        and non-decreasing. This is the kernel's curve parameter: every
        arc-length argument elsewhere (``max_seg_len``, the fibre rise,
        ``resample_arc_length``'s ``step``) is measured in it.
    """

    points: np.ndarray
    tangents: np.ndarray
    s: np.ndarray

    def __post_init__(self) -> None:
        pts = np.asarray(self.points, dtype=float)
        tan = np.asarray(self.tangents, dtype=float)
        arc = np.asarray(self.s, dtype=float)
        if pts.ndim != 2 or pts.shape[1] != 3:
            raise ValueError(f"Path.points must be (N, 3), got {pts.shape}")
        if tan.shape != pts.shape:
            raise ValueError(
                f"Path.tangents must match points {pts.shape}, got {tan.shape}"
            )
        if arc.shape != (pts.shape[0],):
            raise ValueError(f"Path.s must be ({pts.shape[0]},), got {arc.shape}")
        object.__setattr__(self, "points", pts)
        object.__setattr__(self, "tangents", tan)
        object.__setattr__(self, "s", arc)

    def __len__(self) -> int:
        return int(self.points.shape[0])

    @property
    def length(self) -> float:
        """Total arc length — ``s[-1]``, 0 for a path of fewer than 2 samples."""
        return float(self.s[-1]) if len(self) else 0.0


def polyline(points: np.ndarray, *, closed: bool = False) -> Path:
    """A :class:`Path` through ``points`` ``(N, 3)`` with tangents from finite
    differences: central inside, one-sided at an open end, wrapped when
    ``closed``.

    ``closed=True`` does *not* append a repeat of the first point — it only
    changes how the end tangents are estimated. A caller wanting a closed
    sample set (first point repeated last, e.g. to measure a frame holonomy
    around a loop) supplies it in ``points``.
    """
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"points must be (N, 3), got {pts.shape}")
    n = pts.shape[0]
    if n == 0:
        return Path(pts, pts.copy(), np.zeros(0))
    if n == 1:
        return Path(pts, np.array([[1.0, 0.0, 0.0]]), np.zeros(1))
    raw = np.empty_like(pts)
    raw[1:-1] = pts[2:] - pts[:-2]
    if closed:
        raw[0] = pts[1] - pts[-1]
        raw[-1] = pts[0] - pts[-2]
    else:
        raw[0] = pts[1] - pts[0]
        raw[-1] = pts[-1] - pts[-2]
    return Path(pts, _unit(raw), _chord_arc_length(pts))


def hermite(
    p0: np.ndarray,
    m0: np.ndarray,
    p1: np.ndarray,
    m1: np.ndarray,
    *,
    samples: int = 32,
) -> Path:
    """The cubic Hermite span from ``p0`` (tangent vector ``m0``) to ``p1``
    (tangent vector ``m1``), sampled at ``samples`` points inclusive of both
    ends.

    ``m0``/``m1`` are *velocity* vectors over the unit parameter interval,
    not unit directions — their magnitudes set how hard the curve is pulled
    along them. Tangents in the returned path are the analytic derivative,
    normalised.
    """
    if samples < 2:
        raise ValueError(f"hermite needs samples >= 2, got {samples}")
    a = np.asarray(p0, dtype=float)
    b = np.asarray(p1, dtype=float)
    v0 = np.asarray(m0, dtype=float)
    v1 = np.asarray(m1, dtype=float)
    u = np.linspace(0.0, 1.0, samples)[:, None]
    h00 = 2 * u**3 - 3 * u**2 + 1
    h10 = u**3 - 2 * u**2 + u
    h01 = -2 * u**3 + 3 * u**2
    h11 = u**3 - u**2
    pts = h00 * a + h10 * v0 + h01 * b + h11 * v1
    d00 = 6 * u**2 - 6 * u
    d10 = 3 * u**2 - 4 * u + 1
    d01 = -6 * u**2 + 6 * u
    d11 = 3 * u**2 - 2 * u
    der = d00 * a + d10 * v0 + d01 * b + d11 * v1
    return Path(pts, _unit(der), _chord_arc_length(pts))


def _catmull_rom_span(
    p0: np.ndarray,
    p1: np.ndarray,
    p2: np.ndarray,
    p3: np.ndarray,
    alpha: float,
) -> tuple[np.ndarray, np.ndarray]:
    """The Barry-Goldman tangents ``(m1, m2)`` for the span ``p1 -> p2`` of a
    non-uniform Catmull-Rom spline with knot exponent ``alpha`` (0 = uniform,
    0.5 = centripetal, 1 = chordal). Zero-length knot intervals (a repeated
    waypoint) fall back to the uniform tangent so a duplicate point cannot
    divide by zero."""
    t01 = float(np.linalg.norm(p1 - p0)) ** alpha
    t12 = float(np.linalg.norm(p2 - p1)) ** alpha
    t23 = float(np.linalg.norm(p3 - p2)) ** alpha
    if t01 < _EPS or t12 < _EPS or t23 < _EPS:
        return 0.5 * (p2 - p0), 0.5 * (p3 - p1)
    m1 = (p2 - p1) + t12 * ((p1 - p0) / t01 - (p2 - p0) / (t01 + t12))
    m2 = (p2 - p1) + t12 * ((p3 - p2) / t23 - (p3 - p1) / (t12 + t23))
    return m1, m2


def catmull_rom(
    waypoints: np.ndarray,
    *,
    closed: bool = False,
    samples_per_span: int = 16,
    alpha: float = 0.5,
) -> Path:
    """Interpolating cubic spline through every waypoint.

    ``waypoints`` is ``(W, 3)``, ``W >= 2``. The result has
    ``W * samples_per_span + 1`` samples when open and
    ``W * samples_per_span + 1`` when ``closed`` (the first waypoint is
    repeated as the last sample, so the sample set closes) — in both cases
    ``points[k * samples_per_span]`` is waypoint ``k`` *exactly*, which is
    the interpolation property callers rely on.

    ``alpha`` is the knot exponent: ``0.5`` (centripetal, the default) is the
    choice that cannot cusp or self-intersect within a span, which matters
    for a chain path that a clash pass will later trust.

    Open ends get phantom control points by linear extrapolation
    (``p_-1 = 2*p_0 - p_1``) — a deliberate choice over a
    zero-second-derivative natural end: it keeps the first span's tangent
    aligned with the first chord, which is what a helix leaving a lattice
    site wants.
    """
    wp = np.asarray(waypoints, dtype=float)
    if wp.ndim != 2 or wp.shape[1] != 3:
        raise ValueError(f"waypoints must be (W, 3), got {wp.shape}")
    w = wp.shape[0]
    if w < 2:
        raise ValueError(f"catmull_rom needs >= 2 waypoints, got {w}")
    if samples_per_span < 1:
        raise ValueError(f"samples_per_span must be >= 1, got {samples_per_span}")
    if closed:
        ctrl = np.concatenate([wp[-1:], wp, wp[:2]])
        n_spans = w
    else:
        ctrl = np.concatenate([[2 * wp[0] - wp[1]], wp, [2 * wp[-1] - wp[-2]]])
        n_spans = w - 1

    pieces: list[np.ndarray] = []
    tangent_pieces: list[np.ndarray] = []
    for k in range(n_spans):
        p0, p1, p2, p3 = ctrl[k], ctrl[k + 1], ctrl[k + 2], ctrl[k + 3]
        m1, m2 = _catmull_rom_span(p0, p1, p2, p3, alpha)
        span = hermite(p1, m1, p2, m2, samples=samples_per_span + 1)
        last = k == n_spans - 1
        pieces.append(span.points if last else span.points[:-1])
        tangent_pieces.append(span.tangents if last else span.tangents[:-1])
    pts = np.concatenate(pieces)
    tan = np.concatenate(tangent_pieces)
    return Path(pts, tan, _chord_arc_length(pts))


def sample_at(path: Path, s_query: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Linear interpolation of ``path`` at arc-length positions ``s_query``
    ``(M,)`` → ``(points (M, 3), tangents (M, 3))``.

    Positions outside ``[0, path.length]`` clamp to the ends. Points land on
    the sampled *polyline*, not on whatever smooth curve it approximates —
    the kernel never re-fits.
    """
    if len(path) == 0:
        raise ValueError("cannot sample an empty path")
    q = np.clip(np.atleast_1d(np.asarray(s_query, dtype=float)), 0.0, path.length)
    if len(path) == 1:
        return (
            np.repeat(path.points, len(q), axis=0),
            np.repeat(path.tangents, len(q), axis=0),
        )
    idx = np.clip(np.searchsorted(path.s, q, side="right") - 1, 0, len(path) - 2)
    seg = path.s[idx + 1] - path.s[idx]
    frac = np.where(seg > _EPS, (q - path.s[idx]) / np.where(seg > _EPS, seg, 1.0), 0.0)
    f = frac[:, None]
    pts = path.points[idx] * (1.0 - f) + path.points[idx + 1] * f
    tan = path.tangents[idx] * (1.0 - f) + path.tangents[idx + 1] * f
    return pts, _unit(tan)


def resample_arc_length(path: Path, step: float) -> Path:
    """Resample ``path`` at a uniform arc-length spacing close to ``step``.

    The spacing actually used is ``path.length / n`` with
    ``n = max(1, round(path.length / step))``, so both endpoints are hit
    exactly and the returned ``s`` is uniform to machine precision. That
    uniformity is in the *parameter*: consecutive returned points sit
    ``step`` apart along the polyline, and their straight-line distance is
    slightly less wherever a step straddles a vertex.

    Raises on a zero-length path or a non-positive ``step`` — silently
    returning one sample would hide a caller bug.
    """
    if step <= 0.0:
        raise ValueError(f"resample_arc_length needs step > 0, got {step}")
    total = path.length
    if total <= _EPS:
        raise ValueError(
            "cannot resample a path of zero arc length (all samples coincide)"
        )
    n = max(1, round(total / step))
    s_new = np.linspace(0.0, total, n + 1)
    pts, tan = sample_at(path, s_new)
    return Path(pts, tan, s_new)
