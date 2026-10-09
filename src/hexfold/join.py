"""join: compose two resolved blocks over a matched port pair (SPEC §22.2
"Block joiner", `docs/backlog/hexfold-integration.md` ruling step 5).

Until this module, whole-spec composition (one ``.hx`` text, every
instance fused in one ``build``) was the only joiner.  This is the
block-level equivalent: two already-resolved :class:`Block` s (each its
own ``build`` + ``stick``, independently relaxed) plus a matched port pair
produce a :class:`Composite` — rigidly placed, seam-bonded, re-relaxed
only in a seam radius around the join (the rest of each block stays
bit-for-bit what it was), with the same ``seam.rings`` census a whole-spec
fuse of the same two parts would report.

Numpy + hexfold only (`tests/test_hexfold_import_boundary.py`); reuses
``build``'s placement/ranking primitives (``_fuse_transform``,
``_rank_fit``, ``_seam_faces``) directly rather than re-deriving them —
they take rim-walk/dangling ordinal tuples and a position array, not a
``Net``, so a two-block caller can feed them a stacked array instead.

The se side (`precis_se/atomic/join.py`, not built here) is a thin
store-aware wrapper: it rebuilds each block's ``Net`` from its persisted
generator spec, calls :func:`compose`, and mints the composite structure.

``compose_k3`` is a separate deterministic straight-Y entry over private
open SD segments, with explicit equal-120 type/dihedrals. Endpoint-aware
segments avoid cyclic Port guesses; no relaxer, compiler grammar or SE
mutation is involved. Its eight-cycle faces are actual local topology,
not a measured/stable carbon motif or a closed-tube rail implementation.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .catalogue import CatalogueStore

from .build import (
    _FIT_CAP,
    Net,
    Port,
    _fuse_transform,
    _k_cost,
    _rank_fit,
    _seam_faces,
)
from .lattice import ideal_angle_deg
from .report import Finding, Severity
from .stick import stick_relax_pinned

#: SPEC 11.3/22.2 "Seam decay, measured": shells of unperturbed geometry
#: beyond which a block's own atoms are safe to leave frozen during a
#: join's re-relax, by rim type.  Conservative (rounded up from the
#: measured decay lengths in `docs/backlog/diamondoid-pattern-language.md`
#: "Seam decay, measured": zigzag decays over 5-8 shells depending on tube
#: length, armchair over 1); a mixed rim (mismatched dangling pattern, no
#: ``rim_type``) has no measurement of its own and falls back to the
#: zigzag number with a `seam.radius.unmeasured` finding.
SEAM_RADIUS: dict[str, int] = {"z": 8, "a": 2}
_SEAM_RADIUS_DEFAULT = SEAM_RADIUS["z"]

#: seam.leak thresholds (bond-length / bond-angle change in the guard
#: band, shells r+1..r+2, vs. the block's own pre-join geometry), by the
#: *pinned* side's own rim type -- kept per-type like `SEAM_RADIUS`, not
#: one shared number, because the two rims decay at genuinely different
#: rates on the stick rung (see below), same as their radii differ.
#: Measured on `compose`'s own re-relax (`stick_relax_pinned`,
#: `_ITERS=3000`) at the table radius, both tubes onto an identical copy
#: of themselves at k=0: zigzag `tube(8,0,len=3)`, r=8, guard shells 9-10
#: (the only atoms this short a tube leaves beyond the radius): max |dl|
#: < 2e-15 A (float noise -- shells 9-10 are pinned end to end, no
#: movable neighbour to pull on them), max |dtheta| 0.0111 deg.  Armchair
#: `tube(5,5,len=3)`, r=2, guard shells 3-4: max |dl| < 2e-15 A, max
#: |dtheta| 1.4108 deg -- shell 3 sits directly against movable shell 2 on
#: a 60-atom, 4-shell-deep tube (no room for the perturbation to die down
#: before the guard band starts, unlike the zigzag case's 10 shells of
#: depth): a real boundary effect of freezing so short a tube, not a bug
#: (`zigzag tube(8,0,len=3) at seam_radius={"a": 1}` below shows the same
#: effect scaled up further -- 0.154 deg at guard shells 2-3 -- confirming
#: the guard band gets *tighter*, not looser, close to the join).  Each
#: entry is 2x that type's own measured maximum; `dl` is not 2x of a noise
#: floor (meaningless) but a fixed value far above both measured near-zero
#: maxima and still far below any bond genuinely stretching.  A mixed rim
#: (no `rim_type`) falls back to the tighter zigzag entry, same
#: conservative choice as `SEAM_RADIUS`'s fallback.  The geo rung (slice
#: 2) re-measures against its own thresholds (0.002 A / 0.15 deg,
#: `tests/test_hexfold_seam_decay.py`, measured with no pinning at all) --
#: not comparable to these: pinning a guard band, by construction,
#: reflects some strain back at its own boundary that a fully free relax
#: never sees.
_LEAK_THRESH: dict[str, tuple[float, float]] = {
    "z": (0.0001, 0.025),  # (dl_A, dtheta_deg)
    "a": (0.0001, 2.9),
}
_LEAK_THRESH_DEFAULT = _LEAK_THRESH["z"]

#: seam.sigma (gr456212, 2026-09-29 prod dogfood): the relative tolerance
#: below which two blocks' ``sigma`` count as "the same number" rather
#: than a genuine bond-length mismatch. `compose` places and relaxes
#: using only `a.sigma` (`place`'s `_fuse_transform` call, `_stick_relaxer`
#: below) -- a caller joining a sigma=1.75 A block to a sigma=1.42 A one
#: was accepted silently; strain roughly tripled and both sides raised
#: `seam.leak`, but nothing named the cause. 1e-6 relative is not a
#: measured physical floor (unlike `_LEAK_THRESH`) -- it is a "these are
#: not the same float" floor, far tighter than any two lattices' sigmas
#: that are meant to differ (the smallest gap in the catalogue's own
#: rim families is >1%) and far looser than ordinary float round-trip
#: noise (~1e-15), so it never fires on two copies of the same lattice.
_SIGMA_REL_TOL = 1e-6

#: seam.leak thresholds for the geo rung (slice 2), overriding
#: :data:`_LEAK_THRESH` via `compose`'s ``leak_thresholds`` keyword -- the
#: SAME numbers `tests/test_hexfold_seam_decay.py` measures and pins
#: (0.002 A / 0.15 deg), but measured with NO pinning at all (a free
#: relax of the whole tube), unlike the stick numbers above (measured
#: WITH a pinned guard band, which by construction reflects some strain
#: back at its own boundary). Uniform across rim type -- both catalogue
#: rim families share the one geo relaxer, unlike stick's per-type split
#: (`_LEAK_THRESH`'s own docstring explains that split; geo has no
#: equivalent measurement showing one rim type decays differently from
#: the other, so one number covers both, keyed the same way so the
#: lookup in `compose` needs no special-casing).
LEAK_THRESH_GEO: dict[str, tuple[float, float]] = {
    "z": (0.002, 0.15),
    "a": (0.002, 0.15),
}

#: `Relaxer(elements, coords, bonds, rings, pinned_mask) -> coords`: the
#: seam so precis can inject the geo rung (slice 2, `relax_graph`) without
#: this module importing `precis.structure`.  `pinned_mask` is a 0/1
#: float array, 1 meaning "never move" -- the same sense as
#: `precis.structure.georelax.relax_graph`'s `pinned` set, so the slice-2
#: adapter is a one-line `{i for i, p in enumerate(pinned_mask) if p}`.
#: Called with the seam sub-graph only (`_seam_subgraph`), not the whole
#: composite -- `elements`/`coords`/`pinned_mask` are `|S|` long and
#: `bonds`/`rings` are already remapped to local `0..|S|-1` indices.
Relaxer = Callable[
    [
        list[str],
        np.ndarray,
        list[tuple[int, int, int]],
        list[tuple[int, ...]],
        np.ndarray,
    ],
    np.ndarray,
]


@dataclass
class Block:
    """A resolved sp2 block: one instance's (or one composite's) final
    atoms/bonds/rings/ports, coordinates included.  Not a ``Net`` --
    ``Net`` carries a whole ``.hx`` spec's build machinery; a ``Block`` is
    just the atomic result plus the port table a join needs, so a
    composite (itself never built from a spec) can be one too."""

    elements: tuple[str, ...]
    coords: np.ndarray  # (N, 3) float64 Angstrom
    bonds: tuple[tuple[int, int, int], ...]  # (i, j, kind), follows Net.bonds
    rings: tuple[tuple[int, ...], ...]
    ports: dict[str, Port]
    sigma: float


@dataclass
class Composite:
    elements: tuple[str, ...]
    coords: np.ndarray
    bonds: tuple[tuple[int, int, int], ...]
    rings: tuple[tuple[int, ...], ...]
    ports: dict[str, Port]
    movable: np.ndarray  # (N,) bool, True = re-relaxed by this join
    findings: list[Finding]
    seam: dict[str, Any]
    transform: tuple[np.ndarray, np.ndarray]  # (R, t) placing b onto a


def block_from_net(net: Net, coords: np.ndarray) -> Block:
    """A :class:`Block` from a built (+ typically stuck) ``Net`` and its
    final coordinates -- the ordinary way to get a block for a first
    join; a composite from an earlier :func:`compose` is already one."""
    return Block(
        elements=tuple(a.element for a in net.atoms),
        coords=np.asarray(coords, dtype=np.float64),
        bonds=tuple(net.bonds),
        rings=net.rings,
        ports={name: p for name, p in net.ports},
        sigma=net.lattice.sigma_A,
    )


def rank_k(
    a: Block, pa: Port, b: Block, pb: Port
) -> list[tuple[int, float, float, int]]:
    """Every phase ``k`` in ``0..N-1``, ranked exactly as build's
    ``k=fit`` (:func:`hexfold.build._rank_fit` over the seam faces each
    phase would make), best first.  ``a``/``b`` complete the signature
    that :func:`place`/:func:`compose` share even though only the ports'
    rim walks/dangling atoms feed the ranking itself."""
    n = len(pa.dangling)
    candidates: list[tuple[int, float, float, int]] = []
    for kc in range(min(n, _FIT_CAP)):
        trial = [(pa.dangling[i], pb.dangling[(kc - i) % n]) for i in range(n)]
        mx, charge = _k_cost(_seam_faces(pa.atoms, pb.atoms, trial))
        candidates.append((kc, mx, charge, kc))
    return _rank_fit(candidates)


def place(
    a: Block, pa: Port, b: Block, pb: Port, k: int
) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) placing ``b``'s resolved coordinates onto ``a``'s port, via
    :func:`hexfold.build._fuse_transform`'s rim-frame convention:
    centroids sigma apart along the port normal, normals antiparallel,
    twist pairing ``pa.dangling[i]`` <-> ``pb.dangling[(k-i) % N]``.
    Reuses ``_fuse_transform`` directly (a stacked position array plus
    ``b``'s dangling ordinals offset into it) rather than a local
    equivalent -- its signature only needs ordinal tuples into one
    position array and each side's own instance centroid, both of which a
    two-block caller can build without a ``Net``."""
    n_a = len(a.elements)
    pos = np.vstack([a.coords, b.coords])
    q_dang = tuple(i + n_a for i in pb.dangling)
    c_a = a.coords.mean(axis=0)
    c_b = b.coords.mean(axis=0)
    r, t = _fuse_transform(pos, pa.dangling, q_dang, k, a.sigma, c_a, c_b)
    return r, t


