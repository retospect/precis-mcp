"""3D density-field topology optimisation (SIMP) on a voxel grid, with an
optional additive-manufacturing overhang filter and a naive implicit-lattice
fill — the "nTop leg" of docs/backlog/structural-solution-space.md (slice 4).

**Advisory tier, never a hard DRC.** A compliance number computed from a
voxelised model is an *estimate*: the geometry is a staircase, the mesh is
one trilinear hexahedron per voxel, void is modelled as very soft material
rather than absent material, and only the single linear-elastic load case
you pass is examined. Use the result to *screen* layouts against each other,
never to certify one. Every :class:`SimpResult` carries a ``notes`` tuple
spelling out what the run did and did not check; propagate it rather than
reporting a bare number.

The method is the classic one (Bendsøe–Sigmund, the ``top88``/``top3d``
lineage), 3D and matrix-free:

- **Modified SIMP** stiffness ``E(ρ) = emin + ρ^p (e0 − emin)`` per element.
- **Matrix-free preconditioned conjugate gradients.** scipy is not a core
  dependency here, so there is no sparse assembly and no factorisation:
  ``K·u`` is formed element-by-element (one ``einsum`` against the 24×24 hex
  stiffness for all elements at once, scattered with ``bincount``) and
  preconditioned by the Jacobi diagonal of the same assembled operator.
  Fixed DOFs are handled by projection, not by row elimination.
- **Sensitivity filter** of radius ``rmin`` (in elements): a distance-weighted
  local average, precomputed as a fixed set of in-radius offsets and evaluated
  as shifted sums. It is **mask-aware** — inactive elements neither contribute
  to the numerator nor dilute the weight sum — so nothing bleeds across a
  keep-out. Note that this filter smooths the *gradient*, not the design: it
  is a deliberate, well-known heuristic and is therefore not part of the
  differentiable forward map.
- **Optimality-criteria update** with a move limit of 0.2 and bisection on
  the volume Lagrange multiplier.

**The AM filter** (``build_dir='z+'``) is Langelaar's layer-by-layer filter.
Sweeping from the build plate upwards, the printed density of an element is
``smin(ρ, smax(the 3×3 patch of printed densities one layer below))``: an
element can be no denser than the best support available beneath it. Layer
``k = 0`` sits on the plate and is unconstrained. With **cubic voxels the
3×3 stencil is exactly the 45° overhang rule** — the furthest supporting
element is one voxel across and one voxel down. The smooth maximum is
Langelaar's shifted P-norm ``S(x) = (Σ (x_i+ε)^P)^(1/Q) − ε`` with ``P = 40``,
``ε = 1e-4`` and ``Q = P + ln(n)/ln(ε)`` (``n = 9``), the exponent chosen so
an all-void stencil maps to exactly 0; the smooth minimum is
``½(a + b − √((a−b)² + ε) + √ε)``. Both are smooth so the sweep can be
differentiated, and the chain rule is propagated back through the stored
layer intermediates.

v1 honesty limits on the AM filter, stated so they are not assumed away:

- **One build direction** (``'z+'``), fixed for the whole run. No
  build-orientation search, no multi-axis deposition.
- **Voxel-resolution 45°.** The overhang rule is only as precise as the voxel
  pitch; a 44° wall and a 46° wall are the same staircase to this filter.
- **No bridging allowance.** Real processes can span a short unsupported gap;
  this filter cannot, so it is conservative there.
- **The volume constraint is enforced on the design field**, not the printed
  field, so the achieved printed volume fraction is ≤ the requested one when
  ``build_dir`` is set. :attr:`SimpResult.volume_fraction` reports what was
  actually achieved (printed) and a note records the design-field figure.
- The optimiser differentiates the *smooth* sweep; the returned
  :attr:`SimpResult.density` is the **exact** (hard min/max) sweep of the same
  design field, which is self-supporting by construction. A note reports the
  largest disagreement between the two.

Unit-agnostic, pure over passed-in arrays, no store access (the package
docstring's house rules): the voxel pitch ``h`` and the load magnitudes are
in whatever unit the caller uses, and compliance comes back in
force × length. The cad/se bridge — voxelising a keep-in expression, reading
``objectives.force``/``objectives.fixed``, storing a run summary — is a later
slice and lives outside this module.

Shapes, once: element grids are ``(nx, ny, nz)``; the node grid is
``(nx+1, ny+1, nz+1)``; node ``(i, j, k)`` owns DOFs ``x, y, z`` in that
order. Element ``(i, j, k)`` spans nodes ``(i+dx, j+dy, k+dz)`` for
``dx, dy, dz ∈ {0, 1}``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

#: Poisson's ratio whose element stiffness is derived once at import.
_DEFAULT_NU = 0.3

#: Langelaar smooth-maximum exponent and offset, and the 3×3 stencil size.
#: ``Q`` is fixed by the requirement that an all-void stencil map to exactly
#: zero: ``(n ε^P)^(1/Q) = ε`` ⟺ ``Q = P + ln(n)/ln(ε)``.
_AM_P = 40.0
_AM_EPS = 1e-4
_AM_STENCIL = 9
_AM_Q = _AM_P + math.log(_AM_STENCIL) / math.log(_AM_EPS)

#: Smoothing of the ``min`` in the AM sweep. ``√ε`` is the worst-case error at
#: a crossing, so 1e-4 costs at most 0.005 of density there.
_AM_SMIN_EPS = 1e-4

#: Optimality-criteria move limit and damping exponent (the ``top88`` values).
_OC_MOVE = 0.2
_OC_ETA = 0.5

#: Floor on the design density in the sensitivity-filter denominator — keeps
#: the filtered gradient finite where a design variable has gone to zero.
_FILTER_GAMMA = 1e-3

#: Mean ``|∇g|`` of the gyroid level-set over its own zero surface, measured
#: numerically on a 200³ sample of one period. Turns a requested wall
#: thickness into a level-set threshold (see :func:`lattice_fill`).
_GYROID_MEAN_GRAD = 1.533

#: Local node offsets ``(dx, dy, dz)`` in the element's 24-DOF ordering.
_LOCAL_OFFSETS = np.array(
    [[(n >> 2) & 1, (n >> 1) & 1, n & 1] for n in range(8)], dtype=int
)

#: The 3×3 in-plane stencil of the AM support set.
_AM_OFFSETS: tuple[tuple[int, int], ...] = tuple(
    (di, dj) for di in (-1, 0, 1) for dj in (-1, 0, 1)
)

_HONESTY_NOTES: tuple[str, ...] = (
    "advisory tier: compliance from a voxel model is an estimate (staircase "
    "geometry, one trilinear hex per voxel, void modelled as soft material) "
    "— screening only, never a hard DRC",
    "checked: static linear-elastic compliance under the single load case "
    "supplied, and the volume fraction",
    "not checked: stress, buckling, fatigue, self-weight, contact, material "
    "anisotropy, more than one load case, or whether the field is "
    "manufacturable beyond the overhang rule below",
)


def _isotropic_constitutive(nu: float) -> np.ndarray:
    """6×6 isotropic elasticity matrix for ``E = 1``, Voigt order
    ``(εxx, εyy, εzz, γxy, γyz, γzx)``."""
    scale = 1.0 / ((1.0 + nu) * (1.0 - 2.0 * nu))
    c = np.zeros((6, 6))
    c[:3, :3] = scale * nu
    np.fill_diagonal(c[:3, :3], scale * (1.0 - nu))
    shear = scale * (1.0 - 2.0 * nu) / 2.0
    c[3, 3] = c[4, 4] = c[5, 5] = shear
    return c


def _derive_hex_stiffness(nu: float) -> np.ndarray:
    """24×24 stiffness of a **unit** cube with ``E = 1``, integrated with
    2×2×2 Gauss quadrature from ``Bᵀ C B``.

    Derived rather than transcribed on purpose: a typo in a hand-copied
    24×24 literal is invisible, whereas the quadrature is checkable against
    invariants (symmetry, six rigid-body modes, positive semidefiniteness —
    ``tests/test_structsolve_simp.py`` asserts all three).
    """
    c = _isotropic_constitutive(nu)
    signs = 2.0 * _LOCAL_OFFSETS.astype(float) - 1.0
    gauss = np.array([-1.0, 1.0]) / math.sqrt(3.0)
    ke = np.zeros((24, 24))
    for xi in gauss:
        for eta in gauss:
            for zeta in gauss:
                base = 1.0 + signs * np.array([xi, eta, zeta])
                dn = np.empty((8, 3))
                dn[:, 0] = 0.125 * signs[:, 0] * base[:, 1] * base[:, 2]
                dn[:, 1] = 0.125 * signs[:, 1] * base[:, 0] * base[:, 2]
                dn[:, 2] = 0.125 * signs[:, 2] * base[:, 0] * base[:, 1]
                # Unit cube: x = (ξ+1)/2, so ∂ξ/∂x = 2 and det J = (1/2)³.
                grad = dn * 2.0
                b = np.zeros((6, 24))
                b[0, 0::3] = grad[:, 0]
                b[1, 1::3] = grad[:, 1]
                b[2, 2::3] = grad[:, 2]
                b[3, 0::3] = grad[:, 1]
                b[3, 1::3] = grad[:, 0]
                b[4, 1::3] = grad[:, 2]
                b[4, 2::3] = grad[:, 1]
                b[5, 0::3] = grad[:, 2]
                b[5, 2::3] = grad[:, 0]
                ke += (b.T @ c @ b) * 0.125
    return 0.5 * (ke + ke.T)


#: Derived once at import for the default Poisson ratio.
_KE_UNIT_DEFAULT = _derive_hex_stiffness(_DEFAULT_NU)


def hex_element_stiffness(nu: float = _DEFAULT_NU, h: float = 1.0) -> np.ndarray:
    """The 24×24 element stiffness of a cubic voxel of side ``h`` with
    ``E = 1`` and Poisson ratio ``nu``. Scales linearly in ``h`` (``B ∝ 1/h``,
    ``dV ∝ h³``), so the unit-cube matrix is derived once and rescaled."""
    if not -1.0 < nu < 0.5:
        raise ValueError(f"Poisson ratio must lie in (-1, 0.5), got {nu}")
    if not (h > 0 and math.isfinite(h)):
        raise ValueError(f"voxel pitch h must be a positive finite number, got {h}")
    unit = _KE_UNIT_DEFAULT if nu == _DEFAULT_NU else _derive_hex_stiffness(nu)
    return unit * h


# --------------------------------------------------------------------------
# grid plumbing
# --------------------------------------------------------------------------


def _element_dofs(shape: tuple[int, int, int]) -> np.ndarray:
    """``(ne, 24)`` global DOF indices, elements in C order over
    ``(nx, ny, nz)``."""
    nx, ny, nz = shape
    ei, ej, ek = np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing="ij")
    nodes = np.empty((nx * ny * nz, 8), dtype=np.int64)
    for local in range(8):
        dx, dy, dz = _LOCAL_OFFSETS[local]
        idx = ((ei + dx) * (ny + 1) + (ej + dy)) * (nz + 1) + (ek + dz)
        nodes[:, local] = idx.reshape(-1)
    return (3 * nodes[:, :, None] + np.arange(3)[None, None, :]).reshape(-1, 24)


def _shift(a: np.ndarray, di: int, dj: int) -> np.ndarray:
    """``out[i, j] = a[i+di, j+dj]``, zero outside the grid."""
    out = np.zeros_like(a)
    ni, nj = a.shape[0], a.shape[1]
    src_i = slice(max(di, 0), ni + min(di, 0))
    dst_i = slice(max(-di, 0), ni + min(-di, 0))
    src_j = slice(max(dj, 0), nj + min(dj, 0))
    dst_j = slice(max(-dj, 0), nj + min(-dj, 0))
    out[dst_i, dst_j] = a[src_i, src_j]
    return out


def _shift3(a: np.ndarray, off: tuple[int, int, int]) -> np.ndarray:
    """``out[i, j, k] = a[i+di, j+dj, k+dk]``, zero outside the grid."""
    out = np.zeros_like(a)
    src = []
    dst = []
    for axis, d in enumerate(off):
        n = a.shape[axis]
        src.append(slice(max(d, 0), n + min(d, 0)))
        dst.append(slice(max(-d, 0), n + min(-d, 0)))
    out[dst[0], dst[1], dst[2]] = a[src[0], src[1], src[2]]
    return out


# --------------------------------------------------------------------------
# the AM (overhang) filter
# --------------------------------------------------------------------------


def _smax(stack: np.ndarray) -> np.ndarray:
    """Langelaar's shifted P-norm maximum over ``axis=0`` of ``stack``."""
    total = np.sum(np.power(stack + _AM_EPS, _AM_P), axis=0)
    return np.power(total, 1.0 / _AM_Q) - _AM_EPS


