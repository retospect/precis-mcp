"""Mold release checks — ``n̂·p ≥ sin α`` per slide pull (spec
``docs/backlog/se-cylinder-wrap-mold.md``, slice 2).

A ``relief`` leaf's surface is a cylindrical height field
``ρ(θ, z) = R + s·depth·h(u, v)``: whether it pulls from a k-slide mold
at draft angle ``α`` is a question on the unrolled map, not on mesh
facets. :func:`pull_check` evaluates the release condition on every map
edge (forward differences, ``δ`` at the edge midpoint — the discrete
form the generator's :func:`precis.cad.hmap._relax_flanks` is bound to)
with each point's own slide pull ``p(θ)`` from :func:`slide_delta`, and
checks analytic faces against the same pull set via
:func:`precis.cad.probe.probe_draft`.

Two honest exemptions, both Q1's parting-line rule:

- a *flat* edge at the map's base level is the bare cylinder at the
  join — the mold halves split there, no draft is asked of it. A flat
  edge *above* base (a plateau top, a groove floor) is a feature
  surface and is checked.
- an analytic face whose normal is (anti-)parallel to the pull axis —
  the part's open ends, ``|n·axis| ≈ 1`` — is not captured by the
  slides at all and is skipped; a true witness face (``n·p ≈ 0`` for
  every pull) is a candidate parting surface, reported but not failed.

``relief_fit="clip"`` fixes a failing map the only way that adds no
material: cap it at the join dead-zone fade (``hcap``) and run the same
edge-bound relaxation the generator uses. The report names the depth
removed per leaf.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.hmap import (
    HeightMap,
    _relax_flanks,
    pull_edge_margins,
    slide_delta,
)
from precis.cad.primitives import CylRelief
from precis.cad.probe import DraftFace, probe_draft
from precis.cad.vec import Transform, Vec3, vec3

__all__ = [
    "LeafCheck",
    "PullReport",
    "SlideLayout",
    "clip_map",
    "pull_check",
    "render_undercut_png",
    "undercut_image",
]

#: n̂·p slack below which an edge counts as undercut — matches the
#: relax bound's own margin scale (the relax enforces sin α + 1e-3).
EDGE_TOL = 1e-3


@dataclass(frozen=True)
class SlideLayout:
    """A k-slide radial mold layout: pulls at ``phi + 2πj/k``."""

    k: int
    phi: float = 0.0

    def __post_init__(self) -> None:
        if self.k not in (2, 3):
            raise ValueError(f"a slide layout has k = 2 or 3 pulls, got {self.k}")

    @property
    def pulls(self) -> list[Vec3]:
        return [
            vec3(
                math.cos(self.phi + j * 2.0 * math.pi / self.k),
                math.sin(self.phi + j * 2.0 * math.pi / self.k),
                0.0,
            )
            for j in range(self.k)
        ]


def _live_edges(
    m: NDArray[np.floating[Any]],
) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    """``(live_u, live_v)`` — every edge except the flat-at-base ones.

    Base is ``clip(0, min, max)``: the untouched nominal-cylinder level —
    ``h = 0`` whenever one exists in range, else the level nearest it (a
    wholly-recessed map's floor is its bare surface).
    """
    mf = np.asarray(m, dtype=np.float64)
    base = float(np.clip(0.0, mf.min(), mf.max()))
    tol = max(1e-6, 2e-3 * float(mf.max() - mf.min()))
    nxt = np.roll(mf, -1, axis=0)
    dh_u = np.abs(nxt - mf)
    live_u = (dh_u > tol) | (mf > base + tol) | (nxt > base + tol)
    dv = np.abs(mf[:, 1:] - mf[:, :-1])
    live_v = (dv > tol) | (mf[:, :-1] > base + tol) | (mf[:, 1:] > base + tol)
    return live_u, live_v


def undercut_image(
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
    tol: float = EDGE_TOL,
) -> tuple[NDArray[np.bool_], float]:
    """The undercut image over ``(u, v)``: a pixel is marked when any of
    its incident live edges leans out of its slide's release cone.
    Returns ``(mask, min_margin)`` — ``min_margin`` is the worst live
    ``n̂·p − sin α`` (``nan`` when nothing is live)."""
    m = np.asarray(h, dtype=np.float64)
    nu, nv = m.shape
    mu, mv = pull_edge_margins(
        m,
        r=r,
        height=height,
        depth=depth,
        sign=sign,
        repeat=repeat,
        twist=twist,
        k=k,
        phi=phi,
        alpha=alpha,
    )
    live_u, live_v = _live_edges(m)
    bad_u = live_u & (mu < -tol)
    bad_v = live_v & (mv < -tol)
    # an edge blames both its pixels
    mask = bad_u | np.roll(bad_u, 1, axis=0)
    mask[:, :-1] |= bad_v
    mask[:, 1:] |= bad_v
    vals = np.concatenate([mu[live_u].ravel(), mv[live_v].ravel()])
    return mask, float(vals.min()) if vals.size else float("nan")


def clip_map(
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
    """``relief_fit=clip``: lower ``h`` only where the layout demands it.

    Caps at the join dead-zone fade ``−r·ln cos(90°−α−|δ|)/depth`` — the
    surface height still reachable at each angle — then runs the
    generator's edge-bound relaxation. Returns ``(clipped, removed)``,
    both in normalized map units (``removed * depth`` is the physical
    depth cut)."""
    m = np.asarray(h, dtype=np.float64).copy()
    nu, nv = m.shape
    iu = (np.arange(nu) + 0.5) / nu
    jv = np.arange(nv) / (nv - 1)
    theta = (2.0 * math.pi * iu[:, None] - twist * jv[None, :]) / repeat
    delta = np.abs(slide_delta(theta, k, phi))
    pole = math.pi / 2.0 - alpha
    hcap = np.where(
        delta < pole,
        -r * np.log(np.cos(pole - np.abs(delta))) / depth,
        0.0,
    )
    hcap = np.clip(hcap, 0.0, None)
    np.minimum(m, hcap, out=m)
    _relax_flanks(
        m,
        r=r,
        height=height,
        depth=depth,
        sign=sign,
        repeat=repeat,
        twist=twist,
        k=k,
        alpha=alpha,
        phi=phi,
    )
    return m, np.asarray(h, dtype=np.float64) - m


@dataclass
class LeafCheck:
    """One relief leaf's verdict under one layout."""

    label: str
    layout: SlideLayout
    alpha: float
    mask: NDArray[np.bool_]
    min_margin: float
    undercut_fraction: float
    undercut_area: float
    min_k: int | None
    clipped: HeightMap | None = None
    clip_removed_max: float = 0.0
    clip_removed_mean: float = 0.0

    @property
    def ok(self) -> bool:
        return not bool(self.mask.any())


@dataclass
class PullReport:
    alpha: float
    leaves: list[LeafCheck]
    faces: list[DraftFace]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.leaves) and all(f.ok for f in self.faces)


