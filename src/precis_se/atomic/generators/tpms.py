"""The ``tpms``/``schwarzite`` generator (docs/backlog/precis-surface-kernel.md
"Slice 1 -- the dual route"): triangulated-minimal-surface periodic carbon
scaffolds -- Schwarz P, Schwarz D, or the gyroid -- via
:mod:`precis_surface`'s level-set/marching-cubes/dual pipeline, never a
direction field or MIQ solve.

**Pipeline**: :func:`~precis_surface.level_set`'s nodal form ->
:func:`~precis_surface.periodic_mesh.periodic_mesh` -> (optional
``remesh``, see below) -> :func:`~precis_surface.dual.dualise` ->
:func:`~precis_surface.dual.unroll` -> scale to Å -> :class:`GeneratedBlock`.
Bond order is the same Pauling 4/3 ``"aromatic"`` estimate
:mod:`precis_se.atomic.generators.sp2`'s CNT/cone families use (a fully
delocalized sp² sheet, three equivalent bonds per interior atom summing to
carbon's max valence of 4) -- this net is exactly that kind of sheet,
merely curved into a triply-periodic minimal surface instead of rolled
into a cylinder. Provenance cites the dual-route construction itself: it
is not this codebase's invention (Terrones & Terrones, *New J. Phys.* 5
(2003) 126 (``pa449642``); the leapfrog/schwarzite literature surveyed in
docs/backlog/precis-surface-kernel.md's "Prior art" section) -- see that
section before assuming novelty anywhere near this module.

**One length-unit crossing, at the ``a`` argument.**
:mod:`precis_surface.level_set` and
:mod:`precis_surface.periodic_mesh.periodic_mesh` are unit-agnostic: every
length they take (the cell edge ``a``) and therefore every length they
produce (mesh vertex coordinates) is in whatever unit the caller's ``a``
was in (:mod:`precis_surface`'s house rule -- "the numbers never know").
Passing ``a=cell_A`` (Å) directly means the mesh, the dual net, and hence
every atom this generator emits are *already* in Å with no further
rescaling step -- unlike a generator that meshes in dimensionless
unit-cell coordinates and multiplies by ``cell_A`` afterward, which would
be an extra no-op bookkeeping step doing the same conversion a second way.

**Ruling (Reto, 2026-09-26): rings are ``{5, 6, 7}`` only** -- pentagons
are legitimate sp2 carbon (every fullerene is pentagons and hexagons), but
4-rings and below and 8-rings and above are not. The raw marching-cubes
dual does not respect this on its own (measured Schwarz P at ``n=17``:
``{4: 158, 5: 114, 6: 532, 7: 108, 8: 158, 9: 10}``, 49 % hexagons), so the
registry-driven path here runs :func:`precis_surface.remesh.remesh`
(docs/backlog/precis-surface-kernel.md's full slice 1 pipeline:
collapse/split/flip-for-degree/tangential-smooth/reproject) **by default**,
right before :func:`~precis_surface.dual.dualise`, and then REFUSES to
emit (:class:`GeneratorError`) if any ring still falls outside ``{5, 6,
7}`` -- see "Ring purity is enforced, not merely reported" below. Schwarz P
comes out clean this way: after remesh, ``{5: 114, 6: 700, 7: 138}``, 73.5%
hexagons, zero rings outside ``{5, 6, 7}``. What remains true even on this
default path: the geometry is still a topology-first scaffold, not a
force-field-relaxed structure -- bond lengths carry real spread rather than
one relaxed value, and vertex positions are Newton-reprojected onto the
level set, not energy-minimized. The topology this generator reports
(``chi_per_cell``, the ring histogram) is exact and mesh-derived either way.

**Ring purity is enforced, not merely reported.** After
:func:`~precis_surface.dual.dualise`, this module computes the dual net's
ring histogram and raises :class:`GeneratorError` if any key falls outside
``{5, 6, 7}`` -- but only when a remesh actually ran (the default, or an
injected ``remesh=`` callable). This currently means Schwarz P generates
cleanly (measured; Schwarz D not yet measured) while **gyroid refuses**:
its frozen wrap seam leaves a genuine fixed point of 20 (of 1365) welded
vertices no admissible collapse/split/flip can reach
(docs/backlog/precis-surface-kernel.md; :mod:`precis_surface.remesh`'s
module docstring) -- a clear refusal, not a silent 4-ring emission, until
that seam-freeze residual is fixed.

**The ``remesh`` hook and its off switch.** ``build_tpms`` takes an
optional keyword-only ``remesh: RemeshFn | None`` (a ``PeriodicMesh ->
PeriodicMesh`` callable) for injection/testing, applied instead of the
default when given. The registry-driven ``params`` dict also takes a
``"remesh"`` boolean (default ``True``); a caller can pass ``remesh=False``
in ``params`` to skip remeshing entirely and get the **raw scaffold**
back -- unvalidated against the ``{5, 6, 7}`` ruling, on purpose, since
that path exists to inspect what the raw marching-cubes dual produced (the
measured histograms quoted above came from exactly this escape hatch).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis_se.atomic.generators._types import (
    GeneratedBlock,
    GeneratedPort,
    GeneratorError,
    fmt_length_A,
)
from precis_se.atomic.generators.sp2 import VDW_MARGIN_A
from precis_surface.dual import DualNet, dualise, unroll
from precis_surface.level_set import (
    gyroid,
    gyroid_grad,
    schwarz_d,
    schwarz_d_grad,
    schwarz_p,
    schwarz_p_grad,
)
from precis_surface.periodic_mesh import PeriodicMesh, periodic_mesh, welded_euler
from precis_surface.remesh import remesh as _default_remesh

#: ``PeriodicMesh -> PeriodicMesh`` -- the remesh-loop hook (module
#: docstring's "The remesh hook" section).
RemeshFn = Callable[[PeriodicMesh], PeriodicMesh]

#: family letter -> (value function, analytic gradient), both
#: ``(pts, a) -> ...`` per :mod:`precis_surface.level_set`'s contract.
_FAMILIES: dict[str, tuple[Any, Any]] = {
    "P": (schwarz_p, schwarz_p_grad),
    "D": (schwarz_d, schwarz_d_grad),
    "G": (gyroid, gyroid_grad),
}

#: Default marching-cubes grid (per axis) -- odd, per
#: docs/backlog/precis-surface-kernel.md's measured sliver-fraction table
#: (odd n: 0-5.6% near-degenerate triangles; even n: 18-49%).
_DEFAULT_N = 17

#: Round-1 sanity cap on requested replica count per axis -- generator
#: geometry-size sanity, mirroring the sp2 family's length caps, not a
#: physical limit.
_MAX_REPS = 8

#: Bond order/kind -- same delocalized-sp²-sheet Pauling estimate as
#: :mod:`precis_se.atomic.generators.sp2`'s CNT/cone families.
_BOND_ORDER = 4.0 / 3.0


def _validate_params(
    raw: dict[str, Any],
) -> tuple[str, float, int, tuple[int, int, int], bool]:
    family = raw.get("family")
    if family not in _FAMILIES:
        raise GeneratorError(
            f"tpms 'family' must be one of {sorted(_FAMILIES)} (P=Schwarz P, "
            f"D=Schwarz D, G=gyroid), got {family!r}"
        )

    cell_raw = raw.get("cell_A")
    if cell_raw is None:
        raise GeneratorError(
            "tpms needs 'cell_A' (float > 0, the cubic cell edge in Å)"
        )
    try:
        cell_A = float(cell_raw)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(
            f"tpms 'cell_A' must be a number, got {cell_raw!r}"
        ) from exc
    if not (cell_A > 0.0):
        raise GeneratorError(f"tpms 'cell_A' must be > 0, got {cell_A!r}")

    n_raw = raw.get("n", _DEFAULT_N)
    try:
        n = int(n_raw)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(f"tpms 'n' must be an integer, got {n_raw!r}") from exc
    if n != n_raw:
        raise GeneratorError(f"tpms 'n' must be an exact integer, got {n_raw!r}")
    if n % 2 == 0:
        raise GeneratorError(
            "tpms 'n' must be odd -- at even n the marching-cubes grid "
            "planes coincide with the surface's own symmetry planes, "
            "producing 18-49% near-degenerate slivers (measured, "
            "docs/backlog/precis-surface-kernel.md); odd n measures "
            f"0.0-5.6%. got n={n}"
        )
    if n < 3:
        raise GeneratorError(f"tpms 'n' must be >= 3, got {n}")

    reps_raw = raw.get("reps", (1, 1, 1))
    try:
        reps_t = tuple(int(x) for x in reps_raw)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(
            f"tpms 'reps' must be 3 integers >= 1, got {reps_raw!r}"
        ) from exc
    if len(reps_t) != 3:
        raise GeneratorError(
            f"tpms 'reps' must have exactly 3 entries, got {reps_raw!r}"
        )
    if any(r < 1 for r in reps_t):
        raise GeneratorError(f"tpms 'reps' entries must all be >= 1, got {reps_t}")
    if any(r > _MAX_REPS for r in reps_t):
        raise GeneratorError(
            f"tpms 'reps' entries must all be <= {_MAX_REPS} (round-1 "
            f"generator-geometry sanity cap), got {reps_t}"
        )
    reps: tuple[int, int, int] = (reps_t[0], reps_t[1], reps_t[2])

    remesh_enabled_raw = raw.get("remesh", True)
    if not isinstance(remesh_enabled_raw, bool):
        raise GeneratorError(
            f"tpms 'remesh' must be a bool, got {remesh_enabled_raw!r}"
        )

    return family, cell_A, n, reps, remesh_enabled_raw


def _rim_port_directions(
    dnet: DualNet, reps: tuple[int, int, int]
) -> dict[int, NDArray[np.float64]]:
    """Global (unrolled) atom index -> outward unit direction, for every
    atom that loses at least one of its 3 dual bonds to the ``reps``
    supercell boundary (mirrors :func:`~precis_surface.dual.unroll`'s own
    cell-by-cell drop logic so the two stay in lock-step, but additionally
    records *which* axis/side each drop happened on -- information
    ``unroll``'s ``(coords, bonds)`` return does not carry).

    A dual bond's shift is always a single ``+1`` on one axis (module
    :mod:`precis_surface.dual`'s convention): the *source* atom of a bond
    loses it when the ``+shift`` neighbour cell falls outside ``reps``
    (outward direction ``+axis``); the *target* atom loses it when the
    ``-shift`` cell that would have supplied it falls outside ``reps``
    (outward direction ``-axis``). An atom missing more than one bond gets
    the (renormalized) sum of its missing directions, matching a corner
    atom's true outward corner direction; an atom whose missing directions
    exactly cancel (rare) falls back to the first one found.
    """
    rx, ry, rz = reps
    n_atoms = len(dnet.atoms)

    def cell_lin(ci: int, cj: int, ck: int) -> int:
        return (ci * ry + cj) * rz + ck

    missing: dict[int, list[NDArray[np.float64]]] = {}
    bonds = dnet.bonds.tolist()
    shifts = dnet.shifts.tolist()
    for ci in range(rx):
        for cj in range(ry):
            for ck in range(rz):
                base = cell_lin(ci, cj, ck) * n_atoms
                for (i, j, _order), (sx, sy, sz) in zip(bonds, shifts, strict=True):
                    if (sx, sy, sz) == (0, 0, 0):
                        continue
                    axis = 0 if sx else (1 if sy else 2)
                    direction = np.zeros(3, dtype=np.float64)
                    direction[axis] = 1.0
                    ni, nj, nk = ci + sx, cj + sy, ck + sz
                    if not (0 <= ni < rx and 0 <= nj < ry and 0 <= nk < rz):
                        missing.setdefault(base + i, []).append(direction.copy())
                    pi, pj, pk = ci - sx, cj - sy, ck - sz
                    if not (0 <= pi < rx and 0 <= pj < ry and 0 <= pk < rz):
                        missing.setdefault(base + j, []).append(-direction)

    out: dict[int, NDArray[np.float64]] = {}
    for atom, dirs in missing.items():
        total = np.sum(dirs, axis=0)
        norm = float(np.linalg.norm(total))
        out[atom] = (total / norm) if norm > 1e-9 else dirs[0]
    return out


def build_tpms(
    raw: dict[str, Any], *, remesh: RemeshFn | None = None
) -> GeneratedBlock:
    """A periodic Schwarz P/D or gyroid carbon scaffold: ``{"family":
    "P"|"D"|"G", "cell_A": float, "n"?: odd int (default 17), "reps"?: 3
    ints >= 1 (default (1,1,1)), "remesh"?: bool (default True)}`` (module
    docstring's pipeline, ring-purity ruling and ``remesh=False`` escape
    hatch)."""
    family, cell_A, n, reps, remesh_enabled = _validate_params(raw)
    field, grad = _FAMILIES[family]

    def f(pts: NDArray[np.float64]) -> NDArray[np.float64]:
        return field(pts, cell_A)

    def g(pts: NDArray[np.float64]) -> NDArray[np.float64]:
        return grad(pts, cell_A)

    pm = periodic_mesh(f, a=cell_A, n=n, grad=g)
    ring_purity_enforced = False
    if remesh is not None:
        pm = remesh(pm)
        ring_purity_enforced = True
    elif remesh_enabled:
        pm, _remesh_report = _default_remesh(pm, f=f, grad=g)
        ring_purity_enforced = True
    _v, _e, _mesh_f, chi = welded_euler(pm)

    dnet = dualise(pm, f=f, grad=g)
    hist_pre = dnet.ring_histogram()
    if ring_purity_enforced:
        bad_rings = {k: v for k, v in sorted(hist_pre.items()) if k not in (5, 6, 7)}
        if bad_rings:
            raise GeneratorError(
                f"tpms family={family} cell_A={cell_A!r} n={n}: ring "
                f"histogram has {sum(bad_rings.values())} ring(s) outside "
                f"{{5,6,7}} after remesh: {bad_rings} (full histogram="
                f"{dict(sorted(hist_pre.items()))}) -- ruling (module "
                "docstring) is rings in {5,6,7} only; known cause is the "
                "frozen wrap seam (precis_surface.remesh's module "
                "docstring), not yet fixed for this family/n. Use "
                "remesh=False in params to inspect the raw scaffold "
                "instead of emitting carbon with disallowed ring sizes."
            )
    coords, bond_pairs = unroll(dnet, reps=reps)
    n_atoms = len(coords)
    if n_atoms == 0:
        raise GeneratorError(
            f"tpms family={family} cell_A={cell_A!r} n={n}: dual net has no "
            "atoms -- generator bug, file a gripe"
        )
    bonds = [(i, j, _BOND_ORDER, "aromatic") for i, j in bond_pairs]

    rim_directions = _rim_port_directions(dnet, reps)
    ports = [
        GeneratedPort(
            name=f"rim{k}",
            atom_index=atom,
            direction=[float(x) for x in direction],
            roles=["covalent", "sp2-rim"],
            expected_element="C",
        )
        for k, (atom, direction) in enumerate(sorted(rim_directions.items()), start=1)
    ]

    rx, ry, rz = reps
    extent = np.array([rx, ry, rz], dtype=np.float64) * cell_A
    # box (module docstring: centred x/y, base-at-z=0, the DSL's own `box`
    # convention -- precis.cad.primitives.box) -- shift x/y to match,
    # leave z unshifted (already 0..extent[2] from `unroll`).
    coords = coords.copy()
    coords[:, 0] -= extent[0] / 2.0
    coords[:, 1] -= extent[1] / 2.0
    envelope = (
        f"box:w{fmt_length_A(extent[0] + 2 * VDW_MARGIN_A)}"
        f"d{fmt_length_A(extent[1] + 2 * VDW_MARGIN_A)}"
        f"h{fmt_length_A(extent[2] + 2 * VDW_MARGIN_A)}"
    )

    hist = hist_pre
    topology: dict[str, Any] = {
        "family": family,
        "cell_A": cell_A,
        "n": n,
        "reps": [rx, ry, rz],
        "chi_per_cell": chi,
        "rings": dict(sorted(hist.items())),
        "heptagons": hist.get(7, 0),
        "pentagons": hist.get(5, 0),
        "n_atoms_per_cell": len(dnet.atoms),
        "n_bonds_per_cell": len(dnet.bonds),
    }
    ring_desc = "ring histogram" if ring_purity_enforced else "raw ring histogram"
    scaffold_desc = (
        "Remeshed to a degree-controlled net (precis_surface.remesh): "
        "rings are enforced within {5,6,7} before this block is emitted -- "
        "still a topology-first scaffold, not force-field relaxed: bond "
        "lengths carry real spread and vertex positions are only "
        "Newton-reprojected onto the level set (module docstring)."
        if ring_purity_enforced
        else "SCAFFOLD, not remeshed (remesh=False): this net keeps "
        "marching-cubes' own ring sizes (4/5/8/9-rings alongside "
        "hexagons/heptagons, unenforced against the {5,6,7} ruling) and "
        "un-equalized bond lengths -- topology is exact, geometry is a "
        "raw preview (module docstring)."
    )
    provenance = (
        f"TPMS schwarzite scaffold, family={family}, cell_A={cell_A:g} Å, "
        f"n={n}, reps={list(reps)} -- triangulate-and-dualise construction "
        "(the dual of a degree-controlled triangulation IS the hex tiling; "
        "docs/backlog/precis-surface-kernel.md 'Slice 1 -- the dual route'; "
        "Terrones & Terrones, New J. Phys. 5 (2003) 126, pa449642; see that "
        "backlog section's 'Prior art' for the leapfrog/schwarzite "
        "literature this route is not the first to use); "
        f"{n_atoms} atoms, {len(bonds)} bonds, {len(ports)} open-valence "
        f"rim port(s); chi_per_cell={chi}, {ring_desc} {topology['rings']}. "
        f"{scaffold_desc}"
    )

    return GeneratedBlock(
        envelope=envelope,
        ports=ports,
        topology=topology,
        provenance=provenance,
        elements=["C"] * n_atoms,
        coords=coords,
        bonds=bonds,
    )