def _smax_partials(s: np.ndarray, values: np.ndarray) -> np.ndarray:
    """``∂smax/∂x`` for one stencil member, given the smooth maximum ``s`` it
    produced and that member's values. Evaluated in logs: the raw power sum
    is ~1e-160 for an all-void stencil, and ``Σ = (s+ε)^Q`` lets the two
    extreme exponents cancel before exponentiating."""
    log_sum = _AM_Q * np.log(s + _AM_EPS)
    return np.exp(
        math.log(_AM_P / _AM_Q)
        + (1.0 / _AM_Q - 1.0) * log_sum
        + (_AM_P - 1.0) * np.log(values + _AM_EPS)
    )


def _smin_partials(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(∂smin/∂a, ∂smin/∂b)`` for the smooth minimum used by the sweep."""
    d = a - b
    root = np.sqrt(d * d + _AM_SMIN_EPS)
    return 0.5 * (1.0 - d / root), 0.5 * (1.0 + d / root)


def _am_sweep_smooth(rho: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Sweep ``+z`` from the build plate. Returns the printed field and the
    per-layer smooth-maximum support field (``support[:, :, 0]`` is unused —
    layer 0 sits on the plate)."""
    printed = np.empty_like(rho)
    support = np.zeros_like(rho)
    printed[:, :, 0] = rho[:, :, 0]
    for k in range(1, rho.shape[2]):
        below = printed[:, :, k - 1]
        stack = np.stack([_shift(below, di, dj) for di, dj in _AM_OFFSETS], axis=0)
        s = _smax(stack)
        support[:, :, k] = s
        a = rho[:, :, k]
        d = a - s
        printed[:, :, k] = 0.5 * (
            a + s - np.sqrt(d * d + _AM_SMIN_EPS) + math.sqrt(_AM_SMIN_EPS)
        )
    return printed, support


def _am_sweep_exact(rho: np.ndarray) -> np.ndarray:
    """The same sweep with a hard ``min``/``max``. Self-supporting by
    construction: if the result exceeds a threshold anywhere above layer 0,
    the stencil maximum beneath it exceeded the same threshold."""
    printed = np.empty_like(rho)
    printed[:, :, 0] = rho[:, :, 0]
    for k in range(1, rho.shape[2]):
        below = printed[:, :, k - 1]
        s = np.max(
            np.stack([_shift(below, di, dj) for di, dj in _AM_OFFSETS], axis=0), axis=0
        )
        printed[:, :, k] = np.minimum(rho[:, :, k], s)
    return printed


def _am_backward(
    dc_dprinted: np.ndarray,
    rho: np.ndarray,
    printed: np.ndarray,
    support: np.ndarray,
) -> np.ndarray:
    """Chain ``∂c/∂printed`` back to ``∂c/∂ρ`` through the stored sweep.

    Layers are visited top-down so that by the time layer ``k`` is processed
    every contribution from layer ``k+1`` has already landed in ``lam[k]``.
    """
    lam = dc_dprinted.astype(float).copy()
    out = np.zeros_like(rho)
    for k in range(rho.shape[2] - 1, 0, -1):
        d_rho, d_support = _smin_partials(rho[:, :, k], support[:, :, k])
        out[:, :, k] = lam[:, :, k] * d_rho
        flow = lam[:, :, k] * d_support
        below = printed[:, :, k - 1]
        for di, dj in _AM_OFFSETS:
            part = _smax_partials(support[:, :, k], _shift(below, di, dj))
            lam[:, :, k - 1] += _shift(flow * part, -di, -dj)
    out[:, :, 0] = lam[:, :, 0]
    return out


def overhang_violations(density: np.ndarray, *, threshold: float = 0.5) -> int:
    """Count elements that a ``+z`` build could not have printed.

    An element above the build plate violates the 45° rule when it is solid
    (``density > threshold``) and no element of the 3×3 patch one layer below
    is solid. Layer ``k = 0`` sits on the plate and never violates. Cubic
    voxels make that stencil exactly 45°; nothing here knows about bridging,
    so the count is conservative.
    """
    rho = np.asarray(density, dtype=float)
    if rho.ndim != 3:
        raise ValueError(f"density must be a 3D (nx, ny, nz) array, got {rho.shape}")
    solid = rho > threshold
    count = 0
    for k in range(1, solid.shape[2]):
        below = solid[:, :, k - 1]
        supported = np.zeros_like(below)
        for di, dj in _AM_OFFSETS:
            supported |= _shift(below, di, dj)
        count += int(np.count_nonzero(solid[:, :, k] & ~supported))
    return count


# --------------------------------------------------------------------------
# the problem: validation, FEA, sensitivities
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Problem:
    """Everything the inner loop needs, validated once. Pure data."""

    domain: np.ndarray
    shape: tuple[int, int, int]
    edof: np.ndarray
    ke: np.ndarray
    forces: np.ndarray
    free: np.ndarray
    penal: float
    e0: float
    emin: float
    build_dir: str | None
    cg_tol: float
    cg_max_iter: int


@dataclass(frozen=True)
class _Eval:
    """One forward solve plus its exact gradient w.r.t. the design field."""

    compliance: float
    dc: np.ndarray
    printed: np.ndarray
    displacement: np.ndarray
    cg_iterations: int
    cg_capped: bool


def _node_index(shape: tuple[int, int, int], node: tuple[int, int, int]) -> int:
    nx, ny, nz = shape
    i, j, k = node
    return int(((i * (ny + 1) + j) * (nz + 1)) + k)


def _touches_active(domain: np.ndarray, node: tuple[int, int, int]) -> bool:
    i, j, k = node
    nx, ny, nz = domain.shape
    lo = (max(i - 1, 0), max(j - 1, 0), max(k - 1, 0))
    hi = (min(i + 1, nx), min(j + 1, ny), min(k + 1, nz))
    if lo[0] >= hi[0] or lo[1] >= hi[1] or lo[2] >= hi[2]:
        return False
    return bool(domain[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]].any())


