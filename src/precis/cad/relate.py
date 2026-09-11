"""Inter-part relations — clearance / interference / translational DOF.

These operate on the *material* regions of whole components,
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
between the closest surfaces. That minimisation is seeded on a coarse grid
over the shared region *plus* closest-point seeds derived from the bodies
themselves, and refined by a nonsmooth descent to analytic precision —
deterministic, not Monte-Carlo.

**Why the extra seeds** (gr334763): a coarse grid alone cannot see a
shallow interpenetration. A 0.23 Å overlap lens inside a 24 Å query region
is ~⅛ of the seed spacing, so no grid point lands inside it, the descent
starts outside both bodies and stalls on the ``d_A == d_B`` ridge — and a
real −0.23 Å interference reported as +0.022 Å "clear". The cure is two
parts, both here in :func:`_min_max_sdf`:

1. **Closest-point seeds.** Alternating projection between the two bodies
   (each exact SDF gives the projection in one Newton step) walks straight
   to the contact, whatever the grid spacing; the segment joining that
   pair is then sampled, so a lens arbitrarily thinner than the grid still
   gets seeds inside it.
2. **Ridge-following descent.** ``max(d_A, d_B)`` is nonsmooth where the
   two are equal, and its minimum lies *on* that ridge. Steepest descent
   stalls there; each step therefore also tries the min-norm element of
   ``conv{∇d_A, ∇d_B}`` (the Clarke steepest-descent direction — the one
   direction that decreases *both*), and takes whichever candidate is
   lower.

Every tolerance here is relative to a governing length (the query region's
diagonal, or the smaller body's, per the units policy in
``docs/backlog/multiscale-design-architecture.md``): the same code has to
hold at Å and at km, and an absolute epsilon holds at neither.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from precis.cad.fold import Diff, Expr, Inter, Leaf, Union
from precis.cad.graph import Design
from precis.cad.vec import Vec3, as_vec3, normalize, vec3

#: Central-difference step for numeric SDF gradients, as a fraction of the
#: governing length. Relative, so the finite difference straddles the same
#: *shape* of feature whether the design is drawn in Å or in km.
_GRAD_REL_EPS = 1e-7

#: Initial descent step, as a fraction of the governing length.
_STEP_REL = 0.05

#: Descent gives up once the trust step falls below this fraction of the
#: governing length (≈ float64's useful resolution for a length).
_STEP_FLOOR_REL = 1e-9

#: A candidate must beat the incumbent by this fraction of the governing
#: length to count as progress (guards a descent that only churns noise).
_IMPROVE_REL = 1e-13

#: Samples taken along each structural seed segment. Density is set by the
#: segment's own length, so it is scale-relative for free.
_SEGMENT_SEEDS = 17

#: Alternating closest-point projections used to find the contact seed pair.
_PROJECT_ROUNDS = 3

#: Points per axis in the coarse region grid. It is a *coverage* net, not a
#: resolving one — the closest-point seeds and the descent do the resolving.
#: It stays at the historical 14 all the same: the closest-point seeds are
#: derived from the two AABB centroids, and a carved body (the wheel rim,
#: whose centroid sits in its own bore) gives them nothing to work from, so
#: the blind net is still what covers that case.
SEED_GRID = 14

#: How many of the best seeds get a full descent. The minimum of
#: ``max(d_A, d_B)`` has few basins; a handful of starts covers them without
#: multiplying the per-pair cost (this runs per block pair in clearance and
#: validate views).
_STARTS = 4


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


def _grad(f, p: Vec3, eps: float) -> Vec3:
    g = np.zeros(3)
    for i in range(3):
        e = np.zeros(3)
        e[i] = eps
        g[i] = (f(p + e) - f(p - e)) / (2 * eps)
    return g


def _min_norm_on_segment(ga: Vec3, gb: Vec3) -> Vec3:
    """The shortest vector in ``conv{ga, gb}``.

    For ``g = max(f_a, f_b)`` this is the Clarke steepest-descent direction
    where the two are equally active: ``-v`` decreases *both* branches, so
    the descent can slide **along** the ``f_a == f_b`` ridge instead of
    zig-zagging across it. ``v ≈ 0`` means the ridge point is stationary —
    a genuine local minimum, not a stall.
    """
    diff = ga - gb
    den = float(diff @ diff)
    if den <= 0.0:
        return ga
    t = float(np.clip((ga @ diff) / den, 0.0, 1.0))
    return (1.0 - t) * ga + t * gb


def _bounds(design: Design, expr: Expr) -> tuple[Vec3, Vec3] | None:
    """Union AABB of one expr's leaf primitives (``None`` = unbounded/empty)."""
    los: list[Vec3] = []
    his: list[Vec3] = []

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
    if not los:
        return None
    return np.min(np.array(los), axis=0), np.max(np.array(his), axis=0)


