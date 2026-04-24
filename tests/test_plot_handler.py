"""Phase 5b — declarative matplotlib plot handler.

Covers:

- Path parsing: empty / format suffix / /help / /export / /export/<file>
- Spec parsing: valid / missing type / unknown field / bad JSON
- Rendering: PNG / SVG / WebP inline; PDF / PNG export to tmp dir
- Fit overlay: linear / log / exp / arrhenius report lines
- Errors: mode must be 'render', mismatched series lengths, oversize data
- Safety: absolute export paths rejected, ``..`` rejected, inline cap
- Registration: plot kind registers when matplotlib + pydantic present
- Opaque URI: parser keeps ``/`` and ``.`` inside plot paths
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

matplotlib = pytest.importorskip("matplotlib")
pydantic = pytest.importorskip("pydantic")

from precis.handlers.plot import (
    PlotHandler,
    _linear_regression,
    _parse_plot_path,
    _parse_spec,
)
from precis.protocol import ErrorCode, PrecisError
from precis.registry import KINDS, SCHEMES
from precis.uri import _OPAQUE_PATH_SCHEMES, parse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _put(h: PlotHandler, path: str, spec: dict, mode: str = "render") -> str:
    return h.put(
        path=path,
        selector=None,
        text=json.dumps(spec),
        mode=mode,
    )


def _read(h: PlotHandler, path: str = "") -> str:
    return h.read(
        path=path,
        selector=None,
        view=None,
        subview=None,
        query="",
        summarize=False,
        depth=0,
        page=1,
    )


_MIN_LINE = {"type": "line", "x": [1, 2, 3, 4], "y": [1, 4, 9, 16]}
_MIN_SCATTER = {"type": "scatter", "x": [1, 2, 3], "y": [2, 4, 6]}
_MIN_BAR = {"type": "bar", "labels": ["A", "B", "C"], "values": [1, 2, 3]}
_MIN_HIST = {"type": "hist", "values": [1, 2, 2, 3, 3, 3, 4, 4, 5]}
_MIN_ERRORBAR = {
    "type": "errorbar",
    "x": [1, 2, 3],
    "y": [1.0, 2.0, 3.0],
    "yerr": [0.1, 0.2, 0.15],
}


# ---------------------------------------------------------------------------
# Path parsing
# ---------------------------------------------------------------------------


class TestParsePlotPath:
    def test_empty_defaults_png_inline(self):
        fmt, view, exp = _parse_plot_path("")
        assert fmt == "png" and view is None and exp is None

    def test_slash_only_equivalent_to_empty(self):
        fmt, view, exp = _parse_plot_path("/")
        assert fmt == "png" and view is None and exp is None

    def test_svg(self):
        fmt, view, exp = _parse_plot_path("/svg")
        assert fmt == "svg" and view is None and exp is None

    def test_webp(self):
        fmt, view, exp = _parse_plot_path("/webp")
        assert fmt == "webp" and view is None and exp is None

    def test_help(self):
        fmt, view, exp = _parse_plot_path("/help")
        assert view == "help"

    def test_export_default(self):
        fmt, view, exp = _parse_plot_path("/export")
        assert fmt == "png"
        assert exp is not None
        assert exp.name.endswith(".png")

    def test_export_explicit_filename_pdf(self):
        fmt, view, exp = _parse_plot_path("/export/chart.pdf")
        assert fmt == "pdf"
        assert exp == Path("figures") / "chart.pdf"

    def test_export_nested_path_kept(self):
        fmt, view, exp = _parse_plot_path("/export/deep/nested.svg")
        assert fmt == "svg"
        assert exp == Path("figures") / "deep" / "nested.svg"

    def test_unknown_suffix_errors(self):
        with pytest.raises(PrecisError) as ei:
            _parse_plot_path("/gif")
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_export_empty_filename_errors(self):
        with pytest.raises(PrecisError) as ei:
            _parse_plot_path("/export/")
        assert ei.value.code == ErrorCode.PARAM_INVALID
        assert "filename" in ei.value.cause.lower()

    def test_export_absolute_path_rejected(self):
        with pytest.raises(PrecisError) as ei:
            _parse_plot_path("/export//etc/passwd.png")
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_export_parent_traversal_rejected(self):
        with pytest.raises(PrecisError) as ei:
            _parse_plot_path("/export/../escape.png")
        assert ei.value.code == ErrorCode.PARAM_INVALID
        assert ".." in ei.value.cause

    def test_export_unknown_extension_rejected(self):
        with pytest.raises(PrecisError) as ei:
            _parse_plot_path("/export/chart.tiff")
        assert ei.value.code == ErrorCode.PARAM_INVALID


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


class TestParseSpec:
    def test_empty_text_errors(self):
        with pytest.raises(PrecisError) as ei:
            _parse_spec("")
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_invalid_json_errors(self):
        with pytest.raises(PrecisError) as ei:
            _parse_spec("{not json")
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_missing_type_errors(self):
        with pytest.raises(PrecisError) as ei:
            _parse_spec('{"x":[1,2],"y":[1,2]}')
        assert ei.value.code == ErrorCode.PARAM_INVALID
        assert "type" in ei.value.cause

    def test_unknown_type_errors(self):
        with pytest.raises(PrecisError) as ei:
            _parse_spec('{"type":"violin","values":[1,2]}')
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_unknown_field_errors(self):
        # extra='forbid' rejects unknown fields.
        bad = json.dumps({**_MIN_LINE, "badkey": 1})
        with pytest.raises(PrecisError) as ei:
            _parse_spec(bad)
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_root_not_object_errors(self):
        with pytest.raises(PrecisError) as ei:
            _parse_spec("[1,2,3]")
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_line_parses(self):
        spec = _parse_spec(json.dumps(_MIN_LINE))
        assert spec.type == "line"
        assert spec.x == [1, 2, 3, 4]

    def test_scatter_parses(self):
        spec = _parse_spec(json.dumps(_MIN_SCATTER))
        assert spec.type == "scatter"

    def test_bar_parses(self):
        spec = _parse_spec(json.dumps(_MIN_BAR))
        assert spec.type == "bar"

    def test_hist_parses(self):
        spec = _parse_spec(json.dumps(_MIN_HIST))
        assert spec.type == "hist"

    def test_errorbar_parses(self):
        spec = _parse_spec(json.dumps(_MIN_ERRORBAR))
        assert spec.type == "errorbar"

    def test_oversize_array_rejected(self):
        big = {"type": "line", "x": list(range(50_001)), "y": list(range(50_001))}
        with pytest.raises(PrecisError) as ei:
            _parse_spec(json.dumps(big))
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_figsize_bounds_enforced(self):
        bad = {**_MIN_LINE, "figsize": [100, 100]}
        with pytest.raises(PrecisError) as ei:
            _parse_spec(json.dumps(bad))
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_dpi_bounds_enforced(self):
        bad = {**_MIN_LINE, "dpi": 1200}
        with pytest.raises(PrecisError) as ei:
            _parse_spec(json.dumps(bad))
        assert ei.value.code == ErrorCode.PARAM_INVALID


# ---------------------------------------------------------------------------
# Linear regression helper
# ---------------------------------------------------------------------------


class TestLinearRegression:
    def test_perfect_line(self):
        slope, intercept, r2 = _linear_regression([0, 1, 2, 3], [1, 3, 5, 7])
        assert slope == pytest.approx(2.0)
        assert intercept == pytest.approx(1.0)
        assert r2 == pytest.approx(1.0)

    def test_r_squared_for_noisy_data(self):
        slope, intercept, r2 = _linear_regression(
            [1, 2, 3, 4, 5], [2.0, 4.1, 5.9, 8.0, 10.1]
        )
        assert slope == pytest.approx(2.02, abs=0.05)
        assert r2 > 0.99

    def test_too_few_points_errors(self):
        with pytest.raises(ValueError):
            _linear_regression([1.0], [2.0])

    def test_identical_xs_errors(self):
        with pytest.raises(ValueError):
            _linear_regression([1, 1, 1], [1, 2, 3])


# ---------------------------------------------------------------------------
# Inline rendering
# ---------------------------------------------------------------------------


class TestInlineRendering:
    def test_line_png_default(self):
        h = PlotHandler()
        out = _put(h, "", _MIN_LINE)
        assert "image/png" in out
        assert "data:image/png;base64," in out
        assert "matplotlib" in out  # footer

    def test_line_svg(self):
        h = PlotHandler()
        out = _put(h, "/svg", _MIN_LINE)
        assert "image/svg+xml" in out
        # SVG is inline text, not base64.
        assert "<svg" in out
        # PNG data URL should NOT appear.
        assert "data:image/png" not in out

    def test_line_webp(self):
        h = PlotHandler()
        out = _put(h, "/webp", _MIN_LINE)
        assert "image/webp" in out
        assert "data:image/webp;base64," in out

    def test_scatter_with_fit_report(self):
        h = PlotHandler()
        spec = {
            "type": "scatter",
            "x": [0, 1, 2, 3, 4],
            "y": [1, 3, 5, 7, 9],
            "fit": {"kind": "linear", "report": True},
        }
        out = _put(h, "", spec)
        assert "fit (linear)" in out
        assert "R²" in out

    def test_arrhenius_fit_relabelled(self):
        h = PlotHandler()
        spec = {
            "type": "scatter",
            "x": [2.10, 2.25, 2.41, 2.58],
            "y": [-12.3, -11.4, -10.6, -9.9],
            "fit": {"kind": "arrhenius"},
        }
        out = _put(h, "", spec)
        assert "fit (arrhenius)" in out
        assert "slope" in out

    def test_log_fit_rejects_non_positive_x(self):
        h = PlotHandler()
        spec = {
            "type": "line",
            "x": [0, 1, 2],
            "y": [1, 2, 3],
            "fit": {"kind": "log"},
        }
        with pytest.raises(PrecisError) as ei:
            _put(h, "", spec)
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_bar_single_series(self):
        h = PlotHandler()
        out = _put(h, "", _MIN_BAR)
        assert "image/png" in out

    def test_bar_grouped_series(self):
        h = PlotHandler()
        spec = {
            "type": "bar",
            "labels": ["Fe", "Cu", "Ni"],
            "series": [
                {"label": "2023", "values": [3.2, 5.1, 2.8]},
                {"label": "2024", "values": [3.5, 4.9, 3.1]},
            ],
        }
        out = _put(h, "", spec)
        assert "image/png" in out

    def test_bar_label_count_mismatch_errors(self):
        h = PlotHandler()
        spec = {"type": "bar", "labels": ["A", "B"], "values": [1]}
        with pytest.raises(PrecisError) as ei:
            _put(h, "", spec)
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_hist(self):
        h = PlotHandler()
        out = _put(h, "", _MIN_HIST)
        assert "image/png" in out

    def test_errorbar(self):
        h = PlotHandler()
        out = _put(h, "", _MIN_ERRORBAR)
        assert "image/png" in out

    def test_errorbar_mismatched_yerr_errors(self):
        h = PlotHandler()
        spec = {
            **_MIN_ERRORBAR,
            "yerr": [0.1, 0.2],  # length 2, y is length 3
        }
        with pytest.raises(PrecisError) as ei:
            _put(h, "", spec)
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_line_mismatched_xy_errors(self):
        h = PlotHandler()
        spec = {"type": "line", "x": [1, 2, 3], "y": [1, 2]}
        with pytest.raises(PrecisError) as ei:
            _put(h, "", spec)
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_line_missing_xy_errors(self):
        h = PlotHandler()
        spec = {"type": "line"}
        with pytest.raises(PrecisError) as ei:
            _put(h, "", spec)
        assert ei.value.code == ErrorCode.PARAM_INVALID

    def test_title_in_header(self):
        h = PlotHandler()
        spec = {**_MIN_LINE, "title": "Arrhenius"}
        out = _put(h, "", spec)
        assert "Arrhenius" in out

    def test_log_scale_applied(self):
        h = PlotHandler()
        spec = {**_MIN_LINE, "yscale": "log"}
        out = _put(h, "", spec)
        # No assertion on image bytes — just that log scale doesn't crash.
        assert "image/png" in out

    def test_pdf_inline_rejected(self):
        # PDF is export-only; trying inline should error helpfully.
        h = PlotHandler()
        spec = json.dumps(_MIN_LINE)
        with pytest.raises(PrecisError) as ei:
            # /export/chart.pdf would hit the export path, so we fake
            # a direct inline request by round-tripping through the
            # handler with a path that isn't a valid inline format.
            h.put(path="/pdf", selector=None, text=spec, mode="render")
        assert ei.value.code == ErrorCode.PARAM_INVALID


# ---------------------------------------------------------------------------
# Export rendering
# ---------------------------------------------------------------------------


class TestExportRendering:
    def test_export_png(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        h = PlotHandler()
        out = _put(h, "/export/chart.png", _MIN_LINE)
        assert "✓ Wrote" in out
        target = tmp_path / "figures" / "chart.png"
        assert target.exists()
        assert target.read_bytes().startswith(b"\x89PNG")

    def test_export_svg(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        h = PlotHandler()
        out = _put(h, "/export/chart.svg", _MIN_LINE)
        assert "✓ Wrote" in out
        target = tmp_path / "figures" / "chart.svg"
        assert target.exists()
        assert b"<svg" in target.read_bytes()

    def test_export_pdf(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        h = PlotHandler()
        out = _put(h, "/export/chart.pdf", _MIN_LINE)
        assert "✓ Wrote" in out
        target = tmp_path / "figures" / "chart.pdf"
        assert target.exists()
        assert target.read_bytes().startswith(b"%PDF")

    def test_export_auto_filename(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        h = PlotHandler()
        out = _put(h, "/export", _MIN_LINE)
        assert "✓ Wrote" in out
        figures = tmp_path / "figures"
        pngs = list(figures.glob("plot-*.png"))
        assert len(pngs) == 1
        # Name contains a short content hash, not "AUTO".
        assert "AUTO" not in pngs[0].name

    def test_export_auto_filename_memoises_on_spec(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        h = PlotHandler()
        out1 = _put(h, "/export", _MIN_LINE)
        out2 = _put(h, "/export", _MIN_LINE)
        figures = tmp_path / "figures"
        pngs = list(figures.glob("plot-*.png"))
        # Same spec → same filename → one file, not two.
        assert len(pngs) == 1

    def test_export_nested_path_creates_dirs(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        h = PlotHandler()
        out = _put(h, "/export/deep/nested/chart.png", _MIN_LINE)
        target = tmp_path / "figures" / "deep" / "nested" / "chart.png"
        assert target.exists()


# ---------------------------------------------------------------------------
# Mode enforcement & misc
# ---------------------------------------------------------------------------


class TestModeAndMisc:
    def test_replace_mode_rejected(self):
        h = PlotHandler()
        with pytest.raises(PrecisError) as ei:
            h.put(
                path="",
                selector=None,
                text=json.dumps(_MIN_LINE),
                mode="replace",
            )
        assert ei.value.code == ErrorCode.MODE_UNSUPPORTED

    def test_read_returns_help(self):
        h = PlotHandler()
        out = _read(h)
        assert "plot:" in out
        assert "skill:plot-basics" in out

    def test_unknown_kwargs_ignored(self):
        # Server forwards tracked/note/link/tags; plot shouldn't reject.
        h = PlotHandler()
        out = h.put(
            path="",
            selector=None,
            text=json.dumps(_MIN_LINE),
            mode="render",
            tracked=True,
            note="",
            link="",
            tags=["some-tag"],
        )
        assert "image/png" in out

    def test_attribution_footer_present(self):
        h = PlotHandler()
        out = _put(h, "", _MIN_LINE)
        assert "matplotlib" in out
        assert "Verify values before citing" in out

    def test_next_hint_points_at_export(self):
        h = PlotHandler()
        out = _put(h, "", _MIN_LINE)
        assert "plot:/export" in out


# ---------------------------------------------------------------------------
# URI parser — opaque scheme
# ---------------------------------------------------------------------------


class TestPlotIsOpaque:
    def test_plot_in_opaque_schemes(self):
        assert "plot" in _OPAQUE_PATH_SCHEMES

    def test_parser_keeps_slash_in_export_path(self):
        p = parse("plot:/export/chart.pdf")
        # Opaque: the entire path after the colon belongs to the handler.
        assert p.path == "/export/chart.pdf"
        assert p.view is None
        assert p.subview is None

    def test_parser_keeps_dot_in_filename(self):
        p = parse("plot:/export/deep/foo.svg")
        assert p.path == "/export/deep/foo.svg"

    def test_parser_empty_path(self):
        p = parse("plot:")
        assert p.path == ""


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestPlotRegistration:
    @classmethod
    def setup_class(cls):
        import precis.registry as reg

        reg._discover()

    def test_plot_scheme_registered(self):
        assert "plot" in SCHEMES

    def test_plot_kind_registered(self):
        assert "plot" in KINDS
        spec = KINDS["plot"].spec
        assert spec.cost_hint == "free"
        assert "matplotlib" in spec.description.lower()

    def test_plot_handler_class(self):
        from precis.handlers.plot import PlotHandler

        assert SCHEMES["plot"] is PlotHandler
