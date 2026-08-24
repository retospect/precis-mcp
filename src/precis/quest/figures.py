"""Static (export-facing) figure renderers for the quest layer.

Twin of the web SVG renderers (``build_frontier_scatter`` +
``quest_detail.html.j2``'s inline scatter; ``precis_pathway.analysis``'s
profile view): those render **interactively** for the reader, these render
**once, to PNG bytes**, for a draft's ``figure`` chunk / an export. Object-
oriented matplotlib only (``matplotlib.figure.Figure`` +
``FigureCanvasAgg``) — never ``pyplot`` (no global backend state to step
on when multiple renders run in one process).

Every renderer has a paired **snapshot builder** that freezes the exact
numbers behind the pixels into a small JSON-serializable dict (schema
below). The snapshot rides along in the figure chunk's
``meta.figure.data_package`` so the export-time data-package appendix
(``draft-pathway-figures-data-package`` backlog item) always matches what
was plotted — no re-derivation, no drift between a chart and its numbers.

Marker grammar (pareto figure) mirrors :func:`precis.quest.frontier.
build_frontier_scatter` / the quest hub template exactly: **shape** = Pareto
frontier membership (star vs circle), **fill** = trust/convergence (solid
vs hollow), **color** = band (confirmed blue / provisional orange / grey
unconverged).

Snapshot schema (contract with the export side — do not deviate)::

    {
      "schema": 1,
      "source": {"kind": "quest"|"pathway", "ref_id": int, "handle": str, "title": str},
      "generated_at": "<iso8601 UTC>",
      "autocatpath_version": str | None,
      "precis": {"version": str, "sha": str | None},
      "params": {...},
      "columns": [<ordered row-key strings>],
      "rows": [{<column key>: value, ...}, ...],
    }
"""

from __future__ import annotations

import io
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

if TYPE_CHECKING:
    from precis.quest.frontier import FrontierResult, FrontierScatter
    from precis.store import Store

#: Marker grammar shared with the web scatter (quest_detail.html.j2).
_BAND_COLOR_CONFIRMED = "#0369a1"
_BAND_COLOR_PROVISIONAL = "#c2410c"
_BAND_COLOR_UNCONVERGED = "#94a3b8"
_RATE_LIMITING_COLOR = "#c2410c"
_STATE_COLOR = "#0369a1"
_CONNECTOR_COLOR = "#475569"

_FRONTIER_MARKER_SIZE = 160.0
_OFF_FRONTIER_MARKER_SIZE = 45.0


def _precis_provenance() -> dict[str, Any]:
    """``{"version", "sha"}`` for the running precis build.

    Mirrors :func:`precis.nanopub.mint._software_provenance`'s env-first
    resolution chain (``PRECIS_GIT_SHA`` baked into images, else live-
    checkout git state) without importing the (heavier) nanopub package —
    this module has no other nanopub dependency.
    """
    from precis import __version__

    sha = os.environ.get("PRECIS_GIT_SHA", "").strip() or None
    if not sha or sha.lower() == "unknown":
        try:
            from precis.handlers.skill import _SOURCE_GIT_INFO

            sha = _SOURCE_GIT_INFO.get("git_sha")
        except Exception:  # pragma: no cover - status surface unavailable
            sha = None
    return {"version": __version__, "sha": sha}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _fig_to_png(fig: Figure) -> bytes:
    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
    return buf.getvalue()


# --------------------------------------------------------------------------
# Pareto (quest) figure
# --------------------------------------------------------------------------


def render_pareto_png(scatter: FrontierScatter, *, title: str | None = None) -> bytes:
    """Render a :class:`~precis.quest.frontier.FrontierScatter` to PNG bytes.

    Marker grammar mirrors the web scatter exactly: ``*`` (large) on the
    frontier, ``o`` (small) off it; filled when converged and trusted,
    hollow (white face, coloured edge) otherwise; colour by band (confirmed
    blue / provisional orange / grey when not converged).
    """
    fig = _pareto_figure(scatter, title=title)
    return _fig_to_png(fig)


