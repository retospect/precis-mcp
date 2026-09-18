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
import re
from collections import Counter
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


#: Collapses a flat warning string's numeric literals to a single ``N`` so
#: repeated templates that differ only by e.g. an fmax reading, a seed index,
#: or an eV value count as one template — see :func:`_collapse_template`.
#: The negative lookbehind excludes a digit run directly glued to a letter
#: (``NH2``, ``N2H4``) — those are species-formula tokens, not a
#: measurement, and must stay distinct (``NH2`` must never collapse onto
#: ``NH3``).
_NUM_RE = re.compile(r"(?<![A-Za-z])[0-9]+(?:\.[0-9]+)?")

#: Cap on distinct message templates rendered by ``warnings_toon``'s
#: "messages" section (docs/backlog/pathway-conditions-effects-report.md
#: "Warnings" fix 1) — a long tail of one-off templates still shows as a
#: trailing "(+K more templates)" count rather than being silently dropped.
_MESSAGE_TEMPLATE_CAP = 25


def _collapse_template(msg: str) -> str:
    return _NUM_RE.sub("N", str(msg))


def _trust_context(
    meta: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[str] | None, dict[str, Any]]:
    """``(records, route_steps, trust_summary)`` pulled off ``meta['results']``
    — the same structured trust-records contract :func:`trust_toon` reads
    (``trust_schema`` 1-2). ``route_steps`` is ``None`` when the pathway's
    results don't carry it (older engine) — callers must then treat every
    fatal fail as on-route, per the spec's "when route_steps is absent, all
    fatal fails" rule."""
    results = meta.get("results")
    results = results if isinstance(results, dict) else {}
    records = [r for r in (results.get("trust") or []) if isinstance(r, dict)]
    route_steps = results.get("route_steps")
    route_steps = set(route_steps) if isinstance(route_steps, list) else None
    trust_summary = results.get("trust_summary")
    trust_summary = trust_summary if isinstance(trust_summary, dict) else {}
    return records, route_steps, trust_summary


def _on_route(rec: dict[str, Any], route_steps: set[str] | None) -> bool:
    return route_steps is None or rec.get("step") in route_steps


def _is_blocking(rec: dict[str, Any], route_steps: set[str] | None) -> bool:
    return (
        rec.get("verdict") == "fail"
        and rec.get("severity") == "fatal"
        and _on_route(rec, route_steps)
    )


