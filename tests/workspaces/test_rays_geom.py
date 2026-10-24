"""``pga.ray`` / ``out.ray`` — structure-workspaces slice 2r script side
(DB-free): units, ``along``/``axis`` builders, the manifest ``rays`` row
and its round-trip through the importer's validator."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from precis.errors import BadInput
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_geom import assembly as pga
from precis_geom import out as geom_out
from precis_geom import trace as _trace
from precis_se.workspace_import import _vet_manifest


def _chain() -> Scene:
    """4 C atoms on the z axis, 1.5 Å apart (extent 4.5 Å)."""
    cell = Cell(np.eye(3) * 50.0, (False, False, False))
    atoms = {}
    for i in range(4):
        la = f"aC{i + 1}"
        atoms[la] = Atom(
            label=la, element="C", frac=np.array([0.5, 0.5, (10.0 + 1.5 * i) / 50.0])
        )
    return Scene(cell=cell, atoms=atoms)


def test_units_bare_metres_vs_string() -> None:
    a = pga.ray("r", [0, 0, -2e-9], [0, 0, 2], 1.2e-8, block="b")
    b = pga.ray("r", "[0 0 -2] nm", [0, 0, 1], "12 nm", block="b")
    assert a.origin == pytest.approx((0.0, 0.0, -2e-9))
    assert b.origin == pytest.approx(a.origin)
    assert a.length == pytest.approx(1.2e-8)
    assert b.length == pytest.approx(1.2e-8)
    assert a.direction == (0.0, 0.0, 1.0)  # normalised
    assert a.kind == "probe" and a.frame_in is None


def test_bad_direction_and_length() -> None:
    with pytest.raises(BadInput, match="nonzero"):
        pga.ray("r", [0, 0, 0], [0, 0, 0], 1e-9, block="b")
    with pytest.raises(BadInput, match="> 0"):
        pga.ray("r", [0, 0, 0], [0, 0, 1], 0.0, block="b")


def test_along_port() -> None:
    g = pga.geom(
        _chain(),
        ports={"tail": {"pos": (0.0, 0.0, 0.0)}},
        meta={"name": "p"},
    )
    r = pga.ray.along(g.port("tail"), "5 nm", name="bolt")
    assert r.frame_in == "port:tail"
    assert r.block == "p" and r.anchor == ("p.tail",)
    assert r.origin == (0.0, 0.0, 0.0) and r.direction == (0.0, 0.0, 1.0)
    assert r.kind == "bolt_axis" and r.length == pytest.approx(5e-9)


def test_axis_pca() -> None:
    g = pga.geom(_chain(), meta={"name": "chain"})
    r = pga.ray.axis(g, name="ax")
    assert r.block == "chain"
    assert r.direction == pytest.approx((0.0, 0.0, 1.0), abs=1e-9)
    assert r.length == pytest.approx((4.5 + 4.0) * 1e-10)
    # origin = centroid(z=12.25 Å) - (4.5/2 + 2) Å along +z
    assert r.origin[2] == pytest.approx((12.25 - 4.25) * 1e-10)
    with pytest.raises(BadInput, match="needs atoms"):
        pga.ray.axis(pga.Geom(template="t"), name="x")


def test_run_writes_manifest_rays(tmp_path: Path) -> None:
    run = geom_out.Run(tmp_path)
    with _trace.run():
        run.block("p", _chain())
        r = pga.ray("r1", "[0 0 -2] nm", [0, 0, 1], "12 nm", block="p", step=0.5)
        run.ray(r)
        run.ray(pga.ray("r2", [0, 0, 0], [1, 0, 0], 1e-9, block="p", frame="world"))
        with pytest.raises(ValueError, match="duplicate"):
            run.ray(r)
        with pytest.raises(ValueError, match="not declared"):
            run.ray("r3", [0, 0, 0], [1, 0, 0], 1e-9, block="nope")
    run.finish()
    doc = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    row = doc["rays"][0]
    assert set(row) == {
        "name",
        "block",
        "origin",
        "direction",
        "length",
        "kind",
        "step",
        "frame",
    }
    assert row["name"] == "r1" and row["block"] == "p"
    assert row["origin"] == pytest.approx([0.0, 0.0, -2e-9])
    assert row["direction"] == [0.0, 0.0, 1.0]
    assert row["length"] == pytest.approx(1.2e-8)
    assert row["step"] == 0.5 and row["kind"] == "probe"
    assert isinstance(row["frame"], int)
    assert doc["rays"][1]["in"] == "world"
    vetted = _vet_manifest(doc)["rays"]
    assert [v["name"] for v in vetted] == ["r1", "r2"]
