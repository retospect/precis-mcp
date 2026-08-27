"""TOON renderings of pathway data — the LLM-facing tables.

Uses precis's own ``format.toon`` serialiser (braced ``{col⇥col}`` header,
TAB-separated homogeneous rows) so pathway output reads exactly like ``search``.
Numbers are pre-formatted to strings (2 dp) — TOON renders floats via ``repr``
otherwise, which spends tokens on ``0.7400000001``.

Imports precis, so this module is handler-side (not part of the precis-free
``runner``/``analysis``/``text_views`` set).
"""

from __future__ import annotations

import math
from typing import Any

from precis.format import toon
from precis.utils.handle_registry import try_format

from . import analysis

# Drill-down hint appended once a table actually carries a structure handle —
# see gripe 161576 (structure_refs was written by ingest.py but never surfaced
# anywhere an agent could read it back out).
_STRUCTURE_HINT = (
    "# {col} = precis structure handle for {what} relaxed geometry (slice 1b "
    "ingest). get(kind='structure', id=<handle>, view='atom') for per-atom "
    "fields, view='runs' for calc metadata."
)


def _structure_hint(col: str, what: str) -> str:
    return _STRUCTURE_HINT.format(col=col, what=what)


def _e(x: Any) -> str:
    return "" if x is None else f"{float(x):+.2f}"  # signed (relative energies)


def _b(x: Any) -> str:
    return "" if x is None else f"{float(x):.2f}"  # barriers (positive)


def _conf(low: Any) -> str:
    return "low" if low else "ok"


def _roots(meta: dict[str, Any]) -> tuple[str, str]:
    return analysis.roots(meta.get("graph") or {}, meta.get("results", {}))


def _structure_handle(refs: dict[str, Any], state: Any) -> str:
    """``structure_refs`` maps state -> `structure` ref_id (int); render the
    universal handle (``st<ref_id>``) an agent can hand straight to
    ``get(kind='structure', id=...)``. Blank when this state has no ingested
    structure (older pathway, preview-only run, or a skipped bad geometry)."""
    ref_id = refs.get(state)
    if ref_id is None:
        return ""
    return try_format("structure", ref_id) or ""


# ── single-pathway tables ───────────────────────────────────────────────
def intermediates_toon(meta: dict[str, Any]) -> str:
    graph = meta.get("graph") or {}
    nm = {n["id"]: n for n in graph.get("nodes", [])}
    order = meta.get("results", {}).get("pathway", list(nm))
    refs = meta.get("structure_refs") or {}
    rows = [
        {
            "state": s,
            "rel_eV": _e(nm.get(s, {}).get("rel_energy")),
            "std": _b(nm.get(s, {}).get("energy_std")),
            "conf": _conf(nm.get(s, {}).get("low_confidence")),
            "structure": _structure_handle(refs, s),
        }
        for s in order
        if s in nm
    ]
    table = toon.dump(rows, schema=["state", "rel_eV", "std", "conf", "structure"])
    if any(r["structure"] for r in rows):
        table += "\n" + _structure_hint("structure", "that state's")
    return table


def steps_toon(meta: dict[str, Any]) -> str:
    graph = meta.get("graph") or {}
    refs = meta.get("structure_refs") or {}

    def _endpoints(source: Any, target: Any) -> str:
        a, b = _structure_handle(refs, source), _structure_handle(refs, target)
        if not a and not b:
            return ""
        return f"{a or '?'}→{b or '?'}"

    rows = [
        {
            "reaction": f"{e['source']}→{e['target']}",
            "Ea_eV": _b(e.get("barrier")),
            "std": _b(e.get("barrier_std")),
            "dE_eV": _e(e.get("delta_e")),
            "conf": _conf(e.get("low_confidence")),
            "structures": _endpoints(e["source"], e["target"]),
        }
        for e in analysis._reaction_edges(graph)
    ]
    table = toon.dump(
        rows, schema=["reaction", "Ea_eV", "std", "dE_eV", "conf", "structures"]
    )
    if any(r["structures"] for r in rows):
        table += "\n" + _structure_hint("structures", "each side's (source→target)")
    return table


def warnings_toon(meta: dict[str, Any]) -> str:
    warns = meta.get("warnings") or []
    if not warns:
        return "no warnings — states/barriers converged and within tolerance."
    rows = [{"warning": w} for w in warns]
    return toon.dump(rows, schema=["warning"])