def _diagonal(box: tuple[Vec3, Vec3] | None) -> float:
    if box is None:
        return 0.0
    return float(np.linalg.norm(box[1] - box[0]))


def _governing_length(design: Design, exprs: list[Expr]) -> float:
    """The length every tolerance in this module is expressed against.

    Per the units policy (feature size, else bbox diagonal) it is the
    **smaller** body's diagonal — the smallest thing the query has to
    resolve — falling back to the joint region when a body is unbounded.
    """
    diags = [_diagonal(_bounds(design, e)) for e in exprs]
    positive = [d for d in diags if d > 0.0]
    if positive:
        return min(positive)
    lo, hi = _region(design, exprs)
    return float(np.linalg.norm(hi - lo)) or 1.0


def _region(design: Design, exprs: list[Expr]) -> tuple[Vec3, Vec3]:
    """Shared bounding region (intersection-biased union AABB) of the exprs."""
    boxes = [b for b in (_bounds(design, e) for e in exprs) if b is not None]
    lo = np.min(np.array([b[0] for b in boxes]), axis=0)
    hi = np.max(np.array([b[1] for b in boxes]), axis=0)
    span = hi - lo
    # 10 % of each axis' span, floored at 10 % of the overall diagonal so a
    # flat (zero-thickness) axis still gets a working margin. The floor is
    # relative: a fixed ``+1.0`` pad is a whole extra body at Å scale and
    # invisible at km scale.
    pad = 0.1 * np.maximum(span, 0.1 * float(np.linalg.norm(span)))
    return lo - pad, hi + pad


@dataclass(frozen=True)
class ClearanceResult:
    """Signed minimum gap between two components.

    ``gap`` > 0 → clear (mm of space); ``gap`` < 0 → interference
    (penetration depth, mm). ``point`` is the witness midpoint.

    ``resolution`` is the scale-relative band inside which this query
    cannot tell "clear" from "touching" from "just interfering" — a
    fraction (:data:`CONTACT_TOL_REL`) of the governing length, so it means
    the same thing at Å and at km. A ``|gap| <= resolution`` result is
    *touching within resolution*, and renderers must say so rather than
    print an unqualified "clear" (gr334763).
    """

    gap: float
    interfering: bool
    point: Vec3
    resolution: float = 0.0


def _round_relative(value: float, scale: float) -> float:
    """Round to ~6 significant figures **of the governing length**.

    An absolute ``round(v, 5)`` is a different promise at every scale: it
    is 6 significant figures on a 24 mm query and it silently zeroes every
    gap in a design drawn in metres at nanometre sizes.
    """
    if not math.isfinite(scale) or scale <= 0.0:
        return float(value)
    digits = int(np.clip(6 - math.floor(math.log10(scale)), 0, 15))
    return round(float(value), digits)


def _project_onto(f, p: Vec3, eps: float) -> Vec3:
    """One Newton step of ``p`` onto ``f``'s zero level set (its surface).

    Exact in one step for a planar face and quadratically convergent
    otherwise, because ``f`` is a true Euclidean signed distance
    (``|∇f| = 1``).
    """
    grad = _grad(f, p, eps)
    nrm = float(np.linalg.norm(grad))
    if nrm < 1e-9:
        return as_vec3(p)
    return as_vec3(p - f(p) * grad / (nrm * nrm))


def _contact_seeds(
    da,
    db,
    box_a: tuple[Vec3, Vec3] | None,
    box_b: tuple[Vec3, Vec3] | None,
    eps: float,
) -> list[Vec3]:
    """Seeds drawn from the bodies themselves, not from the query region.

    Alternating closest-point projection (``q_a`` onto A, ``q_b`` onto B,
    repeatedly) converges on the closest surface pair, so the segment
    ``q_a … q_b`` runs straight through the contact — and sampling it
    puts seeds *inside* an overlap lens however thin it is relative to the
    grid. The AABB-centroid segment is sampled too, as the fallback for
    the cases where projection degenerates (concentric bodies, a body whose
    centroid sits in its own carved-out void).
    """
    if box_a is None or box_b is None:
        return []
    ca = as_vec3(0.5 * (box_a[0] + box_a[1]))
    cb = as_vec3(0.5 * (box_b[0] + box_b[1]))

    qa, qb = ca, cb
    for _ in range(_PROJECT_ROUNDS):
        qa = _project_onto(da, qb, eps)
        qb = _project_onto(db, qa, eps)

    seeds: list[Vec3] = [ca, cb, qa, qb]
    for u, v in ((qa, qb), (ca, cb)):
        span = float(np.linalg.norm(np.asarray(v) - np.asarray(u)))
        if span <= 0.0:
            continue
        # Overshoot both ends: with a deep overlap the true minimum sits
        # *past* the projected surface points, not between them.
        for t in np.linspace(-0.25, 1.25, _SEGMENT_SEEDS):
            seeds.append(as_vec3(np.asarray(u) + t * (np.asarray(v) - np.asarray(u))))
    return seeds