def _check_node(
    shape: tuple[int, int, int], node: tuple[int, int, int], what: str
) -> tuple[int, int, int]:
    if len(node) != 3:
        raise ValueError(f"{what} node must be a (i, j, k) triple, got {node!r}")
    i, j, k = (int(node[0]), int(node[1]), int(node[2]))
    nx, ny, nz = shape
    if not (0 <= i <= nx and 0 <= j <= ny and 0 <= k <= nz):
        raise ValueError(
            f"{what} node {(i, j, k)} is outside the node grid "
            f"(0..{nx}, 0..{ny}, 0..{nz}) of a {shape} element grid"
        )
    return (i, j, k)


def _build_problem(
    domain: np.ndarray,
    loads: Sequence[tuple[tuple[int, int, int], tuple[float, float, float]]],
    supports: Sequence[tuple[tuple[int, int, int], Iterable[str]]],
    *,
    h: float = 1.0,
    penal: float = 3.0,
    e0: float = 1.0,
    emin: float = 1e-9,
    nu: float = _DEFAULT_NU,
    build_dir: str | None = None,
    cg_tol: float = 1e-8,
    cg_max_iter: int | None = None,
) -> _Problem:
    """Validate the problem and precompute the FEA plumbing. Refusals are
    loud (:class:`ValueError`) and name the offending object: a confident
    wrong shape is worse than no answer."""
    mask = np.asarray(domain, dtype=bool)
    if mask.ndim != 3:
        raise ValueError(f"domain must be a 3D (nx, ny, nz) array, got {mask.shape}")
    if min(mask.shape) < 1:
        raise ValueError(
            f"domain must have at least one element per axis, got {mask.shape}"
        )
    shape = (int(mask.shape[0]), int(mask.shape[1]), int(mask.shape[2]))
    if not mask.any():
        raise ValueError(
            "domain is empty — every element is inactive, so there is nothing "
            "to optimise (keep-outs may have swallowed the keep-in)"
        )
    if build_dir is not None and build_dir != "z+":
        raise ValueError(
            f"build_dir must be None or 'z+', got {build_dir!r} — v1 supports "
            "a single build direction along +z (see the module docstring)"
        )
    if not (0.0 <= emin < e0):
        raise ValueError(f"need 0 <= emin < e0, got emin={emin}, e0={e0}")
    if penal < 1.0:
        raise ValueError(f"SIMP penalty must be >= 1, got {penal}")

    ke = hex_element_stiffness(nu, h)
    edof = _element_dofs(shape)
    n_nodes = (shape[0] + 1) * (shape[1] + 1) * (shape[2] + 1)
    ndof = 3 * n_nodes

    fixed = np.zeros(ndof, dtype=bool)
    n_support_dofs = 0
    for entry in supports:
        node, axes = entry
        node = _check_node(shape, node, "support")
        if not _touches_active(mask, node):
            raise ValueError(
                f"support node {node} touches no active element — it would "
                "restrain nothing; move it onto the domain or drop it"
            )
        letters = list(axes)
        if not letters:
            raise ValueError(
                f"support node {node} fixes no axis — give it 'x', 'y' and/or 'z'"
            )
        base = 3 * _node_index(shape, node)
        for letter in letters:
            if letter not in ("x", "y", "z"):
                raise ValueError(
                    f"support axis {letter!r} on node {node} is not one of 'x', 'y', 'z'"
                )
            fixed[base + "xyz".index(letter)] = True
            n_support_dofs += 1
    if n_support_dofs == 0:
        raise ValueError(
            "no supports — an unrestrained domain has six rigid-body modes and "
            "no unique displacement; declare at least one fixed node"
        )

    forces = np.zeros(ndof)
    for entry_l in loads:
        node, vec = entry_l
        node = _check_node(shape, node, "load")
        if not _touches_active(mask, node):
            raise ValueError(
                f"load node {node} touches no active element — the force would "
                "be applied to void; move it onto the domain"
            )
        if len(vec) != 3:
            raise ValueError(
                f"load on node {node} must be an (fx, fy, fz) triple, got {vec!r}"
            )
        values = np.asarray(vec, dtype=float)
        if not np.all(np.isfinite(values)):
            raise ValueError(f"load on node {node} must be finite, got {vec!r}")
        base_l = 3 * _node_index(shape, node)
        forces[base_l : base_l + 3] += values
    free = ~fixed
    forces[fixed] = 0.0
    if not np.any(forces):
        raise ValueError(
            "no non-zero load survives the supports — compliance would be "
            "identically zero and the optimiser would have nothing to minimise"
        )

    if cg_max_iter is None:
        cg_max_iter = int(min(20000, max(500, 3 * ndof)))
    return _Problem(
        domain=mask,
        shape=shape,
        edof=edof,
        ke=ke,
        forces=forces,
        free=free,
        penal=float(penal),
        e0=float(e0),
        emin=float(emin),
        build_dir=build_dir,
        cg_tol=float(cg_tol),
        cg_max_iter=int(cg_max_iter),
    )