def _leaf_layout(leaf: CylRelief, layout: SlideLayout | None) -> SlideLayout:
    if layout is not None:
        return layout
    k = leaf.k if leaf.k is not None else 3
    return SlideLayout(k=int(k), phi=float(leaf.phi or 0.0))


def _leaf_alpha(leaf: CylRelief, alpha: float | None) -> float:
    a = alpha if alpha is not None else leaf.alpha
    if a is None:
        raise ValueError(
            f"relief '{_leaf_label(leaf)}' carries no draft angle — pass "
            "alpha= or stamp the map with pull intent"
        )
    return float(a)


def _leaf_label(leaf: CylRelief) -> str:
    return f"relief@{id(leaf):x}"


def check_leaf(
    leaf: CylRelief,
    label: str,
    layout: SlideLayout | None,
    alpha: float | None,
    *,
    relief_fit: str | None = None,
    tol: float = EDGE_TOL,
) -> LeafCheck:
    """One leaf vs one layout (or its recorded pull when ``layout`` is
    ``None``). With ``relief_fit="clip"`` a failing map is clipped and
    the removed depth reported; the clip result is verified against the
    same check."""
    lay = _leaf_layout(leaf, layout)
    a = _leaf_alpha(leaf, alpha)
    m = np.asarray(leaf.hmap, dtype=np.float64)
    kw: dict[str, Any] = dict(
        r=leaf.r,
        height=leaf.h,
        depth=leaf.depth,
        sign=float(leaf.s),
        repeat=leaf.repeat,
        twist=leaf.twist,
        phi=lay.phi,
        alpha=a,
    )
    mask, mmin = undercut_image(m, k=lay.k, tol=tol, **kw)
    min_k: int | None = None
    if mask.any():
        for kk in (2, 3):
            if kk == lay.k:
                continue
            mk, _ = undercut_image(m, k=kk, tol=tol, **kw)
            if not mk.any():
                min_k = kk
                break
    frac = float(mask.mean())
    area = frac * 2.0 * math.pi * leaf.r * leaf.h
    check = LeafCheck(
        label=label,
        layout=lay,
        alpha=a,
        mask=mask,
        min_margin=mmin,
        undercut_fraction=frac,
        undercut_area=area,
        min_k=min_k,
    )
    if relief_fit == "clip" and mask.any():
        clipped, removed = clip_map(m, k=lay.k, **kw)
        cm, cmin = undercut_image(clipped, k=lay.k, tol=tol, **kw)
        check.clipped = HeightMap(
            clipped.astype(np.float32),
            pull={"sign": float(leaf.s), "k": lay.k, "alpha": a, "phi": lay.phi},
            seam="wall",
        )
        check.clip_removed_max = float(removed.max()) * leaf.depth
        check.clip_removed_mean = float(removed.mean()) * leaf.depth
        check.mask = cm
        check.min_margin = cmin
        check.undercut_fraction = float(cm.mean())
        check.undercut_area = float(cm.mean()) * 2.0 * math.pi * leaf.r * leaf.h
    return check


