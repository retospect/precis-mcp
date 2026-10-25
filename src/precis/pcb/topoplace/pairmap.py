"""The pairwise part map (Decision 8): every net-connected part pair,
scored for connectivity strength and for how "straightforward" the pair
is — the exact crossing count between two facing pin rows, which R14
shows is an inversion count between their pin orders.

Facing order, per spec: for pair (a, b) at relative rotations (ra, rb),
project each part's pins on nets shared with the other onto the axis
perpendicular to the centre line, order them, map b's sequence through
the shared nets, count inversions by merge sort. A shared net's pin on
a side is the one nearest the other part's centre — the pin the net
would actually face with. Rotations follow the pose convention
(mirror-then-quarter-turn); the mirror axis explores a back-side part
b (``crossings[:, :, m]``, m = "b flipped").

The map is built once from ``d.pose0`` for the centre-line direction;
pairs whose centres coincide or are unplaced fall back to a horizontal
centre line (the order projections still capture the relative-rotation
content the map exists for).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from precis.pcb.topoplace.design import TopoDesign


def _rotate_xy(pins: np.ndarray, rot_q: int, mirror: bool) -> np.ndarray:
    """Quarter-turn of part-frame offsets — same convention as
    :func:`~.proxy.pin_world` (mirror x first), minus the translate."""
    mx = -pins[:, 0] if mirror else pins[:, 0]
    ly = pins[:, 1]
    r = rot_q % 4
    if r == 0:
        rx, ry = mx, ly
    elif r == 1:
        rx, ry = ly, -mx
    elif r == 2:
        rx, ry = -mx, -ly
    else:
        rx, ry = -ly, mx
    return np.column_stack([rx, ry])


def _inversions(seq: list[int]) -> int:
    """Inversion count by merge sort — the crossing count between two
    facing rows (R14). ``seq`` is b's rank order read in a's order."""
    n = len(seq)
    if n < 2:
        return 0
    buf = seq[:]

    def sort(lo: int, hi: int) -> int:
        if hi - lo < 2:
            return 0
        mid = (lo + hi) // 2
        inv = sort(lo, mid) + sort(mid, hi)
        i, j, k = lo, mid, lo
        while i < mid and j < hi:
            if buf[i] <= buf[j]:
                seq[k] = buf[i]
                i += 1
            else:
                seq[k] = buf[j]
                j += 1
                inv += mid - i
            k += 1
        while i < mid:
            seq[k] = buf[i]
            i += 1
            k += 1
        while j < hi:
            seq[k] = buf[j]
            j += 1
            k += 1
        buf[lo:hi] = seq[lo:hi]
        return inv

    return sort(0, n)


@dataclass(frozen=True, slots=True)
class PairStat:
    a: int
    b: int
    strength: float  # Σ over shared nets of 1/(k-1)
    crossings: np.ndarray  # (4, 4, 2) inversions per relative rotation (+mirror of b)
    best: tuple[int, int]  # argmin rotation pair (min over mirror too)
    best_crossings: int


@dataclass(frozen=True, slots=True)
class PairSummary:
    """Board-level read of the pair map (Decision 8: "mean, p95, worst
    pairs"). ``worst`` is the top-5 pairs by
    ``best_crossings × strength`` — high-count pairs that also pull hard
    are the ones the summary should name."""

    mean: float
    p95: float
    worst: tuple[PairStat, ...]


def _pair_crossings(
    d: TopoDesign,
    a: int,
    b: int,
    shared: list[tuple[int, int, int]],
    perp: np.ndarray,
) -> np.ndarray:
    """The (4, 4, 2) inversion grid: rows a-rotation, cols b-rotation,
    last axis b's mirror. ``shared`` rows are ``(net_index, pin_a,
    pin_b)`` — one representative pin per side (nearest the other part's
    centre in the part's own frame at ``pose0``, fixed before the grid
    is swept, so the grid stays a pure function of relative rotation)."""
    ca = np.asarray(d.pose0[a, :2])
    cb = np.asarray(d.pose0[b, :2])
    if not np.isfinite(ca).all():
        ca = np.zeros(2)
    if not np.isfinite(cb).all():
        cb = np.zeros(2)
    grid = np.zeros((4, 4, 2), dtype=np.int32)
    # per (rotation, mirror): each side's net -> (perp projection,
    # distance² to the other part's centre). The distance is the
    # tiebreak: a quarter-turned part's pins can project collinear with
    # the centre line (a column, not a row), and then the crossing
    # order IS near-to-far — pins nearer the other part exit first.
    proj: dict[tuple[int, int, int], dict[int, tuple[float, float]]] = {}
    for part in (a, b):
        pins_idx = np.flatnonzero(d.pin_part == part)
        centre = cb if part == b else ca
        other = ca if part == b else cb
        for r in range(4):
            for m in range(2 if part == b else 1):
                placed = _rotate_xy(d.pin_xy[pins_idx], r, bool(m)) + centre
                by_pin = dict(zip(pins_idx.tolist(), placed.tolist()))
                pmap: dict[int, tuple[float, float]] = {}
                for net_i, pa, pb in shared:
                    p = np.asarray(by_pin[pa if part == a else pb])
                    pmap[net_i] = (
                        float(p @ perp),
                        float(np.sum((p - other) ** 2)),
                    )
                proj[(part, r, m)] = pmap
    nets = [s[0] for s in shared]
    for ra in range(4):
        order_a = sorted(nets, key=lambda n: proj[(a, ra, 0)][n])
        for rb in range(4):
            for m in range(2):
                rank_b = {
                    n: i
                    for i, n in enumerate(
                        sorted(nets, key=lambda n: proj[(b, rb, m)][n])
                    )
                }
                seq = [rank_b[n] for n in order_a]
                grid[ra, rb, m] = _inversions(seq)
    return grid


class PairMap:
    """All strength > 0 part pairs (spec stores nothing else)."""

    def __init__(self, pairs: dict[tuple[int, int], PairStat]) -> None:
        self.pairs = pairs

    def stats(self) -> PairSummary:
        if not self.pairs:
            return PairSummary(0.0, 0.0, ())
        best = np.asarray([s.best_crossings for s in self.pairs.values()])
        worst = tuple(
            sorted(
                self.pairs.values(),
                key=lambda s: s.best_crossings * s.strength,
                reverse=True,
            )[:5]
        )
        return PairSummary(float(best.mean()), float(np.percentile(best, 95)), worst)

    def seed_rotations(self) -> np.ndarray:
        """(N,) quarter turns — greedy over strongest pairs: fix both
        sides of the strongest unassigned pair, then the free side of
        every half-assigned one, minimising that pair's inversions."""
        n = (
            max(
                (i for ab in self.pairs for i in ab),
                default=-1,
            )
            + 1
        )
        out = np.zeros(n, dtype=np.int64)
        assigned = np.zeros(n, dtype=bool)
        for (a, b), s in sorted(self.pairs.items(), key=lambda kv: -kv[1].strength):
            if not assigned[a] and not assigned[b]:
                out[a], out[b] = s.best
                assigned[a] = assigned[b] = True
            elif not assigned[a]:
                # b fixed: cheapest (ra, m) for a given b's rotation
                sub = s.crossings[:, int(out[b]), :]
                out[a] = int(np.unravel_index(int(np.argmin(sub)), sub.shape)[0])
                assigned[a] = True
            elif not assigned[b]:
                sub = s.crossings[int(out[a]), :, :]
                out[b] = int(np.unravel_index(int(np.argmin(sub)), sub.shape)[0])
                assigned[b] = True
        return out

    def clusters(self, k: int) -> list[list[int]]:
        """Agglomerative on strength — union the strongest pairs first,
        stop when only k components remain (a max-weight-forest cut)."""
        n = max((i for ab in self.pairs for i in ab), default=-1) + 1
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def components() -> int:
            roots = {find(a) for ab in self.pairs for a in ab}
            return len(roots)

        for (a, b), _s in sorted(self.pairs.items(), key=lambda kv: -kv[1].strength):
            if components() <= k:
                break
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
        groups: dict[int, list[int]] = {}
        for a, b in self.pairs:
            for part in (a, b):
                groups.setdefault(find(part), []).append(part)
        return [sorted(set(m)) for m in groups.values()]