def _adjacency(bonds: tuple[tuple[int, int, int], ...]) -> dict[int, list[int]]:
    adj: dict[int, list[int]] = {}
    for i, j, *_rest in bonds:
        adj.setdefault(i, []).append(j)
        adj.setdefault(j, []).append(i)
    return adj


def _shells_from(rim: set[int], adj: dict[int, list[int]]) -> dict[int, int]:
    """BFS graph distance from ``rim`` (distance 0) over ``adj``."""
    dist = dict.fromkeys(rim, 0)
    queue: deque[int] = deque(rim)
    while queue:
        u = queue.popleft()
        for v in adj.get(u, ()):
            if v not in dist:
                dist[v] = dist[u] + 1
                queue.append(v)
    return dist


def _seam_subgraph(
    movable: np.ndarray,
    dist_a: dict[int, int],
    dist_b: dict[int, int],
    n_a: int,
    r_a: int,
    r_b: int,
    seam_pairs: list[tuple[int, int]],
) -> list[int]:
    """``S`` (plan "Re-relax only the seam radii"): the composite ordinals
    the relaxer actually needs to see -- movable atoms (shells ``<= r`` on
    their own side), each side's pinned guard band (shells ``r+1..r+2``,
    an anchor for the movable atoms' bond/angle springs), and the new seam
    bonds' own endpoints.  The last are redundant in practice (a fused
    rim's dangling atoms sit at shell 0 on their own side, always
    ``<= r`` for any non-negative radius, so they are already movable) but
    unioned in explicitly per the plan, and as a guard against a future
    caller passing a negative override.  Sorted ascending for a
    deterministic remap."""
    s = {int(i) for i in np.nonzero(movable)[0]}
    s |= {o for o, d in dist_a.items() if r_a < d <= r_a + 2}
    s |= {n_a + o for o, d in dist_b.items() if r_b < d <= r_b + 2}
    for i, j in seam_pairs:
        s.add(i)
        s.add(j)
    return sorted(s)


