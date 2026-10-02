"""gr450675 — the atomic↔smooth overlay's own server route,
``/se/{slug}/atomic3d.json``, plus the ``bt3d-smooth`` slider's presence
on the detail3d page. Web test-client pattern borrowed from
``tests/precis_web/test_blocktree_view.py``'s own ``blocktree_client``
fixture; the store-aware bind mirrors ``tests/test_se_atomic_bind.py``'s
``_make_structure``/``bind_structure`` shape."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_se
from precis.cad.tessellate import apply_rigid
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis.handlers.structure import StructureHandler
from precis_se.atomic.generators.sp2 import build_fullerene
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.config import WebConfig

_SE_MIGRATIONS = Path(precis_se.__file__).parent / "migrations"


def _apply_migrations(store: Any, directory: Path) -> None:
    with store.pool.connection() as c:
        for sql in sorted(directory.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))


@pytest.fixture
def atomic3d_client(store, runtime_with_store, tmp_path) -> TestClient:
    _apply_migrations(store, _SE_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


_C60 = build_fullerene({"atoms": 60})


def _seed_c60_structure(runtime_with_store, slug: str) -> None:
    """A ``structure`` design whose atoms are the real C60 coordinates
    (module-level ``_C60``) — no declared bonds, so the atomic3d route
    exercises its ``detect_bonds`` fallback (the same one
    ``routes/structure.py::_geom_payload`` uses)."""
    StructureHandler(hub=runtime_with_store.hub).put(
        id=slug,
        text=json.dumps(
            {
                "cell": {"a": 40.0, "b": 40.0, "c": 40.0, "pbc": [False, False, False]},
                "ops": [
                    {"op": "add_atom", "element": "C", "cart": [float(x) for x in c]}
                    for c in _C60.coords
                ],
            }
        ),
    )


def _seed_atomic_se(runtime_with_store, *, slug: str, structure_slug: str) -> None:
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5e-9"},
        {"op": "bind_structure", "block": "hub", "design": structure_slug},
    ]
    SeHandler(hub=runtime_with_store.hub).put(id=slug, text=json.dumps({"ops": ops}))


def _seed_plain_se(runtime_with_store, slug: str) -> None:
    ops = [{"op": "add_block", "name": "hub", "envelope": "sphere:r5e-9"}]
    SeHandler(hub=runtime_with_store.hub).put(id=slug, text=json.dumps({"ops": ops}))


def test_atomic3d_json_one_block_60_atoms_90_bonds_32_faces(
    atomic3d_client, runtime_with_store
) -> None:
    _seed_c60_structure(runtime_with_store, "c60frag")
    _seed_atomic_se(runtime_with_store, slug="c60design", structure_slug="c60frag")

    r = atomic3d_client.get("/se/c60design/atomic3d.json")
    assert r.status_code == 200
    body = r.json()
    assert len(body["blocks"]) == 1
    block = body["blocks"][0]
    assert len(block["elements"]) == 60
    assert set(block["elements"]) == {"C"}
    assert len(block["bonds"]) == 90
    assert len(block["faces"]) == 32
    assert len(block["deviation"]) == 60
    assert len(block["coords"]) == 60
    assert len(block["smooth"]) == 60

    scene_r = atomic3d_client.get("/se/c60design/scene3d.json")
    assert scene_r.status_code == 200
    assert body["scale"] == pytest.approx(scene_r.json()["scale"])


def test_atomic3d_json_atoms_land_inside_the_blocks_own_envelope(
    atomic3d_client, runtime_with_store
) -> None:
    """The world-placement check the build brief asks for: every atom
    coordinate must sit inside the SAME (scaled) envelope mesh
    scene3d.json itself would draw for block ``hub`` — computed
    independently here via :func:`~precis.cad.tessellate.apply_rigid`
    over the envelope's own tessellated vertices, not by re-deriving the
    atomic3d route's own transform."""
    _seed_c60_structure(runtime_with_store, "c60frag2")
    _seed_atomic_se(runtime_with_store, slug="c60design2", structure_slug="c60frag2")

    r = atomic3d_client.get("/se/c60design2/atomic3d.json")
    body = r.json()
    block = body["blocks"][0]
    scale = body["scale"]

    from precis.cad.tessellate import mesh_config

    verts, _tris = mesh_config("sphere:r5e-9")
    # hub's pose/rot are both the add_block default ([0, 0, 0]) — identity.
    xf = cad_pose(cad_as_vec3([0.0, 0.0, 0.0]), cad_as_vec3([0.0, 0.0, 0.0]))
    world = apply_rigid(xf, verts) * scale
    lo, hi = world.min(axis=0), world.max(axis=0)

    coords = np.array(block["coords"])
    assert np.all(coords >= lo - 1e-9)
    assert np.all(coords <= hi + 1e-9)
    # not a vacuous containment check: the atoms actually fill a
    # meaningfully smaller sub-volume than the envelope (a real fit, not
    # an envelope so big anything would pass).
    atom_diag = float(np.linalg.norm(coords.max(axis=0) - coords.min(axis=0)))
    envelope_diag = float(np.linalg.norm(hi - lo))
    assert atom_diag < 0.5 * envelope_diag


