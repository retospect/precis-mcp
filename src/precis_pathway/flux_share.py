"""Greedy net-flux decomposition of a stored kinetics record onto the
pathway explorer's enumerated routes (gr553482).

A pathway's kinetics solve is ONE master equation over the whole reaction
network — routes compete inside it and the stored
``transitions[].net_rate`` (``meta.results.kinetics``, written by
``kinetics.solve``) is the steady-state current each elementary
transition carries. The explorer's route picker enumerates root->leaf
routes over ``meta.graph``; this module answers "how much of the target
TOF does each route carry" without re-solving, by the standard greedy
flux decomposition:

1.  Build the net-flux graph: one directed edge per transition in its
    positive-net direction, capacity ``|net_rate|``. An edge *produces*
    the target when its net traversal releases the product gas —
    ``desorption`` transitions produce on their forward leg, every other
    gas-bearing kind on its backward leg (the same attribution
    ``kinetics.production_rates`` uses: TOF is the producing edges' total
    minus the consuming ones).
2.  Repeatedly extract the root->target path of largest *bottleneck*
    capacity (widest path), record its rate, subtract it from every edge
    on the path. Stop when the remaining target-producing capacity drops
    below ``rel_tol`` of the TOF or after ``max_paths`` extractions —
    whichever comes first.
3.  Map each extracted path onto the enumerated routes by step sequence
    (``~history`` landing suffixes folded — the kinetics record stores
    physical state names, ``meta.graph`` node ids can keep them). A
    cycle-closure desorption lands back on the root, so a path is also
    tried against routes ending at the state that releases the product.

The split is not unique when routes share steps; the greedy order is the
convention the gripe pins (largest bottleneck first). Everything here is
pure stdlib math over stored dicts — the web layer only renders the
payload :func:`route_flux` returns.
"""

from __future__ import annotations

import heapq
import math
from itertools import pairwise
from typing import Any

#: Extraction stops once the target-producing capacity left on the graph
#: is under this fraction of the TOF (gr553482 — the residual tail is
#: numerical noise beside the reported shares).
_REL_TOL = 1e-6

#: Hard cap on extracted paths — a loopy network can shave off a long
#: geometric tail; 50 rounds is far past any route count the picker draws.
_MAX_PATHS = 50

#: The one-line note the picker shows beside the shares (gr553482, Reto's
#: wording): a share is steady-state flux attribution at the recorded
#: environment, not a separate kinetics answer per route.
SHARE_NOTE = (
    "kinetics solves all routes together; shares are flux at the stated conditions"
)

#: Variant for the re-adsorption case: gross decomposition can exceed the
#: NET tof (consumption edges make tof a difference, not a sum), so shares
#: are fractions of the decomposed flux — the note must say which
#: denominator they carry.
SHARE_NOTE_GROSS = (
    "kinetics solves all routes together; shares are fractions of the "
    "decomposed flux at the stated conditions — the net TOF is lower "
    "because of re-adsorption"
)


def _phys(name: object) -> str:
    """Physical state name: ``meta.graph`` node ids may keep a ``~history``
    landing suffix that the kinetics record strips (autocatpath's own
    ``_phys`` — re-derived, not imported, so this module stays
    stdlib-only)."""
    return str(name).split("~", 1)[0]


def _net_edges(transitions: list[Any], product: str) -> list[dict[str, Any]]:
    """One directed edge per transition in its positive-net direction,
    capacity ``|net_rate|``. ``produces`` marks an edge whose traversal
    releases the product gas — the production attribution
    ``kinetics.production_rates`` sums: a ``desorption`` releases on its
    forward leg (``net_rate > 0``), every other gas-bearing kind on its
    backward one."""
    edges: list[dict[str, Any]] = []
    for t in transitions:
        if not isinstance(t, dict):
            continue
        r = t.get("net_rate")
        if (
            not isinstance(r, (int, float))
            or isinstance(r, bool)
            or not math.isfinite(r)
            or r == 0.0
        ):
            continue
        a, b = _phys(t.get("from")), _phys(t.get("to"))
        gas, kind = t.get("gas"), t.get("kind")
        produces = gas == product and (
            (kind == "desorption" and r > 0) or (kind != "desorption" and r < 0)
        )
        u, v = (a, b) if r > 0 else (b, a)
        edges.append(
            {
                "u": u,
                "v": v,
                "cap": abs(r),
                "produces": produces,
                "name": t.get("name"),
                "kind": kind,
                "gas": gas,
            }
        )
    return edges