def _angle_springs_from(rings: tuple[tuple[int, ...], ...], sigma: float) -> np.ndarray:
    """Ring-chord angle springs (sp2 ideal at every vertex -- a composed
    block's ring interiors are always sp2 in this slice; sp3 seam
    vertices are a later slice's scope, same as `hexfold.stick`'s own
    per-atom `hyb` lookup falling back to "" -- non-sp3 -- when unknown)."""
    out: list[tuple[int, int, float]] = []
    for ring in rings:
        n = len(ring)
        ideal = math.radians(ideal_angle_deg(n, ""))
        chord = 2.0 * sigma * math.sin(ideal / 2.0)
        for i in range(n):
            out.append((ring[(i - 1) % n], ring[(i + 1) % n], chord))
    return np.array(out, dtype=np.float64) if out else np.zeros((0, 3))


def _stick_relaxer(sigma: float) -> Relaxer:
    """The default :data:`Relaxer`: :func:`hexfold.stick.stick_relax_pinned`
    over whatever sub-graph it is called with (the seam sub-graph in
    ordinary use), sigma-uniform bond rest lengths (a composed block
    carries no termination/element-pair info of its own -- a future slice
    adding `terminate` support to `Block` would thread `sigma_CH` through
    here too; `compose` flags a termination atom inside the sub-graph with
    `seam.terminated` so this simplification is never silent)."""

    def relax(
        elements: list[str],
        coords: np.ndarray,
        bonds: list[tuple[int, int, int]],
        rings: list[tuple[int, ...]],
        pinned_mask: np.ndarray,
    ) -> np.ndarray:
        del elements  # sigma-uniform bonds: element identity unused here
        bonds_arr = np.array([(i, j) for i, j, *_ in bonds], dtype=np.int64)
        brest = np.full(len(bonds_arr), sigma, dtype=np.float64)
        springs = _angle_springs_from(tuple(rings), sigma)
        movable = 1.0 - np.asarray(pinned_mask, dtype=np.float64)
        pos, _max_force = stick_relax_pinned(
            np.asarray(coords, dtype=np.float64),
            bonds_arr,
            brest,
            springs,
            sigma,
            movable=movable,
        )
        return pos

    return relax


def _angles_at(coords: np.ndarray, v: int, nbrs: list[int]) -> list[float]:
    out = []
    for x in range(len(nbrs)):
        for y in range(x + 1, len(nbrs)):
            u = coords[nbrs[x]] - coords[v]
            w = coords[nbrs[y]] - coords[v]
            cosang = float(u @ w) / (
                float(np.linalg.norm(u)) * float(np.linalg.norm(w))
            )
            out.append(math.degrees(math.acos(max(-1.0, min(1.0, cosang)))))
    return out


