"""Figure digitiser — calibrated, uncertainty-bearing numbers off a plotted series.

``docs/backlog/figure-digitise.md`` (gr514185): papers print results only as
curves; precis can quote a caption but cannot turn the picture into numbers.
The design rule is *deterministic geometry measures, a vision model only
proposes* — nothing a model says becomes a number without passing a pixel
check.

- :mod:`precis.figdigit.axis` — axis calibration: spine + tick detection on
  the raster and the pixel→value map fit (linear or declared log; reversed
  axes are declared and checked, never inferred). numpy only.
- :mod:`precis.figdigit.series` — series extraction: colour masks
  (nearest-swatch assignment or k-means discovery), marker centroids,
  per-column polyline, sub-pixel peak with a noise-derived sigma.
- :mod:`precis.figdigit.page` — input raster: image file via PIL, or a PDF
  page clipped to a bbox rendered with pymupdf (``[paper]`` extra, lazy).
- :mod:`precis.figdigit.propose` — the checked-proposal seam: a
  :class:`~precis.figdigit.propose.Proposer` protocol, a Sonnet vision
  implementation routed through ``precis.utils.llm.router``, and the
  :func:`~precis.figdigit.propose.digitise` orchestrator that verifies every
  claim against the geometry before it may reach a table. An offline
  geometry-only mode takes caller-supplied tick values and series colours
  and needs no proposer and no network.

Uncertainty (spec step 5) is carried in data units, in quadrature:
half-pixel quantisation, the calibration residual, and for peaks the
sub-pixel vertex error of the parabolic fit — each result names the
dominant term.
"""

from __future__ import annotations

from precis.figdigit.axis import (
    AxisError,
    AxisFit,
    AxisLines,
    Ticks,
    as_gray,
    find_axis_lines,
    fit_axis,
    tick_positions,
)
from precis.figdigit.page import load_image, png_bytes, render_pdf
from precis.figdigit.propose import (
    AxisSpec,
    DigitiseResult,
    PeakClaim,
    Proposal,
    Proposer,
    SeriesSpec,
    SonnetProposer,
    Unverified,
    VerifiedPeak,
    digitise,
)
from precis.figdigit.series import (
    Series,
    SeriesError,
    SeriesPeak,
    SeriesPoint,
    colour_assign,
    colour_mask,
    dark_mask,
    extract_series,
    find_peak,
    label_components,
    polyline,
)

__all__ = [
    "AxisError",
    "AxisFit",
    "AxisLines",
    "AxisSpec",
    "DigitiseResult",
    "PeakClaim",
    "Proposal",
    "Proposer",
    "Series",
    "SeriesError",
    "SeriesPeak",
    "SeriesPoint",
    "SeriesSpec",
    "SonnetProposer",
    "Ticks",
    "Unverified",
    "VerifiedPeak",
    "as_gray",
    "colour_assign",
    "colour_mask",
    "dark_mask",
    "digitise",
    "extract_series",
    "find_axis_lines",
    "find_peak",
    "fit_axis",
    "label_components",
    "load_image",
    "png_bytes",
    "polyline",
    "render_pdf",
    "tick_positions",
]
