"""Cylinder-relief height maps — the ``(u, v)`` field a ``relief:`` leaf wraps.

Everything a :class:`~precis.cad.primitives.CylRelief` carries that is not
a point query: the map container, the ``chunk_blobs`` wire format
(content-addressed like :func:`precis.cad.fieldops.encode_field`), the
periodicity rule, and the generators that fill a map — arrays, PNG z-maps
(Pillow), knurl/ribs, and **pull-aware** sketch/stroke/text reliefs.

**The map.** ``values[i, j]`` is the *normalized* height at unrolled
``u = (i + 0.5)/nu`` (periodic: ``u + 1 ≡ u``) and ``v = j/(nv - 1)``;
the leaf renders it as surface radius ``ρ(θ, z) = R + s·depth·h(u, v)``
with ``repeat``/``twist`` applied at sample time, so the same blob serves
any radius/depth and needs no rescaling on export. Typical generators
emit values in ``[0, 1]``; nothing requires it.

**The seam rule** (spec §Model): ``u`` is periodic, so the first and last
columns are neighbours — a map whose edges jump is a cliff in the relief
and is *refused* at construction. ``seam="blend"`` in :func:`hmap_array`
/ :func:`hmap_png` cross-fades a stated number of columns instead (the
correction is a ramp added near both edges, so the map's own slopes are
preserved everywhere else).

**Pull-aware flanks** (spec §Decisions Q1/Q2): the pattern adapts to the
mold. A relief feature pulled by ``k`` slides at parting angle ``φ`` may
lean on its slide's pull only up to the per-point bound

    ``n·p = cos β cos θ − sin β sin θ cos ψ ≥ sin α``

(``θ`` = |signed angle| from the slide's pull direction, ``β`` = flank
tilt off the local surface, ``ψ`` = the direction the flank normal leans:
0 toward the nearer join, 180° toward the pull centre, 90° axial).
Join-facing flanks must lie down (``β ≤ 90° − α − θ``, going to 0 at a
``k = 2`` join — the feature fades out); centre-facing flanks are nearly
free (``β ≤ 90° − α + θ``, even overhanging); axial flanks are nearly
free until the join. Emboss and deboss swap which wall faces the join —
:func:`_flank_raster` takes the leaf's ``sign`` and mirrors the flank
assignment accordingly. The generated surface is the upper envelope
``h(p) = min(1, tan β·d_in/depth)`` off each pixel's nearest boundary,
with ``β`` from that boundary's lean — mouldable *by construction*; the
per-point DRC is slice 2's job.
"""

from __future__ import annotations

import io
import json
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

#: Wire-format magic of an encoded height map (:func:`encode_hmap`).
HMAP_MAGIC = b"PHMP"

#: MIME type the store records on a height map's ``chunk_blobs`` row —
#: the discriminator that keeps ``hmap`` payloads out of ``field:`` /
#: ``field_sha`` lookups (they share the ``field`` chunk kind until a
#: migration adds ``hmap``; lookup is mime-scoped).
HMAP_MIME = "application/x-precis-hmap"

#: Default seam tolerance, as a fraction of the map's height range: the
#: first and last columns may differ by at most this (plus a small
#: absolute floor for flat maps) before construction refuses the map.
SEAM_RTOL = 1e-3

#: Absolute floor on the seam tolerance (a flat map's range is 0).
_SEAM_ATOL = 1e-6


@dataclass(frozen=True, eq=False)
class HeightMap:
    """A normalized height field ``h(u, v)`` on the unrolled cylinder.

    ``values[i, j]`` is the height at ``u = (i + 0.5)/nu``, ``v =
    j/(nv - 1)`` — ``u`` is periodic (column ``nu - 1`` neighbours column
    ``0`` across the seam), ``v`` is clamped. Construction enforces the
    seam rule: the wrap interpolation between the edge columns must be
    near-continuous (see :data:`SEAM_RTOL`); a discontinuous edge pair is
    refused — repair it first with ``seam="blend"`` in the array/PNG
    loaders.

    ``seam`` selects the tolerance policy: ``"strict"`` (default) bounds
    the wrap step to ``rtol`` of the map's range — a hard-edged map whose
    edges disagree is a cliff and is refused; ``"wall"`` additionally
    permits a wrap step up to 1.5× the map's own steepest interior column
    step, for generators that legitimately put a feature wall on the wrap.
    ``pull`` records the pull intent a pull-aware generator built the map
    for — ``{"sign", "k", "alpha", "phi"}`` — carried into the wire header
    so a ``relief:`` leaf can refuse a mismatched layout (Fable s1 review
    item 7). ``None`` on maps with no declared intent (ribs, knurl, PNG).
    """

    values: NDArray[np.floating[Any]]
    pull: dict[str, Any] | None = None
    seam: str = "strict"

    def __post_init__(self) -> None:
        arr = np.asarray(self.values)
        if arr.ndim != 2 or min(arr.shape) < 2:
            raise ValueError(
                f"a height map must be (nu, nv) with both axes >= 2, got "
                f"shape {arr.shape}"
            )
        if not np.all(np.isfinite(arr)):
            raise ValueError("a height map must be finite everywhere")
        if self.seam not in ("strict", "wall"):
            raise ValueError(f"seam must be 'strict' | 'wall', got {self.seam!r}")
        arr = np.ascontiguousarray(arr, dtype=np.float32)
        err = seam_error(arr)
        tol = seam_tolerance(arr, wall=self.seam == "wall")
        if err > tol:
            raise ValueError(
                f"height map is not periodic in u: edge columns differ by "
                f"{err:.4g} (seam tolerance {tol:.4g}) — the u=0/1 wrap is a "
                "cliff in the relief; rebuild with seam='blend' to cross-fade "
                "the edges"
            )
        object.__setattr__(self, "values", arr)

    @property
    def shape(self) -> tuple[int, int]:
        nu, nv = self.values.shape
        return int(nu), int(nv)


