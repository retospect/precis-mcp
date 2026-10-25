"""The incremental straight-airwire proxy (Decision 7): crossings plus
length, delta-scored for the one part a move touches.

An airwire is one edge of a net's MST over its PINS' board positions —
pin-level, not centroid, because 1b's pin assignment moves the wire
endpoints the crossings are counted on. ``try_move`` rebuilds only the
moved part's nets' MSTs and re-counts crossings vectorised numpy,
bounding boxes first, so an eval is O(the part's wires × all wires)
instead of O(all wires²).

Exactness contract: ``try_move`` + ``commit`` is bit-for-bit the full
recompute — same crossing predicate as
:func:`precis.pcb.geom.segments_cross` (proper straddle only:
shared-endpoint, collinear and touch-only do not count; same-net pairs
never count), same deterministic Prim order for the MST. The
1000-random-move parity test pins that, because a placer optimising a
metric that drifts from its recompute is optimising a ghost.

Cost is the share-weighted, reference-normalised sum — at the seed
pose each term contributes exactly its share of J (ObjectiveShares /
Decision 18), which is what makes "0.8 crossings / 0.2 ratsnest" a
statement about priority rather than unit conversion.
"""

from __future__ import annotations

import numpy as np

from precis.pcb.topoplace.design import ObjectiveShares, TopoDesign

#: Same straddle epsilon as ``geom.segments_cross`` — parity between the
#: incremental count and the scalar predicate is literal, not "close".
_EPS = 1e-9


def pin_world(d: TopoDesign, pose: np.ndarray, part: int) -> np.ndarray:
    """The part's pins in board space under a ``(4,)`` pose row —
    mirror (side) then quarter-turn, the same order
    :func:`precis.pcb.pose.rel_to_world` fixes, specialised to exact
    quarter turns so no trig enters the hot loop."""
    pins = d.pin_xy[d.pin_part == part]
    x, y, rot_q, side = (float(v) for v in pose[:4])
    mx = np.where(side >= 0.5, -pins[:, 0], pins[:, 0])
    ly = pins[:, 1]
    r = round(rot_q) % 4
    if r == 0:
        rx, ry = mx, ly
    elif r == 1:  # 90° cw-from-north: (x, y) -> (y, -x)
        rx, ry = ly, -mx
    elif r == 2:
        rx, ry = -mx, -ly
    else:  # r == 3
        rx, ry = -ly, mx
    return np.column_stack([x + rx, y + ry])


def all_pin_world(d: TopoDesign, pose: np.ndarray) -> np.ndarray:
    """``pin_world`` for every part at once — (P, 2)."""
    out = np.empty_like(d.pin_xy)
    for part in range(len(d.parts)):
        out[d.pin_part == part] = pin_world(d, pose[part], part)
    return out


def _mst_edges(pts: np.ndarray) -> list[tuple[int, int]]:
    """Prim from vertex 0 with a ``(dist, i, j)`` min key — the same
    deterministic order :func:`precis.pcb.ratsnest._mst_edges` produces
    (ties resolve to the lowest-numbered pair, so a rebuilt MST is
    bit-identical to a fresh one)."""
    n = len(pts)
    if n < 2:
        return []
    dmat = np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2))
    in_tree = np.zeros(n, dtype=bool)
    in_tree[0] = True
    edges: list[tuple[int, int]] = []
    for _ in range(n - 1):
        masked = np.where(in_tree[:, None] & ~in_tree[None, :], dmat, np.inf)
        i, j = np.unravel_index(int(np.argmin(masked)), masked.shape)
        edges.append((int(i), int(j)))
        in_tree[j] = True
    return edges


def _net_wires(d: TopoDesign, net_pins: np.ndarray, pw: np.ndarray) -> np.ndarray:
    """One net's MST airwires as (E, 4) ``[x1, y1, x2, y2]`` rows."""
    pts = pw[net_pins]
    return np.asarray(
        [(pts[i, 0], pts[i, 1], pts[j, 0], pts[j, 1]) for i, j in _mst_edges(pts)],
        dtype=np.float64,
    ).reshape(-1, 4)