def _leak_finding(
    side: str,
    block: Block,
    port: Port,
    dist: dict[int, int],
    r: int,
    coords_local: np.ndarray,
    thresh: tuple[float, float],
) -> Finding | None:
    """``seam.leak`` (SPEC 22.2): over shells ``r+1..r+2`` (the frozen
    guard band beyond the re-relaxed radius), the max bond-length and
    bond-angle change vs. ``block``'s own pre-join geometry.  ``None``
    when both are below the measured-and-doubled thresholds, or the block
    has no atoms out that far (too short to have a guard band -- nothing
    to leak into)."""
    lo, hi = r + 1, r + 2
    adj = _adjacency(block.bonds)
    shell_atoms = [o for o, d in dist.items() if lo <= d <= hi]
    if not shell_atoms:
        return None
    max_dl = 0.0
    for i, j, *_rest in block.bonds:
        s = min(dist.get(i, 1 << 30), dist.get(j, 1 << 30))
        if lo <= s <= hi:
            dl = abs(
                float(np.linalg.norm(coords_local[i] - coords_local[j]))
                - float(np.linalg.norm(block.coords[i] - block.coords[j]))
            )
            max_dl = max(max_dl, dl)
    max_dtheta = 0.0
    for v in shell_atoms:
        nbrs = adj.get(v, [])
        before = _angles_at(block.coords, v, nbrs)
        after = _angles_at(coords_local, v, nbrs)
        for x, y in zip(before, after, strict=True):
            max_dtheta = max(max_dtheta, abs(x - y))
    dl_thresh, dtheta_thresh = thresh
    if max_dl < dl_thresh and max_dtheta < dtheta_thresh:
        return None
    # name the measure that actually tripped and print its threshold
    # beside it: the other one is typically far below its own and formats
    # to a bare `0.0000`, which reads as "nothing happened" and made a
    # genuine 0.060-vs-0.025 deg angle breach look like a false alarm in
    # the 2026-09-29 prod dogfood.
    breached = [
        name
        for name, value, limit in (
            ("|dl|", max_dl, dl_thresh),
            ("|dtheta|", max_dtheta, dtheta_thresh),
        )
        if value >= limit
    ]
    return Finding(
        "seam.leak",
        Severity.WARN,
        f"{side}: re-relax leaked past shell {r} on {port.name}: "
        f"{' and '.join(breached)} over threshold "
        f"(max |dl| {max_dl:.4f} of {dl_thresh:.4f} A, "
        f"max |dtheta| {max_dtheta:.3f} of {dtheta_thresh:.3f} deg)",
        data=(
            ("block", side),
            ("port", port.name),
            ("type", list(port.rim_type) if port.rim_type else None),
            ("r", r),
            ("shell", [lo, hi]),
            ("max_dl", round(max_dl, 5)),
            ("max_dtheta", round(max_dtheta, 4)),
            ("thresh_dl", dl_thresh),
            ("thresh_dtheta", dtheta_thresh),
            ("breached", breached),
        ),
        fix="raise seam_radius or resolve a longer block",
    )


def _strain_finding(
    sigma: float,
    bonds: list[tuple[int, int, int]],
    rings: list[tuple[int, ...]],
    movable: np.ndarray,
    coords: np.ndarray,
) -> Finding:
    """``seam.strain`` INFO: rms/max bond-length deviation from sigma over
    every bond with at least one movable (re-relaxed) endpoint, plus
    rms/max bond-angle deviation from the ring-ideal angle (the same
    ring-chord convention `_angle_springs_from`/`stick` use, not a generic
    all-neighbour-pairs angle like `seam.leak`'s -- simpler, and it is
    exactly the quantity the spring relax is driving toward zero) at every
    movable ring vertex."""
    dl_devs = [
        float(np.linalg.norm(coords[i] - coords[j])) - sigma
        for i, j, *_ in bonds
        if movable[i] or movable[j]
    ]
    dtheta_devs: list[float] = []
    for ring in rings:
        n = len(ring)
        ideal = ideal_angle_deg(n, "")
        for i in range(n):
            v = ring[i]
            if not movable[v]:
                continue
            u = coords[ring[(i - 1) % n]] - coords[v]
            w = coords[ring[(i + 1) % n]] - coords[v]
            cosang = float(u @ w) / (
                float(np.linalg.norm(u)) * float(np.linalg.norm(w))
            )
            ang = math.degrees(math.acos(max(-1.0, min(1.0, cosang))))
            dtheta_devs.append(ang - ideal)
    dl_arr = np.array(dl_devs) if dl_devs else np.zeros(1)
    dtheta_arr = np.array(dtheta_devs) if dtheta_devs else np.zeros(1)
    rms_dl = float(np.sqrt((dl_arr**2).mean()))
    max_dl = float(np.abs(dl_arr).max())
    rms_dtheta = float(np.sqrt((dtheta_arr**2).mean()))
    max_dtheta = float(np.abs(dtheta_arr).max())
    return Finding(
        "seam.strain",
        Severity.INFO,
        f"seam strain rms|dl| {rms_dl:.4f} A max {max_dl:.4f} A over "
        f"{len(dl_devs)} bond(s); rms|dtheta| {rms_dtheta:.3f} deg max "
        f"{max_dtheta:.3f} deg over {len(dtheta_devs)} vertex-ring(s)",
        data=(
            ("rms_dl", round(rms_dl, 4)),
            ("max_dl", round(max_dl, 4)),
            ("rms_dtheta", round(rms_dtheta, 4)),
            ("max_dtheta", round(max_dtheta, 4)),
            ("bonds", len(dl_devs)),
            ("vertices", len(dtheta_devs)),
        ),
    )


def _empty_composite(findings: list[Finding]) -> Composite:
    return Composite(
        elements=(),
        coords=np.zeros((0, 3)),
        bonds=(),
        rings=(),
        ports={},
        movable=np.zeros(0, dtype=bool),
        findings=findings,
        seam={},
        transform=(np.eye(3), np.zeros(3)),
    )


def _thresh_for(
    rim_type: tuple[str, int] | None, table: dict[str, tuple[float, float]]
) -> tuple[float, float]:
    """The ``(dl_A, dtheta_deg)`` :func:`_leak_finding` threshold for one
    side's rim type against ``table`` (:data:`_LEAK_THRESH` by default, or
    a caller override e.g. :data:`LEAK_THRESH_GEO`) -- a mixed/unknown rim
    falls back to ``table``'s own ``"z"`` entry (the tighter of the two in
    every table defined so far), same fallback :func:`compose` already used
    inline before this was factored out for the override to reuse."""
    key = rim_type[0] if rim_type is not None else "z"
    return table.get(key, table.get("z", _LEAK_THRESH_DEFAULT))