def _descend(
    da,
    db,
    lo: Vec3,
    hi: Vec3,
    start: Vec3,
    *,
    iters: int,
    step: float,
    scale: float,
    stop_at_contact: bool,
) -> tuple[float, Vec3]:
    """Minimise ``max(da, db)`` locally from ``start``, ridge-aware.

    Each iteration proposes two candidates — down the active branch's own
    gradient, and down the min-norm subgradient
    (:func:`_min_norm_on_segment`) — and takes the lower. The second is
    what rescues the ``da == db`` ridge, where the first cannot move
    without going uphill and the trust step collapses at a point that is
    not a minimum (and, for a shallow overlap, has the wrong sign).
    """
    eps = _GRAD_REL_EPS * scale
    improve = _IMPROVE_REL * scale
    floor = _STEP_FLOOR_REL * scale
    p = as_vec3(start)
    va, vb = da(p), db(p)
    cur = max(va, vb)
    s = step
    for _ in range(iters):
        ga = _grad(da, p, eps)
        gb = _grad(db, p, eps)
        primary = ga if va >= vb else gb
        directions = [primary, _min_norm_on_segment(ga, gb)]
        best_cand: Vec3 | None = None
        best_v = cur
        best_pair = (va, vb)
        for d in directions:
            nrm = float(np.linalg.norm(d))
            if nrm < 1e-9:
                continue
            cand = as_vec3(np.clip(p - s * d / nrm, lo, hi))
            ca, cb = da(cand), db(cand)
            cv = max(ca, cb)
            if cv < best_v - improve:
                best_cand, best_v, best_pair = cand, cv, (ca, cb)
        if best_cand is None:
            s *= 0.5
            if s < floor:
                break
            continue
        p, cur, (va, vb) = best_cand, best_v, best_pair
        if stop_at_contact and cur <= 0.0:
            break
    return cur, as_vec3(p)


def _min_max_sdf(
    design: Design,
    ea: Expr,
    eb: Expr,
    offset: Vec3,
    region: tuple[Vec3, Vec3],
    *,
    grid: int = SEED_GRID,
    iters: int = 80,
    step: float | None = None,
    stop_at_contact: bool = False,
    starts: int = _STARTS,
) -> tuple[float, Vec3]:
    """Minimise ``max(d_A(p − offset), d_B(p))`` over the region.

    The minimum value is the half-gap between ``A`` (shifted by ``offset``)
    and ``B`` — positive when separate, negative when overlapping.

    Seeded from **both** a coarse grid over the region and the bodies' own
    closest-point pair (:func:`_contact_seeds`), then refined from the best
    few seeds by the ridge-aware descent (:func:`_descend`). The grid alone
    is blind to an overlap lens thinner than its spacing; the closest-point
    seeds are spaced by the *bodies*, not the region, so they see it
    (gr334763 — a −0.23 Å interference reported as +0.022 Å "clear").

    ``stop_at_contact`` returns the first value ≤ 0 found (seed or descent)
    without finishing the minimisation — for callers that only need the
    overlap *boolean* (the DOF probe's contact scan), not the true minimum;
    the returned value is then merely "some overlap depth". That path takes
    a single descent, since a boolean needs no polishing.
    """
    lo, hi = region
    offset = as_vec3(offset)

    def da(p: Vec3) -> float:
        return component_sdf(design, ea, p - offset)

    def db(p: Vec3) -> float:
        return component_sdf(design, eb, p)

    def g(p: Vec3) -> float:
        return max(da(p), db(p))

    scale = _governing_length(design, [ea, eb])
    if scale <= 0.0:
        scale = float(np.linalg.norm(hi - lo)) or 1.0
    step = _STEP_REL * float(np.linalg.norm(hi - lo)) if step is None else step
    eps = _GRAD_REL_EPS * scale

    box_a = _bounds(design, ea)
    if box_a is not None:
        box_a = (box_a[0] + offset, box_a[1] + offset)
    seeds = [vec3(*(0.5 * (lo + hi)))]
    # Structural seeds first: they are the ones that land inside a thin
    # lens, so a ``stop_at_contact`` probe usually answers before it ever
    # walks the grid.
    seeds.extend(_contact_seeds(da, db, box_a, _bounds(design, eb), eps))
    axes = [np.linspace(lo[i], hi[i], grid) for i in range(3)]
    seeds.extend(vec3(x, y, z) for x in axes[0] for y in axes[1] for z in axes[2])

    scored: list[tuple[float, Vec3]] = []
    for seed in seeds:
        p = as_vec3(np.clip(seed, lo, hi))
        v = g(p)
        if stop_at_contact and v <= 0.0:
            return v, p
        scored.append((v, p))

    scored.sort(key=lambda sv: sv[0])
    n_starts = 1 if stop_at_contact else max(1, starts)
    # Spread the starts: near-duplicate seeds share a basin, so descending
    # from all of them buys nothing. "Near" is a fraction of the governing
    # length, not a fixed distance.
    apart = 0.05 * scale

    def far_from_picked(p: Vec3, picked: list[Vec3]) -> bool:
        return all(float(np.linalg.norm(p - q)) > apart for q in picked)

    picked: list[Vec3] = []
    for _v, p in scored:
        if far_from_picked(p, picked):
            picked.append(p)
            if len(picked) >= n_starts:
                break
    if not picked:
        picked = [scored[0][1]]

    best_v, best_p = scored[0]
    for start in picked:
        v, p = _descend(
            da,
            db,
            lo,
            hi,
            start,
            iters=iters,
            step=step,
            scale=scale,
            stop_at_contact=stop_at_contact,
        )
        if v < best_v:
            best_v, best_p = v, p
        if stop_at_contact and best_v <= 0.0:
            break
    return best_v, as_vec3(best_p)


