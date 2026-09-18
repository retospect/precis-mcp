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
  the pops of a step are masked to the lines that need them). The
  surface sits half a voxel outside the last inside sample, so the
  result reads ``±0.5·pitch`` on the two samples straddling it and the
  zero crossing of the trilinear interpolant lands between them.
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
    ``d[p] = min_q f[q] + (p − q)²`` per line.

    The Felzenszwalb–Huttenlocher parabola stack is kept per line
    (``v``: site of the k-th envelope parabola, ``z``: where it takes over
    from the previous one, ``k``: stack top), advanced for every line at
    the same ``q``; a step's pops are masked to the lines whose newest
    parabola still buries the stack top. Amortised the same O(n) per line
    as the scalar algorithm; every op is a vector op over the ``m`` lines.
    """
    n, m = f.shape
    if n == 1:
        return f.copy()
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
    k = np.zeros(m, dtype=np.int64)
    for p in range(n):
        while True:
            adv = z[k + 1, cols] < p
            if not np.any(adv):
                break
            k[adv] += 1
        vk = v[k, cols]
        d[p] = (p - vk) ** 2 + f[vk, cols]
    return d


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
    over. The result is flagged ``exact``; its zero set is half a pitch
    outside the last inside voxel centre.

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
    d_out = np.sqrt(_edt_sq(binary))  # for outside voxels: distance to material
    d_in = np.sqrt(_edt_sq(~binary))  # for inside voxels: distance to void
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