def compose(
    a: Block,
    pa: Port,
    b: Block,
    pb: Port,
    k: int,
    *,
    seam_radius: dict[str, int] | None = None,
    leak_thresholds: dict[str, tuple[float, float]] | None = None,
    relax: Relaxer | None = None,
    prefix_a: str = "a",
    prefix_b: str = "b",
    rung: str = "stick",
    catalogue: CatalogueStore | None = None,
    relaxer: str | None = None,
) -> Composite:
    """Rigidly place ``b`` onto ``a`` at ``pa``/``pb`` phase ``k``, bond
    the two rims exactly as a whole-spec ``fuse`` would, and re-relax only
    the seam radius around the join (SPEC 22.2, plan "Re-relax only the
    seam radii"): the relaxer sees only ``S`` (:func:`_seam_subgraph`) --
    movable atoms plus each side's pinned guard band -- not the whole
    composite, so chained joins stay local cost instead of going quadratic
    in the total atom count.  A ring that straddles ``S``'s boundary (some
    vertices in, some out) is dropped from the sub-relax entirely rather
    than clipped -- in practice this never affects a movable atom's own
    springs (a ring's vertices are always within one shell of each other,
    and the guard band is two shells deep beyond the movable radius, so
    any ring touching a movable atom is fully inside ``S``); it only
    drops rings anchored entirely in the far, untouched interior, which
    contribute nothing to a sub-relax that never moves them anyway.
    ``prefix_a``/``prefix_b`` name the composite ports (``<prefix>_<port>``
    default ``"a"``/``"b"``, unchanged) -- the se side passes the real
    block names so a chain of joins doesn't collide. ``leak_thresholds``
    overrides :data:`_LEAK_THRESH` (the se side passes
    :data:`LEAK_THRESH_GEO` when ``relax`` is the geo rung, ``None`` for
    the stick default) -- the two are measured on genuinely different
    relax physics (see :data:`LEAK_THRESH_GEO`'s own docstring), so a geo
    relax checked against stick's tighter numbers would spuriously fire
    ``seam.leak`` on ordinary geo-rung noise.

    ``rung``/``catalogue``/``relaxer`` (SPEC §26, ``hexfold.catalogue``,
    slice 6) let a caller resolve the radius and threshold from a measured
    ``EdgeMotif`` instead of the module tables: per side, an explicit
    ``seam_radius``/``leak_thresholds`` entry still wins outright; failing
    that, ``catalogue`` (when given) is queried via
    ``hexfold.catalogue.resolve_edge``; failing that, the ``rung``-
    appropriate table (:data:`_LEAK_THRESH` for ``"stick"``,
    :data:`LEAK_THRESH_GEO` for ``"geo"``) or the mixed-rim default, same
    as before ``catalogue`` existed.  ``compose`` never writes to
    ``catalogue`` -- read-only by design (a warm-up fill is a separate,
    explicit call to ``hexfold.catalogue.measure_environment``).
    ``composite.seam`` gains ``"radius_source"`` (per side: ``"explicit"``,
    the ``resolve_edge`` label, ``"table"``, or ``"default"``) and
    ``"rung"``.  With no ``catalogue`` and ``rung="stick"`` (the defaults),
    every number is unchanged from before this parameter existed.

    ``composite.seam["catalogue"]`` (per side) is the resolution record
    the se ``view='catalogue'`` reads back: ``consulted`` (a typed rim
    with a catalogue to ask), ``label`` (``resolve_edge``'s own label --
    ``"pinned z"``, ``"exact z10"``, ``"nearest z12"``, ``"none"`` -- or
    ``None`` when not consulted), ``sigma`` (the key the lookup used),
    ``row`` (``key_hash``/``source``/``seam_radius``/``leak_thresh`` of
    the row found, or ``None``), and ``radius_source``/``thresh_source``
    (what actually governed each number: ``"explicit"``, the label,
    ``"table"`` or ``"default"``) with ``thresh`` the leak threshold in
    force.  It is recorded here, at the one place
    the label exists, rather than re-derived later against a catalogue
    that may have changed since the join."""
    n = len(pa.dangling)
    if len(pb.dangling) != n:
        return _empty_composite(
            [
                Finding(
                    "port.mismatch",
                    Severity.ERROR,
                    f"port sizes {len(pa.dangling)} != {len(pb.dangling)}",
                    data=(("a_n", len(pa.dangling)), ("b_n", len(pb.dangling))),
                    fix="match port sizes",
                )
            ]
        )

    findings: list[Finding] = []
    if not math.isclose(a.sigma, b.sigma, rel_tol=_SIGMA_REL_TOL):
        findings.append(
            Finding(
                "seam.sigma",
                Severity.WARN,
                f"bond-length mismatch: a.sigma={a.sigma:.4f} A, "
                f"b.sigma={b.sigma:.4f} A -- the seam uses a's sigma "
                f"({a.sigma:.4f} A) for both the fuse placement and the "
                "re-relax; b's own bonds were built to a different rest "
                "length",
                data=(("a_sigma", a.sigma), ("b_sigma", b.sigma)),
                fix="regenerate one side so both blocks share one sigma before joining",
            )
        )
    # seam.element (gr456212's second half, 2026-09-29 prod dogfood): the
    # sigma check above landed and the element one never did. The seam
    # bonds `pa.dangling` to `pb.dangling` pairwise by geometry and port
    # size alone -- nothing anywhere in the join path compares the two
    # rims' elements, in contrast to bind/generate/propose, which all
    # check `port.expected_element != atom.element`. So a rim of one
    # element fuses to a rim of another in silence.
    #
    # WARN, not ERROR, and deliberately so: `JOINERS` is keyed on a
    # lattice *pair* precisely so a future heterojunction entry can tell
    # its two sides apart (`_hexfold_join`'s own docstring), so refusing
    # here would pre-empt a direction the design already anticipates, and
    # the placement is geometrically valid either way. Promoting this to
    # ERROR is a product call, not a bug fix. Severity matches
    # `seam.sigma`, whose WARN is itself pinned by a test as intentional.
    a_els = tuple(sorted({a.elements[o] for o in pa.dangling}))
    b_els = tuple(sorted({b.elements[o] for o in pb.dangling}))
    if a_els != b_els:
        findings.append(
            Finding(
                "seam.element",
                Severity.WARN,
                f"element mismatch at the seam: a's {pa.name!r} rim is "
                f"{'/'.join(a_els)}, b's {pb.name!r} rim is "
                f"{'/'.join(b_els)} -- the seam bonds rim atoms pairwise "
                "by geometry and port size, never by element, so this is "
                "placed and relaxed as if both sides were one material",
                data=(("a_elements", list(a_els)), ("b_elements", list(b_els))),
                fix=(
                    "regenerate one side so both rims share an element, or "
                    "accept the heterojunction knowing the seam was not "
                    "parameterised for it"
                ),
            )
        )
    ta, tb = pa.rim_type, pb.rim_type
    motif = "fuse"
    if ta is not None and tb is not None and ta[0] != tb[0]:
        motif = "adapter"
        findings.append(
            Finding(
                "seam.adapter",
                Severity.INFO,
                f"grain-boundary adapter {ta[0]}{ta[1]} <-> {tb[0]}{tb[1]}",
                data=(("a_type", list(ta)), ("b_type", list(tb))),
            )
        )

    r, t = place(a, pa, b, pb, k)
    n_a = len(a.elements)
    coords_b = (r @ b.coords.T).T + t
    coords = np.vstack([a.coords, coords_b])
    elements = a.elements + b.elements

    seam_pairs = [(pa.dangling[i], n_a + pb.dangling[(k - i) % n]) for i in range(n)]
    bonds: list[tuple[int, int, int]] = (
        list(a.bonds)
        + [(i + n_a, j + n_a, kd) for i, j, kd in b.bonds]
        + [(min(x, y), max(x, y), 1) for x, y in seam_pairs]
    )

    q_atoms = tuple(x + n_a for x in pb.atoms)
    seam_faces = _seam_faces(pa.atoms, q_atoms, seam_pairs)
    rings: list[tuple[int, ...]] = (
        list(a.rings) + [tuple(x + n_a for x in ring) for ring in b.rings] + seam_faces
    )

    census: dict[int, int] = {}
    for f in seam_faces:
        census[len(f)] = census.get(len(f), 0) + 1
    findings.append(
        Finding(
            "seam.rings",
            Severity.INFO,
            f"seam rings {dict(sorted(census.items()))}",
            data=(("k", 2), ("rings", dict(sorted(census.items())))),
        )
    )

    def resolve_side(
        side: str, rim_type: tuple[str, int] | None, block_sigma: float
    ) -> tuple[int, tuple[float, float], str, dict[str, Any]]:
        row = None
        row_label: str = ""
        consulted = catalogue is not None and rim_type is not None
        # a mixed rim (rim_type is None) never consults the catalogue --
        # no row is ever measured for one (module docstring), and it must
        # still fall through to the seam.radius.unmeasured finding below,
        # not silently absorb the wildcard row `catalogue.seed_rows` puts
        # at rim_type=None for other lookups' sake.
        if catalogue is not None and rim_type is not None:
            from .catalogue import resolve_edge

            row, row_label = resolve_edge(
                catalogue, rim_type[0], rim_type[1], rung, relaxer, sigma=block_sigma
            )
        if seam_radius and side in seam_radius:
            r = seam_radius[side]
            label = "explicit"
        elif row is not None:
            r = row.seam_radius
            label = row_label
            if rim_type is not None:
                table_val = SEAM_RADIUS.get(rim_type[0], _SEAM_RADIUS_DEFAULT)
                if r < table_val:
                    findings.append(
                        Finding(
                            "seam.radius.narrowed",
                            Severity.INFO,
                            f"{side}: catalogue row {label!r} narrows the seam "
                            f"radius from the pinned {table_val} to {r}",
                            data=(
                                ("side", side),
                                ("measured", r),
                                ("pinned", table_val),
                                ("source", label),
                            ),
                        )
                    )
        elif rim_type is None:
            findings.append(
                Finding(
                    "seam.radius.unmeasured",
                    Severity.INFO,
                    f"{side}: mixed rim, using the unmeasured default radius "
                    f"{_SEAM_RADIUS_DEFAULT}",
                    data=(("side", side), ("radius", _SEAM_RADIUS_DEFAULT)),
                )
            )
            r = _SEAM_RADIUS_DEFAULT
            label = "default"
        else:
            r = SEAM_RADIUS.get(rim_type[0], _SEAM_RADIUS_DEFAULT)
            label = "table"
        if leak_thresholds is not None:
            th = _thresh_for(rim_type, leak_thresholds)
            thresh_source = "explicit"
        elif row is not None:
            th = row.leak_thresh
            thresh_source = row_label
        else:
            th = _thresh_for(
                rim_type, LEAK_THRESH_GEO if rung == "geo" else _LEAK_THRESH
            )
            thresh_source = "table"
        record: dict[str, Any] = {
            "consulted": consulted,
            "label": row_label if consulted else None,
            "sigma": f"{block_sigma:g}",
            "row": None
            if row is None
            else {
                "key_hash": row.key.hash(),
                "source": row.source,
                "seam_radius": row.seam_radius,
                "leak_thresh": list(row.leak_thresh),
            },
            "radius_source": label,
            "thresh": list(th),
            "thresh_source": thresh_source,
        }
        return r, th, label, record

    r_a, thresh_a, radius_source_a, cat_a = resolve_side("a", ta, a.sigma)
    r_b, thresh_b, radius_source_b, cat_b = resolve_side("b", tb, b.sigma)

    adj_a = _adjacency(a.bonds)
    adj_b = _adjacency(b.bonds)
    dist_a = _shells_from(set(pa.atoms), adj_a)
    dist_b = _shells_from(set(pb.atoms), adj_b)

    movable = np.zeros(n_a + len(b.elements), dtype=bool)
    for o, d in dist_a.items():
        if d <= r_a:
            movable[o] = True
    for o, d in dist_b.items():
        if d <= r_b:
            movable[n_a + o] = True

    seam_set = _seam_subgraph(movable, dist_a, dist_b, n_a, r_a, r_b, seam_pairs)
    idx_map = {g: i for i, g in enumerate(seam_set)}
    sub_elements = [elements[g] for g in seam_set]
    sub_coords = coords[seam_set]
    sub_bonds = [
        (idx_map[i], idx_map[j], kd)
        for i, j, kd in bonds
        if i in idx_map and j in idx_map
    ]
    sub_rings = [
        tuple(idx_map[x] for x in ring)
        for ring in rings
        if all(x in idx_map for x in ring)
    ]
    sub_pinned = np.array(
        [0.0 if movable[g] else 1.0 for g in seam_set], dtype=np.float64
    )

    non_c = sorted({el for el in sub_elements if el != "C"})
    if non_c:
        findings.append(
            Finding(
                "seam.terminated",
                Severity.INFO,
                f"seam sub-graph includes termination atom(s) {non_c}; "
                "re-relax still uses a uniform sigma bond rest length "
                "(no sigma_CH split yet)",
                data=(("elements", non_c),),
            )
        )

    relax_fn = relax if relax is not None else _stick_relaxer(a.sigma)
    sub_new = np.asarray(
        relax_fn(sub_elements, sub_coords, sub_bonds, sub_rings, sub_pinned),
        dtype=np.float64,
    )
    new_coords = coords.copy()
    new_coords[np.array(seam_set, dtype=np.int64)] = sub_new

    leak_a = _leak_finding("a", a, pa, dist_a, r_a, new_coords[:n_a], thresh_a)
    if leak_a is not None:
        findings.append(leak_a)
    local_b = (new_coords[n_a:] - t) @ r  # inverse rigid transform
    leak_b = _leak_finding("b", b, pb, dist_b, r_b, local_b, thresh_b)
    if leak_b is not None:
        findings.append(leak_b)

    findings.append(_strain_finding(a.sigma, bonds, rings, movable, new_coords))

    ports: dict[str, Port] = {}
    for name, p in a.ports.items():
        if name == pa.name:
            continue
        ports[f"{prefix_a}_{name}"] = replace(p, name=f"{prefix_a}_{name}")
    for name, p in b.ports.items():
        if name == pb.name:
            continue
        ports[f"{prefix_b}_{name}"] = replace(
            p,
            name=f"{prefix_b}_{name}",
            atoms=tuple(o + n_a for o in p.atoms),
            dangling=tuple(o + n_a for o in p.dangling),
        )

    seam = {
        "motif": motif,
        "k": k,
        "N": n,
        "types": {
            "a": list(ta) if ta is not None else None,
            "b": list(tb) if tb is not None else None,
        },
        "rings": dict(sorted(census.items())),
        "radius": {"a": r_a, "b": r_b},
        "radius_source": {"a": radius_source_a, "b": radius_source_b},
        "catalogue": {"a": cat_a, "b": cat_b},
        "rung": rung,
    }

    return Composite(
        elements=elements,
        coords=new_coords,
        bonds=tuple(bonds),
        rings=tuple(rings),
        ports=ports,
        movable=movable,
        findings=findings,
        seam=seam,
        transform=(r, t),
    )


