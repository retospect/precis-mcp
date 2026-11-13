"""Decision analysis over a computed reaction graph — pure, dependency-free.

Lives in core rather than in ``precis_pathway`` because ``precis.quest``
(:mod:`~precis.quest.figures`, :mod:`~precis.quest.results_table`) reads
these scalars for any quest whose candidates carry a graph, and core must
not import a plugin — see ``docs/backlog/plugin-split-runtime-shell.md``.
Nothing here touches the store, a handler or the engine; it is stdlib-only
graph math over the dict shape described below.

It sits in ``utils`` rather than in a new top-level package because that is
where this repo already keeps pure, dependency-free helper modules, and not
in ``precis.sim`` — which despite the name is the CLI harness for driving
external simulator repos, not a home for analysis math. A later extraction
may move this into ``precis-geom``; nothing outside its import line depends
on where it lives.

Operates on the ``graph`` dict a run stores (networkx ``node_link_data`` with
``edges='links'``): ``nodes`` carry ``id / rel_energy / energy_std /
low_confidence``; ``links`` carry ``source / target / barrier / barrier_std /
delta_e / kind / low_confidence``.

The point is to hand the LLM the *scalars it optimises on* — the rate-limiting
barrier and the whole-path energetic span — precomputed, so it never has to
find a max or subtract across cells. The fork-window section adds the
closed-form selectivity read over potential: which branch points the target
route wins, and over which U interval all of them.
"""

from __future__ import annotations

import math
from collections import deque
from itertools import combinations, pairwise
from typing import Any, cast

from precis.utils.coerce import finite_or


def roots(graph: dict[str, Any], results: dict[str, Any]) -> tuple[str, str]:
    """Resolve the (root, target) node ids. The substrate *label* (``NO``) may
    not be a node — autocatpath's oxidation root node is e.g. ``NO+O`` — so the root
    is the first entry of the topological ``pathway`` order, and the target is
    the ``cfg.target`` node if present else the last path entry."""
    order = results.get("pathway", [])
    nodes = {n["id"] for n in graph.get("nodes", [])}
    root = order[0] if order else str(results.get("substrate", ""))
    tgt = results.get("target")
    target = tgt if tgt in nodes else (order[-1] if order else (tgt or ""))
    return cast(str, root), cast(str, target)


def _finite_or_none(x: Any) -> float | None:
    """The barrier as a number, or None when missing/NaN/inf — one shape for
    every consumer, whether the graph is pre- or post-``_json_finite``."""
    if x is None:
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _reaction_edges(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in graph.get("links", []) if e.get("kind") != "supply"]


def _is_supply(e: dict[str, Any] | None) -> bool:
    return e is not None and e.get("kind") == "supply"


def _node_map(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in graph.get("nodes", [])}


def _edge(graph: dict[str, Any], a: str, b: str) -> dict[str, Any] | None:
    """Any edge a→b — reaction *or* supply (the path traverses both)."""
    for e in graph.get("links", []):
        if e["source"] == a and e["target"] == b:
            return e
    return None


def reaction_path(graph: dict[str, Any], root: str, target: str) -> list[str]:
    """Shortest chain of states from root to target. Traverses reaction AND
    supply edges (the +O*/+H* stoichiometry bridges are zero-barrier connectors
    that the productive path must cross); the barrier accounting later skips the
    supply steps. Returns [] if unreachable."""
    adj: dict[str, list[str]] = {}
    for e in graph.get("links", []):
        adj.setdefault(e["source"], []).append(e["target"])
    q: deque[list[str]] = deque([[root]])
    seen = {root}
    while q:
        path = q.popleft()
        if path[-1] == target:
            return path
        for nxt in adj.get(path[-1], []):
            if nxt not in seen:
                seen.add(nxt)
                q.append(path + [nxt])
    return []


def _path_steps(graph: dict[str, Any], path: list[str]) -> list[dict[str, Any]]:
    """Reaction (barrier-bearing) steps along the path — supply bridges skipped."""
    out = []
    for a, b in pairwise(path):
        e = _edge(graph, a, b)
        if e is not None and not _is_supply(e):
            out.append(e)
    return out