def _pareto_figure(scatter: FrontierScatter, *, title: str | None = None) -> Figure:
    """The :class:`matplotlib.figure.Figure` :func:`render_pareto_png`
    rasterizes — split out so a caller (or a test) can inspect the ``Axes``
    (e.g. its axis labels) without round-tripping through PNG bytes."""
    fig = Figure(figsize=(6.4, 4.4), dpi=200)
    ax = fig.add_subplot(111)

    for p in scatter.points:
        band = p.get("band")
        converged = bool(p.get("converged"))
        on_frontier = bool(p.get("on_frontier"))
        untrusted = bool(p.get("untrusted"))
        if band == "provisional":
            color = _BAND_COLOR_PROVISIONAL
        elif not converged:
            color = _BAND_COLOR_UNCONVERGED
        else:
            color = _BAND_COLOR_CONFIRMED
        marker = "*" if on_frontier else "o"
        size = _FRONTIER_MARKER_SIZE if on_frontier else _OFF_FRONTIER_MARKER_SIZE
        filled = converged and not untrusted
        face = color if filled else "white"
        ax.scatter(
            [p["x"]],
            [p["y"]],
            marker=marker,
            s=size,
            facecolor=face,
            edgecolor=color,
            linewidths=1.5,
            zorder=3,
        )

    # "Which way is better" suffix (optunacy-style) — reuses the SAME
    # scatter.x_better/y_better `better_arrow_for` already computed onto the
    # scatter (:func:`~precis.quest.frontier.build_frontier_scatter`), so
    # the PNG twin's labels never drift from the web scatter's mapping.
    x_label = scatter.x_label + (f"  {scatter.x_better}" if scatter.x_better else "")
    y_label = scatter.y_label + (f"  {scatter.y_better}" if scatter.y_better else "")
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    if title:
        ax.set_title(title, fontsize=10)

    legend_elems = [
        Line2D(
            [0],
            [0],
            marker="*",
            linestyle="none",
            markerfacecolor=_BAND_COLOR_CONFIRMED,
            markeredgecolor=_BAND_COLOR_CONFIRMED,
            markersize=11,
            label="Frontier (confirmed)",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            markerfacecolor=_BAND_COLOR_CONFIRMED,
            markeredgecolor=_BAND_COLOR_CONFIRMED,
            markersize=6,
            label="Dominated (confirmed)",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            markerfacecolor=_BAND_COLOR_PROVISIONAL,
            markeredgecolor=_BAND_COLOR_PROVISIONAL,
            markersize=6,
            label="Provisional",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            markerfacecolor="white",
            markeredgecolor="#334155",
            markersize=6,
            label="Hollow = untrusted / unconverged",
        ),
    ]
    ax.legend(handles=legend_elems, loc="best", fontsize=6.5, frameon=True)
    ax.grid(True, linewidth=0.4, alpha=0.4)

    return fig


