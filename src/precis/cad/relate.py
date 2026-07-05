"""Inter-part relations — clearance / interference / translational DOF.

ADR 0041 §7. These operate on the *material* regions of whole components,
not raw primitives, so a shaft sitting in a bored hub reads as the radial
wall gap (the trap with naive primitive-pair GJK: it ignores that the
plate has a hole where the shaft sits, and reports a false collision).

The exact tool is the per-component **CSG signed-distance field**: each
primitive exposes an exact signed distance (negative inside), combined
through the booleans —

    union     → min(d)
    intersect → max(d)
    subtract  → max(d_base, −d_cutter)

— whose **sign is exact everywhere** and whose magnitude is exact on the
governing surface (so the bore wall reads true). Clearance is then
``2·min_p max(d_A(p), d_B(p))``: the half-gap is realised at the midpoint
between the closest surfaces. We seed that minimisation on a coarse grid
over the shared region and refine by gradient descent to analytic
precision — deterministic, not Monte-Carlo.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from precis.cad.fold import Diff, Expr, Inter, Leaf, Union
from precis.cad.graph import Design
from precis.cad.vec import Vec3, as_vec3, normalize, vec3

_GRAD_EPS = 1e-6


def component_sdf(design: Design, expr: Expr, p: Vec3) -> float:
    """Exact-sign CSG signed distance of a point to a component's material."""
    p = as_vec3(p)
    if isinstance(expr, Leaf):
        return float(design.instances[expr.iid].placed.distance(p))
    if isinstance(expr, Union):
        return min(component_sdf(design, part, p) for part in expr.parts)
    if isinstance(expr, Inter):
        return max(component_sdf(design, part, p) for part in expr.parts)
    if isinstance(expr, Diff):
        d = component_sdf(design, expr.base, p)
        for c in expr.cutters:
            d = max(d, -component_sdf(design, c, p))
        return d
    raise TypeError(f"unknown expr node: {expr!r}")


def _grad(f, p: Vec3) -> Vec3:
    g = np.zeros(3)
    for i in range(3):
        e = np.zeros(3)
        e[i] = _GRAD_EPS
        g[i] = (f(p + e) - f(p - e)) / (2 * _GRAD_EPS)
    return g


def _region(design: Design, exprs: list[Expr]) -> tuple[Vec3, Vec3]:
    """Shared bounding region (intersection-biased union AABB) of the exprs."""
    los, his = [], []
    for expr in exprs:

        def walk(e: Expr) -> None:
            if isinstance(e, Leaf):
                lo, hi = design.instances[e.iid].placed.aabb()
                if np.all(np.isfinite(lo)):
                    los.append(lo)
                    his.append(hi)
            for child in getattr(e, "parts", ()):
                walk(child)
            base = getattr(e, "base", None)
            if base is not None:
                walk(base)
            for c in getattr(e, "cutters", ()):
                walk(c)

        walk(expr)
    lo = np.min(np.array(los), axis=0)
    hi = np.max(np.array(his), axis=0)
    pad = 0.1 * (hi - lo + 1.0)
    return lo - pad, hi + pad


@dataclass(frozen=True)
class ClearanceResult:
    """Signed minimum gap between two components.

    ``gap`` > 0 → clear (mm of space); ``gap`` < 0 → interference
    (penetration depth, mm). ``point`` is the witness midpoint.
    """

    gap: float
    interfering: bool
    point: Vec3