def rate_limiting(
    graph: dict[str, Any], root: str, target: str
) -> dict[str, Any] | None:
    """The max-barrier step on the root→target path (falls back to the whole
    network if no clean path)."""
    path = reaction_path(graph, root, target)
    steps = _path_steps(graph, path) or _reaction_edges(graph)
    if not steps:
        return None
    # A null barrier (a non-finite EMT value persisted as JSON null) sorts last.
    top = max(steps, key=lambda e: finite_or(e.get("barrier"), float("-inf")))
    ea = _finite_or_none(top.get("barrier"))
    return {
        "step": f"{top['source']}→{top['target']}",
        "ea": ea,
        # a missing barrier has no measured spread — None, not a fake 0.0.
        "std": (top.get("barrier_std", 0.0) if ea is not None else None),
        "low_confidence": bool(top.get("low_confidence", False)),
    }


def energetic_span(graph: dict[str, Any], root: str, target: str) -> float | None:
    """Whole-path apparent barrier: the biggest climb from any intermediate to
    any *later* transition state along the path (Kozuch–Shaik energetic span).
    Can exceed every single-step Eₐ when a deep well precedes a high TS.
    Unavailable if any reaction barrier on the path is unknown."""
    path = reaction_path(graph, root, target)
    if len(path) < 2:
        return None
    nm = _node_map(graph)
    state_e = [finite_or(nm.get(s, {}).get("rel_energy")) for s in path]
    span = 0.0
    min_state = state_e[0]
    for i, (a, b) in enumerate(pairwise(path)):
        e = _edge(graph, a, b)
        # supply bridges carry no barrier — no extra climb at that step.
        ea = (
            0.0
            if _is_supply(e)
            else _finite_or_none(e.get("barrier") if e is not None else None)
        )
        if ea is None:
            return None
        ts_energy = state_e[i] + ea  # cumulative TS height
        min_state = min(min_state, state_e[i])
        span = max(span, ts_energy - min_state)
    return span


