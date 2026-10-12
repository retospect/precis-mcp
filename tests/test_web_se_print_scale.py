"""Scaled print files over the web route: ``?scale=``, the ``-x<factor>``
file name, the 422/409 refusal order and the ``.json`` dialog descriptor.
Fixture pattern as ``test_web_se_print.py``; every slug is unique per test."""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_se
from precis_se import printscale
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.config import WebConfig

_SE_MIGRATIONS = Path(precis_se.__file__).parent / "migrations"
_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}


def _apply_migrations(store: Any, directory: Path) -> None:
    with store.pool.connection() as c:
        for sql in sorted(directory.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))


@pytest.fixture
def client(store, runtime_with_store, tmp_path) -> TestClient:
    _apply_migrations(store, _SE_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed(runtime_with_store, slug: str) -> None:
    """``cube``: a realized 1 mm fdm cube; ``nano``: a realized 5 nm one;
    ``bare``: no mode (not fdm); ``todo``: fdm declared, never realized."""
    handler = SeHandler(hub=runtime_with_store.hub)
    handler.put(
        id=slug,
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "cube",
                        "envelope": "box:w0.001d0.001h0.001",
                    },
                    {
                        "op": "add_block",
                        "name": "nano",
                        "envelope": "box:w0.000000005d0.000000005h0.000000005",
                    },
                    {
                        "op": "add_block",
                        "name": "bare",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "add_block",
                        "name": "todo",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {"op": "set_mode", "block": "todo", "mode": "fdm/pla"},
                ]
            }
        ),
    )
    handler.edit(
        id=slug,
        ops=[
            {"op": "realize", "block": "cube", "mode": "fdm/pla"},
            {"op": "realize", "block": "nano", "mode": "fdm/pla"},
        ],
    )


def _model(content: bytes) -> tuple[ET.Element, list[float]]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        model = ET.fromstring(zf.read("3D/3dmodel.model"))
    pts = np.array(
        [[float(v.attrib[a]) for a in "xyz"] for v in model.findall(".//m:vertex", _NS)]
    )
    return model, [float(x) for x in np.ptp(pts, axis=0)]


def test_scaled_download_is_named_sized_and_recorded(
    client, runtime_with_store
) -> None:
    _seed(runtime_with_store, "wps-scale")
    r = client.get("/se/wps-scale/print/cube.3mf?scale=100")
    assert r.status_code == 200, r.text
    assert (
        r.headers["content-disposition"]
        == 'attachment; filename="wps-scale-cube-x100.3mf"'
    )
    model, extent = _model(r.content)
    assert extent == pytest.approx([100.0] * 3)
    meta = {m.attrib["name"]: m.text for m in model.findall("m:metadata", _NS)}
    assert meta["precis:scale_factor"] == "100"


def test_scale_one_keeps_the_historical_name_and_size(
    client, runtime_with_store
) -> None:
    _seed(runtime_with_store, "wps-one")
    r = client.get("/se/wps-one/print/cube.3mf?scale=1")
    assert r.status_code == 200, r.text
    assert 'filename="wps-one-cube-print.3mf"' in r.headers["content-disposition"]
    model, extent = _model(r.content)
    assert extent == pytest.approx([1.0] * 3)
    assert model.find("m:metadata[@name='precis:scale_factor']", _NS) is None


def test_nanometre_block_at_2e7_is_100_mm(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "wps-nano")
    r = client.get("/se/wps-nano/print/nano.3mf?scale=2e7")
    assert r.status_code == 200, r.text
    assert 'filename="wps-nano-nano-x2e7.3mf"' in r.headers["content-disposition"]
    assert _model(r.content)[1] == pytest.approx([100.0] * 3, rel=1e-6)


def test_scaled_stl(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "wps-stl")
    r = client.get("/se/wps-stl/print/cube.stl?scale=100")
    assert r.status_code == 200, r.text
    assert 'filename="wps-stl-cube-x100.stl"' in r.headers["content-disposition"]


