"""``GET /se/{slug}/print/{block}.3mf`` — the browser download of the checked
print file — and the "Print files" section of the se reader page.

Web fixture pattern borrowed from ``tests/test_web_se_atomic3d.py``. Every
design slug is unique per test (shared, unisolated test DB)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

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
    # gr537832 — the report is fetched after first paint; the shell
    # carries only the empty slot.
    assert 'id="bt3d-print-files"' in r.text
    assert 'data-url="/se/webprint-page/print-panel"' in r.text
    frag = client.get("/se/webprint-page/print-panel")
    assert frag.status_code == 200
    assert "/se/webprint-page/print/hub.3mf" in frag.text
    assert "Download 3MF" in frag.text
    assert "/print/bare.3mf" not in frag.text
    assert "/print/todo.3mf" not in frag.text


def test_page_render_never_calls_print_files(
    client, runtime_with_store, monkeypatch
) -> None:
    """gr537832 — the report pays printsolid's ray-cast volume per block
    (minutes on a realized design), so the page route must not call
    ``print_files`` at all; it renders the empty slot only."""
    from precis_web.routes import se_print as web_se_print

    _seed(runtime_with_store, "webprint-nopay")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("page render ran the print report")

    monkeypatch.setattr(web_se_print, "print_files", boom)
    r = client.get("/se/webprint-nopay")
    assert r.status_code == 200
    assert 'id="bt3d-print-files"' in r.text


def test_print_panel_is_404_for_an_unknown_design(client) -> None:
    assert client.get("/se/nope/print-panel").status_code == 404


def test_print_panel_second_call_hits_the_cache(
    client, runtime_with_store, monkeypatch
) -> None:
    """The panel is cached by the design's validation identity: a second
    fetch of an unchanged design does not rerun the report."""
    from precis_web.routes import blocktree_view as bv
    from precis_web.routes import se_print as web_se_print

    _seed(runtime_with_store, "webprint-cache")
    bv._PRINT_PANEL.clear()
    calls = 0
    real = web_se_print.print_files

    def spy(*a: Any, **k: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*a, **k)

    monkeypatch.setattr(web_se_print, "print_files", spy)
    r1 = client.get("/se/webprint-cache/print-panel")
    r2 = client.get("/se/webprint-cache/print-panel")
    assert r1.status_code == r2.status_code == 200
    assert r2.text == r1.text
    assert calls == 1


def test_print_panel_does_not_build_the_mesh(
    client, runtime_with_store, monkeypatch
) -> None:
    """The panel lists from the cheap report: the full mesh check runs
    only when a download link is clicked."""
    _seed(runtime_with_store, "webprint-lazy")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("panel render built the print mesh")

    monkeypatch.setattr(se_printing, "build_print_mesh", boom)
    monkeypatch.setattr(se_printing, "_island_issue", boom)
    monkeypatch.setattr(se_printing, "_mesh_findings", boom)
    r = client.get("/se/webprint-lazy/print-panel")
    assert r.status_code == 200
    assert "/se/webprint-lazy/print/hub.3mf" in r.text


def test_print_panel_concurrent_requests_compute_once(
    client, runtime_with_store, monkeypatch
) -> None:
    """gr537832 — a reload while the first (minutes-long) compute runs must
    join it, not start another ``print_files``."""
    import asyncio
    import threading
    import time

    import httpx

    from precis_web.routes import blocktree_view as bv
    from precis_web.routes import se_print as web_se_print

    _seed(runtime_with_store, "webprint-dedup")
    bv._PRINT_PANEL.clear()
    bv._PRINT_INFLIGHT.clear()
    calls = 0
    lock = threading.Lock()
    real = web_se_print.print_files

    def slow(*a: Any, **k: Any) -> Any:
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.5)
        return real(*a, **k)

    monkeypatch.setattr(web_se_print, "print_files", slow)

    async def go() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            return list(
                await asyncio.gather(
                    *(ac.get("/se/webprint-dedup/print-panel") for _ in range(3))
                )
            )

    rs = asyncio.run(go())
    assert [r.status_code for r in rs] == [200, 200, 200]
    assert len({r.text for r in rs}) == 1
    assert calls == 1
    assert bv._PRINT_INFLIGHT == {}


def test_print_panel_error_is_not_cached(
    client, runtime_with_store, monkeypatch
) -> None:
    from precis_web.routes import blocktree_view as bv
    from precis_web.routes import se_print as web_se_print

    _seed(runtime_with_store, "webprint-err")
    bv._PRINT_PANEL.clear()
    bv._PRINT_INFLIGHT.clear()
    real = web_se_print.print_files
    calls = 0

    def flaky(*a: Any, **k: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(web_se_print, "print_files", flaky)
    assert client.get("/se/webprint-err/print-panel").status_code == 500
    assert bv._PRINT_INFLIGHT == {}
    assert client.get("/se/webprint-err/print-panel").status_code == 200
    assert calls == 2


def test_print_panel_cache_invalidates_after_a_design_edit(
    client, runtime_with_store, monkeypatch
) -> None:
    """An edit changes the validation identity, so the next fetch reruns
    the report instead of serving the stale entry."""
    from precis_web.routes import blocktree_view as bv
    from precis_web.routes import se_print as web_se_print

    _seed(runtime_with_store, "webprint-edit")
    bv._PRINT_PANEL.clear()
    calls = 0
    real = web_se_print.print_files

    def spy(*a: Any, **k: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*a, **k)

    monkeypatch.setattr(web_se_print, "print_files", spy)
    client.get("/se/webprint-edit/print-panel")
    client.get("/se/webprint-edit/print-panel")
    assert calls == 1
    SeHandler(hub=runtime_with_store.hub).edit(
        id="webprint-edit",
        ops=[{"op": "add_block", "name": "extra", "envelope": "box:w0.01d0.01h0.01"}],
    )
    client.get("/se/webprint-edit/print-panel")
    assert calls == 2


def test_se_print_key_is_uncacheable_on_a_transient_cad_sha_failure(
    monkeypatch,
) -> None:
    """A bound cad design whose expansion raised keys to ``None`` — a
    stable-looking key that must not remember the result."""
    from types import SimpleNamespace

    from precis_web.routes import blocktree_view as bv

    tree = SimpleNamespace(
        blocks={
            "a": SimpleNamespace(bound_kind="cad", bound="d1"),
            "b": SimpleNamespace(bound_kind="cad", bound="d2"),
        }
    )
    monkeypatch.setattr(bv, "_validation_identity", lambda *_a: "ident")
    shas = {"d1": ("abc", False), "d2": (None, True)}
    monkeypatch.setattr(bv, "_bound_cad_sha", lambda _s, slug: shas[slug])
    key, cacheable = bv._se_print_key(cast(Any, None), 7, tree, 3)
    assert cacheable is False
    assert key == (7, "ident", (("d1", "abc"), ("d2", None)))
    shas["d2"] = (None, False)  # unresolved binding is stable: cacheable
    assert bv._se_print_key(cast(Any, None), 7, tree, 3)[1] is True
