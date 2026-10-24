"""Per-net current trees (spec §State "Nets as trees").

Every net, GND included, is a tree: pins are leaves, Steiner points free
junctions, each tree edge one wire. The edge currents follow from the pin
currents by Kirchhoff's current law — solve ``B·f = i``, ``B`` the oriented
node×edge incidence matrix, ``i`` the per-node injection (positive where a
source pin feeds the net, negative where a load draws). In a connected tree
the solution is unique when the injections balance (``Σ i = 0``). A net that
grows loops — the mesh a negative-appetite net adds to close the gaps in its
copper (spec §State "Nets as trees") — has no unique tree current; its
current divides by conductance, one Laplacian solve ``G·φ = i`` with
``G = B diag(g) Bᵀ`` and ``g ∝ width / length``.

Each edge's minimum width is the IPC-2221 width for its current magnitude
(:func:`precis.pcb.rules.ipc2221_track_width_mm`), floored at the net class's
track width, so width steps at Steiner points. A heavy edge changing layer
needs ``⌈I / I_via⌉`` vias in parallel
(:func:`precis.pcb.rules.via_capacity_a`).

Torch-free numpy, like the rest of :mod:`precis.pcb.pr.state`: the seed tree
and its currents are built on the host; the relaxer tensors come later.

The seed tree a board enters the solve with is the IR's own net
decomposition — the star :func:`precis.pcb.ir._net_tree_edges` builds, whose
per-edge current :attr:`precis.pcb.ir.PcbIR.seg_current_a` already resolves
over the trunk-and-branch structure (gr497904). :func:`build_net_trees` reads
that decomposition straight off the IR; :func:`solve_tree_currents` is the
same answer derived from first principles, the mechanism a 4d tree re-plan
(spec §Method 4d) uses when it invents a topology the IR never had.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np

from precis.pcb import rules

# The etypes that INJECT current into a net (a regulator's ``power_out``, a
# driver's ``output``); every other declaring pin draws. Imported rather
# than redefined so the tree's source/load split is the one the IR's own
# current resolution and the ERC already share (ir.py spells it out to break
# the ir → drc → rules → ir import cycle).
from precis.pcb.ir import _CURRENT_DRIVER_ETYPES, PcbIR


@dataclass(frozen=True, slots=True)
class NetTree:
    """One net's routed copper as a tree (spec §State "Nets as trees").

    Nodes are the net's terminals and any Steiner junctions; ``node_pin``
    maps a node to its IR pin id (``-1`` = Steiner). ``node_inject_a`` is the
    signed current injected at each node (``+`` source, ``−`` load, ``0``
    Steiner), balanced to ``Σ = 0`` so :func:`solve_tree_currents` is
    well-posed. ``edges`` are ``(node, node)`` pairs oriented
    ``edges[e,0] → edges[e,1]``; ``edge_seg`` maps an edge back to the IR
    segment it came from (``-1`` for an edge a tree re-plan invented).
    ``edge_current_a`` is the current magnitude on each edge and
    ``edge_width_mm`` the width that carries it (floored at the net class's
    track width), so width steps at the Steiner points current steps at.
    """

    net_id: int
    node_pin: np.ndarray  # (V,) int64, IR pin id or -1 (Steiner)
    node_inject_a: np.ndarray  # (V,) float64, + source / - load / 0
    edges: np.ndarray  # (E, 2) int64, node ids
    edge_seg: np.ndarray  # (E,) int64, IR seg id or -1
    edge_current_a: np.ndarray  # (E,) float64, magnitude
    edge_width_mm: np.ndarray  # (E,) float64

    @property
    def n_nodes(self) -> int:
        return int(self.node_pin.shape[0])

    @property
    def n_edges(self) -> int:
        return int(self.edges.shape[0])

    @property
    def is_tree(self) -> bool:
        """The edge-count criterion ``E == V − 1``. A connected graph that
        meets it is a tree; the IR-seeded shape is connected by
        construction, so this doubles as "no mesh has been grown yet"
        (``E > V − 1`` once a negative-appetite net adds a loop)."""
        return self.n_edges == self.n_nodes - 1


def incidence(edges: np.ndarray, n_nodes: int) -> np.ndarray:
    """The oriented node×edge incidence matrix ``B``: ``+1`` at
    ``edges[e,0]``, ``−1`` at ``edges[e,1]`` (spec §State "Nets as trees")."""
    edges = np.asarray(edges, dtype=np.int64).reshape(-1, 2)
    b = np.zeros((n_nodes, edges.shape[0]), dtype=np.float64)
    if edges.shape[0]:
        cols = np.arange(edges.shape[0])
        b[edges[:, 0], cols] = 1.0
        b[edges[:, 1], cols] = -1.0
    return b


def solve_tree_currents(
    edges: np.ndarray,
    node_inject: np.ndarray,
    n_nodes: int,
    *,
    ref: int = 0,
) -> np.ndarray:
    """Per-edge current from the node injections by KCL, ``B·f = i`` (spec
    §State "Nets as trees").

    A connected tree's incidence ``B`` has rank ``n_nodes − 1``; dropping the
    reference node's row gives a square system whose unique solution is the
    tree current, oriented ``edges[e,0] → edges[e,1]``. Raises on unbalanced
    injections (a net whose sources and loads do not sum to zero is not
    conserved) or a singular system — a disconnected graph, or a mesh, which
    needs :func:`solve_loop_currents`."""
    node_inject = np.asarray(node_inject, dtype=np.float64).reshape(-1)
    edges = np.asarray(edges, dtype=np.int64).reshape(-1, 2)
    scale = max(1.0, float(np.abs(node_inject).sum()))
    if abs(float(node_inject.sum())) > 1e-9 * scale:
        raise ValueError("tree injections do not balance; Σ i must be 0")
    if edges.shape[0] != n_nodes - 1:
        raise ValueError(
            f"{edges.shape[0]} edges for {n_nodes} nodes is not a spanning tree"
        )
    b = incidence(edges, n_nodes)
    keep = [r for r in range(n_nodes) if r != ref]
    try:
        return np.linalg.solve(b[keep], node_inject[keep])
    except np.linalg.LinAlgError as exc:  # disconnected forest, or a cycle
        raise ValueError("edges are not a single connected tree") from exc


def solve_loop_currents(
    edges: np.ndarray,
    node_inject: np.ndarray,
    conductance: np.ndarray,
    n_nodes: int,
    *,
    ref: int = 0,
) -> np.ndarray:
    """Per-edge current for a net with loops, by conductance division (spec
    §State "Nets as trees": a mesh "divides current by conductance").

    One Laplacian solve ``G·φ = i`` with ``G = B diag(g) Bᵀ`` for the node
    potentials ``φ`` (the reference node pinned to 0), then ``f = g · Bᵀ φ``.
    ``conductance`` is ``g ∝ width / length`` per edge. Works on a tree too —
    and agrees with :func:`solve_tree_currents` there — but a tree needs no
    conductances, so that function is the one to call for the seed."""
    node_inject = np.asarray(node_inject, dtype=np.float64).reshape(-1)
    g = np.asarray(conductance, dtype=np.float64).reshape(-1)
    b = incidence(edges, n_nodes)
    lap = b @ np.diag(g) @ b.T
    keep = [r for r in range(n_nodes) if r != ref]
    phi = np.zeros(n_nodes, dtype=np.float64)
    phi[keep] = np.linalg.solve(lap[np.ix_(keep, keep)], node_inject[keep])
    return g * (b.T @ phi)


def edge_min_width_mm(
    current_a: np.ndarray,
    floor_mm: np.ndarray | float,
    *,
    layer_is_outer: bool = False,
    copper_oz: float = 1.0,
    temp_rise_c: float = 10.0,
) -> np.ndarray:
    """Per-edge minimum track width: the IPC-2221 width that carries the
    edge's current (:func:`precis.pcb.rules.ipc2221_track_width_mm`), floored
    at ``floor_mm`` (the net class's track width).

    Current magnitude sizes copper — direction does not — and a zero-current
    edge takes the floor. ``layer_is_outer`` defaults False: an inner layer
    needs the wider trace for the same current, so it is the conservative
    floor to hold a span to before its layer is fixed (spec §State "Nets as
    trees", "IPC-2152 for the stackup's copper weight")."""
    cur = np.abs(np.asarray(current_a, dtype=np.float64))
    ipc = np.array(
        [
            rules.ipc2221_track_width_mm(
                float(c),
                layer_is_outer=layer_is_outer,
                copper_oz=copper_oz,
                temp_rise_c=temp_rise_c,
            )
            if c > 0.0
            else 0.0
            for c in cur.reshape(-1)
        ],
        dtype=np.float64,
    ).reshape(cur.shape)
    return np.maximum(ipc, np.asarray(floor_mm, dtype=np.float64))


def parallel_via_count(current_a: np.ndarray | float, via_dia_mm: float) -> np.ndarray:
    """Vias needed in parallel where a tree edge changes layer: ``⌈I /
    I_via⌉`` (spec §State "Wires, spans and vias"), ``I_via`` a single via's
    capacity (:func:`precis.pcb.rules.via_capacity_a`).

    At least one via per layer change; a via with no modelled capacity
    (``via_dia_mm ≤ 0``) takes one — nothing to size against."""
    cap = rules.via_capacity_a(float(via_dia_mm))
    cur = np.abs(np.asarray(current_a, dtype=np.float64))
    if cap <= 0.0:
        return np.ones(cur.shape, dtype=np.int64)
    return np.maximum(1, np.ceil(cur / cap)).astype(np.int64)


def build_net_trees(
    ir: PcbIR,
    *,
    track_floor_mm: Mapping[int, float] | None = None,
    copper_oz: float = 1.0,
    nets: Iterable[int] | None = None,
) -> dict[int, NetTree]:
    """One :class:`NetTree` per routable net off a built IR, seeded from the
    IR's own star decomposition (:func:`precis.pcb.ir._net_tree_edges`): the
    net's segments are the tree edges, its member pins the leaf nodes.

    ``edge_current_a`` is read straight off :attr:`PcbIR.seg_current_a` — the
    IR already resolves trunk-and-branch currents over this same tree
    (gr497904) — and ``edge_width_mm`` is :func:`edge_min_width_mm` of it,
    floored at ``track_floor_mm[net]`` (the net class's track width; 0 where a
    caller passes none). ``node_inject_a`` is the resolved per-pin current
    (:func:`precis.pcb.rules.pin_current_a_or_none`) signed by driver etype,
    the net's hub taking the balancing residual so the tree is conserved and
    :func:`solve_tree_currents` reproduces ``edge_current_a``; for a net whose
    feeds the IR allocated implicitly the injections are best-effort and the
    IR's ``seg_current_a`` stays authoritative. Nets with fewer than two pins
    (nothing to route) are skipped."""
    floor = dict(track_floor_mm or {})
    seg_net = np.asarray(ir.seg_net)
    seg_a = np.asarray(ir.seg_pin_a)
    seg_b = np.asarray(ir.seg_pin_b)
    seg_cur = np.asarray(ir.seg_current_a, dtype=np.float64)
    want = set(nets) if nets is not None else None
    by_net: dict[int, list[int]] = {}
    for s in range(ir.n_segments):
        n = int(seg_net[s])
        if n < 0 or (want is not None and n not in want):
            continue
        by_net.setdefault(n, []).append(s)

    out: dict[int, NetTree] = {}
    for net_id, segs in by_net.items():
        local: dict[int, int] = {}
        for s in segs:
            for pin in (int(seg_a[s]), int(seg_b[s])):
                if pin not in local:
                    local[pin] = len(local)
        if len(local) < 2:
            continue
        node_pin = np.empty(len(local), dtype=np.int64)
        for pin, idx in local.items():
            node_pin[idx] = pin
        edges = np.array(
            [(local[int(seg_a[s])], local[int(seg_b[s])]) for s in segs],
            dtype=np.int64,
        )
        edge_seg = np.array(segs, dtype=np.int64)
        edge_current = np.array(
            [0.0 if np.isnan(seg_cur[s]) else abs(float(seg_cur[s])) for s in segs],
            dtype=np.float64,
        )
        edge_width = edge_min_width_mm(
            edge_current, float(floor.get(net_id, 0.0)), copper_oz=copper_oz
        )
        inject = np.zeros(len(local), dtype=np.float64)
        for pin, idx in local.items():
            mag = rules.pin_current_a_or_none(ir, pin)
            if mag is None:
                continue
            etype = ir.pin_etype[pin]
            sign = 1.0 if etype in _CURRENT_DRIVER_ETYPES else -1.0
            inject[idx] = sign * float(mag)
        # The hub (highest degree, the star's shared pin) absorbs whatever
        # the signed per-pin exchanges leave unbalanced, so Σ i = 0 exactly.
        degree = np.bincount(edges.reshape(-1), minlength=len(local))
        inject[int(degree.argmax())] -= float(inject.sum())
        out[net_id] = NetTree(
            net_id=net_id,
            node_pin=node_pin,
            node_inject_a=inject,
            edges=edges,
            edge_seg=edge_seg,
            edge_current_a=edge_current,
            edge_width_mm=edge_width,
        )
    return out