def seam_error(values: NDArray[np.floating[Any]]) -> float:
    """The wrap discontinuity: ``max |m[0, :] - m[-1, :]|``."""
    m = np.asarray(values)
    return float(np.max(np.abs(m[0, :] - m[-1, :])))


def seam_tolerance(
    values: NDArray[np.floating[Any]],
    rtol: float = SEAM_RTOL,
    *,
    wall: bool = False,
) -> float:
    """The allowed seam error for ``values``.

    Default (strict): ``rtol`` of the range plus a small absolute floor —
    any harder wrap step is a cliff (Fable s1 review item 5: the interior-
    step allowance was vacuous for hard-edged maps, passing a full-range
    mismatch whenever one interior wall existed). ``wall=True`` opts back
    into the interior-step allowance — 1.5× the map's own steepest column
    step — for generators that legitimately land a feature wall on the
    wrap; callers must know the map is periodic by construction."""
    m = np.asarray(values, dtype=np.float64)
    tol = max(_SEAM_ATOL, rtol * float(m.max() - m.min()))
    if wall:
        u_step = float(np.max(np.abs(np.diff(m, axis=0))))
        tol = max(tol, 1.5 * u_step + _SEAM_ATOL)
    return tol


def check_seam(values: NDArray[np.floating[Any]], rtol: float = SEAM_RTOL) -> None:
    """Raise ``ValueError`` when the map's u-edges disagree past ``rtol``."""
    err, tol = seam_error(values), seam_tolerance(values, rtol)
    if err > tol:
        raise ValueError(
            f"height map is not periodic in u: edge columns differ by "
            f"{err:.4g} (seam tolerance {tol:.4g})"
        )


