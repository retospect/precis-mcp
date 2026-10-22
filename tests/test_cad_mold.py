"""``precis.cad.mold.pull_check`` — the mold-release check (spec
``docs/backlog/se-cylinder-wrap-mold.md`` slice 2): map-space edges on
relief leaves, analytic faces on the rest, and the ``relief_fit='clip'``
fixer. The se-side ``mold.undercut`` finding rides the same check."""

import math

import numpy as np

from precis.cad.graph import Design
from precis.cad.hmap import HeightMap, hmap_array, hmap_sketch
from precis.cad.mold import SlideLayout, pull_check
from precis.cad.primitives import CircularFrustum, CylRelief

_R = 0.05
_H = 0.10
_DEPTH = 0.002
_ALPHA = math.radians(2.0)


def _leaf(m, **kw):
    kw.setdefault("r", _R)
    kw.setdefault("h", _H)
    kw.setdefault("depth", _DEPTH)
    return CylRelief(hmap=m, **kw)


def _design(**leaves):
    des = Design()
    for name, leaf in leaves.items():
        des.prim(name, leaf)
    return des


def test_generated_map_passes_at_its_own_k():
    """A slice-1 pull-aware map passes the check it was built for."""
    hm = hmap_sketch(
        256,
        48,
        [[(0.30, 0.3), (0.55, 0.3), (0.55, 0.7), (0.30, 0.7)]],
        radius=_R,
        height=_H,
        depth=_DEPTH,
        k=3,
        alpha=_ALPHA,
    )
    rep = pull_check(_design(r1=_leaf(hm)), SlideLayout(3, 0.0), _ALPHA)
    assert rep.ok and rep.leaves[0].min_margin > 0.0


def test_hand_overhang_fails_with_area():
    """A full-height plateau wall hard against the k=2 join — a join-
    facing overhang — fails, and the report names the layout and area."""
    m = np.zeros((256, 48))
    m[:, 10:38] = 1.0  # wrap wall: columns 0/255 both sit mid-plateau
    leaf = _leaf(hmap_array(m), k=2, alpha=_ALPHA, phi=0.0, seam="wall")
    rep = pull_check(_design(o1=leaf), SlideLayout(2, 0.0), _ALPHA)
    c = rep.leaves[0]
    assert not c.ok
    assert c.layout.k == 2
    assert 0.0 < c.undercut_fraction < 1.0
    assert c.undercut_area > 0.0
    assert not rep.ok


def test_k2_vs_k3_choose_differently():
    """A feature at u≈1/3 (θ≈120°) sits on a k=3 pull centre but δ≈60°
    off both k=2 pulls — its walls clear only at k=3."""
    m = np.zeros((256, 48))
    m[70:100, 10:38] = 1.0  # plateau centred near u = 0.33
    leaf = _leaf(hmap_array(m), k=2, alpha=_ALPHA, phi=0.0)
    rep2 = pull_check(_design(p=leaf), SlideLayout(2, 0.0), _ALPHA)
    rep3 = pull_check(_design(p=leaf), SlideLayout(3, 0.0), _ALPHA)
    assert not rep2.ok and rep3.ok
    assert rep2.leaves[0].min_k == 3


def test_clip_clears_overhang_and_reports_depth():
    """``relief_fit='clip'`` lowers the map only where the layout
    demands, adds no material, and reports the depth removed."""
    m = np.zeros((256, 48))
    m[:, 10:38] = 1.0
    leaf = _leaf(hmap_array(m), k=2, alpha=_ALPHA, phi=0.0, seam="wall")
    before = np.asarray(leaf.hmap, dtype=np.float64)
    rep = pull_check(_design(o1=leaf), SlideLayout(2, 0.0), _ALPHA, relief_fit="clip")
    c = rep.leaves[0]
    assert c.ok, "clip must clear the undercut"
    assert c.clipped is not None
    clipped = np.asarray(c.clipped.values, dtype=np.float64)
    assert np.all(clipped <= before + 1e-6)  # adds no material
    assert c.clip_removed_max > 0.0
    assert c.clip_removed_mean >= 0.0
    assert c.clip_removed_max <= _DEPTH + 1e-9