def analysis_text(meta: dict[str, Any]) -> str:
    graph = meta.get("graph") or {}
    root, target = _roots(meta)
    r = meta.get("results", {})
    n = r.get("n_samples", "?")
    models = ",".join(r.get("models", [])) or r.get("backend", "?")

    rl = analysis.rate_limiting(graph, root, target)
    span = analysis.energetic_span(graph, root, target)
    head = [f"{root} → {target}  ({models}, {n} samples)", ""]
    if rl:
        flag = "  [LOW CONFIDENCE]" if rl["low_confidence"] else ""
        head.append(
            f"rate-limiting: {rl['step']}   Ea = {_b(rl['ea'])} ± {_b(rl['std'])} eV{flag}"
        )
        if rl["low_confidence"]:
            head.append(
                "  → spread exceeds tolerance; escalate this step's fidelity "
                "before trusting it."
            )
    if span is not None:
        head.append(f"energetic span (whole-path apparent barrier): {_b(span)} eV")
    head.append("")

    ranked = analysis.barriers_ranked(graph)
    brows = [
        {
            "reaction": s["reaction"],
            "Ea_eV": _b(s["ea"]),
            "std": _b(s["std"]),
            "conf": s["conf"],
        }
        for s in ranked
    ]
    head.append("barriers (descending):")
    head.append(toon.dump(brows, schema=["reaction", "Ea_eV", "std", "conf"]))

    sel = analysis.selectivity(graph, root, target)
    if len(sel) > 1:  # only meaningful when the root branches
        srows = [
            {
                "entry_step": s["entry_step"],
                "entry_Ea": _b(s["entry_ea"]),
                "role": "target-path" if s["on_target_path"] else "competing",
            }
            for s in sel
        ]
        head += [
            "",
            "selectivity (first steps out of root, lowest entry wins):",
            toon.dump(srows, schema=["entry_step", "entry_Ea", "role"]),
        ]
    return "\n".join(head)


def step_view(meta: dict[str, Any], pw_handle: str, edge: dict[str, Any]) -> str:
    """Focused single-step view for a ``pw<id>~<source>→<target>`` selector
    (Simulation step deep-links, docs/backlog/quest-dossier-dialectic.md).
    ``edge`` is one row of ``meta['graph']['links']`` — see
    :func:`precis_pathway.analysis._reaction_edges` for the shape."""
    refs = meta.get("structure_refs") or {}
    source, target = edge["source"], edge["target"]
    row = {
        "reaction": f"{source}→{target}",
        "Ea_eV": _b(edge.get("barrier")),
        "std": _b(edge.get("barrier_std")),
        "dE_eV": _e(edge.get("delta_e")),
        "conf": _conf(edge.get("low_confidence")),
        "kind": edge.get("kind") or "reaction",
    }
    table = toon.dump(
        [row], schema=["reaction", "Ea_eV", "std", "dE_eV", "conf", "kind"]
    )
    lines = [f"step {pw_handle}~{row['reaction']}", table]
    a, b = _structure_handle(refs, source), _structure_handle(refs, target)
    if a or b:
        lines.append(f"structures: {a or '?'} → {b or '?'}")
        lines.append(_structure_hint("structures", "each side's (source→target)"))
    return "\n".join(lines)


# ── microkinetics digest (Eyring rates, honest v1) ───────────────────────
# CODATA-adjacent constants — good enough for an order-of-magnitude digest,
# not a metrology claim.
_KB_EV_PER_K = 8.617e-5  # Boltzmann constant, eV/K
_H_EV_S = 4.136e-15  # Planck constant, eV·s


def _eyring_rate(barrier_eV: Any, T_k: float = 300.0) -> float:
    """Eyring transition-state-theory rate constant k = (k_B·T/h)·exp(−Ea/k_B·T)
    for an electronic barrier (no ZPE/entropy correction — see the caveat line
    in :func:`kinetics_text`). ``float('nan')`` for a missing barrier, so
    downstream formatting renders '—' instead of raising."""
    if barrier_eV is None:
        return float("nan")
    kt = _KB_EV_PER_K * T_k
    try:
        return (kt / _H_EV_S) * math.exp(-float(barrier_eV) / kt)
    except (OverflowError, ValueError):
        return float("nan")


def _ea_str(x: Any) -> str:
    return "—" if x is None else f"{float(x):.2f}"


def _rate_str(x: float) -> str:
    return "—" if math.isnan(x) else f"{x:.2e}"


