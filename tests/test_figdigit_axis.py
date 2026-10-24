"""Slice-1 spec test: calibration core (figdigit/axis.py).

A synthetic matplotlib figure with known ticks must recover the pixel→value
map within 0.5 px; non-monotone tick labels are refused. Tick *positions*
are detected on the raster; the *values* are supplied by the caller (the
proposer/OCR seam arrives in slice 3).
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg

from precis.figdigit.axis import (
    AxisError,
    as_gray,
    find_axis_lines,
    fit_axis,
    tick_positions,
)


def _render(xticks, yticks, *, ylog=False, dpi=150):
    """A boxed line plot; returns (rgb, true_xtick_px, true_ytick_px)."""
    fig, ax = plt.subplots(figsize=(4.0, 3.0), dpi=dpi)
    xs = np.linspace(xticks[0], xticks[-1], 200)
    ys = np.linspace(yticks[0], yticks[-1], 200)
    ax.plot(xs, ys)
    ax.set_xticks(xticks)
    ax.set_yticks(yticks)
    ax.set_xlim(xticks[0], xticks[-1])
    if ylog:
        ax.set_yscale("log")
        ax.set_ylim(yticks[0], yticks[-1])
    else:
        ax.set_ylim(yticks[0], yticks[-1])
    fig.canvas.draw()
    assert isinstance(fig.canvas, FigureCanvasAgg)
    rgba = np.asarray(fig.canvas.buffer_rgba())
    rgb = rgba[..., :3].copy()
    h, _w = rgb.shape[:2]
    # display coords (origin bottom-left) → pixel coords (origin top-left)
    x_disp = ax.transData.transform(
        np.column_stack([xticks, np.full(len(xticks), yticks[0])])
    )[:, 0]
    y_disp = ax.transData.transform(
        np.column_stack([np.full(len(yticks), xticks[0]), yticks])
    )[:, 1]
    plt.close(fig)
    return rgb, x_disp, h - y_disp


def test_linear_axes_recover_map_within_half_px():
    xticks = np.array([0.0, 20, 40, 60, 80, 100])
    yticks = np.array([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    rgb, true_x, true_y = _render(xticks, yticks)
    gray = as_gray(rgb)

    lines = find_axis_lines(gray)
    assert len(lines.horizontal) >= 2  # boxed: top + bottom spines
    assert len(lines.vertical) >= 2

    det_x = tick_positions(
        gray, axis="x", line=lines.x_spine, span=(lines.y_spine, lines.vertical[-1])
    ).positions
    det_y = tick_positions(
        gray, axis="y", line=lines.y_spine, span=(lines.horizontal[0], lines.x_spine)
    ).positions
    assert det_x.size == xticks.size
    assert det_y.size == yticks.size
    # detection itself lands within ~1.5 px of the true mark; det_y runs
    # top→bottom while true_y is ordered by increasing value
    assert np.max(np.abs(det_x - true_x)) < 1.5
    assert np.max(np.abs(det_y - true_y[::-1])) < 1.5

    fx = fit_axis(det_x, xticks)
    fy = fit_axis(det_y, yticks[::-1])
    assert fx.residual_px <= 0.5
    assert fy.residual_px <= 0.5
    # the fitted map reproduces the true display positions within ~1.5 px
    # (detection quantisation rides on top of the 0.5 px fit residual)
    assert np.max(np.abs([fx.pixel_at(v) for v in xticks] - true_x)) < 1.5
    assert np.max(np.abs([fy.pixel_at(v) for v in yticks] - true_y)) < 1.5
    # y runs downward in pixels → decreasing map; x is increasing
    assert fy.decreasing and not fx.decreasing
    # round-trip on a mid-range point
    assert abs(fx.value_at(fx.pixel_at(55.0)) - 55.0) < 1e-9


def test_log_axis_recovers_map():
    xticks = np.array([0.0, 25, 50, 75, 100])
    yticks = np.array([1.0, 10.0, 100.0])
    rgb, _tx, true_y = _render(xticks, yticks, ylog=True)
    gray = as_gray(rgb)
    lines = find_axis_lines(gray)
    ticks = tick_positions(
        gray, axis="y", line=lines.y_spine, span=(lines.horizontal[0], lines.x_spine)
    )
    # a log axis draws unlabelled minor ticks; the run length separates the
    # labelled majors (3.5 pt) from the minors (2 pt)
    major = ticks.runs >= 0.7 * ticks.runs.max()
    det_y = ticks.positions[major]
    assert det_y.size == yticks.size
    fy = fit_axis(det_y, yticks[::-1], scale="log")
    assert fy.residual_px <= 0.5
    assert np.max(np.abs([fy.pixel_at(v) for v in yticks] - true_y)) < 1.5
    # a linear read of a log axis is refused by the residual bar
    with pytest.raises(AxisError):
        fit_axis(det_y, yticks[::-1], scale="linear")


def test_non_monotone_tick_labels_refused():
    px = [10.0, 30.0, 50.0, 70.0]
    with pytest.raises(AxisError, match="monotone"):
        fit_axis(px, [0.0, 10.0, 5.0, 20.0])
    with pytest.raises(AxisError, match="monotone"):
        fit_axis(px, [0.0, 5.0, 5.0, 15.0])  # repeated label


def test_bad_spacing_refused_over_half_px():
    # monotone but geometrically inconsistent: residual 5 px
    with pytest.raises(AxisError, match="residual"):
        fit_axis([0.0, 10.0, 30.0], [0.0, 1.0, 2.0])


def test_declared_direction_is_checked():
    with pytest.raises(AxisError, match="increasing"):
        fit_axis([0.0, 10.0, 20.0], [3.0, 2.0, 1.0], expect="increasing")
    fit = fit_axis([0.0, 10.0, 20.0], [3.0, 2.0, 1.0], expect="decreasing")
    assert fit.decreasing


def test_log_axis_refuses_nonpositive():
    with pytest.raises(AxisError, match="positive"):
        fit_axis([0.0, 10.0, 20.0], [-1.0, 1.0, 10.0], scale="log")