def _pcg(
    problem: _Problem, evec: np.ndarray, u0: np.ndarray | None
) -> tuple[np.ndarray, int, bool]:
    """Matrix-free Jacobi-preconditioned CG on the free DOFs.

    ``K·u`` never exists as a matrix: gather the 24 DOFs of every element,
    contract against the shared 24×24 element stiffness with one ``einsum``,
    scale by ``E(ρ)`` and scatter back with ``bincount``. The preconditioner
    is the diagonal of that same assembled operator.
    """
    edof, ke, free = problem.edof, problem.ke, problem.free
    ndof = problem.forces.size
    flat = edof.ravel()

    diag = np.bincount(
        flat,
        weights=(evec[:, None] * np.diag(ke)[None, :]).ravel(),
        minlength=ndof,
    )
    diag = np.where(free & (diag > 0.0), diag, 1.0)

    def matvec(vec: np.ndarray) -> np.ndarray:
        ue = vec[edof]
        fe = np.einsum("ab,eb->ea", ke, ue) * evec[:, None]
        out = np.bincount(flat, weights=fe.ravel(), minlength=ndof)
        out[~free] = 0.0
        return out

    u = np.zeros(ndof) if u0 is None else np.asarray(u0, dtype=float).copy()
    u[~free] = 0.0
    f = problem.forces
    fnorm = float(np.linalg.norm(f))
    r = f - matvec(u)
    r[~free] = 0.0
    z = r / diag
    p = z.copy()
    rz = float(r @ z)
    iterations = 0
    for iterations in range(1, problem.cg_max_iter + 1):
        if float(np.linalg.norm(r)) <= problem.cg_tol * fnorm:
            iterations -= 1
            break
        ap = matvec(p)
        pap = float(p @ ap)
        if not math.isfinite(pap) or pap <= 0.0:
            break
        alpha = rz / pap
        u += alpha * p
        r -= alpha * ap
        z = r / diag
        rz_next = float(r @ z)
        if not math.isfinite(rz_next):
            break
        p = z + (rz_next / rz) * p
        rz = rz_next
    capped = float(np.linalg.norm(r)) > problem.cg_tol * fnorm
    return u, iterations, capped


