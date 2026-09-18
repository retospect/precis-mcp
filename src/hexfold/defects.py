"""Defects and Volterra surgery on the flat lattice (SPEC section 4).

A defect ``(ring r, site p, dir d)`` is a wedge of ``|6 - r| * 60`` degrees
apexed at the centre of a hexagon adjacent to ``p``; ``p`` is the first site
on the wedge's fixed boundary ray and ``d`` selects the hexagon (``d mod 3``
picks one of the three hexagons incident to ``p``, ``d // 3`` the wedge side).
Excision removes the open sector and glues its trailing ray onto the leading
ray; insertion splits the leading ray and fills the gap with a rotated copy
of the wedge.  The surviving vertices and edges *are* the bond graph.

The embedding is tracked as dart directions: every directed edge carries its
direction in the *head vertex's local chart*, so face orbits follow the
rotation system without any coordinates ever entering the format.
"""

from __future__ import annotations

import math
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .lattice import Lattice, Site, neighbors

Vid = Hashable  # Site for native atoms, ("d", i, u, v, s) for inserted ones

SQRT3 = math.sqrt(3.0)

# Bond direction angles (deg) leaving an A site / B site, for dir classes 0..2.
_BOND_ANG = {0: (30.0, 150.0, 270.0), 1: (90.0, 210.0, 330.0)}


@dataclass(frozen=True)
class Defect:
    """A ring defect: ring size r at site p, wedge orientation dir d."""

    ring: int
    site: Site
    dir: int = 0

    @property
    def wedges(self) -> int:
        return abs(6 - self.ring)


def _unit(deg: float) -> np.ndarray:
    return np.array([math.cos(math.radians(deg)), math.sin(math.radians(deg))])


def defect_apex(d: Defect, lat: Lattice) -> np.ndarray:
    """Centre of the hexagon adjacent to the bond (p, dir-class d mod 3).

    The three hexagon centres adjacent to a site sit at bond_dir +/- 60 deg;
    ``d < 3`` picks the left one, ``d >= 3`` the right one.
    """
    ang = _BOND_ANG[d.site.s][d.dir % 3] + (60.0 if d.dir < 3 else -60.0)
    return lat.cart(d.site) + lat.sigma_A * _unit(ang)


def defect_ray_angle(d: Defect, lat: Lattice) -> float:
    """Angle (deg) of the fixed boundary ray, apex -> site -> outward."""
    c = defect_apex(d, lat)
    v = lat.cart(d.site) - c
    return math.degrees(math.atan2(v[1], v[0])) % 360.0