def _min_max_sdf(
    design: Design,
    ea: Expr,
    eb: Expr,
    offset: Vec3,
    region: tuple[Vec3, Vec3],
    *,
    grid: int = 14,
    iters: int = 120,
    step: float = 1.0,
) -> tuple[float, Vec3]:
    """Minimise ``max(d_A(p − offset), d_B(p))`` over the region.

    The minimum value is the half-gap between ``A`` (shifted by ``offset``)
    and ``B`` — positive when separate, negative when overlapping. Seeded
    on a coarse grid, refined by gradient descent to analytic precision.
    """
    lo, hi = region
    offset = as_vec3(offset)

    def g(p: Vec3) -> float:
        return max(component_sdf(design, ea, p - offset), component_sdf(design, eb, p))

    axes = [np.linspace(lo[i], hi[i], grid) for i in range(3)]
    best_p = vec3(*(0.5 * (lo + hi)))
    best_v = g(best_p)
    for x in axes[0]:
        for y in axes[1]:
            for z in axes[2]:
                p = vec3(x, y, z)
                v = g(p)
                if v < best_v:
                    best_v, best_p = v, p
    p, cur, s = best_p, best_v, step
    for _ in range(iters):
        grad = _grad(g, p)
        nrm = float(np.linalg.norm(grad))
        if nrm < 1e-9:
            break
        cand = np.clip(p - s * grad / nrm, lo, hi)
        cv = g(cand)
        if cv < cur - 1e-12:
            p, cur = cand, cv
        else:
            s *= 0.5
            if s < 1e-8:
                break
    return cur, as_vec3(p)


def clearance(design: Design, a: str, b: str) -> ClearanceResult:
    """Signed min surface gap between components ``a`` and ``b``."""
    ea, eb = design.components[a], design.components[b]
    region = _region(design, [ea, eb])
    half, p = _min_max_sdf(design, ea, eb, vec3(0, 0, 0), region)
    return ClearanceResult(gap=round(2.0 * half, 5), interfering=half < 0, point=p)


# ── connectivity — the assembly contact graph ─────────────────────────────
#: Two components count as *connected* when their signed gap is ≤ this many mm
#: (touching or interfering). It absorbs the small residual of the coarse-grid
#: + gradient-descent minimiser so a true face-to-face contact reads as 0.
CONTACT_TOL_MM = 1e-2


@dataclass(frozen=True)
class Contact:
    """A touching (or interfering) pair of components, with their signed gap."""

    a: str
    b: str
    gap: float
    interfering: bool


@dataclass(frozen=True)
class ConnectivityResult:
    """The contact graph over a design's components.

    ``components`` are the graph nodes; ``contacts`` the edges (pairs whose
    realised material touches or overlaps); ``groups`` the connected
    components of that graph — each a set of parts welded into one solid body
    by mutual contact. ``connected`` is True iff the whole assembly is a
    single such body.
    """

    components: tuple[str, ...]
    contacts: tuple[Contact, ...]
    groups: tuple[tuple[str, ...], ...]
    tol: float

    @property
    def connected(self) -> bool:
        """True iff every component belongs to one contact group (one solid)."""
        return len(self.groups) <= 1

    def _adjacency(self) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {c: set() for c in self.components}
        for c in self.contacts:
            adj[c.a].add(c.b)
            adj[c.b].add(c.a)
        return adj

    def neighbors(self, name: str) -> list[str]:
        """The components directly touching ``name`` (empty ⇒ floating body)."""
        return sorted(self._adjacency().get(name, set()))

    def isolated(self) -> list[str]:
        """Components that touch nothing (only meaningful with ≥2 parts)."""
        if len(self.components) <= 1:
            return []
        adj = self._adjacency()
        return [c for c in self.components if not adj[c]]

    def path(self, a: str, b: str) -> list[str] | None:
        """A contact chain ``a … b`` (BFS, fewest hops), or None if the two
        parts are in different bodies. ``[a]`` when ``a == b``."""
        if a not in self.components or b not in self.components:
            return None
        if a == b:
            return [a]
        adj = self._adjacency()
        prev: dict[str, str | None] = {a: None}
        q: deque[str] = deque([a])
        while q:
            cur = q.popleft()
            for nxt in sorted(adj[cur]):
                if nxt in prev:
                    continue
                prev[nxt] = cur
                if nxt == b:
                    chain = [b]
                    while prev[chain[-1]] is not None:
                        chain.append(prev[chain[-1]])  # type: ignore[arg-type]
                    return list(reversed(chain))
                q.append(nxt)
        return None