def build_pareto_snapshot(
    store: Store, quest_ref: Any, fr: FrontierResult
) -> dict[str, Any]:
    """The data-package snapshot for a quest's Pareto figure.

    Rebuilds the same :func:`~precis.quest.frontier.build_frontier_scatter`
    the renderer plots (cheap, pure — no extra store hit) so "one row per
    plotted point" is exactly the set of points a caller actually sees on
    the PNG. ``fr`` (a :class:`~precis.quest.frontier.FrontierResult`) is
    passed in rather than recomputed — see :func:`quest_pareto_figure` for
    the convenience wrapper that calls :func:`~precis.quest.frontier.
    quest_frontier` once and builds both artifacts from it. The axis pair
    is the quest's own :func:`~precis.quest.frontier.plot_axes_for` pick
    (its first two declared ``rubric_objectives`` when it declares >= 2,
    else the hub-v2 fallback) — kept in sync with the web scatter and the
    PNG's marker grammar, not a fixed ``barrier``/``energy`` pair anymore.
    """
    from precis.utils import handle_registry

    from . import frontier as frontier_mod

    objectives = frontier_mod._objectives_for(store, quest_ref.id)
    x_measure, y_measure, _x_label, _y_label = frontier_mod.plot_axes_for(
        getattr(quest_ref, "meta", None), objectives
    )
    scatter = frontier_mod.build_frontier_scatter(
        [*fr.frontier, *fr.dominated],
        provisional=fr.provisional,
        frontier_ref_ids={c.ref_id for c in fr.frontier},
        x_measure=x_measure,
        y_measure=y_measure,
        x_label=_x_label,
        y_label=_y_label,
        objectives=objectives,
    )
    points = scatter.points if scatter is not None else []

    candidates_by_id = {c.ref_id: c for c in (*fr.frontier, *fr.dominated)}
    provisional_by_id = {pc.candidate.ref_id: pc for pc in fr.provisional}

    extra_axis_keys = [
        k for k in (x_measure, y_measure) if k not in ("barrier", "energy")
    ]
    columns = [
        "handle",
        "name",
        "band",
        "on_frontier",
        "converged",
        "trusted",
        "tier",
        "barrier",
        "energy",
        *extra_axis_keys,
    ]

    rows: list[dict[str, Any]] = []
    for p in points:
        ref_id = p["ref_id"]
        if p.get("band") == "provisional":
            pc = provisional_by_id.get(ref_id)
            measures = pc.measures if pc is not None else {}
            flags = pc.candidate.flags if pc is not None else {}
        else:
            c = candidates_by_id.get(ref_id)
            measures = c.measures if c is not None else {}
            flags = c.flags if c is not None else {}
        # "trusted" must track EVERY trust gate feeding the plotted axes,
        # not just the barrier lane: post kinetics-cutover a row can be
        # provisional purely because kinetics is untrusted while its
        # barrier is fine. False if any present gate is False; None when
        # no gate has reported yet.
        gate_vals = [
            v
            for v in (flags.get("barrier_trusted"), flags.get("kinetics_trusted"))
            if isinstance(v, bool)
        ]
        row: dict[str, Any] = {
            "handle": p["handle"],
            "name": p["name"],
            "band": p["band"],
            "on_frontier": p["on_frontier"],
            "converged": p["converged"],
            "trusted": all(gate_vals) if gate_vals else None,
            "tier": flags.get("barrier_tier") or flags.get("tier"),
            "barrier": measures.get("barrier"),
            "energy": measures.get("energy"),
        }
        for k in extra_axis_keys:
            row[k] = measures.get(k)
        rows.append(row)

    from precis.quest.compute import _autocatpath_engine_token

    return {
        "schema": 1,
        "source": {
            "kind": "quest",
            "ref_id": quest_ref.id,
            "handle": handle_registry.try_format("quest", quest_ref.id),
            "title": quest_ref.title,
        },
        "generated_at": _iso_now(),
        "autocatpath_version": _autocatpath_engine_token(),
        "precis": _precis_provenance(),
        "params": {
            "objectives": [{"key": k, "sense": s} for k, s in objectives],
            "x_measure": x_measure,
            "y_measure": y_measure,
        },
        "columns": columns,
        "rows": rows,
    }


def quest_pareto_figure(store: Store, quest_ref: Any) -> tuple[bytes, dict[str, Any]]:
    """Convenience: one :func:`~precis.quest.frontier.quest_frontier` call,
    both the PNG and its data-package snapshot.

    Raises :class:`ValueError` when fewer than two candidates are
    plottable (mirrors :func:`~precis.quest.frontier.build_frontier_scatter`'s
    own ``None`` guard — a figure with no shape isn't worth minting)."""
    from . import frontier as frontier_mod

    fr = frontier_mod.quest_frontier(store, quest_ref.id)
    objectives = frontier_mod._objectives_for(store, quest_ref.id)
    x_measure, y_measure, x_label, y_label = frontier_mod.plot_axes_for(
        getattr(quest_ref, "meta", None), objectives
    )
    scatter = frontier_mod.build_frontier_scatter(
        [*fr.frontier, *fr.dominated],
        provisional=fr.provisional,
        frontier_ref_ids={c.ref_id for c in fr.frontier},
        x_measure=x_measure,
        y_measure=y_measure,
        x_label=x_label,
        y_label=y_label,
        objectives=objectives,
    )
    if scatter is None:
        raise ValueError(
            f"quest {quest_ref.id}: fewer than 2 plottable candidates — "
            "not enough data for a Pareto figure yet"
        )
    png = render_pareto_png(scatter, title=quest_ref.title)
    snapshot = build_pareto_snapshot(store, quest_ref, fr)
    return png, snapshot


# --------------------------------------------------------------------------
# Profile (pathway) figure
# --------------------------------------------------------------------------