class Patch:
    """Mutable build-time graph: vertices + directed-edge local directions."""

    def __init__(self, lat: Lattice, sites: list[Site]):
        self.lat = lat
        self.flatpos: dict[Vid, np.ndarray] = {s: lat.cart(s) for s in sites}
        self.hole_b: dict[frozenset[Vid], int] = {}
        # expected boundary term per hole rim: -(6*chi_S - sint_S) of the
        # removed cell complex S (a flat disc of whole cells -> -6)
        self.hole_bexp: dict[frozenset[Vid], int] = {}
        self.pos3: dict[Vid, np.ndarray] | None = None
        self.tube_nm: tuple[int, int] | None = None
        self.cone_p: int | None = None
        # cap(6k,0) flat lid: a flat disc (the hex(k-1) flake), so its rim
        # expects B_expected +6 like a sheet outer rim, not a cap's 0
        # (SPEC 6.1); the C60 hemisphere cap keeps flat_lid False.
        self.flat_lid: bool = False
        self.seam: float = 0.0
        self.edges: set[frozenset[Vid]] = set()
        self.dirs: dict[tuple[Vid, Vid], np.ndarray] = {}
        for a in sites:
            for b in neighbors(a):
                if b in self.flatpos and frozenset((a, b)) not in self.edges:
                    self.edges.add(frozenset((a, b)))
                    dv = lat.cart(b) - lat.cart(a)
                    self.dirs[(a, b)] = dv
                    self.dirs[(b, a)] = -dv
        self._apex = np.zeros(2)

    # -- geometry helpers -------------------------------------------------

    def _angle(self, v: Vid) -> float:
        p = self.flatpos[v] - self._apex
        return math.degrees(math.atan2(p[1], p[0])) % 360.0

    def _onray(self, v: Vid, t: float) -> bool:
        x = (self._angle(v) - t) % 360.0
        return x < 1e-6 or x > 360.0 - 1e-6

    def _insector(self, v: Vid, t1: float, w: float) -> bool:
        x = (self._angle(v) - t1) % 360.0
        return 1e-9 < x < w - 1e-9

    def _site_at(self, p: np.ndarray) -> Vid | None:
        for s, pp in self.flatpos.items():
            if float(np.linalg.norm(pp - p)) < 0.1 * self.lat.sigma_A:
                return s
        return None

    def _rot(self, k: int) -> np.ndarray:
        t = math.radians(60.0 * k)
        c, s = math.cos(t), math.sin(t)
        return np.array([[c, -s], [s, c]])

    def _add_edge(
        self,
        nd: dict[frozenset[Vid], tuple[Vid, Vid, np.ndarray, np.ndarray, int]],
        a: Vid,
        b: Vid,
        da: np.ndarray,
        db: np.ndarray,
        score: int = 0,
    ) -> None:
        if a == b:
            return
        e = frozenset((a, b))
        if e in nd:
            old = nd[e]
            # deterministic resolution independent of set iteration order:
            # higher score wins; ties keep the lexicographically smaller pair
            if old[4] > score or (
                old[4] == score and (str(old[0]), str(old[1])) <= (str(a), str(b))
            ):
                return
        nd[e] = (a, b, np.asarray(da), np.asarray(db), score)

    def _commit(
        self, nd: dict[frozenset[Vid], tuple[Vid, Vid, np.ndarray, np.ndarray, int]]
    ) -> None:
        self.edges = set(nd)
        self.dirs = {}
        for e in self.edges:
            a, b, da, db, _ = nd[e]
            self.dirs[(a, b)] = da
            self.dirs[(b, a)] = db

    # -- surgery ------------------------------------------------------------

    def excise(self, d: Defect) -> None:
        """Remove a |6-r|*60 deg wedge, glue trailing ray to leading ray."""
        c = _apex_of(d, self.lat)
        t1 = _ray_of(d, self.lat)
        w = 60.0 * d.wedges
        self._apex = c
        rm = self._rot(-d.wedges)
        deleted = {
            s
            for s in self.flatpos
            if self._insector(s, t1, w)
            and float(np.linalg.norm(self.flatpos[s] - c)) > 1e-9
        }
        absorbed: dict[Vid, Vid] = {}
        for s in list(self.flatpos):
            if s in deleted:
                continue
            if self._onray(s, (t1 + w) % 360.0):
                tgt = self._site_at(rm @ (self.flatpos[s] - c) + c)
                if tgt is not None and tgt is not s:
                    absorbed[s] = tgt
        nd: dict[frozenset[Vid], Any] = {}
        eye = np.eye(2)
        for e in self.edges:
            a, b = tuple(e)
            if a in deleted or b in deleted:
                continue
            A, B = absorbed.get(a, a), absorbed.get(b, b)
            if A == B:
                continue
            ra = rm if a in absorbed else eye
            rb = rm if b in absorbed else eye
            self._add_edge(
                nd,
                A,
                B,
                ra @ self.dirs[(a, b)],
                rb @ self.dirs[(b, a)],
                score=int(a in absorbed) + int(b in absorbed),
            )
        self._commit(nd)
        for s in deleted | set(absorbed):
            self.flatpos.pop(s, None)

    def insert(self, d: Defect, index: int) -> None:
        """Split the leading ray and fill with a rotated wedge copy."""
        c = _apex_of(d, self.lat)
        t1 = _ray_of(d, self.lat)
        w = 60.0 * d.wedges
        self._apex = c
        rp = self._rot(d.wedges)
        rm = self._rot(-d.wedges)

        def side(s: Vid) -> str:
            a = self._angle(s)
            x = (a - t1) % 360.0
            if x < 1e-6 or x > 360.0 - 1e-6:
                return "R1"
            if abs(x - w) < 1e-6:
                return "R2"
            if x < w:
                return "W"
            if x > 180.0:
                return "L"
            return "H"

        sides = {s: side(s) for s in self.flatpos}
        ray1 = [s for s in self.flatpos if sides[s] == "R1"]
        wedge = [s for s in self.flatpos if sides[s] == "W"]
        ray2 = [s for s in self.flatpos if sides[s] == "R2"]
        dup = {q: ("d", index, *self._frame_coords(q, d)) for q in ray1}
        wcp = {x: ("w", index, *self._frame_coords(x, d)) for x in wedge}
        for q in ray1:
            self.flatpos[dup[q]] = self.flatpos[q]
        for x in wedge:
            self.flatpos[wcp[x]] = self.flatpos[x]
        r1, r2, wset = set(ray1), set(ray2), set(wedge)
        nd: dict[frozenset[Vid], Any] = {}
        for e in self.edges:
            a, b = tuple(e)
            sa, sb = sides[a], sides[b]
            da0, db0 = self.dirs[(a, b)], self.dirs[(b, a)]
            if sa == "R1" and sb == "R1":
                self._add_edge(nd, a, b, da0, db0)
                self._add_edge(nd, dup[a], dup[b], rp @ da0, rp @ db0)
            elif (sa == "R1" and sb == "L") or (sa == "L" and sb == "R1"):
                self._add_edge(nd, a, b, da0, db0)
            elif sa == "R1":
                self._add_edge(nd, dup[a], b, rp @ da0, db0)
            elif sb == "R1":
                self._add_edge(nd, a, dup[b], da0, rp @ db0)
            else:
                self._add_edge(nd, a, b, da0, db0)
        for x in wedge:
            if not isinstance(x, Site):
                continue
            for y in neighbors(x):
                if y in wset:
                    self._add_edge(
                        nd,
                        wcp[x],
                        wcp[y],
                        self.lat.cart(y) - self.lat.cart(x),
                        self.lat.cart(x) - self.lat.cart(y),
                    )
                elif y in r1:
                    self._add_edge(
                        nd,
                        wcp[x],
                        y,
                        self.lat.cart(y) - self.lat.cart(x),
                        self.lat.cart(x) - self.lat.cart(y),
                    )
                elif y in r2:
                    z = self._site_at(rm @ (self.flatpos[y] - c) + c)
                    if z in dup:
                        self._add_edge(
                            nd,
                            wcp[x],
                            dup[z],
                            self.lat.cart(y) - self.lat.cart(x),
                            rp @ (self.lat.cart(x) - self.lat.cart(y)),
                        )
        for q in ray1:
            if not isinstance(q, Site):
                continue
            for z in neighbors(q):
                if z in r2:
                    zz = self._site_at(rm @ (self.flatpos[z] - c) + c)
                    if zz in dup:
                        self._add_edge(
                            nd,
                            q,
                            dup[zz],
                            self.lat.cart(z) - self.lat.cart(q),
                            rp @ (self.lat.cart(q) - self.lat.cart(z)),
                        )
        self._commit(nd)

    def _frame_coords(self, x: Vid, d: Defect) -> tuple[int, int, int]:
        """Wedge-frame (u, v, s) of a native site, per SPEC section 5.

        Origin at the defect site, +a1 along ``dir d``, wedge opening CCW.
        """
        rel = self.flatpos[x] - self.lat.cart(d.site)
        rot = self._rot(-d.dir)
        rel = rot @ rel
        a = self.lat.a
        # solve rel = u*a1 + v*a2 + s*delta in the rotated basis
        a1 = np.array([a, 0.0])
        a2 = np.array([a / 2.0, SQRT3 * a / 2.0])
        delta = (a1 + a2) / 3.0
        for s in (0, 1):
            uv = np.linalg.solve(np.column_stack([a1, a2]), rel - s * delta)
            ui, vi = round(float(uv[0])), round(float(uv[1]))
            if np.allclose(uv, [ui, vi], atol=0.25):
                return (ui, vi, s)
        ui, vi = round(float(uv[0])), round(float(uv[1]))
        return (ui, vi, 0)

    # -- faces, rims, words -------------------------------------------------

    def degrees(self) -> dict[Vid, int]:
        deg: dict[Vid, int] = {v: 0 for v in self.flatpos}
        for e in self.edges:
            for v in e:
                deg[v] += 1
        return deg

    def faces(self) -> list[tuple[list[Vid], list[float]]]:
        """All dart orbits with per-vertex signed turns (deg).

        The walk keeps the orbit's region on a consistent side; interior
        rings turn only one way (all non-negative here), while a rim walk
        wraps the outside and so contains at least one negative turn.
        """
        darts: dict[Vid, list[tuple[float, Vid]]] = {}
        for e in self.edges:
            a, b = tuple(e)
            for x, y in ((a, b), (b, a)):
                dv = self.dirs[(x, y)]
                darts.setdefault(x, []).append((math.atan2(dv[1], dv[0]), y))
        for v in darts:
            darts[v].sort(key=lambda t: t[0])
        visited: set[tuple[Vid, Vid]] = set()
        out: list[tuple[list[Vid], list[float]]] = []
        for e in sorted(self.edges, key=lambda e: sorted(map(str, e))):
            a, b = tuple(e)
            for cur in ((a, b), (b, a)):
                if cur in visited:
                    continue
                face: list[Vid] = []
                turns: list[float] = []
                while cur not in visited:
                    visited.add(cur)
                    face.append(cur[0])
                    a, b = cur
                    lst = darts[b]
                    idx = next(i for i, (_, vv) in enumerate(lst) if vv == a)
                    nxt = lst[(idx - 1) % len(lst)][1]
                    dv_in = self.dirs[(b, a)]
                    dv_out = self.dirs[(b, nxt)]
                    a_in = math.atan2(dv_in[1], dv_in[0])
                    a_out = math.atan2(dv_out[1], dv_out[0])
                    t = (a_out - a_in) % (2 * math.pi) - math.pi
                    turns.append(math.degrees(t))
                    cur = (b, nxt)
                out.append((face, turns))
        return out

    def rings_and_rims(
        self,
    ) -> tuple[list[list[Vid]], list[tuple[list[Vid], list[float]]]]:
        """Split face orbits into interior rings and rim (boundary) walks."""
        rings: list[list[Vid]] = []
        rims: list[tuple[list[Vid], list[float]]] = []
        for face, turns in self.faces():
            if any(t < -1e-6 for t in turns):
                rims.append((face, turns))
            else:
                rings.append(face)
        return rings, rims

    def euler_components(self) -> tuple[int, int]:
        """(V - E + F, number of connected components)."""
        rings, rims = self.rings_and_rims()
        f = len(rings) + len(rims)
        # components via union-find on edges
        parent = {v: v for v in self.flatpos}

        def find(x: Vid) -> Vid:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for e in self.edges:
            a, b = tuple(e)
            parent[find(a)] = find(b)
        return len(self.flatpos) - len(self.edges) + f, len({find(v) for v in parent})

    def rim_word(
        self,
        rim: list[Vid],
        turns: list[float],
        ring_of: dict[frozenset[Vid], int],
    ) -> str:
        """Edge-word for one rim walk.

        One symbol per rim edge: 'z' for a +/-60 deg turn, 'a' for +/-120,
        '<r>' when the interior ring at the vertex is a non-hexagon.  The
        boundary term B is computed combinatorially in build._assemble.
        """
        n = len(rim)
        syms: list[str] = []
        for i in range(n):
            v = rim[i]
            degt = abs(turns[i])
            r = ring_of.get(frozenset((v, rim[(i + 1) % n])))
            if r is not None and r != 6:
                syms.append(str(r))
            elif degt < 90.0:
                syms.append("z")
            else:
                syms.append("a")
        return "".join(syms)


