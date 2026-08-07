"""Computational hydrogen electrode (CHE) post-processing over a computed graph.

Pure, precis-free *and* autocatpath-free — operates only on the ``graph`` dict a
run stores (networkx ``node_link_data`` with ``edges='links'``; see
:mod:`precis_pathway.analysis`). **No new relax/NEB runs**: the applied-potential
lever is post-processing over energies we already have.

The physics (Nørskov CHE, thermodynamic v1). autocatpath models hydrogenation as
**supply edges** — ``+H*`` staged from a reservoir. Under CHE an applied
potential ``U`` (vs RHE) enters *only* through that reservoir's chemical
potential: each supplied H represents H⁺ + e⁻, so a node's free energy shifts

    G_node(U) = G_node(0) + n_H(node) · eU

where ``n_H(node)`` = the number of reservoir H atoms the node has absorbed
(node-intrinsic: count H in the node's own composition, minus the root's). In
eV/volt units ``e`` = 1, so the shift is numerically ``n_H · U``. Chemical steps
(N–O scission, on-surface recombination) keep their barriers; the ±eU shift
rides the ``n_H``-changing (electrochemical) steps.

Every downstream objective is an extremum of functions **affine in U**, so it is
closed-form — no binary search, no LLM:

* **Limiting potential** ``U_L`` — one pass over the electrochemical steps.
* **Optimal-span potential** — ``span(U)`` is a max of affine functions
  (piecewise-linear convex); its minimizer is a line intersection.

pH rides the same machinery. With ``U`` referenced to **RHE**, every
proton-coupled electron-transfer (PCET) step is pH-independent (that is the point
of the RHE choice); pH enters only as the display conversion to the SHE scale
(:func:`she_from_rhe`) and as a real per-step shift for *decoupled* proton/
hydroxide steps (none in the ammonia template today — dormant). Solvation,
surface charging, and coverage-vs-pH are outside the vacuum-slab ML envelope and
are deliberately *not* modeled.
"""

from __future__ import annotations

import itertools
import re
from typing import Any

from .analysis import reaction_path, roots

#: Boltzmann constant in eV/K — barriers and energies are all in eV.
BOLTZMANN_EV_PER_K = 8.617_333_262e-5
#: Standard ambient temperature (25 °C): the T electrochemical reference states
#: are tabulated at (Reto, 2026-08-07 — *not* the 300 K computational
#: round-number; kT differs by 0.6 %, immaterial, but the default matches the
#: standard condition).
T_DEFAULT = 298.15
#: Default operating-potential window (V vs RHE) the span minimizer searches
#: when a quest declares none. Wide enough to bracket NOx electroreduction.
DEFAULT_U_WINDOW = (-2.0, 2.0)

#: An element token in a fragment formula: a capital + optional lower-case letter,
#: then an optional integer subscript (``H2`` → 2, bare ``H`` → 1).
_ELEMENT_RE = re.compile(r"([A-Z][a-z]?)(\d*)")


def h_count(label: str) -> int:
    """Number of H atoms in a state label / node id.

    Node ids are fragment labels joined by ``+`` (``"NH2+H"`` = NH2* co-adsorbed
    with H*), optionally carrying a site-isomer suffix (``"NO@top"``). Split the
    suffix off, split on ``+``, and sum H across every fragment's formula so
    ``H2O`` contributes 2 and ``NH3`` contributes 3. An unparseable label
    contributes 0 (never raises — the caller treats it as no reservoir H).
    """
    base = str(label).split("@", 1)[0]
    total = 0
    for frag in base.split("+"):
        for sym, num in _ELEMENT_RE.findall(frag):
            if sym == "H":
                total += int(num) if num else 1
    return total


def n_h_per_node(
    graph: dict[str, Any], results: dict[str, Any] | None = None
) -> dict[str, int]:
    """``{node_id → n_H}`` — reservoir H atoms each node has absorbed.

    ``n_H`` is measured relative to the root (``root`` has ``n_H = 0`` by
    construction — the ammonia network's ``NO`` carries no H). Node-intrinsic:
    derived from the label alone, never path-dependent.
    """
    root = roots(graph, results or {})[0] if graph.get("nodes") else ""
    root_h = h_count(root) if root else 0
    return {n["id"]: h_count(n["id"]) - root_h for n in graph.get("nodes", [])}


def _node_map(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in graph.get("nodes", [])}


def _num(x: Any, default: float = 0.0) -> float:
    """Coerce a possibly-null (JSON) energy/barrier to a finite float."""
    return default if x is None else float(x)


