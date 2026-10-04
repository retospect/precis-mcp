"""Grid-side operations on a sampled signed-distance field.

Everything a :class:`~precis.cad.primitives.Field` leaf needs that is not
a point query: the **exact Euclidean re-distance** the rounding contract
names (``docs/backlog/cad-sdf-rounding-and-field-export.md`` — "shrink in
the parameters, never in the field … or re-distance in between"), the
morphology built on it, the SIMP density bridge, and the wire format the
store keeps a grid in. numpy only — no scipy, no skimage; the kernel
boundary (no DB, no handler) holds here too.

* :func:`redistance` — exact signed Euclidean distance transform of a
  binary occupancy (or of a field's own sign), both sides. Per axis a
  Felzenszwalb–Huttenlocher lower-envelope pass over the squared
  distance, run on every line of the grid at once (the sequential
  parabola stack is kept per line as arrays and advanced in lock-step;
  the pops of a step are masked to the lines that need them). For a bool
  occupancy the surface sits half a voxel outside the last inside sample,
  so the result reads ``±0.5·pitch`` on the two samples straddling it.
  For a :class:`Field` the input's own sub-voxel zero set is kept: a
  closest-point transform to foot points fitted from the samples beside
  each sign change (exact for an axis-aligned plane, within ~0.4 voxel
  for an oblique one), so an offset survives the
  re-distance instead of snapping back to the lattice (gr464340).
* :func:`offset` — a constant offset (``-r`` dilates, ``+r`` erodes) of a
  field re-distanced first unless it is flagged ``exact``. The result is
  exact on the side the offset moved *away* from and only sign-correct on
  the other (a concave corner's inside distance is shorter than
  ``|d| + r`` once the corner is rounded), so it is **not** flagged
  exact; the compound ops below re-distance in between, which is the
  whole point of the contract's rule.
* :func:`open` (erode → redistance → dilate — rounds convex edges to
  ``r``; a feature thinner than ``2r`` vanishes and is **reported**, per
  connected component, never dropped silently) and :func:`close` (dilate
  → redistance → erode — rounds concave seams to an exact ``r``, fills
  necks narrower than ``2r``).
* :func:`from_density` — threshold a SIMP density grid, re-distance. The
  one place cad reads an optimiser's output; :mod:`precis.structsolve.simp`
  stays cad- and store-free.
* :func:`encode_field` / :func:`decode_field` — the ``chunk_blobs`` payload
  (a small JSON header + raw little-endian float32), content-addressed by
  the store (:meth:`precis.store.Store.put_field`).

Every grid here is in the caller's length unit (the store keeps metres;
the export backend scales a field to millimetres through
:meth:`~precis.cad.primitives.Field.scaled`).
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.primitives import Field
from precis.cad.vec import Vec3, as_vec3

#: A point the caller may hand in as a tuple/list/array — every origin
#: argument below is coerced through :func:`~precis.cad.vec.as_vec3`.
Point3 = Vec3 | Sequence[float]

#: Wire-format magic of an encoded field payload (:func:`encode_field`).
FIELD_MAGIC = b"PSDF"

#: MIME type the store records on a field's ``chunk_blobs`` row.
FIELD_MIME = "application/x-precis-sdf-grid"

#: Voxels of padding :func:`redistance` adds around a binary occupancy so
#: the zero set never touches the grid box (the field is only trusted
#: inside its AABB — see :class:`~precis.cad.primitives.Field`).
REDISTANCE_PAD = 2

# ---------------------------------------------------------------------------
# exact Euclidean distance transform (Felzenszwalb–Huttenlocher, vectorised
# across lines)
# ---------------------------------------------------------------------------


def _edt_1d_pass(f: NDArray[np.float64]) -> NDArray[np.float64]:
    """One lower-envelope pass along axis 0 of ``f`` (shape ``(n, m)``:
    ``m`` independent lines of ``n`` squared distances) →
    ``d[p] = min_q f[q] + (p − q)²`` per line. See :func:`_edt_1d_arg`."""
    return _edt_1d_arg(f)[0]


def _edt_1d_arg(
    f: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """:func:`_edt_1d_pass` plus the minimiser: ``(d, arg)`` with
    ``arg[p] = argmin_q f[q] + (p − q)²`` per line (the row index ``q``),
    which is what lets :func:`_edt_sq_nearest` carry a feature transform
    through the three separable passes.

    The Felzenszwalb–Huttenlocher parabola stack is kept per line
    (``v``: site of the k-th envelope parabola, ``z``: where it takes over
    from the previous one, ``k``: stack top), advanced for every line at
    the same ``q``; a step's pops are masked to the lines whose newest
    parabola still buries the stack top. Amortised the same O(n) per line
    as the scalar algorithm; every op is a vector op over the ``m`` lines.
    """
    n, m = f.shape
    if n == 1:
        return f.copy(), np.zeros((1, m), dtype=np.int64)
    cols = np.arange(m)
    v = np.zeros((n, m), dtype=np.int64)
    z = np.empty((n + 1, m), dtype=np.float64)
    z[0] = -np.inf
    z[1] = np.inf
    k = np.zeros(m, dtype=np.int64)
    fq_all = f + (np.arange(n, dtype=np.float64) ** 2)[:, None]  # f[q] + q²
    for q in range(1, n):
        fq = fq_all[q]
        while True:
            vk = v[k, cols]
            s = (fq - fq_all[vk, cols]) / (2.0 * (q - vk))
            pop = s <= z[k, cols]  # never true at k == 0: z[0] is -inf
            if not np.any(pop):
                break
            k[pop] -= 1
        k += 1
        v[k, cols] = q
        z[k, cols] = s
        z[k + 1, cols] = np.inf
    d = np.empty_like(f)
    arg = np.empty((n, m), dtype=np.int64)
    k = np.zeros(m, dtype=np.int64)
    for p in range(n):
        while True:
            adv = z[k + 1, cols] < p
            if not np.any(adv):
                break
            k[adv] += 1
        vk = v[k, cols]
        d[p] = (p - vk) ** 2 + f[vk, cols]
        arg[p] = vk
    return d, arg


def _edt_sq(sites: NDArray[np.bool_]) -> NDArray[np.float64]:
    """Squared Euclidean distance (in voxels) from every voxel to the
    nearest ``True`` voxel of ``sites`` — exact, separable, three
    lower-envelope passes. ``sites`` must have at least one ``True``."""
    n = np.array(sites.shape, dtype=np.int64)
    big = float(np.sum(n.astype(np.float64) ** 2)) * 4.0 + 1.0
    d = np.where(sites, 0.0, big).astype(np.float64)
    for axis in range(3):
        moved = np.moveaxis(d, axis, 0)
        flat = moved.reshape(moved.shape[0], -1)
        out = _edt_1d_pass(np.ascontiguousarray(flat))
        d = np.moveaxis(out.reshape(moved.shape), 0, axis)
    return np.ascontiguousarray(d)


def _edt_sq_nearest(
    sites: NDArray[np.bool_],
) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """:func:`_edt_sq` plus the feature transform: ``(d_sq, nearest)`` with
    ``nearest`` the flat (C-order) index of a nearest ``True`` voxel. The
    minimiser of each 1-D pass picks which earlier-pass row a voxel's
    distance came from, so the site index rides along with it."""
    n = np.array(sites.shape, dtype=np.int64)
    big = float(np.sum(n.astype(np.float64) ** 2)) * 4.0 + 1.0
    d = np.where(sites, 0.0, big).astype(np.float64)
    idx = np.arange(sites.size, dtype=np.int64).reshape(sites.shape)
    for axis in range(3):
        moved_d = np.moveaxis(d, axis, 0)
        moved_i = np.moveaxis(idx, axis, 0)
        shape = moved_d.shape
        flat_d = np.ascontiguousarray(moved_d.reshape(shape[0], -1))
        flat_i = np.ascontiguousarray(moved_i.reshape(shape[0], -1))
        out, arg = _edt_1d_arg(flat_d)
        carried = np.take_along_axis(flat_i, arg, axis=0)
        d = np.moveaxis(out.reshape(shape), 0, axis)
        idx = np.moveaxis(carried.reshape(shape), 0, axis)
    return np.ascontiguousarray(d), np.ascontiguousarray(idx)


def _interface_feet(
    grid: NDArray[np.float64],
) -> tuple[NDArray[np.bool_], NDArray[np.float64]]:
    """The samples next to the zero set and, for each, its foot point on
    the zero set — read off the field's values, not just its sign.

    A sample is in the band when a 6-neighbour has the other sign
    (``grid <= 0`` is inside). Along each axis with such a neighbour the
    linear zero crossing sits at signed offset ``t_i = ±φ_s / (φ_s − φ_n)``
    voxels (the nearer of the two directions). The interface is taken as
    the plane through those axis intercepts; the foot is the sample's
    orthogonal projection onto it, ``s + t̂ / Σ(1/t_i²)`` with
    ``t̂_i = 1/t_i`` (0 on an axis with no crossing) — a plane fit that
    needs no gradient, so a field of any slope gives the same foot.
    Returns ``(band, feet)``, ``feet`` of shape ``(*grid.shape, 3)`` in
    voxel coordinates (meaningful on the band only)."""
    inside = grid <= 0.0
    n = grid.shape
    inv_t = np.zeros((*n, 3), dtype=np.float64)
    on_surface = np.zeros(n, dtype=bool)
    for axis in range(3):
        best = np.full(n, np.inf, dtype=np.float64)
        signed = np.zeros(n, dtype=np.float64)
        for step in (1, -1):
            nb = np.roll(grid, -step, axis=axis)
            nb_in = np.roll(inside, -step, axis=axis)
            valid = np.ones(n, dtype=bool)
            edge = [slice(None)] * 3
            edge[axis] = slice(-1, None) if step == 1 else slice(0, 1)
            valid[tuple(edge)] = False  # np.roll wrapped these
            cross = valid & (nb_in != inside)
            frac = np.abs(
                np.where(cross, grid / np.where(cross, grid - nb, 1.0), np.inf)
            )
            nearer = frac < best
            best = np.where(nearer, frac, best)
            signed = np.where(nearer, step * frac, signed)
        hit = np.isfinite(best)
        on_surface |= hit & (best == 0.0)
        safe = np.where(hit & (best > 0.0), signed, 1.0)
        inv_t[..., axis] = np.where(hit & (best > 0.0), 1.0 / safe, 0.0)
    band = np.any(inv_t != 0.0, axis=-1) | on_surface
    norm_sq = np.sum(inv_t * inv_t, axis=-1, keepdims=True)
    shift = inv_t / np.where(norm_sq > 0.0, norm_sq, 1.0)
    shift = np.where(on_surface[..., None], 0.0, shift)
    feet = np.indices(n, dtype=np.float64).transpose(1, 2, 3, 0) + shift
    return band, feet


def _redistance_field(grid: NDArray[np.float64], pitch: float) -> NDArray[np.float64]:
    """Signed distance that keeps the input field's sub-voxel zero set
    (gr464340): a closest-point transform. Each band sample has a foot
    point on the interface (:func:`_interface_feet`); every sample's
    distance is the distance to the foot of its nearest band sample on its
    own side, refined over the feet its neighbours resolved to. Exact for
    an axis-aligned plane (gr464340's test); an oblique plane comes back
    within ~0.4 voxel (an axis whose crossing lies beyond the neighbour is
    left out of the plane fit) where the bool path is off by up to 0.5.
    Re-placing the surface half a pitch outside the last inside sample
    instead (the bool path) snaps an offset back to the lattice, which
    biased :func:`open`/:func:`close` by up to half a pitch."""
    inside = grid <= 0.0
    band, feet = _interface_feet(grid)
    flat_feet = feet.reshape(-1, 3)
    shape = np.array(grid.shape, dtype=np.int64)
    out = np.zeros(grid.shape, dtype=np.float64)
    for side, sign in ((inside, -1.0), (~inside, 1.0)):
        sites = band & side
        if not np.any(sites):
            continue  # no sign change: the caller refused empty/full already
        site_d_sq, nearest = _edt_sq_nearest(sites)
        pts = np.argwhere(side)  # (k, 3) voxel coordinates on this side
        own = nearest[pts[:, 0], pts[:, 1], pts[:, 2]]
        d_sq = np.sum((pts - flat_feet[own]) ** 2, axis=-1)
        # The nearest band SAMPLE need not own the nearest FOOT (a sample
        # with one axis crossing fits a plane square to that axis): within
        # a few voxels of the band, also try the feet the 26 neighbours
        # resolved to and keep the closest. Farther out the choice of foot
        # barely moves the distance, so the refinement is skipped there.
        near = site_d_sq[pts[:, 0], pts[:, 1], pts[:, 2]] <= _REFINE_REACH_SQ
        q = pts[near]
        best = d_sq[near]
        for off in _NEIGHBOUR_OFFSETS:
            nb = np.clip(q + np.array(off), 0, shape - 1)
            cand = nearest[nb[:, 0], nb[:, 1], nb[:, 2]]
            best = np.minimum(best, np.sum((q - flat_feet[cand]) ** 2, axis=-1))
        d_sq[near] = best
        d = np.sqrt(d_sq) * pitch
        if sign > 0.0:
            # an outside sample must read > 0 (``<= 0`` is inside)
            d = np.maximum(d, np.finfo(np.float32).tiny)
        out[pts[:, 0], pts[:, 1], pts[:, 2]] = sign * d
    return out


#: Squared voxel distance from the band within which
#: :func:`_redistance_field` refines a sample's foot over its neighbours'.
_REFINE_REACH_SQ = 36.0

#: The 26 neighbour shifts :func:`_redistance_field` borrows candidate
#: feet from.
_NEIGHBOUR_OFFSETS: tuple[tuple[int, int, int], ...] = tuple(
    (i, j, k)
    for i in (-1, 0, 1)
    for j in (-1, 0, 1)
    for k in (-1, 0, 1)
    if (i, j, k) != (0, 0, 0)
)


def _padded(binary: NDArray[np.bool_], pad: int) -> NDArray[np.bool_]:
    return np.pad(binary, pad, mode="constant", constant_values=False)


def redistance(
    binary_or_field: NDArray[np.bool_] | Field,
    pitch: float | None = None,
    origin: Point3 | None = None,
    *,
    pad: int | None = None,
) -> Field:
    """Exact signed Euclidean distance to the surface of an occupancy.

    ``binary_or_field`` is either a ``(nx, ny, nz)`` bool array (``True``
    = material; ``pitch`` and ``origin`` — the position of voxel
    ``[0,0,0]``'s centre — are then required) or a :class:`Field`, whose
    sign (``grid <= 0``) is the occupancy and whose pitch/origin carry
    over. The result is flagged ``exact``. For a bool input its zero set
    is half a pitch outside the last inside voxel centre; for an unpadded
    field input it is the field's own (linearly interpolated) zero set
    (:func:`_redistance_field`). A field passed with ``pad > 0`` is padded
    and re-distanced from its sign only — the bool path.

    A bool input is padded by ``pad`` voxels (default
    :data:`REDISTANCE_PAD`) of empty space on every side so the surface
    stays inside the field's AABB; the origin shifts accordingly. A field
    input is **not** padded (its box is already the caller's contract).
    Raises ``ValueError`` when the occupancy is empty or full — neither
    has a surface to measure from.
    """
    if isinstance(binary_or_field, Field):
        fld = binary_or_field
        binary = np.asarray(fld.grid) <= 0.0
        pitch = fld.pitch
        origin = fld.origin
        if pad is None:
            pad = 0
    else:
        binary = np.asarray(binary_or_field, dtype=bool)
        if binary.ndim != 3:
            raise ValueError(
                f"occupancy must be (nx, ny, nz), got shape {binary.shape}"
            )
        if pitch is None or origin is None:
            raise ValueError("redistance of a bool occupancy needs pitch= and origin=")
        if pad is None:
            pad = REDISTANCE_PAD
    if not (pitch is not None and pitch > 0.0 and math.isfinite(pitch)):
        raise ValueError(f"pitch must be a positive length, got {pitch}")
    origin = as_vec3(origin) - pad * float(pitch)
    if pad > 0:
        binary = _padded(binary, pad)
    n_in = int(binary.sum())
    if n_in == 0:
        raise ValueError("occupancy is empty — no material, no surface to re-distance")
    if n_in == binary.size:
        raise ValueError(
            "occupancy is full — no empty voxel, no surface to re-distance"
        )
    if isinstance(binary_or_field, Field) and pad == 0:
        # a field carries its zero set between samples: keep it (gr464340)
        signed = _redistance_field(
            np.asarray(binary_or_field.grid, dtype=np.float64), float(pitch)
        )
    else:
        d_out = np.sqrt(_edt_sq(binary))  # outside voxels: distance to material
        d_in = np.sqrt(_edt_sq(~binary))  # inside voxels: distance to void
        # The surface lies midway between an inside and an outside centre:
        # shift both sides in by half a voxel so they read ±0.5 there.
        signed = np.where(binary, -(d_in - 0.5), d_out - 0.5) * float(pitch)
    return Field(
        grid=signed.astype(np.float32),
        pitch=float(pitch),
        origin=as_vec3(origin),
        exact=True,
    )


# ---------------------------------------------------------------------------
# connected components (6-connectivity; label propagation + pointer jumping)
# ---------------------------------------------------------------------------


def label_components(binary: NDArray[np.bool_]) -> tuple[NDArray[np.int64], int]:
    """6-connected components of a bool grid → ``(labels, count)`` with
    ``labels`` the component index (``0 .. count-1``) per ``True`` voxel and
    ``-1`` elsewhere. Iterative minimum-label propagation over the six
    face neighbours, with pointer jumping between passes so a long thin
    strut converges in O(log) rounds rather than O(length)."""
    binary = np.asarray(binary, dtype=bool)
    flat = np.arange(binary.size, dtype=np.int64)
    labels = np.where(binary.ravel(), flat, -1)
    shape = binary.shape
    lab3 = labels.reshape(shape)
    mask = binary
    while True:
        cur = lab3.copy()
        for axis in range(3):
            fwd = np.roll(cur, 1, axis=axis)
            bwd = np.roll(cur, -1, axis=axis)
            # roll wraps; kill the wrapped slab on each side
            sl_f = [slice(None)] * 3
            sl_f[axis] = slice(0, 1)
            fwd[tuple(sl_f)] = -1
            sl_b = [slice(None)] * 3
            sl_b[axis] = slice(-1, None)
            bwd[tuple(sl_b)] = -1
            for nb in (fwd, bwd):
                take = mask & (nb >= 0) & (nb < cur)
                cur = np.where(take, nb, cur)
        # pointer jumping: label ← label of the voxel my label points at
        flat_cur = cur.ravel()
        while True:
            nxt = np.where(flat_cur >= 0, flat_cur[np.maximum(flat_cur, 0)], -1)
            if np.array_equal(nxt, flat_cur):
                break
            flat_cur = nxt
        cur = flat_cur.reshape(shape)
        if np.array_equal(cur, lab3):
            break
        lab3 = cur
    roots = np.unique(lab3[mask]) if np.any(mask) else np.empty(0, dtype=np.int64)
    dense = np.full(lab3.shape, -1, dtype=np.int64)
    if len(roots):
        dense[mask] = np.searchsorted(roots, lab3[mask])
    return dense, len(roots)


# ---------------------------------------------------------------------------
# morphology
# ---------------------------------------------------------------------------


def _exact(fld: Field) -> Field:
    return fld if fld.exact else redistance(fld)


def offset(fld: Field, r: float) -> Field:
    """The field shifted by a constant: ``offset(f, -r)`` dilates the body
    by ``r`` (a Minkowski sum with the ``r``-ball), ``offset(f, +r)``
    erodes it. Re-distances first when ``fld`` is not flagged exact — an
    offset of a merely sign-correct field is not an offset of the body.
    The result is not flagged exact (see the module docstring)."""
    if not math.isfinite(r):
        raise ValueError(f"offset needs a finite length, got {r}")
    base = _exact(fld)
    return Field(
        grid=base.grid + np.float32(r),
        pitch=base.pitch,
        origin=base.origin,
        exact=False,
    )


@dataclass(frozen=True)
class FieldFinding:
    """One design finding a morphology op raises — the same shape as
    :class:`precis.cad.printability.PrintFinding` (rule / measured /
    expected / severity / suggested_fix / detail) so a caller can list them
    together; this module imports neither."""

    rule: str
    measured: str
    expected: str
    severity: str
    suggested_fix: str
    detail: str


@dataclass(frozen=True)
class VanishedComponent:
    """A connected piece of material :func:`open` erased that is more than
    edge rounding: a component of the removed set (inside before, outside
    after) reaching farther than :data:`VANISH_MARGIN` · ``r`` (+ half a
    pitch of grid slack) from the opened body. Rounding a convex edge or
    corner removes a sliver within ``(√3 − 1)·r`` of the new surface; a
    strut or blob thinner than ``2r`` extends beyond it. ``voxels`` is the
    component's size in the input grid, ``volume`` that count × pitch³,
    ``centroid`` its voxel-centre mean in the field's local frame."""

    voxels: int
    volume: float
    centroid: tuple[float, float, float]


@dataclass(frozen=True)
class OpenResult:
    """:func:`open`'s result: the opened ``field`` plus every piece of the
    input that vanished, as data (``vanished``) and as findings
    (``findings``, one per piece). Empty lists = nothing was lost.
    ``empty`` is True when the erosion left nothing at all — ``field`` is
    then the eroded (all-positive, material-free) grid and every
    component of the input is in ``vanished`` with severity ``error``."""

    field: Field
    vanished: list[VanishedComponent]
    findings: list[FieldFinding]
    empty: bool = False


#: Removed material farther than this many ``r`` from the opened body is a
#: vanished feature, not the sliver a rounded corner sheds (a cube corner's
#: tip is ``(√3 − 1)·r`` from the sphere cap that replaces it).
VANISH_MARGIN = math.sqrt(3.0) - 1.0


def _components_lost(
    before: Field, after: Field | None, r: float
) -> list[VanishedComponent]:
    """The vanished pieces of ``before`` under an opening whose result is
    ``after`` (``None`` = the erosion left nothing: every component of
    ``before`` vanished)."""
    b_in = np.asarray(before.grid) <= 0.0
    if after is None:
        removed = b_in
        far = b_in
    else:
        a_in = np.asarray(after.grid) <= 0.0
        removed = b_in & ~a_in
        # after.grid is exact outside the opened body (a dilation is exact
        # on the side it moved away from), so it measures each removed
        # voxel's distance to the surviving surface directly.
        far = removed & (
            np.asarray(after.grid) > VANISH_MARGIN * r + 0.5 * before.pitch
        )
    if not np.any(far):
        return []
    labels, count = label_components(removed)
    lost = np.unique(labels[far])
    out: list[VanishedComponent] = []
    idx = np.indices(before.grid.shape).reshape(3, -1).T  # (N, 3)
    flat_labels = labels.ravel()
    for lab in lost.tolist():
        sel = flat_labels == lab
        n_vox = int(sel.sum())
        centre = before.origin + idx[sel].mean(axis=0) * before.pitch
        out.append(
            VanishedComponent(
                voxels=n_vox,
                volume=n_vox * before.pitch**3,
                centroid=(float(centre[0]), float(centre[1]), float(centre[2])),
            )
        )
    return out


def _check_radius(r: float, fld: Field, what: str) -> None:
    if not (r > 0.0 and math.isfinite(r)):
        raise ValueError(f"{what} needs a positive radius, got {r}")
    if r < 0.5 * fld.pitch:
        raise ValueError(
            f"{what}: radius {r:g} is below half the field pitch {fld.pitch:g} — "
            "the grid cannot resolve it; re-sample finer or use a larger radius"
        )


def open(fld: Field, r: float) -> OpenResult:
    """Morphological opening by radius ``r``: erode → re-distance → dilate.
    Rounds every convex edge and corner to ``r`` (the grid twin of
    slice-1's ``rd``); anything thinner than ``2r`` is erased — and
    **reported**: each vanished piece (a connected component of the
    removed material beyond the edge-rounding margin, see
    :class:`VanishedComponent`) comes back in :attr:`OpenResult.vanished`
    / :attr:`OpenResult.findings` (count, voxel volume, centroid). For an
    organic strut that is a design finding, not housekeeping. When the
    erosion leaves nothing (nothing is thicker than ``2r``) the result is
    still returned — ``empty=True``, the material-free eroded grid as the
    field, every input component reported at severity ``error`` — rather
    than raised, so the caller sees *what* vanished."""
    _check_radius(r, fld, "open")
    base = _exact(fld)
    eroded = offset(base, +r)
    empty = not np.any(np.asarray(eroded.grid) <= 0.0)
    if empty:
        opened = eroded
        lost = _components_lost(base, None, r)
    else:
        opened = offset(redistance(eroded), -r)
        lost = _components_lost(base, opened, r)
    findings = [
        FieldFinding(
            rule="open-vanished-component",
            measured=(
                f"{c.voxels} voxel(s), {c.volume:.6g} unit³ at "
                f"({c.centroid[0]:.6g}, {c.centroid[1]:.6g}, {c.centroid[2]:.6g})"
            ),
            expected=f"every feature thicker than 2r = {2 * r:g}",
            severity="error" if empty else "warn",
            suggested_fix="thicken the feature, lower r, or accept the loss",
            detail=(
                f"open(r={r:g}) erased the whole body — nothing is thicker than 2r"
                if empty
                else (
                    f"a connected piece of material vanished under open(r={r:g}) "
                    f"(thinner than 2r; it reached beyond {VANISH_MARGIN:.3g}·r "
                    "from the opened body, so this is not edge rounding)"
                )
            ),
        )
        for c in lost
    ]
    return OpenResult(field=opened, vanished=lost, findings=findings, empty=empty)


def close(fld: Field, r: float) -> Field:
    """Morphological closing by radius ``r``: dilate → re-distance →
    erode. Rounds every concave edge to an exact ``r`` (the exact concave
    fillet the contract reserves for a re-distanced field) and fills any
    neck or gap narrower than ``2r``. The dilated body must stay inside
    the field's box: ``r`` beyond the input's clearance to the box edge is
    refused, because a field is only trusted inside its own AABB."""
    _check_radius(r, fld, "close")
    base = _exact(fld)
    dilated = offset(base, -r)
    g = np.asarray(dilated.grid)
    rim = np.concatenate(
        [g[0].ravel(), g[-1].ravel(), g[:, 0].ravel(), g[:, -1].ravel(),
         g[:, :, 0].ravel(), g[:, :, -1].ravel()]
    )  # fmt: skip
    if np.any(rim <= 0.0):
        raise ValueError(
            f"close: dilating by {r:g} reaches the field's box — the field is "
            "only trusted inside its own AABB; pad the grid (redistance with a "
            "larger pad) or use a smaller radius"
        )
    return offset(redistance(dilated), +r)


# ---------------------------------------------------------------------------
# SIMP bridge
# ---------------------------------------------------------------------------


def from_density(
    rho: NDArray[np.floating[Any]],
    threshold: float = 0.5,
    *,
    pitch: float,
    origin: Point3,
    pad: int | None = None,
) -> Field:
    """A SIMP density grid → an exact field: ``rho >= threshold`` is
    material, then :func:`redistance`. ``rho`` is element-centred
    (``simp_optimize``'s ``density``); ``origin`` is the centre of element
    ``[0,0,0]`` in the caller's frame and ``pitch`` the element size
    (``simp_optimize``'s ``h``, in the caller's length unit)."""
    arr = np.asarray(rho, dtype=np.float64)
    if arr.ndim != 3:
        raise ValueError(f"density must be (nx, ny, nz), got shape {arr.shape}")
    if not (0.0 < threshold < 1.0):
        raise ValueError(f"threshold must lie inside (0, 1), got {threshold}")
    return redistance(arr >= threshold, pitch, origin, pad=pad)


#: Fraction of a pitch within which a column's lowest crossing must lie of
#: the bed plane for :func:`flat_bed` to square its foot.
_FLAT_BED_BAND = 0.25


def flat_bed(
    fld: Field, axis: int, sign: int, *, n_pad: int = 2
) -> tuple[Field, float]:
    """The field with a FLAT bed face, for printing — ``(field, bed)``.

    ``axis`` (0/1/2) and ``sign`` (``-1`` = the bed is on the low-index
    side, ``+1`` = the high side) name the build-down direction ``sign *
    e_axis``. A voxel-derived SDF has its lowest surface rounded off (the
    zero set between the lowest material slab and the empty slab below it
    bevels in at the wall columns), so a 1-voxel fin stands on a knife edge
    and the slicer's first layer shrinks below one line width. This pads
    the field instead of touching any mesh: the slabs beyond the lowest
    material slab are replaced by ``n_pad`` copies of that slab (walls run
    straight on, past the old bottom) — in the columns whose own bottom is
    already within a quarter pitch of the lowest surface, i.e. the flat
    face the staircase rounded off; a smooth or tilted bottom keeps its
    shape — and ``bed`` — the lowest zero
    crossing of the ORIGINAL field along ``axis``, in the field's own
    coordinates and unit — is where a half-space cut must be applied
    (intersect with everything on the far side of ``bed``) to leave a flat
    bottom. The cut is not baked in: between samples it would only be
    approximate. The result's ``exact`` flag is cleared (the extrusion is
    not a Euclidean distance below the old bed). Raises :class:`ValueError`
    when the field holds no material."""
    if axis not in (0, 1, 2) or sign not in (-1, 1):
        raise ValueError(f"axis must be 0/1/2 and sign +/-1, got {axis}, {sign}")
    g = np.moveaxis(np.asarray(fld.grid, dtype=np.float32), axis, 0)
    if sign > 0:
        g = g[::-1]
    inside = g <= 0.0
    cols = inside.any(axis=0)
    if not cols.any():
        raise ValueError("flat_bed: the field holds no material")
    first_all = np.argmax(inside, axis=0)
    first = first_all[cols]
    i0 = int(first.min())
    cc = np.nonzero(cols)
    below = g[np.maximum(first - 1, 0), cc[0], cc[1]].astype(np.float64)
    here = g[first, cc[0], cc[1]].astype(np.float64)
    denom = below - here
    cross = np.where(
        (first > 0) & (denom > 0.0),
        first - 1 + below / np.where(denom > 0.0, denom, 1.0),
        first.astype(np.float64),
    )
    x_bed = float(cross.min())
    # Bevel repair only: extrude a column when ITS lowest zero crossing lies
    # within a quarter pitch of the bed plane (the voxel's own flat face,
    # which the staircase SDF rounds off), plus the empty columns that touch
    # one, so the wall between them stays square. A smooth bottom (a
    # sphere, a tilted face) has only a small contact patch within that band
    # and is not filled down to the bed.
    near_bed = np.zeros(cols.shape, dtype=bool)
    near_bed[cc] = cross <= x_bed + _FLAT_BED_BAND
    ring = near_bed.copy()
    padq = np.pad(near_bed, 1)
    for da in (-1, 0, 1):
        for db in (-1, 0, 1):
            ring |= padq[
                1 + da : 1 + da + cols.shape[0], 1 + db : 1 + db + cols.shape[1]
            ]
    extrude = near_bed | (ring & (g[i0] > 0.0))
    slabs = [np.where(extrude, g[i0], g[max(i0 - n_pad + j, 0)]) for j in range(n_pad)]
    padded = np.concatenate([np.stack(slabs, axis=0), g[i0:]], axis=0)
    origin = np.array(fld.origin, dtype=np.float64)
    p = float(fld.pitch)
    if sign > 0:
        padded = padded[::-1]
        bed = float(origin[axis] + (g.shape[0] - 1 - x_bed) * p)
    else:
        origin[axis] += (i0 - n_pad) * p
        bed = float(fld.origin[axis] + x_bed * p)
    out = Field(grid=np.moveaxis(padded, 0, axis), pitch=p, origin=origin, exact=False)
    return out, bed


# ---------------------------------------------------------------------------
# wire format — what the store keeps in chunk_blobs
# ---------------------------------------------------------------------------


def field_header(
    fld: Field, provenance: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The JSON header :func:`encode_field` writes ahead of the samples."""
    return {
        "shape": list(fld.shape),
        "pitch_m": float(fld.pitch),
        "origin_m": [float(x) for x in fld.origin],
        "dtype": "float32",
        "byteorder": "<",
        "exact": bool(fld.exact),
        "provenance": dict(provenance or {}),
    }


def encode_field(fld: Field, provenance: dict[str, Any] | None = None) -> bytes:
    """``FIELD_MAGIC`` + ``<I`` header length + JSON header + raw
    little-endian float32 samples, C order. The store content-addresses
    the whole payload (:func:`payload_sha256`)."""
    header = json.dumps(
        field_header(fld, provenance), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    body = np.ascontiguousarray(fld.grid, dtype="<f4").tobytes()
    return FIELD_MAGIC + struct.pack("<I", len(header)) + header + body


def decode_field(payload: bytes) -> tuple[dict[str, Any], Field]:
    """Inverse of :func:`encode_field` → ``(header, field)``. Raises
    ``ValueError`` on a foreign or truncated payload."""
    if len(payload) < 8 or payload[:4] != FIELD_MAGIC:
        raise ValueError("not a precis field payload (bad magic)")
    (hlen,) = struct.unpack("<I", payload[4:8])
    header = json.loads(payload[8 : 8 + hlen].decode("utf-8"))
    shape = tuple(int(x) for x in header["shape"])
    expected = 8 + hlen + 4 * int(np.prod(shape))
    if len(payload) != expected:
        raise ValueError(
            f"field payload is {len(payload)} bytes, header says {expected} "
            f"(shape {shape})"
        )
    grid = np.frombuffer(payload, dtype="<f4", offset=8 + hlen).reshape(shape)
    fld = Field(
        grid=grid.astype(np.float32),
        pitch=float(header["pitch_m"]),
        origin=as_vec3(header["origin_m"]),
        exact=bool(header.get("exact", False)),
    )
    return header, fld


def payload_sha256(payload: bytes) -> str:
    """The content address of an encoded field — the ``field:<sha256>`` the
    DSL carries."""
    return hashlib.sha256(payload).hexdigest()
