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
statement about priority rather than unit conversion. The third term,
``overlap``, is the same-side courtyard overlap AREA in mm² — a real
violation the placer must price between rung-2 evals (Reto ruling
2026-10-25), counted with the ``bottom_by_refdes`` split DRC applies
(parts on opposite faces overlap legally). The fourth term, ``spread``,
is a board-fill repulsion (the loop's whole-board-usage ask): part
pairs sharing NO net that the same collision predicate admits repel
each other through a Gaussian on part origins, so unconnected clumps
disperse over the outline while net-connected parts still cluster
under the ratsnest pull.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from shapely.geometry import Polygon

from precis.pcb.topoplace.design import ObjectiveShares, TopoDesign

#: Same straddle epsilon as ``geom.segments_cross`` — parity between the
#: incremental count and the scalar predicate is literal, not "close".
_EPS = 1e-9


def local_to_world(pts: np.ndarray, pose_row: np.ndarray) -> np.ndarray:
    """Footprint-local points → board space under a ``(4,)`` pose row —
    mirror (side) then quarter-turn, the same order
    :func:`precis.pcb.pose.rel_to_world` fixes, specialised to exact
    quarter turns so no trig enters the hot loop."""
    pts = np.asarray(pts, dtype=np.float64)
    x, y, rot_q, side = (float(v) for v in pose_row[:4])
    mx = np.where(side >= 0.5, -pts[:, 0], pts[:, 0])
    ly = pts[:, 1]
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


def pin_world(d: TopoDesign, pose: np.ndarray, part: int) -> np.ndarray:
    """The part's pins in board space under a ``(4,)`` pose row."""
    return local_to_world(d.pin_xy[d.pin_part == part], pose)


def _ring_geom(d: TopoDesign, row: np.ndarray, part: int):
    """The part's courtyard in board space as a shapely polygon —
    ``None`` for a part with no ring (synthetic designs)."""
    ring = d.part_outline[part] if part < len(d.part_outline) else ()
    pts = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 4:
        return None
    g = Polygon(local_to_world(pts, row)[:-1])
    return g if not g.is_empty and g.area > _EPS else None


def through_hole_parts(d: TopoDesign) -> np.ndarray:
    """Per-part flag: any pin on a through-hole land (``pin_side == 2``).
    A TH part's pads exist on EVERY copper layer (the plated barrel), so
    an opposite-side pair that includes one still collides — the DRC's
    pad model agrees (``pads_for_ir`` places TH pads on all layers).
    Only an SMD-vs-SMD opposite-side pair is a legal facing."""
    th = np.zeros(len(d.parts), dtype=bool)
    if len(d.pin_side):
        for p in np.unique(d.pin_part[d.pin_side == 2]):
            th[int(p)] = True
    return th


def _collides(sides: np.ndarray, th: np.ndarray, i: int, j: int) -> bool:
    """The courtyard-pair test — same mount side, or either part
    through-hole (its barrel occupies the other face's copper too)."""
    return bool(sides[i] == sides[j] or th[i] or th[j])


def _ring_overlap_total(geoms: list[Any], pose: np.ndarray, th: np.ndarray) -> float:
    """Sum of same-side intersection areas over a placed ring list."""
    sides = np.asarray(pose)[:, 3] >= 0.5
    total = 0.0
    for i, gi in enumerate(geoms):
        if gi is None:
            continue
        x0, y0, x1, y1 = gi.bounds
        for j in range(i + 1, len(geoms)):
            gj = geoms[j]
            if gj is None or not _collides(sides, th, i, j):
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            total += float(gi.intersection(gj).area)
    return total


def overlap_area(d: TopoDesign, pose: np.ndarray) -> float:
    """Total SAME-MOUNT-SIDE courtyard overlap in mm² — the count the
    mesh's ``conflicts`` reports and placement DRC flags (parts on
    opposite faces overlap legally; ``_ring_geom`` mirroring makes a
    bottom part's ring its physical one). Full recompute — the
    ``ProxyState`` parity-check baseline and the reference magnitude."""
    return _ring_overlap_total(
        [_ring_geom(d, pose[i], i) for i in range(len(d.parts))],
        pose,
        through_hole_parts(d),
    )


def all_pin_world(d: TopoDesign, pose: np.ndarray) -> np.ndarray:
    """``pin_world`` for every part at once — (P, 2)."""
    out = np.empty_like(d.pin_xy)
    for part in range(len(d.parts)):
        out[d.pin_part == part] = pin_world(d, pose[part], part)
    return out


def _part_shared(d: TopoDesign) -> np.ndarray:
    """(N, N) bool — True when two parts share at least one routed net
    (``net_pins``; plane nets are already dropped). The spread term
    exempts connected pairs so net clusters stay clustered."""
    n = len(d.parts)
    shared = np.zeros((n, n), dtype=bool)
    for pins in d.net_pins:
        members = np.unique(d.pin_part[pins])
        shared[np.ix_(members, members)] = True
    return shared


def _spread_scale(d: TopoDesign, pose: np.ndarray) -> float:
    """The repulsion kernel's range: the mean part cell size
    sqrt(board_area / N) — the outline's area when it carries one, the
    pose bounding box otherwise (synthetic designs)."""
    n = max(len(d.parts), 1)
    if len(d.outline) >= 3:
        area = float(Polygon(np.asarray(d.outline, dtype=np.float64)).area)
    elif len(pose):
        span = pose[:, :2].max(axis=0) - pose[:, :2].min(axis=0)
        area = float(span[0] * span[1])
    else:
        area = 0.0
    return float(np.sqrt(area / n)) if area > _EPS else 1.0


def _spread_pairs(
    cent: np.ndarray,
    sides: np.ndarray,
    th: np.ndarray,
    noshare: np.ndarray,
    scale: float,
) -> float:
    """Σ over unordered ``noshare & collidable`` pairs of
    exp(−d²/2s²) on part origins — the full-recompute body shared by
    :func:`spread_total` and ``ProxyState`` init."""
    d2 = ((cent[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
    kern = np.exp(-d2 / (2.0 * scale * scale))
    collide = (sides[:, None] == sides[None, :]) | (th[:, None] | th[None, :])
    return float(np.triu(kern * (noshare & collide), 1).sum())


def spread_total(d: TopoDesign, pose: np.ndarray) -> float:
    """Board-fill repulsion, full recompute — the reference the
    ``ProxyState`` spread parity-check runs against."""
    if not len(d.parts):
        return 0.0
    noshare = ~_part_shared(d)
    np.fill_diagonal(noshare, False)
    return _spread_pairs(
        np.asarray(pose, dtype=np.float64)[:, :2],
        np.asarray(pose)[:, 3] >= 0.5,
        through_hole_parts(d),
        noshare,
        _spread_scale(d, pose),
    )


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
        #: placed courtyard polygons per part (None = ringless); the
        #: overlap term re-touches only the moved part's pairs. A zero
        #: overlap share skips the ring work entirely — the polygons
        #: and pair scan were the whole marginal cost (3136→1319
        #: evals/s regression, seam fix2).
        self._rings = (
            [_ring_geom(d, self.pose[i], i) for i in range(len(d.parts))]
            if self.w.overlap
            else []
        )
        self._th = through_hole_parts(d)
        self._ov = (
            _ring_overlap_total(self._rings, self.pose, self._th)
            if self._rings
            else 0.0
        )
        #: spread term: the no-shared-net pair mask, kernel scale and
        #: committed repulsion total — built only when the share is on
        #: (an (N, N) mask is the whole setup cost).
        self._noshare: np.ndarray | None = None
        self._s_scale = 1.0
        self._sp = 0.0
        if self.w.spread and len(d.parts):
            self._noshare = ~_part_shared(d)
            np.fill_diagonal(self._noshare, False)
            self._s_scale = _spread_scale(d, self.pose)
            self._sp = _spread_pairs(
                self.pose[:, :2],
                self.pose[:, 3] >= 0.5,
                self._th,
                self._noshare,
                self._s_scale,
            )
        #: reference magnitudes for share-normalised cost (floors keep a
        #: zero-crossing seed from dividing by zero — same "one per term
        #: in code" fallback the intent-api spec gives R_i ≈ 0).
        self._ref_cross = max(float(self._cross), 1.0)
        self._ref_len = max(self._len, 1.0)
        self._ref_ov = max(self._ov, 1.0)
        self._ref_sp = max(self._sp, 1.0)
        self._stage: dict[int, np.ndarray] = {}
        self._stage_wires: dict[int, np.ndarray] = {}
        self._stage_rings: dict[int, Any] = {}
        self._d_cross = 0
        self._d_len = 0.0
        self._d_ov = 0.0
        self._d_sp = 0.0

    @property
    def crossings(self) -> int:
        return self._cross + self._d_cross

    @property
    def length(self) -> float:
        return self._len + self._d_len

    @property
    def overlap(self) -> float:
        """Same-side courtyard overlap area, mm²."""
        return self._ov + self._d_ov

    @property
    def spread(self) -> float:
        """Board-fill repulsion total (dimensionless pair-kernel sum)."""
        return self._sp + self._d_sp

    @property
    def cost(self) -> float:
        """Share-weighted, reference-normalised J."""
        return (
            self.w.crossings * self.crossings / self._ref_cross
            + self.w.ratsnest * self.length / self._ref_len
            + self.w.overlap * self.overlap / self._ref_ov
            + self.w.spread * self.spread / self._ref_sp
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

    def _ring_of(self, part: int):
        """The part's ring under the CURRENT staged-or-committed state."""
        return self._stage_rings.get(part, self._rings[part])

    def _side_of(self, part: int) -> bool:
        row = self._stage.get(part, self.pose[part])
        return bool(row[3] >= 0.5)

    def _pair_overlap(self, part: int, geom, side: bool) -> float:
        """``part``'s same-side overlap total against every OTHER part
        under the current staged-or-committed state — the unit the
        ``try_move`` delta telescopes on: each staged move replaces its
        own pair sums, so a two-part swap lands ``A_new∩B_new −
        A_old∩B_old`` exactly once."""
        if geom is None:
            return 0.0
        x0, y0, x1, y1 = geom.bounds
        total = 0.0
        part_th = self._th[part]
        for j in range(len(self.d.parts)):
            if j == part:
                continue
            gj = self._ring_of(j)
            if gj is None or (
                side != self._side_of(j) and not (part_th or self._th[j])
            ):
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            total += float(geom.intersection(gj).area)
        return total

    def _pair_spread(self, part: int, cent: np.ndarray, side: bool) -> float:
        """``part``'s repulsion total against every OTHER part under the
        current staged-or-committed state — the same telescoping unit as
        ``_pair_overlap``: O(N) numpy per staged move, each staged move
        replacing its own pair row."""
        assert self._noshare is not None
        c = self.pose[:, :2].copy()
        sides = self.pose[:, 3] >= 0.5
        for q, row in self._stage.items():
            c[q] = row[:2]
            sides[q] = row[3] >= 0.5
        d2 = ((c - cent) ** 2).sum(axis=1)
        kern = np.exp(-d2 / (2.0 * self._s_scale * self._s_scale))
        mask = self._noshare[part] & ((sides == side) | self._th)
        return float(kern[mask].sum())

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
        staged_row = np.asarray(pose_row, dtype=np.float64)
        if self.w.overlap:
            new_geom = _ring_geom(self.d, staged_row, part)
            self._d_ov += self._pair_overlap(
                part, new_geom, staged_row[3] >= 0.5
            ) - self._pair_overlap(part, self._ring_of(part), self._side_of(part))
            self._stage_rings[part] = new_geom
        if self.w.spread:
            old_row = self._stage.get(part, self.pose[part])
            self._d_sp += self._pair_spread(
                part, staged_row[:2], bool(staged_row[3] >= 0.5)
            ) - self._pair_spread(part, old_row[:2], bool(old_row[3] >= 0.5))
        self._stage[part] = staged_row.copy()
        self._stage_wires.update(new_wires)
        self._d_cross += d_cross
        self._d_len += new_len - old_len
        return self.cost - cost_before

    def commit(self) -> None:
        """Apply every staged row (one for a move, two for a swap)."""
        for p, row in self._stage.items():
            self.pose[p] = row
            self._pw[self.d.pin_part == p] = pin_world(self.d, row, p)
            if self._rings:
                self._rings[p] = self._stage_rings.get(p, self._rings[p])
        self._ov += self._d_ov
        self._sp += self._d_sp
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
        self._stage_rings.clear()
        self._d_cross = 0
        self._d_len = 0.0
        self._d_ov = 0.0
        self._d_sp = 0.0