@dataclass(frozen=True)
class _ZigzagSegment:
    """Private straight open rim: S-D-...-D-S, endpoints are first/last S.

    D sites have two sheet bonds; internal S sites have three. This is
    neither a cyclic Port nor a new authored port/rail grammar.
    """

    name: str
    atoms: tuple[int, ...]
    dangling: tuple[int, ...]
    endpoints: tuple[int, int]


@dataclass
class _K3Composite:
    """Local Y result, not a port-bearing/SE composite or relaxed structure."""

    elements: tuple[str, ...]
    coords: np.ndarray
    bonds: tuple[tuple[int, int, int], ...]
    rings: tuple[tuple[int, ...], ...]
    hybridisation: tuple[str, ...]
    seam_atoms: tuple[int, ...]
    transforms: tuple[tuple[np.ndarray, np.ndarray], ...]
    findings: list[Finding]


def _k3_refusal(code: str, message: str, **requested: Any) -> _K3Composite:
    return _K3Composite(
        (),
        np.empty((0, 3)),
        (),
        (),
        (),
        (),
        (),
        [
            Finding(
                code,
                Severity.ERROR,
                message,
                data=tuple(requested.items()),
                fix="use three carbon zigzag segments, equal 120 degree dihedrals, phase=0",
            )
        ],
    )