def _is_supply(e: dict[str, Any] | None) -> bool:
    return e is not None and e.get("kind") == "supply"


def _edge(graph: dict[str, Any], a: str, b: str) -> dict[str, Any] | None:
    for e in graph.get("links", []):
        if e["source"] == a and e["target"] == b:
            return e
    return None


def shifted_energies(
    graph: dict[str, Any], u: float, n_h: dict[str, int] | None = None
) -> dict[str, float]:
    """``{node_id → rel_energy(U)}`` under the CHE shift ``+ n_H·U`` (eV)."""
    nh = n_h if n_h is not None else n_h_per_node(graph)
    nm = _node_map(graph)
    return {
        nid: _num(node.get("rel_energy")) + nh.get(nid, 0) * u
        for nid, node in nm.items()
    }


def _electrochemical_steps(
    graph: dict[str, Any], n_h: dict[str, int]
) -> list[tuple[dict[str, Any], int]]:
    """Reaction-graph edges whose ``n_H`` changes — the PCET (H-transfer) steps
    that pick up the ±eU shift. Returned as ``(edge, delta_n_H)`` pairs."""
    out: list[tuple[dict[str, Any], int]] = []
    for e in graph.get("links", []):
        dn = n_h.get(e["target"], 0) - n_h.get(e["source"], 0)
        if dn != 0:
            out.append((e, dn))
    return out