def test_atomic3d_json_non_atomic_design_has_no_blocks(
    atomic3d_client, runtime_with_store
) -> None:
    _seed_plain_se(runtime_with_store, "plain_se")
    r = atomic3d_client.get("/se/plain_se/atomic3d.json")
    assert r.status_code == 200
    body = r.json()
    assert body["blocks"] == []
    assert body["deviation_max"] == 0.0
    assert body["scale"] > 0.0


def test_atomic3d_json_unknown_design_404s(atomic3d_client) -> None:
    r = atomic3d_client.get("/se/nope/atomic3d.json")
    assert r.status_code == 404


def test_detail3d_page_has_smooth_slider_only_for_the_atomic_design(
    atomic3d_client, runtime_with_store
) -> None:
    _seed_c60_structure(runtime_with_store, "c60frag3")
    _seed_atomic_se(runtime_with_store, slug="c60design3", structure_slug="c60frag3")
    _seed_plain_se(runtime_with_store, "plain_se2")

    atomic_page = atomic3d_client.get("/se/c60design3")
    assert atomic_page.status_code == 200
    assert 'id="bt3d-smooth"' in atomic_page.text
    assert "atomic3d.json" in atomic_page.text

    plain_page = atomic3d_client.get("/se/plain_se2")
    assert plain_page.status_code == 200
    assert 'id="bt3d-smooth"' not in plain_page.text


def test_detail3d_page_has_atoms_toggle_only_for_the_atomic_design(
    atomic3d_client, runtime_with_store
) -> None:
    """Reto, 2026-09-29: atoms need an off switch. The slider does not
    provide one — its far end swaps the atoms for the SMOOTHED SURFACE,
    which is still the structure, never the plain block envelope. Gated
    on ``has_atomic`` exactly like the slider, so a box-only design pays
    for neither control."""
    _seed_c60_structure(runtime_with_store, "c60frag4")
    _seed_atomic_se(runtime_with_store, slug="c60design4", structure_slug="c60frag4")
    _seed_plain_se(runtime_with_store, "plain_se3")

    atomic_page = atomic3d_client.get("/se/c60design4")
    assert atomic_page.status_code == 200
    assert 'id="bt3d-atoms"' in atomic_page.text
    assert 'document.getElementById("bt3d-atoms")' in atomic_page.text

    plain_page = atomic3d_client.get("/se/plain_se3")
    assert plain_page.status_code == 200
    assert 'id="bt3d-atoms"' not in plain_page.text


def test_atomic3d_json_strain_layers_on_c60(
    atomic3d_client, runtime_with_store
) -> None:
    """The viewer's strain layers: one bond strain per bond and two
    angle strains per atom. Ideal C60 pins both angle measures: θp is the
    textbook 11.6°, and every atom's three bond angles are 108°, 120°,
    120° (one pentagon, two hexagons), an RMS deviation of √48 ≈ 6.93°."""
    _seed_c60_structure(runtime_with_store, "c60frag8")
    _seed_atomic_se(runtime_with_store, slug="c60design8", structure_slug="c60frag8")
    block = atomic3d_client.get("/se/c60design8/atomic3d.json").json()["blocks"][0]

    assert len(block["bond_dev"]) == len(block["bonds"]) == 90
    assert len(block["angle_strain_thetap"]) == 60
    assert len(block["angle_strain_120"]) == 60
    assert np.allclose(block["angle_strain_thetap"], 11.6, atol=0.2)
    assert np.allclose(block["angle_strain_120"], 48**0.5, atol=0.3)
    # C60's bonds are 1.40/1.45 Å around the 1.42 Å reference
    assert max(block["bond_dev"]) < 0.06