def barriers_ranked(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """All reaction steps, highest barrier first (the drill-down table)."""
    rows = []
    for e in _reaction_edges(graph):
        ea = _finite_or_none(e.get("barrier"))
        rows.append(
            {
                "reaction": f"{e['source']}→{e['target']}",
                "ea": ea,
                # a missing barrier has no measured spread and no trust
                # verdict — "ok" on a non-measurement reads as one.
                "std": (e.get("barrier_std", 0.0) if ea is not None else None),
                "conf": (
                    "low" if e.get("low_confidence") else "n/a" if ea is None else "ok"
                ),
            }
        )

    # A NaN barrier (the engine's "not computed") sorts last with the None
    # ones: NaN compares false both ways, so left in the key it gives the sort
    # no defined order and the table came out scrambled differently per Python.
    def _key(r: dict[str, Any]) -> tuple[bool, float]:
        ea = r["ea"]
        missing = ea is None
        return (missing, 0.0 if missing else -ea)

    return sorted(rows, key=_key)


def selectivity(graph: dict[str, Any], root: str, target: str) -> list[dict[str, Any]]:
    """Competing first steps out of the root: the target-path entry vs the
    other branches leaving root. Lower entry barrier = kinetically favored."""
    path = reaction_path(graph, root, target)
    on_path = set(pairwise(path))
    out = []
    for e in _reaction_edges(graph):
        if e["source"] != root:
            continue
        out.append(
            {
                "entry_step": f"{e['source']}→{e['target']}",
                "entry_ea": _finite_or_none(e.get("barrier")),
                "on_target_path": (e["source"], e["target"]) in on_path,
            }
        )
    return sorted(out, key=lambda r: (r["entry_ea"] is None, r["entry_ea"] or 0.0))


def profile_positions(
    graph: dict[str, Any], root: str, target: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """The reaction coordinate as an interleaved [state, ‡, state, ‡, …] list —
    the columns of the compare table. State cells hold rel energy; ‡ cells hold
    the step barrier Eₐ (so a cell is directly the number you'd compare)."""
    path = reaction_path(graph, root, target)
    nm = _node_map(graph)
    cols: list[dict[str, Any]] = []
    for i, s in enumerate(path):
        node = nm.get(s, {})
        cols.append(
            {
                "pos": f"s{i}",
                "kind": "state",
                "label": s,
                "value": node.get("rel_energy"),
                "low_confidence": bool(node.get("low_confidence")),
            }
        )
        if i < len(path) - 1:
            e = _edge(graph, s, path[i + 1])
            # supply bridge → no ‡ column (no barrier); the two states sit
            # adjacent. Only reaction steps get a barrier column.
            if e is not None and not _is_supply(e):
                cols.append(
                    {
                        "pos": f"‡{i + 1}",
                        "kind": "ts",
                        "label": f"{s}→{path[i + 1]}",
                        "value": _finite_or_none(e.get("barrier")),
                        "low_confidence": bool(e.get("low_confidence")),
                    }
                )
    return path, cols


def summarize(graph: dict[str, Any], root: str, target: str) -> dict[str, Any]:
    """The scalar bundle a leaderboard row / analysis headline needs."""
    rl = rate_limiting(graph, root, target)
    span = energetic_span(graph, root, target)
    return {
        "rate_limiting": rl,
        "span": span,
        "low_confidence": bool(rl and rl["low_confidence"]),
    }


# ── the potential lever (CHE) ────────────────────────────────────────────
#
# Under the computational hydrogen electrode every reservoir H is H⁺ + e⁻,
# so a state that has absorbed ``n_H`` of them relative to the root shifts
# by ``n_H·eU`` (U vs RHE): ``G(U) = G(0) + n_H·U`` — the same closed form
# the web explorer applies client-side (``pathway_detail.html.j2`` "potential
# lever"). Chemical (NEB) steps join states of equal ``n_H``, so their
# barriers do not move; only the supply (PCET) bridges and hence the state
# ladder, the energetic span and the most-endergonic step do. A graph catpath
# never stamped (pre-CHE run) carries no ``n_H`` at all — the lever is then
# unavailable, not silently zero.


def has_potential_lever(graph: dict[str, Any]) -> bool:
    """True when at least one node carries an ``n_H`` stamp (a CHE-aware run)."""
    return any(n.get("n_H") is not None for n in graph.get("nodes", []))


def at_potential(graph: dict[str, Any], U: float) -> dict[str, Any]:
    """A copy of ``graph`` re-levered to ``U`` V vs RHE: node ``rel_energy``
    (and ``energy``, when present) shifted by ``n_H·U``; link ``delta_e``
    shifted by the endpoints' ``n_H`` difference. Barriers untouched. A
    node without ``n_H`` shifts by 0 (legacy node inside a stamped graph)."""
    nodes_out: list[dict[str, Any]] = []
    n_h: dict[str, float] = {}
    for n in graph.get("nodes", []):
        nn = dict(n)
        shift = finite_or(n.get("n_H")) * U
        n_h[str(n.get("id"))] = finite_or(n.get("n_H"))
        for key in ("rel_energy", "energy"):
            if nn.get(key) is not None:
                nn[key] = float(nn[key]) + shift
        nodes_out.append(nn)
    links_out: list[dict[str, Any]] = []
    for e in graph.get("links", []):
        ee = dict(e)
        if ee.get("delta_e") is not None:
            d_nh = n_h.get(str(e.get("target")), 0.0) - n_h.get(
                str(e.get("source")), 0.0
            )
            ee["delta_e"] = float(ee["delta_e"]) + d_nh * U
        links_out.append(ee)
    out = dict(graph)
    out["nodes"] = nodes_out
    out["links"] = links_out
    return out


def most_endergonic_step(
    graph: dict[str, Any], root: str, target: str
) -> dict[str, Any] | None:
    """The route step (reaction OR supply bridge) with the largest state-to-
    state ΔG along the root→target path — the thermodynamic bottleneck at
    the potential the graph is levered to (at U = 0 its ΔG is ``−U_L`` of
    the target path when that step is a PCET). None without a clean path."""
    path = reaction_path(graph, root, target)
    if len(path) < 2:
        return None
    nm = _node_map(graph)
    best: dict[str, Any] | None = None
    for a, b in pairwise(path):
        ea, eb = nm.get(a, {}).get("rel_energy"), nm.get(b, {}).get("rel_energy")
        if ea is None or eb is None:
            continue
        dg = float(eb) - float(ea)
        if best is None or dg > best["delta_g"]:
            e = _edge(graph, a, b)
            best = {
                "step": f"{a}→{b}",
                "delta_g": dg,
                "kind": "supply" if _is_supply(e) else "reaction",
            }
    return best


# ── the fork window (closed form) ─────────────────────────────────────
#
# For each branch point on the target route the fork margin is a function
# of potential: ``margin(U) = dG_side(U) − dG_on(U)``, closed form under
# CHE — each step's dG moves by its ``n_H`` change times eU, so the margin
# is the line ``m0 + slope·U`` with ``m0`` the margin at U = 0 and
# ``slope`` the side step's n_H change minus the on-route step's. A
# proton-coupled step shifts with U and a chemical step does not, so a
# PCET-versus-chemical fork has a U-dependent winner while a
# chemical-versus-chemical fork is a flat line.
#
# This is the closed-form *thermodynamic* reading of selectivity —
# distinct from the engine's ``score.selectivity`` (``margin_eV``,
# ``P_side``) and the explorer's fork percentages, which are all
# barrier-based, and from the slab viability window of
# ``precis.quest.window_objectives``: say "fork window". The per-fork
# test is right when the fork steps are effectively irreversible. ``+H``
# supply edges are never branch-point competitors — under CHE a supply
# step's dG at U is set by U whether the hydrogen comes from solution or
# from bulk β-PdH, so they take no side in the comparison — and where a
# reversible intermediate sits before a fork the report flags that fork
# (``reversible``) rather than solve the kinetics.

_EPS = 1e-9

#: Reverse-barrier ceiling (eV) under which the route step into a branch
#: point counts as reversible — the intermediate feeding the fork then
#: sits in quasi-equilibrium with its precursor and the thermodynamic
#: margin does not decide the winner. No spec threshold exists; 0.3 eV ≈
#: a step still crossed on a catalytic timescale near ambient
#: temperature. The kinetic half (per-fork Ea) replaces this heuristic.
REVERSIBLE_BARRIER_EV = 0.3


def _side_line(nm: dict[str, dict[str, Any]], a: str, b: str, s: str) -> dict[str, Any]:
    """One side of a fork as the margin line ``m0 + slope·U``.

    ``m0`` is the margin at U = 0 — the side product's ``rel_energy``
    minus the on-route product's (the shared source cancels); ``slope``
    is the side step's ``n_H`` change minus the on-route step's, which
    reduces to the products' ``n_H`` difference. Either is None when its
    node field is absent or non-finite — a raw NaN off the engine reads
    like a persisted null: unavailable, never zeroed (a NaN coefficient
    would poison every downstream comparison).
    """
    nb, ns = nm.get(b, {}), nm.get(s, {})
    eb = _finite_or_none(nb.get("rel_energy"))
    es = _finite_or_none(ns.get("rel_energy"))
    hb = _finite_or_none(nb.get("n_H"))
    hs = _finite_or_none(ns.get("n_H"))
    return {
        "step": f"{a}→{s}",
        "m0": None if eb is None or es is None else es - eb,
        "slope": None if hb is None or hs is None else hs - hb,
    }


def _reversible_feed(
    graph: dict[str, Any],
    nm: dict[str, dict[str, Any]],
    path: list[str],
    i: int,
) -> bool:
    """True when the route step into ``path[i]`` is effectively reversible:
    a supply bridge (a +H step held at equilibrium with the reservoir —
    never a committed step) or a reaction whose implied reverse barrier
    ``barrier − dG0`` is at most :data:`REVERSIBLE_BARRIER_EV`. The root
    has no incoming step. An unknown barrier or endpoint energy reads as
    not demonstrably reversible — False, not a guess."""
    if i == 0:
        return False
    e = _edge(graph, path[i - 1], path[i])
    if e is None:
        return False
    if _is_supply(e):
        return True
    ea = _finite_or_none(e.get("barrier"))
    ep = nm.get(path[i - 1], {}).get("rel_energy")
    ec = nm.get(path[i], {}).get("rel_energy")
    if ea is None or ep is None or ec is None:
        return False
    return ea - (float(ec) - float(ep)) <= REVERSIBLE_BARRIER_EV


def forks(graph: dict[str, Any], root: str, target: str) -> list[dict[str, Any]]:
    """The branch points on the root→target route, one dict per fork:
    ``node`` the branch-point state id, ``step`` the on-route step named
    as :func:`rate_limiting` names a step (supply or reaction — a supply
    on-route step against a chemical side is exactly the
    PCET-versus-chemical fork), ``sides`` the other outgoing non-supply
    steps each as ``{step, m0, slope}``, ``reversible`` the
    reversible-intermediate flag, ``name`` the label ``losing_fork`` and
    ``edge_fork`` report. Several sides fold into one fork — the margin
    reads their minimum. A state with no non-supply side is no fork; no
    path gives []."""
    path = reaction_path(graph, root, target)
    if len(path) < 2:
        return []
    nm = _node_map(graph)
    out: list[dict[str, Any]] = []
    for i, (a, b) in enumerate(pairwise(path)):
        sides = [
            _side_line(nm, a, b, str(e["target"]))
            for e in graph.get("links", [])
            if e["source"] == a and e["target"] != b and not _is_supply(e)
        ]
        if not sides:
            continue
        step = f"{a}→{b}"
        out.append(
            {
                "node": a,
                "step": step,
                "sides": sides,
                "reversible": _reversible_feed(graph, nm, path, i),
                "name": f"{step} vs {' | '.join(s['step'] for s in sides)}",
            }
        )
    return out


def fork_margin(fork: dict[str, Any], U: float) -> float | None:
    """The fork's margin at ``U`` V vs RHE — the minimum over its side
    lines ``m0 + slope·U``; positive means the target route wins the
    fork. None when any side line is unavailable (a missing
    ``rel_energy`` or ``n_H``): an unmeasured competitor is unknown, not
    zero."""
    vals = []
    for s in fork.get("sides", []):
        if s.get("m0") is None or s.get("slope") is None:
            return None
        vals.append(s["m0"] + s["slope"] * U)
    return min(vals) if vals else None


def fork_window(
    graph: dict[str, Any],
    root: str,
    target: str,
    *,
    bounds: tuple[float, float] | None = None,
    u_l: float | None = None,
) -> dict[str, Any] | None:
    """The fork window: the closed-form selectivity read over potential.

    Every (fork, side) margin is the line ``m0 + slope·U``; the worst
    margin is their lower envelope, a concave piecewise-linear function
    whose maximum — ``U_sel`` — sits on a domain edge or at an interior
    crossing, so it is found exactly, no sampling. ``bounds`` is the
    swept U domain ``(lo, hi)`` in V vs RHE (``None`` = no cut; the
    caller passes the bound because ``precis.quest`` imports this module
    and may not be imported back — cf. ``U_RHE_WINDOW``); ``u_l`` is the
    route's potential limit and cuts the window at ``U ≤ U_L``, never
    ``U_sel``'s domain. Among U values attaining the maximal worst margin
    the lowest is reported (a plateau's left edge).

    With supply edges left out as competitors and chemical side steps
    joining states of equal ``n_H``, every fork line on a reduction route
    has slope 0 or −1: the worst margin never rises with U, ``U_sel``
    sits on the lower bound (``u_sel_on_bound``) and
    ``sel_margin_at_Usel`` is a function of that bound, not of the slab.
    The numbers that describe the slab are therefore reported alongside:
    ``window_upper_edge``, the least negative U at which every fork is
    still won after the ``U ≤ U_L`` cut; ``edge_fork``, the constraint
    that sets it — a fork ``name``, ``"U_L"`` or ``"bounds"``; and
    ``worst_fixed_margin``, the smallest margin among the slope-0 forks
    (None when there is none) — the bound-independent floor.

    Returns None — unavailable, distinct from an empty window — when the
    graph carries no ``n_H`` lever, the route is unreachable, or no
    branch point exists. ``U_sel_window`` is ``{lo, hi}`` (a None end is
    unbounded) or None when no U wins every fork. The interval is the
    strict-positivity set cut by the closed domain and ``U ≤ U_L``: a
    singleton is a valid window when its one point wins every fork,
    while a point sitting on a zero crossing is tied, not won.
    ``losing_fork`` then names the fork — the flat-loss fork, or the
    one capping the window from above when the margins contradict
    (``U > a`` and ``U < b`` with ``a ≥ b``, infeasible before any cut)
    — or the limit (``"U_L"``/``"bounds"``) only when the raw interval
    held winners and the cut excluded all of them. A fork whose side is
    missing or non-finite in ``rel_energy`` or ``n_H`` is listed with
    its margin None, joins ``unknown_forks``, and leaves the window
    scalars None: the window certifies every fork or says it cannot."""
    if not has_potential_lever(graph):
        return None
    fs = forks(graph, root, target)
    if not fs:
        return None
    lines: list[dict[str, Any]] = []
    unknown: list[str] = []
    for f in fs:
        if any(s["m0"] is None or s["slope"] is None for s in f["sides"]):
            unknown.append(f["name"])
            continue
        for s in f["sides"]:
            lines.append({"fork": f["name"], "m0": s["m0"], "slope": s["slope"]})
    out: dict[str, Any] = {
        "U_sel": None,
        "sel_margin_at_Usel": None,
        "U_sel_window": None,
        "losing_fork": None,
        "forks": fs,
        "window_upper_edge": None,
        "edge_fork": None,
        "worst_fixed_margin": None,
        "u_sel_on_bound": None,
        "unknown_forks": unknown,
    }
    # An unmeasurable fork makes the whole-window certificate unavailable.
    if unknown:
        return out

    lo = float(bounds[0]) if bounds else -math.inf
    hi = float(bounds[1]) if bounds else math.inf

    def _worst(u: float) -> float:
        return min(l["m0"] + l["slope"] * u for l in lines)

    # U_sel: candidates are the finite domain edges and the pairwise line
    # crossings inside the domain — the envelope peaks only there. The
    # exception is an unbounded domain where every line slides the same
    # way: the maximiser then runs off to ±∞ (gr489166) and U_sel is
    # honestly None.
    cands = [u for u in (lo, hi) if math.isfinite(u)]
    for a, b in combinations(lines, 2):
        ds = a["slope"] - b["slope"]
        if abs(ds) < _EPS:
            continue
        u = (b["m0"] - a["m0"]) / ds
        if lo < u < hi:
            cands.append(u)
    runs_left = not math.isfinite(lo) and all(l["slope"] < -_EPS for l in lines)
    runs_right = not math.isfinite(hi) and all(l["slope"] > _EPS for l in lines)
    u_sel: float | None = None
    if cands and not (runs_left or runs_right):
        best = max(_worst(u) for u in cands)
        u_sel = min(u for u in cands if abs(_worst(u) - best) <= _EPS)
        out["U_sel"] = u_sel
        out["sel_margin_at_Usel"] = best
        out["u_sel_on_bound"] = bool(
            (math.isfinite(lo) and u_sel == lo) or (math.isfinite(hi) and u_sel == hi)
        )

    # The window: {U : every line > 0} ∩ domain ∩ (−∞, u_l]. Strict
    # positivity makes the raw feasibility interval the OPEN one (p, q);
    # the domain and the U_L cut are closed. Feasibility is decided on
    # the raw interval first — contradictory margins (p ≥ q) name a
    # losing fork, never a cut — and endpoint inclusion is tracked: a
    # domain/cut edge counts, a zero crossing does not (the fork is
    # tied there, not won), so a singleton is a valid window exactly
    # when its one point sits strictly inside (p, q).
    p, q = -math.inf, math.inf
    p_by: str | None = None
    q_by: str | None = None
    impossible: str | None = None
    for l in lines:
        m0, s = l["m0"], l["slope"]
        if abs(s) < _EPS:
            # a flat line at or below zero wins at no U
            if m0 <= 0 and impossible is None:
                impossible = l["fork"]
        else:
            x = -m0 / s
            if s > 0 and x > p:
                p, p_by = x, l["fork"]
            elif s < 0 and x < q:
                q, q_by = x, l["fork"]

    d_hi_by: str | None = None
    d_hi = hi
    if u_l is not None and float(u_l) < d_hi:
        d_hi, d_hi_by = float(u_l), "U_L"

    if impossible is not None:
        out["losing_fork"] = impossible
        out["edge_fork"] = impossible
    elif p >= q:
        # Contradictory margins — one fork needs U above p, another needs
        # U below q with p ≥ q: no U wins every fork even before any cut,
        # so the emptiness is a fork's, not the cut's. Name the fork that
        # caps the window from above (the descending side).
        loser = q_by if q_by is not None else p_by
        out["losing_fork"] = loser
        out["edge_fork"] = loser
    else:
        w_lo = max(p, lo)
        w_hi = min(q, d_hi)
        if w_lo < w_hi or (w_lo == w_hi and lo > p and d_hi < q):
            out["U_sel_window"] = {
                "lo": w_lo if math.isfinite(w_lo) else None,
                "hi": w_hi if math.isfinite(w_hi) else None,
            }
            if math.isfinite(w_hi):
                out["window_upper_edge"] = w_hi
                out["edge_fork"] = (d_hi_by or "bounds") if d_hi < q else q_by
        else:
            # The raw interval held winners but the domain or the U_L cut
            # excluded all of them — the cut, not a fork, emptied it.
            blocker = (d_hi_by or "bounds") if d_hi <= p else "bounds"
            out["losing_fork"] = blocker
            out["edge_fork"] = blocker

    flats = [l["m0"] for l in lines if abs(l["slope"]) < _EPS]
    out["worst_fixed_margin"] = min(flats) if flats else None
    return out