def _widest_tree(
    root: str, edges: list[dict[str, Any]]
) -> tuple[dict[str, float], dict[str, tuple[str, int]]]:
    """Widest-path (maximin) arborescence out of ``root`` over the edges'
    remaining capacities: ``best[u]`` is the largest bottleneck a
    root->``u`` path can still carry, ``parent[u]`` the
    ``(predecessor, edge index)`` that achieves it. Settles nodes in
    decreasing-width order, Dijkstra-style — the first pop of a node is
    its final width."""
    adj: dict[str, list[int]] = {}
    for ei, e in enumerate(edges):
        if e["cap"] > 0.0:
            adj.setdefault(e["u"], []).append(ei)
    best: dict[str, float] = {root: math.inf}
    parent: dict[str, tuple[str, int]] = {}
    heap: list[tuple[float, str]] = [(-math.inf, root)]
    while heap:
        neg, u = heapq.heappop(heap)
        bw = -neg
        if bw < best.get(u, 0.0):
            continue  # stale queue entry — a wider path already settled u
        for ei in adj.get(u, ()):
            e = edges[ei]
            w = min(bw, e["cap"])
            if w > best.get(e["v"], 0.0):
                best[e["v"]] = w
                parent[e["v"]] = (u, ei)
                heapq.heappush(heap, (-w, e["v"]))
    return best, parent


def flux_decomposition(
    record: dict[str, Any] | None,
    *,
    rel_tol: float = _REL_TOL,
    max_paths: int = _MAX_PATHS,
) -> dict[str, Any] | None:
    """Greedy flux decomposition of a stored ``kinetics.solve`` record.

    ``record`` is ``meta.results.kinetics`` — the v1 block, the only one
    carrying ``transitions[].net_rate``; the v2 record keeps no per-edge
    currents, so a v2-only pathway cannot be decomposed (``None``).

    Returns ``None`` when the record cannot support a decomposition: no
    transition list, no named ``product``/``root``, or no finite positive
    ``tof`` — a solve that runs backwards or does not turn over has no
    target flux to attribute. Otherwise::

        {"product", "root", "tof", "conditions",
         "paths": [{"rate", "states", "steps"}],
         "remainder"}

    Each ``paths`` entry is one extracted unit of target flux — its
    ``states`` are the visited state names (ending through the
    product-releasing edge's destination), ``steps`` the transitions in
    traversal order, ``rate`` the bottleneck flux it carried.
    ``remainder`` is the TOF share no extracted path accounts for (loops,
    flux killed against a consumption edge), clipped at zero — the TOF is
    *net* production, so full attribution can exceed it by what
    re-adsorption cancels.
    """
    record = record if isinstance(record, dict) else {}
    transitions = record.get("transitions")
    product = record.get("product")
    root = record.get("root")
    tof = record.get("tof")
    if (
        not isinstance(transitions, list)
        or not isinstance(product, str)
        or not product
        or not isinstance(root, str)
        or not root
        or not isinstance(tof, (int, float))
        or isinstance(tof, bool)
        or not math.isfinite(tof)
        or tof <= 0.0
    ):
        return None
    edges = _net_edges(transitions, product)
    if not edges:
        return None
    paths: list[dict[str, Any]] = []
    remaining = math.fsum(e["cap"] for e in edges if e["produces"])
    while len(paths) < max_paths and remaining >= rel_tol * tof:
        best, parent = _widest_tree(root, edges)
        # the path ends by crossing whichever target-producing edge gives
        # the largest rate: min(bottleneck to its source, its own capacity)
        rate, term = 0.0, -1
        for ei, e in enumerate(edges):
            if not e["produces"] or e["cap"] <= 0.0:
                continue
            cand = min(best.get(e["u"], 0.0), e["cap"])
            if cand > rate:
                rate, term = cand, ei
        if term < 0 or rate <= 0.0:
            break
        chain = [term]
        node = edges[term]["u"]
        while node != root:
            prev, pei = parent[node]
            chain.append(pei)
            node = prev
        chain.reverse()
        states = [root]
        steps = []
        for ei in chain:
            e = edges[ei]
            e["cap"] = max(0.0, e["cap"] - rate)
            if e["produces"]:
                remaining -= rate
            states.append(e["v"])
            steps.append(
                {
                    "from": e["u"],
                    "to": e["v"],
                    "name": e["name"],
                    "kind": e["kind"],
                    "gas": e["gas"],
                }
            )
        paths.append({"rate": rate, "states": states, "steps": steps})
    extracted = math.fsum(p["rate"] for p in paths)
    conditions = record.get("conditions")
    return {
        "product": product,
        "root": root,
        "tof": tof,
        "conditions": conditions if isinstance(conditions, dict) else {},
        "paths": paths,
        "remainder": max(0.0, tof - extracted),
    }