def _segment_frame(block: Block, rim: _ZigzagSegment) -> np.ndarray | None:
    """Validate the SD walk and planar outward half-sheet before placement."""
    n = len(rim.dangling)
    walk = rim.atoms
    if (
        n < 2
        or len(walk) != 2 * n + 1
        or len(set(walk)) != len(walk)
        or walk[1::2] != rim.dangling
        or rim.endpoints != (walk[0], walk[-1])
        or any(type(i) is not int or not 0 <= i < len(block.elements) for i in walk)
    ):
        return None
    pos = np.asarray(block.coords)
    if pos.shape != (len(block.elements), 3) or not np.isfinite(pos).all():
        return None
    if any(e != "C" for e in block.elements):
        return None
    if any(
        type(i) is not int
        or type(j) is not int
        or i == j
        or not (0 <= i < len(pos) and 0 <= j < len(pos))
        for i, j, _ in block.bonds
    ):
        return None
    adj = _adjacency(block.bonds)
    # Validate EVERY copied face before offsets can disguise a local
    # out-of-range/negative index as a valid atom in a different block.
    for ring in block.rings:
        if (
            len(ring) < 3
            or any(type(i) is not int or not 0 <= i < len(pos) for i in ring)
            or len(set(ring)) != len(ring)
            or any(b not in adj.get(a, ()) for a, b in zip(ring, ring[1:] + ring[:1]))
        ):
            return None
    if any(b not in adj.get(a, ()) for a, b in pairwise(walk)):
        return None
    if any(len(adj.get(d, ())) != 2 for d in rim.dangling):
        return None
    if any(len(adj.get(s, ())) != 3 for s in walk[2:-1:2]):
        return None
    dpos = pos[list(rim.dangling)]
    delta = np.diff(dpos, axis=0)
    period = math.sqrt(3) * block.sigma
    if not np.allclose(np.linalg.norm(delta, axis=1), period):
        return None
    tangent = delta[0] / np.linalg.norm(delta[0])
    if not np.allclose(delta, period * tangent):
        return None
    outward = pos.mean(axis=0) - dpos[0]
    outward -= outward.dot(tangent) * tangent
    length = np.linalg.norm(outward)
    if length == 0:
        return None
    outward /= length
    normal = np.cross(tangent, outward)
    local = (pos - dpos[0]) @ np.column_stack((tangent, outward, normal))
    if not np.allclose(local[:, 2], 0) or np.any(local[:, 1] < -1e-8):
        return None
    # Bondable D atoms are collinear; intervening S sites sit half a
    # sigma into the sheet and halfway along the zigzag period.
    expected = np.column_stack(
        (
            np.arange(-0.5, n, 0.5) * period,
            np.tile([0.5 * block.sigma, 0], n + 1)[: 2 * n + 1],
            np.zeros(2 * n + 1),
        )
    )
    if not np.allclose(local[list(walk)], expected):
        return None
    return np.column_stack((tangent, outward, normal))