def pair_map(d: TopoDesign) -> PairMap:
    """Build the map (spec signature). Only pairs sharing a signal net
    are stored — a pair with no shared wire has no strength and no
    facing order to read."""
    n_parts = len(d.parts)
    part_nets: list[list[int]] = [[] for _ in d.parts]
    net_part_set: list[set[int]] = []
    for net_i, pins in enumerate(d.net_pins):
        parts = {int(p) for p in np.unique(d.pin_part[pins])}
        net_part_set.append(parts)
        for p in parts:
            part_nets[p].append(net_i)

    # centre line + perpendicular axis from pose0 (fallback: +x line,
    # +y order axis — see module docstring).
    centres = np.asarray(d.pose0[:, :2])

    pairs: dict[tuple[int, int], PairStat] = {}
    for a in range(n_parts):
        for b in range(a + 1, n_parts):
            shared_nets = [net_i for net_i in part_nets[a] if b in net_part_set[net_i]]
            if not shared_nets:
                continue
            strength = sum(1.0 / (len(net_part_set[n]) - 1) for n in shared_nets)
            ca, cb = centres[a], centres[b]
            line = cb - ca
            norm = float(np.hypot(line[0], line[1]))
            if not np.isfinite(norm) or norm < 1e-9:
                perp = np.array([0.0, 1.0])
            else:
                perp = np.array([-line[1], line[0]]) / norm
            # representative pin per net per side: nearest the other's
            # centre in the part's own (unrotated) frame.
            shared: list[tuple[int, int, int]] = []
            for net_i in shared_nets:
                pins = d.net_pins[net_i]
                pa = min(
                    (int(p) for p in pins if d.pin_part[p] == a),
                    key=lambda p: float(
                        np.sum(
                            (d.pin_xy[p] + np.nan_to_num(ca) - np.nan_to_num(cb)) ** 2
                        )
                    ),
                )
                pb = min(
                    (int(p) for p in pins if d.pin_part[p] == b),
                    key=lambda p: float(
                        np.sum(
                            (d.pin_xy[p] + np.nan_to_num(cb) - np.nan_to_num(ca)) ** 2
                        )
                    ),
                )
                shared.append((net_i, pa, pb))
            grid = _pair_crossings(d, a, b, shared, perp)
            flat = int(np.argmin(grid))
            best = (flat // 8, (flat % 8) // 2)
            pairs[(a, b)] = PairStat(
                a=a,
                b=b,
                strength=float(strength),
                crossings=grid,
                best=best,
                best_crossings=int(grid[best[0], best[1], :].min()),
            )
    return PairMap(pairs)


__all__ = ["PairMap", "PairStat", "PairSummary", "pair_map"]
