"""PlotHandler — declarative matplotlib renderer (Phase 5b).

Stateless compute kind.  Accepts a JSON plot spec via
``put(id='plot:', text='<json>', mode='render')`` and returns either an
inline PNG data URL (default), an inline SVG / WebP, or — when the
id carries an ``export`` suffix — writes the image to ``./figures/``
and returns a short confirmation string.

No persistence, no code execution: the spec is pure data and matplotlib
calls are driven by a validated pydantic schema (``line``, ``scatter``,
``bar``, ``hist``, ``errorbar``).  For anything outside that vocabulary
the caller falls back to ``calc:`` for numerics or, later, a dedicated
``pyeval-mcp`` package for free-form Python.

URI surface
-----------

The scheme is registered as *opaque-path* (see
``precis.uri._OPAQUE_PATH_SCHEMES``) so export filenames with ``/`` or
``.`` pass through untouched.  The handler parses the path itself:

* ``plot:``                     → inline PNG   (default)
* ``plot:/svg``                 → inline SVG
* ``plot:/webp``                → inline WebP
* ``plot:/help``                → onboarding skill pointer
* ``plot:/export``              → write ``./figures/plot-<hash>.png``
* ``plot:/export/<filename>``   → write ``./figures/<filename>``,
                                  format inferred from extension
                                  (``.png`` / ``.svg`` / ``.pdf`` /
                                  ``.webp`` / ``.jpg``)

Writes are restricted to the ``./figures/`` directory (relative to
CWD); absolute paths and parent-dir traversal (``..``) are rejected.

Safety
------

The spec is parsed via a strict pydantic discriminated union with
``extra='forbid'``.  No arbitrary kwargs pass through to matplotlib —
every visual choice maps to an explicit schema field.  Numeric arrays
are bounded (``_MAX_DATA_POINTS``) to guard against accidental
denial-of-service through huge payloads.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
from pathlib import Path
from typing import Any, ClassVar, Literal

from precis.protocol import ErrorCode, Handler, PrecisError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_INLINE_FORMATS: frozenset[str] = frozenset({"png", "svg", "webp"})
_EXPORT_FORMATS: frozenset[str] = frozenset({"png", "svg", "pdf", "webp", "jpg", "jpeg"})
_FORMAT_MIME: dict[str, str] = {
    "png": "image/png",
    "svg": "image/svg+xml",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

_DEFAULT_EXPORT_DIR = "figures"

#: Soft cap on how many points a single series may carry.  matplotlib
#: happily renders millions but the JSON payload + base64 round-trip
#: turns into a DOS vector above this.  Export path uses the same cap.
_MAX_DATA_POINTS = 50_000

#: Soft cap on the inline payload after base64 encoding.  Above this
#: the handler refuses the inline render and points the caller at
#: ``/export`` instead.  PDF never returns inline regardless.
_MAX_INLINE_BYTES = 2 * 1024 * 1024  # 2 MB

#: Short-hash length used for auto-generated export filenames.
_HASH_LEN = 8

_FOOTER = (
    "---\n"
    "_Rendered locally by matplotlib {version} — no network, no cost. "
    "Verify values before citing._"
)


# ---------------------------------------------------------------------------
# Path parsing
# ---------------------------------------------------------------------------


def _parse_plot_path(raw: str) -> tuple[str, str | None, Path | None]:
    """Split a ``plot:`` opaque path into ``(format, view, export_path)``.

    ``view`` is either ``"help"`` (for ``/help``) or ``None``.  The
    caller short-circuits help before looking at format / export_path.

    Examples::

        >>> _parse_plot_path("")            # plot:
        ('png', None, None)
        >>> _parse_plot_path("/svg")
        ('svg', None, None)
        >>> _parse_plot_path("/help")
        ('png', 'help', None)
        >>> _parse_plot_path("/export")
        ('png', None, PosixPath('figures/plot-<hash>.png'))  # hash varies
        >>> _parse_plot_path("/export/arrhenius.pdf")
        ('pdf', None, PosixPath('figures/arrhenius.pdf'))
    """
    p = (raw or "").lstrip("/")

    if not p:
        return "png", None, None
    if p == "help":
        return "png", "help", None
    if p in _INLINE_FORMATS:
        return p, None, None
    if p == "export":
        # Auto-generated filename — caller didn't specify.  The hash is
        # a placeholder; the handler replaces it with a content-hash of
        # the spec before rendering.
        return "png", None, Path(_DEFAULT_EXPORT_DIR) / "plot-AUTO.png"
    if p.startswith("export/"):
        rel = p[len("export/") :]
        if not rel:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="plot: /export/ requires a filename (e.g. /export/chart.svg)",
                next="put(id='plot:/export/chart.png', text='<spec>', mode='render')",
            )
        rel_path = Path(rel)
        if rel_path.is_absolute():
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"plot: absolute export paths not allowed ({rel!r})",
                next="use a relative filename — writes land under ./figures/",
            )
        if ".." in rel_path.parts:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"plot: '..' not allowed in export path ({rel!r})",
                next="use a relative filename — writes land under ./figures/",
            )
        ext = rel_path.suffix.lstrip(".").lower()
        if ext not in _EXPORT_FORMATS:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"plot: unsupported export format {ext!r}",
                options=sorted(_EXPORT_FORMATS),
                next="choose one of: " + ", ".join(sorted(_EXPORT_FORMATS)),
            )
        return ext, None, Path(_DEFAULT_EXPORT_DIR) / rel_path
    raise PrecisError(
        ErrorCode.PARAM_INVALID,
        cause=f"plot: unknown path suffix {raw!r}",
        options=["/svg", "/webp", "/help", "/export", "/export/<name>.<ext>"],
        next="see get(id='skill:plot-basics')",
    )


# ---------------------------------------------------------------------------
# Spec schema (pydantic)
# ---------------------------------------------------------------------------


def _spec_models() -> Any:
    """Build the pydantic model classes lazily.

    Imported inside a function so precis still starts without pydantic
    installed — the handler is gated on ImportError at registration
    time.  Returns the ``PlotSpec`` discriminated-union adapter.
    """
    from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

    _Style = Literal["solid", "dashed", "dotted", "dashdot"]
    _Marker = Literal["", "o", "s", "^", "v", "D", "x", "+", "*", ".", "1", "2"]

    class _Annotation(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: float
        y: float
        text: str = ""

    class _HLine(BaseModel):
        model_config = ConfigDict(extra="forbid")
        y: float
        label: str = ""
        style: _Style = "dashed"

    class _VLine(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: float
        label: str = ""
        style: _Style = "dashed"

    class _FitConfig(BaseModel):
        model_config = ConfigDict(extra="forbid")
        kind: Literal["linear", "log", "exp", "arrhenius"] = "linear"
        report: bool = True

    class _Series(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: list[float] = Field(default_factory=list)
        y: list[float] = Field(default_factory=list)
        label: str = ""
        style: _Style = "solid"
        marker: _Marker = ""

        @field_validator("x", "y")
        @classmethod
        def _cap_points(cls, v: list[float]) -> list[float]:
            if len(v) > _MAX_DATA_POINTS:
                raise ValueError(
                    f"array length {len(v)} exceeds cap {_MAX_DATA_POINTS}"
                )
            return v

    class _BarSeries(BaseModel):
        model_config = ConfigDict(extra="forbid")
        label: str = ""
        values: list[float]

        @field_validator("values")
        @classmethod
        def _cap_points(cls, v: list[float]) -> list[float]:
            if len(v) > _MAX_DATA_POINTS:
                raise ValueError(
                    f"array length {len(v)} exceeds cap {_MAX_DATA_POINTS}"
                )
            return v

    class _BaseSpec(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = ""
        xlabel: str = ""
        ylabel: str = ""
        xscale: Literal["linear", "log"] = "linear"
        yscale: Literal["linear", "log"] = "linear"
        xlim: tuple[float, float] | None = None
        ylim: tuple[float, float] | None = None
        grid: bool = True
        legend: bool = True
        figsize: tuple[float, float] = (6.0, 4.0)
        dpi: int = Field(default=100, ge=50, le=600)
        palette: Literal[
            "default", "tab10", "viridis", "plasma", "grayscale", "colorblind"
        ] = "default"
        annotate: list[_Annotation] = Field(default_factory=list)
        hline: list[_HLine] = Field(default_factory=list)
        vline: list[_VLine] = Field(default_factory=list)

        @field_validator("figsize")
        @classmethod
        def _check_figsize(cls, v: tuple[float, float]) -> tuple[float, float]:
            w, h = v
            if not (1.0 <= w <= 20.0 and 1.0 <= h <= 20.0):
                raise ValueError(
                    f"figsize must be between 1 and 20 inches, got {v}"
                )
            return v

    class LineSpec(_BaseSpec):
        type: Literal["line"]
        x: list[float] | None = None
        y: list[float] | None = None
        label: str = ""
        style: _Style = "solid"
        marker: _Marker = ""
        series: list[_Series] = Field(default_factory=list)
        fit: _FitConfig | None = None

        @field_validator("x", "y")
        @classmethod
        def _cap_points(cls, v: list[float] | None) -> list[float] | None:
            if v is not None and len(v) > _MAX_DATA_POINTS:
                raise ValueError(
                    f"array length {len(v)} exceeds cap {_MAX_DATA_POINTS}"
                )
            return v

    class ScatterSpec(_BaseSpec):
        type: Literal["scatter"]
        x: list[float] | None = None
        y: list[float] | None = None
        label: str = ""
        marker: _Marker = "o"
        series: list[_Series] = Field(default_factory=list)
        fit: _FitConfig | None = None

        @field_validator("x", "y")
        @classmethod
        def _cap_points(cls, v: list[float] | None) -> list[float] | None:
            if v is not None and len(v) > _MAX_DATA_POINTS:
                raise ValueError(
                    f"array length {len(v)} exceeds cap {_MAX_DATA_POINTS}"
                )
            return v

    class BarSpec(_BaseSpec):
        type: Literal["bar"]
        labels: list[str]
        values: list[float] | None = None
        series: list[_BarSeries] = Field(default_factory=list)
        horizontal: bool = False

    class HistSpec(_BaseSpec):
        type: Literal["hist"]
        values: list[float]
        bins: int | list[float] = 30
        label: str = ""

        @field_validator("values")
        @classmethod
        def _cap_points(cls, v: list[float]) -> list[float]:
            if len(v) > _MAX_DATA_POINTS:
                raise ValueError(
                    f"array length {len(v)} exceeds cap {_MAX_DATA_POINTS}"
                )
            return v

    class ErrorbarSpec(_BaseSpec):
        type: Literal["errorbar"]
        x: list[float]
        y: list[float]
        yerr: list[float] | None = None
        xerr: list[float] | None = None
        label: str = ""
        marker: _Marker = "o"

        @field_validator("x", "y", "yerr", "xerr")
        @classmethod
        def _cap_points(cls, v: list[float] | None) -> list[float] | None:
            if v is not None and len(v) > _MAX_DATA_POINTS:
                raise ValueError(
                    f"array length {len(v)} exceeds cap {_MAX_DATA_POINTS}"
                )
            return v

    Union = LineSpec | ScatterSpec | BarSpec | HistSpec | ErrorbarSpec
    adapter = TypeAdapter(Union)
    return adapter, (LineSpec, ScatterSpec, BarSpec, HistSpec, ErrorbarSpec)


def _parse_spec(text: str) -> Any:
    """Validate and return a PlotSpec instance from raw JSON text."""
    if not text or not text.strip():
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause="plot: text= required (the JSON spec)",
            next=(
                "put(id='plot:', text='{\"type\":\"line\",\"x\":[1,2,3],"
                "\"y\":[1,4,9]}', mode='render')"
            ),
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"plot: invalid JSON — {exc.msg} at line {exc.lineno} col {exc.colno}",
            next="see get(id='skill:plot-basics') for the spec schema",
        ) from exc

    if not isinstance(data, dict):
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"plot: spec must be a JSON object, got {type(data).__name__}",
            next="see get(id='skill:plot-basics')",
        )
    if "type" not in data:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause="plot: spec is missing required field 'type'",
            options=["line", "scatter", "bar", "hist", "errorbar"],
            next="add e.g. \"type\": \"line\" to the spec",
        )

    try:
        from pydantic import ValidationError
    except ImportError as exc:  # pragma: no cover — registry gates on this
        raise PrecisError(
            ErrorCode.KIND_UNAVAILABLE,
            "pydantic not installed. Install with: pip install precis-mcp[plot]",
        ) from exc

    adapter, _ = _spec_models()
    try:
        return adapter.validate_python(data)
    except ValidationError as exc:
        # Surface the first error compactly; the agent can ask for the
        # skill to see the full schema.
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err["loc"])
        msg = err.get("msg", "invalid")
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"plot: spec invalid at {loc or '<root>'} — {msg}",
            next="see get(id='skill:plot-basics') for the full schema",
        ) from exc


# ---------------------------------------------------------------------------
# Curve fitting (linear / log / exp / arrhenius)
# ---------------------------------------------------------------------------


def _linear_regression(
    xs: list[float], ys: list[float]
) -> tuple[float, float, float]:
    """Return ``(slope, intercept, r_squared)`` for ``y ≈ slope*x + intercept``.

    Pure-Python implementation — matplotlib is already imported but we
    avoid pulling in numpy just for this one helper.
    """
    n = len(xs)
    if n < 2:
        raise ValueError("need at least 2 points for a linear fit")
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ssxx = sum((x - mean_x) ** 2 for x in xs)
    ssxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    sstot = sum((y - mean_y) ** 2 for y in ys)
    if ssxx == 0:
        raise ValueError("cannot fit: all x values identical")
    slope = ssxy / ssxx
    intercept = mean_y - slope * mean_x
    ssres = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)
    )
    r_squared = 1.0 - (ssres / sstot) if sstot > 0 else 1.0
    return slope, intercept, r_squared


def _fit_series(xs: list[float], ys: list[float], kind: str) -> tuple[Any, str]:
    """Compute fit curve + report string.

    Returns ``(fit_fn, report_text)`` where ``fit_fn(x) -> y`` evaluates
    the fitted model.  ``report_text`` is a human-readable summary
    suitable for prepending to the handler output.
    """
    if kind == "linear":
        slope, intercept, r2 = _linear_regression(xs, ys)
        fit_fn = lambda x: slope * x + intercept  # noqa: E731
        report = (
            f"fit (linear): y = {slope:.4g}·x + {intercept:.4g}   R² = {r2:.4f}"
        )
        return fit_fn, report

    if kind == "log":
        # y = a + b·ln(x)  — requires x > 0
        try:
            log_xs = [math.log(x) for x in xs]
        except ValueError as exc:
            raise ValueError("log fit requires all x > 0") from exc
        slope, intercept, r2 = _linear_regression(log_xs, ys)
        fit_fn = lambda x: slope * math.log(x) + intercept  # noqa: E731
        report = (
            f"fit (log): y = {intercept:.4g} + {slope:.4g}·ln(x)   R² = {r2:.4f}"
        )
        return fit_fn, report

    if kind == "exp":
        # y = a·exp(b·x)  — fit ln(y) = ln(a) + b·x ; requires y > 0
        try:
            log_ys = [math.log(y) for y in ys]
        except ValueError as exc:
            raise ValueError("exp fit requires all y > 0") from exc
        slope, intercept, r2 = _linear_regression(xs, log_ys)
        a = math.exp(intercept)
        fit_fn = lambda x: a * math.exp(slope * x)  # noqa: E731
        report = (
            f"fit (exp): y = {a:.4g}·exp({slope:.4g}·x)   R² = {r2:.4f}"
        )
        return fit_fn, report

    if kind == "arrhenius":
        # Convenience wrapper — Arrhenius plots already use ln(k) vs 1/T,
        # so the underlying fit is linear.  We relabel the report.
        slope, intercept, r2 = _linear_regression(xs, ys)
        fit_fn = lambda x: slope * x + intercept  # noqa: E731
        report = (
            f"fit (arrhenius): slope = {slope:.4g}  "
            f"intercept = {intercept:.4g}   R² = {r2:.4f}"
        )
        return fit_fn, report

    raise ValueError(f"unknown fit kind: {kind!r}")


# ---------------------------------------------------------------------------
# Matplotlib rendering
# ---------------------------------------------------------------------------


def _get_matplotlib() -> tuple[Any, str]:
    """Import matplotlib with Agg backend enforced.

    Returns ``(pyplot, version)``.  Raises ``KIND_UNAVAILABLE`` when
    matplotlib isn't installed.
    """
    try:
        import matplotlib

        matplotlib.use("Agg", force=False)
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise PrecisError(
            ErrorCode.KIND_UNAVAILABLE,
            "matplotlib not installed. Install with: pip install precis-mcp[plot]",
        ) from exc
    return plt, matplotlib.__version__


def _style_list(n: int, palette: str) -> list[str]:
    """Return a colour cycle for ``n`` series from the named palette."""
    # Kept lean — precis doesn't need the full cmap machinery.
    tab10 = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]
    viridis = [
        "#440154", "#482878", "#3e4a89", "#31688e", "#26828e",
        "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725",
    ]
    plasma = [
        "#0d0887", "#46039f", "#7201a8", "#9c179e", "#bd3786",
        "#d8576b", "#ed7953", "#fb9f3a", "#fdca26", "#f0f921",
    ]
    grayscale = [f"#{v:02x}{v:02x}{v:02x}" for v in (30, 80, 130, 180, 60, 110, 160)]
    colorblind = [  # Okabe–Ito
        "#000000", "#E69F00", "#56B4E9", "#009E73",
        "#F0E442", "#0072B2", "#D55E00", "#CC79A7",
    ]
    palettes = {
        "default": tab10,
        "tab10": tab10,
        "viridis": viridis,
        "plasma": plasma,
        "grayscale": grayscale,
        "colorblind": colorblind,
    }
    base = palettes.get(palette, tab10)
    return [base[i % len(base)] for i in range(max(n, 1))]


def _apply_common(ax: Any, spec: Any) -> None:
    """Apply title / labels / scales / limits / grid / hline / vline."""
    if spec.title:
        ax.set_title(spec.title)
    if spec.xlabel:
        ax.set_xlabel(spec.xlabel)
    if spec.ylabel:
        ax.set_ylabel(spec.ylabel)
    if spec.xscale != "linear":
        ax.set_xscale(spec.xscale)
    if spec.yscale != "linear":
        ax.set_yscale(spec.yscale)
    if spec.xlim is not None:
        ax.set_xlim(*spec.xlim)
    if spec.ylim is not None:
        ax.set_ylim(*spec.ylim)
    if spec.grid:
        ax.grid(True, alpha=0.3)
    for hl in spec.hline:
        ax.axhline(
            y=hl.y,
            linestyle=_STYLE_MAP[hl.style],
            label=hl.label or None,
            color="#888",
            linewidth=1,
        )
    for vl in spec.vline:
        ax.axvline(
            x=vl.x,
            linestyle=_STYLE_MAP[vl.style],
            label=vl.label or None,
            color="#888",
            linewidth=1,
        )
    for ann in spec.annotate:
        ax.annotate(
            ann.text,
            xy=(ann.x, ann.y),
            xytext=(6, 6),
            textcoords="offset points",
            fontsize=9,
        )


_STYLE_MAP: dict[str, str] = {
    "solid": "-",
    "dashed": "--",
    "dotted": ":",
    "dashdot": "-.",
}


def _collect_line_series(spec: Any) -> list[tuple[list[float], list[float], str, str, str]]:
    """Return a list of ``(x, y, label, style, marker)`` for line/scatter.

    Fans out the top-level ``x``/``y`` form into a single-element list
    when no ``series`` list was provided.
    """
    if spec.series:
        return [
            (s.x, s.y, s.label, s.style, s.marker)
            for s in spec.series
        ]
    xs = spec.x or []
    ys = spec.y or []
    if not xs or not ys:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"plot: {spec.type!r} needs x and y (or series[])",
            next="add x=[...], y=[...] or series=[{x:[...], y:[...]}, ...]",
        )
    return [(xs, ys, spec.label, getattr(spec, "style", "solid"), getattr(spec, "marker", ""))]


def _render_line(ax: Any, spec: Any, colors: list[str]) -> list[str]:
    """Render a line plot.  Returns any fit-report lines."""
    series = _collect_line_series(spec)
    reports: list[str] = []
    for i, (xs, ys, label, style, marker) in enumerate(series):
        if len(xs) != len(ys):
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=(
                    f"plot: series {i} has mismatched lengths "
                    f"(x={len(xs)}, y={len(ys)})"
                ),
            )
        ax.plot(
            xs,
            ys,
            linestyle=_STYLE_MAP[style],
            marker=marker or None,
            label=label or None,
            color=colors[i],
        )
    if spec.fit is not None and series:
        xs, ys, *_ = series[0]
        try:
            fit_fn, report = _fit_series(xs, ys, spec.fit.kind)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"plot: fit failed — {exc}",
            ) from exc
        if spec.fit.report:
            reports.append(report)
        # Overlay on a dense grid so log/exp curves look smooth.
        xmin, xmax = min(xs), max(xs)
        n = 100
        step = (xmax - xmin) / max(n - 1, 1)
        grid = [xmin + i * step for i in range(n)]
        ax.plot(
            grid,
            [fit_fn(x) for x in grid],
            linestyle="--",
            color=colors[0],
            alpha=0.7,
            label=f"fit ({spec.fit.kind})",
        )
    return reports


def _render_scatter(ax: Any, spec: Any, colors: list[str]) -> list[str]:
    series = _collect_line_series(spec)
    reports: list[str] = []
    for i, (xs, ys, label, _style, marker) in enumerate(series):
        if len(xs) != len(ys):
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=(
                    f"plot: series {i} has mismatched lengths "
                    f"(x={len(xs)}, y={len(ys)})"
                ),
            )
        ax.scatter(
            xs,
            ys,
            marker=marker or "o",
            label=label or None,
            color=colors[i],
        )
    if spec.fit is not None and series:
        xs, ys, *_ = series[0]
        try:
            fit_fn, report = _fit_series(xs, ys, spec.fit.kind)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"plot: fit failed — {exc}",
            ) from exc
        if spec.fit.report:
            reports.append(report)
        xmin, xmax = min(xs), max(xs)
        n = 100
        step = (xmax - xmin) / max(n - 1, 1)
        grid = [xmin + i * step for i in range(n)]
        ax.plot(
            grid,
            [fit_fn(x) for x in grid],
            linestyle="--",
            color=colors[0],
            alpha=0.7,
            label=f"fit ({spec.fit.kind})",
        )
    return reports


def _render_bar(ax: Any, spec: Any, colors: list[str]) -> list[str]:
    if spec.series:
        n_groups = len(spec.labels)
        n_series = len(spec.series)
        if n_series == 0:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="plot: bar with empty series",
            )
        # Grouped bars.
        total_width = 0.8
        bar_w = total_width / n_series
        positions = list(range(n_groups))
        for i, s in enumerate(spec.series):
            if len(s.values) != n_groups:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    cause=(
                        f"plot: bar series {i!r} has {len(s.values)} values "
                        f"but labels has {n_groups}"
                    ),
                )
            offsets = [p + (i - n_series / 2) * bar_w + bar_w / 2 for p in positions]
            if spec.horizontal:
                ax.barh(offsets, s.values, height=bar_w, label=s.label or None, color=colors[i])
            else:
                ax.bar(offsets, s.values, width=bar_w, label=s.label or None, color=colors[i])
        if spec.horizontal:
            ax.set_yticks(positions)
            ax.set_yticklabels(spec.labels)
        else:
            ax.set_xticks(positions)
            ax.set_xticklabels(spec.labels)
    else:
        values = spec.values or []
        if len(values) != len(spec.labels):
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=(
                    f"plot: bar has {len(values)} values but "
                    f"{len(spec.labels)} labels"
                ),
            )
        if spec.horizontal:
            ax.barh(spec.labels, values, color=colors[0])
        else:
            ax.bar(spec.labels, values, color=colors[0])
    return []


def _render_hist(ax: Any, spec: Any, colors: list[str]) -> list[str]:
    ax.hist(
        spec.values,
        bins=spec.bins,
        label=spec.label or None,
        color=colors[0],
        edgecolor="white",
        linewidth=0.5,
    )
    return []


def _render_errorbar(ax: Any, spec: Any, colors: list[str]) -> list[str]:
    if len(spec.x) != len(spec.y):
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=(
                f"plot: errorbar mismatched lengths "
                f"(x={len(spec.x)}, y={len(spec.y)})"
            ),
        )
    if spec.yerr is not None and len(spec.yerr) != len(spec.y):
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=(
                f"plot: errorbar yerr length {len(spec.yerr)} != y length "
                f"{len(spec.y)}"
            ),
        )
    if spec.xerr is not None and len(spec.xerr) != len(spec.x):
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=(
                f"plot: errorbar xerr length {len(spec.xerr)} != x length "
                f"{len(spec.x)}"
            ),
        )
    ax.errorbar(
        spec.x,
        spec.y,
        yerr=spec.yerr,
        xerr=spec.xerr,
        fmt=spec.marker or "o",
        label=spec.label or None,
        color=colors[0],
        capsize=3,
    )
    return []


_DISPATCH = {
    "line": _render_line,
    "scatter": _render_scatter,
    "bar": _render_bar,
    "hist": _render_hist,
    "errorbar": _render_errorbar,
}


def _series_count(spec: Any) -> int:
    """Number of colour slots the plot needs."""
    series = getattr(spec, "series", [])
    if series:
        return len(series)
    return 1


def _render(
    spec: Any, fmt: str
) -> tuple[bytes, list[str], str]:
    """Render ``spec`` to ``fmt`` bytes.

    Returns ``(image_bytes, fit_reports, matplotlib_version)``.
    """
    plt, version = _get_matplotlib()
    fig, ax = plt.subplots(figsize=spec.figsize, dpi=spec.dpi)
    try:
        colors = _style_list(_series_count(spec), spec.palette)
        reports = _DISPATCH[spec.type](ax, spec, colors)
        _apply_common(ax, spec)
        if spec.legend and ax.get_legend_handles_labels()[1]:
            ax.legend(loc="best", fontsize=9)
        buf = io.BytesIO()
        save_fmt = "jpeg" if fmt == "jpg" else fmt
        fig.savefig(buf, format=save_fmt, bbox_inches="tight")
        return buf.getvalue(), reports, version
    finally:
        plt.close(fig)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _attribution(version: str) -> str:
    return _FOOTER.format(version=version)


def _format_inline(
    data: bytes, fmt: str, spec: Any, reports: list[str], version: str
) -> str:
    """Assemble the text response for an inline render."""
    if len(data) > _MAX_INLINE_BYTES and fmt != "svg":
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=(
                f"plot: rendered image is {len(data) // 1024} KB which "
                f"exceeds the inline cap of {_MAX_INLINE_BYTES // 1024} KB"
            ),
            next="use /export or reduce dpi / figsize",
        )
    mime = _FORMAT_MIME[fmt]
    title = spec.title or f"{spec.type} plot"
    header = f"📊 {title}  ({len(data)} bytes, {mime})"
    parts: list[str] = [header, ""]
    for r in reports:
        parts.append(r)
    if reports:
        parts.append("")
    if fmt == "svg":
        parts.append(data.decode("utf-8", errors="replace"))
    else:
        b64 = base64.b64encode(data).decode("ascii")
        parts.append(f"data:{mime};base64,{b64}")
    parts.append("")
    parts.append(
        "Next: put(id='plot:/export/<name>.<ext>', text='<spec>', mode='render') "
        "— save to ./figures/"
    )
    parts.append("")
    parts.append(_attribution(version))
    return "\n".join(parts)


def _format_export(
    data: bytes,
    target: Path,
    fmt: str,
    spec: Any,
    reports: list[str],
    version: str,
    spec_text: str,
) -> str:
    """Write ``data`` to ``target`` and return a confirmation string.

    Resolves the AUTO placeholder into a short content-hash so repeated
    renders of the same spec reuse the filename (free memoisation at
    the filesystem level).
    """
    if target.name == "plot-AUTO.png":
        digest = hashlib.sha256(spec_text.encode()).hexdigest()[:_HASH_LEN]
        target = target.with_name(f"plot-{digest}.{fmt}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    title = spec.title or f"{spec.type} plot"
    lines = [
        f"✓ Wrote {target} ({len(data)} bytes, {fmt})",
        f"  Title: {title}",
    ]
    for r in reports:
        lines.append(f"  {r}")
    lines.append("")
    lines.append(_attribution(version))
    return "\n".join(lines)


def _help_text() -> str:
    """Concise inline help pointing at the onboarding skill."""
    return (
        "# plot — declarative matplotlib renderer (local)\n"
        "\n"
        "Free, offline, deterministic.  Pass a JSON spec via put();\n"
        "get a PNG / SVG / WebP back inline, or export to ./figures/.\n"
        "\n"
        "**Inline** — default returns a data URL embedded in text:\n"
        "  put(id='plot:',     text='<spec>', mode='render')   # PNG\n"
        "  put(id='plot:/svg', text='<spec>', mode='render')   # SVG\n"
        "  put(id='plot:/webp',text='<spec>', mode='render')   # WebP\n"
        "\n"
        "**Export** — writes a file under ./figures/:\n"
        "  put(id='plot:/export',             text='<spec>', mode='render')\n"
        "  put(id='plot:/export/arrhenius.pdf', text='<spec>', mode='render')\n"
        "\n"
        "**Minimum spec** — one required field, ``type``:\n"
        "  {\"type\": \"line\", \"x\": [1,2,3,4], \"y\": [1,4,9,16]}\n"
        "\n"
        "**Types**: line, scatter, bar, hist, errorbar.\n"
        "**Fit overlay** (line/scatter): "
        "``\"fit\": {\"kind\": \"linear|log|exp|arrhenius\"}``.\n"
        "\n"
        "See: get(id='skill:plot-basics') — full schema + examples.\n"
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class PlotHandler(Handler):
    """Handler for the ``plot:`` scheme — declarative matplotlib renderer.

    Agent usage::

        put(id='plot:', text='{\"type\":\"line\",\"x\":[1,2],\"y\":[1,4]}',
            mode='render')                               # inline PNG
        put(id='plot:/svg', text='<spec>', mode='render')   # inline SVG
        put(id='plot:/export/chart.pdf', text='<spec>',
            mode='render')                                  # write to disk
        get(id='plot:')                                     # onboarding
    """

    scheme = "plot"
    writable = True
    views: ClassVar[set[str]] = {"help"}
    allowed_modes: ClassVar[set[str]] = {"render"}
    onboarding_skill: ClassVar[str | None] = "plot-basics"

    # ---- Core read --------------------------------------------------

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
    ) -> str:
        # plot: is a write-only compute kind — reads return onboarding.
        # Verify matplotlib is actually available so the help message
        # isn't misleading in a broken install.
        _get_matplotlib()  # raises KIND_UNAVAILABLE if missing
        return _help_text()

    # ---- Write surface ----------------------------------------------

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        # Server forwards 'tracked', 'note', 'link', optionally 'tags'.
        # None of these apply to plot — silently drop them rather than
        # rejecting via extract_kwargs, so the generic MCP `put` tool
        # signature stays compatible.
        if mode != "render":
            raise PrecisError(
                ErrorCode.MODE_UNSUPPORTED,
                cause=f"plot: mode must be 'render', got {mode!r}",
                next=(
                    "put(id='plot:', text='<json-spec>', mode='render') — "
                    "stateless compute, no persistence"
                ),
            )

        fmt, view, export_path = _parse_plot_path(path or "")
        if view == "help":
            return _help_text()

        spec = _parse_spec(text)

        if export_path is not None:
            # Writing to disk — allow any supported export format.
            data, reports, version = _render(spec, fmt)
            return _format_export(
                data, export_path, fmt, spec, reports, version, text
            )

        # Inline path — only PNG / SVG / WebP reach here; PDF has to go
        # through /export.
        if fmt not in _INLINE_FORMATS:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"plot: format {fmt!r} is export-only",
                next=f"use put(id='plot:/export/chart.{fmt}', ...)",
            )
        data, reports, version = _render(spec, fmt)
        return _format_inline(data, fmt, spec, reports, version)
