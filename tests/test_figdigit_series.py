"""Slice-2 spec test: series extraction (figdigit/series.py).

Synthetic TPD-like traces — Gaussian peaks at known T_p, four heating
rates, noise, similar colours — must recover T_p within the reported sigma
in at least 95 percent of seeded trials. numpy colour masking (nearest
swatch assignment) is timed per trial; the opencv ``digitise`` extra is
only added if this proves too slow or brittle.
"""

from __future__ import annotations

import time

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg

from precis.figdigit.axis import find_axis_lines, fit_axis, tick_positions
from precis.figdigit.series import (
    colour_assign,
    extract_series,
    label_components,
    local_extrema,
)

XTICKS = np.array([300.0, 400.0, 500.0, 600.0, 700.0])
YTICKS = np.array([0.0, 0.5, 1.0, 1.5])
# four rates, deliberately similar blues (ColorBrewer): ~40-60 RGB units
# apart — inside one another's tolerance radius, so only nearest-swatch
# assignment keeps them apart
COLOURS = ["#08519c", "#2171b5", "#4292c6", "#6baed6"]
T_P = np.array([418.0, 437.0, 456.0, 478.0])
SEEDS = range(25)


def _rgb(hexes):
    return [(int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)) for h in hexes]


def _render_tpd(seed: int, noise: float = 0.035):
    rng = np.random.default_rng(seed)
    t = np.linspace(300.0, 700.0, 800)
    fig, ax = plt.subplots(figsize=(4.5, 3.2), dpi=150)
    for tp, col, i in zip(T_P, COLOURS, range(4)):
        width = 9.0 + 2.0 * i
        amp = 0.9 + 0.15 * i
        y = amp * np.exp(-0.5 * ((t - tp) / width) ** 2)
        y += 0.05 * i + rng.normal(0.0, noise, t.size)
        ax.plot(t, y, color=col, lw=1.4)
    ax.set_xticks(XTICKS)
    ax.set_yticks(YTICKS)
    ax.set_xlim(300, 700)
    ax.set_ylim(0, 1.6)
    fig.canvas.draw()
    assert isinstance(fig.canvas, FigureCanvasAgg)
    rgb = np.asarray(fig.canvas.buffer_rgba())[..., :3].copy()
    plt.close(fig)
    return rgb


def _calibrate(rgb):
    from precis.figdigit.axis import as_gray

    gray = as_gray(rgb)
    lines = find_axis_lines(gray)
    det_x = tick_positions(
        gray, axis="x", line=lines.x_spine, span=(lines.y_spine, lines.vertical[-1])
    ).positions
    det_y = tick_positions(
        gray, axis="y", line=lines.y_spine, span=(lines.horizontal[0], lines.x_spine)
    ).positions
    return (
        fit_axis(det_x, XTICKS),
        fit_axis(det_y, YTICKS[::-1]),
    )


def test_tpd_peaks_within_sigma_95pct():
    covered = 0
    total = 0
    mask_seconds = 0.0
    worst = 0.0
    for seed in SEEDS:
        rgb = _render_tpd(seed)
        x_axis, y_axis = _calibrate(rgb)
        t0 = time.perf_counter()
        masks = colour_assign(rgb, _rgb(COLOURS), tol=38.0)
        mask_seconds += time.perf_counter() - t0
        for mask, tp in zip(masks, T_P):
            series = extract_series(
                rgb, x_axis=x_axis, y_axis=y_axis, mask=mask, tol=38.0
            )
            assert len(series.peaks) == 1
            peak = series.peaks[0]
            err = abs(peak.x - tp)
            worst = max(worst, err / peak.sigma)
            total += 1
            if err <= peak.sigma:
                covered += 1
    frac = covered / total
    # spec bar: inside the reported sigma in >=95% of seeded trials
    assert frac >= 0.95, (
        f"coverage {frac:.2%} over {total} trials "
        f"(worst error {worst:.2f}σ, colour masking {mask_seconds:.2f}s total)"
    )


def test_marker_centroids():
    rgb = np.full((60, 60, 3), 255, dtype=np.uint8)
    for cx, cy in [(10, 15), (30, 40), (50, 22)]:
        rgb[cy - 2 : cy + 3, cx - 2 : cx + 3] = (200, 30, 30)
    mask = colour_assign(rgb, [(200, 30, 30)], tol=80.0)[0]
    cents = label_components(mask)
    assert len(cents) == 3
    got = sorted((round(c.x), round(c.y)) for c in cents)
    assert got == [(10, 15), (30, 40), (50, 22)]


def test_local_extrema_finds_peak_region():
    xs = np.arange(50.0)
    ys = np.abs(xs - 25.0) + 5  # dip at x=25 (a peak on a normal y axis)
    idx = local_extrema(ys, mode="min")
    assert idx.tolist() == [25]