def clearance(design: Design, a: str, b: str) -> ClearanceResult:
    """Signed min surface gap between components ``a`` and ``b``."""
    ea, eb = design.components[a], design.components[b]
    region = _region(design, [ea, eb])
    half, p = _min_max_sdf(design, ea, eb, vec3(0, 0, 0), region)
    scale = _governing_length(design, [ea, eb])
    return ClearanceResult(
        gap=_round_relative(2.0 * half, scale),
        interfering=half < 0,
        point=p,
        resolution=CONTACT_TOL_REL * scale,
    )


# ── connectivity — the assembly contact graph ─────────────────────────────
#: Two components count as *connected* when their signed gap is ≤ this many mm
#: (touching or interfering). It absorbs the small residual of the coarse-grid
#: + gradient-descent minimiser so a true face-to-face contact reads as 0.
#:
#: Absolute, and therefore only meaningful for a design whose numbers are
#: O(1)–O(1000) — see :data:`CONTACT_TOL_REL` for the scale-relative band
#: that renderers and interference checks use instead.
CONTACT_TOL_MM = 1e-2

#: The "can't tell clear from touching" band, as a fraction of the governing
#: length (:func:`_governing_length` — the smaller body's diagonal). This is
#: the scale-relative form of :data:`CONTACT_TOL_MM`: it lands in the same
#: place for the O(10 mm) parts that constant was tuned on, and it keeps
#: meaning something for a design drawn in Å or in km. Surfaced per query as
#: :attr:`ClearanceResult.resolution`.
CONTACT_TOL_REL = 1e-3


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
                    while True:
                        # indexed lookup, not the loop var, so mypy can't
                        # narrow the `while` guard's None-check into the body.
                        parent = prev[chain[-1]]
                        if parent is None:
                            break
                        chain.append(parent)
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
    dirs: tuple[str, ...] | None = None,
) -> DofResult:
    """How far ``moving`` can translate along ±x/±y/±z before hitting
    ``fixed``. ``dirs`` restricts the probe to a subset of ``('+x', '-x',
    '+y', '-y', '+z', '-z')`` — each direction costs a full contact scan,
    so callers that read only one axis (the se DOF probe) should name it;
    ``None`` probes all six."""
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
        half, _ = _min_max_sdf(design, em, ef, offset, (lo, hi), stop_at_contact=True)
        return half <= 0.0

    travel: dict[str, float] = {}
    all_dirs = {
        "+x": vec3(1, 0, 0),
        "-x": vec3(-1, 0, 0),
        "+y": vec3(0, 1, 0),
        "-y": vec3(0, -1, 0),
        "+z": vec3(0, 0, 1),
        "-z": vec3(0, 0, -1),
    }
    if dirs is not None:
        if not dirs:
            raise ValueError(
                "dirs must name at least one probe direction "
                f"({', '.join(all_dirs)}) — pass None for all six"
            )
        unknown = set(dirs) - set(all_dirs)
        if unknown:
            raise ValueError(
                f"unknown probe direction(s) {sorted(unknown)} — "
                f"valid: {', '.join(all_dirs)}"
            )
        all_dirs = {name: all_dirs[name] for name in dirs}
    scan = 120  # coarse first-contact scan; AABB fast-reject keeps it cheap
    for name, d in all_dirs.items():
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