def route_flux(
    record: dict[str, Any] | None,
    routes: list[list[str]],
    *,
    rel_tol: float = _REL_TOL,
    max_paths: int = _MAX_PATHS,
) -> dict[str, Any] | None:
    """The route picker's payload: per-route TOF share and rate off the
    stored kinetics record, ordered by yield (gr553482).

    ``routes`` is the explorer's enumerated path list
    (``diagram['paths']`` — node-id sequences). Each extracted flux path
    maps onto the route whose ``~history``-folded node sequence equals
    the extracted state sequence, tried both with and without the
    release's destination state (a network-declared desorption edge *is*
    a route's last step; a kinetics-added cycle closure lands on the
    root, outside every route). A path that matches no route is
    unattributed flux and lands in the remainder.

    Per route (aligned to ``routes`` order)::

        {"share", "rate", "log10", "dominant", "excluded"}

    ``share``/``rate``/``log10`` are ``None`` for a route no extracted
    path reached (zero or untracked share — the picker draws a dash).
    ``share`` is the route's rate over the TOF — unless the gross
    decomposition exceeds it (re-adsorption makes the TOF a difference,
    not a sum): then ``gross`` is set, shares are fractions of the
    decomposed flux and none reads above 100%, and ``note`` says so.
    ``excluded`` flags a route crossing an edge that has NO transition at
    all between its endpoints — a step the solve never modelled
    (excluded/unpriced, ``skipped_steps``) — which is a stronger claim
    than zero flux and gets its own tag. ``dominant`` marks the single
    largest attributed share. ``order`` is the display order: attributed
    routes by share descending, then the rest in enumeration order.
    ``remainder_share`` is what no ENUMERATED ROUTE accounts for —
    unextracted flux plus extracted paths that matched no route (a path
    that matches nothing must not vanish), on the same denominator as
    the shares.

    ``None`` when the record can't be decomposed
    (:func:`flux_decomposition`) — the picker then renders exactly as it
    did before shares existed.
    """
    decomp = flux_decomposition(record, rel_tol=rel_tol, max_paths=max_paths)
    if decomp is None:
        return None
    tof = decomp["tof"]
    phys_routes = [[_phys(n) for n in r] for r in routes]
    by_seq: dict[tuple[str, ...], int] = {}
    for pi, seq in enumerate(phys_routes):
        by_seq.setdefault(tuple(seq), pi)
    # every directed state pair the solve modelled — a route step absent
    # from this set was never priced (kinetics skipped it)
    modelled: set[tuple[str, str]] = set()
    for t in (record or {}).get("transitions") or ():
        if isinstance(t, dict):
            a, b = _phys(t.get("from")), _phys(t.get("to"))
            modelled.add((a, b))
            modelled.add((b, a))
    out: list[dict[str, Any]] = [
        {
            "share": None,
            "rate": None,
            "log10": None,
            "dominant": False,
            "excluded": any(pair not in modelled for pair in pairwise(seq)),
        }
        for seq in phys_routes
    ]
    for p in decomp["paths"]:
        states = tuple(str(s) for s in p["states"])
        hit = by_seq.get(states)
        if hit is None:
            hit = by_seq.get(states[:-1])
        if hit is None:
            continue  # unattributed — loops / routes over the cap
        out[hit]["rate"] = (out[hit]["rate"] or 0.0) + p["rate"]
    attributed = math.fsum(r["rate"] for r in out if r["rate"])
    extracted = math.fsum(p["rate"] for p in decomp["paths"])
    # gross extraction can exceed the NET tof — consumption edges
    # (re-adsorption) make tof a difference, not a sum. Shares are then
    # fractions of the decomposed flux, so none can read above 100%, and
    # the note says which denominator they carry. The rel_tol slack keeps
    # an fsum landing one ulp over the stored tof from tripping it.
    gross = extracted > tof * (1 + rel_tol)
    denom = extracted if gross else tof
    for r in out:
        if r["rate"]:
            r["share"] = r["rate"] / denom
            r["log10"] = math.log10(r["rate"])
    top = max(range(len(out)), key=lambda i: out[i]["rate"] or 0.0, default=None)
    if top is not None and out[top]["rate"]:
        out[top]["dominant"] = True
    order = sorted(
        range(len(out)),
        key=lambda i: (out[i]["share"] is None, -(out[i]["share"] or 0.0), i),
    )
    # the remainder is what no route accounts for: unextracted flux plus
    # extracted paths that matched no route — subtracting the attributed
    # total, not the extracted one (gr553482 review: an unmatched path
    # used to vanish, leaving shares that never summed to 100%).
    remainder = (extracted - attributed) if gross else max(0.0, tof - attributed)
    return {
        "note": SHARE_NOTE_GROSS if gross else SHARE_NOTE,
        "product": decomp["product"],
        "tof": tof,
        "conditions": decomp["conditions"],
        "routes": out,
        "order": order,
        "gross": gross,
        "remainder_share": remainder / denom,
    }
