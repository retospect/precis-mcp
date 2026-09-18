"""Volcano-plot compute: family-level overpotential vs. descriptor.

Reads ``reaction_eval`` chunks across a set of materials and pairs
each material's network overpotential with a chosen descriptor
(typically the *OH binding free energy). Returns the numeric
points plus a self-contained SVG of the plot.

The descriptor selection is by name string. Supported forms:

- ``"G(*OH)"`` (or ``"G:*OH"``) — looks up the calc with
  ``species='*OH'`` on the material and returns its CHE-referenced
  free energy.
- ``"eads(*OH)"`` — same data but pulled from
  ``meta.derived.eads`` if present, falling back to ``G_che``.
- Any other species name in parentheses (``"G(*O)"``,
  ``"G(*H)"``) follows the same convention.

The SVG is a plain self-contained ``<svg>`` document with axis
ticks, a title, and a labelled point per material. No JavaScript
dependencies. Designed to embed cleanly in a markdown chunk.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VolcanoPoint:
    material_id: str
    descriptor_value: float
    overpotential: float
    rate_determining_step: int | None
    conditions_hash: str | None


@dataclass
class VolcanoResult:
    """Wrapped record returned by :func:`compute`.

    Carries the numeric points (for downstream tabular use), the
    SVG (for the agent's ``get(view='volcano')``), and the chosen
    network + descriptor labels (for the chart axes and the
    written-back chunk meta).
    """

    network_id: str
    descriptor: str
    family: str
    points: list[VolcanoPoint]
    svg: str
    summary: str


def compute(
    *,
    materials: list[tuple[str, dict[str, Any], list[dict[str, Any]]]],
    calc_lookup: dict[str, dict[str, Any]],
    network_id: str,
    descriptor: str,
    family: str = "(unnamed)",
    conditions_hash: str | None = None,
) -> VolcanoResult:
    """Compute the volcano points + SVG.

    Inputs:

    - ``materials``: list of ``(material_id, material_meta, reaction_eval_chunks)``
      tuples. Each ``reaction_eval_chunks`` is the list of mock
      blocks (each with ``.text`` JSON and ``.meta`` dict) the
      store returns. Real store callers pass the same shape.
    - ``calc_lookup``: maps ``calc_id`` → calc meta dict, so the
      descriptor lookup can pull the *OH binding energy etc.
    - ``network_id``: the reaction network to plot for.
    - ``descriptor``: name of the descriptor axis (e.g.
      ``'G(*OH)'``).
    - ``family``: cosmetic label for the plot title.
    - ``conditions_hash``: if set, only consider reaction_eval chunks
      with matching conditions_hash. ``None`` accepts any.
    """
    points: list[VolcanoPoint] = []
    target_species = _species_from_descriptor(descriptor)

    for material_id, material_meta, eval_chunks in materials:
        # Find the matching reaction_eval chunk.
        record = _pick_record(eval_chunks, network_id, conditions_hash)
        if record is None:
            continue
        # Find the descriptor value.
        d_value = _descriptor_value(
            target_species,
            material_meta=material_meta,
            calc_lookup=calc_lookup,
        )
        if d_value is None:
            continue
        points.append(
            VolcanoPoint(
                material_id=material_id,
                descriptor_value=d_value,
                overpotential=float(record.get("overpotential", 0.0)),
                rate_determining_step=record.get("rate_determining_step"),
                conditions_hash=record.get("conditions_hash"),
            )
        )

    # Sort by descriptor value so the plot reads left-to-right.
    points.sort(key=lambda p: p.descriptor_value)

    svg = _render_volcano_svg(
        points,
        network_id=network_id,
        descriptor=descriptor,
        family=family,
    )
    summary = _render_summary(
        points,
        network_id=network_id,
        descriptor=descriptor,
        family=family,
    )
    return VolcanoResult(
        network_id=network_id,
        descriptor=descriptor,
        family=family,
        points=points,
        svg=svg,
        summary=summary,
    )


# ── Internals ─────────────────────────────────────────────────────


_DESCRIPTOR_RE = re.compile(r"^\s*[A-Za-z_]+\s*[:(](\*[A-Za-z0-9]+)[)\s]*$")


def _species_from_descriptor(descriptor: str) -> str:
    """Pull the species id out of a descriptor name.

    ``"G(*OH)"`` / ``"eads(*OH)"`` / ``"G:*OH"`` all → ``"*OH"``.
    Falls back to the descriptor text itself when no parens are
    found, so a bare ``"*OH"`` works too.
    """
    match = _DESCRIPTOR_RE.match(descriptor)
    if match:
        return match.group(1)
    if descriptor.startswith("*"):
        return descriptor
    raise ValueError(
        f"could not parse descriptor {descriptor!r} (expected 'G(*OH)' / 'eads(*OH)' / '*OH')"
    )


def _pick_record(
    eval_chunks: list[Any],
    network_id: str,
    conditions_hash: str | None,
) -> dict[str, Any] | None:
    """Find the reaction_eval chunk matching network + (optionally) conditions."""
    for chunk in eval_chunks:
        meta = getattr(chunk, "meta", {}) or {}
        if meta.get("network_id") != network_id:
            continue
        if (
            conditions_hash is not None
            and meta.get("conditions_hash") != conditions_hash
        ):
            continue
        try:
            return json.loads(getattr(chunk, "text", "") or "{}")
        except json.JSONDecodeError:
            continue
    return None


def _descriptor_value(
    species: str,
    *,
    material_meta: dict[str, Any],
    calc_lookup: dict[str, dict[str, Any]],
) -> float | None:
    """Resolve the descriptor value for ``species`` on a material.

    Lookup order:
    1. The material's ``derived.eads_summary[species]`` if present.
    2. The first calc in the material's ``calculations`` list whose
       ``meta.species == species``. Pull its ``derived.G_che`` or
       ``derived.G`` value.
    """
    derived = material_meta.get("derived", {}) or {}
    eads_summary = derived.get("eads_summary", {}) or {}
    if species in eads_summary:
        entry = eads_summary[species]
        if isinstance(entry, dict) and "value" in entry:
            return float(entry["value"])
        if isinstance(entry, (int, float)):
            return float(entry)

    for calc_id in material_meta.get("calculations", []) or []:
        meta = calc_lookup.get(calc_id, {}) or {}
        if meta.get("species") != species:
            continue
        d = meta.get("derived", {}) or {}
        if "G_che" in d:
            return float(d["G_che"])
        # Try keyed forms.
        for key, entry in (d.get("G") or {}).items():
            _ = key
            if isinstance(entry, dict) and "value" in entry:
                return float(entry["value"])
            if isinstance(entry, (int, float)):
                return float(entry)
    return None


def _render_summary(
    points: list[VolcanoPoint],
    *,
    network_id: str,
    descriptor: str,
    family: str,
) -> str:
    """One-screen text summary of the volcano (table + best material)."""
    if not points:
        return (
            f"# volcano {network_id} on family {family}\n"
            f"no materials with both descriptor {descriptor} and a "
            f"{network_id} reaction_eval chunk\n"
        )
    best = min(points, key=lambda p: p.overpotential)
    lines = [
        f"# volcano {network_id} on family {family}",
        f"descriptor: {descriptor}",
        f"n_points:   {len(points)}",
        f"best:       {best.material_id} (η={best.overpotential:.3f} V)",
        "",
        "descriptor | η (V) | RDS | material",
        "-" * 60,
    ]
    for p in points:
        rds = p.rate_determining_step if p.rate_determining_step is not None else "-"
        lines.append(
            f"{p.descriptor_value:>9.3f}  | {p.overpotential:.3f}  | {rds!s:>3}  | {p.material_id}"
        )
    return "\n".join(lines)


def _render_volcano_svg(
    points: list[VolcanoPoint],
    *,
    network_id: str,
    descriptor: str,
    family: str,
) -> str:
    """Render a volcano-style SVG with axis labels and points.

    The plot doesn't fit a Sabatier curve through the points — that
    would require choosing one or two scaling relations between
    descriptor and intermediates, and the v1 SVG is informational,
    not predictive. Each material is one point, with the descriptor
    on x and the thermodynamic overpotential on y.
    """
    width = 480
    height = 320
    margin_left = 64
    margin_right = 24
    margin_top = 36
    margin_bottom = 56

    if not points:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" font-family="sans-serif">'
            f'<rect width="100%" height="100%" fill="white"/>'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'font-size="14" fill="#333">no volcano points</text>'
            f"</svg>"
        )

    x_vals = [p.descriptor_value for p in points]
    y_vals = [p.overpotential for p in points]
    x_min, x_max = min(x_vals), max(x_vals)
    y_min, y_max = min(y_vals), max(y_vals)
    x_pad = max(0.1, 0.1 * (x_max - x_min))
    y_pad = max(0.05, 0.1 * (y_max - y_min))
    x_min -= x_pad
    x_max += x_pad
    y_min = max(0.0, y_min - y_pad)
    y_max += y_pad
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    def x_of(v: float) -> float:
        return margin_left + (v - x_min) / (x_max - x_min) * plot_w

    def y_of(v: float) -> float:
        return margin_top + (1.0 - (v - y_min) / (y_max - y_min)) * plot_h

    svg_parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="11">',
        '<rect width="100%" height="100%" fill="white"/>',
        # Title.
        f'<text x="{width / 2}" y="20" text-anchor="middle" font-size="13" font-weight="bold">'
        f"Volcano: {network_id} on family {family}</text>",
        # Axes.
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" '
        f'y2="{height - margin_bottom}" stroke="#333"/>',
        f'<line x1="{margin_left}" y1="{height - margin_bottom}" '
        f'x2="{width - margin_right}" y2="{height - margin_bottom}" stroke="#333"/>',
        # Axis labels.
        f'<text x="{margin_left + plot_w / 2}" y="{height - 12}" text-anchor="middle">'
        f"descriptor: {descriptor}</text>",
        f'<text x="14" y="{margin_top + plot_h / 2}" text-anchor="middle" '
        f'transform="rotate(-90 14 {margin_top + plot_h / 2})">η (V, thermodynamic)</text>',
    ]

    # Ticks.
    for i in range(5):
        frac = i / 4.0
        xv = x_min + frac * (x_max - x_min)
        xpx = x_of(xv)
        svg_parts.append(
            f'<line x1="{xpx}" y1="{height - margin_bottom}" '
            f'x2="{xpx}" y2="{height - margin_bottom + 5}" stroke="#333"/>'
        )
        svg_parts.append(
            f'<text x="{xpx}" y="{height - margin_bottom + 18}" text-anchor="middle">'
            f"{xv:.2f}</text>"
        )
        yv = y_min + frac * (y_max - y_min)
        ypx = y_of(yv)
        svg_parts.append(
            f'<line x1="{margin_left}" y1="{ypx}" x2="{margin_left - 5}" y2="{ypx}" stroke="#333"/>'
        )
        svg_parts.append(
            f'<text x="{margin_left - 8}" y="{ypx + 4}" text-anchor="end">{yv:.2f}</text>'
        )

    # Points.
    for p in points:
        cx = x_of(p.descriptor_value)
        cy = y_of(p.overpotential)
        svg_parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="5" fill="#1f77b4" stroke="#0d3b66" stroke-width="0.5"/>'
        )
        # Label to the right of the marker.
        label = p.material_id.removeprefix("material:")
        svg_parts.append(
            f'<text x="{cx + 8}" y="{cy + 4}" font-size="10" fill="#222">{_escape(label)}</text>'
        )

    svg_parts.append("</svg>")
    return "".join(svg_parts)


def _escape(text: str) -> str:
    """Minimal XML escaping for the labels inside the SVG."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── Pathway diagram ──────────────────────────────────────────────


@dataclass
class PathwayResult:
    """Free-energy ladder for one or more materials on one network."""

    network_id: str
    material_ids: list[str]
    step_dGs_per_material: dict[str, list[float]]
    svg: str
    summary: str


def pathway_diagram(
    *,
    materials: list[tuple[str, list[Any]]],
    network_id: str,
    conditions_hash: str | None = None,
) -> PathwayResult:
    """Render a step-by-step free-energy ladder.

    ``materials`` is a list of ``(material_id, reaction_eval_chunks)``
    tuples — same shape as :func:`compute` but without descriptor
    lookup (the ladder uses the step ΔGs directly).

    The cumulative ΔG along the reaction coordinate is drawn for
    each material on the same chart so the overpotential-limiting
    step is visually obvious. At U=0 the curve climbs; at U=U_eq
    it would flatten; at U > U_eq it tilts downhill. The v1 chart
    plots U=0 (the most informative single condition).
    """
    series: dict[str, list[float]] = {}
    for material_id, eval_chunks in materials:
        record = _pick_record(eval_chunks, network_id, conditions_hash)
        if record is None:
            continue
        step_dG = list(record.get("step_dG", []) or [])
        if not step_dG:
            continue
        series[material_id] = step_dG

    svg = _render_pathway_svg(series, network_id=network_id)
    summary = _render_pathway_summary(series, network_id=network_id)
    return PathwayResult(
        network_id=network_id,
        material_ids=list(series.keys()),
        step_dGs_per_material=series,
        svg=svg,
        summary=summary,
    )


def _render_pathway_summary(series: dict[str, list[float]], *, network_id: str) -> str:
    if not series:
        return f"# pathway {network_id}\nno material reaction_eval chunks matched\n"
    lines = [f"# pathway diagram: {network_id}", ""]
    for material_id, step_dG in series.items():
        cum = [0.0]
        for d in step_dG:
            cum.append(cum[-1] + d)
        worst_step = max(range(len(step_dG)), key=lambda i: step_dG[i])
        steps_str = [f"{d:.2f}" for d in step_dG]
        cum_str = [f"{c:.2f}" for c in cum]
        lines.append(
            f"  {material_id}: steps={steps_str} cumulative={cum_str} limiting=step{worst_step}"
        )
    return "\n".join(lines)


_PATHWAY_COLORS = (
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#ff7f0e",
    "#9467bd",
    "#8c564b",
)


def _render_pathway_svg(series: dict[str, list[float]], *, network_id: str) -> str:
    width = 520
    height = 320
    margin_left = 56
    margin_right = 24
    margin_top = 36
    margin_bottom = 56

    if not series:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" font-family="sans-serif">'
            f'<rect width="100%" height="100%" fill="white"/>'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'font-size="14" fill="#333">no pathway data</text>'
            f"</svg>"
        )

    n_steps = max(len(v) for v in series.values())
    # Cumulative ΔG along the coordinate.
    cum_series: dict[str, list[float]] = {}
    for mid, step_dG in series.items():
        cum = [0.0]
        for d in step_dG:
            cum.append(cum[-1] + d)
        cum_series[mid] = cum

    all_y = [y for cum in cum_series.values() for y in cum]
    y_min = min(0.0, min(all_y))
    y_max = max(all_y) + 0.2

    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    def x_of(step: int) -> float:
        if n_steps == 0:
            return margin_left
        return margin_left + step / n_steps * plot_w

    def y_of(v: float) -> float:
        return margin_top + (1.0 - (v - y_min) / max(1e-9, (y_max - y_min))) * plot_h

    svg_parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="11">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="20" text-anchor="middle" font-size="13" '
        f'font-weight="bold">Pathway: {network_id} at U=0</text>',
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" '
        f'y2="{height - margin_bottom}" stroke="#333"/>',
        f'<line x1="{margin_left}" y1="{height - margin_bottom}" '
        f'x2="{width - margin_right}" y2="{height - margin_bottom}" stroke="#333"/>',
        f'<text x="{margin_left + plot_w / 2}" y="{height - 12}" text-anchor="middle">step</text>',
        f'<text x="14" y="{margin_top + plot_h / 2}" text-anchor="middle" '
        f'transform="rotate(-90 14 {margin_top + plot_h / 2})">'
        f"cumulative ΔG (eV)</text>",
    ]
    # Step x-axis labels.
    for i in range(n_steps + 1):
        xpx = x_of(i)
        svg_parts.append(
            f'<line x1="{xpx}" y1="{height - margin_bottom}" '
            f'x2="{xpx}" y2="{height - margin_bottom + 5}" stroke="#333"/>'
        )
        svg_parts.append(
            f'<text x="{xpx}" y="{height - margin_bottom + 18}" text-anchor="middle">{i}</text>'
        )
    # Y ticks.
    for i in range(5):
        frac = i / 4.0
        yv = y_min + frac * (y_max - y_min)
        ypx = y_of(yv)
        svg_parts.append(
            f'<line x1="{margin_left}" y1="{ypx}" x2="{margin_left - 5}" y2="{ypx}" stroke="#333"/>'
        )
        svg_parts.append(
            f'<text x="{margin_left - 8}" y="{ypx + 4}" text-anchor="end">{yv:.2f}</text>'
        )

    # Polyline per material.
    for color_idx, (material_id, cum) in enumerate(cum_series.items()):
        color = _PATHWAY_COLORS[color_idx % len(_PATHWAY_COLORS)]
        points_attr = " ".join(f"{x_of(i)},{y_of(v)}" for i, v in enumerate(cum))
        svg_parts.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points_attr}"/>'
        )
        for i, v in enumerate(cum):
            svg_parts.append(
                f'<circle cx="{x_of(i)}" cy="{y_of(v)}" r="4" fill="{color}"/>'
            )
        # Legend at top-right.
        legend_y = margin_top + 10 + color_idx * 14
        svg_parts.append(
            f'<line x1="{width - margin_right - 90}" y1="{legend_y}" '
            f'x2="{width - margin_right - 70}" y2="{legend_y}" '
            f'stroke="{color}" stroke-width="2"/>'
        )
        label = material_id.removeprefix("material:")
        svg_parts.append(
            f'<text x="{width - margin_right - 64}" y="{legend_y + 4}" '
            f'font-size="10">{_escape(label)}</text>'
        )

    svg_parts.append("</svg>")
    return "".join(svg_parts)


__all__ = [
    "PathwayResult",
    "VolcanoPoint",
    "VolcanoResult",
    "compute",
    "pathway_diagram",
]


# Suppress unused-import warning when math is unused after refactors.
_ = math