def run_length(word: str) -> str:
    """zaaaazzz -> z·a4·z3 style: 'z4' run-length, '.' separated.

    Only the letter symbols ``z``/``a`` compress: a ring-size symbol is a
    digit string, so ``55`` run-lengthed to ``52`` reads back as the single
    symbol ``52`` (:func:`expand_word` takes ``\\d+`` as one symbol) and two
    equal port words could serialise differently. Digit runs stay
    ``5.5``.
    """
    if not word:
        return ""
    out: list[str] = []
    i = 0
    while i < len(word):
        j = i
        while j < len(word) and word[j] == word[i]:
            j += 1
        if j - i > 1 and word[i] in "za":
            out.append(f"{word[i]}{j - i}")
        else:
            out.extend([word[i]] * (j - i))
        i = j
    return ".".join(out)


def expand_word(word: str) -> str:
    """Inverse of run_length: 'z5.a3.z2' -> 'zzzzzaaazz'."""
    import re

    out = []
    for part in word.split("."):
        m = re.fullmatch(r"([za]|\d+)(\d*)", part)
        if not m:
            raise ValueError(f"bad edge-word part {part!r}")
        sym, cnt = m.group(1), int(m.group(2) or "1")
        out.append(sym * cnt)
    return "".join(out)


def cut_disk(d: Defect, lat: Lattice) -> frozenset[Site]:
    """The disk a surgery owns: its wedge plus one hexagon corona.

    Computed as every flat site within ``corona`` of the wedge apex sector;
    disjoint disks always compose.
    """
    c = _apex_of(d, lat)
    t1 = _ray_of(d, lat)
    w = 60.0 * d.wedges
    radius = 3.6 * lat.sigma_A  # wedge neighbourhood + one hexagon corona
    out: set[Site] = set()
    lim = 6
    for u in range(d.site.u - lim, d.site.u + lim + 1):
        for v in range(d.site.v - lim, d.site.v + lim + 1):
            for s in (0, 1):
                site = Site(u, v, s)
                p = lat.cart(site) - c
                r = float(np.linalg.norm(p))
                if r > radius or r < 1e-9:
                    continue
                ang = math.degrees(math.atan2(p[1], p[0])) % 360.0
                x = (ang - t1) % 360.0
                # the wedge sector (any radius) or inside the corona disk
                if r <= 2.2 * lat.sigma_A or x <= w + 1e-6:
                    out.add(site)
    return frozenset(out)


