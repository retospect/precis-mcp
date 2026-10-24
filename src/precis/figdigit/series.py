"""Series extraction: colour masks → centroids / polyline → sub-pixel peak.

A series is found by colour, never by position: the caller names a swatch
(or the proposer's legend mapping names one — checked the same way) and
:func:`colour_assign` gives every pixel to its *nearest* target colour, so
several similar-coloured traces stay separable where tolerance-radius masks
would overlap. ``cluster_colours`` (k-means over non-grey pixels) is the
discovery path when nobody names the palette.

Geometry stays in pixel space — a log y axis would distort an apex fit done
in value space — and numbers cross into data units only through the fitted
:class:`~precis.figdigit.axis.AxisFit` maps, which also supply the
calibration terms of the reported sigma.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from precis.figdigit.axis import AxisFit

__all__ = [
    "Centroid",
    "Peak",
    "Series",
    "SeriesError",
    "SeriesPeak",
    "SeriesPoint",
    "cluster_colours",
    "colour_assign",
    "colour_mask",
    "dark_mask",
    "extract_series",
    "find_peak",
    "label_components",
    "local_extrema",
    "polyline",
]

Floats = NDArray[np.float64]
Bools = NDArray[np.bool_]


class SeriesError(ValueError):
    """Extraction refusal: empty mask, no extremum, degenerate peak fit."""


def _rgb(img: np.ndarray) -> Floats:
    arr = np.asarray(img, dtype=np.float64)
    if arr.ndim == 2:
        raise SeriesError("series extraction needs an RGB raster, got grey")
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise SeriesError(f"expected an (H, W, 3) raster, got {arr.shape}")
    return arr[..., :3]


def colour_mask(
    rgb: np.ndarray,
    colour: tuple[float, float, float],
    *,
    tol: float = 40.0,
) -> Bools:
    """Pixels within ``tol`` (Euclidean, 0-255 space) of ``colour``."""
    arr = _rgb(rgb)
    dist = np.sqrt(((arr - np.asarray(colour, dtype=np.float64)) ** 2).sum(axis=-1))
    return dist <= tol


def colour_assign(
    rgb: np.ndarray,
    colours: list[tuple[float, float, float]],
    *,
    tol: float = 60.0,
) -> list[Bools]:
    """One mask per target colour, disjoint: every pixel goes to its nearest
    target and is kept only when within ``tol``.

    Similar-coloured series (the TPD multi-rate figure case) separate here —
    where ``colour_mask`` radii would overlap, assignment still splits the
    anti-aliased blend pixels along the Voronoi boundary.
    """
    arr = _rgb(rgb)
    targets = np.asarray(colours, dtype=np.float64).reshape(-1, 3)
    if targets.shape[0] == 0:
        return []
    dist = np.sqrt(((arr[..., None, :] - targets) ** 2).sum(axis=-1))
    nearest = dist.argmin(axis=-1)
    close = dist.min(axis=-1) <= tol
    return [(nearest == i) & close for i in range(targets.shape[0])]


def cluster_colours(
    rgb: np.ndarray,
    k: int,
    *,
    seed: int = 0,
    min_chroma: float = 25.0,
    max_grey: float = 245.0,
) -> Floats:
    """k-means centroids over the non-grey pixels — palette discovery.

    Grey/near-white pixels (spines, text, background) are excluded by the
    chroma and brightness cuts before ``scipy.cluster.vq.kmeans2`` runs;
    the caller maps returned centroids to legend entries.
    """
    from scipy.cluster.vq import kmeans2

    arr = _rgb(rgb)
    flat = arr.reshape(-1, 3)
    chroma = flat.max(axis=1) - flat.min(axis=1)
    keep = flat[(chroma >= min_chroma) & (flat.max(axis=1) < max_grey)]
    if keep.shape[0] < k:
        raise SeriesError(f"only {keep.shape[0]} coloured pixels for {k} clusters")
    centroids, _labels = kmeans2(keep, k, seed=seed, minit="++")
    return np.asarray(centroids, dtype=np.float64)


def dark_mask(
    gray: np.ndarray,
    *,
    thresh: float = 128.0,
    exclude: list[tuple[int, int, int, int]] | None = None,
) -> Bools:
    """Black-on-white overlay mask (spec step 3): dark pixels minus the
    boxes the caller names — axes and text boxes found in step 2 are the
    usual exclusions, ``(row0, col0, row1, col1)`` each."""
    from precis.figdigit.axis import as_gray

    mask = as_gray(gray) < thresh
    for r0, c0, r1, c1 in exclude or []:
        mask[r0:r1, c0:c1] = False
    return mask


@dataclass(frozen=True, slots=True)
class Centroid:
    """One connected component's centre of mass in pixel coords."""

    x: float
    y: float
    size: int


