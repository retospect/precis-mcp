"""Static SVG renderer for the surface-Pourbaix map (slice 2 web surface).

Pure ``map dict → svg string`` over the object
:mod:`precis.quest.pourbaix_map` builds — no store, no engine. The layout:

* x = U_RHE (the sweep is pH-flat on the RHE scale, so the surface regions
  are vertical bands; the same map in SHE terms would tilt 0.059 V/pH),
* y = pH (0 at the bottom),
* each boundary is a line with the *propagated* band as a translucent strip
  and the *model-form* band as a second, hatched strip — the two bands are
  drawn separately, never summed (transfer prompt §5),
* the bulk Pourbaix layer is a dashed, labelled overlay (ruling q3: its
  boundaries carry a few tenths of a volt of oxide-DFT error and never
  merge into the surface envelope).

Deliberately static — slice 3 puts the shared slider bank beside it; the
template keeps a slot for it.
"""

from __future__ import annotations

import html
import math
from itertools import pairwise
from typing import Any

#: Termination fill colours (surface layer). Keys are the winning
#: adsorbate; clean is the gamma=0 reference.
_TERMINATION_COLORS: dict[str, str] = {
    "clean": "#e5e7eb",
    "H": "#93c5fd",
    "O": "#fca5a5",
    "OH": "#86efac",
}
_TERMINATION_DEFAULT = "#d8b4fe"

#: Bulk-verdict colours for the labelled uncertain layer (dashed stroke).
_BULK_COLORS: dict[str, str] = {
    "dissolved": "#dc2626",
    "leached": "#ea580c",
    "oxidised": "#ca8a04",
    "transformed": "#65a30d",
    "unmatched": "#6b7280",
    "stable": "#16a34a",
}

_ML = 70
_MR = 20
_MT = 24
_MB = 64


def _esc(x: Any) -> str:
    return html.escape(str(x), quote=True)


def _ticks(lo: float, hi: float, n: int = 6) -> list[float]:
    """~``n`` round tick positions inside ``[lo, hi]``."""
    if not lo < hi:
        return [lo]
    raw = (hi - lo) / n
    step = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if step * m >= raw:
            step *= m
            break
    start = math.ceil(lo / step) * step
    out = []
    v = start
    while v <= hi + 1e-9:
        out.append(round(v, 6))
        v += step
    return out


