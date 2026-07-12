"""Pure-function tests for the figure SVG helpers — no DB, no model."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from precis.figure import svg as S

# ── compile check ────────────────────────────────────────────────────────


def test_parse_error_none_for_clean_svg() -> None:
    assert S.parse_error('<svg viewBox="0 0 10 10"><rect/></svg>') is None


def test_parse_error_reports_malformed() -> None:
    err = S.parse_error("<svg><rect></svg>")  # unclosed rect
    assert err is not None and "well-formed" in err


def test_parse_error_rejects_non_svg_root() -> None:
    err = S.parse_error("<rect x='1'/>")
    assert err is not None and "must be <svg>" in err


# ── sanitize (the XSS/SSRF seam) ─────────────────────────────────────────


def test_sanitize_strips_script() -> None:
    out = S.sanitize_svg(
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect/></svg>'
    )
    assert "script" not in out.lower()
    assert "rect" in out


def test_sanitize_strips_foreignobject() -> None:
    out = S.sanitize_svg(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        "<foreignObject><body/></foreignObject><circle/></svg>"
    )
    assert "foreignobject" not in out.lower()
    assert "circle" in out


def test_sanitize_strips_event_handlers() -> None:
    out = S.sanitize_svg('<svg><rect onclick="evil()" onload="x"/></svg>')
    assert "onclick" not in out.lower()
    assert "onload" not in out.lower()


def test_sanitize_strips_external_href() -> None:
    out = S.sanitize_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<image xlink:href="http://evil/x.png"/>'
        '<a href="https://evil/"/></svg>'
    )
    assert "evil" not in out


def test_sanitize_strips_data_uri_href() -> None:
    out = S.sanitize_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<image xlink:href="data:text/html,&lt;script&gt;"/></svg>'
    )
    assert "data:" not in out


def test_sanitize_keeps_local_fragment_href() -> None:
    out = S.sanitize_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<defs><linearGradient id="g"/></defs>'
        '<rect xlink:href="#g"/></svg>'
    )
    assert "#g" in out


def test_sanitize_promotes_bare_svg_to_namespace() -> None:
    out = S.sanitize_svg('<svg viewBox="0 0 10 10"><rect/></svg>')
    # A standalone document must carry xmlns so it renders in an <img>.
    assert "http://www.w3.org/2000/svg" in out
    # And it must still parse.
    assert S.parse_error(out) is None


def test_sanitize_raises_on_unparseable() -> None:
    with pytest.raises(S.SvgError):
        S.sanitize_svg("<svg><rect></svg>")


# ── viewBox ──────────────────────────────────────────────────────────────


def test_read_viewbox() -> None:
    assert S.read_viewbox('<svg viewBox="0 0 100 50"/>') == (0.0, 0.0, 100.0, 50.0)


def test_read_viewbox_comma_separated() -> None:
    assert S.read_viewbox('<svg viewBox="1,2,3,4"/>') == (1.0, 2.0, 3.0, 4.0)


def test_read_viewbox_absent_is_none() -> None:
    assert S.read_viewbox("<svg/>") is None


def test_read_viewbox_malformed_is_none() -> None:
    assert S.read_viewbox('<svg viewBox="0 0 100"/>') is None


# ── out-of-bounds lint ───────────────────────────────────────────────────


def test_lint_clean_when_inside() -> None:
    svg = '<svg viewBox="0 0 100 100"><rect id="a" x="10" y="10" width="20" height="20"/></svg>'
    assert S.lint_svg(svg) == []


def test_lint_flags_rect_outside() -> None:
    svg = '<svg viewBox="0 0 100 100"><rect id="a" x="90" y="90" width="50" height="50"/></svg>'
    findings = S.lint_svg(svg)
    assert len(findings) == 1
    assert findings[0].kind == "bounds"
    assert findings[0].node == "a"


def test_lint_flags_circle_negative() -> None:
    svg = '<svg viewBox="0 0 100 100"><circle id="c" cx="5" cy="5" r="20"/></svg>'
    findings = S.lint_svg(svg)
    assert findings and findings[0].node == "c"


def test_lint_circle_inside() -> None:
    svg = '<svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="20"/></svg>'
    assert S.lint_svg(svg) == []


def test_lint_polyline_bbox() -> None:
    svg = '<svg viewBox="0 0 100 100"><polyline id="p" points="10,10 50,50 200,20"/></svg>'
    findings = S.lint_svg(svg)
    assert findings and findings[0].node == "p"


def test_lint_compile_failure_short_circuits() -> None:
    findings = S.lint_svg("<svg><rect></svg>")
    assert len(findings) == 1 and findings[0].kind == "compile"


def test_lint_explicit_viewbox_overrides_document() -> None:
    svg = '<svg viewBox="0 0 1000 1000"><rect x="90" y="90" width="50" height="50"/></svg>'
    # Inside the document's own 1000×1000 box, but outside an explicit 100×100.
    assert S.lint_svg(svg) == []
    assert S.lint_svg(svg, viewbox=(0.0, 0.0, 100.0, 100.0))


def test_lint_ignores_transformed_shape() -> None:
    svg = (
        '<svg viewBox="0 0 100 100">'
        '<rect x="90" y="90" width="50" height="50" transform="translate(-80,-80)"/>'
        "</svg>"
    )
    # Transformed shapes aren't bounds-checked (documented slice-1 limitation).
    assert S.lint_svg(svg) == []


def test_lint_skips_path_and_text() -> None:
    svg = '<svg viewBox="0 0 100 100"><path d="M200 200 L300 300"/><text x="500">hi</text></svg>'
    assert S.lint_svg(svg) == []


# ── default canvas ───────────────────────────────────────────────────────


def test_default_svg_parses_and_has_viewbox() -> None:
    out = S.default_svg()
    assert S.parse_error(out) is None
    assert S.read_viewbox(out) == S.DEFAULT_VIEWBOX


def test_default_svg_custom_viewbox() -> None:
    out = S.default_svg((0.0, 0.0, 512.0, 384.0))
    assert S.read_viewbox(out) == (0.0, 0.0, 512.0, 384.0)
    # No trailing .0 in the serialized attribute.
    assert 'viewBox="0 0 512 384"' in out


def test_roundtrip_sanitize_is_stable() -> None:
    once = S.sanitize_svg(S.default_svg())
    twice = S.sanitize_svg(once)
    # Sanitizing an already-clean document is idempotent (modulo the dropped
    # comment on first pass).
    assert ET.tostring(ET.fromstring(once)) == ET.tostring(ET.fromstring(twice))
