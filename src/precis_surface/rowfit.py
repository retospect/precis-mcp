"""precis_surface.rowfit -- fit a triangulation of degree-6 rows to a
surface of revolution (docs/backlog/precis-surface-kernel.md "Slice --
smooth drum", step 3). The dual of the result is a hex-lattice net whose
5- and 7-rings sit where the smooth target turns.

No marching cubes and no remeshing: the vertices lie on circles (rows)
laid directly on the meridian, so triangles are near-equilateral by
construction.

**Rows.** Row ``i`` holds ``n_i`` vertices at spacing ``edge``. The row
count changes by ``c`` per row: ``c = 0`` on a cylinder, ``c = q`` on a
flat, and ``c`` corners on a cone facet. A defect row is where ``c``
steps; its charge is ``-(n_{i+1} - 2 n_i + n_{i-1})``. Walking off a
cylinder, ``c`` follows the smooth tilt, ``round(q |dr/ds|)``, which is the
midpoint rule of :mod:`precis_surface.revolution`. Off a cylinder a row
goes where its circumference matches, ``2 pi r = n edge``; on a cylinder
rows step ``edge sqrt(3)/2`` along the axis.

**Closures are integer.** Cylinders are anchors with a fixed count (a
tube's ``n``):

- a piece between two anchors must arrive at the next anchor's count;
- a piece ending at the axis must close on a ring of ``q`` (a pole);
- an open end is free.

The geometric walk rarely closes on its own. It is repaired by holding one
facet state for an extra row, or skipping one (at most two such moves per
piece, fewest first), which moves the defect rows by about one row.

**Loft.** In a strip between a smaller ring P and a larger ring Q, each P
vertex takes ``b >= 1`` Q-advances and then one P-advance. A vertex's
degree is ``4 + extra(below) + extra(above)``. On the smaller side
``extra = b``. On the larger side ``extra`` is 0 for a Q vertex that
opens a run of two or more, else 1. Choosing extras that complement the
previous strip keeps every vertex at degree 6; the strips where that is
impossible are exactly the defect rows. The loft starts at a pole so that
a closed flat lid gets regular hexagonal rings. An irregular corner set
cannot close to a point, which is the hexagonal faceting a round lid
forces. Defects are placed farthest-first in angle from earlier defects,
so no two 7s (or 5s) touch.

Pure functions, unit-agnostic, deterministic.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from precis_surface.revolution import Meridian

_TWO_PI = 2.0 * math.pi


@dataclass(frozen=True)
class RowFit:
    """Rows (pole or open end first), the triangulation and its dual."""

    s: tuple[float, ...]
    counts: tuple[int, ...]
    verts: NDArray[np.float64]
    tris: NDArray[np.int64]
    #: vertex ids whose degree is not 6 (the sheet edge excluded)
    defects: tuple[int, ...]
    #: dual: one atom per triangle, snapped to the surface; bonds = shared edges
    atoms: NDArray[np.float64]
    bonds: NDArray[np.int64]

    def degree(self) -> NDArray[np.int64]:
        return np.bincount(self.tris.ravel(), minlength=len(self.verts))


# ---------- the dense meridian ----------


class _Track:
    def __init__(self, m: Meridian, ds: float) -> None:
        pts, _tilt, seg = m.sample(ds)
        self.p = pts
        self.s = np.concatenate(
            [[0.0], np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]
        )
        self.drds = np.gradient(pts[:, 0], self.s)
        self.seg = seg
        self.names = [sg.name for sg in m.segments]
        self.kinds = [sg.kind for sg in m.segments]

    def at(self, s: float) -> tuple[float, float]:
        return (
            float(np.interp(s, self.s, self.p[:, 0])),
            float(np.interp(s, self.s, self.p[:, 1])),
        )

    def slope(self, s: float) -> float:
        return float(np.interp(s, self.s, self.drds))

    def segment(self, s: float) -> int:
        i = int(np.clip(np.searchsorted(self.s, s), 0, len(self.s) - 1))
        return int(self.seg[i])

    def span(self, index: int) -> tuple[float, float]:
        idx = np.where(self.seg == index)[0]
        return float(self.s[idx[0]]), float(self.s[idx[-1]])

    def find_r(self, s0: float, s_lim: float, r_target: float) -> float | None:
        ss = np.linspace(s0, s_lim, max(2, int(abs(s_lim - s0) / 0.005)))
        rr = np.interp(ss, self.s, self.p[:, 0]) - r_target
        hit = np.where(np.sign(rr[1:]) * np.sign(rr[:-1]) <= 0)[0]
        if len(hit) == 0:
            return None
        i = int(hit[0])
        f = rr[i] / (rr[i] - rr[i + 1] + 1e-15)
        return float(ss[i] + f * (ss[i + 1] - ss[i]))


_Extra = dict[int, int]  # facet state c -> +1 hold a row / -1 skip a row


def _walk(
    tr: _Track,
    s0: float,
    n0: int,
    direction: int,
    s_lim: float,
    *,
    edge: float,
    q: int,
    stop_on_cylinder: bool,
    extra: _Extra,
) -> list[tuple[float, int]]:
    """Rows from an anchor row ``(s0, n0)`` towards ``s_lim``."""
    pitch = edge * math.sqrt(3.0) / 2.0
    rows = [(s0, n0)]
    s, n, c, hold = s0, n0, 0, 0
    left = False
    extra = dict(extra)
    while True:
        slope = tr.slope(s) * direction
        geo = min(q, max(0, math.floor(q * abs(slope) + 0.5)))
        if hold > 0:
            hold -= 1
        elif geo != c:
            e = extra.pop(geo, 0)
            if e < 0 and 0 < geo < q:
                geo += 1 if geo > c else -1
            c = geo
            hold = max(e, 0)
        if c == 0:
            if stop_on_cylinder and left:
                return rows
            s_next = s + direction * pitch
            if (s_next - s_lim) * direction > 0:
                return rows
            rows.append((s_next, n))
            s = s_next
            continue
        left = True
        n_next = n + (c if slope > 0 else -c)
        if n_next <= 0:
            rows.append((s_lim, 0))
            return rows
        hit = tr.find_r(s + direction * 1e-6, s_lim, n_next * edge / _TWO_PI)
        if hit is None:
            return rows
        rows.append((hit, n_next))
        s, n = hit, n_next


def _extras(q: int) -> list[_Extra]:
    one = [{c: e} for c in range(1, q + 1) for e in (1, -1)]
    out: list[_Extra] = [{}]
    out += one
    out += [
        {**a, **b}
        for a, b in itertools.combinations(one, 2)
        if next(iter(a)) != next(iter(b))
    ]
    return out


def fit_rows(
    m: Meridian,
    anchors: dict[str, int],
    *,
    edge: float,
    q: int = 6,
    ds: float = 0.01,
) -> list[tuple[float, int]]:
    """Rows ``(s, n)`` along the whole meridian, start to end.

    ``anchors`` maps cylinder segment names to their vertex count (for a
    zigzag ``(n, 0)`` tube, ``n``). Raises ``ValueError`` when a closure
    cannot be met within two moves.
    """
    tr = _Track(m, ds)
    names = tr.names
    order = sorted(anchors, key=names.index)
    if not order:
        raise ValueError("need at least one cylinder anchor")
    for a in order:
        if tr.kinds[names.index(a)] != "cylinder":
            raise ValueError(f"anchor {a!r} is not a cylinder segment")
    mids = {a: 0.5 * sum(tr.span(names.index(a))) for a in order}
    s_end = float(tr.s[-1])
    r_start, r_end = float(tr.p[0, 0]), float(tr.p[-1, 0])

    def solve(
        s0: float, n0: int, direction: int, s_lim: float, ok: object, stop: bool
    ) -> list[tuple[float, int]]:
        for e in _extras(q):
            rows = _walk(
                tr,
                s0,
                n0,
                direction,
                s_lim,
                edge=edge,
                q=q,
                stop_on_cylinder=stop,
                extra=e,
            )
            if ok(rows):  # type: ignore[operator]
                return rows
        raise ValueError(f"no row closure from s={s0:.2f} towards s={s_lim:.2f}")

    def pole_ok(rows: list[tuple[float, int]]) -> bool:
        return len(rows) >= 2 and rows[-1][1] == 0 and rows[-2][1] == q

    def free_ok(rows: list[tuple[float, int]]) -> bool:
        return True

    # the end pieces
    first, last = order[0], order[-1]
    head_ok = pole_ok if r_start <= 1e-9 else free_ok
    tail_ok = pole_ok if r_end <= 1e-9 else free_ok
    head = solve(mids[first], anchors[first], -1, 0.0, head_ok, False)
    tail = solve(mids[last], anchors[last], +1, s_end, tail_ok, False)
    # between consecutive anchors: walk off the first, arrive at the second
    middle: list[tuple[float, int]] = []
    for a, b in itertools.pairwise(order):
        target = anchors[b]
        seg_b = names.index(b)

        def arrive(
            rows: list[tuple[float, int]], t: int = target, sb: int = seg_b
        ) -> bool:
            return rows[-1][1] == t and tr.segment(rows[-1][0]) in (sb, sb - 1, sb + 1)

        piece = solve(mids[a], anchors[a], +1, s_end, arrive, True)
        # then the cylinder rows of b up to its midpoint
        s_b, pitch = piece[-1][0], edge * math.sqrt(3.0) / 2.0
        while s_b + pitch <= mids[b]:
            s_b += pitch
            piece.append((s_b, target))
        middle += piece[1:] if middle else piece
    if len(order) == 1:
        rows = list(reversed(head)) + tail[1:]
    else:
        # head ends at the first anchor's midpoint, middle spans anchors,
        # tail starts at the last anchor's midpoint: re-seed tail and head
        # from the middle's actual anchor rows so rows join without a gap
        s_last = middle[-1][0]
        tail = solve(s_last, anchors[last], +1, s_end, tail_ok, False)
        rows = list(reversed(head))[:-1] + middle + tail[1:]
    return rows


# ---------- the loft ----------


def _circ(a: float, b: float) -> float:
    d = abs(a - b) % _TWO_PI
    return min(d, _TWO_PI - d)


def _pick(
    cands: list[int], k: int, ang: NDArray[np.float64], avoid: list[float]
) -> list[int]:
    chosen: list[int] = []
    pts = list(avoid)
    pool = sorted(cands)
    for _ in range(k):
        best = max(
            pool,
            key=lambda i: (
                min((_circ(float(ang[i]), p) for p in pts), default=0.0),
                -i,
            ),
        )
        chosen.append(best)
        pool.remove(best)
        pts.append(float(ang[best]))
    return chosen


def _special(
    prev: NDArray[np.int64],
    want: int,
    need: int,
    ang: NDArray[np.float64],
    avoid: list[float],
) -> set[int]:
    ideal = [i for i in range(len(prev)) if prev[i] == want]
    rest = [i for i in range(len(prev)) if prev[i] != want]
    if len(ideal) == need:
        return set(ideal)
    if len(ideal) > need:
        return set(ideal) - set(_pick(ideal, len(ideal) - need, ang, avoid))
    return set(ideal) | set(_pick(rest, need - len(ideal), ang, avoid))


def _even(ang: NDArray[np.float64], iters: int = 30) -> NDArray[np.float64]:
    if len(ang) < 3:
        return ang
    base = np.unwrap(ang)
    for _ in range(iters):
        prev = np.roll(base, 1)
        prev[0] -= _TWO_PI
        nxt = np.roll(base, -1)
        nxt[-1] += _TWO_PI
        base = 0.5 * base + 0.25 * (prev + nxt)
    return np.asarray(np.mod(base, _TWO_PI), dtype=np.float64)


def loft(
    counts: list[int], q: int = 6
) -> tuple[list[NDArray[np.float64]], list[tuple[int, int, int]], list[int]]:
    """Ring counts (first may be 0 = pole) -> ring angles, triangles over the
    concatenated rings, and the vertex ids whose degree is not 6."""
    offs = np.cumsum([0] + [max(n, 1) for n in counts]).tolist()
    tris: list[tuple[int, int, int]] = []
    defects: list[int] = []
    seen: list[float] = []
    angles: list[NDArray[np.float64]]
    if counts[0] == 0:
        n1 = counts[1]
        angles = [np.zeros(1), _TWO_PI * (np.arange(n1) + 0.5) / n1]
        tris += [(0, 1 + j, 1 + (j + 1) % n1) for j in range(n1)]
        prev = np.zeros(n1, dtype=np.int64)
        if n1 != q:
            defects.append(0)
        start = 1
    else:
        angles = [_TWO_PI * (np.arange(counts[0]) + 0.5) / counts[0]]
        prev = np.ones(counts[0], dtype=np.int64)
        start = 0
    for i in range(start, len(counts) - 1):
        nx, ny = counts[i], counts[i + 1]
        ox, oy = offs[i], offs[i + 1]
        ax = angles[i]
        if ny == 0:
            tris += [(ox + j, ox + (j + 1) % nx, oy) for j in range(nx)]
            angles.append(np.zeros(1))
            defects += [ox + j for j in range(nx) if prev[j] != 2]
            if nx != q:
                defects.append(oy)
            break
        if ny >= nx:
            corners = _special(prev, 0, ny - nx, ax, seen)
            for j in range(nx):
                if prev[j] + (2 if j in corners else 1) != 2:
                    defects.append(ox + j)
                    seen.append(float(ax[j]))
            ay = np.empty(ny)
            nxt_prev = np.empty(ny, dtype=np.int64)
            k = 0
            for j in range(nx):
                jn = (j + 1) % nx
                b = 2 if j in corners else 1
                for t in range(b):
                    qi = (k + 1) % ny
                    tris.append((ox + j, oy + qi, oy + k % ny))
                    last = t == b - 1
                    nxt_prev[qi] = 1 if last else 0
                    gap = (ax[jn] - ax[j]) % _TWO_PI
                    ay[qi] = (ax[j] + 0.5 * gap) % _TWO_PI if last else ax[j]
                    k += 1
                tris.append((ox + j, ox + jn, oy + k % ny))
        else:
            zset = _special(prev, 2, nx - ny, ax, seen)
            for j in range(nx):
                if prev[j] + (0 if j in zset else 1) != 2:
                    defects.append(ox + j)
                    seen.append(float(ax[j]))
            first = next(j for j in range(nx) if j not in zset)
            runs: list[list[int]] = []
            for k in range(nx):
                j = (first + k) % nx
                if j not in zset:
                    runs.append([j])
                else:
                    runs[-1].append(j)
            ay = np.empty(ny)
            nxt_prev = np.empty(ny, dtype=np.int64)
            for yi, run in enumerate(runs):
                tris += [(ox + j, ox + (j + 1) % nx, oy + yi) for j in run]
                tris.append((ox + (run[-1] + 1) % nx, oy + (yi + 1) % ny, oy + yi))
                nxt_prev[yi] = len(run)
                ang = np.array([ax[j] for j in run] + [ax[(run[-1] + 1) % nx]])
                ay[yi] = math.atan2(np.sin(ang).mean(), np.cos(ang).mean()) % _TWO_PI
        angles.append(_even(ay))
        prev = nxt_prev
    return angles, tris, defects


# ---------- assemble ----------


def realise(
    m: Meridian, rows: list[tuple[float, int]], *, q: int = 6, ds: float = 0.01
) -> RowFit:
    """Loft the rows (pole first if there is one), place vertices on their
    circles, and build the dual net snapped to the surface."""
    if rows[-1][1] == 0 and rows[0][1] != 0:
        rows = rows[::-1]
    tr = _Track(m, ds)
    counts = [n for _, n in rows]
    angles, tris, defects = loft(counts, q)
    verts: list[list[float]] = []
    for (s, n), ang in zip(rows, angles, strict=True):
        r, z = tr.at(s)
        if n == 0:
            verts.append([0.0, 0.0, z])
        else:
            verts += [[r * math.cos(t), r * math.sin(t), z] for t in ang]
    v = np.asarray(verts, dtype=np.float64)
    t = np.asarray(tris, dtype=np.int64)
    edge_ring = set()
    if counts[-1] > 0:
        edge_ring = set(range(len(v) - counts[-1], len(v)))
    if counts[0] > 0:
        edge_ring |= set(range(counts[0]))
    defects = sorted(d for d in set(defects) if d not in edge_ring)
    # dual atoms: triangle centroids snapped to the nearest meridian point
    cent = v[t].mean(axis=1)
    rc = np.hypot(cent[:, 0], cent[:, 1])
    th = np.arctan2(cent[:, 1], cent[:, 0])
    atoms = np.empty_like(cent)
    for k in range(len(cent)):
        j = int(np.argmin((tr.p[:, 0] - rc[k]) ** 2 + (tr.p[:, 1] - cent[k, 2]) ** 2))
        atoms[k] = (
            tr.p[j, 0] * math.cos(th[k]),
            tr.p[j, 0] * math.sin(th[k]),
            tr.p[j, 1],
        )
    edge_tris: dict[tuple[int, int], list[int]] = {}
    for ti, tri in enumerate(t):
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edge_tris.setdefault((min(a, b), max(a, b)), []).append(ti)
    bonds = np.asarray(
        sorted(tuple(x) for x in edge_tris.values() if len(x) == 2), dtype=np.int64
    )
    return RowFit(
        s=tuple(s for s, _ in rows),
        counts=tuple(counts),
        verts=v,
        tris=t,
        defects=tuple(defects),
        atoms=atoms,
        bonds=bonds,
    )
