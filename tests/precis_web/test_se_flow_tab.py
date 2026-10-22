"""The se Flow tab (docs/backlog/se-flow-kernel.md slice 2b,
``routes/se_flow.py``): the page offers the design's ``flow:`` chamber
spec and points the player at ``flow.json``; the payload of the slice-2
hex chamber (a ``field:`` envelope with drilled floor holes, tip port
plates) carries one instance per traced particle, the port masks, the
tracking plane, per-particle outcomes and the regime card.

The JS half: ``scripts/se_flow_frames_smoke.mjs`` (frame advance,
interpolation, clock) via ``tests/test_se_flow_frames_js.py``; the pins
below hold the player's instance count to the payload's particle axis.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_se
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests._flow_hex import hex_chamber_field, hex_chamber_ops

_SE_MIGRATIONS = Path(precis_se.__file__).parent / "migrations"
_STATIC = Path(__file__).resolve().parents[2] / "src" / "precis_web" / "static"


def _apply_migrations(store: Any, directory: Path) -> None:
    with store.pool.connection() as c:
        for sql in sorted(directory.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))


@pytest.fixture
def flow_client(store, runtime_with_store, tmp_path) -> TestClient:
    _apply_migrations(store, _SE_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed_hex(runtime_with_store, store, slug: str = "hex_chamber") -> None:
    se = SeHandler(hub=runtime_with_store.hub)
    ops = hex_chamber_ops("0" * 64)
    se.put(id=slug, text=json.dumps({"ops": ops[1:3]}))  # the ref to own the grid
    ref = store.get_ref(kind="se", id=slug)
    sha = store.put_field(ref.id, hex_chamber_field(pitch=5e-4))
    se.put(id=slug, text=json.dumps({"ops": hex_chamber_ops(sha)}))


def _u8(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), dtype=np.uint8)


def test_flow_tab_loads_hex_chamber_payload(
    flow_client, runtime_with_store, store
) -> None:
    _seed_hex(runtime_with_store, store)
    page = flow_client.get("/se/hex_chamber/flow")
    assert page.status_code == 200
    assert "/static/se-flow-player.js" in page.text
    assert "/se/hex_chamber/flow.json?block=chamber&amp;pocket=flow" in page.text or (
        "/se/hex_chamber/flow.json?block=chamber\\u0026pocket=flow" in page.text
    )
    # the 3-D view links the tab
    assert "/se/hex_chamber/flow" in flow_client.get("/se/hex_chamber").text

    r = flow_client.get(
        "/se/hex_chamber/flow.json",
        params={
            "block": "chamber",
            "pocket": "flow",
            "n": 300,
            "frames": 24,
            "nx": 100,
            "u0": 0.01,
            "mu": 1e-3,
            "rho": 1000,
        },
    )
    assert r.status_code == 200, r.text
    p = r.json()
    fr = p["frames"]
    # the player draws one instance per particle on the frames' n axis
    assert fr["shape"] == [24, 300, 3]
    assert p["particles"]["n"] == fr["shape"][1]
    frames = np.frombuffer(base64.b64decode(fr["data_b64"]), dtype="<f4")
    assert frames.size == 24 * 300 * 3
    mask = _u8(p["mesh"]["mask"]["data_b64"])
    inlet = _u8(p["mesh"]["ports"]["inlet_b64"])
    outlet = _u8(p["mesh"]["ports"]["outlet_b64"])
    assert inlet.sum() > 0 and outlet.sum() > 0
    assert not (inlet & ~mask.astype(bool)).any()
    depth = np.frombuffer(
        base64.b64decode(p["mesh"]["floor_depth"]["data_b64"]), dtype="<f4"
    )
    assert depth.max() > 1.0  # the drilled holes read as floor topography
    assert p["z_plane_star"] == pytest.approx(0.99)
    assert p["profile"] == "plug" and p["pocket"] == "flow"
    assert p["card"] is not None and p["card"]["ok"]
    assert p["scale"]["u0"] == pytest.approx(0.01)

    # a size change re-judges the card on the cached trace
    big = flow_client.get(
        "/se/hex_chamber/flow.json",
        params={
            "n": 300,
            "frames": 24,
            "nx": 100,
            "size": 0.5,
            "u0": 0.05,
            "mu": 1e-3,
            "rho": 1000,
        },
    ).json()
    assert big["scale"]["L"] == pytest.approx(0.5)
    assert big["scale"]["geometry_L"] == pytest.approx(p["scale"]["geometry_L"])
    assert not big["card"]["ok"]  # the spec's refused worked point
    assert big["frames"]["data_b64"] == fr["data_b64"]


def test_flow_json_refusals(flow_client, runtime_with_store, store) -> None:
    assert flow_client.get("/se/nope/flow.json").status_code == 404
    assert flow_client.get("/se/nope/flow").status_code == 404
    SeHandler(hub=runtime_with_store.hub).put(
        id="plain",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "b", "envelope": "box:w0.1d0.03h0.001"}
                ]
            }
        ),
    )
    r = flow_client.get("/se/plain/flow.json")
    assert r.status_code == 422 and "flow chamber spec" in r.json()["error"]
    assert "flow:in=" in r.json()["next"]
    page = flow_client.get("/se/plain/flow")
    assert page.status_code == 200 and "no flow chamber spec" in page.text
    assert "/se/plain/flow" not in flow_client.get("/se/plain").text
    bad = flow_client.get("/se/plain/flow.json", params={"n": "many"})
    assert bad.status_code == 422 and "n must be a number" in bad.json()["error"]


def test_player_instance_count_is_the_payload_particle_axis() -> None:
    js = (_STATIC / "se-flow-player.js").read_text(encoding="utf-8")
    assert "new THREE.InstancedMesh(\n      sphere," in js
    assert "      d.n,\n    );" in js
    assert "particles.castShadow = true" in js
    assert "floor.receiveShadow = true" in js
    frames = (_STATIC / "se-flow-frames.js").read_text(encoding="utf-8")
    assert "const [K, n] = fr ? fr.shape : [0, 0];" in frames