def disks_disjoint(ds: list[Defect], lat: Lattice) -> bool:
    seen: set[Site] = set()
    for d in ds:
        disk = cut_disk(d, lat)
        if seen & disk:
            return False
        seen |= disk
    return True


def glyph_footprint(
    glyph: str, site: Site, d: int, lat: Lattice
) -> tuple[tuple[str, Defect], ...] | None:
    """Fixed cut arrangement for the named glyphs, or None if unknown.

    Returns a list of (operation, defect) pairs: 'x' excise, 'i' insert.
    The footprints were found by searching disclination dipoles for exactly
    the ring signatures {5,7} and {5,7,7,5}; they are part of the spec, not
    user-tunable.
    """
    sig = lat.sigma_A

    def hex_center(pos: np.ndarray) -> np.ndarray:
        return pos

    if glyph == "57":
        # pentagon at the hexagon adjacent to (site, d), heptagon at the
        # neighbouring centre along the ray-1 direction minus 30 deg.
        d5 = Defect(5, site, d)
        c1 = defect_apex(d5, lat)
        t1 = defect_ray_angle(d5, lat)
        c2 = c1 + SQRT3 * sig * _unit(t1 - 30.0)
        d7 = _defect_at_center(c2, t1, 7, site, d, lat)
        if d7 is None:
            return None
        return (("x", d5), ("i", d7))
    if glyph == "sw":
        d5 = Defect(5, site, d)
        c1 = defect_apex(d5, lat)
        t1 = defect_ray_angle(d5, lat)
        c2 = c1 + SQRT3 * sig * _unit(t1 - 30.0)
        d7a = _defect_at_center(c2, t1, 7, site, d, lat)
        # second dipole: excise at c3 = c1 + sqrt3*e(t1+90), insert at
        # c4 = c3 + sqrt3*e(t1+30), both rays at t1+120
        c3 = c1 + SQRT3 * sig * _unit(t1 + 90.0)
        t2 = (t1 + 120.0) % 360.0
        c4 = c3 + SQRT3 * sig * _unit(t1 + 30.0)
        d5b = _defect_at_center(c3, t2, 5, site, d, lat)
        d7b = _defect_at_center(c4, t2, 7, site, d, lat)
        if d7a is None or d5b is None or d7b is None:
            return None
        return (("x", d5), ("i", d7a), ("x", d5b), ("i", d7b))
    return None