def label_components(mask: np.ndarray, *, min_size: int = 4) -> list[Centroid]:
    """Marker centroids: 8-connected components of ``mask``, small junk
    (sub-``min_size`` blobs — anti-aliasing crumbs) dropped."""
    from scipy import ndimage

    labels, n = ndimage.label(
        np.asarray(mask, dtype=np.bool_), structure=np.ones((3, 3))
    )
    out: list[Centroid] = []
    for i in range(1, n + 1):
        ys, xs = np.nonzero(labels == i)
        if ys.size < min_size:
            continue
        out.append(Centroid(x=float(xs.mean()), y=float(ys.mean()), size=int(ys.size)))
    return out


def polyline(mask: np.ndarray) -> tuple[Floats, Floats]:
    """Per-column traced row of the mask → ``(xs, ys)`` pixel polyline.

    A column's mask pixels are split into contiguous row clumps; the curve
    point is the clump nearest the previous column's accepted row (the
    median of that clump, or the plain column median on the first column).
    Nearest-clump tracking is what keeps two same-ish-colour traces honest:
    where ``colour_assign`` lets a stray blend pixel of the neighbouring
    series through, a flat median lands between the clumps and the traced
    apex shifts — tracking follows the connected stroke instead. Columns
    with no mask pixel are skipped (gaps stay gaps — a broken curve is not
    interpolated across).
    """
    m = np.asarray(mask, dtype=np.bool_)
    cols = np.flatnonzero(m.any(axis=0))
    if cols.size == 0:
        raise SeriesError("empty mask — nothing to trace")
    xs = cols.astype(np.float64)
    out = np.empty(cols.size)
    prev: float | None = None
    for i, c in enumerate(cols):
        rows = np.flatnonzero(m[:, c])
        cuts = np.flatnonzero(np.diff(rows) > 1)
        bounds = np.concatenate([[0], cuts + 1, [rows.size]])
        clumps = [rows[a:b] for a, b in pairwise(bounds)]
        if prev is None:
            best = clumps[0]
        else:
            p = prev
            best = min(clumps, key=lambda g: abs(float(g.mean()) - p))
        prev = float(np.median(best))
        out[i] = prev
    return xs, out


def local_extrema(
    ys: np.ndarray, *, mode: Literal["min", "max"] = "min"
) -> NDArray[np.int64]:
    """Indices of strict local extrema of the polyline rows (interior
    points only — an endpoint is never an extremum)."""
    y = np.asarray(ys, dtype=np.float64)
    if y.size < 3:
        return np.empty(0, dtype=np.int64)
    d = np.diff(y)
    if mode == "min":
        idx = np.flatnonzero((d[:-1] < 0) & (d[1:] > 0)) + 1
    else:
        idx = np.flatnonzero((d[:-1] > 0) & (d[1:] < 0)) + 1
    return idx


