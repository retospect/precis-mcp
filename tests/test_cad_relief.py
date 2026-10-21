"""CylRelief — the cylinder wrap/relief leaf, slice 1 of
``docs/backlog/se-cylinder-wrap-mold.md``.

Pins the acceptance list: ``h ≡ 0`` equals ``cyl:``, ``h ≡ c`` equals a
``cyl:`` of ``R + c``; the u-seam rule (refuse a jump, blend it); the
conservative distance bound against dense brute force; enclosed volume
against ``∫∫ ½(R + s·h)² dθ dz``; the ``relief:…map:<sha>`` DSL and the
``chunk_blobs`` store round-trip; and the pull-aware flanks on
sketch/text inputs — every sampled point's ``n·p ≥ sin α`` (the Q1
corrected check: ``cos β cos θ − sin β sin θ cos ψ``).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis.cad.dsl import DslError, build, build_config, format_spec, parse
from precis.cad.fieldmesh import field_mesh
from precis.cad.fieldops import payload_sha256
from precis.cad.hmap import (
    HMAP_MIME,
    decode_hmap,
    encode_hmap,
    hmap_array,
    hmap_knurl,
    hmap_png,
    hmap_ribs,
    hmap_sketch,
    hmap_text,
)
from precis.cad.primitives import CircularFrustum, CylRelief
from precis.cad.vec import vec3
from precis.store._cad_ops import FieldNotFound

_R = 0.05
_H = 0.1
_DEPTH = 0.002


def _leaf(m, **kw):
    kw.setdefault("r", _R)
    kw.setdefault("h", _H)
    kw.setdefault("depth", _DEPTH)
    return CylRelief(hmap=m, **kw)


def _cloud(n=4000, seed=7, r_out=0.065):
    rng = np.random.default_rng(seed)
    pts = rng.uniform(-r_out, r_out, (n, 3))
    pts[:, 2] = rng.uniform(-0.02, _H + 0.02, n)
    return pts


def test_h_zero_equals_cyl():
    leaf = _leaf(np.zeros((64, 8)))
    cyl = CircularFrustum(rb=_R, rt=_R, h=_H)
    pts = _cloud()
    dl, dc = leaf.distance_local_np(pts), cyl.distance_local_np(pts)
    assert np.all((dl <= 0) == (dc <= 0))
    # the CSG bound: never overestimates, and stays within the √2 of the
    # exact distance that a max() of constraints loses near rim corners
    assert np.max(dl - dc) <= 1e-12
    assert np.all(dl >= np.where(dc > 0, dc / math.sqrt(2), dc * math.sqrt(2)) - 1e-9)
    # exact where a single constraint governs
    assert leaf.distance_local(vec3(0.0, 0.0, _H + 0.01)) == pytest.approx(0.01)
    assert leaf.distance_local(vec3(_R + 0.01, 0.0, _H / 2)) == pytest.approx(0.01)
    assert leaf.distance_local(vec3(_R / 2, 0.0, _H / 2)) == pytest.approx(-_R / 2)
    # ray hits agree with the analytic cylinder on an axial ray and a
    # chord ray
    hits = leaf.ray_hits_local(vec3(0.0, 0.0, -0.5), vec3(0.0, 0.0, 1.0))
    assert len(hits) == 1
    assert hits[0][0] == pytest.approx(0.5, abs=1e-6)
    assert hits[0][1] == pytest.approx(_H + 0.5, abs=1e-6)
    hits = leaf.ray_hits_local(vec3(-1.0, 0.0, _H / 2), vec3(1.0, 0.0, 0.0))
    assert len(hits) == 1
    assert abs(hits[0][0] - (1.0 - _R)) < 1e-6
    assert abs(hits[0][1] - (1.0 + _R)) < 1e-6


def test_h_constant_equals_wider_cyl():
    leaf = _leaf(np.ones((64, 8)))
    cyl = CircularFrustum(rb=_R + _DEPTH, rt=_R + _DEPTH, h=_H)
    pts = _cloud(r_out=_R + 4 * _DEPTH)
    dl, dc = leaf.distance_local_np(pts), cyl.distance_local_np(pts)
    assert np.all((dl <= 0) == (dc <= 0))
    assert np.max(dl - dc) <= 1e-12


def test_deboss_is_carved_inward():
    leaf = _leaf(np.ones((64, 8)), s=-1.0)
    # surface at rho = R - depth everywhere
    assert leaf.distance_local(vec3(_R - _DEPTH, 0.0, _H / 2)) < 1e-9
    assert leaf.distance_local(vec3(_R - _DEPTH + 1e-4, 0.0, _H / 2)) > 0


def test_seam_refused_and_blended():
    m = np.linspace(0.0, 1.0, 64)[:, None] * np.ones((64, 16))
    assert not np.allclose(m[0], m[-1])
    with pytest.raises(ValueError, match="not periodic"):
        hmap_array(m)
    with pytest.raises(ValueError, match="not periodic"):
        _leaf(m)
    blended = hmap_array(m, seam="blend", blend_width=8)
    assert blended.values[0] == pytest.approx(blended.values[-1])
    _leaf(blended.values)  # now admissible


def test_seam_surface_continuity():
    hm = hmap_knurl(256, 32, count=7, mode="diamond", helix=0.4)
    leaf = _leaf(hm.values)
    # distance sampled on a ring just outside the surface must match
    # across theta = 0 ≡ 2pi (no seam cliff)
    z = np.linspace(0.01, _H - 0.01, 50)
    th = np.linspace(-0.01, 0.01, 21)
    pts = np.stack(
        [
            (_R + 3 * _DEPTH) * np.cos(th)[:, None] * np.ones(len(z))[None, :],
            (_R + 3 * _DEPTH) * np.sin(th)[:, None] * np.ones(len(z))[None, :],
            np.broadcast_to(z[None, :], (len(th), len(z))),
        ],
        axis=-1,
    ).reshape(-1, 3)
    d = leaf.distance_local_np(pts).reshape(len(th), len(z))
    # the function is periodic: samples near theta=0 from both sides must
    # vary smoothly (no step > a map cell's slope)
    step = np.abs(d[10, :] - d[9, :])
    assert step.max() < 5e-4


def test_distance_bound_never_overestimates():
    rng = np.random.default_rng(3)
    hm = hmap_ribs(96, 48, count=6, duty=0.4, flank=0.3)
    leaf = _leaf(hm.values)
    # dense surface sample: parametric rho_s(theta, z)
    th = np.linspace(0.0, 2 * math.pi, 721)[:-1]
    zz = np.linspace(0.0, _H, 400)
    u_map = (leaf.repeat * (th / (2 * math.pi))) % 1.0
    T, Z = np.meshgrid(th, zz, indexing="ij")
    U = np.broadcast_to(u_map[:, None], T.shape)
    V = Z / _H
    hs = leaf._h_at(U, V)
    rho_s = _R + leaf.s * _DEPTH * hs
    surf = np.stack([rho_s * np.cos(T), rho_s * np.sin(T), Z], axis=-1).reshape(-1, 3)
    # query points near the surface
    q = surf[rng.integers(0, len(surf), 2000)]
    q += rng.normal(0.0, 3e-4, q.shape)
    d_leaf = leaf.distance_local_np(q)
    # brute-force distance to the sampled surface (upper bound on the
    # true distance; the leaf's bound must stay under it)
    d_true = np.empty(len(q))
    for i in range(0, len(q), 250):
        d_true[i : i + 250] = np.linalg.norm(
            q[i : i + 250, None, :] - surf[None, :, :], axis=2
        ).min(axis=1)
    assert np.all(d_leaf <= d_true + 1e-9)


def test_volume_matches_map_integral():
    hm = hmap_ribs(128, 48, count=5, duty=0.5, flank=0.2)
    leaf = _leaf(hm.values)
    lo, hi = leaf.aabb_local()
    verts, tris = field_mesh(
        leaf.distance_local_np, lo, hi, pitch=0.0007, name="relief"
    )
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    vol = abs(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum() / 6.0)
    # analytic: ∫0^H ∫0^2pi 1/2 (R + s·d·h)^2 dtheta dz; repeat/twist are
    # measure preserving, so a uniform (u, v) average suffices
    m = hm.values.astype(np.float64)
    v_analytic = math.pi * _H * float(np.mean((_R + leaf.s * _DEPTH * m) ** 2))
    assert vol == pytest.approx(v_analytic, rel=0.02)


def test_mesh_is_watertight():
    leaf = _leaf(hmap_knurl(96, 32, count=8).values)
    lo, hi = leaf.aabb_local()
    verts, tris = field_mesh(leaf.distance_local_np, lo, hi, pitch=0.001, name="relief")
    directed = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    _u, counts = np.unique(np.sort(directed, axis=1), axis=0, return_counts=True)
    assert np.all(counts == 2)


# ---------------------------------------------------------------------------
# DSL + codec + store
# ---------------------------------------------------------------------------


def test_dsl_parse_format_roundtrip():
    ref = "ab" * 32
    spec = parse(f"relief:r0.05h0.1d0.002s1n3k2a0.03p0t0map:{ref}")
    assert spec.alias == "relief" and spec.ref == ref
    assert spec.params["n"] == 3.0 and spec.params["k"] == 2.0
    assert format_spec(spec) == f"relief:r0.05h0.1d0.002s1n3k2a0.03p0t0map:{ref}"
    again = parse(format_spec(spec))
    assert again == spec
    b = parse(
        "relief:r50mmh100mmd2mms-1n2k3a1.5degp10degt5degmap:" + ref[:16],
        require_units=True,
    )
    assert b.params["s"] == -1.0 and b.params["k"] == 3.0
    assert b.params["a"] == pytest.approx(math.radians(1.5))
    # canonical round-trip through units=True emission
    assert parse(format_spec(b, units=True), require_units=True) == b


def test_dsl_errors():
    ref = "ab" * 32
    with pytest.raises(DslError):
        parse(f"relief:r0.05h0.1map:{ref}")  # missing d
    with pytest.raises(DslError):
        parse(f"relief:r0.05h0.1d0.002k4map:{ref}")  # k not in {2,3}
    with pytest.raises(DslError):
        parse(f"relief:r0.05h0.1d0.002s2map:{ref}")
    with pytest.raises(DslError):
        parse(f"relief:r0.05h0.1d0.002map:{ref[:8]}")  # ref too short
    with pytest.raises(DslError):
        parse(f"relief:r0.05h0.1d0.002rd0.001map:{ref}")  # rd refused


def test_build_needs_loader_and_uses_it():
    hm = hmap_ribs(64, 16, count=4)
    spec = parse("relief:r0.05h0.1d0.002map:" + "cd" * 32)
    with pytest.raises(DslError, match="loader"):
        build(spec)
    leaf = build(spec, hmap_loader={spec.ref: hm}.__getitem__)
    assert isinstance(leaf, CylRelief) and leaf.repeat == 1 and leaf.s == 1.0
    spec2 = parse("relief:r0.05h0.1d0.002s-1n2k3a0.02p0.5t0.1map:" + "cd" * 32)
    leaf2 = build_config(format_spec(spec2), hmap_loader={spec2.ref: hm}.__getitem__)
    assert isinstance(leaf2, CylRelief)
    assert (leaf2.s, leaf2.repeat, leaf2.k) == (-1.0, 2, 3)
    assert leaf2.alpha == pytest.approx(0.02) and leaf2.phi == pytest.approx(0.5)


def test_hmap_codec_roundtrip():
    hm = hmap_ribs(100, 20, count=3)
    payload = encode_hmap(hm, {"source": "test"})
    header, back = decode_hmap(payload)
    assert header["shape"] == [100, 20] and header["u_periodic"]
    assert np.array_equal(back.values, hm.values)
    assert payload_sha256(payload) == payload_sha256(
        encode_hmap(hm, {"source": "test"})
    )
    with pytest.raises(ValueError):
        decode_hmap(b"nope")
    with pytest.raises(ValueError):
        decode_hmap(payload[:-4])


def test_hmap_png():
    import io

    from PIL import Image

    img = Image.new("I;16", (64, 32))
    px = img.load()
    assert px is not None
    for y in range(32):
        for x in range(64):
            px[x, y] = int(65535 * y / 31)  # v-gradient: periodic in u
    buf = io.BytesIO()
    img.save(buf, "PNG")
    hm = hmap_png(buf.getvalue())
    assert hm.shape == (64, 32)
    # image row 0 (top) is v=1 → the map's last row
    assert hm.values[:, -1] == pytest.approx(0.0, abs=1e-4)
    assert hm.values[:, 0] == pytest.approx(1.0, abs=1e-4)


def test_put_get_hmap_roundtrip(store):
    hm = hmap_ribs(48, 12, count=4)
    ref = store.insert_ref(kind="cad", slug="relief-t", title="t", meta={})
    sha = store.put_hmap(ref.id, hm, provenance={"source": "test-ribs"})
    assert store.put_hmap(ref.id, hm, provenance={"source": "test-ribs"}) == sha
    header, back = store.get_hmap(sha)
    assert np.array_equal(back.values, hm.values)
    assert header["sha256"] == sha
    assert store.hmap_sha(sha[:16]) == sha
    with pytest.raises(FieldNotFound):
        store.hmap_sha("ff" * 32)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT mime FROM chunk_blobs WHERE sha256 = %s", (sha,)
        ).fetchone()
    assert row[0] == HMAP_MIME


# ---------------------------------------------------------------------------
# pull-aware flanks (Q1/Q2, corrected rule)
# ---------------------------------------------------------------------------


def _surface_np(m, leaf, k, phi):
    """``n·p`` at every map pixel, from the map gradient (the DRC form).

    The surface is rho = R + s·depth·h(u,v); its normal ∝ e_r − g_x e_t −
    g_z e_z with g_x = s·depth·h_u/(P), g_z = s·depth·(h_u·twist/(2πH) +
    h_v/H); the pull is the slide direction at the nearest of
    phi + 2πj/k. Returns the min over the map.
    """
    nu, nv = m.shape
    period = 2 * math.pi * _R / leaf.repeat
    mf = m.astype(np.float64)
    # SIGNED gradients — the normal's lean direction is the whole point
    # of the check (periodic central difference in u, one-sided at v)
    du = (np.roll(mf, -1, axis=0) - np.roll(mf, 1, axis=0)) * 0.5 * nu
    fd = (mf[:, 1:] - mf[:, :-1]) * (nv - 1)
    dv = np.concatenate(
        [fd[:, :1], 0.5 * (mf[:, 2:] - mf[:, :-2]) * (nv - 1), fd[:, -1:]],
        axis=1,
    )
    iu = (np.arange(nu) + 0.5) / nu
    jv = np.arange(nv) / (nv - 1)
    theta = (2 * math.pi * iu[:, None] - leaf.twist * jv[None, :]) / leaf.repeat
    g_x = leaf.s * _DEPTH * du / period
    g_z = leaf.s * _DEPTH * (du * leaf.twist / (2 * math.pi) + dv) / _H
    # only meaningful slope pixels are checked; flat base is bare cylinder
    slope2 = g_x**2 + g_z**2
    # dead-zone fade pixels also legitimately sit at h≈0 with a join-
    # facing slope — include them: the check is on the actual surface
    period_k = 2 * math.pi / k
    delta = (theta - phi + 0.5 * period_k) % period_k - 0.5 * period_k
    # pull at phi + 2πj/k, nearest by |delta|: pull angle = theta - delta
    th_pull = theta - delta
    n_r = 1.0
    n_t = -g_x
    n_z = -g_z
    n_norm = np.sqrt(n_r**2 + n_t**2 + n_z**2)
    # n·p in cartesian: n = e_r − g_x e_t − g_z e_z, p = (cos θ_pull,
    # sin θ_pull, 0)
    ex, ey = np.cos(theta), np.sin(theta)
    tx, ty = -np.sin(theta), np.cos(theta)
    nx = n_r * ex + n_t * tx
    ny = n_r * ey + n_t * ty
    ndotp = (nx * np.cos(th_pull) + ny * np.sin(th_pull)) / n_norm
    return ndotp, slope2


def _assert_moldable(m, leaf, k, phi, alpha):
    ndotp, slope2 = _surface_np(m, leaf, k, phi)
    live = slope2 > 1e-12  # skip flat base (bare-cylinder exemption)
    assert live.any()
    assert ndotp[live].min() >= math.sin(alpha) - 1e-2


@pytest.mark.parametrize("k", [2, 3])
def test_sketch_flanks_release(k):
    alpha = math.radians(2.0)
    hm = hmap_sketch(
        512,
        64,
        [
            [(0.30, 0.3), (0.55, 0.3), (0.55, 0.7), (0.30, 0.7)],
            [(0.62, 0.35), (0.68, 0.35), (0.68, 0.65), (0.62, 0.65)],
        ],
        radius=_R,
        height=_H,
        depth=_DEPTH,
        k=k,
        alpha=alpha,
        phi=0.0,
    )
    leaf = _leaf(hm.values, k=k, alpha=alpha, phi=0.0)
    _assert_moldable(hm.values, leaf, k, 0.0, alpha)


@pytest.mark.parametrize("k", [2, 3])
def test_text_flanks_release(k):
    alpha = math.radians(2.0)
    hm = hmap_text(
        "AB",
        512,
        64,
        at=(0.30, 0.35),
        size=0.03,
        radius=_R,
        height=_H,
        depth=_DEPTH,
        k=k,
        alpha=alpha,
        phi=0.0,
    )
    leaf = _leaf(hm.values, k=k, alpha=alpha, phi=0.0)
    _assert_moldable(hm.values, leaf, k, 0.0, alpha)


def test_k2_join_fades_to_zero():
    alpha = math.radians(2.0)
    # a big plateau covering the whole v range centred near the k=2 join
    # at 90°: the dead zone must pull it to ~0 there
    hm = hmap_sketch(
        512,
        32,
        [[(0.0, 0.2), (0.9, 0.2), (0.9, 0.8), (0.0, 0.8)]],
        radius=_R,
        height=_H,
        depth=_DEPTH,
        k=2,
        alpha=alpha,
        phi=0.0,
    )
    nu = hm.shape[0]
    # join at u = 0.25 and u = 0.75 of the tile (pull at phi=0, k=2:
    # joins at ±90° → u = 0.25, 0.75)
    for u_join in (0.25, 0.75):
        i = int(u_join * nu - 0.5) % nu
        assert hm.values[i].max() < 0.05


# ---------------------------------------------------------------------------
# end to end: handler put → cad_load → field-backend export
# ---------------------------------------------------------------------------


def _stl_volume(path) -> float:
    """Signed volume of a binary STL (80 B header, count, 50 B per facet)."""
    raw = path.read_bytes()
    n = int(np.frombuffer(raw[80:84], dtype="<u4")[0])
    rec = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    tri = np.frombuffer(raw[84 : 84 + 50 * n], dtype=rec)["v"].astype(np.float64)
    v0, v1, v2 = tri[:, 0], tri[:, 1], tri[:, 2]
    return float(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum() / 6.0)


def test_relief_exports_end_to_end_through_the_cad_handler(store, tmp_path):
    from precis.cad.scene import build_design
    from precis.dispatch import Hub
    from precis.errors import BadInput
    from precis.handlers.cad import CadHandler

    cad = CadHandler(hub=Hub(store=store))
    cad.put(id="knob", text="base add box:w10mmd10mmh10mm")
    ref = store.get_ref(kind="cad", id="knob")
    hm = hmap_ribs(96, 16, count=8, duty=0.4, flank=0.3)
    sha = store.put_hmap(ref.id, hm, provenance={"source": "e2e-ribs"})
    # a 16-char prefix on the boundary; the stored config carries the full sha
    resp = cad.put(
        id="knob", text=f"component p\ngrip add relief:r10mmh10mmd1mmmap:{sha[:16]}\n"
    )
    assert "updated" in resp.body
    spec, _handles = store.cad_load(ref.id)
    assert spec.nodes[0].config.endswith(f"map:{sha}")
    assert spec.hmap_loader is not None
    design = build_design(spec)
    leaf = design.instances["i1"].placed.prim
    assert isinstance(leaf, CylRelief)
    # stl through the field backend (a relief has no analytic tessellation)
    out = tmp_path / "knob.stl"
    resp = cad.get(id="knob", view="stl", args={"path": str(out), "pitch": "0.2mm"})
    assert out.exists() and "STL" in resp.body
    # the exported mesh encloses ∫∫ ½(R + s·d·h)² dθ dz, in mm³
    r_mm, h_mm, d_mm = 10.0, 10.0, 1.0
    m = hm.values.astype(np.float64)
    v_analytic = math.pi * h_mm * float(np.mean((r_mm + d_mm * m) ** 2))
    assert abs(_stl_volume(out)) == pytest.approx(v_analytic, rel=0.02)
    # an unknown map is refused on put, naming it, before anything is saved
    with pytest.raises(BadInput, match="deadbeefdead"):
        cad.put(id="knob2", text="grip add relief:r10mmh10mmd1mmmap:deadbeefdeadbeef")
    assert store.get_ref(kind="cad", id="knob2") is None