def connectivity(design: Design, *, tol: float = CONTACT_TOL_MM) -> ConnectivityResult:
    """Contact graph over a design's components: which bodies touch, the
    connected groups they form, and whether the assembly is one solid.

    Two components are *connected* when their realised (post-cut) material
    touches or overlaps — signed gap ≤ ``tol`` mm via :func:`clearance` (the
    exact-sign CSG SDF, so the overlapping-discs-before-cuts trap never
    arises). This is the graph behind "what's connected to X", "is there a
    path from A to B", and the "a real part is one connected solid" truism.
    """
    comps = tuple(design.components.keys())
    parent = {c: c for c in comps}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    contacts: list[Contact] = []
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            cl = clearance(design, comps[i], comps[j])
            if cl.gap <= tol:
                contacts.append(
                    Contact(
                        a=comps[i], b=comps[j], gap=cl.gap, interfering=cl.interfering
                    )
                )
                parent[find(comps[i])] = find(comps[j])

    grouped: dict[str, list[str]] = {}
    for c in comps:
        grouped.setdefault(find(c), []).append(c)
    groups = tuple(
        tuple(g) for g in sorted(grouped.values(), key=lambda g: comps.index(g[0]))
    )
    return ConnectivityResult(
        components=comps, contacts=tuple(contacts), groups=groups, tol=tol
    )


@dataclass(frozen=True)
class DofResult:
    """Translational freedom of a component along the principal axes.

    Each entry is the mm of travel along ±axis before the moving
    component's material first contacts the fixed component (``inf`` =
    unbounded within the search range).
    """

    moving: str
    fixed: str
    travel: dict[str, float]


def translational_dof(
    design: Design,
    moving: str,
    fixed: str,
    *,
    reach: float | None = None,
    tol: float = 1e-3,
) -> DofResult:
    """How far ``moving`` can translate along ±x/±y/±z before hitting ``fixed``."""
    em, ef = design.components[moving], design.components[fixed]
    mlo, mhi = _region(design, [em])
    flo, fhi = _region(design, [ef])
    span = float(np.max(np.maximum(mhi, fhi) - np.minimum(mlo, flo)))
    reach = reach if reach is not None else 2.0 * span

    def contact_at(offset: Vec3) -> bool:
        # fast reject: shifted AABBs must overlap before materials can.
        slo, shi = mlo + offset, mhi + offset
        if np.any(shi < flo - 1e-9) or np.any(slo > fhi + 1e-9):
            return False
        lo = np.minimum(slo, flo)
        hi = np.maximum(shi, fhi)
        half, _ = _min_max_sdf(design, em, ef, offset, (lo, hi))
        return half <= 0.0

    travel: dict[str, float] = {}
    dirs = {
        "+x": vec3(1, 0, 0),
        "-x": vec3(-1, 0, 0),
        "+y": vec3(0, 1, 0),
        "-y": vec3(0, -1, 0),
        "+z": vec3(0, 0, 1),
        "-z": vec3(0, 0, -1),
    }
    scan = 120  # coarse first-contact scan; AABB fast-reject keeps it cheap
    for name, d in dirs.items():
        d = normalize(d)
        if contact_at(0.0 * d):
            travel[name] = 0.0
            continue
        # coarse scan for the FIRST contact (contact is an interval — the
        # part can pass through and separate again — so we cannot just test
        # the far end).
        first: float | None = None
        prev = 0.0
        for k in range(1, scan + 1):
            t = reach * k / scan
            if contact_at(t * d):
                first = t
                break
            prev = t
        if first is None:
            travel[name] = float("inf")
            continue
        t_lo, t_hi = prev, first
        while t_hi - t_lo > tol:
            mid = 0.5 * (t_lo + t_hi)
            if contact_at(mid * d):
                t_hi = mid
            else:
                t_lo = mid
        travel[name] = round(t_lo, 4)
    return DofResult(moving=moving, fixed=fixed, travel=travel)