def test_web_and_agent_view_share_the_scaled_mesh(
    client, runtime_with_store, tmp_path
) -> None:
    _seed(runtime_with_store, "wps-agent")
    web = client.get("/se/wps-agent/print/cube.3mf?scale=100").content
    out = tmp_path / "cube.3mf"
    SeHandler(hub=runtime_with_store.hub).get(
        id="wps-agent",
        view="print",
        args={"block": "cube", "fmt": "3mf", "scale": 100, "path": str(out)},
    )
    agent = out.read_bytes()
    assert _model(agent)[1] == _model(web)[1] == pytest.approx([100.0] * 3)
    with zipfile.ZipFile(io.BytesIO(agent)) as a, zipfile.ZipFile(io.BytesIO(web)) as b:
        assert (
            a.read("3D/3dmodel.model").split(b"<resources>")[1]
            == b.read("3D/3dmodel.model").split(b"<resources>")[1]
        )


def test_handler_refuses_a_bad_scale(runtime_with_store) -> None:
    from precis.errors import BadInput

    _seed(runtime_with_store, "wps-hbad")
    with pytest.raises(BadInput, match="scale"):
        SeHandler(hub=runtime_with_store.hub).get(
            id="wps-hbad", view="print", args={"block": "cube", "scale": -1}
        )


@pytest.mark.parametrize("bad", ["abc", "nan", "inf", "0", "-2"])
def test_malformed_scale_is_422_invalid_scale(client, runtime_with_store, bad) -> None:
    _seed(runtime_with_store, "wps-bad")
    r = client.get(f"/se/wps-bad/print/cube.3mf?scale={bad}")
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["error"] == "invalid_scale"
    assert "min_scale" not in r.json()


def test_model_on_a_solid_and_unknown_model_are_422(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "wps-model")
    for model in ("vdw", "nonsense"):
        r = client.get(f"/se/wps-model/print/cube.3mf?model={model}")
        assert r.status_code == 422
        assert r.json()["error"] == "unsupported_model"


def test_below_floor_is_422_with_min_scale(
    client, runtime_with_store, monkeypatch
) -> None:
    _seed(runtime_with_store, "wps-floor")
    monkeypatch.setattr(printscale, "floor_for", lambda tree, block, model: 5e7)
    r = client.get("/se/wps-floor/print/nano.3mf?scale=2e7")
    assert r.status_code == 422
    assert r.json() == {"min_scale": 5e7}
    assert client.get("/se/wps-floor/print/nano.3mf?scale=5e7").status_code == 200
    desc = client.get("/se/wps-floor/print/nano.json").json()
    assert desc["min_scale"] == 5e7
    assert desc["suggested_scale"] == 5e7  # the floor wins over the 2e7 fit


def test_solid_has_no_floor_and_tiny_scale_reaches_the_check(
    client, runtime_with_store
) -> None:
    _seed(runtime_with_store, "wps-tiny")
    r = client.get("/se/wps-tiny/print/cube.3mf?scale=0.001")
    assert r.status_code == 200, r.text  # not refused: the print check reports


def test_refusal_order(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "wps-order")
    assert client.get("/se/wps-order/print/ghost.3mf?scale=abc").status_code == 404
    # a malformed scale is reported before the 409 of an unprintable block
    assert client.get("/se/wps-order/print/bare.3mf?scale=abc").status_code == 422
    assert client.get("/se/wps-order/print/bare.3mf?scale=5").status_code == 409


def test_known_unavailable_atoms_are_409_not_a_floor_error(
    client, runtime_with_store, monkeypatch
) -> None:
    _seed(runtime_with_store, "wps-atom")
    monkeypatch.setattr(printscale, "is_atomic", lambda tree, block: block == "bare")
    monkeypatch.setattr(printscale, "floor_for", lambda tree, block, model: 5e7)
    r = client.get("/se/wps-atom/print/bare.3mf?scale=2&model=vdw")
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("text/plain")
    assert "not printable yet" in r.text


def test_descriptor_for_a_5_nm_block(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "wps-json")
    r = client.get("/se/wps-json/print/nano.json")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["real_size_mm"] == pytest.approx([5e-6] * 3)
    assert body["suggested_scale"] == pytest.approx(2e7)
    assert body["min_scale"] is None
    assert body["models"] == []
    assert body["selected_model"] is None
    assert client.get("/se/wps-json/print/cube.json").json()["suggested_scale"] == 1.0
    assert client.get("/se/wps-json/print/ghost.json").status_code == 404
    assert client.get("/se/wps-json/print/cube.json?model=vdw").status_code == 422


def test_descriptor_of_an_unprintable_block_is_409(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "wps-json409")
    assert client.get("/se/wps-json409/print/bare.json").status_code == 409
    assert client.get("/se/wps-json409/print/todo.json").status_code == 409