def _compliance_and_sensitivity(
    x: np.ndarray, problem: _Problem, u0: np.ndarray | None = None
) -> _Eval:
    """Compliance of the design field ``x`` and its exact gradient.

    The differentiable forward map is ``x → (AM sweep) → E(ρ) → K u = f →
    c = fᵀu``; the sensitivity filter is *not* part of it (it perturbs the
    gradient on purpose). ``tests/test_structsolve_simp.py`` checks this
    gradient against central finite differences through the whole map — that
    is the test that catches a chain-rule slip in the sweep.
    """
    design = np.clip(np.asarray(x, dtype=float), 0.0, 1.0)
    design = np.where(problem.domain, design, 0.0)
    if problem.build_dir is None:
        printed = design
        support = None
    else:
        printed, support = _am_sweep_smooth(design)

    span = problem.e0 - problem.emin
    evec = (problem.emin + np.power(printed, problem.penal) * span).reshape(-1)
    u, iters, capped = _pcg(problem, evec, u0)
    compliance = float(problem.forces @ u)

    ue = u[problem.edof]
    energy = np.einsum("ea,ab,eb->e", ue, problem.ke, ue)
    dc_dprinted = (
        -problem.penal
        * np.power(printed, problem.penal - 1.0)
        * span
        * energy.reshape(problem.shape)
    )
    if support is None:
        dc = dc_dprinted
    else:
        dc = _am_backward(dc_dprinted, design, printed, support)
    dc = np.where(problem.domain, dc, 0.0)
    return _Eval(
        compliance=compliance,
        dc=dc,
        printed=printed,
        displacement=u,
        cg_iterations=iters,
        cg_capped=capped,
    )