def _blocking_records(
    records: list[dict[str, Any]],
    route_steps: set[str] | None,
    trust_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fatal on-route fails, ``trust_summary.barrier.blocked_by`` ids first
    (when that list exists), then by step/seed for a stable read."""
    blocking = [r for r in records if _is_blocking(r, route_steps)]
    priority = list((trust_summary.get("barrier") or {}).get("blocked_by") or [])
    order = {rid: i for i, rid in enumerate(priority)}
    blocking.sort(
        key=lambda r: (
            order.get(r.get("id"), len(order)),
            _trust_subject(r),
            r.get("seed") or 0,
        )
    )
    return blocking


def _blocked_by_note(trust_summary: dict[str, Any]) -> str:
    barrier = trust_summary.get("barrier") or {}
    selectivity = trust_summary.get("selectivity") or {}
    n_barrier = len(barrier.get("blocked_by") or [])
    n_sel = len(selectivity.get("blocked_by") or [])
    return f"barrier blocked_by: {n_barrier} · selectivity blocked_by: {n_sel}"


def _counts_rows(
    records: list[dict[str, Any]],
    route_steps: set[str] | None,
) -> list[dict[str, Any]]:
    """One row per ``(check, verdict)`` among everything that isn't blocking
    and isn't a plain ``pass`` — the "3 marginal, 5 off-route" collapse from
    the spec. A ``fail`` at ``severity: warn`` renders as ``fail(warn)`` so it
    reads distinctly from a blocking fatal fail; a fatal fail that's simply
    off the reported route (so not blocking) still renders as plain ``fail``,
    with its off-route share called out via the count column."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in records:
        if _is_blocking(r, route_steps) or r.get("verdict") == "pass":
            continue
        verdict = r.get("verdict")
        vdisp = (
            "fail(warn)"
            if verdict == "fail" and r.get("severity") == "warn"
            else str(verdict or "?")
        )
        key = (str(r.get("check") or "?"), vdisp)
        groups.setdefault(key, []).append(r)

    rows = []
    for (check, vdisp), recs in sorted(groups.items()):
        total = len(recs)
        off = sum(1 for r in recs if not _on_route(r, route_steps))
        count = str(total) if not off else f"{total} (off-route {off})"
        rows.append({"check": check, "verdict": vdisp, "count": count})
    return rows


def _messages_toon(warns: list[Any]) -> str:
    if not warns:
        return "no messages — states/barriers converged and within tolerance."
    counts = Counter(_collapse_template(w) for w in warns)
    first: dict[str, str] = {}
    for w in warns:
        first.setdefault(_collapse_template(w), str(w))
    ordered = counts.most_common()  # count desc, ties in first-seen order
    shown = ordered[:_MESSAGE_TEMPLATE_CAP]
    # A template seen once keeps its literal numbers — collapsing "edge 3"
    # to "edge N" only pays off when it merges repeats.
    rows = [{"message": f"{n} × {t if n > 1 else first[t]}"} for t, n in shown]
    table = toon.dump(rows, schema=["message"])
    extra = len(ordered) - len(shown)
    if extra > 0:
        table += f"\n(+{extra} more templates)"
    return table


def _warnings_summary(meta: dict[str, Any]) -> str:
    """Compact ``blocking=<n> · other-records=<m> · messages=<k
    templates/<total>>`` line — replaces a raw ``len(warnings)`` count
    wherever one would otherwise read as "17 warnings" for one real
    blocker (spec fix 1, item 2)."""
    records, route_steps, trust_summary = _trust_context(meta)
    warns = meta.get("warnings") or []
    if records:
        n_blocking = sum(1 for r in records if _is_blocking(r, route_steps))
        n_other = sum(
            1
            for r in records
            if not _is_blocking(r, route_steps) and r.get("verdict") != "pass"
        )
    else:
        n_blocking = n_other = 0
    n_templates = len({_collapse_template(w) for w in warns})
    return (
        f"blocking={n_blocking} · other-records={n_other} · "
        f"messages={n_templates} templates/{len(warns)}"
    )


def warnings_toon(meta: dict[str, Any]) -> str:
    """Severity- and route-aware warnings view (spec:
    docs/backlog/pathway-conditions-effects-report.md "Warnings" fix 1):
    the flat ``meta['warnings']`` prose list mixes informational marginal
    notes, a repeated data gap, off-route notes, and the few fatal on-route
    blockers, so on prod (2026-09-16, n=24) 16/24 pathways showed >10
    warnings for at most a couple of real problems. Three sections:
    ``blocking`` (the real, route-scoped fatal fails, from the structured
    ``trust`` records — never the prose), ``counts`` (everything else,
    collapsed to one row per check/verdict), ``messages`` (the flat prose,
    collapsed by numeric-literal template and capped)."""
    warns = meta.get("warnings") or []
    records, route_steps, trust_summary = _trust_context(meta)

    sections = [f"warnings: {_warnings_summary(meta)}", ""]

    if not records:
        sections.append("## blocking")
        sections.append("no trust records: pre-trust-schema artifact")
    else:
        blocking = _blocking_records(records, route_steps, trust_summary)
        sections.append("## blocking")
        if blocking:
            rows = [
                {
                    "step": _trust_subject(r),
                    "seed": r.get("seed"),
                    "check": r.get("check"),
                    "evidence": _trust_evidence(r),
                    "id": r.get("id"),
                }
                for r in blocking
            ]
            sections.append(
                toon.dump(rows, schema=["step", "seed", "check", "evidence", "id"])
            )
        else:
            sections.append("no blocking (fatal, on-route) trust records.")

        sections.append("")
        sections.append("## counts")
        counts_rows = _counts_rows(records, route_steps)
        sections.append(toon.dump(counts_rows, schema=["check", "verdict", "count"]))
        sections.append(_blocked_by_note(trust_summary))

    sections.append("")
    sections.append("## messages")
    sections.append(_messages_toon(warns))
    return "\n".join(sections)


def _trust_subject(rec: dict[str, Any]) -> str:
    """A trust record's step/state — matches catpath's own id-context
    convention (``state@step``, trust.record()): a state-relax record gives
    both ``state`` and ``step`` (the step context it relaxed for), a step
    (NEB) record only ``step``."""
    state, step = rec.get("state"), rec.get("step")
    if state and step:
        return f"{state}@{step}"
    return str(state or step or "?")


def _trust_evidence(rec: dict[str, Any]) -> str:
    ev = rec.get("evidence")
    if not isinstance(ev, dict) or not ev:
        return ""
    return ", ".join(f"{k}={v}" for k, v in ev.items())


def trust_toon(meta: dict[str, Any]) -> str:
    """Per-step structured trust records (catpath ``trust_schema`` 1-2,
    ``docs/backlog/per-step-trust-records.md`` upstream) as a TOON table
    grouped by step/state — the artifact answers "which step, which check,
    which evidence" directly, no prose regex. Record ids render VERBATIM
    (:mod:`precis.quest.compute` ``_pathway_quality_v1`` cites them as
    handles; they are designed to be stable). A pathway harvested before the
    trust-records contract existed (``results`` carries no ``trust_schema``,
    or an older/absent one) gets a guidance message instead of an empty or
    misleadingly-silent table — never guess at a shape this reader doesn't
    understand.
    """
    results = meta.get("results")
    results = results if isinstance(results, dict) else {}
    schema = results.get("trust_schema")
    if schema not in (1, 2):  # 2 (engine 0.20.0) is additive over 1
        return (
            "this pathway predates per-step trust records (its results carry "
            "no trust_schema in 1-2) — re-run it against a current "
            "autocatpath to get this view; try view='warnings' for the "
            "prose fallback."
        )
    records = [r for r in (results.get("trust") or []) if isinstance(r, dict)]
    if not records:
        return f"trust_schema={schema} but no trust records on this pathway."

    rows = [
        {
            "step": _trust_subject(r),
            "seed": r.get("seed"),
            "check": r.get("check"),
            "verdict": r.get("verdict"),
            "severity": r.get("severity"),
            "evidence": _trust_evidence(r),
            "id": r.get("id"),
        }
        for r in sorted(
            records,
            key=lambda r: (_trust_subject(r), r.get("seed") or 0, r.get("check") or ""),
        )
    ]
    return toon.dump(
        rows, schema=["step", "seed", "check", "verdict", "severity", "evidence", "id"]
    )


def potential_line(U: float) -> str:
    """The one-line CHE caveat every U-levered view leads with."""
    return (
        f"at U = {U:+.2f} V vs RHE — CHE: state energies shifted by n_H·eU; "
        "‡ barriers are U-independent (thermodynamic lever only, no solvation)"
    )


def analysis_text(meta: dict[str, Any], *, U: float | None = None) -> str:
    """The analysis headline + tables; ``U`` (V vs RHE) re-levers the graph
    first (:func:`analysis.at_potential`) so span and the most endergonic
    route step are read at that potential."""
    graph = meta.get("graph") or {}
    root, target = _roots(meta)
    r = meta.get("results", {})
    n = r.get("n_samples", "?")
    models = ",".join(r.get("models", [])) or r.get("backend", "?")

    head = [f"{root} → {target}  ({models}, {n} samples)"]
    if U is not None:
        graph = analysis.at_potential(graph, U)
        head.append(potential_line(U))
    head.append("")
    rl = analysis.rate_limiting(graph, root, target)
    span = analysis.energetic_span(graph, root, target)
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
        at = f" at U = {U:+.2f} V" if U is not None else ""
        head.append(f"energetic span (whole-path apparent barrier){at}: {_b(span)} eV")
    if U is not None:
        step = analysis.most_endergonic_step(graph, root, target)
        if step is not None:
            head.append(
                f"most endergonic route step at U: {step['step']}   "
                f"ΔG = {step['delta_g']:+.2f} eV ({step['kind']} step)"
            )
    head.append(f"warnings: {_warnings_summary(meta)}   (view='warnings' for detail)")
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
def compare_toon(candidates: list[dict[str, Any]], *, U: float | None = None) -> str:
    """`candidates`: [{slug, lever, graph, root, target}]. Rows = candidates.
    When they share a network, columns interleave state(rel eV) + ‡(barrier Eₐ)
    along the reaction coordinate; always: RATE (max step), SPAN, conf. Sorted
    by RATE ascending (best first). With ``U`` (V vs RHE) every CHE-stamped
    candidate is re-levered to that potential and the table ranks by SPAN at
    U instead (RATE is U-independent); a candidate without ``n_H`` stays
    unshifted and is named in the header."""
    if not candidates:
        return "no computed candidates to compare."

    notes: list[str] = []
    if U is not None:
        levered: list[dict[str, Any]] = []
        unshifted: list[str] = []
        for c in candidates:
            if analysis.has_potential_lever(c["graph"]):
                levered.append({**c, "graph": analysis.at_potential(c["graph"], U)})
            else:
                unshifted.append(str(c["slug"]))
                levered.append(c)
        candidates = levered
        notes.append(f"# {potential_line(U)}; ranked by SPAN at U")
        if unshifted:
            notes.append(
                "# no n_H (pre-CHE run), shown unshifted: " + ", ".join(unshifted)
            )

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

    def _sort_key(p: dict[str, Any]) -> tuple[bool, float]:
        # numeric, not on the 2-dp strings: "10.00" < "9.00" lexically.
        if U is not None:
            span = p["summ"]["span"]
            return (span is None, float(span) if span is not None else 0.0)
        ea = (p["summ"]["rate_limiting"] or {}).get("ea")
        return (ea is None, float(ea) if ea is not None else 0.0)

    profiles.sort(key=_sort_key)
    prefix = "".join(n + "\n" for n in notes)

    if not aligned:
        rows = [_row_scalars(p) for p in profiles]
        note = "# networks differ — scalar comparison only (RATE = rate-limiting Eₐ)\n"
        return (
            prefix
            + note
            + toon.dump(rows, schema=["cand", "lever", "RATE", "SPAN", "conf"])
        )

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

    head = "# ‡ = step barrier Eₐ; state cols = rel eV vs root.  " + "  ".join(legend)
    return prefix + head + "\n" + toon.dump(rows, schema=schema)