def _cross_matrix(
    a: np.ndarray, b: np.ndarray, net_a: np.ndarray, net_b: np.ndarray
) -> np.ndarray:
    """The (n_a, n_b) proper-straddle bool matrix — vectorised
    :func:`precis.pcb.geom.segments_cross`: AABB first, then
    both-orientation straddle; same-net pairs, shared endpoints,
    collinear/touch excluded."""
    n_a, n_b = len(a), len(b)
    if n_a == 0 or n_b == 0:
        return np.zeros((n_a, n_b), dtype=bool)
    aminx, amaxx = np.minimum(a[:, 0], a[:, 2]), np.maximum(a[:, 0], a[:, 2])
    aminy, amaxy = np.minimum(a[:, 1], a[:, 3]), np.maximum(a[:, 1], a[:, 3])
    bminx, bmaxx = np.minimum(b[:, 0], b[:, 2]), np.maximum(b[:, 0], b[:, 2])
    bminy, bmaxy = np.minimum(b[:, 1], b[:, 3]), np.maximum(b[:, 1], b[:, 3])
    overlap = (
        (aminx[:, None] <= bmaxx[None, :])
        & (bminx[None, :] <= amaxx[:, None])
        & (aminy[:, None] <= bmaxy[None, :])
        & (bminy[None, :] <= amaxy[:, None])
    )
    overlap &= net_a[:, None] != net_b[None, :]
    if not overlap.any():
        return np.zeros((n_a, n_b), dtype=bool)
    p1x, p1y = a[:, 0][:, None], a[:, 1][:, None]
    p2x, p2y = a[:, 2][:, None], a[:, 3][:, None]
    p3x, p3y = b[:, 0][None, :], b[:, 1][None, :]
    p4x, p4y = b[:, 2][None, :], b[:, 3][None, :]
    # shares_endpoint: any exact endpoint equality disqualifies the pair.
    shared = (
        ((p1x == p3x) & (p1y == p3y))
        | ((p1x == p4x) & (p1y == p4y))
        | ((p2x == p3x) & (p2y == p3y))
        | ((p2x == p4x) & (p2y == p4y))
    )
    d1 = (p4x - p3x) * (p1y - p3y) - (p4y - p3y) * (p1x - p3x)
    d2 = (p4x - p3x) * (p2y - p3y) - (p4y - p3y) * (p2x - p3x)
    d3 = (p2x - p1x) * (p3y - p1y) - (p2y - p1y) * (p3x - p1x)
    d4 = (p2x - p1x) * (p4y - p1y) - (p2y - p1y) * (p4x - p1x)
    straddle_a = ((d1 > _EPS) & (d2 < -_EPS)) | ((d1 < -_EPS) & (d2 > _EPS))
    straddle_b = ((d3 > _EPS) & (d4 < -_EPS)) | ((d3 < -_EPS) & (d4 > _EPS))
    return overlap & ~shared & straddle_a & straddle_b


def _cross_ab(
    a: np.ndarray, b: np.ndarray, net_a: np.ndarray, net_b: np.ndarray
) -> int:
    return int(_cross_matrix(a, b, net_a, net_b).sum())


def _within(a: np.ndarray, nets: np.ndarray) -> int:
    """Crossings inside one wire set — the strict upper triangle of the
    full matrix (i<j), so each pair counts once."""
    if len(a) < 2:
        return 0
    return int(np.triu(_cross_matrix(a, a, nets, nets), 1).sum())


