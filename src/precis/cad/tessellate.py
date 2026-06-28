"""Analytic primitive → triangle mesh (ADR 0041 §10, export only).

The probe / relate layers never mesh — they work on the exact analytic
IR. *Export* is the one place we tessellate, and because we own the
geometry we can emit clean, watertight, outward-oriented meshes directly
(numpy only — no heavy dependency). The mesh backends
(:mod:`precis.cad.export`) feed these into a CSG kernel (manifold3d) or a
B-rep kernel (OpenCASCADE) to fold the booleans.

Conventions match :mod:`precis.cad.primitives` exactly so the exported
solid is the same geometry the agent probed:

- ``box`` centred in x/y, base at ``z=0``;
- ``cyl``/``cone``/``tcone`` (circular frustum) axis ``+z``, base at
  ``z=0``, radius ``r(z)=rb+(rt-rb)·z/h``;
- ``hex``/``ngon``/``frustum``/``pyramid`` regular polygon, first vertex
  at angle 0 (matching ``_ngon``);
- ``sphere`` centred at the origin; ``torus`` major radius ``R`` about
  ``+z``, minor ``r``.

Each builder returns ``(verts, tris)`` — an ``(N, 3) float64`` vertex
array and an ``(M, 3) int`` triangle array — already oriented so every
face normal points outward (enforced by :func:`_orient_outward` via the
mesh's signed volume, so a hand-winding slip can never ship an inverted
solid).
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from precis.cad.dsl import ShapeSpec, parse
from precis.cad.scene import NodeSpec, _node_xform, _pattern_transforms
from precis.cad.vec import Transform

#: Segment count for curved primitives (cyl/cone/sphere/torus). Matches the
#: OpenSCAD ``$fn`` used by the text export so the two routes agree.
_FN = 64
#: Latitude bands for the sphere (longitude uses the full ``_FN``).
_FN_LAT = 32
_EPS = 1e-9

Mesh = tuple[NDArray[np.float64], NDArray[np.int64]]


class TessellationError(ValueError):
    """A shape that has no finite mesh (e.g. the unbounded chamfer half-space)."""


# ---------------------------------------------------------------------------
# orientation
# ---------------------------------------------------------------------------


def _signed_volume(verts: NDArray[np.float64], tris: NDArray[np.int64]) -> float:
    """Six times the signed volume — positive iff faces wind outward (CCW)."""
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum())


def _orient_outward(verts: NDArray[np.float64], tris: NDArray[np.int64]) -> Mesh:
    """Flip winding if the mesh came out inside-out (signed volume < 0)."""
    if _signed_volume(verts, tris) < 0.0:
        tris = tris[:, ::-1].copy()
    return verts, tris


# ---------------------------------------------------------------------------
# ring / cap helpers
# ---------------------------------------------------------------------------


def _ngon_xy(n: int, r: float) -> list[tuple[float, float]]:
    return [
        (r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def _fan(indices: list[int]) -> list[tuple[int, int, int]]:
    """Triangulate a convex ring (vertex indices) as a fan from the first."""
    return [
        (indices[0], indices[i], indices[i + 1]) for i in range(1, len(indices) - 1)
    ]


def _extrude(
    bottom_xy: list[tuple[float, float]],
    top_xy: list[tuple[float, float]],
    h: float,
) -> Mesh:
    """Closed solid between two equal-length convex rings at z=0 / z=h."""
    n = len(bottom_xy)
    verts = np.array(
        [(x, y, 0.0) for x, y in bottom_xy] + [(x, y, h) for x, y in top_xy],
        dtype=np.float64,
    )
    bot = list(range(n))
    top = list(range(n, 2 * n))
    tris: list[tuple[int, int, int]] = []
    # Caps wind oppositely so the closed surface is consistently oriented
    # (a same-wound pair is non-orientable → non-manifold); _orient_outward
    # then fixes the global sign.
    tris += [(a, c, b) for (a, b, c) in _fan(bot)]  # bottom cap (reversed)
    tris += _fan(top)  # top cap
    for i in range(n):  # sides
        j = (i + 1) % n
        tris.append((bot[i], bot[j], top[j]))
        tris.append((bot[i], top[j], top[i]))
    return _orient_outward(verts, np.array(tris, dtype=np.int64))


def _cone(bottom_xy: list[tuple[float, float]], h: float) -> Mesh:
    """Closed solid between a convex base ring at z=0 and an apex at (0,0,h)."""
    n = len(bottom_xy)
    verts = np.array(
        [(x, y, 0.0) for x, y in bottom_xy] + [(0.0, 0.0, h)], dtype=np.float64
    )
    apex = n
    # base cap reversed so it's consistent with the apex-fan sides (see _extrude)
    tris: list[tuple[int, int, int]] = [(a, c, b) for (a, b, c) in _fan(list(range(n)))]
    for i in range(n):
        tris.append((i, (i + 1) % n, apex))
    return _orient_outward(verts, np.array(tris, dtype=np.int64))


def _circular(rb: float, rt: float, h: float, *, seg: int = _FN) -> Mesh:
    if rt <= _EPS:
        return _cone(_ngon_xy(seg, rb), h)
    if rb <= _EPS:  # inverted cone — apex at the base
        verts_top = _ngon_xy(seg, rt)
        v, t = _cone(verts_top, -h)
        v = v.copy()
        v[:, 2] += h  # shift so the base sits at z=h, apex at z=0
        return _orient_outward(v, t)
    return _extrude(_ngon_xy(seg, rb), _ngon_xy(seg, rt), h)


def _sphere(r: float) -> Mesh:
    nlon, nlat = _FN, _FN_LAT
    verts: list[tuple[float, float, float]] = [(0.0, 0.0, r)]  # north pole
    for i in range(1, nlat):
        phi = math.pi * i / nlat
        z = r * math.cos(phi)
        rho = r * math.sin(phi)
        for j in range(nlon):
            th = 2 * math.pi * j / nlon
            verts.append((rho * math.cos(th), rho * math.sin(th), z))
    south = len(verts)
    verts.append((0.0, 0.0, -r))  # south pole
    tris: list[tuple[int, int, int]] = []

    def ring(i: int, j: int) -> int:  # vertex index in latitude band i (1-based)
        return 1 + (i - 1) * nlon + (j % nlon)

    for j in range(nlon):  # north cap
        tris.append((0, ring(1, j), ring(1, j + 1)))
    for i in range(1, nlat - 1):  # bands
        for j in range(nlon):
            a, b = ring(i, j), ring(i, j + 1)
            c, d = ring(i + 1, j), ring(i + 1, j + 1)
            tris.append((a, c, d))
            tris.append((a, d, b))
    for j in range(nlon):  # south cap
        tris.append((south, ring(nlat - 1, j + 1), ring(nlat - 1, j)))
    return _orient_outward(np.array(verts, dtype=np.float64), np.array(tris, np.int64))


def _torus(R: float, r: float) -> Mesh:
    nu, nv = _FN, _FN
    verts: list[tuple[float, float, float]] = []
    for i in range(nu):
        u = 2 * math.pi * i / nu
        for j in range(nv):
            v = 2 * math.pi * j / nv
            rho = R + r * math.cos(v)
            verts.append((rho * math.cos(u), rho * math.sin(u), r * math.sin(v)))

    def idx(i: int, j: int) -> int:
        return (i % nu) * nv + (j % nv)

    tris: list[tuple[int, int, int]] = []
    for i in range(nu):
        for j in range(nv):
            a, b = idx(i, j), idx(i + 1, j)
            c, d = idx(i, j + 1), idx(i + 1, j + 1)
            tris.append((a, b, d))
            tris.append((a, d, c))
    return _orient_outward(np.array(verts, dtype=np.float64), np.array(tris, np.int64))


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def mesh_shape(spec: ShapeSpec) -> Mesh:
    """Tessellate a parsed :class:`ShapeSpec` in its local frame."""
    a, p = spec.alias, spec.params
    if a == "box":
        hw, hd = p["w"] / 2.0, p["d"] / 2.0
        ring = [(-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)]
        return _extrude(ring, list(ring), p["h"])
    if a == "cyl":
        return _circular(p["r"], p["r"], p["h"])
    if a == "cone":
        return _circular(p["r"], 0.0, p["h"])
    if a == "tcone":
        return _circular(p["rb"], p["rt"], p["h"])
    if a == "sphere":
        return _sphere(p["r"])
    if a == "torus":
        return _torus(p["R"], p["r"])
    if a == "hex":
        ring = _ngon_xy(6, p["r"])
        return _extrude(ring, list(ring), p["h"])
    if a == "ngon":
        ring = _ngon_xy(int(p["n"]), p["r"])
        return _extrude(ring, list(ring), p["h"])
    if a == "frustum":
        return _extrude(
            _ngon_xy(int(p["n"]), p["rb"]), _ngon_xy(int(p["n"]), p["rt"]), p["h"]
        )
    if a == "pyramid":
        return _cone(_ngon_xy(int(p["n"]), p["r"]), p["h"])
    raise TessellationError(
        f"shape {a!r} has no finite mesh (chamfer is an unbounded half-space)"
    )


def mesh_config(config: str) -> Mesh:
    """Parse a ``config`` string and tessellate it."""
    return mesh_shape(parse(config))


def _apply(xf: Transform, verts: NDArray[np.float64]) -> NDArray[np.float64]:
    """Map local vertices to world coords (rigid, so winding is preserved)."""
    return (xf.R @ verts.T).T + xf.t


def node_meshes(node: NodeSpec) -> list[Mesh]:
    """World-space meshes for a node — one per pattern instance (or one)."""
    base = mesh_shape(parse(node.config))
    v, t = base
    if node.pattern is not None:
        return [(_apply(xf, v), t) for xf in _pattern_transforms(node)]
    return [(_apply(_node_xform(node.loc, node.rot), v), t)]
