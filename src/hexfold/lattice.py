"""Hexagonal lattice arithmetic.

Everything in hexfold is integer bookkeeping on the honeycomb's triangular
Bravais lattice.  Cartesian coordinates exist here only to *derive*
combinatorial facts (which sites a wedge ray hits, tube radii in tests);
the format itself never carries positions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from math import gcd

import numpy as np

SIGMA_DEFAULT = 1.42  # Angstrom, ideal C-C bond

#: sp3 tetrahedral interior angle (0.2: check/stick use this at sp3
#: vertices instead of the ring ideal; 0.1 used the ring ideal everywhere).
SP3_IDEAL_DEG = 109.47


def ideal_angle_deg(ring_size: int, hyb: str) -> float:
    """Ideal interior angle at a ring vertex: sp3 tetrahedral, else ring."""
    if hyb == "sp3":
        return SP3_IDEAL_DEG
    return (ring_size - 2) * 180.0 / ring_size


# Bravais basis (times a = sqrt(3)*sigma), see SPEC section 1.
_B1 = np.array([1.0, 0.0])
_B2 = np.array([0.5, math.sqrt(3.0) / 2.0])

# The six lattice directions, counter-clockwise from +a1, as axial steps.
DIR_AXIAL: tuple[tuple[int, int], ...] = (
    (1, 0),
    (0, 1),
    (-1, 1),
    (-1, 0),
    (0, -1),
    (1, -1),
)


@dataclass(frozen=True)
class Site:
    """A lattice site: unit cell (u, v), sublattice s in {0=A, 1=B}."""

    u: int
    v: int
    s: int

    @property
    def letter(self) -> str:
        return "AB"[self.s]

    def __str__(self) -> str:
        return f"({self.u},{self.v},{self.letter})"

    @staticmethod
    def parse(text: str) -> Site:
        t = text.strip().removeprefix("(").removesuffix(")")
        u, v, s = (p.strip() for p in t.split(","))
        return Site(int(u), int(v), 0 if s.upper() == "A" else 1)


@dataclass(frozen=True)
class Lattice:
    """Lattice parameters; sigma and elements are data, not constants."""

    elements: tuple[str, str] = ("C", "C")
    sigma_A: float = SIGMA_DEFAULT
    #: C-H termination bond length (Angstrom) — data, like sigma_A.
    sigma_CH_A: float = 1.09

    @property
    def a(self) -> float:
        return math.sqrt(3.0) * self.sigma_A

    @property
    def a1(self) -> np.ndarray:
        return self.a * _B1

    @property
    def a2(self) -> np.ndarray:
        return self.a * _B2

    def cart(self, site: Site) -> np.ndarray:
        """Flat cartesian position in Angstrom."""
        p: np.ndarray = site.u * self.a1 + site.v * self.a2
        if site.s:
            p = p + (self.a1 + self.a2) / 3.0
        return p

    def element(self, site: Site) -> str:
        return self.elements[site.s]


def neighbors(site: Site) -> tuple[Site, Site, Site]:
    """The three bonded neighbours of a site on the honeycomb."""
    u, v, s = site.u, site.v, site.s
    if s == 0:
        return (Site(u, v, 1), Site(u - 1, v, 1), Site(u, v - 1, 1))
    return (Site(u, v, 0), Site(u + 1, v, 0), Site(u, v + 1, 0))


def rotate_axial(u: int, v: int, k: int) -> tuple[int, int]:
    """Rotate an axial vector by k*60 deg counter-clockwise (C6 about a site).

    a1 -> a2 and a2 -> a2 - a1 under +60 deg, i.e. (u, v) -> (-v, u + v).
    """
    k %= 6
    for _ in range(k):
        u, v = -v, u + v
    return u, v


def rotate_site(site: Site, k: int) -> Site:
    """C6 rotation of a site about the lattice origin (an A site)."""
    u, v = rotate_axial(site.u, site.v, k)
    return Site(u, v, site.s)


def rot_matrix(k: int) -> np.ndarray:
    t = math.radians(60.0 * k)
    c, s = math.cos(t), math.sin(t)
    return np.array([[c, -s], [s, c]])


# --- (n, m) roll-up -------------------------------------------------------


def chiral_vector(n: int, m: int) -> tuple[int, int]:
    return (n, m)


def dr(n: int, m: int) -> int:
    return gcd(2 * n + m, 2 * m + n)


def translation_vector(n: int, m: int) -> tuple[int, int]:
    """T, the lattice vector perpendicular to C_h spanning one cell."""
    d = dr(n, m)
    return ((2 * m + n) // d, -(2 * n + m) // d)


def n_cells(n: int, m: int) -> int:
    """Graphene unit cells per tube unit cell; atoms = 2 * n_cells * len."""
    return 2 * (n * n + n * m + m * m) // dr(n, m)


def tube_radius(n: int, m: int, lat: Lattice | None = None) -> float:
    lat = lat or Lattice()
    m2 = n * n + n * m + m * m
    return lat.a * math.sqrt(m2) / (2.0 * math.pi)


def _lattice_metric_dot(p: tuple[int, int], q: tuple[int, int]) -> float:
    """Dot product of two axial vectors in units of a^2."""
    return p[0] * q[0] + p[1] * q[1] + 0.5 * (p[0] * q[1] + p[1] * q[0])


def twist_per_cell(n: int, m: int) -> float:
    """Azimuthal rotation of the out-port per unit cell along T, in radians.

    T is exactly perpendicular to C_h, so the two rims carry identical
    azimuth sets; the registry advance is the screw phase of the tube's
    smallest non-cell lattice translation, scaled to one cell.  For
    achiral tubes this is 0.
    """
    if m == 0 or m == n:
        return 0.0
    ch = (n, m)
    t = translation_vector(n, m)
    ch_len2 = _lattice_metric_dot(ch, ch)
    t_len2 = _lattice_metric_metric = _lattice_metric_dot(t, t)
    # smallest lattice vector with positive axial component gives the screw
    best = None
    lim = n + m + 2
    for u in range(-lim, lim + 1):
        for v in range(-lim, lim + 1):
            w = (u, v)
            axial = _lattice_metric_dot(w, t)
            if axial <= 1e-9:
                continue
            frac = axial / t_len2
            if frac >= 1.0 - 1e-9:
                continue
            circ = _lattice_metric_dot(w, ch) / ch_len2
            if best is None or frac < best[0] - 1e-12:
                best = (frac, circ)
    if best is None:
        return 0.0
    frac, circ = best
    return 2.0 * math.pi * circ / frac


def cell_sites(n: int, m: int) -> list[Site]:
    """Sites of one tube unit cell: inside the C_h x T parallelogram."""
    ch = (n, m)
    t = translation_vector(n, m)
    det = ch[0] * t[1] - ch[1] * t[0]  # = -2(n2+nm+m2)/dR
    out: list[Site] = []
    lim = n + m + 2
    for u in range(-lim, lim + 1):
        for v in range(-lim, lim + 1):
            # coordinates in the (C_h, T) basis
            al = (u * t[1] - v * t[0]) / det
            be = (ch[0] * v - ch[1] * u) / det
            if -1e-9 <= al < 1.0 - 1e-9 and -1e-9 <= be < 1.0 - 1e-9:
                out.append(Site(u, v, 0))
                out.append(Site(u, v, 1))
    out.sort(key=lambda s: (s.u, s.v, s.s))
    return out


def tube_sites(n: int, m: int, length: int) -> list[Site]:
    """All atom sites of a tube of `length` unit cells (canonical reps)."""
    return cell_sites(n, m) * 1 if length == 1 else _tube_sites(n, m, length)


def _tube_sites(n: int, m: int, length: int) -> list[Site]:
    t = translation_vector(n, m)
    base = cell_sites(n, m)
    out: list[Site] = []
    for j in range(length):
        for s in base:
            out.append(Site(s.u + j * t[0], s.v + j * t[1], s.s))
    return out


def wrap_tube(site: Site, n: int, m: int, shift: float = 0.0) -> Site:
    """Map a site onto the canonical unit-cell representative mod C_h.

    ``shift`` moves the seam of the fundamental domain to circumferential
    coordinate ``shift`` (used to keep a collar region seam-free).
    """
    ch = (n, m)
    t = translation_vector(n, m)
    det = ch[0] * t[1] - ch[1] * t[0]
    while True:
        al = (site.u * t[1] - site.v * t[0]) / det - shift
        if al < -1e-9:
            site = Site(site.u + ch[0], site.v + ch[1], site.s)
        elif al >= 1.0 - 1e-9:
            site = Site(site.u - ch[0], site.v - ch[1], site.s)
        else:
            return site


def cell_index(site: Site, n: int, m: int) -> int:
    """Axial cell index (along T) of a site on a (n, m) tube."""
    ch = (n, m)
    t = translation_vector(n, m)
    det = ch[0] * t[1] - ch[1] * t[0]
    be = (ch[0] * site.v - ch[1] * site.u) / det
    return math.floor(be + 1e-9)


def circumferential(site: Site, n: int, m: int) -> float:
    """Position of a site around the tube circumference, in [0, 1)."""
    ch = (n, m)
    t = translation_vector(n, m)
    det = ch[0] * t[1] - ch[1] * t[0]
    al = (site.u * t[1] - site.v * t[0]) / det
    return al % 1.0


def cone_opening_deg(p: int) -> float:
    """Cone opening angle theta with sin(theta/2) = 1 - P/6."""
    return math.degrees(2.0 * math.asin(1.0 - p / 6.0))