def _profile_root_target(
    graph: dict[str, Any], results: dict[str, Any]
) -> tuple[str, str]:
    """Best-effort ``(root, target)`` node ids — :func:`precis_pathway.
    analysis.roots` when ``results`` carries the ``pathway``/``substrate``/
    ``target`` keys it needs, else the graph's first/last node as a
    fallback (a hand-built synthetic graph, or a pathway persisted without
    ``results``)."""
    nodes = graph.get("nodes") or []
    root = target = ""
    if results:
        try:
            from precis_pathway.analysis import roots as _roots

            root, target = _roots(graph, results)
        except Exception:  # pragma: no cover - defensive
            root = target = ""
    if not root and nodes:
        root = nodes[0].get("id", "")
    if not target and nodes:
        target = nodes[-1].get("id", "")
    return root, target


def _profile_positions(
    graph: dict[str, Any], root: str, target: str
) -> list[dict[str, Any]]:
    """The interleaved ``[state, ‡, state, ‡, …]`` row list this module's
    snapshot/renderer share — mirrors the selection
    :func:`precis_pathway.analysis.profile_positions` does (shortest
    root→target path, supply bridges skipped as barrier-less connectors),
    but keeps ``energy``/``rel_energy``/``barrier``/``delta_e`` as separate
    columns instead of collapsing them to one ``value`` (the data-package
    needs all four, not just the plotted axis)."""
    from precis_pathway.analysis import reaction_path

    path = reaction_path(graph, root, target)
    node_map = {n.get("id"): n for n in graph.get("nodes") or []}
    links = graph.get("links") or []

    def _edge(a: str, b: str) -> dict[str, Any] | None:
        for e in links:
            if e.get("source") == a and e.get("target") == b:
                return e
        return None

    rows: list[dict[str, Any]] = []
    for i, s in enumerate(path):
        node = node_map.get(s, {})
        rows.append(
            {
                "pos": f"s{i}",
                "kind": "state",
                "label": s,
                "energy": node.get("energy"),
                "rel_energy": node.get("rel_energy"),
                "barrier": None,
                "delta_e": None,
                "low_confidence": bool(node.get("low_confidence")),
            }
        )
        if i < len(path) - 1:
            e = _edge(s, path[i + 1])
            if e is not None and e.get("kind") != "supply":
                rows.append(
                    {
                        "pos": f"‡{i + 1}",
                        "kind": "ts",
                        "label": f"{s}→{path[i + 1]}",
                        "energy": None,
                        "rel_energy": None,
                        "barrier": e.get("barrier"),
                        "delta_e": e.get("delta_e"),
                        "low_confidence": bool(e.get("low_confidence")),
                    }
                )
    return rows


def render_profile_png(
    rows: list[dict[str, Any]], *, title: str | None = None
) -> bytes:
    """Render an energy-profile diagram from :func:`_profile_positions`
    rows: horizontal levels at each state's ``rel_energy``, dashed
    connectors rising through each TS apex, the rate-limiting step (max
    barrier) highlighted, low-confidence states dashed."""
    states = [r for r in rows if r["kind"] == "state"]
    if not states:
        raise ValueError("no plottable states in this profile")
    state_idx = {r["label"]: i for i, r in enumerate(states)}

    ts_rows = [r for r in rows if r["kind"] == "ts"]
    finite_ts = [r for r in ts_rows if isinstance(r.get("barrier"), (int, float))]
    rate_limiting_pos = (
        max(finite_ts, key=lambda r: r["barrier"])["pos"] if finite_ts else None
    )

    fig = Figure(figsize=(6.4, 4.4), dpi=200)
    ax = fig.add_subplot(111)
    half_w = 0.3

    for i, r in enumerate(states):
        y = r.get("rel_energy")
        if y is None:
            continue
        linestyle = "dashed" if r.get("low_confidence") else "solid"
        ax.plot(
            [i - half_w, i + half_w],
            [y, y],
            color=_STATE_COLOR,
            linewidth=2.5,
            linestyle=linestyle,
            zorder=3,
        )
        ax.annotate(
            f"{r['label']}\n{y:g}",
            (i, y),
            textcoords="offset points",
            xytext=(0, 8),
            ha="center",
            fontsize=7,
        )

    for r in ts_rows:
        try:
            a, b = r["label"].split("→", 1)
        except ValueError:  # pragma: no cover - defensive
            continue
        ia, ib = state_idx.get(a), state_idx.get(b)
        ea = states[ia].get("rel_energy") if ia is not None else None
        barrier = r.get("barrier")
        if ia is None or ib is None or ea is None or barrier is None:
            continue
        apex_y = ea + barrier
        apex_x = (ia + ib) / 2.0
        eb = states[ib].get("rel_energy")
        is_rl = r["pos"] == rate_limiting_pos
        color = _RATE_LIMITING_COLOR if is_rl else _CONNECTOR_COLOR
        linewidth = 2.0 if is_rl else 1.2
        xs = [ia + half_w, apex_x]
        ys = [ea, apex_y]
        if eb is not None:
            xs.append(ib - half_w)
            ys.append(eb)
        ax.plot(
            xs,
            ys,
            color=color,
            linewidth=linewidth,
            linestyle="dashed",
            zorder=2,
        )
        ax.annotate(
            f"Eₐ={barrier:g}",
            (apex_x, apex_y),
            textcoords="offset points",
            xytext=(0, 6),
            ha="center",
            fontsize=6.5,
            color=color,
        )

    ax.set_xticks(range(len(states)))
    ax.set_xticklabels([r["label"] for r in states], fontsize=7)
    ax.set_xlabel("Reaction coordinate")
    ax.set_ylabel("Relative energy (eV)")
    if title:
        ax.set_title(title, fontsize=10)
    ax.grid(True, axis="y", linewidth=0.4, alpha=0.4)

    return _fig_to_png(fig)