@dataclass(frozen=True, slots=True)
class Peak:
    """A sub-pixel extremum of the polyline, in pixel space.

    ``position`` is the parabola vertex column (or the arg-extremum column
    when the fit degenerates — then ``sigma_px`` widens to cover the region
    rather than pretend precision); ``sigma_px`` is the vertex standard
    error from the fit covariance; ``offset_px`` the |vertex − arg-extremum|
    gap — a cheap asymmetry/bias proxy the caller folds into the peak
    sigma; ``fwhm_px`` the region width at half prominence; ``row`` the
    polyline's row at the vertex (for reporting).
    """

    position: float
    row: float
    sigma_px: float
    offset_px: float
    fwhm_px: float
    n_fit: int


def find_peak(
    xs: np.ndarray,
    ys: np.ndarray,
    *,
    mode: Literal["min", "max"] = "min",
) -> Peak:
    """Arg-extremum + parabolic sub-pixel fit over the half-prominence cap.

    ``mode='min'`` fits the apex of a *dip* in row value — the peak of a
    curve on a normal (value-up) y axis. The window is the contiguous region
    around the extremum lying inside half the prominence against a robust
    baseline (median of the polyline, valid when the peak occupies less than
    half the column span — the usual case for one dominant feature).
    """
    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)
    if x.size < 3:
        raise SeriesError("need at least three polyline points for a peak")
    sign = 1.0 if mode == "min" else -1.0
    s = sign * y  # extremum = minimum of s
    i0 = int(np.argmin(s))
    base = float(np.median(s))
    depth = base - s[i0]
    if depth <= 0:
        raise SeriesError("no extremum above the baseline")
    half = s[i0] + 0.5 * depth
    below = s <= half
    lo = i0
    while lo > 0 and below[lo - 1]:
        lo -= 1
    hi = i0
    while hi < x.size - 1 and below[hi + 1]:
        hi += 1
    if hi - lo + 1 < 3:
        lo, hi = max(i0 - 1, 0), min(i0 + 1, x.size - 1)
    fwhm = float(x[hi] - x[lo])
    idx = np.arange(lo, hi + 1)

    a, b, c = np.polyfit(x[idx], y[idx], 2)
    vertex = -b / (2 * a) if a != 0 else math.nan
    ok_shape = (mode == "min" and a > 0) or (mode == "max" and a < 0)
    if not (ok_shape and np.isfinite(vertex) and x[lo] <= vertex <= x[hi]):
        # degenerate apex: report the arg-extremum with the region width as
        # the honest uncertainty rather than a fabricated sub-pixel vertex
        return Peak(
            position=float(x[i0]),
            row=float(y[i0]),
            sigma_px=fwhm / 2.0,
            offset_px=0.0,
            fwhm_px=fwhm,
            n_fit=int(idx.size),
        )
    design = np.column_stack([x[idx] ** 2, x[idx], np.ones(idx.size)])
    resid = y[idx] - design @ np.array([a, b, c])
    # the fit residual covers curve noise; column-to-column jitter (mask /
    # tracking glitches the parabola can still fit through) shows up as
    # high-frequency wiggle — std of y minus its 3-pt median. Both go into
    # the vertex error budget, in quadrature.
    noise = float(resid.std()) if idx.size > 3 else 0.0
    if idx.size >= 3:
        med = np.array(
            [np.median(y[idx][max(k - 1, 0) : k + 2]) for k in range(idx.size)]
        )
        jitter = float((y[idx] - med).std())
        noise = math.hypot(noise, jitter)
    if noise > 0:
        cov = noise**2 * np.linalg.pinv(design.T @ design)
        grad = np.array([b / (2 * a * a), -1.0 / (2 * a), 0.0])
        var = float(grad @ cov @ grad)
        sigma = math.sqrt(max(var, 0.0))
    else:
        sigma = 0.0
    row_at = a * vertex * vertex + b * vertex + c
    return Peak(
        position=float(vertex),
        row=float(row_at),
        sigma_px=sigma,
        offset_px=abs(float(vertex) - float(x[i0])),
        fwhm_px=fwhm,
        n_fit=int(idx.size),
    )


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One extracted point in data units with its per-point sigma."""

    x: float
    y: float
    x_sigma: float
    y_sigma: float


@dataclass(frozen=True, slots=True)
class SeriesPeak:
    """A peak position in x-data units; ``dominant`` names the largest of
    the quadrature terms (``quantisation`` | ``calibration`` | ``vertex``)."""

    x: float
    sigma: float
    fwhm: float
    dominant: str


@dataclass(frozen=True, slots=True)
class Series:
    """One extracted series: pixel-space polyline (or marker centroids),
    data-space points, and any peaks. ``verified`` is set by the caller —
    extraction itself never asserts the proposal was right."""

    points_px: tuple[Floats, Floats]
    points: tuple[SeriesPoint, ...]
    peaks: tuple[SeriesPeak, ...] = ()
    centroids: tuple[Centroid, ...] = ()
    verified: bool = field(default=True)


def extract_series(
    rgb: np.ndarray,
    *,
    x_axis: AxisFit,
    y_axis: AxisFit,
    mask: np.ndarray | None = None,
    colour: tuple[float, float, float] | None = None,
    tol: float = 40.0,
    kind: Literal["curve", "markers"] = "curve",
    peak: bool = True,
    min_component: int = 4,
) -> Series:
    """Colour mask → polyline/centroids → data-space points and peak.

    Exactly one of ``mask`` / ``colour`` selects the pixels (``mask`` wins
    when both are given — the caller did the assign/legend work itself).
    ``kind='markers'`` returns connected-component centroids instead of a
    traced polyline. The peak mode follows the y map: a data maximum is a
    row *minimum* on a normal axis, a row maximum on a reversed one —
    ``peak=False`` skips peak fitting entirely.

    Sigmas follow spec step 5: half-pixel quantisation and the calibration
    residual in quadrature at the local data-per-pixel (via
    :meth:`AxisFit.sigma_at`), plus the vertex standard error for peaks;
    the dominant term is named on the result.
    """
    if mask is None:
        if colour is None:
            raise SeriesError("extract_series needs a mask or a colour")
        mask = colour_mask(rgb, colour, tol=tol)
    m = np.asarray(mask, dtype=np.bool_)

    if kind == "markers":
        cents = label_components(m, min_size=min_component)
        if not cents:
            raise SeriesError("no marker components in mask")
        xs = np.array([c.x for c in cents])
        ys = np.array([c.y for c in cents])
        order = np.argsort(xs)
        xs, ys = xs[order], ys[order]
        cents_t: tuple[Centroid, ...] = tuple(cents)
        peaks: tuple[SeriesPeak, ...] = ()
    else:
        xs, ys = polyline(m)
        cents_t = ()
        peaks = ()
        if peak and xs.size >= 3:
            mode: Literal["min", "max"] = "min" if y_axis.decreasing else "max"
            pk = find_peak(xs, ys, mode=mode)
            sf = x_axis.scale_factor(pk.position)
            quant = 0.5 * sf
            calib = x_axis.residual_px * sf
            # the vertex term carries the fit's own standard error plus the
            # vertex-vs-argextremum offset — an asymmetry/bias proxy that
            # catches noisy apices the clean-fit sigma under-covers
            vert = (pk.sigma_px + pk.offset_px) * sf
            terms = {"quantisation": quant, "calibration": calib, "vertex": vert}
            peaks = (
                SeriesPeak(
                    x=x_axis.value_at(pk.position),
                    sigma=math.hypot(quant, calib, vert),
                    fwhm=pk.fwhm_px * sf,
                    dominant=max(terms, key=lambda k: terms[k]),
                ),
            )

    points = tuple(
        SeriesPoint(
            x=x_axis.value_at(float(px)),
            y=y_axis.value_at(float(py)),
            x_sigma=x_axis.sigma_at(float(px)),
            y_sigma=y_axis.sigma_at(float(py)),
        )
        for px, py in zip(xs, ys)
    )
    return Series(points_px=(xs, ys), points=points, peaks=peaks, centroids=cents_t)
