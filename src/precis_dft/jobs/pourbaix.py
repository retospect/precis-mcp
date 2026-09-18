"""Bulk Pourbaix diagram compute.

For a set of competing phases (metal, oxide, hydroxide, dissolved ions),
the dominant phase at each (U, pH) point minimises a chemical
potential of the form::

    μ(U, pH) = μ_0 - n_e * U - 0.0592 * n_H * pH       # at 298 K

where ``μ_0`` is the formation energy (eV) relative to the reference
elements, and ``n_e`` / ``n_H`` are the stoichiometric electrons and
protons exchanged on forming the phase from those references. This
is the standard simplified Pourbaix formulation; pymatgen's full
PourbaixDiagram class adds ion-activity corrections and entropy
of dissolution which v1 ignores. The v1 SVG is informational, not
publication-quality, and the limitations are noted in the
``summary`` output.

A Pourbaix entry packs everything one phase needs::

    {
      "material_id":     "material:Pt_metal",
      "formula":         "Pt",          # for the label on the diagram
      "formation_energy": 0.0,           # eV, reference for the metal
      "n_e":             0,              # electrons exchanged from reference
      "n_H":             0,              # protons exchanged from reference
      "aqueous":         False,          # solid or dissolved
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: kT * ln(10) at 298.15 K, in eV (matches Sterner / Norskov).
_KT_LN10 = 0.05916

#: Default U-axis range (V vs. SHE).
_DEFAULT_U_RANGE: tuple[float, float] = (-1.0, 3.0)

#: Default pH-axis range.
_DEFAULT_PH_RANGE: tuple[float, float] = (0.0, 14.0)


@dataclass(frozen=True)
class PourbaixEntry:
    """One competing phase on the Pourbaix grid."""

    material_id: str
    formula: str
    formation_energy: float
    n_e: int = 0
    n_H: int = 0
    aqueous: bool = False


@dataclass
class PourbaixResult:
    """Pourbaix compute output: the dominant-phase map + SVG."""

    entries: list[PourbaixEntry]
    u_range: tuple[float, float]
    ph_range: tuple[float, float]
    n_u: int
    n_ph: int
    dominant_grid: list[list[int]]
    """``dominant_grid[i][j]`` is the index in ``entries`` of the
    dominant phase at the i-th pH bin and j-th U bin."""
    svg: str
    summary: str


def compute(
    *,
    entries: list[PourbaixEntry],
    u_range: tuple[float, float] = _DEFAULT_U_RANGE,
    ph_range: tuple[float, float] = _DEFAULT_PH_RANGE,
    n_u: int = 96,
    n_ph: int = 28,
) -> PourbaixResult:
    """Compute the Pourbaix stability map + SVG.

    The (U, pH) grid is uniform; each cell's dominant phase is the
    entry with the minimum chemical potential at that point. Phases
    are coloured cyclically in the SVG; the legend on the right
    identifies them.
    """
    if not entries:
        return PourbaixResult(
            entries=entries,
            u_range=u_range,
            ph_range=ph_range,
            n_u=n_u,
            n_ph=n_ph,
            dominant_grid=[[]],
            svg=_empty_svg("no Pourbaix entries"),
            summary="# Pourbaix\nno entries\n",
        )

    u_lo, u_hi = u_range
    ph_lo, ph_hi = ph_range
    u_step = (u_hi - u_lo) / max(1, n_u - 1)
    ph_step = (ph_hi - ph_lo) / max(1, n_ph - 1)

    dominant: list[list[int]] = [[0 for _ in range(n_u)] for _ in range(n_ph)]
    for i in range(n_ph):
        pH = ph_lo + i * ph_step
        for j in range(n_u):
            U = u_lo + j * u_step
            # Min μ over all entries.
            best_idx = 0
            best_mu = _mu(entries[0], U, pH)
            for k in range(1, len(entries)):
                mu = _mu(entries[k], U, pH)
                if mu < best_mu:
                    best_mu = mu
                    best_idx = k
            dominant[i][j] = best_idx

    svg = _render_pourbaix_svg(
        dominant,
        entries=entries,
        u_range=u_range,
        ph_range=ph_range,
    )
    summary = _render_summary(
        dominant,
        entries=entries,
        u_range=u_range,
        ph_range=ph_range,
    )
    return PourbaixResult(
        entries=entries,
        u_range=u_range,
        ph_range=ph_range,
        n_u=n_u,
        n_ph=n_ph,
        dominant_grid=dominant,
        svg=svg,
        summary=summary,
    )


def from_materials(
    materials: list[tuple[str, dict[str, Any]]],
) -> list[PourbaixEntry]:
    """Extract Pourbaix entries from material meta dicts.

    Each material's ``meta.derived.pourbaix`` (when present) carries
    the per-phase block this expects. Materials without that block
    are skipped — the caller adds them via a derive_eform run that
    knows enough chemistry to write the block. The aim here is to
    keep the meta surface small and let the compute layer demand
    only what it needs.
    """
    out: list[PourbaixEntry] = []
    for material_id, meta in materials:
        derived = meta.get("derived", {}) or {}
        pourbaix = derived.get("pourbaix")
        if not isinstance(pourbaix, dict):
            continue
        try:
            out.append(
                PourbaixEntry(
                    material_id=material_id,
                    formula=str(pourbaix.get("formula", material_id)),
                    formation_energy=float(pourbaix["formation_energy"]),
                    n_e=int(pourbaix.get("n_e", 0)),
                    n_H=int(pourbaix.get("n_H", 0)),
                    aqueous=bool(pourbaix.get("aqueous", False)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ── Internals ─────────────────────────────────────────────────────


def _mu(entry: PourbaixEntry, U: float, pH: float) -> float:
    """Standard-Pourbaix chemical potential of one phase.

    ``μ = E_formation - n_e * U - kT * ln(10) * n_H * pH``.
    """
    return entry.formation_energy - entry.n_e * U - _KT_LN10 * entry.n_H * pH


def _render_summary(
    dominant: list[list[int]],
    *,
    entries: list[PourbaixEntry],
    u_range: tuple[float, float],
    ph_range: tuple[float, float],
) -> str:
    """Text summary: which phases appear and how much grid area they cover."""
    n_ph = len(dominant)
    n_u = len(dominant[0]) if n_ph else 0
    counts: dict[int, int] = {idx: 0 for idx in range(len(entries))}
    for row in dominant:
        for cell in row:
            counts[cell] = counts.get(cell, 0) + 1
    total = max(1, n_ph * n_u)

    lines = [
        "# Pourbaix (bulk)",
        f"U range:  {u_range[0]:.2f} … {u_range[1]:.2f} V vs SHE",
        f"pH range: {ph_range[0]:.1f} … {ph_range[1]:.1f}",
        f"phases:   {len(entries)}",
        "",
        "phase coverage (% of grid):",
    ]
    # Sort by coverage descending; phases with zero coverage still
    # listed so the caller can see what was considered.
    for idx, count in sorted(counts.items(), key=lambda item: -item[1]):
        entry = entries[idx]
        pct = 100.0 * count / total
        marker = " (aq)" if entry.aqueous else ""
        lines.append(
            f"  {entry.material_id:<32s} {entry.formula:<10s}{marker:<5s} {pct:5.1f}%"
        )
    lines.append("")
    lines.append(
        "NOTE: v1 Pourbaix is a grid-min of the standard chemical "
        "potential; ion-activity corrections and entropy-of-dissolution "
        "terms are not applied. Use pymatgen.PourbaixDiagram for "
        "publication-quality work."
    )
    return "\n".join(lines)


_POURBAIX_COLORS = (
    "#cfe2f3",
    "#fff2cc",
    "#d9ead3",
    "#f4cccc",
    "#d9d2e9",
    "#ead1dc",
    "#fce5cd",
    "#c9daf8",
)


def _render_pourbaix_svg(
    dominant: list[list[int]],
    *,
    entries: list[PourbaixEntry],
    u_range: tuple[float, float],
    ph_range: tuple[float, float],
) -> str:
    """Render the Pourbaix dominant-phase map as a coloured grid.

    A flat tile-grid SVG: one filled rect per (pH, U) cell, coloured
    by the dominant phase index. The legend on the right names each
    phase. Suitable for quick visual inspection but not optimised
    for sharp domain boundaries (those would need contouring).
    """
    width = 560
    height = 360
    margin_left = 64
    margin_right = 160
    margin_top = 36
    margin_bottom = 48
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    if not dominant or not dominant[0]:
        return _empty_svg("no Pourbaix data")

    n_ph = len(dominant)
    n_u = len(dominant[0])
    cell_w = plot_w / n_u
    cell_h = plot_h / n_ph

    u_lo, u_hi = u_range
    ph_lo, ph_hi = ph_range

    svg_parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="11">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{(margin_left + plot_w / 2)}" y="20" text-anchor="middle" '
        f'font-size="13" font-weight="bold">Pourbaix (bulk) — '
        f"{len(entries)} phase(s)</text>",
    ]

    # Tiles. We collapse adjacent same-phase tiles in the same row
    # into one wider rect to keep the SVG small.
    for i in range(n_ph):
        j = 0
        while j < n_u:
            k = j
            phase = dominant[i][j]
            while k + 1 < n_u and dominant[i][k + 1] == phase:
                k += 1
            x = margin_left + j * cell_w
            y = margin_top + (n_ph - 1 - i) * cell_h
            w = (k - j + 1) * cell_w
            color = _POURBAIX_COLORS[phase % len(_POURBAIX_COLORS)]
            svg_parts.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" '
                f'height="{cell_h:.2f}" fill="{color}" stroke="none"/>'
            )
            j = k + 1

    # Axes (drawn over the tiles so they read clearly).
    svg_parts.append(
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" '
        f'y2="{height - margin_bottom}" stroke="#333"/>'
    )
    svg_parts.append(
        f'<line x1="{margin_left}" y1="{height - margin_bottom}" '
        f'x2="{margin_left + plot_w}" y2="{height - margin_bottom}" stroke="#333"/>'
    )
    svg_parts.append(
        f'<text x="{margin_left + plot_w / 2}" y="{height - 12}" text-anchor="middle">pH</text>'
    )
    svg_parts.append(
        f'<text x="14" y="{margin_top + plot_h / 2}" text-anchor="middle" '
        f'transform="rotate(-90 14 {margin_top + plot_h / 2})">U (V vs SHE)</text>'
    )

    # Tick labels.
    for i in range(5):
        frac = i / 4.0
        pH = ph_lo + frac * (ph_hi - ph_lo)
        xpx = margin_left + frac * plot_w
        svg_parts.append(
            f'<line x1="{xpx}" y1="{height - margin_bottom}" '
            f'x2="{xpx}" y2="{height - margin_bottom + 5}" stroke="#333"/>'
        )
        svg_parts.append(
            f'<text x="{xpx}" y="{height - margin_bottom + 18}" '
            f'text-anchor="middle">{pH:.1f}</text>'
        )
        U = u_lo + frac * (u_hi - u_lo)
        ypx = margin_top + (1.0 - frac) * plot_h
        svg_parts.append(
            f'<line x1="{margin_left}" y1="{ypx}" x2="{margin_left - 5}" y2="{ypx}" stroke="#333"/>'
        )
        svg_parts.append(
            f'<text x="{margin_left - 8}" y="{ypx + 4}" text-anchor="end">{U:.2f}</text>'
        )

    # Legend.
    legend_x = margin_left + plot_w + 16
    used_phases = sorted({cell for row in dominant for cell in row})
    for slot, phase in enumerate(used_phases):
        entry = entries[phase]
        legend_y = margin_top + slot * 18
        color = _POURBAIX_COLORS[phase % len(_POURBAIX_COLORS)]
        svg_parts.append(
            f'<rect x="{legend_x}" y="{legend_y}" width="14" height="12" '
            f'fill="{color}" stroke="#333" stroke-width="0.5"/>'
        )
        marker = " (aq)" if entry.aqueous else ""
        label = _escape(entry.formula + marker)
        svg_parts.append(
            f'<text x="{legend_x + 20}" y="{legend_y + 10}">{label}</text>'
        )

    svg_parts.append("</svg>")
    return "".join(svg_parts)


def _empty_svg(message: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="240" '
        f'viewBox="0 0 480 240" font-family="sans-serif">'
        f'<rect width="100%" height="100%" fill="white"/>'
        f'<text x="240" y="120" text-anchor="middle" font-size="14" fill="#333">{_escape(message)}</text>'
        f"</svg>"
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


__all__ = [
    "PourbaixEntry",
    "PourbaixResult",
    "compute",
    "from_materials",
]