def limiting_potential(
    graph: dict[str, Any], results: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The CHE limiting potential ``U_L`` (V vs RHE) and its limiting step.

    ``U_L`` is the potential at which the most-endergonic electrochemical step
    becomes thermoneutral — below it (more cathodic) every H-transfer is
    downhill. For a step ``a→b`` adding ``Δn`` reservoir H,
    ``ΔG_step(U) = ΔG_step(0) + Δn·U`` with ``ΔG_step(0) = rel_energy(b) −
    rel_energy(a)``; the per-electron limit is ``−ΔG_step(0)/Δn`` and

        U_L = −(1/e) · max over electrochemical steps of ΔG_step(0)/Δn.

    Returns ``None`` when the graph has no electrochemical step (nothing to
    apply a potential to).
    """
    nh = n_h_per_node(graph, results)
    steps = _electrochemical_steps(graph, nh)
    if not steps:
        return None
    nm = _node_map(graph)
    worst: tuple[float, dict[str, Any], int] | None = None
    for e, dn in steps:
        dg0 = _num(nm.get(e["target"], {}).get("rel_energy")) - _num(
            nm.get(e["source"], {}).get("rel_energy")
        )
        per_e = dg0 / dn  # dn ≠ 0 by construction
        if worst is None or per_e > worst[0]:
            worst = (per_e, e, dn)
    assert worst is not None
    per_e, e, dn = worst
    return {
        "U_L": -per_e,
        "limiting_step": f"{e['source']}→{e['target']}",
        "limiting_dg0": _num(nm.get(e["target"], {}).get("rel_energy"))
        - _num(nm.get(e["source"], {}).get("rel_energy")),
        "delta_n_H": dn,
    }


def _span_affine_pieces(
    graph: dict[str, Any], path: list[str], n_h: dict[str, int]
) -> list[tuple[float, float]]:
    """The affine ``(slope, intercept)`` pieces whose max is ``span(U)``.

    Kozuch–Shaik energetic span along ``path``: the largest climb from any
    intermediate state ``k`` to any later transition state ``j`` (``k ≤ j``).
    State ``k`` energy is affine ``rel_energy[k] + n_H[k]·U``; TS height at step
    ``j`` is ``rel_energy[j] + ea_j + n_H[j]·U`` (a supply bridge carries no
    barrier). Each ``(j, k)`` pair contributes one affine function of ``U`` —
    ``span(U)`` = their pointwise max, hence piecewise-linear convex.
    """
    nm = _node_map(graph)
    rel = [_num(nm.get(s, {}).get("rel_energy")) for s in path]
    slope = [n_h.get(s, 0) for s in path]
    ts_intercept: list[float] = []  # TS height intercept at step j (state j → j+1)
    ts_slope: list[int] = []
    for j in range(len(path) - 1):
        e = _edge(graph, path[j], path[j + 1])
        ea = 0.0 if (e is None or _is_supply(e)) else _num(e.get("barrier"))
        ts_intercept.append(rel[j] + ea)
        ts_slope.append(slope[j])
    pieces: list[tuple[float, float]] = []
    for j in range(len(ts_intercept)):
        for k in range(j + 1):  # intermediate at or before the TS
            pieces.append((ts_slope[j] - slope[k], ts_intercept[j] - rel[k]))
    return pieces


def _span_at(pieces: list[tuple[float, float]], u: float) -> float:
    return max((m * u + b for m, b in pieces), default=0.0)


def optimal_span_potential(
    graph: dict[str, Any],
    results: dict[str, Any] | None = None,
    *,
    window: tuple[float, float] = DEFAULT_U_WINDOW,
) -> dict[str, Any] | None:
    """Minimize the energetic span over ``U`` in ``window`` (closed-form).

    ``span(U)`` is a max of affine functions → convex piecewise-linear, so its
    minimum sits at a breakpoint (a pairwise line intersection) or a window
    edge. Enumerate those candidates, clamp to ``window``, and take the argmin —
    exact at this graph size. Returns ``None`` if root→target has no path.
    """
    root, target = roots(graph, results or {})
    path = reaction_path(graph, root, target)
    if len(path) < 2:
        return None
    nh = n_h_per_node(graph, results)
    pieces = _span_affine_pieces(graph, path, nh)
    if not pieces:
        return None
    lo, hi = window
    candidates = {lo, hi}
    for i in range(len(pieces)):
        m1, b1 = pieces[i]
        for j in range(i + 1, len(pieces)):
            m2, b2 = pieces[j]
            if m1 != m2:
                u = (b2 - b1) / (m1 - m2)
                if lo <= u <= hi:
                    candidates.add(u)
    best_u = min(candidates, key=lambda u: _span_at(pieces, u))
    return {
        "U_opt": best_u,
        "span_at_Uopt": _span_at(pieces, best_u),
        "window": [lo, hi],
    }


def span_at_potential(
    graph: dict[str, Any], u: float, results: dict[str, Any] | None = None
) -> float | None:
    """The energetic span at a fixed applied potential ``U`` (V vs RHE)."""
    root, target = roots(graph, results or {})
    path = reaction_path(graph, root, target)
    if len(path) < 2:
        return None
    nh = n_h_per_node(graph, results)
    pieces = _span_affine_pieces(graph, path, nh)
    return _span_at(pieces, u) if pieces else None


def she_from_rhe(u_rhe: float, ph: float, *, temperature: float = T_DEFAULT) -> float:
    """Convert a potential from the RHE scale to SHE at a given pH.

    ``U_SHE = U_RHE − (ln10·kT/e)·pH`` (−0.0592 V/pH at 298.15 K). A display /
    literature-comparison conversion only — under RHE the PCET steps are
    pH-independent, so this never changes ``U_L`` or the span optimum.
    """
    import math

    return u_rhe - math.log(10.0) * BOLTZMANN_EV_PER_K * temperature * ph


#: Node/edge flags that disqualify a fork from selectivity scoring — a barrier
#: measured off a mis-bound / desorbed / infeasible state is not trustworthy, so
#: its branch fraction would be fiction.
_FORK_DISQUALIFYING_FLAGS = ("low_confidence", "wrong_site", "infeasible", "detached")


def _reaction_forks(graph: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """``{state → competing reaction edges}`` for states with ≥2 of them.

    Supply edges are excluded — they are bookkeeping, not kinetics."""
    by_src: dict[str, list[dict[str, Any]]] = {}
    for e in graph.get("links", []):
        if _is_supply(e):
            continue
        by_src.setdefault(e["source"], []).append(e)
    return {s: es for s, es in by_src.items() if len(es) >= 2}


def _fork_disqualified(graph: dict[str, Any], edges: list[dict[str, Any]]) -> bool:
    """A fork is scored only when every competing barrier is computed and no
    state/edge involved is flagged wrong-site / low-confidence / infeasible /
    detached — else "insufficient data", never a fabricated ratio."""
    nm = _node_map(graph)
    for e in edges:
        if e.get("barrier") is None:
            return True
        if any(e.get(f) for f in _FORK_DISQUALIFYING_FLAGS):
            return True
        for endpoint in (e["source"], e["target"]):
            node = nm.get(endpoint, {})
            if any(node.get(f) for f in _FORK_DISQUALIFYING_FLAGS):
                return True
    return False


def fork_probabilities(
    graph: dict[str, Any],
    results: dict[str, Any] | None = None,
    *,
    temperature: float = T_DEFAULT,
) -> list[dict[str, Any]]:
    """Branch fractions at each competing fork (equal-prefactor kinetics).

    At a state with ≥2 competing *reaction* edges the branch fraction is
    ``∝ exp(−ΔEa/kT)`` (measured relative to the lowest barrier for numerical
    stability). Guarded: a fork with any missing barrier or any
    wrong-site/low-confidence/infeasible/detached state is returned with
    ``insufficient_data=True`` and no fractions. ``temperature`` (K) is carried
    on every row so a reader always sees the T the ratio was taken at.
    """
    import math

    kt = BOLTZMANN_EV_PER_K * temperature
    out: list[dict[str, Any]] = []
    for state, edges in sorted(_reaction_forks(graph).items()):
        row: dict[str, Any] = {"state": state, "temperature": temperature}
        if _fork_disqualified(graph, edges):
            row["insufficient_data"] = True
            row["branches"] = [
                {"step": f"{e['source']}→{e['target']}", "ea": e.get("barrier")}
                for e in edges
            ]
            out.append(row)
            continue
        eas = [float(e["barrier"]) for e in edges]
        e_min = min(eas)
        weights = [math.exp(-(ea - e_min) / kt) for ea in eas]
        z = sum(weights)
        row["insufficient_data"] = False
        row["branches"] = [
            {
                "step": f"{e['source']}→{e['target']}",
                "target": e["target"],
                "ea": ea,
                "fraction": w / z,
            }
            for e, ea, w in zip(edges, eas, weights)
        ]
        out.append(row)
    return out


def selectivity_penalty(
    graph: dict[str, Any],
    results: dict[str, Any] | None = None,
    *,
    temperature: float = T_DEFAULT,
) -> float | None:
    """``P_side`` — the probability of leaving the target path at some fork.

    ``P_side = 1 − Π target-branch-fraction`` over the forks that lie on the
    root→target path. Returns ``None`` (insufficient data — never a fabricated
    ratio) if any on-path fork is disqualified, so a quest never ranks
    selectivity off untrustworthy branches.
    """
    root, target = roots(graph, results or {})
    path = reaction_path(graph, root, target)
    if len(path) < 2:
        return None
    on_path = set(itertools.pairwise(path))
    forks = fork_probabilities(graph, results, temperature=temperature)
    stay = 1.0
    scored_any = False
    for fork in forks:
        if fork["state"] not in path:
            continue
        # Only forks whose *target-path* branch is one of the competitors count.
        branch_on_path = [
            b for b in fork["branches"] if (fork["state"], b.get("target")) in on_path
        ]
        if not branch_on_path:
            continue
        if fork.get("insufficient_data"):
            return None
        scored_any = True
        stay *= sum(b["fraction"] for b in branch_on_path)
    return (1.0 - stay) if scored_any else None


def che_summary(
    graph: dict[str, Any],
    results: dict[str, Any] | None = None,
    *,
    temperature: float = T_DEFAULT,
    window: tuple[float, float] = DEFAULT_U_WINDOW,
) -> dict[str, Any]:
    """The closed-form electrochemistry bundle a `pathway` ref persists.

    Every field is post-processing over energies already in ``graph`` — no new
    compute. ``span_at_UL`` is the span *at the limiting potential* (the usual
    operating point); ``span_at_Uopt`` is the span at its own optimum.
    """
    lp = limiting_potential(graph, results)
    osp = optimal_span_potential(graph, results, window=window)
    p_side = selectivity_penalty(graph, results, temperature=temperature)
    summary: dict[str, Any] = {
        "temperature": temperature,
        "U_L": lp["U_L"] if lp else None,
        "limiting_step": lp["limiting_step"] if lp else None,
        "U_opt": osp["U_opt"] if osp else None,
        "span_at_Uopt": osp["span_at_Uopt"] if osp else None,
        "P_side": p_side,
        "forks": fork_probabilities(graph, results, temperature=temperature),
    }
    if lp is not None:
        summary["span_at_UL"] = span_at_potential(graph, lp["U_L"], results)
    return summary


__all__ = [
    "BOLTZMANN_EV_PER_K",
    "DEFAULT_U_WINDOW",
    "T_DEFAULT",
    "che_summary",
    "fork_probabilities",
    "h_count",
    "limiting_potential",
    "n_h_per_node",
    "optimal_span_potential",
    "selectivity_penalty",
    "she_from_rhe",
    "shifted_energies",
    "span_at_potential",
]