def kinetics_text(meta: dict[str, Any], T_k: float = 300.0) -> str:
    """Microkinetics digest for a computed pathway (Simulation step deep-links,
    docs/backlog/quest-dossier-dialectic.md): Eyring rates + residence times +
    the rate-limiting step, so a dossier can cite ``pw<id>~<label>`` and argue
    "the slow step is [...], τ ≈ ...".

    Honest v1 only: no steady-state coverages, no degree-of-rate-control — both
    need a full microkinetic solve (site balance, reverse rates) this view does
    not attempt; faking them would be worse than not having them.
    """
    graph = meta.get("graph") or {}
    root, target = _roots(meta)
    path = analysis.reaction_path(graph, root, target)
    steps = analysis._path_steps(graph, path) or analysis._reaction_edges(graph)

    rows = []
    for e in steps:
        ea = e.get("barrier")
        k = _eyring_rate(ea, T_k)
        tau = 1.0 / k if k == k and k > 0 else float("nan")  # k==k rejects nan
        rows.append(
            {
                "reaction": f"{e['source']}→{e['target']}",
                "Ea_eV": _ea_str(ea),
                "k_f_/s": _rate_str(k),
                "tau_s": _rate_str(tau),
                "conf": _conf(e.get("low_confidence")),
            }
        )
    table = toon.dump(rows, schema=["reaction", "Ea_eV", "k_f_/s", "tau_s", "conf"])

    lines = [
        f"{root} → {target} — microkinetics (Eyring TST, T = {T_k:.0f} K)",
        "",
        table,
        "",
    ]
    rl = analysis.rate_limiting(graph, root, target)
    if rl:
        k_rl = _eyring_rate(rl["ea"], T_k)
        tau_rl = 1.0 / k_rl if k_rl == k_rl and k_rl > 0 else float("nan")
        flag = "  [LOW CONFIDENCE]" if rl["low_confidence"] else ""
        lines.append(
            f"Rate-limiting step: {rl['step']} (Ea = {_ea_str(rl['ea'])} eV, "
            f"τ ≈ {_rate_str(tau_rl)} s){flag}"
        )
        lines.append("")
    lines.append(
        "Barriers are electronic (NEB, no ZPE/entropy). Eyring pre-exponential "
        "A = k_B·T/h ≈ 6.2e12 /s at 300 K; residence times ignore reverse "
        "reactions and surface-coverage effects."
    )
    return "\n".join(lines)


# ── cross-candidate compare (interleaved profile) ───────────────────────
def compare_toon(candidates: list[dict[str, Any]]) -> str:
    """`candidates`: [{slug, lever, graph, root, target}]. Rows = candidates.
    When they share a network, columns interleave state(rel eV) + ‡(barrier Eₐ)
    along the reaction coordinate; always: RATE (max step), SPAN, conf. Sorted
    by RATE ascending (best first)."""
    if not candidates:
        return "no computed candidates to compare."

    profiles: list[dict[str, Any]] = []
    for c in candidates:
        path, cols = analysis.profile_positions(c["graph"], c["root"], c["target"])
        summ = analysis.summarize(c["graph"], c["root"], c["target"])
        profiles.append({"c": c, "path": path, "cols": cols, "summ": summ})

    paths = {tuple(p["path"]) for p in profiles}
    aligned = len(paths) == 1 and all(p["path"] for p in profiles)

    def _row_scalars(p: dict[str, Any]) -> dict[str, Any]:
        rl = p["summ"]["rate_limiting"] or {}
        return {
            "cand": p["c"]["slug"],
            "lever": p["c"].get("lever", ""),
            "RATE": _b(rl.get("ea")),
            "SPAN": _b(p["summ"]["span"]),
            "conf": _conf(rl.get("low_confidence")),
        }

    if not aligned:
        rows = [_row_scalars(p) for p in profiles]
        rows.sort(key=lambda r: (r["RATE"] == "", r["RATE"]))
        note = "# networks differ — scalar comparison only (RATE = rate-limiting Eₐ)\n"
        return note + toon.dump(rows, schema=["cand", "lever", "RATE", "SPAN", "conf"])

    # aligned: build interleaved columns from the shared coordinate.
    template = profiles[0]["cols"]
    legend, col_names = [], []
    for col in template:
        if col["kind"] == "state":
            col_names.append(col["label"])
        else:
            col_names.append(col["pos"])
            legend.append(f"{col['pos']} {col['label']}")
    schema = ["cand", "lever", *col_names, "RATE", "SPAN", "conf"]

    rows = []
    for p in profiles:
        row = _row_scalars(p)
        for col, name in zip(p["cols"], col_names):
            row[name] = _e(col["value"]) if col["kind"] == "state" else _b(col["value"])
        rows.append(row)
    rows.sort(key=lambda r: (r["RATE"] == "", r["RATE"]))

    head = "# ‡ = step barrier Eₐ; state cols = rel eV vs root.  " + "  ".join(legend)
    return head + "\n" + toon.dump(rows, schema=schema)