def _hexagon(
    bond_A: float = 1.42,
) -> tuple[list[str], np.ndarray, list[tuple[int, int]]]:
    ang = np.radians(60.0 * np.arange(6))
    cart = np.stack([bond_A * np.cos(ang), bond_A * np.sin(ang), np.zeros(6)], axis=1)
    return ["C"] * 6, cart, [(k, (k + 1) % 6) for k in range(6)]


def test_strain_arrays_ideal_bond_is_zero_and_non_carbon_is_omitted() -> None:
    from precis_web.routes.blocktree_view import _strain_arrays

    elements, cart, bonds = _hexagon()
    out = _strain_arrays(elements, cart, bonds)
    assert out["bond_dev"] == pytest.approx([0.0] * 6, abs=1e-12)
    # a lone ring is 2-coordinated throughout: no angle strain applies
    assert "angle_strain_thetap" not in out
    assert "angle_strain_120" not in out

    # a C–N bond has no reference length: None, the C–C bonds still scored
    mixed = ["N"] + elements[1:]
    dev = _strain_arrays(mixed, cart * (1.5 / 1.42), bonds)["bond_dev"]
    assert dev[0] is None and dev[5] is None
    assert dev[1] == pytest.approx(0.08)

    assert _strain_arrays(["Si"] * 6, cart, bonds) == {}


def test_detail3d_page_has_strain_layer_rows_for_the_atomic_design(
    atomic3d_client, runtime_with_store
) -> None:
    _seed_c60_structure(runtime_with_store, "c60frag9")
    _seed_atomic_se(runtime_with_store, slug="c60design9", structure_slug="c60frag9")
    _seed_plain_se(runtime_with_store, "plain_se9")
    page = atomic3d_client.get("/se/c60design9").text
    for el in (
        "bt3d-layer-bond",
        "bt3d-layer-angle",
        "bt3d-dev-threshold",
        "bt3d-angle-measure",
    ):
        assert f'id="{el}"' in page
    assert 'id="bt3d-layer-bond"' not in atomic3d_client.get("/se/plain_se9").text


_CYL_MERIDIAN = [[0.0, -5.0], [10.0, -5.0], [10.0, 5.0], [0.0, 5.0]]


def _stamp_surface_meridian(runtime_with_store, slug: str, meridian: Any) -> None:
    store = runtime_with_store.hub.store
    ref = store.get_ref(kind="structure", id=slug)
    assert ref is not None
    store.stamp_ref_meta(ref.id, {"generated": {"surface_meridian": meridian}})


def test_atomic3d_json_target_surface_is_the_revolved_meridian_placed_like_smooth(
    atomic3d_client, runtime_with_store
) -> None:
    from precis_surface.revolution import revolve

    _seed_c60_structure(runtime_with_store, "c60frag5")
    _stamp_surface_meridian(runtime_with_store, "c60frag5", _CYL_MERIDIAN)
    _seed_atomic_se(runtime_with_store, slug="c60design5", structure_slug="c60frag5")

    body = atomic3d_client.get("/se/c60design5/atomic3d.json").json()
    block = body["blocks"][0]
    target = block["target"]

    verts_A, tris = revolve(np.array(_CYL_MERIDIAN), 96)
    xf = cad_pose(cad_as_vec3([0.0, 0.0, 0.0]), cad_as_vec3([0.0, 0.0, 0.0]))
    expect = apply_rigid(xf, verts_A * 1e-10) * body["scale"]
    got = np.array(target["verts"])
    assert got.shape == expect.shape
    assert np.allclose(got, expect, rtol=0, atol=1e-12 * max(1.0, body["scale"]))
    assert np.array_equal(np.array(target["tris"]), tris)
    # Placement: radius 10 Å and half-height 5 Å in display units.
    radial = np.hypot(got[:, 0], got[:, 1]).max()
    assert radial == pytest.approx(10e-10 * body["scale"], rel=1e-9)
    assert got[:, 2].max() == pytest.approx(5e-10 * body["scale"], rel=1e-9)
    # Same frame as the atoms: C60 sits inside the cylinder.
    coords = np.array(block["coords"])
    assert np.hypot(coords[:, 0], coords[:, 1]).max() < radial


