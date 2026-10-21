"""Batched differentiable core distances for the slice-1 prototype
(spec §Shapes and distance). Every function is torch code imported
lazily by its callers — the host venv has no torch.

Cores are segments (a point is a degenerate segment), oriented boxes and
the board outline (convex). The kernels mirror ``clearance.py:116-201``'s
forms — segment–segment, not point–point, so wires cannot slip between
samples — and stay differentiable away from kinks, which is all the
relaxation needs. ``gap(a, b) = dist(core_a, core_b) − r_a − r_b −
required(a, b, layer)``; gap ≥ 0 is legal.
"""

from __future__ import annotations

_EPS = 1e-12


def cross2(u, v):
    """``u x v`` for (..., 2) tensors — the orientation predicate every
    closed form here is built from."""
    return u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]


def point_seg_dist(p, a, b):
    """Distance from points ``p`` (..., 2) to segments ``ab`` (..., 2)."""
    d = b - a
    den = (d * d).sum(-1).clamp_min(_EPS)
    t = (((p - a) * d).sum(-1) / den).clamp(0.0, 1.0)
    proj = a + t[..., None] * d
    return (p - proj).norm(dim=-1)


def seg_seg_cross(a0, a1, b0, b1):
    """``ab`` intersects ``cd`` (endpoint contact counts — touching
    copper is a violation too, and the swept check wants the strictest
    reading). Bool tensor."""
    o1 = cross2(a1 - a0, b0 - a0)
    o2 = cross2(a1 - a0, b1 - a0)
    o3 = cross2(b1 - b0, a0 - b0)
    o4 = cross2(b1 - b0, a1 - b0)
    return (o1 * o2 <= 0.0) & (o3 * o4 <= 0.0)


def seg_seg_dist(a0, a1, b0, b1):
    """Segment–segment distance, closed form: 0 on intersection else the
    min of the four endpoint-to-segment distances."""
    import torch

    d = torch.minimum(
        torch.minimum(point_seg_dist(a0, b0, b1), point_seg_dist(a1, b0, b1)),
        torch.minimum(point_seg_dist(b0, a0, a1), point_seg_dist(b1, a0, a1)),
    )
    return torch.where(seg_seg_cross(a0, a1, b0, b1), torch.zeros_like(d), d)


def _to_box_frame(p, c, cs):
    """Points ``p`` (..., 2) in the oriented box's local frame:
    ``cs`` is (cos θ, sin θ) of the box's x axis, ``c`` its centre."""
    d = p - c
    x = d[..., 0] * cs[..., 0] + d[..., 1] * cs[..., 1]
    y = -d[..., 0] * cs[..., 1] + d[..., 1] * cs[..., 0]
    import torch

    return torch.stack((x, y), dim=-1)


def _box_edges(hw):
    """The four edges of an axis-aligned box with half extents ``hw``
    (..., 2) as (..., 4, 2, 2) endpoint pairs."""
    import torch

    hx, hy = hw[..., 0], hw[..., 1]
    c0 = torch.stack((-hx, -hy), dim=-1)
    c1 = torch.stack((hx, -hy), dim=-1)
    c2 = torch.stack((hx, hy), dim=-1)
    c3 = torch.stack((-hx, hy), dim=-1)
    return torch.stack(
        (
            torch.stack((c0, c1), dim=-2),
            torch.stack((c1, c2), dim=-2),
            torch.stack((c2, c3), dim=-2),
            torch.stack((c3, c0), dim=-2),
        ),
        dim=-3,
    )


def seg_box_dist(a0, a1, c, hw, cs):
    """Distance from segment ``a0a1`` to a filled oriented box: 0 when
    the segment enters the box (an endpoint inside, or an edge crossed),
    else the min distance to its four edges. Broadcasting is the
    caller's job (align the leading dims)."""
    import torch

    p0 = _to_box_frame(a0, c, cs)
    p1 = _to_box_frame(a1, c, cs)
    inside = ((p0.abs() <= hw).all(-1)) | ((p1.abs() <= hw).all(-1))
    edges = _box_edges(hw)
    d = (
        torch.stack(
            [
                seg_seg_dist(p0, p1, edges[..., i, 0, :], edges[..., i, 1, :])
                for i in range(4)
            ],
            dim=-1,
        )
        .min(dim=-1)
        .values
    )
    return torch.where(inside, torch.zeros_like(d), d)


def point_box_dist(p, c, hw, cs):
    """Distance from points ``p`` to filled oriented boxes."""
    import torch

    pl = _to_box_frame(p, c, cs)
    inside = (pl.abs() <= hw).all(-1)
    edges = _box_edges(hw)
    d = (
        torch.stack(
            [
                point_seg_dist(pl, edges[..., i, 0, :], edges[..., i, 1, :])
                for i in range(4)
            ],
            dim=-1,
        )
        .min(dim=-1)
        .values
    )
    return torch.where(inside, torch.zeros_like(d), d)


def outline_margin(p, poly):
    """Signed distance from points ``p`` (..., 2) to the interior of a
    CONVEX CCW polygon ``poly`` (V, 2): positive inside, negative
    outside. Convexity makes the min-over-edges answer exact."""
    import torch

    verts = poly
    nxt = torch.roll(verts, -1, dims=0)
    e = nxt - verts
    n = torch.stack((-e[..., 1], e[..., 0]), dim=-1)
    n = n / n.norm(dim=-1, keepdim=True).clamp_min(_EPS)
    d = ((p[..., None, :] - verts) * n).sum(-1)
    return d.min(dim=-1).values


def gap(dist, r_a, r_b, required):
    """The spec's gap: core distance minus both radii minus the required
    spacing. ≥ 0 is legal."""
    return dist - r_a - r_b - required
