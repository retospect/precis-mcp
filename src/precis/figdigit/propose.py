"""The checked-proposal seam: a vision model proposes, geometry disposes.

A :class:`Proposer` sees the crop and returns a :class:`Proposal` — axis
tick values, scales, series colours and rough peak positions. Every claim
is then checked against the deterministic geometry before it may reach a
table: tick values go through :func:`~precis.figdigit.axis.fit_axis`
(monotone + the 0.5 px residual bar), and a claimed peak must land within
3 px of a local extremum of the extracted polyline. Failures come back on
``DigitiseResult.unverified`` and produce no numbers.

The proposer is only consulted for what the caller did not already supply
— with every tick list and series colour named by the caller the proposer
is never invoked, which is what makes the offline geometry-only mode
(proposer=None, no network) a natural special case rather than a flag.

:class:`SonnetProposer` is the one real implementation: a single vision
call routed through ``precis.utils.llm.router`` (Tier.BIG — the
Sonnet-class rung, the only transport tier that attaches images), its JSON
reply parsed tolerantly via ``extract_json_object``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from precis.figdigit.axis import (
    AxisError,
    AxisFit,
    Scale,
    Ticks,
    as_gray,
    find_axis_lines,
    fit_axis,
    tick_positions,
)
from precis.figdigit.series import (
    Series,
    SeriesError,
    colour_assign,
    extract_series,
    local_extrema,
    polyline,
)

__all__ = [
    "AxisSpec",
    "DigitiseResult",
    "PeakClaim",
    "Proposal",
    "ProposeError",
    "Proposer",
    "SeriesResult",
    "SeriesSpec",
    "SonnetProposer",
    "Unverified",
    "VerifiedPeak",
    "digitise",
]

Floats = NDArray[np.float64]

#: Peak claims must sit within this many pixels of a real polyline
#: extremum (spec step 4).
PEAK_TOLERANCE_PX = 3.0


class ProposeError(RuntimeError):
    """The proposer itself failed (transport or unparseable reply) — the
    proposal's claims are absent, not unverified."""


@dataclass(frozen=True, slots=True)
class AxisSpec:
    """Proposal/caller input for one axis: declared scale and the tick
    values **in increasing pixel order** — left→right for x, top→bottom
    for y (so a normal y axis lists its values descending)."""

    values: tuple[float, ...]
    scale: Scale = "linear"


@dataclass(frozen=True, slots=True)
class SeriesSpec:
    """One series to extract, identified by its swatch colour (0-255 RGB)."""

    colour: tuple[float, float, float]
    label: str = ""
    kind: Literal["curve", "markers"] = "curve"


@dataclass(frozen=True, slots=True)
class PeakClaim:
    """A claimed peak: index into the series list + the x position in data
    units."""

    series: int
    x: float