def _place_k3(
    blocks: tuple[Block, ...],
    rims: tuple[_ZigzagSegment, ...],
    frames: list[np.ndarray],
) -> _K3Composite:
    """Rigidly place the three validated half-sheets; mint only seam atoms."""
    sigma = blocks[0].sigma
    n = len(rims[0].dangling)
    coords: list[np.ndarray] = []
    transforms: list[tuple[np.ndarray, np.ndarray]] = []
    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    walks: list[tuple[int, ...]] = []
    dangling: list[tuple[int, ...]] = []
    elements: tuple[str, ...] = ()
    offset = 0
    for j, (block, rim, frame) in enumerate(zip(blocks, rims, frames)):
        angle = 2 * math.pi * j / 3
        radial = np.array([0.0, math.cos(angle), math.sin(angle)])
        target = np.column_stack(
            (np.array([1.0, 0.0, 0.0]), radial, np.cross([1.0, 0.0, 0.0], radial))
        )
        rotation = target @ frame.T
        shift = sigma * radial - rotation @ block.coords[rim.dangling[0]]
        coords.append(block.coords @ rotation.T + shift)
        transforms.append((rotation, shift))
        bonds.extend((a + offset, b + offset, kind) for a, b, kind in block.bonds)
        rings.extend(tuple(i + offset for i in ring) for ring in block.rings)
        walks.append(tuple(i + offset for i in rim.atoms))
        dangling.append(tuple(i + offset for i in rim.dangling))
        elements += block.elements
        offset += len(block.elements)
    seam = tuple(range(offset, offset + n))
    spos = np.zeros((n, 3))
    spos[:, 0] = np.arange(n) * math.sqrt(3) * sigma
    coords.append(spos)
    for i, atom in enumerate(seam):
        bonds.extend((dangling[j][i], atom, 1) for j in range(3))
    # Each adjacent pair of seam sites bounds an eight-cycle between
    # each sheet pair. No cyclic endpoint wraparound; actual graph faces.
    for a, b in ((0, 1), (1, 2), (2, 0)):
        for i in range(n - 1):
            rings.append(
                (
                    seam[i],
                    dangling[a][i],
                    walks[a][2 * i + 2],
                    dangling[a][i + 1],
                    seam[i + 1],
                    dangling[b][i + 1],
                    walks[b][2 * i + 2],
                    dangling[b][i],
                )
            )
    return _K3Composite(
        elements + ("C",) * n,
        np.vstack(coords),
        tuple(bonds),
        tuple(rings),
        ("sp2",) * (offset + n),
        seam,
        tuple(transforms),
        [],
    )


def compose_k3(
    blocks: tuple[Block, ...],
    rims: tuple[_ZigzagSegment, ...],
    *,
    seam_type: str,
    dihedrals_deg: tuple[float, ...],
    phase: int = 0,
) -> _K3Composite:
    """Selected k3-sp2-120-z join: private segments, deterministic, no relax.

    Refuse unsupported declarations before examining coordinates/placement.
    All blocks must be finite planar carbon half-sheets at the same sigma;
    SD walks must have equal counts, spacing and defined open endpoints.
    No public cyclic Port, parser, compiler or SE mutation is changed.
    """
    if (
        len(blocks) != 3
        or len(rims) != 3
        or seam_type != "k3-sp2-120-z"
        or len(dihedrals_deg) != 3
        or any(
            isinstance(a, bool) or not isinstance(a, int | float) or a != 120
            for a in dihedrals_deg
        )
        or type(phase) is not int
        or phase != 0
    ):
        return _k3_refusal(
            "fit.unsolvable",
            "unsupported authored k3 seam declaration",
            multiplicity=len(rims),
            seam_type=seam_type,
            dihedrals_deg=dihedrals_deg,
            phase=phase,
        )
    sigma = blocks[0].sigma
    if (
        not math.isfinite(sigma)
        or sigma <= 0
        or any(not math.isclose(b.sigma, sigma, rel_tol=1e-12) for b in blocks)
    ):
        return _k3_refusal(
            "fit.unsolvable", "segments require one finite positive sigma"
        )
    counts = tuple(len(r.dangling) for r in rims)
    if len(set(counts)) != 1:
        return _k3_refusal(
            "port.mismatch", "zigzag segment dangling counts differ", counts=counts
        )
    frames: list[np.ndarray] = []
    for block, rim in zip(blocks, rims):
        frame = _segment_frame(block, rim)
        if frame is None:
            return _k3_refusal(
                "port.mismatch",
                "invalid straight carbon SD segment/frame",
                rim=rim.name,
                endpoints=rim.endpoints,
            )
        frames.append(frame)
    return _place_k3(blocks, rims, frames)