def blend_seam(values: NDArray[np.floating[Any]], width: int) -> NDArray[np.float64]:
    """Cross-fade the u-seam: a symmetric ramp added over ``width``
    columns at each edge so the edge columns meet at their midpoint.
    Slopes away from the seam are untouched; the result satisfies the
    seam check exactly at the wrap."""
    m = np.asarray(values, dtype=np.float64)
    nu = m.shape[0]
    w = max(1, min(int(width), nu // 2))
    gap = m[0, :] - m[-1, :]  # the jump the wrap would make
    ramp = np.linspace(1.0 / w, 1.0, w)[:, None]
    out = m.copy()
    # the last columns rise/fall by +gap/2, the first by -gap/2, so the
    # two edge columns meet at their average
    out[-w:, :] += 0.5 * gap[None, :] * ramp
    out[:w, :] -= 0.5 * gap[None, :] * ramp[::-1]
    return out


# ---------------------------------------------------------------------------
# wire format — what the store keeps in chunk_blobs
# ---------------------------------------------------------------------------


def hmap_header(
    hm: HeightMap, provenance: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The JSON header :func:`encode_hmap` writes ahead of the samples."""
    nu, nv = hm.shape
    header: dict[str, Any] = {
        "shape": [nu, nv],
        "axes": "u,v",
        "u_periodic": True,
        "dtype": "float32",
        "byteorder": "<",
        "provenance": dict(provenance or {}),
    }
    if hm.pull is not None:
        header["pull"] = dict(hm.pull)
    if hm.seam != "strict":
        header["seam"] = hm.seam
    return header


def encode_hmap(hm: HeightMap, provenance: dict[str, Any] | None = None) -> bytes:
    """``HMAP_MAGIC`` + ``<I`` header length + JSON header + raw
    little-endian float32 samples, C order — the
    :func:`precis.cad.fieldops.encode_field` layout, content-addressed by
    the store the same way."""
    header = json.dumps(
        hmap_header(hm, provenance), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    body = np.ascontiguousarray(hm.values, dtype="<f4").tobytes()
    return HMAP_MAGIC + struct.pack("<I", len(header)) + header + body


def decode_hmap(payload: bytes) -> tuple[dict[str, Any], HeightMap]:
    """Inverse of :func:`encode_hmap` → ``(header, map)``. Raises
    ``ValueError`` on a foreign or truncated payload."""
    if len(payload) < 8 or payload[:4] != HMAP_MAGIC:
        raise ValueError("not a precis height-map payload (bad magic)")
    (hlen,) = struct.unpack("<I", payload[4:8])
    header = json.loads(payload[8 : 8 + hlen].decode("utf-8"))
    shape = tuple(int(x) for x in header["shape"])
    expected = 8 + hlen + 4 * int(np.prod(shape))
    if len(payload) != expected:
        raise ValueError(
            f"height-map payload is {len(payload)} bytes, header says "
            f"{expected} (shape {shape})"
        )
    values = np.frombuffer(payload, dtype="<f4", offset=8 + hlen).reshape(shape)
    pull = header.get("pull")
    return header, HeightMap(
        values.astype(np.float32),
        pull=dict(pull) if isinstance(pull, dict) else None,
        seam="wall" if header.get("seam") == "wall" else "strict",
    )


# ---------------------------------------------------------------------------
# loaders: array, PNG
# ---------------------------------------------------------------------------


def hmap_array(
    values: NDArray[np.floating[Any]],
    *,
    seam: str = "check",
    blend_width: int = 0,
) -> HeightMap:
    """A float ``(nu, nv)`` array → :class:`HeightMap`.

    ``seam="check"`` (default) refuses a non-periodic map under the
    strict tolerance; ``seam="wall"`` additionally permits a wrap step
    within 1.5× the map's steepest interior column step (a legitimately
    periodic hard-edged map whose feature wall lands on the wrap);
    ``seam="blend"`` cross-fades ``blend_width`` columns at each u-edge
    first (default a 32nd of ``nu``); ``seam="raw"`` stores whatever it is
    given — the *leaf* still refuses a jumping edge, so raw is only for
    callers constructing their own continuity."""
    m = np.asarray(values, dtype=np.float64)
    if m.ndim != 2 or min(m.shape) < 2:
        raise ValueError(
            f"a height map must be (nu, nv) with both axes >= 2, got shape {m.shape}"
        )
    if not np.all(np.isfinite(m)):
        raise ValueError("a height map must be finite everywhere")
    if seam == "blend":
        m = blend_seam(m, blend_width or max(2, m.shape[0] // 32))
        return HeightMap(m)
    if seam == "check":
        check_seam(m)
        return HeightMap(m)
    if seam == "wall":
        return HeightMap(m, seam="wall")
    if seam != "raw":
        raise ValueError(
            f"seam must be 'check' | 'wall' | 'blend' | 'raw', got {seam!r}"
        )
    return HeightMap(m)


def hmap_png(png: bytes, *, seam: str = "check", blend_width: int = 0) -> HeightMap:
    """A grayscale PNG (bytes) → :class:`HeightMap`: white = high.

    The image's columns become ``u``, its rows become ``v`` with the TOP
    row at ``v = 1``. 16-bit grays normalize by 65535, 8-bit by 255,
    colour images are converted to luminance first. The seam rule
    applies: wrap a panorama or pass ``seam="blend"``."""
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    arr = np.asarray(img)
    if arr.ndim == 3:
        arr = np.asarray(img.convert("L"), dtype=np.uint8)
    if arr.dtype == np.uint16:
        m = arr.astype(np.float64) / 65535.0
    elif arr.dtype == np.uint8:
        m = arr.astype(np.float64) / 255.0
    else:
        m = arr.astype(np.float64)
    # image rows run top→down; v runs bottom→top, and the map is (u, v)
    m = m[::-1, :].T
    return hmap_array(m, seam=seam, blend_width=blend_width)


# ---------------------------------------------------------------------------
# analytic generators: ribs, knurl — periodic by construction
# ---------------------------------------------------------------------------


def _u_axis(nu: int) -> NDArray[np.float64]:
    return (np.arange(nu, dtype=np.float64) + 0.5) / nu


def _tri(x: NDArray[np.float64]) -> NDArray[np.float64]:
    """Triangle wave 0..1, period 1."""
    return 1.0 - np.abs(2.0 * np.mod(x, 1.0) - 1.0)


def hmap_ribs(
    nu: int,
    nv: int,
    *,
    count: int,
    duty: float = 0.5,
    flank: float = 0.25,
) -> HeightMap:
    """``count`` axial ribs (parallel to z), height 1 on the flats.

    ``duty`` is the flat-top width as a fraction of the tooth pitch,
    ``flank`` the linear runout on each side (same units) — a symmetric
    trapezoid. ``flank = 0`` gives square ribs."""
    if count < 1:
        raise ValueError(f"ribs: count must be >= 1, got {count}")
    if not (0.0 < duty <= 1.0):
        raise ValueError(f"ribs: duty must be in (0, 1], got {duty}")
    if not (0.0 < flank <= 0.5):
        raise ValueError(f"ribs: flank must be in (0, 0.5], got {flank}")
    phase = np.abs(np.mod(count * _u_axis(nu), 1.0) - 0.5)  # 0..0.5, centre 0
    h = np.clip((0.5 * duty + flank - phase) / flank, 0.0, 1.0)
    return HeightMap(np.broadcast_to(h[:, None], (nu, nv)).copy())


def hmap_knurl(
    nu: int,
    nv: int,
    *,
    count: int,
    mode: str = "diamond",
    helix: float = 0.0,
) -> HeightMap:
    """A knurl: ``count`` teeth per tile.

    ``mode="straight"`` — axial ridges; ``"helical"`` — one helical
    family, ``helix`` turns per unit ``v``; ``"diamond"`` — two opposed
    helical families at ±``helix`` (default 0.5 when unset) whose product
    gives the classic pyramid knurl."""
    if count < 1:
        raise ValueError(f"knurl: count must be >= 1, got {count}")
    u = _u_axis(nu)[:, None]
    v = (np.arange(nv, dtype=np.float64) / (nv - 1))[None, :]
    if mode == "straight":
        h = np.broadcast_to(_tri(count * u), (nu, nv)).copy()
    elif mode == "helical":
        h = _tri(count * u + helix * v)
    elif mode == "diamond":
        hv = helix if helix else 0.5
        h = _tri(count * u + hv * v) * _tri(count * u - hv * v)
    else:
        raise ValueError(
            f"knurl mode must be 'straight' | 'helical' | 'diamond', got {mode!r}"
        )
    return HeightMap(np.ascontiguousarray(h))


# ---------------------------------------------------------------------------
# pull-aware flanks (spec §Decisions Q1/Q2)
# ---------------------------------------------------------------------------


def slide_delta(theta: NDArray[np.float64], k: int, phi: float) -> NDArray[np.float64]:
    """Signed angle from the nearest slide pull, in ``(-π/k, π/k]``;
    positive toward the nearer join."""
    period = 2.0 * math.pi / k
    return (theta - phi + 0.5 * period) % period - 0.5 * period


def flank_tan_beta(
    delta: NDArray[np.float64], cos_psi: NDArray[np.float64], alpha: float
) -> NDArray[np.float64]:
    """``tan β_max`` at each point: the most tilt the flank may take.

    Solves ``n·p = cos β cos θ − sin β sin θ cos ψ = sin α`` for ``β``:
    with ``R = sqrt(cos²θ + sin²θ cos²ψ)`` the left side is
    ``R·cos(β + γ)``, ``γ = atan2(sin θ cos ψ, cos θ)``, so
    ``β_max = arccos(sin α / R) − γ``. ``R < sin α`` (inside the join's
    dead zone on a join-facing wall) means no tilt releases: the flank
    must lie flat — ``tan β_max = 0`` — and the feature fades out there.

    A centre-facing wall may pass vertical (``β_max = 90° − α + θ > 90°``
    is an overhang the slide still releases), but a height map cannot
    represent an overhang: its steepest wall is vertical. ``β_max ≥ 90°``
    therefore means *no slope limit* — ``tan β_max = +∞`` — never the
    negative tangent of an obtuse angle, which would read as a negative
    slope budget and drag the relaxed map below its base.
    """
    th = np.abs(delta)
    ct, st = np.cos(th), np.sin(th)
    cp = np.clip(cos_psi, -1.0, 1.0)
    r = np.hypot(ct, st * cp)
    sa = math.sin(alpha)
    beta = np.where(
        r >= sa,
        np.arccos(np.clip(sa / np.where(r > 0.0, r, 1.0), -1.0, 1.0))
        - np.arctan2(st * cp, ct),
        0.0,
    )
    beta = np.clip(beta, 0.0, None)
    with np.errstate(over="ignore"):
        return np.where(beta >= 0.5 * math.pi, np.inf, np.tan(beta))


def _wrap_shift_range(period: float, lo: float, hi: float) -> range:
    """The ``±P`` copy shifts a segment set spanning ``[lo, hi]`` needs so
    every pixel in ``[0, P)`` sees all copies that can touch it."""
    n0 = -math.ceil(hi / period)
    n1 = math.ceil((period - lo) / period)
    return range(n0, n1 + 1)


def _seg_dist_foot(
    px: NDArray[np.float64],
    py: NDArray[np.float64],
    a: NDArray[np.float64],
    b: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Distance and foot point of every ``(px, py)`` to segment ``ab``
    (arrays broadcast together) → ``(d, fx, fy)``."""
    ab = b - a
    denom = float(ab @ ab)
    if denom <= 0.0:
        return (
            np.hypot(px - a[0], py - a[1]),
            np.broadcast_to(a[0], px.shape),
            np.broadcast_to(a[1], px.shape),
        )
    t = np.clip(((px - a[0]) * ab[0] + (py - a[1]) * ab[1]) / denom, 0.0, 1.0)
    fx = a[0] + t * ab[0]
    fy = a[1] + t * ab[1]
    return np.hypot(px - fx, py - fy), fx, fy


def _flank_raster(
    nu: int,
    nv: int,
    contours: list[NDArray[np.float64]],
    strokes: list[tuple[NDArray[np.float64], float]],
    *,
    period: float,
    height: float,
    depth: float,
    sign: float,
    repeat: int,
    twist: float,
    k: int,
    alpha: float,
    phi: float,
    relax: bool = True,
) -> NDArray[np.float64]:
    """The upper envelope ``h(u, v)`` for plateau contours and stroked
    centre-lines under the pull-aware flank bound — the core the
    sketch/text generators share.

    ``contours``/``strokes`` are in PHYSICAL tile coordinates ``(x, y)``:
    ``x`` = arc length within the tile (period ``period``), ``y`` = axial.
    Even-odd parity decides inside; a plateau pixel's flank slope is
    ``tan β`` from its nearest boundary's lean direction — see the module
    docstring. For a pixel at signed pull angle ``δ``, the
    toward-the-join tangent is ``sign(δ)·x̂``; an emboss wall's normal
    leans outward (``foot − p``), a deboss wall's into the groove — so
    ``cos ψ = sign·sign(δ)·l̂_x`` flips the tight side between them.
    """
    xs = (np.arange(nu, dtype=np.float64) + 0.5) * (period / nu)
    ys = np.arange(nv, dtype=np.float64) * (height / (nv - 1))
    px = np.repeat(xs[:, None], nv, axis=1)
    py = np.repeat(ys[None, :], nu, axis=0)
    u = px / period  # (i+0.5)/nu
    v = py / height  # j/(nv-1)
    theta_cyl = (2.0 * math.pi * u - twist * v) / repeat
    delta = slide_delta(theta_cyl, k, phi)
    toward_join = np.sign(delta)  # 0 at the pull centre: treated as +1
    toward_join[toward_join == 0.0] = 1.0

    out = np.zeros((nu, nv), dtype=np.float64)
    # The join dead-zone cap: a flat plateau top at |δ| > 90°−α is itself
    # an undercut (its normal is radial), so the pattern must be ~0
    # across the whole dead zone and can climb out of it only through
    # join-facing flanks that shallow to 0 at the pole. The reachable
    # height is the allowed-slope budget inward of the pole:
    #   hcap(θ) = ρ·∫_θ^{90−α} tan(90°−α−θ') dθ' / depth
    #           = −ρ·ln cos(90°−α−θ) / depth   (θ < 90°−α)
    #           = 0                            (θ ≥ 90°−α)
    # The fade surface itself tilts at exactly the join-facing bound, so
    # it is legal. For k = 3 the band sits inside the pole-free region
    # (θ ≤ 60° < 90°−α) and the cap is inert.
    th = np.abs(delta)
    pole = math.pi / 2 - alpha
    r_arc = period / (2.0 * math.pi) * repeat  # ρ the arc coordinate spans
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        hcap = np.where(
            th < pole,
            -r_arc * np.log(np.cos(pole - th)) / depth,
            0.0,
        )

    # feature set 1: the contour union — inside by even-odd parity, flank
    # rise from the nearest boundary segment, whose outward lean is
    # p → foot (the wall faces out of the interior)
    inside = np.zeros((nu, nv), dtype=bool)
    d_in = np.full((nu, nv), np.inf)
    lean_x = np.zeros((nu, nv), dtype=np.float64)
    for ring in contours:
        if len(ring) < 2:
            continue
        lo, hi = float(ring[:, 0].min()), float(ring[:, 0].max())
        for s_i in _wrap_shift_range(period, lo, hi):
            s = s_i * period
            n = len(ring)
            for i in range(n):
                a = np.array([ring[i, 0] + s, ring[i, 1]])
                b = np.array([ring[(i + 1) % n, 0] + s, ring[(i + 1) % n, 1]])
                if a[1] != b[1]:
                    straddle = (a[1] > py) != (b[1] > py)
                    if np.any(straddle):
                        xc = a[0] + (py - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                        inside ^= straddle & (xc > px)
                d, fx, _fy = _seg_dist_foot(px, py, a, b)
                nearer = d < d_in
                d_in = np.where(nearer, d, d_in)
                lean_x = np.where(nearer, fx - px, lean_x)
    _accumulate(out, inside, d_in, lean_x, delta, toward_join, sign, alpha, depth, hcap)

    # feature set 2: stroked centre-lines — inside the band d_c < w/2;
    # the wall is the offset line, leaning away from the centreline
    for pts, width in strokes:
        if len(pts) < 2 or not (width > 0.0):
            continue
        lo, hi = float(pts[:, 0].min()), float(pts[:, 0].max())
        half = 0.5 * width
        s_in = np.zeros((nu, nv), dtype=bool)
        s_d = np.zeros((nu, nv), dtype=np.float64)
        s_lx = np.zeros((nu, nv), dtype=np.float64)
        for s_i in _wrap_shift_range(period, lo, hi):
            s = s_i * period
            for i in range(len(pts) - 1):
                a = np.array([pts[i, 0] + s, pts[i, 1]])
                b = np.array([pts[i + 1, 0] + s, pts[i + 1, 1]])
                dc, cx, _cy = _seg_dist_foot(px, py, a, b)
                band = half - dc  # inward distance to the offset boundary
                deeper = band > s_d
                s_in |= dc < half
                s_d = np.where(deeper, band, s_d)
                s_lx = np.where(deeper & (dc > 0.0), px - cx, s_lx)
        _accumulate(out, s_in, s_d, s_lx, delta, toward_join, sign, alpha, depth, hcap)

    if relax:
        _relax_flanks(
            out,
            r=period * repeat / (2.0 * math.pi),
            height=height,
            depth=depth,
            sign=sign,
            repeat=repeat,
            twist=twist,
            k=k,
            alpha=alpha,
            phi=phi,
        )
    return out


def _pull_grids(
    nu: int, nv: int, repeat: int, twist: float, k: int, phi: float
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Signed pull angle ``δ`` at every pixel's u-edge midpoint (``(nu,
    nv)``, edge ``i`` joins pixels ``i`` and ``i+1`` periodic) and v-edge
    midpoint (``(nu, nv-1)``). Edges are the surface samples the slope
    bound acts on — evaluating ``δ`` at the midpoint, not at a pixel
    endpoint, is what makes the discrete bound honest (Fable s1 review
    item 3)."""
    iu = (np.arange(nu) + 0.5) / nu
    jv = np.arange(nv) / (nv - 1)
    theta = (2.0 * math.pi * iu[:, None] - twist * jv[None, :]) / repeat
    # u-edge midpoint: exactly half a u-step past pixel i's centre —
    # valid across the seam too since θ is linear in u within a v row
    d_u = slide_delta(theta + math.pi / (nu * repeat), k, phi)
    # v-edge midpoint between rows j and j+1 (θ falls with v under twist)
    d_v = slide_delta(theta[:, :-1] - twist / (2.0 * (nv - 1) * repeat), k, phi)
    return theta, d_u, d_v


def _central_slopes(
    h: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Per-unit-``u`` / per-unit-``v`` central differences at pixel
    centres — ``u`` periodic, ``v`` one-sided at the caps."""
    nu, nv = h.shape
    h_u = (np.roll(h, -1, axis=0) - np.roll(h, 1, axis=0)) * 0.5 * nu
    h_v = np.empty_like(h)
    if nv > 2:
        h_v[:, 1:-1] = (h[:, 2:] - h[:, :-2]) * 0.5 * (nv - 1)
    h_v[:, 0] = (h[:, 1] - h[:, 0]) * (nv - 1)
    h_v[:, -1] = (h[:, -1] - h[:, -2]) * (nv - 1)
    return h_u, h_v


def _relax_flanks(
    out: NDArray[np.float64],
    *,
    r: float,
    height: float,
    depth: float,
    sign: float,
    repeat: int,
    twist: float,
    k: int,
    alpha: float,
    phi: float,
    iters: int = 96,
    margin: float = 1e-3,
) -> None:
    """Discrete slope-bound relaxation, in place (Fable s1 review items
    1–3, rewritten).

    The cone envelope ``tan β·d`` overstates reachable height wherever
    ``tan β`` drops fast (near the join pole its derivative diverges), so
    a wall built from it can exceed the pull bound *between* samples —
    the relax is load-bearing, not cosmetic. Each map edge is bound to
    the **combined-normal** condition it actually has to satisfy:
    ``n̂·p = (cos δ + g_t sin δ)/√(1+g_t²+g_z²) ≥ sin α`` at the edge
    midpoint's own ``δ``, with ``g_t`` the edge's tangential surface
    slope and ``g_z`` the axial one. That is the total-tilt bound
    ``g_t² + g_z² ≤ tan²β(δ, ψ)`` of :func:`flank_tan_beta` in the edge's
    current lean direction ``ψ`` (lagged one sweep so it converges with
    the map), with the twist coupling folded in exactly:
    ``g_z = m·g_t + c`` where ``m = r·twist/(repeat·height)`` and ``c``
    is the lagged axial slope — a clean quadratic interval for ``g_t``,
    no separable u/v approximation and no missing ``/depth`` (the slice-1
    bug: it bounded the *normalized* map by *physical* steps, crushing
    plateaus to a fraction of depth; this version solves in physical
    slope and converts).

    Alternating forward/backward fast-sweeps propagate the caps inward;
    a few dozen passes converge since the updates only ever *lower* the
    map. ``margin`` (in ``n̂·p`` units) is added to ``sin α`` inside the
    bound so the lagged-``ψ`` iteration lands comfortably on the safe
    side. An edge whose orthogonal slope alone already breaks the bound
    (``D < 0`` — the other axis's sweep owns that fix) is flattened to
    ``Δh = 0`` rather than left violating.
    """
    nu, nv = out.shape
    sa_eff = math.sin(alpha) + margin
    a_eff = math.asin(min(1.0, sa_eff))
    _theta, d_u, d_v = _pull_grids(nu, nv, repeat, twist, k, phi)
    # g_t = a1·Δh on a u-edge; g_z = m·g_t + c (twist feedback) there;
    # g_z = a2·Δv + c2 on a v-edge with c2 the lagged twist part.
    a1 = sign * depth * nu * repeat / (2.0 * math.pi * r)
    m = r * twist / (repeat * height)
    a2 = sign * depth * (nv - 1) / height
    c_tw = sign * depth * twist / (2.0 * math.pi * height)
    sign_du = np.sign(d_u)
    sign_dv = np.sign(d_v)
    for _ in range(iters):
        prev = out.copy()
        h_u, h_v = _central_slopes(out)
        # ---- u-edges: edge i joins pixel i and i+1 (periodic) ----
        dh = np.roll(out, -1, axis=0) - out
        g_t = a1 * dh
        c = sign * depth * 0.5 * (h_v + np.roll(h_v, -1, axis=0)) / height
        g_z = m * g_t + c
        gabs = np.maximum(np.hypot(g_t, g_z), 1e-300)
        cos_psi = np.clip(-g_t * sign_du / gabs, -1.0, 1.0)
        t = flank_tan_beta(d_u, cos_psi, a_eff)
        # (1+m²)g² + 2mc·g + c² − t² ≤ 0 → g interval between the roots;
        # t = +inf (centre-facing, free) means no bound; t finite with a
        # negative discriminant means the edge is infeasible at any slope
        # (the other axis's sweep owns the fix) → flatten it.
        aa = 1.0 + m * m
        with np.errstate(invalid="ignore", over="ignore"):
            disc = aa * t * t - c * c
        bounded = np.isfinite(t)
        infeasible = bounded & (disc < 0.0)
        root = np.sqrt(np.maximum(disc, 0.0))
        lo_g = (-m * c - root) / aa
        hi_g = (-m * c + root) / aa
        if a1 > 0:
            lo_dh, hi_dh = lo_g / a1, hi_g / a1
        else:
            lo_dh, hi_dh = hi_g / a1, lo_g / a1
        lo_dh = np.where(bounded, lo_dh, -np.inf)
        hi_dh = np.where(bounded, hi_dh, np.inf)
        lo_dh = np.where(infeasible, 0.0, lo_dh)
        hi_dh = np.where(infeasible, 0.0, hi_dh)
        for i in range(nu):
            ir = (i + 1) % nu
            np.minimum(out[ir], out[i] + hi_dh[i], out=out[ir])
        for i in range(nu - 1, -1, -1):
            ir = (i + 1) % nu
            np.minimum(out[i], out[ir] - lo_dh[i], out=out[i])
        # ---- v-edges: edge j joins rows j and j+1 ----
        dv = out[:, 1:] - out[:, :-1]
        h_u_e = 0.5 * (h_u[:, :-1] + h_u[:, 1:])
        w_t = sign * depth * h_u_e * repeat / (2.0 * math.pi * r)
        c2 = c_tw * h_u_e
        g_zv = a2 * dv + c2
        gabsv = np.maximum(np.hypot(w_t, g_zv), 1e-300)
        cos_psi_v = np.clip(-w_t * sign_dv / gabsv, -1.0, 1.0)
        tv = flank_tan_beta(d_v, cos_psi_v, a_eff)
        # w_t² + (a2·dv + c2)² ≤ tv² → dv ∈ [(-c2 ± √(tv²−w_t²))/a2];
        # tv = +inf means no bound, tv² < w_t² means the tangential slope
        # alone already breaks it → flatten and let the u-sweep fix w_t.
        disc_v = tv * tv - w_t * w_t
        bounded_v = np.isfinite(tv)
        infeasible_v = bounded_v & (disc_v < 0.0)
        rv = np.sqrt(np.maximum(disc_v, 0.0))
        lo_dv = (-c2 - rv) / a2
        hi_dv = (-c2 + rv) / a2
        if a2 > 0:
            vlo, vhi = lo_dv, hi_dv
        else:
            vlo, vhi = hi_dv, lo_dv
        vlo = np.where(bounded_v, vlo, -np.inf)
        vhi = np.where(bounded_v, vhi, np.inf)
        vlo = np.where(infeasible_v, 0.0, vlo)
        vhi = np.where(infeasible_v, 0.0, vhi)
        for j in range(nv - 1):
            np.minimum(out[:, j + 1], out[:, j] + vhi[:, j], out=out[:, j + 1])
        for j in range(nv - 2, -1, -1):
            np.minimum(out[:, j], out[:, j + 1] - vlo[:, j], out=out[:, j])
        if float(np.max(np.abs(out - prev))) < 1e-12:
            break


def pull_edge_margins(
    h: NDArray[np.floating[Any]],
    *,
    r: float,
    height: float,
    depth: float,
    sign: float,
    repeat: int,
    twist: float,
    k: int,
    phi: float,
    alpha: float,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """``n̂·p − sin α`` on every map edge — the production form of slice
    1's ``_surface_np`` test helper (the Q1 corrected check, evaluated on
    cell edges with forward differences).

    Returns ``(margin_u, margin_v)``: ``margin_u[i, j]`` on the u-edge
    between pixels ``i`` and ``i+1`` (periodic), ``margin_v[i, j]`` on
    the v-edge between rows ``j`` and ``j+1``. Negative means undercut —
    the surface there leans out of its slide's release cone. The
    tangential surface slope is ``g_t = s·depth·h_u·repeat/(2πr)`` on the
    nominal radius (conservative: the true ``ρ`` is larger on emboss
    features, so real slopes are gentler) and the axial
    ``g_z = s·depth·(h_u·twist/(2π) + h_v)/height``.
    """
    m = np.asarray(h, dtype=np.float64)
    nu, nv = m.shape
    _theta, d_u, d_v = _pull_grids(nu, nv, repeat, twist, k, phi)
    sa = math.sin(alpha)
    a1 = sign * depth * nu * repeat / (2.0 * math.pi * r)
    mm = r * twist / (repeat * height)
    a2 = sign * depth * (nv - 1) / height
    c_tw = sign * depth * twist / (2.0 * math.pi * height)
    _h_u, h_v = _central_slopes(m)
    # u-edges (forward difference in u, the edge's own slope)
    dh = np.roll(m, -1, axis=0) - m
    g_t = a1 * dh
    c = sign * depth * 0.5 * (h_v + np.roll(h_v, -1, axis=0)) / height
    g_z = mm * g_t + c
    margin_u = (
        np.cos(d_u) + g_t * np.sin(d_u) - sa * np.sqrt(1.0 + g_t * g_t + g_z * g_z)
    )
    # v-edges (forward difference in v)
    dvv = m[:, 1:] - m[:, :-1]
    h_u_e = 0.5 * (_h_u[:, :-1] + _h_u[:, 1:])
    w_t = sign * depth * h_u_e * repeat / (2.0 * math.pi * r)
    g_zv = a2 * dvv + c_tw * h_u_e
    margin_v = (
        np.cos(d_v) + w_t * np.sin(d_v) - sa * np.sqrt(1.0 + w_t * w_t + g_zv * g_zv)
    )
    return margin_u, margin_v


def _accumulate(
    out: NDArray[np.float64],
    inside: NDArray[np.bool_],
    d_in: NDArray[np.float64],
    lean_x: NDArray[np.float64],
    delta: NDArray[np.float64],
    toward_join: NDArray[np.float64],
    sign: float,
    alpha: float,
    depth: float,
    hcap: NDArray[np.float64],
) -> None:
    """Fold one feature set into ``out`` (in place): pixels get
    ``h = clip(tan β·d_in/depth, 0, 1)`` with ``β`` the pull-aware bound
    at the pixel's own ``δ`` and the boundary's lean — ``cos ψ =
    sign·sign(δ)·l̂_x`` (emboss: the wall leans outward; deboss: the same
    map flipped makes that wall's normal point the other way, so the
    tight side mirrors — the Q1 emboss/deboss swap) — then capped by
    ``hcap``, the join dead-zone fade."""
    live = inside & (d_in > 0.0)
    if not np.any(live):
        return
    lx = np.where(live, lean_x / np.where(d_in > 0.0, d_in, 1.0), 0.0)
    cos_psi = sign * toward_join * lx
    tan_b = flank_tan_beta(delta, cos_psi, alpha)
    # tan_b is +inf where the wall may stand vertical (centre-facing past
    # 90°): d·inf clips to the full plateau; inf·0 off-feature is masked
    with np.errstate(invalid="ignore"):
        rise = np.clip(d_in * tan_b / depth, 0.0, 1.0)
    h = np.where(live & np.isfinite(d_in), rise, 0.0)
    # an inside pixel with no finite boundary distance (the map is all
    # feature) is a full plateau
    h = np.where(live & ~np.isfinite(d_in), 1.0, h)
    np.maximum(out, np.minimum(h, hcap), out=out)


def _contours_to_phys(
    contours: Sequence[Sequence[tuple[float, float]] | Sequence[float] | NDArray],
    period: float,
    height: float,
) -> list[NDArray[np.float64]]:
    """``(u, v)`` rings → physical ``(x = u·period, y = v·height)``."""
    out = []
    for ring in contours:
        r = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
        out.append(np.stack([r[:, 0] * period, r[:, 1] * height], axis=1))
    return out


def hmap_sketch(
    nu: int,
    nv: int,
    contours: Sequence[Sequence[tuple[float, float]] | Sequence[float] | NDArray],
    *,
    radius: float,
    height: float,
    depth: float,
    sign: float = 1.0,
    repeat: int = 1,
    twist: float = 0.0,
    k: int = 3,
    alpha: float = math.radians(1.0),
    phi: float = 0.0,
    strokes: Sequence[tuple[Sequence[Sequence[float]], float]] | None = None,
    relax: bool = True,
) -> HeightMap:
    """Sketch polygons / stroked lines in ``(u, v)`` → a plateau map with
    pull-aware flanks (spec §Model row 3 + §Decisions Q2).

    ``contours`` are closed ``(u, v)`` rings (even-odd fill, so a ring
    inside another is a hole); ``strokes`` are ``(points, width)``
    centre-lines — width in the caller's length unit — each becoming a
    ridge band. ``radius``/``height`` fix the uv → physical conversion,
    ``depth`` the plateau's rendered amplitude. ``k``/``alpha``/``phi``
    describe the intended pull (k slides, draft ``alpha`` radians, pull
    centres at ``phi + 2πj/k``); ``sign`` mirrors the flank assignment
    between emboss (+1) and deboss (−1)."""
    if k not in (2, 3):
        raise ValueError(f"pull layout k must be 2 or 3, got {k}")
    if not (0.0 < alpha < math.pi / 2):
        raise ValueError(f"draft alpha must lie in (0, 90°), got {alpha}")
    period = 2.0 * math.pi * radius / repeat
    rings = _contours_to_phys(contours, period, height)
    strokes_phys = [
        (
            np.stack(
                [
                    np.asarray(pts, dtype=np.float64).reshape(-1, 2)[:, 0] * period,
                    np.asarray(pts, dtype=np.float64).reshape(-1, 2)[:, 1] * height,
                ],
                axis=1,
            ),
            float(width),
        )
        for pts, width in (strokes or [])
    ]
    m = _flank_raster(
        nu,
        nv,
        rings,
        strokes_phys,
        period=period,
        height=height,
        depth=depth,
        sign=sign,
        repeat=repeat,
        twist=twist,
        k=k,
        alpha=alpha,
        phi=phi,
        relax=relax,
    )
    return HeightMap(
        m,
        pull={"sign": sign, "k": k, "alpha": alpha, "phi": phi},
        seam="wall",
    )


def hmap_text(
    text: str,
    nu: int,
    nv: int,
    *,
    at: tuple[float, float] = (0.0, 0.0),
    size: float,
    radius: float,
    height: float,
    depth: float,
    sign: float = 1.0,
    repeat: int = 1,
    twist: float = 0.0,
    k: int = 3,
    alpha: float = math.radians(1.0),
    phi: float = 0.0,
    font: str | None = None,
) -> HeightMap:
    """A text relief: matplotlib ``TextPath`` outlines →
    :func:`hmap_sketch` (Q3 — matplotlib is already a dep).

    ``size`` is the text's cap-to-baseline box height in the caller's
    length unit; ``at`` the ``(u, v)`` of its lower-left corner. Glyphs
    come back as closed rings (holes wound opposite — even-odd keeps
    them), then get the same pull-aware flanks as a sketch."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath

    prop = FontProperties(family=font) if font else None
    path = TextPath((0.0, 0.0), text, size=1.0, prop=prop)
    ext = path.get_extents()
    if not (ext.height > 0.0 and ext.width > 0.0):
        raise ValueError(f"text {text!r} produced no outline")
    period = 2.0 * math.pi * radius / repeat
    scale = size / ext.height
    if ext.width * scale > period:
        raise ValueError(
            f"text {text!r} is {ext.width * scale:g} wide at size {size:g} — "
            f"wider than one tile ({period:g}); shrink size or raise repeat"
        )
    x0, y0 = at[0] * period, at[1] * height
    rings: list[NDArray[np.float64]] = []
    for poly in path.to_polygons():
        pts = np.asarray(poly, dtype=np.float64)
        rings.append(
            np.stack(
                [
                    (pts[:, 0] - ext.xmin) * scale + x0,
                    (pts[:, 1] - ext.ymin) * scale + y0,
                ],
                axis=1,
            )
        )
    m = _flank_raster(
        nu,
        nv,
        rings,
        [],
        period=period,
        height=height,
        depth=depth,
        sign=sign,
        repeat=repeat,
        twist=twist,
        k=k,
        alpha=alpha,
        phi=phi,
    )
    return HeightMap(
        m,
        pull={"sign": sign, "k": k, "alpha": alpha, "phi": phi},
        seam="wall",
    )