def test_atomic3d_json_target_surface_absent_without_meridian(
    atomic3d_client, runtime_with_store
) -> None:
    _seed_c60_structure(runtime_with_store, "c60frag6")
    _seed_atomic_se(runtime_with_store, slug="c60design6", structure_slug="c60frag6")
    block = atomic3d_client.get("/se/c60design6/atomic3d.json").json()["blocks"][0]
    assert "target" not in block


def test_detail3d_page_has_target_surface_toggle_for_the_atomic_design(
    atomic3d_client, runtime_with_store
) -> None:
    _seed_c60_structure(runtime_with_store, "c60frag7")
    _seed_atomic_se(runtime_with_store, slug="c60design7", structure_slug="c60frag7")
    _seed_plain_se(runtime_with_store, "plain_se7")
    assert 'id="bt3d-target"' in atomic3d_client.get("/se/c60design7").text
    assert 'id="bt3d-target"' not in atomic3d_client.get("/se/plain_se7").text


def test_atomic3d_payload_cache_etag_and_invalidation(
    atomic3d_client, runtime_with_store, monkeypatch
) -> None:
    """gr462703 — second request is served from the payload cache (no
    rebuild), an ETag round-trips to a 304, and a changed structure
    revision or ``ATOMIC3D_PAYLOAD_VERSION`` misses."""
    from precis_web.routes import blocktree_view as bv

    bv._ATOMIC3D_CACHE.clear()
    calls: list[int] = []
    real = bv.smooth_sheet

    def counting(*a: Any, **kw: Any) -> Any:
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(bv, "smooth_sheet", counting)
    _seed_c60_structure(runtime_with_store, "c60cache")
    _seed_atomic_se(
        runtime_with_store, slug="c60cachedesign", structure_slug="c60cache"
    )
    url = "/se/c60cachedesign/atomic3d.json"

    r1 = atomic3d_client.get(url)
    assert r1.status_code == 200
    assert len(calls) == 1
    etag = r1.headers["etag"]
    assert r1.headers["cache-control"] == "private, no-cache"

    r2 = atomic3d_client.get(url)
    assert r2.status_code == 200 and r2.json() == r1.json()
    assert len(calls) == 1  # cache hit
    assert r2.headers["etag"] == etag

    r3 = atomic3d_client.get(url, headers={"If-None-Match": etag})
    assert r3.status_code == 304
    assert r3.headers["etag"] == etag
    assert len(calls) == 1

    # A new payload version misses the cache and changes the ETag.
    monkeypatch.setattr(bv, "ATOMIC3D_PAYLOAD_VERSION", 2)
    r4 = atomic3d_client.get(url, headers={"If-None-Match": etag})
    assert r4.status_code == 200
    assert r4.headers["etag"] != etag
    assert len(calls) == 2
    monkeypatch.setattr(bv, "ATOMIC3D_PAYLOAD_VERSION", 1)

    # A new structure revision (an edit) misses and changes the ETag.
    StructureHandler(hub=runtime_with_store.hub).put(
        id="c60cache",
        text=json.dumps(
            {
                "cell": {"a": 40.0, "b": 40.0, "c": 40.0, "pbc": [False] * 3},
                "ops": [{"op": "add_atom", "element": "C", "cart": [1, 1, 1]}],
            }
        ),
    )
    r5 = atomic3d_client.get(url, headers={"If-None-Match": etag})
    assert r5.status_code == 200
    assert r5.headers["etag"] != etag
    assert len(calls) == 3


def test_atomic3d_partial_failure_is_not_cacheable(
    atomic3d_client, runtime_with_store, monkeypatch
) -> None:
    """A block whose payload build raises is skipped, and the partial
    response goes out with no ETag and ``no-store`` — revalidating it would
    serve the broken overlay as a 304 until the structure changed."""
    from precis_web.routes import blocktree_view as bv

    bv._ATOMIC3D_CACHE.clear()

    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("ring perception failed")

    monkeypatch.setattr(bv, "smooth_sheet", boom)
    _seed_c60_structure(runtime_with_store, "c60broken")
    _seed_atomic_se(
        runtime_with_store, slug="c60brokendesign", structure_slug="c60broken"
    )
    r = atomic3d_client.get("/se/c60brokendesign/atomic3d.json")
    assert r.status_code == 200
    assert r.json()["blocks"] == []
    assert "etag" not in r.headers
    assert r.headers["cache-control"] == "no-store"
