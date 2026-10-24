"""Slice-3 spec test: render + checked proposal (figdigit/page.py,
figdigit/propose.py).

A fake proposer's claims are checked against the geometry: a wrong tick
label and a hallucinated peak both come back ``unverified``; a true peak
claim is verified. The offline geometry-only mode (caller names colours
and tick values) needs no proposer and no network — no real vision model
is called anywhere in these tests.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg

from precis.figdigit.page import png_bytes, render_pdf
from precis.figdigit.propose import (
    AxisSpec,
    PeakClaim,
    Proposal,
    SeriesSpec,
    digitise,
)

XTICKS = [300.0, 400.0, 500.0, 600.0, 700.0]
YTICKS_TOPDOWN = [1.5, 1.0, 0.5, 0.0]  # increasing pixel order: top → bottom
T_P = 437.0
COLOUR = (31, 119, 180)


def _render():
    t = np.linspace(300.0, 700.0, 800)
    y = np.exp(-0.5 * ((t - T_P) / 12.0) ** 2) + 0.05
    fig, ax = plt.subplots(figsize=(4.5, 3.2), dpi=150)
    ax.plot(t, y, color="#1f77b4", lw=1.4)
    ax.set_xticks(XTICKS)
    ax.set_yticks([0.0, 0.5, 1.0, 1.5])
    ax.set_xlim(300, 700)
    ax.set_ylim(0, 1.6)
    fig.canvas.draw()
    assert isinstance(fig.canvas, FigureCanvasAgg)
    rgb = np.asarray(fig.canvas.buffer_rgba())[..., :3].copy()
    plt.close(fig)
    return rgb


class _FakeProposer:
    """Canned claims: right x ticks, one wrong y tick, the true colour,
    one real peak and one hallucinated on the flat flank."""

    def __init__(self):
        self.calls = 0

    def propose(self, png: bytes) -> Proposal:
        self.calls += 1
        return Proposal(
            x_axis=AxisSpec(values=tuple(XTICKS)),
            # one wrong label: 0.5 → 0.7 breaks the linear spacing
            y_axis=AxisSpec(values=(1.5, 1.0, 0.7, 0.0)),
            series=(SeriesSpec(colour=COLOUR, label="curve"),),
            peaks=(
                PeakClaim(series=0, x=T_P + 0.8),  # real, ~1 K off
                PeakClaim(series=0, x=415.0),  # hallucinated: steep flank
            ),
        )


class _ExplodingProposer:
    def propose(self, png: bytes) -> Proposal:  # pragma: no cover
        raise AssertionError("proposer must not be called")


def test_fake_proposer_bad_claims_unverified():
    rgb = _render()
    res = digitise(rgb, proposer=_FakeProposer())

    assert res.x_axis is not None  # correct x ticks verified
    assert res.y_axis is None  # the doctored label fails the residual bar
    claims = [u.claim for u in res.unverified]
    assert claims.count("y_ticks") == 1
    assert claims.count("peak") == 1  # the hallucination, not the real one
    bad = next(u for u in res.unverified if u.claim == "y_ticks")
    assert "residual" in bad.detail

    assert len(res.verified_peaks) == 1
    vp = res.verified_peaks[0]
    assert abs(vp.x - T_P) < 2.0  # measured position beats the claim's

    # y uncalibrated → the series reports its polyline but not as verified
    assert len(res.series) == 1
    assert res.series[0].verified is False
    assert res.series[0].points_px is not None


def test_offline_mode_needs_no_proposer():
    rgb = _render()
    res = digitise(
        rgb,
        proposer=_ExplodingProposer(),  # must never be invoked
        ticks={"x": XTICKS, "y": YTICKS_TOPDOWN},
        series=[SeriesSpec(colour=COLOUR)],
        peaks=[PeakClaim(series=0, x=T_P)],
    )
    assert res.x_axis is not None and res.y_axis is not None
    assert res.unverified == ()
    assert res.series[0].verified is True
    assert res.series[0].series is not None
    peak = res.series[0].series.peaks[0]
    assert abs(peak.x - T_P) <= peak.sigma
    assert len(res.verified_peaks) == 1
    assert abs(res.verified_peaks[0].x - T_P) <= res.verified_peaks[0].sigma


def test_missing_proposal_pieces_left_uncalibrated():
    rgb = _render()
    res = digitise(rgb)  # no proposer, nothing supplied
    assert res.x_axis is None and res.y_axis is None
    assert res.series == ()
    assert res.unverified == ()


def test_render_pdf_roundtrip(tmp_path):
    pytest.importorskip("fitz", reason="pymupdf is the [paper] extra")
    t = np.linspace(0, 1, 50)
    fig, ax = plt.subplots(figsize=(4.0, 3.0), dpi=100)
    ax.plot(t, t**2)
    pdf = tmp_path / "fig.pdf"
    fig.savefig(pdf, format="pdf")
    plt.close(fig)
    rgb = render_pdf(pdf, page=0, dpi=150)
    assert rgb.ndim == 3 and rgb.shape[2] == 3
    h, w = rgb.shape[:2]
    assert h > 200 and w > 300  # ~150 dpi of a letter-ish page
    assert png_bytes(rgb).startswith(b"\x89PNG")
