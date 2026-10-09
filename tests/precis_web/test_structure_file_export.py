"""Coordinate-file downloads from the web viewers: ``/structure/{slug}/export.
{xyz,pdb,cif}`` and ``/se/{slug}/atoms.{xyz,pdb}`` (docs/backlog/
se-viewer-structure-file-export.md). Round-trips through ``ase.io.read``."""

from __future__ import annotations

import io
import json

import numpy as np
import pytest

pytest.importorskip("fastapi")
ase_io = pytest.importorskip("ase.io")

from fastapi.testclient import TestClient

from precis.handlers.structure import StructureHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.test_web_se_atomic3d import (
    _C60,
    _SE_MIGRATIONS,
    _apply_migrations,
    _seed_atomic_se,
    _seed_c60_structure,
    _seed_plain_se,
)


@pytest.fixture
def web(store, runtime_with_store, tmp_path) -> TestClient:
    _apply_migrations(store, _SE_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _read(text: str, fmt: str):
    return ase_io.read(io.StringIO(text), format=fmt)


def _seed_periodic(runtime_with_store, slug: str) -> None:
    StructureHandler(hub=runtime_with_store.hub).put(
        id=slug,
        text=json.dumps(
            {
                "cell": {"a": 4.0, "b": 4.0, "c": 4.0, "pbc": [True, True, True]},
                "ops": [
                    {"op": "add_atom", "element": "Cu", "cart": [0.0, 0.0, 0.0]},
                    {"op": "add_atom", "element": "Cu", "cart": [2.0, 2.0, 0.0]},
                ],
            }
        ),
    )


def test_structure_export_xyz_and_pdb_round_trip(
    web,
    runtime_with_store,
) -> None:
    _seed_c60_structure(runtime_with_store, "c60exp")
    for fmt, ase_fmt in (("xyz", "extxyz"), ("pdb", "proteindatabank")):
        r = web.get(f"/structure/c60exp/export.{fmt}")
        assert r.status_code == 200
        assert (
            r.headers["content-disposition"] == f'attachment; filename="c60exp.{fmt}"'
        )
        atoms = _read(r.text, ase_fmt)
        assert len(atoms) == 60
        got = np.sort(atoms.get_positions().round(3), axis=0)
        want = np.sort(np.asarray(_C60.coords).round(3), axis=0)
        assert np.allclose(got, want, atol=1e-3)


def test_structure_export_links_follow_periodicity(
    web,
    runtime_with_store,
) -> None:
    _seed_c60_structure(runtime_with_store, "c60links")
    _seed_periodic(runtime_with_store, "cubulk")

    free = web.get("/structure/c60links").text
    assert 'id="sv-export-xyz"' in free
    assert 'id="sv-export-pdb"' in free
    assert 'id="sv-export-cif"' not in free

    bulk = web.get("/structure/cubulk").text
    assert 'id="sv-export-xyz"' in bulk
    assert 'id="sv-export-cif"' in bulk
    assert 'id="sv-export-pdb"' not in bulk

    r = web.get("/structure/cubulk/export.cif")
    assert r.status_code == 200
    atoms = _read(r.text, "cif")
    assert len(atoms) == 2
    assert all(atoms.pbc)


def test_structure_export_404s(web, runtime_with_store) -> None:
    _seed_c60_structure(runtime_with_store, "c60four")
    assert web.get("/structure/nope/export.xyz").status_code == 404
    assert web.get("/structure/c60four/export.mol").status_code == 404


def test_se_atoms_xyz_one_block_60_atoms_inside_envelope(
    web,
    runtime_with_store,
) -> None:
    _seed_c60_structure(runtime_with_store, "c60se")
    _seed_atomic_se(runtime_with_store, slug="c60sedesign", structure_slug="c60se")

    r = web.get("/se/c60sedesign/atoms.xyz")
    assert r.status_code == 200
    assert (
        r.headers["content-disposition"]
        == 'attachment; filename="c60sedesign-atoms.xyz"'
    )
    atoms = _read(r.text, "extxyz")
    assert len(atoms) == 60

    # sphere:r5e-9 at the origin is r = 50 Å; C60 (r ~ 3.5 Å) sits well inside.
    pos = atoms.get_positions()
    assert np.linalg.norm(pos, axis=1).max() < 50.0
    assert np.allclose(
        np.sort(pos.round(3), axis=0),
        np.sort(np.asarray(_C60.coords).round(3), axis=0),
        atol=1e-3,
    )

    pdb = web.get("/se/c60sedesign/atoms.pdb")
    assert pdb.status_code == 200
    assert len(_read(pdb.text, "proteindatabank")) == 60
    assert pdb.text.splitlines()[0][21] == "A"


def test_se_atoms_404s(web, runtime_with_store) -> None:
    _seed_plain_se(runtime_with_store, "plainse")
    assert web.get("/se/plainse/atoms.xyz").status_code == 404
    assert web.get("/se/nope/atoms.xyz").status_code == 404
    _seed_c60_structure(runtime_with_store, "c60se404")
    _seed_atomic_se(runtime_with_store, slug="c60se404d", structure_slug="c60se404")
    assert web.get("/se/c60se404d/atoms.cif").status_code == 404


def test_se_viewer_shows_atom_buttons_only_when_bound(
    web,
    runtime_with_store,
) -> None:
    _seed_plain_se(runtime_with_store, "plainse2")
    # The viewer script names the ids unconditionally; the anchors are what
    # must be absent.
    assert 'id="bt3d-export-xyz"' not in web.get("/se/plainse2").text
    _seed_c60_structure(runtime_with_store, "c60se5")
    _seed_atomic_se(runtime_with_store, slug="c60se5d", structure_slug="c60se5")
    page = web.get("/se/c60se5d").text
    assert "bt3d-export-xyz" in page and "bt3d-export-pdb" in page
