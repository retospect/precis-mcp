"""``GET /se/{slug}/print/{block}.3mf`` — the browser download of the checked
print file — and the "Print files" section of the se reader page.

Web fixture pattern borrowed from ``tests/test_web_se_atomic3d.py``. Every
design slug is unique per test (shared, unisolated test DB)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_se
from precis.cad import mesh_check
from precis_se import printing as se_printing
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
def client(store, runtime_with_store, tmp_path) -> TestClient:
    _apply_migrations(store, _SE_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed(runtime_with_store, slug: str) -> None:
    """``hub``: a realized fdm cylinder (printable). ``bare``: a plain block
    with no mode (not fdm). ``todo``: fdm declared, never realized (nothing
    to print)."""
    handler = SeHandler(hub=runtime_with_store.hub)
    handler.put(
        id=slug,
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "hub", "envelope": "cyl:r0.02h0.01"},
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
    handler.edit(id=slug, ops=[{"op": "realize", "block": "hub", "mode": "fdm/pla"}])


def test_download_is_the_checked_3mf(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "webprint-ok")
    r = client.get("/se/webprint-ok/print/hub.3mf")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("model/3mf")
    assert (
        r.headers["content-disposition"]
        == 'attachment; filename="webprint-ok-hub-print.3mf"'
    )
    assert mesh_check.package_findings(r.content) == []
    assert int(r.headers["x-precis-print-errors"]) >= 0
    assert int(r.headers["x-precis-print-findings"]) >= int(
        r.headers["x-precis-print-errors"]
    )


def test_download_is_what_the_mcp_view_writes(
    client, runtime_with_store, tmp_path
) -> None:
    """Same bytes as ``view='print' fmt='3mf'`` — the zip members' payloads
    (the zip container itself may carry timestamps)."""
    import zipfile

    _seed(runtime_with_store, "webprint-same")
    web = client.get("/se/webprint-same/print/hub.3mf").content
    out = tmp_path / "hub.3mf"
    SeHandler(hub=runtime_with_store.hub).get(
        id="webprint-same",
        view="print",
        args={"block": "hub", "fmt": "3mf", "path": str(out)},
    )
    with (
        zipfile.ZipFile(out) as a,
        zipfile.ZipFile(__import__("io").BytesIO(web)) as b,
    ):
        assert sorted(a.namelist()) == sorted(b.namelist())
        for name in a.namelist():
            assert a.read(name) == b.read(name), name


def test_stl_download(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "webprint-stl")
    r = client.get("/se/webprint-stl/print/hub.stl")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("model/stl")
    assert 'filename="webprint-stl-hub-print.stl"' in r.headers["content-disposition"]
    assert len(r.content) > 84


def test_unknown_design_and_block_are_404(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "webprint-404")
    assert client.get("/se/webprint-nope/print/hub.3mf").status_code == 404
    assert client.get("/se/webprint-404/print/ghost.3mf").status_code == 404
    assert client.get("/se/webprint-404/print/hub.obj").status_code == 404


def test_unprintable_blocks_are_409_with_text(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "webprint-409")
    r = client.get("/se/webprint-409/print/bare.3mf")
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("text/plain")
    assert "fdm" in r.text
    r = client.get("/se/webprint-409/print/todo.3mf")
    assert r.status_code == 409
    assert "nothing to export" in r.text


def test_page_lists_printable_block_only(client, runtime_with_store) -> None:
    _seed(runtime_with_store, "webprint-page")
    r = client.get("/se/webprint-page")
    assert r.status_code == 200
    assert "/se/webprint-page/print/hub.3mf" in r.text
    assert "Download 3MF" in r.text
    assert "/print/bare.3mf" not in r.text
    assert "/print/todo.3mf" not in r.text


def test_page_render_does_not_build_the_mesh(
    client, runtime_with_store, monkeypatch
) -> None:
    _seed(runtime_with_store, "webprint-lazy")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("page render built the print mesh")

    monkeypatch.setattr(se_printing, "build_print_mesh", boom)
    monkeypatch.setattr(se_printing, "_island_issue", boom)
    monkeypatch.setattr(se_printing, "_mesh_findings", boom)
    r = client.get("/se/webprint-lazy")
    assert r.status_code == 200
    assert "/se/webprint-lazy/print/hub.3mf" in r.text