def _cell_step(values: list[float]) -> float:
    """Median spacing of a bulk grid axis (the cell half-extent is step/2)."""
    u = sorted({float(v) for v in values if isinstance(v, int | float)})
    if len(u) < 2:
        return 0.0
    ds = sorted(b - a for a, b in pairwise(u))
    return ds[len(ds) // 2]


def map_svg(
    m: dict[str, Any],
    *,
    width: int = 760,
    height: int = 460,
    u_marker: float | None = None,
    vlines: list[dict[str, Any]] | None = None,
    reset: list[dict[str, Any]] | None = None,
    masks: dict[str, Any] | None = None,
) -> str:
    """One map → an inline ``<svg>`` fragment. ``m`` is the
    :func:`precis.quest.pourbaix_map.build_map` object.

    Slice 3 additions: ``u_marker`` draws the draggable U indicator (a
    ``.pbx-u-marker`` line the slider bank repositions), and ``vlines``
    draws fixed verticals (the campaign carry-over: each route's ``U_L``
    and ``U_opt`` as ``{"u", "label", "color", "dash"}``). The ``data-*``
    attributes on the root carry the plot's coordinate transform so the
    shared slider bank can move the marker client-side.

    Slice 7: ``reset`` draws the guaranteed-reset layer as one togglable
    ``<g class="pbx-reset">`` group — per route ``{"name", "color",
    "ok": [(u_lo, u_hi)…], "band": [(u_lo, u_hi)…]}`` where ``ok`` is the
    guaranteed window (solid tint, the route's own colour) and ``band``
    the hysteretic zone — viable but not guaranteed, the region §6.2
    says not to rely on — drawn dashed at the same colour. It is its own
    layer, never merged with the propagated/model-form bands.

    Slice 4 (gr489471): ``masks`` — ``{"rungs": [{"name", "viable":
    [(u_lo, u_hi)…]}…], "her": [(u_lo, u_hi)…] | None, "window":
    [(u_lo, u_hi)…]}`` — draws one ``<g class="pbx-mask"
    data-rung=…>`` per PCET rung (where it is viable), a
    ``<g class="pbx-mask-her">`` and the operating window as an outlined
    ``<g class="pbx-window">``. The page's toggles show/hide each group;
    rung and HER groups start hidden."""
    window = m.get("U_RHE_window") or [None, None]
    if window[0] is None or window[1] is None or not (window[0] < window[1]):
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="60" role="img">'
            '<text x="10" y="30" font-size="12" fill="#6b7280">'
            "no sweep window recorded — anchors exist but no pooled "
            "che_sweep was stamped</text></svg>"
        )
    u_lo, u_hi = float(window[0]), float(window[1])
    ph_lo, ph_hi = (m.get("pH_window") or [0.0, 14.0])[:2]
    ph_lo, ph_hi = float(ph_lo), float(ph_hi)
    plot_w = width - _ML - _MR
    plot_h = height - _MT - _MB

    def X(u: float) -> float:
        return _ML + (u - u_lo) / (u_hi - u_lo) * plot_w

    def Y(ph: float) -> float:
        return _MT + (ph_hi - ph) / (ph_hi - ph_lo) * plot_h

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        'font-family="ui-monospace, monospace" class="pbx-map" '
        f'data-ulo="{u_lo:g}" data-uhi="{u_hi:g}" '
        f'data-x0="{_ML}" data-pw="{plot_w}" '
        f'data-phlo="{ph_lo:g}" data-phhi="{ph_hi:g}" '
        f'data-y0="{_MT}" data-ploth="{plot_h}">',
        "<defs>"
        '<pattern id="mfHatch" width="6" height="6" '
        'patternTransform="rotate(45)" patternUnits="userSpaceOnUse">'
        '<rect width="6" height="6" fill="none"/>'
        '<line x1="0" y1="0" x2="0" y2="6" stroke="#d97706" '
        'stroke-width="1.4" stroke-opacity="0.55"/></pattern>'
        '<pattern id="bulkHatch" width="7" height="7" '
        'patternTransform="rotate(-30)" patternUnits="userSpaceOnUse">'
        '<rect width="7" height="7" fill="none"/>'
        '<line x1="0" y1="0" x2="0" y2="7" stroke="#94a3b8" '
        'stroke-width="1" stroke-opacity="0.5"/></pattern>'
        "</defs>",
        f'<rect x="{_ML}" y="{_MT}" width="{plot_w}" height="{plot_h}" '
        'fill="#f8fafc" stroke="#cbd5e1"/>',
    ]

    # Validity regions — vertical bands (pH-flat on the RHE scale).
    legend_regions: list[tuple[str, str]] = []
    for r in m.get("regions") or []:
        a, b = r.get("U_RHE_range") or [None, None]
        if a is None or b is None:
            continue
        term = (r.get("termination") or {}).get("adsorbate") or "clean"
        color = _TERMINATION_COLORS.get(term, _TERMINATION_DEFAULT)
        x0, x1 = X(float(a)), X(float(b))
        parts.append(
            f'<rect x="{x0:.1f}" y="{_MT}" width="{max(x1 - x0, 0.5):.1f}" '
            f'height="{plot_h}" fill="{color}" fill-opacity="0.45"/>'
        )
        label = str(r.get("label") or term)
        if x1 - x0 > 34:
            parts.append(
                f'<text x="{(x0 + x1) / 2:.1f}" y="{_MT + 14}" font-size="10" '
                f'fill="#334155" text-anchor="middle">{_esc(label)}</text>'
            )
        legend_regions.append((label, color))

    # Boundaries — a line per crossing, two bands kept apart.
    for b in m.get("boundaries") or []:
        u = b.get("U_RHE")
        if not isinstance(u, int | float):
            continue
        x = X(float(u))
        prop = b.get("band_propagated")
        if isinstance(prop, int | float) and math.isfinite(float(prop)):
            w = abs(X(float(u) + float(prop)) - x)
            parts.append(
                f'<rect x="{x - w:.1f}" y="{_MT}" width="{2 * w:.1f}" '
                f'height="{plot_h}" fill="#64748b" fill-opacity="0.18"/>'
            )
        mf = b.get("band_model_form")
        if isinstance(mf, int | float) and math.isfinite(float(mf)):
            w = abs(X(float(u) + float(mf)) - x)
            parts.append(
                f'<rect x="{x - w:.1f}" y="{_MT}" width="{2 * w:.1f}" '
                f'height="{plot_h}" fill="url(#mfHatch)"/>'
            )
        parts.append(
            f'<line x1="{x:.1f}" y1="{_MT}" x2="{x:.1f}" '
            f'y2="{_MT + plot_h}" stroke="#0f172a" stroke-width="1.2"/>'
        )

    # Bulk layer — the labelled uncertain overlay (dashed, never merged).
    bulk = m.get("bulk") or {}
    cells = bulk.get("cells") or []
    du = _cell_step([c.get("U_RHE") for c in cells]) or (u_hi - u_lo) / 10
    dp = _cell_step([c.get("pH") for c in cells]) or (ph_hi - ph_lo) / 10
    for c in cells:
        u, ph, v = c.get("U_RHE"), c.get("pH"), c.get("verdict")
        if not isinstance(u, int | float) or not isinstance(ph, int | float):
            continue
        color = _BULK_COLORS.get(str(v), "#6b7280")
        x0, x1 = X(float(u) - du / 2), X(float(u) + du / 2)
        y0, y1 = Y(float(ph) + dp / 2), Y(float(ph) - dp / 2)
        parts.append(
            f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" '
            f'height="{y1 - y0:.1f}" fill="url(#bulkHatch)" '
            f'stroke="{color}" stroke-width="1.2" '
            'stroke-dasharray="4 3" fill-opacity="0.35"/>'
        )
    pt = bulk.get("point") or {}
    if isinstance(pt.get("U_RHE"), int | float) and isinstance(
        pt.get("pH"), int | float
    ):
        px, py = X(float(pt["U_RHE"])), Y(float(pt["pH"]))
        parts.append(
            f'<path d="M {px:.1f} {py - 6:.1f} L {px + 6:.1f} {py:.1f} '
            f'L {px:.1f} {py + 6:.1f} L {px - 6:.1f} {py:.1f} Z" '
            f'fill="none" stroke="#111827" stroke-width="1.6"/>'
        )

    # Slice 7: the guaranteed-reset layer — one togglable group per map.
    # ``ok`` spans (guaranteed) get a solid tint, ``band`` spans (the
    # hysteretic zone: viable, not guaranteed) a dashed, denser tint in
    # the same route colour. Drawn under the fixed verticals and marker.
    legend_reset: list[tuple[str, str]] = []
    if reset:
        parts.append('<g class="pbx-reset">')
        for rr in reset:
            color = str(rr.get("color") or "#0f766e")
            for lo, hi in rr.get("ok") or []:
                x0, x1 = X(float(lo)), X(float(hi))
                parts.append(
                    f'<rect x="{x0:.1f}" y="{_MT}" '
                    f'width="{max(x1 - x0, 0.5):.1f}" height="{plot_h}" '
                    f'fill="{color}" fill-opacity="0.14"/>'
                )
            for lo, hi in rr.get("band") or []:
                x0, x1 = X(float(lo)), X(float(hi))
                parts.append(
                    f'<rect x="{x0:.1f}" y="{_MT}" '
                    f'width="{max(x1 - x0, 0.5):.1f}" height="{plot_h}" '
                    f'fill="{color}" fill-opacity="0.28" stroke="{color}" '
                    'stroke-width="1" stroke-dasharray="3 3" '
                    'stroke-opacity="0.6"/>'
                )
            if rr.get("ok") or rr.get("band"):
                legend_reset.append((str(rr.get("name") or "?"), color))
        parts.append("</g>")

    # Slice 4: per-rung viability bands, the HER mask and the operating
    # window — each its own group so the page's toggles address it.
    if masks:

        def spans(group: str, items: Any, style: str) -> None:
            parts.append(group)
            for lo, hi in items or []:
                x0, x1 = X(float(lo)), X(float(hi))
                parts.append(
                    f'<rect x="{x0:.1f}" y="{_MT}" width="{max(x1 - x0, 0.5):.1f}" '
                    f'height="{plot_h}" {style}/>'
                )
            parts.append("</g>")

        for rr in masks.get("rungs") or []:
            spans(
                f'<g class="pbx-mask" data-rung="{_esc(rr.get("name"))}" '
                'style="display:none">',
                rr.get("viable"),
                'fill="#2563eb" fill-opacity="0.12"',
            )
        if masks.get("her") is not None:
            spans(
                '<g class="pbx-mask-her" style="display:none">',
                masks.get("her"),
                'fill="#dc2626" fill-opacity="0.10"',
            )
        spans(
            '<g class="pbx-window">',
            masks.get("window"),
            'fill="#16a34a" fill-opacity="0.10" stroke="#15803d" '
            'stroke-width="1.6" stroke-dasharray="6 3"',
        )

    # Slice 3: route U_L / U_opt verticals (fixed — the affine terms they
    # come from are U-invariant), then the draggable U marker last so it
    # sits on top.
    for vl in vlines or []:
        u = vl.get("u")
        if not isinstance(u, int | float) or not math.isfinite(float(u)):
            continue
        if not u_lo <= float(u) <= u_hi:
            continue  # outside the swept window — clipped, not extrapolated
        x = X(float(u))
        dash = f' stroke-dasharray="{_esc(vl.get("dash"))}"' if vl.get("dash") else ""
        color = _esc(vl.get("color") or "#0f172a")
        parts.append(
            f'<line x1="{x:.1f}" y1="{_MT}" x2="{x:.1f}" '
            f'y2="{_MT + plot_h}" stroke="{color}" stroke-width="1.1"{dash}/>'
        )
        if vl.get("label"):
            parts.append(
                f'<text x="{x + 3:.1f}" y="{_MT + plot_h - 6}" font-size="9" '
                f'fill="{color}">{_esc(vl["label"])}</text>'
            )
    if isinstance(u_marker, int | float) and math.isfinite(float(u_marker)):
        x = X(min(max(float(u_marker), u_lo), u_hi))
        parts.append(
            f'<line class="pbx-u-marker" x1="{x:.1f}" y1="{_MT}" x2="{x:.1f}" '
            f'y2="{_MT + plot_h}" stroke="#dc2626" stroke-width="1.8" '
            'stroke-opacity="0.85"/>'
        )

    # Axes + ticks.
    for t in _ticks(u_lo, u_hi):
        x = X(t)
        parts.append(
            f'<line x1="{x:.1f}" y1="{_MT + plot_h}" x2="{x:.1f}" '
            f'y2="{_MT + plot_h + 5}" stroke="#64748b"/>'
            f'<text x="{x:.1f}" y="{_MT + plot_h + 17}" font-size="10" '
            f'fill="#475569" text-anchor="middle">{t:g}</text>'
        )
    for t in _ticks(ph_lo, ph_hi, n=7):
        y = Y(t)
        parts.append(
            f'<line x1="{_ML - 5}" y1="{y:.1f}" x2="{_ML}" y2="{y:.1f}" '
            f'stroke="#64748b"/>'
            f'<text x="{_ML - 8}" y="{y + 3:.1f}" font-size="10" '
            f'fill="#475569" text-anchor="end">{t:g}</text>'
        )
    parts.append(
        f'<text x="{_ML + plot_w / 2:.1f}" y="{_MT + plot_h + 34}" '
        'font-size="11" fill="#334155" text-anchor="middle">'
        "U (V vs RHE) — pH-flat on this scale; the SHE view tilts "
        "0.059 V/pH</text>"
        f'<text x="16" y="{_MT + plot_h / 2:.1f}" font-size="11" '
        'fill="#334155" text-anchor="middle" '
        f'transform="rotate(-90 16 {_MT + plot_h / 2:.1f})">pH</text>'
    )

    # Legend + provenance footer.
    lx = _ML
    ly = _MT + plot_h + 46
    for label, color in legend_regions:
        parts.append(
            f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" '
            f'fill="{color}" fill-opacity="0.7" stroke="#94a3b8"/>'
            f'<text x="{lx + 13}" y="{ly}" font-size="9" '
            f'fill="#334155">{_esc(label)}</text>'
        )
        lx += 13 + max(60, len(label) * 6)
    parts.append(
        f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" '
        'fill="#64748b" fill-opacity="0.25"/>'
        f'<text x="{lx + 13}" y="{ly}" font-size="9" fill="#334155">'
        "propagated band</text>"
    )
    lx += 110
    parts.append(
        f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" '
        'fill="url(#mfHatch)" stroke="#d97706" stroke-opacity="0.4"/>'
        f'<text x="{lx + 13}" y="{ly}" font-size="9" fill="#334155">'
        "model-form band</text>"
    )
    if cells:
        lx += 118
        parts.append(
            f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" '
            'fill="url(#bulkHatch)" stroke="#dc2626" '
            'stroke-dasharray="3 2" fill-opacity="0.35"/>'
            f'<text x="{lx + 13}" y="{ly}" font-size="9" fill="#334155">'
            "bulk layer (uncertain)</text>"
        )
    for label, color in legend_reset:
        lx += 115
        parts.append(
            f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" '
            f'fill="{color}" fill-opacity="0.2" stroke="{color}" '
            'stroke-dasharray="2 2"/>'
            f'<text x="{lx + 13}" y="{ly}" font-size="9" fill="#334155">'
            f"{_esc(label)} reset (dashed = hysteretic)</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


__all__ = ["map_svg"]