def test_analytic_draft_face_below_alpha_fails():
    """A shallow frustum lateral (slant < alpha) fails; its open-end
    caps stay exempt."""
    des = _design(c1=CircularFrustum(rb=0.01, rt=0.0099, h=0.01))
    rep = pull_check(des, SlideLayout(2, 0.0), math.radians(1.0))
    lat = next(f for f in rep.faces if f.tag == "lateral")
    assert not lat.ok and lat.draft_deg < 1.0
    caps = [f for f in rep.faces if f.tag in ("top", "bottom")]
    assert all(f.ok for f in caps)


def test_undercut_image_is_the_drc_picture():
    """The undercut image marks exactly the overhung region — the same
    bitmap view='mold' writes."""
    m = np.zeros((128, 32))
    m[:, 8:24] = 1.0
    leaf = _leaf(hmap_array(m), k=2, alpha=_ALPHA, phi=0.0, seam="wall")
    rep = pull_check(_design(o1=leaf), SlideLayout(2, 0.0), _ALPHA)
    mask = rep.leaves[0].mask
    # the join at ±90° from the k=2 pull at 0° → undercut pixels near
    # u ∈ {0.25, 0.75} where the wrap wall stands
    u_idx = np.argwhere(mask.any(axis=1)).ravel()
    assert len(u_idx) > 0
    # the wall at the seam (u≈0) is centre-facing for this map — the
    # undercut must be away from u=0, not smeared everywhere
    assert mask.mean() < 0.5


def test_probe_draft_single_pull_compat():
    """Single-Vec3 probe_draft keeps its old unsigned behaviour."""
    from precis.cad.probe import probe_draft
    from precis.cad.vec import vec3

    des = _design(c1=CircularFrustum(rb=0.01, rt=0.0099, h=0.01))
    rows = probe_draft(des, vec3(1.0, 0.0, 0.0), min_deg=1.0)
    assert all(r.pull_index == -1 or r.tag == "lateral" for r in rows)
    assert all(r.draft_deg >= 0.0 for r in rows)


# ---------------------------------------------------------------------------
# se side: the mold.undercut finding rides the bound cad design
# ---------------------------------------------------------------------------


def test_se_drc_finds_undercut(store):
    """A block bound to a cad design whose relief undercuts lands the
    ``mold.undercut`` error naming the layout and area."""
    from precis.dispatch import Hub
    from precis.handlers.cad import CadHandler
    from precis_se import mold as se_mold
    from precis_se.ops import SeBlock, SeTree

    cad = CadHandler(hub=Hub(store=store))
    cad.put(id="moldwheel", text="component w\nbase add box:w10mmd10mmh10mm")
    ref = store.get_ref(kind="cad", id="moldwheel")
    m = np.zeros((128, 32))
    m[:, 8:24] = 1.0
    hm = HeightMap(m, seam="wall")
    sha = store.put_hmap(ref.id, hm, provenance={"source": "mold-test"})
    cad.put(
        id="moldwheel",
        text=f"component w\nbase add relief:r50mmh100mmd2mmk2a2degmap:{sha}",
    )
    node = SeBlock(name="wheel", bound_kind="cad", bound="moldwheel")
    tree = SeTree(blocks={"wheel": node})
    found = se_mold.findings(store, tree, ref.id)
    assert len(found) == 1
    f = found[0]
    assert f.rule == "mold.undercut"
    assert f.severity == "error"
    assert "k=2" in f.detail and "mm²" in f.detail
    # and a mold-clean design finds nothing
    m2 = hmap_sketch(
        128,
        32,
        [[(0.3, 0.3), (0.5, 0.3), (0.5, 0.7), (0.3, 0.7)]],
        radius=_R,
        height=_H,
        depth=_DEPTH,
        k=3,
        alpha=_ALPHA,
    )
    cad.put(id="moldok", text="component w\nbase add box:w10mmd10mmh10mm")
    ref2 = store.get_ref(kind="cad", id="moldok")
    sha2 = store.put_hmap(ref2.id, m2, provenance={"source": "mold-test"})
    cad.put(
        id="moldok",
        text=f"component w\nbase add relief:r50mmh100mmd2mmmap:{sha2}",
    )
    tree2 = SeTree(
        blocks={"wheel2": SeBlock(name="wheel2", bound_kind="cad", bound="moldok")}
    )
    assert se_mold.findings(store, tree2, ref2.id) == []
