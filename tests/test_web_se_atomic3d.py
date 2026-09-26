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