# --------------------------------------------------------------------------
# filter + optimality criteria
# --------------------------------------------------------------------------


def _filter_offsets(rmin: float) -> list[tuple[tuple[int, int, int], float]]:
    """In-radius offsets and their linear (cone) weights ``rmin − distance``."""
    reach = math.floor(rmin)
    out: list[tuple[tuple[int, int, int], float]] = []
    for di in range(-reach, reach + 1):
        for dj in range(-reach, reach + 1):
            for dk in range(-reach, reach + 1):
                w = rmin - math.sqrt(di * di + dj * dj + dk * dk)
                if w > 0.0:
                    out.append(((di, dj, dk), w))
    return out


def _filter_weight_sums(
    domain: np.ndarray, offsets: Sequence[tuple[tuple[int, int, int], float]]
) -> np.ndarray:
    """``Σ H`` over *active* neighbours only — the mask-aware normaliser that
    stops a keep-out from diluting its neighbours' filtered gradient."""
    active = domain.astype(float)
    total = np.zeros_like(active)
    for off, w in offsets:
        total += w * _shift3(active, off)
    return total


def _sensitivity_filter(
    dc: np.ndarray,
    x: np.ndarray,
    domain: np.ndarray,
    offsets: Sequence[tuple[tuple[int, int, int], float]],
    weight_sums: np.ndarray,
) -> np.ndarray:
    """The ``top88`` sensitivity filter, mask-aware: inactive elements carry
    ``x = 0`` so they never contribute to the numerator, and they are excluded
    from ``weight_sums`` so they never dilute it."""
    payload = np.where(domain, x * dc, 0.0)
    num = np.zeros_like(payload)
    for off, w in offsets:
        num += w * _shift3(payload, off)
    denom = np.maximum(x, _FILTER_GAMMA) * np.maximum(weight_sums, 1e-30)
    return np.where(domain, num / denom, 0.0)


def _oc_update(
    x: np.ndarray, dc: np.ndarray, domain: np.ndarray, volfrac: float
) -> np.ndarray:
    """Optimality-criteria step: bisect the volume multiplier until the design
    field hits the target volume, with a per-element move limit of 0.2."""
    target = volfrac * float(np.count_nonzero(domain))
    drive = np.maximum(-dc, 0.0)
    lo, hi = 1e-12, 1e12
    best = x
    for _ in range(150):
        mid = 0.5 * (lo + hi)
        cand = x * np.power(drive / mid, _OC_ETA)
        cand = np.clip(cand, x - _OC_MOVE, x + _OC_MOVE)
        cand = np.clip(cand, 0.0, 1.0)
        cand = np.where(domain, cand, 0.0)
        best = cand
        if float(cand[domain].sum()) > target:
            lo = mid
        else:
            hi = mid
        if (hi - lo) <= 1e-10 * (hi + lo):
            break
    return best


# --------------------------------------------------------------------------
# public results + entry points
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SimpResult:
    """One SIMP run. Advisory tier — read :attr:`notes` before quoting any
    number from here."""

    #: ``(nx, ny, nz)`` densities in ``[0, 1]``, exactly 0 outside the domain.
    #: When ``build_dir`` was set this is the **printed** field (the exact
    #: layer sweep, self-supporting by construction); otherwise it equals
    #: :attr:`design_density`.
    density: np.ndarray
    #: The optimiser's own design variables, before the AM sweep.
    design_density: np.ndarray
    #: Compliance at the start of each iteration, plus a final entry for the
    #: returned design — so ``len == iterations + 1``. Lower is stiffer.
    compliance_history: list[float]
    #: Mean of :attr:`density` over the *active* elements.
    volume_fraction: float
    #: Optimality-criteria iterations actually run.
    iterations: int
    #: True when the max per-element density change fell below ``tol``;
    #: False means the iteration budget ran out (see :attr:`notes`).
    converged: bool
    #: Elements the 45° rule says a ``+z`` build could not have made, measured
    #: on the binarised ``density > 0.5`` field. Zero by construction when
    #: ``build_dir='z+'``; the honest measured number otherwise.
    overhang_violation_count: int
    #: What the run did and did not check, plus filter parameters and any
    #: budget exhaustion. Never drop these when reporting the result.
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class LatticeResult:
    """A naive implicit-lattice fill. Reports what it measured; claims
    nothing about printability it did not measure."""

    #: ``(nx, ny, nz)`` 0/1 field — 1 where the lattice wall is, 0 outside
    #: the domain.
    density: np.ndarray
    #: Mean of :attr:`density` over the *active* elements.
    solid_fraction: float
    #: Level-set threshold the requested wall thickness linearised to.
    threshold: float
    #: Measured overhang violations of the fill itself — a gyroid is
    #: *approximately* self-supporting, which is not the same as being so at
    #: this voxel pitch and this phase offset.
    overhang_violation_count: int
    notes: tuple[str, ...] = ()


