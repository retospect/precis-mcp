"""Axis calibration: spine + tick detection and the pixel→value map.

numpy only — no scipy, no I/O. Callers hand in a raster as an ``(H, W)``
grey or ``(H, W, 3)`` RGB array; tick *values* always arrive from the caller
or a checked proposal (OCR is out of scope, spec step 2), tick *positions*
are measured here.

Detection is run-length based: a tick mark is a contiguous dark run starting
one pixel outside the spine line, so band height never has to be guessed and
label text (which starts further out, after a gap) cannot masquerade as a
tick. The fit regresses pixel on transformed value (``px = a·t + b``, ``t``
the value or its ``log10``) — pixels are the noisy measured quantity — and
the residual is reported in px, the units the 0.5 px bar is written in.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "AxisError",
    "AxisFit",
    "AxisLines",
    "as_gray",
    "find_axis_lines",
    "fit_axis",
    "tick_positions",
]

Floats = NDArray[np.float64]

Scale = Literal["linear", "log"]
AxisName = Literal["x", "y"]


class AxisError(ValueError):
    """Calibration refusal: non-monotone values, degenerate fit, or a
    residual above the pixel bar."""


def as_gray(img: np.ndarray) -> Floats:
    """``(H, W)`` grey or ``(H, W, 3)``/``(H, W, 4)`` raster → float64 grey.

    Accepts uint8 (0-255) or float rasters; floats are returned on the same
    scale they arrive on, so a 0-1 float raster needs ``dark`` scaled too.
    """
    arr = np.asarray(img)
    if arr.ndim == 2:
        return arr.astype(np.float64)
    if arr.ndim == 3 and arr.shape[2] >= 3:
        rgb = arr[..., :3].astype(np.float64)
        return rgb @ np.array([0.299, 0.587, 0.114])
    raise AxisError(f"expected a grey or RGB raster, got shape {arr.shape}")


def _dark_runs(mask: NDArray[np.bool_], start: int, step: int) -> NDArray[np.int64]:
    """Contiguous True-run length along one axis, starting at ``start`` and
    moving by ``step`` (−1 or +1). ``mask`` is ``(N, L)``: N lines of length
    L; runs are measured along axis 0, one per column, returned as ``(L,)``.
    """
    n = mask.shape[0]
    rows = range(start, n) if step > 0 else range(start, -1, -1)
    out = np.zeros(mask.shape[1], dtype=np.int64)
    live = np.ones(mask.shape[1], dtype=np.bool_)
    for r in rows:
        live &= mask[r]
        out += live
    return out


def _max_runs(mask: NDArray[np.bool_]) -> NDArray[np.int64]:
    """Longest contiguous True run per row, along axis 1."""
    ar = np.arange(mask.shape[1])
    last_false = np.maximum.accumulate(np.where(mask, -1, ar), axis=1)
    return (ar - last_false).max(axis=1)


def _group_centres(pos: Floats, weight: Floats) -> list[float]:
    """Contiguous runs of an index array → their weight-averaged centres."""
    if pos.size == 0:
        return []
    cuts = np.flatnonzero(np.diff(pos) > 1)
    out = []
    for lo, hi in zip(
        np.concatenate([[0], cuts + 1]), np.concatenate([cuts + 1, [pos.size]])
    ):
        w = weight[lo:hi]
        out.append(float(np.average(pos[lo:hi], weights=w)))
    return out


@dataclass(frozen=True, slots=True)
class AxisLines:
    """Long straight dark lines in a raster: candidate spines.

    ``horizontal``/``vertical`` hold the row/column coordinate of each
    detected line (centre of the dark band), sorted. On a boxed axes the
    x spine is the bottom-most horizontal (``x_spine``) and the y spine the
    left-most vertical (``y_spine``); callers wanting a top/right axis pick
    from the lists directly.
    """

    horizontal: tuple[float, ...]
    vertical: tuple[float, ...]

    @property
    def x_spine(self) -> float:
        if not self.horizontal:
            raise AxisError("no horizontal axis line found")
        return self.horizontal[-1]

    @property
    def y_spine(self) -> float:
        if not self.vertical:
            raise AxisError("no vertical axis line found")
        return self.vertical[0]


def find_axis_lines(
    gray: np.ndarray,
    *,
    dark: float = 128.0,
    min_frac: float = 0.5,
) -> AxisLines:
    """Rows/columns whose contiguous dark run spans ≥ ``min_frac`` of the
    raster — the spine candidates.

    A plotted curve or a text baseline scatters dark pixels across a row but
    never reaches a half-width *contiguous* run; a spine does. Neighbouring
    qualifying rows (a 1-3 px anti-aliased spine) merge into one line at
    their dark-weighted centre.
    """
    g = as_gray(gray)
    h, w = g.shape
    mask = g < dark

    def lines(m: NDArray[np.bool_], length: int) -> tuple[float, ...]:
        need = min_frac * length
        runs = _max_runs(m)
        hits = np.flatnonzero(runs >= need).astype(np.float64)
        weight = runs[hits.astype(np.int64)].astype(np.float64)
        return tuple(_group_centres(hits, weight))

    return AxisLines(horizontal=lines(mask, w), vertical=lines(mask.T, h))


@dataclass(frozen=True, slots=True)
class Ticks:
    """Detected tick marks along one spine: centroid ``positions`` in pixel
    coordinates (sorted) and the contiguous dark ``runs`` that produced
    them — the tick length in px. Major ticks run longer than minor ones on
    log axes, so ``runs`` lets a caller filter ``positions`` (e.g.
    ``runs >= 0.7 * runs.max()``) when a proposal's labels count only the
    labelled majors."""

    positions: Floats
    runs: NDArray[np.int64]


def tick_positions(
    gray: np.ndarray,
    *,
    axis: AxisName,
    line: float,
    span: tuple[float, float] | None = None,
    side: Literal["out", "in", "auto"] = "auto",
    dark: float = 128.0,
    min_run: int = 3,
) -> Ticks:
    """Tick marks along one spine — positions plus their run lengths.

    For ``axis='x'`` (``line`` a spine row) ticks hang below it; for
    ``axis='y'`` (``line`` a spine column) they sit left of it — the
    matplotlib ``'out'`` default. ``side='in'`` scans inside the axes
    instead; ``'auto'`` tries out first, then in. A tick column is one whose
    contiguous dark run starting at the spine's outer edge is at least
    ``min_run`` long; adjacent tick columns merge to one centroid.

    ``span`` clips the search to the axes box — pass the perpendicular
    spine positions (for ``axis='x'``: the left and right vertical spines).
    It keeps tick *labels* on the other axis out: a label glyph centred on
    the spine row extends past it and otherwise reads exactly like a tick.
    """
    g = as_gray(gray)
    mask = g < dark
    if axis == "x":
        # Runs down rows, indexed by pixel x along the axis; ticks hang below.
        work, step_out = mask, 1
    else:
        # Runs along columns, indexed by pixel y; ticks sit left of the spine.
        work, step_out = mask.T, -1
    base = round(line)
    n = work.shape[0]

    def scan(step: int) -> tuple[Floats, NDArray[np.int64]]:
        start = base + step
        if start < 0 or start > n - 1:
            return np.empty(0), np.empty(0, dtype=np.int64)
        rl = _dark_runs(work, start, step)
        pos = np.flatnonzero(rl >= min_run).astype(np.float64)
        if pos.size == 0:
            return pos, np.empty(0, dtype=np.int64)
        centres, lens = [], []
        cuts = np.flatnonzero(np.diff(pos) > 1)
        for lo_i, hi_i in zip(
            np.concatenate([[0], cuts + 1]),
            np.concatenate([cuts + 1, [pos.size]]),
        ):
            seg = np.arange(lo_i, hi_i)
            w = rl[pos[seg].astype(np.int64)].astype(np.float64)
            centres.append(float(np.average(pos[seg], weights=w)))
            lens.append(int(w.max()))
        return np.array(centres), np.array(lens, dtype=np.int64)

    pos, lens = (
        scan(step_out)
        if side in ("out", "auto")
        else (np.empty(0), np.empty(0, dtype=np.int64))
    )
    if pos.size < 2 and side in ("in", "auto"):
        in_pos, in_lens = scan(-step_out)
        if side == "in" or in_pos.size > pos.size:
            pos, lens = in_pos, in_lens
    if span is not None and pos.size:
        lo, hi = span
        keep = (pos >= lo) & (pos <= hi)
        pos, lens = pos[keep], lens[keep]
    order = np.argsort(pos)
    return Ticks(positions=pos[order], runs=lens[order])


@dataclass(frozen=True, slots=True)
class AxisFit:
    """A fitted pixel→value map, ``px = a·t + b`` with ``t`` the value or
    its ``log10``.

    ``points`` are the accepted ``(px, value)`` calibration pairs in pixel
    order; ``residual_px`` is the worst-case pixel error of the regression
    (the quantity the 0.5 px bar applies to). ``decreasing`` records whether
    values fall as pixels rise (a reversed axis — declared via ``expect`` on
    :func:`fit_axis` and checked against the fit, never inferred).
    """

    scale: Scale
    a: float
    b: float
    points: tuple[tuple[float, float], ...]
    residual_px: float
    decreasing: bool

    def _transform(self, value: float) -> float:
        return math.log10(value) if self.scale == "log" else value

    def value_at(self, px: float) -> float:
        t = (px - self.b) / self.a
        return 10.0**t if self.scale == "log" else t

    def pixel_at(self, value: float) -> float:
        return self.a * self._transform(value) + self.b

    def scale_factor(self, px: float) -> float:
        """``|d value / d px|`` at pixel position — position-dependent on a
        log axis, constant on a linear one."""
        if self.scale == "log":
            return math.log(10.0) * self.value_at(px) / abs(self.a)
        return 1.0 / abs(self.a)

    def sigma_at(self, px: float) -> float:
        """Point sigma in data units (spec step 5): half-pixel quantisation
        and the calibration residual in quadrature, scaled by the local
        data-per-pixel."""
        return self.scale_factor(px) * math.hypot(0.5, self.residual_px)


def fit_axis(
    pixels: np.ndarray | Sequence[float],
    values: np.ndarray | Sequence[float],
    *,
    scale: Scale = "linear",
    expect: Literal["increasing", "decreasing"] | None = None,
    max_residual_px: float = 0.5,
) -> AxisFit:
    """Fit the pixel→value map from ``(pixel, value)`` calibration pairs.

    Values must be strictly monotone in pixel order (either direction);
    ``expect`` declares the direction when known — a fit that comes out the
    other way is refused, never silently accepted. ``scale='log'`` requires
    all-positive values and fits in log10 space; it is declared by the
    caller, never inferred. Refuses (:class:`AxisError`) on <2 points,
    non-monotone values, a degenerate (zero-slope) fit, or a worst-case
    residual above ``max_residual_px``.
    """
    px = np.asarray(pixels, dtype=np.float64).ravel()
    v = np.asarray(values, dtype=np.float64).ravel()
    if px.size != v.size:
        raise AxisError(f"{px.size} pixels vs {v.size} values")
    if px.size < 2:
        raise AxisError("need at least two calibration points")

    order = np.argsort(px)
    px, v = px[order], v[order]
    dv = np.diff(v)
    if np.any(dv == 0) or (np.any(dv > 0) and np.any(dv < 0)):
        raise AxisError("tick values are not strictly monotone along the axis")
    if np.any(~np.isfinite(px)) or np.any(~np.isfinite(v)):
        raise AxisError("non-finite calibration input")
    if scale == "log" and np.any(v <= 0):
        raise AxisError("log axis needs all-positive tick values")

    t = np.log10(v) if scale == "log" else v
    a, b = np.polyfit(t, px, 1)
    if not np.isfinite(a) or a == 0.0:
        raise AxisError("degenerate axis fit (zero pixel range)")
    residual = float(np.max(np.abs(px - (a * t + b))))
    if residual > max_residual_px:
        raise AxisError(
            f"axis fit residual {residual:.2f} px exceeds {max_residual_px} px"
        )
    decreasing = bool(a < 0)
    if expect == "increasing" and decreasing:
        raise AxisError("declared increasing axis fitted decreasing")
    if expect == "decreasing" and not decreasing:
        raise AxisError("declared decreasing axis fitted increasing")
    return AxisFit(
        scale=scale,
        a=float(a),
        b=float(b),
        points=tuple((float(p), float(val)) for p, val in zip(px, v)),
        residual_px=residual,
        decreasing=decreasing,
    )
