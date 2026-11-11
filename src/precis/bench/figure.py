"""Recall-vs-tokens figure for ``scripts/bench report --figure``.

One series per (results file, arm): x = median tokens to first gold over the
tasks that reached it, y = mean recall@k, drawn as a vertical run through
k = 1, 3, 5, 10 (the median does not depend on k). The recall@5 marker is the
headline; it is hollow when the arm has censored tasks, which the label counts
(their budget-capped tokens are left out of the median, as in the tables).
Colours: the dataviz reference palette in fixed slot order; more than eight
series are refused rather than cycled.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from precis.bench import runner

KS = (1, 3, 5, 10)
SERIES_COLORS = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
)
_INK, _MUTED, _GRID, _SURFACE = "#0b0b0b", "#898781", "#e1e0d9", "#fcfcfb"


def figure_points(runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per (run, arm) series with answerable tasks that reached gold:
    ``label``, ``x`` median tokens, ``recall`` {k: mean}, ``censored``, ``n``."""
    names = [str(r["header"].get("caller", {}).get("name", "ladder")) for r in runs]
    multi = len(runs) > 1
    out: list[dict[str, Any]] = []
    for run, caller in zip(runs, names, strict=True):
        rows = run["rows"]
        for arm in runner.ALL_ARMS:
            sel = [
                r
                for r in rows
                if r["arm"] == arm and not r["abstention"] and r.get("applicable", True)
            ]
            agg = runner.aggregate(rows, arm)
            if not sel or agg["median_tokens"] is None:
                continue
            recall = {}
            for k in KS:
                vals = [
                    r[f"recall_at_{k}"]
                    for r in sel
                    if r.get(f"recall_at_{k}") is not None
                ]
                if vals:
                    recall[k] = sum(vals) / len(vals)
            if not recall:
                continue
            out.append(
                {
                    "label": f"{arm} ({caller})" if multi else arm,
                    "x": float(agg["median_tokens"]),
                    "recall": recall,
                    "censored": int(agg["censored"]),
                    "n": int(agg["n"]),
                }
            )
    return out


def render_figure(runs: Sequence[Mapping[str, Any]], fmt: str = "svg") -> bytes:
    """Render the figure as ``svg`` or ``png`` bytes."""
    if fmt not in ("svg", "png"):
        raise ValueError(f"figure format must be svg or png, not {fmt!r}")
    pts = figure_points(runs)
    if not pts:
        raise ValueError("no arm in the results reached gold: nothing to plot")
    if len(pts) > len(SERIES_COLORS):
        raise ValueError(
            f"{len(pts)} series exceed the {len(SERIES_COLORS)}-colour palette; "
            "pass fewer results files"
        )
    fig = Figure(figsize=(8, 5), facecolor=_SURFACE)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111, facecolor=_SURFACE)
    for color, p in zip(SERIES_COLORS, pts, strict=False):
        ks = sorted(p["recall"])
        ys = [p["recall"][k] for k in ks]
        ax.plot([p["x"]] * len(ks), ys, color=color, lw=2, zorder=2)
        ax.scatter([p["x"]] * len(ks), ys, s=14, color=color, zorder=3)
        if 5 in p["recall"]:
            hollow = p["censored"] > 0
            ax.scatter(
                [p["x"]],
                [p["recall"][5]],
                s=70,
                marker="o",
                facecolors=_SURFACE if hollow else color,
                edgecolors=color,
                linewidths=2,
                zorder=4,
                label=f"{p['label']}"
                + (f" - {p['censored']}/{p['n']} censored" if hollow else ""),
            )
    ax.set_xlabel("median tokens to first gold (tasks that reached it)", color=_INK)
    ax.set_ylabel("mean recall@k (k = 1, 3, 5, 10 along each line)", color=_INK)
    ax.set_ylim(0, 1.05)
    ax.set_xlim(left=0)
    ax.grid(color=_GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_MUTED)
    ax.tick_params(colors=_MUTED)
    ax.legend(
        title="arm (hollow = has censored tasks)",
        fontsize=8,
        frameon=False,
        loc="center left",
        bbox_to_anchor=(1.01, 0.5),
    )
    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, bbox_inches="tight", metadata=_meta(fmt))
    return buf.getvalue()


def _meta(fmt: str) -> dict[str, Any]:
    # fixed metadata keeps the SVG/PNG byte-reproducible
    return {"Date": None} if fmt == "svg" else {"Software": None}


def write_figure(runs: Sequence[Mapping[str, Any]], path: Path) -> Path:
    """Write the figure to ``path``; the suffix (``.svg`` / ``.png``) picks
    the format."""
    fmt = path.suffix.lower().lstrip(".")
    data = render_figure(runs, fmt)
    path.write_bytes(data)
    return path