def build_profile_snapshot(store: Store, pathway_ref: Any) -> dict[str, Any]:
    """The data-package snapshot for a pathway's energy-profile figure.

    ``store`` is accepted for symmetry with :func:`build_pareto_snapshot`
    (and so a future revision can resolve provenance the ref alone doesn't
    carry) but unused today — everything needed already lives on
    ``pathway_ref.meta`` (writers: ``precis_pathway.persist``/
    ``_dispatch_common``)."""
    from precis.utils import handle_registry

    meta = pathway_ref.meta or {}
    graph = meta.get("graph") or {}
    results = meta.get("results") or {}
    root, target = _profile_root_target(graph, results)
    rows = _profile_positions(graph, root, target)

    config = meta.get("config") or {}
    mlip_cfg = config.get("mlip") or {}
    search_cfg = config.get("search") or {}

    return {
        "schema": 1,
        "source": {
            "kind": "pathway",
            "ref_id": pathway_ref.id,
            "handle": handle_registry.try_format("pathway", pathway_ref.id),
            "title": pathway_ref.title,
        },
        "generated_at": _iso_now(),
        "autocatpath_version": meta.get("autocatpath_version"),
        "precis": _precis_provenance(),
        "params": {
            "tier": meta.get("tier"),
            "template": config.get("template"),
            "mlip": {
                "backend": mlip_cfg.get("backend"),
                "model": mlip_cfg.get("model"),
                "device": mlip_cfg.get("device"),
                "dtype": mlip_cfg.get("dtype"),
            },
            "search": {
                "neb_schedule": search_cfg.get("neb_schedule"),
                "neb_optimizer": search_cfg.get("neb_optimizer"),
                "screening": search_cfg.get("screening"),
            },
            "content_key": meta.get("content_key"),
        },
        "columns": [
            "pos",
            "kind",
            "label",
            "energy",
            "rel_energy",
            "barrier",
            "delta_e",
            "low_confidence",
        ],
        "rows": rows,
    }


def pathway_profile_figure(
    store: Store, pathway_ref: Any
) -> tuple[bytes, dict[str, Any]]:
    """Convenience: one :func:`_profile_positions` walk, both the PNG and
    its data-package snapshot."""
    meta = pathway_ref.meta or {}
    graph = meta.get("graph") or {}
    results = meta.get("results") or {}
    root, target = _profile_root_target(graph, results)
    rows = _profile_positions(graph, root, target)
    png = render_profile_png(rows, title=pathway_ref.title)
    snapshot = build_profile_snapshot(store, pathway_ref)
    return png, snapshot


__all__ = [
    "build_pareto_snapshot",
    "build_profile_snapshot",
    "pathway_profile_figure",
    "quest_pareto_figure",
    "render_pareto_png",
    "render_profile_png",
]