def _defect_at_center(
    c: np.ndarray, ray_deg: float, ring: int, site: Site, d: int, lat: Lattice
) -> Defect | None:
    """A Defect whose apex lands on hexagon centre c with ray1 = ray_deg.

    Internal helper for glyph expansion: we bypass the (site, dir)
    parametrisation by synthesising a site = first site on the ray.
    """
    p = c + lat.sigma_A * _unit(ray_deg)
    best: Site | None = None
    bd = 1e9
    lim_u = round(p[0] / (SQRT3 * lat.sigma_A))
    for u in range(lim_u - 4, lim_u + 5):
        for v in range(lim_u - 4, lim_u + 5):
            for s in (0, 1):
                st = Site(u, v, s)
                dist = float(np.linalg.norm(lat.cart(st) - p))
                if dist < bd:
                    bd, best = dist, st
    if best is None or bd > 0.3 * lat.sigma_A:
        return None
    return _RawDefect(ring, best, d, c, ray_deg)


@dataclass(frozen=True)
class _RawDefect(Defect):
    """A defect pinned to an explicit apex/ray (glyph-internal)."""

    apex: np.ndarray = None  # type: ignore[assignment]
    ray: float = 0.0


# _RawDefect overrides: patch surgery consults these when present.
def _apex_of(d: Defect, lat: Lattice) -> np.ndarray:
    if isinstance(d, _RawDefect):
        return d.apex
    return defect_apex(d, lat)


def _ray_of(d: Defect, lat: Lattice) -> float:
    if isinstance(d, _RawDefect):
        return d.ray
    return defect_ray_angle(d, lat)


def counting_residual(pn: dict[int, int], b: int, chi: int) -> int:
    """Sigma_n (6-n) P_n + B - 6 chi; zero means the counting law holds."""
    return sum((6 - n) * k for n, k in pn.items()) + b - 6 * chi