def simp_optimize(
    domain: np.ndarray,
    loads: Sequence[tuple[tuple[int, int, int], tuple[float, float, float]]],
    supports: Sequence[tuple[tuple[int, int, int], Iterable[str]]],
    *,
    volfrac: float,
    h: float = 1.0,
    penal: float = 3.0,
    rmin: float = 1.5,
    e0: float = 1.0,
    emin: float = 1e-9,
    nu: float = _DEFAULT_NU,
    max_iter: int = 60,
    tol: float = 0.01,
    build_dir: str | None = None,
) -> SimpResult:
    """Minimise compliance over a voxel domain at a fixed volume fraction.

    ``domain`` is a ``(nx, ny, nz)`` bool array of active elements (keep-in
    minus keep-out); density is identically 0 outside it and the filter never
    reaches across. ``loads`` are ``((i, j, k), (fx, fy, fz))`` nodal forces
    and ``supports`` are ``((i, j, k), axes)`` with ``axes`` a subset of
    ``'x'``, ``'y'``, ``'z'`` — both index the ``(nx+1, ny+1, nz+1)`` **node**
    grid. ``h`` is the isotropic voxel pitch in the caller's length unit.

    ``build_dir='z+'`` turns on the AM overhang filter described in the module
    docstring; ``None`` leaves the design unconstrained and reports the
    overhang violations it happens to have.

    Refuses loudly rather than guessing: no supports, a load or support node
    that touches no active element, a load that nothing survives, ``volfrac``
    outside ``(0, 1)``, or an empty domain.
    """
    if not (0.0 < volfrac < 1.0):
        raise ValueError(
            f"volfrac must lie strictly inside (0, 1), got {volfrac} — 0 is an "
            "empty part and 1 is the uncut block; neither is an optimisation"
        )
    if max_iter < 1:
        raise ValueError(f"max_iter must be at least 1, got {max_iter}")
    if rmin < 1.0:
        raise ValueError(
            f"rmin must be at least 1 element, got {rmin} — a sub-element "
            "filter radius does not regularise the checkerboard mode"
        )
    if tol <= 0.0:
        raise ValueError(f"tol must be positive, got {tol}")

    problem = _build_problem(
        domain,
        loads,
        supports,
        h=h,
        penal=penal,
        e0=e0,
        emin=emin,
        nu=nu,
        build_dir=build_dir,
    )
    mask = problem.domain
    offsets = _filter_offsets(rmin)
    weight_sums = _filter_weight_sums(mask, offsets)

    x = np.where(mask, volfrac, 0.0)
    history: list[float] = []
    warm: np.ndarray | None = None
    change = math.inf
    converged = False
    iterations = 0
    cg_capped_any = False

    for iterations in range(1, max_iter + 1):
        ev = _compliance_and_sensitivity(x, problem, warm)
        history.append(ev.compliance)
        warm = ev.displacement
        cg_capped_any = cg_capped_any or ev.cg_capped
        dc = _sensitivity_filter(ev.dc, x, mask, offsets, weight_sums)
        x_next = _oc_update(x, dc, mask, volfrac)
        change = float(np.max(np.abs(x_next - x)[mask]))
        x = x_next
        if change < tol:
            converged = True
            break

    final = _compliance_and_sensitivity(x, problem, warm)
    history.append(final.compliance)
    cg_capped_any = cg_capped_any or final.cg_capped

    if problem.build_dir is None:
        density = x
        discrepancy = 0.0
    else:
        density = _am_sweep_exact(x)
        discrepancy = float(np.max(np.abs(density - final.printed)))

    notes: list[str] = list(_HONESTY_NOTES)
    notes.append(
        f"sensitivity filter: cone weights of radius rmin={rmin} elements, "
        "mask-aware (keep-outs neither contribute nor dilute)"
    )
    notes.append(
        f"SIMP: penal={penal}, e0={e0}, emin={emin}, nu={nu}, voxel pitch h={h}; "
        f"grid {problem.shape}, {int(np.count_nonzero(mask))} active elements"
    )
    if converged:
        notes.append(
            f"converged after {iterations} iterations (max density change "
            f"{change:.4g} < tol {tol})"
        )
    else:
        notes.append(
            f"ITERATION BUDGET EXHAUSTED at max_iter={max_iter}: the last max "
            f"density change was {change:.4g}, still above tol={tol} — the "
            "field is a snapshot of an unfinished descent, not a converged "
            "design"
        )
        tail = history[-min(5, len(history)) :]
        spread = (max(tail) - min(tail)) / max(min(tail), 1e-30)
        if spread > 0.05:
            notes.append(
                f"the compliance history is still oscillating over the last "
                f"{len(tail)} iterations (spread {spread:.1%} — the "
                "optimality-criteria move limit is bouncing): the returned "
                "field is whichever phase the budget happened to end on"
            )
    if cg_capped_any:
        notes.append(
            "at least one CG solve hit its iteration cap without reaching the "
            "residual tolerance — the compliance history is approximate there"
        )
    if problem.build_dir is not None:
        notes.append(
            f"AM filter active, build_dir={problem.build_dir!r}: Langelaar "
            f"layer sweep, smooth max P={_AM_P:g}/eps={_AM_EPS:g} over the 3x3 "
            "stencil (= the 45 degree rule at cubic voxels), smooth min "
            f"eps={_AM_SMIN_EPS:g}"
        )
        notes.append(
            "AM v1 limits: one build direction, voxel-resolution 45 degrees, "
            "no bridging allowance"
        )
        notes.append(
            "the volume constraint was enforced on the DESIGN field; the "
            f"design fraction is {float(x[mask].mean()):.4f} and the printed "
            f"fraction reported here is {float(density[mask].mean()):.4f}"
        )
        notes.append(
            "the returned field is the exact layer sweep; the optimiser "
            "differentiated the smooth surrogate, which differs from it by at "
            f"most {discrepancy:.4g} in density"
        )
    else:
        notes.append(
            "no build direction given: overhang_violation_count is a MEASURED "
            "count on the binarised field, not a guarantee of anything"
        )

    return SimpResult(
        density=density,
        design_density=x,
        compliance_history=history,
        volume_fraction=float(density[mask].mean()),
        iterations=iterations,
        converged=converged,
        overhang_violation_count=overhang_violations(density),
        notes=tuple(notes),
    )