def _concat(wires: list[np.ndarray], nets: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """Flatten per-net wire rows into one (W, 4) block + (W,) net ids."""
    if not wires:
        return np.empty((0, 4)), np.empty(0, dtype=np.int64)
    return np.concatenate(wires), np.asarray(
        [n for n, w in zip(nets, wires) for _ in range(len(w))],
        dtype=np.int64,
    )


def evaluate(d: TopoDesign, pose: np.ndarray) -> tuple[int, float]:
    """Full recompute — the reference ``try_move`` parity-checks against.
    Returns (crossings, total airwire length mm)."""
    pw = all_pin_world(d, pose)
    wires, nets = _concat(
        [_net_wires(d, np_net, pw) for np_net in d.net_pins],
        list(range(len(d.net_pins))),
    )
    length = float(np.hypot(wires[:, 0] - wires[:, 2], wires[:, 1] - wires[:, 3]).sum())
    return _within(wires, nets), length


class ProxyState:
    """Incremental proxy cost over one pose (spec §1a signature).

    ``try_move`` stages a candidate pose row for one part and returns the
    Δcost; ``commit`` applies every staged row (two for a swap), ``revert``
    drops them. Staged ``try_move`` calls compose — the second sees the
    first's staged wires, so a two-part swap is one atomic decision."""

    def __init__(self, d: TopoDesign, pose: np.ndarray, w: ObjectiveShares) -> None:
        self.d = d
        self.w = w
        self.pose = np.asarray(pose, dtype=np.float64).copy()
        self._pw = all_pin_world(d, self.pose)
        #: committed wires per net: (E, 4) rows
        self._wires: list[np.ndarray] = [
            _net_wires(d, np_net, self._pw) for np_net in d.net_pins
        ]
        self._flat, self._flat_net = _concat(self._wires, list(range(len(d.net_pins))))
        self._cross = _within(self._flat, self._flat_net)
        self._len = (
            float(
                np.hypot(
                    self._flat[:, 0] - self._flat[:, 2],
                    self._flat[:, 1] - self._flat[:, 3],
                ).sum()
            )
            if len(self._flat)
            else 0.0
        )
        #: part id -> net indices it touches
        self._nets_of: list[list[int]] = [[] for _ in d.parts]
        for k, np_net in enumerate(d.net_pins):
            for part in np.unique(d.pin_part[np_net]):
                self._nets_of[int(part)].append(k)
        #: reference magnitudes for share-normalised cost (floors keep a
        #: zero-crossing seed from dividing by zero — same "one per term
        #: in code" fallback the intent-api spec gives R_i ≈ 0).
        self._ref_cross = max(float(self._cross), 1.0)
        self._ref_len = max(self._len, 1.0)
        self._stage: dict[int, np.ndarray] = {}
        self._stage_wires: dict[int, np.ndarray] = {}
        self._d_cross = 0
        self._d_len = 0.0

    @property
    def crossings(self) -> int:
        return self._cross + self._d_cross

    @property
    def length(self) -> float:
        return self._len + self._d_len

    @property
    def cost(self) -> float:
        """Share-weighted, reference-normalised J."""
        return (
            self.w.crossings * self.crossings / self._ref_cross
            + self.w.ratsnest * self.length / self._ref_len
        )

    def _rest_view(self, affected: set[int]) -> tuple[np.ndarray, np.ndarray]:
        """Wires NOT in ``affected``, committed ∪ staged — the other leg
        of a staged delta."""
        wires = [
            self._stage_wires.get(k, self._wires[k])
            for k in range(len(self.d.net_pins))
            if k not in affected
        ]
        nets = [k for k in range(len(self.d.net_pins)) if k not in affected]
        return _concat(wires, nets)

    def _aff_view(self, affected: set[int]) -> tuple[np.ndarray, np.ndarray]:
        """Current (committed ∪ staged) wires OF the affected nets."""
        wires = [self._stage_wires.get(k, self._wires[k]) for k in affected]
        return _concat(wires, list(affected))

    def try_move(self, part: int, pose_row: np.ndarray) -> float:
        """Stage ``pose_row`` for ``part``; return Δcost under the staged
        state (a second ``try_move`` before ``commit`` stacks, which is
        what a two-part swap is). Rebuilds MSTs of the part's nets only."""
        cost_before = self.cost
        staged_pw = self._pw.copy()
        for p, row in self._stage.items():
            staged_pw[self.d.pin_part == p] = pin_world(self.d, row, p)
        staged_pw[self.d.pin_part == part] = pin_world(self.d, pose_row, part)

        affected = set(self._nets_of[part])
        rest_w, rest_n = self._rest_view(affected)
        old_w, old_n = self._aff_view(affected)
        new_wires = {
            k: _net_wires(self.d, self.d.net_pins[k], staged_pw) for k in affected
        }
        new_w, new_n = _concat(list(new_wires.values()), list(new_wires))

        d_cross = (
            _cross_ab(new_w, rest_w, new_n, rest_n)
            + _within(new_w, new_n)
            - _cross_ab(old_w, rest_w, old_n, rest_n)
            - _within(old_w, old_n)
        )
        new_len = (
            float(np.hypot(new_w[:, 0] - new_w[:, 2], new_w[:, 1] - new_w[:, 3]).sum())
            if len(new_w)
            else 0.0
        )
        old_len = (
            float(np.hypot(old_w[:, 0] - old_w[:, 2], old_w[:, 1] - old_w[:, 3]).sum())
            if len(old_w)
            else 0.0
        )
        self._stage[part] = np.asarray(pose_row, dtype=np.float64).copy()
        self._stage_wires.update(new_wires)
        self._d_cross += d_cross
        self._d_len += new_len - old_len
        return self.cost - cost_before

    def commit(self) -> None:
        """Apply every staged row (one for a move, two for a swap)."""
        for p, row in self._stage.items():
            self.pose[p] = row
            self._pw[self.d.pin_part == p] = pin_world(self.d, row, p)
        self._wires = [self._stage_wires.get(k, w) for k, w in enumerate(self._wires)]
        self._flat, self._flat_net = _concat(
            self._wires, list(range(len(self.d.net_pins)))
        )
        self._cross += self._d_cross
        # length is re-derived, not delta-accumulated: float sum order
        # would drift ~1e-11 per commit and the parity contract is exact.
        self._len = (
            float(
                np.hypot(
                    self._flat[:, 0] - self._flat[:, 2],
                    self._flat[:, 1] - self._flat[:, 3],
                ).sum()
            )
            if len(self._flat)
            else 0.0
        )
        self.revert()

    def revert(self) -> None:
        """Drop all staged rows — the pre-``try_move`` state."""
        self._stage.clear()
        self._stage_wires.clear()
        self._d_cross = 0
        self._d_len = 0.0