@dataclass(frozen=True, slots=True)
class Proposal:
    """Everything a proposer (or the offline caller) asserts about a
    figure. Any field may be left out — ``digitise`` fills gaps from the
    caller and verifies whatever it ends up with."""

    x_axis: AxisSpec | None = None
    y_axis: AxisSpec | None = None
    series: tuple[SeriesSpec, ...] = ()
    peaks: tuple[PeakClaim, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Proposal:
        """Tolerant parse of the proposer's JSON reply."""
        axes = d.get("axes") or {}

        def axis(name: str) -> AxisSpec | None:
            a = axes.get(name)
            if not isinstance(a, dict):
                return None
            ticks = a.get("ticks")
            if not isinstance(ticks, list):
                return None
            scale = a.get("scale", "linear")
            return AxisSpec(
                values=tuple(float(v) for v in ticks),
                scale="log" if scale == "log" else "linear",
            )

        def colour(v: Any) -> tuple[float, float, float]:
            if isinstance(v, str) and v.startswith("#") and len(v) == 7:
                return (
                    float(int(v[1:3], 16)),
                    float(int(v[3:5], 16)),
                    float(int(v[5:7], 16)),
                )
            seq = list(v)
            return (float(seq[0]), float(seq[1]), float(seq[2]))

        series = tuple(
            SeriesSpec(
                colour=colour(s["colour"]),
                label=str(s.get("label") or ""),
                kind="markers" if s.get("kind") == "markers" else "curve",
            )
            for s in d.get("series") or []
            if isinstance(s, dict) and "colour" in s
        )
        peaks = tuple(
            PeakClaim(series=int(p["series"]), x=float(p["x"]))
            for p in d.get("peaks") or []
            if isinstance(p, dict) and "series" in p and "x" in p
        )
        return cls(x_axis=axis("x"), y_axis=axis("y"), series=series, peaks=peaks)


class Proposer(Protocol):
    """Sees the crop, returns a proposal. Implementations: SonnetProposer
    (real), a fake in tests."""

    def propose(self, png: bytes) -> Proposal: ...


_PROMPT = """\
You are calibrating a figure-digitisation pass on the attached raster of a
single plot panel. Report ONLY the JSON object below — no prose.

{
  "axes": {
    "x": {"scale": "linear"|"log", "ticks": [numbers, left to right]},
    "y": {"scale": "linear"|"log", "ticks": [numbers, TOP TO BOTTOM of the
         image — so on a normal plot the largest value comes first]}
  },
  "series": [{"label": "legend text", "colour": [r,g,b] 0-255,
              "kind": "curve"|"markers"}],
  "peaks": [{"series": <index into series>, "x": <data-x of the peak>}]
}

Rules: list every labelled tick and only labelled ticks (ignore unlabelled
minor ticks); scales are your best reading, never guess between linear and
log when unsure — omit the axis instead; series colours are the stroke
colours as you see them; peaks are the visible maxima of each series in
data units."""


class SonnetProposer:
    """The one real proposer: a Sonnet-class vision call through the LLM
    router seam. ~$0.05 a figure, capped by ``max_usd`` — spend follows
    ``docs/conventions/thresholds.md``."""

    def __init__(
        self,
        *,
        model: str | None = None,
        max_usd: float = 0.10,
        timeout_s: float = 120.0,
    ) -> None:
        self.model = model
        self.max_usd = max_usd
        self.timeout_s = timeout_s

    def propose(self, png: bytes) -> Proposal:
        from precis.utils.llm.json_reply import extract_json_object
        from precis.utils.llm.router import LlmImage, LlmRequest, Tier, route

        res = route(
            LlmRequest(
                tier=Tier.BIG,
                source="figdigit:propose",
                prompt=_PROMPT,
                images=(LlmImage(data=png),),
                model=self.model,
                max_usd=self.max_usd,
                timeout_s=self.timeout_s,
            )
        )
        if res.error:
            raise ProposeError(f"proposer call failed: {res.error}")
        try:
            data = res.data or extract_json_object(res.text)
        except Exception as exc:
            raise ProposeError(f"proposer reply is not a JSON object: {exc}") from exc
        if data is None:
            raise ProposeError("proposer reply carried no JSON object")
        return Proposal.from_dict(data)


@dataclass(frozen=True, slots=True)
class Unverified:
    """A claim that failed its pixel check — it names what was claimed and
    why it was refused, and produces no numbers."""

    claim: str
    detail: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class SeriesResult:
    """One requested series after extraction. ``verified`` is False when a
    missing/refused axis kept its data-space numbers from existing — the
    pixel-space polyline is still reported so a caller can see what was
    traced."""

    spec: SeriesSpec
    series: Series | None
    points_px: tuple[Floats, Floats] | None
    verified: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class VerifiedPeak:
    """A peak claim that passed: the measured position (the polyline
    extremum nearest the claim) with its sigma in data units."""

    claim: PeakClaim
    x: float
    sigma: float


@dataclass(frozen=True, slots=True)
class DigitiseResult:
    """The verified table plus the refused claims.

    ``x_axis``/``y_axis`` are the accepted fits (``None`` when refused or
    never supplied); ``unverified`` carries every claim that failed a
    check — those claims produced nothing."""

    x_axis: AxisFit | None
    y_axis: AxisFit | None
    series: tuple[SeriesResult, ...]
    verified_peaks: tuple[VerifiedPeak, ...]
    unverified: tuple[Unverified, ...]
    proposal: Proposal | None = field(default=None)


def _fit_declared(
    ticks: Ticks,
    spec: AxisSpec,
    *,
    axis: Literal["x", "y"],
    max_residual_px: float,
) -> AxisFit:
    """Align declared tick values to detected tick pixels and fit.

    A proposal that counts only the labelled majors while the raster shows
    minor ticks too is retried against the long-run (major) subset before
    the count mismatch is allowed to refuse the axis.
    """
    pos = ticks.positions
    if pos.size != len(spec.values) and pos.size:
        major = ticks.majors()
        if major.positions.size == len(spec.values):
            pos = major.positions
    if pos.size != len(spec.values):
        raise AxisError(
            f"{axis} axis: {len(spec.values)} tick labels vs "
            f"{pos.size} detected tick marks"
        )
    return fit_axis(pos, spec.values, scale=spec.scale, max_residual_px=max_residual_px)


def digitise(
    rgb: np.ndarray,
    *,
    proposer: Proposer | None = None,
    ticks: dict[str, Sequence[float]] | None = None,
    scales: dict[str, str] | None = None,
    series: Sequence[SeriesSpec] | None = None,
    peaks: Sequence[PeakClaim] | None = None,
    tol: float = 40.0,
    max_residual_px: float = 0.5,
) -> DigitiseResult:
    """Calibrate axes, extract series, and verify every claim.

    Caller-supplied ``ticks``/``scales``/``series`` win over the proposal
    per piece; the proposer runs only while some piece is still missing
    (never with all three supplied — the offline geometry-only mode needs
    no proposer and no network). Each tick list is verified against the
    detected tick pixels; each peak claim against the extracted polyline;
    anything failing lands on ``unverified`` and contributes nothing.
    """
    arr = np.asarray(rgb)
    gray = as_gray(arr)

    want_axes: tuple[Literal["x", "y"], ...] = ("x", "y")
    have = {
        "x_ticks": ticks is not None and ticks.get("x") is not None,
        "y_ticks": ticks is not None and ticks.get("y") is not None,
        "series": series is not None,
    }
    proposal: Proposal | None = None
    if proposer is not None and not all(have.values()):
        from precis.figdigit.page import png_bytes

        proposal = proposer.propose(png_bytes(arr))

    axis_in: dict[str, AxisSpec | None] = {}
    for name in want_axes:
        caller_vals = None if ticks is None else ticks.get(name)
        scale = (scales or {}).get(name)
        prop = (
            (proposal.x_axis if name == "x" else proposal.y_axis)
            if proposal is not None
            else None
        )
        if caller_vals is not None:
            s = scale or "linear"
            axis_in[name] = AxisSpec(
                values=tuple(float(v) for v in caller_vals),
                scale="log" if s == "log" else "linear",
            )
        elif prop is not None:
            s = scale or prop.scale
            axis_in[name] = AxisSpec(
                values=prop.values, scale="log" if s == "log" else "linear"
            )
        else:
            axis_in[name] = None

    series_specs = (
        tuple(series)
        if series is not None
        else (proposal.series if proposal is not None else ())
    )
    peak_claims = tuple(peaks or ()) + (proposal.peaks if proposal is not None else ())

    lines = find_axis_lines(gray)
    det: dict[str, Ticks] = {}
    if axis_in["x"] is not None or axis_in["y"] is not None:
        det = {
            "x": tick_positions(
                gray,
                axis="x",
                line=lines.x_spine,
                span=(lines.y_spine, lines.vertical[-1]),
            ),
            "y": tick_positions(
                gray,
                axis="y",
                line=lines.y_spine,
                span=(lines.horizontal[0], lines.x_spine),
            ),
        }

    fits: dict[str, AxisFit | None] = {"x": None, "y": None}
    unverified: list[Unverified] = []
    for name in want_axes:
        spec = axis_in[name]
        if spec is None:
            continue
        try:
            fits[name] = _fit_declared(
                det[name], spec, axis=name, max_residual_px=max_residual_px
            )
        except AxisError as exc:
            unverified.append(
                Unverified(
                    claim=f"{name}_ticks", detail=str(exc), value=list(spec.values)
                )
            )

    masks = (
        colour_assign(arr, [s.colour for s in series_specs], tol=max(tol, 25.0))
        if series_specs
        else []
    )
    series_out: list[SeriesResult] = []
    polylines: list[tuple[Floats, Floats] | None] = []
    for sspec, mask in zip(series_specs, masks):
        try:
            pl = polyline(mask) if sspec.kind == "curve" else None
        except SeriesError:
            pl = None
        polylines.append(pl)
        if fits["x"] is not None and fits["y"] is not None:
            try:
                extracted = extract_series(
                    arr,
                    x_axis=fits["x"],
                    y_axis=fits["y"],
                    mask=mask,
                    kind=sspec.kind,
                    peak=sspec.kind == "curve",
                )
                series_out.append(
                    SeriesResult(
                        spec=sspec, series=extracted, points_px=pl, verified=True
                    )
                )
                continue
            except SeriesError as exc:
                series_out.append(
                    SeriesResult(
                        spec=sspec,
                        series=None,
                        points_px=pl,
                        verified=False,
                        reason=str(exc),
                    )
                )
                continue
        missing = [n for n in want_axes if fits[n] is None]
        series_out.append(
            SeriesResult(
                spec=sspec,
                series=None,
                points_px=pl,
                verified=False,
                reason=f"axis uncalibrated: {', '.join(missing)}",
            )
        )

    verified_peaks: list[VerifiedPeak] = []
    for claim in peak_claims:
        if fits["x"] is None:
            unverified.append(
                Unverified(
                    claim="peak",
                    detail="x axis uncalibrated — claim uncheckable",
                    value={"series": claim.series, "x": claim.x},
                )
            )
            continue
        pl = polylines[claim.series] if 0 <= claim.series < len(polylines) else None
        if pl is None:
            unverified.append(
                Unverified(
                    claim="peak",
                    detail=f"no extracted polyline for series {claim.series}",
                    value={"series": claim.series, "x": claim.x},
                )
            )
            continue
        xs, ys = pl
        mode: Literal["min", "max"] = (
            "min" if (fits["y"] is None or fits["y"].decreasing) else "max"
        )
        extrema = local_extrema(ys, mode=mode)
        px = fits["x"].pixel_at(claim.x)
        dists = np.abs(xs[extrema] - px) if extrema.size else np.empty(0)
        if dists.size and float(dists.min()) <= PEAK_TOLERANCE_PX:
            measured_px = float(xs[extrema][int(np.argmin(dists))])
            verified_peaks.append(
                VerifiedPeak(
                    claim=claim,
                    x=fits["x"].value_at(measured_px),
                    sigma=fits["x"].sigma_at(measured_px),
                )
            )
        else:
            unverified.append(
                Unverified(
                    claim="peak",
                    detail=(
                        f"no local extremum within {PEAK_TOLERANCE_PX:.0f} px "
                        f"of claimed x={claim.x}"
                    ),
                    value={"series": claim.series, "x": claim.x},
                )
            )

    return DigitiseResult(
        x_axis=fits["x"],
        y_axis=fits["y"],
        series=tuple(series_out),
        verified_peaks=tuple(verified_peaks),
        unverified=tuple(unverified),
        proposal=proposal,
    )