def lattice_fill(
    domain: np.ndarray,
    *,
    cell: float,
    wall: float,
    h: float = 1.0,
    kind: str = "gyroid",
) -> LatticeResult:
    """Fill a voxel domain with an implicit lattice — the naive uniform kind:
    one cell size, one wall thickness, no grading and no conformal warping.

    The gyroid level set ``g = sin X cos Y + sin Y cos Z + sin Z cos X`` (with
    ``X = 2π x / cell``) is sampled at element centres and the wall is the slab
    ``|g| < t``.

    **The threshold linearisation.** Near its own zero surface the level set is
    locally linear, so the slab ``|g| < t`` has thickness ``2t / |∇_x g|``
    with ``∇_x g = (2π/cell) ∇_X g``. Taking ``|∇_X g|`` at its surface mean
    (``1.533``, measured numerically over one period) and setting the
    thickness equal to ``wall`` gives ``t = π · 1.533 · wall / cell``. That is
    a first-order estimate: it drifts as the wall approaches the cell size,
    and element-centre sampling quantises the result further at a coarse
    ``cell/h``. :attr:`LatticeResult.solid_fraction` is therefore **measured**,
    not predicted.

    Overhang violations of the fill are measured too. Gyroids are widely
    described as approximately self-supporting; that is a statement about the
    smooth surface, not about this voxelisation at this phase, so the number
    comes back as data rather than as a claim.
    """
    if kind != "gyroid":
        raise ValueError(
            f"unknown lattice kind {kind!r} — v1 implements 'gyroid' only "
            "(other TPMS families are a later slice, not a silent fallback)"
        )
    mask = np.asarray(domain, dtype=bool)
    if mask.ndim != 3:
        raise ValueError(f"domain must be a 3D (nx, ny, nz) array, got {mask.shape}")
    if not mask.any():
        raise ValueError("domain is empty — nothing to fill")
    for name, value in (("cell", cell), ("wall", wall), ("h", h)):
        if not (value > 0 and math.isfinite(value)):
            raise ValueError(f"{name} must be a positive finite number, got {value}")
    if wall >= cell:
        raise ValueError(
            f"wall ({wall}) must be smaller than cell ({cell}) — a wall at or "
            "above the cell size is a solid block, not a lattice"
        )

    threshold = math.pi * _GYROID_MEAN_GRAD * wall / cell
    nx, ny, nz = mask.shape
    k = 2.0 * math.pi / cell
    xs = k * (np.arange(nx) + 0.5) * h
    ys = k * (np.arange(ny) + 0.5) * h
    zs = k * (np.arange(nz) + 0.5) * h
    gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
    g = np.sin(gx) * np.cos(gy) + np.sin(gy) * np.cos(gz) + np.sin(gz) * np.cos(gx)
    density = np.where(mask & (np.abs(g) < threshold), 1.0, 0.0)

    notes = (
        f"gyroid, cell={cell}, wall={wall}, voxel pitch h={h} "
        f"({cell / h:.3g} voxels per cell) -> level-set threshold "
        f"t={threshold:.4f}",
        "the wall/threshold relation is a first-order linearisation about the "
        "zero surface; solid_fraction is measured, not predicted",
        "overhang_violation_count is MEASURED on this voxelisation — a gyroid "
        "is only approximately self-supporting and this function claims "
        "nothing beyond the count",
        "naive fill: uniform cell, uniform wall, no grading, no conformal "
        "warping to the domain boundary, no stiffness homogenisation",
    )
    return LatticeResult(
        density=density,
        solid_fraction=float(density[mask].mean()),
        threshold=threshold,
        overhang_violation_count=overhang_violations(density),
        notes=notes,
    )