def pull_check(
    design: Any,
    layout: SlideLayout | int | None = None,
    alpha: float | None = None,
    *,
    relief_fit: str | None = None,
    tol: float = EDGE_TOL,
) -> PullReport:
    """``pull_check(design, layout, α)`` — slice 2's DRC core.

    Relief leaves are checked in map space (edge margins); every other
    instance's analytic faces are checked against the same slide pulls
    (world frame: the first relief leaf's placement maps the local pull
    set out, identity otherwise). ``layout`` accepts a
    :class:`SlideLayout`, a bare ``k``, or ``None`` (each leaf's recorded
    pull). ``relief_fit="clip"`` clips failing maps and reports the
    removed depth."""
    if relief_fit is not None and relief_fit != "clip":
        raise ValueError(f"relief_fit must be 'clip' | None, got {relief_fit!r}")
    lay = SlideLayout(int(layout), phi=0.0) if isinstance(layout, int) else layout
    leaves: list[LeafCheck] = []
    first_leaf_xf: Transform | None = None
    a_seen = alpha
    for name, inst in design.instances.items():
        prim = inst.placed.prim
        if not isinstance(prim, CylRelief):
            continue
        if first_leaf_xf is None:
            first_leaf_xf = inst.placed.xform
        leaves.append(
            check_leaf(prim, name, lay, alpha, relief_fit=relief_fit, tol=tol)
        )
        if a_seen is None:
            a_seen = leaves[-1].alpha
    faces: list[DraftFace] = []
    if lay is not None or leaves:
        eff = lay or leaves[0].layout
        pulls = [p for p in eff.pulls]
        if first_leaf_xf is not None:
            pulls = [first_leaf_xf.apply_dir(p) for p in pulls]
        a_deg = math.degrees(a_seen if a_seen is not None else math.radians(1.0))
        axis = (
            first_leaf_xf.apply_dir(vec3(0.0, 0.0, 1.0))
            if first_leaf_xf is not None
            else vec3(0.0, 0.0, 1.0)
        )
        for f in probe_draft(design, pulls, min_deg=a_deg):
            n = f.normal_unit
            # open end (normal along the pull axis) — not the slides'
            # surface; true witness (n·p ≈ 0 for every pull) — a
            # candidate parting surface; a zero-slant lateral — the bare
            # cylinder at the join. All three are exempt like the
            # map-side flat-at-base rule.
            exempt = (
                (n is not None and abs(float(n @ axis)) >= 0.999)
                or (n is not None and abs(f.ndotp) <= 1e-3)
                or (f.tag == "lateral" and abs(f.draft_deg) < 0.01)
            )
            faces.append(
                DraftFace(
                    f.label,
                    f.tag,
                    f.draft_deg,
                    True if exempt else f.ok,
                    f.pull_index,
                    f.ndotp,
                )
            )
    return PullReport(
        alpha=a_seen if a_seen is not None else math.radians(1.0),
        leaves=leaves,
        faces=faces,
    )


def render_undercut_png(leaf: CylRelief, check: LeafCheck, path: str) -> None:
    """Write the undercut image as a PNG: the map in grayscale, every
    undercut pixel red, the pull centres marked green on the seam rows.
    Rows are ``v`` (bottom→top), columns ``u`` — the unrolled cylinder."""
    from PIL import Image

    m = np.asarray(leaf.hmap, dtype=np.float64)
    span = max(float(m.max() - m.min()), 1e-9)
    g = ((m - m.min()) / span * 200.0 + 30.0).astype(np.uint8)
    rgb = np.stack([g, g, g], axis=-1)
    rgb[check.mask] = np.array([220, 30, 30], dtype=np.uint8)
    img = Image.fromarray(np.transpose(rgb, (1, 0, 2))[::-1], "RGB")
    img.save(path)
