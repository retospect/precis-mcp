"""gr504830 — the shareable structure package: ``/se/{slug}/package/{block}.png``
(and ``.svg``) plus ``/structure/{slug}/package.png``. The caption must also
ride in the file's metadata, so a cropped copy still says what it is."""

from __future__ import annotations

import io
import json
import struct
import zlib

import pytest

pytest.importorskip("fastapi")
Image = pytest.importorskip("PIL.Image")

from fastapi.testclient import TestClient

from precis.handlers.structure import StructureHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.structure_package import with_text_chunks
from tests.test_web_se_atomic3d import (
    _SE_MIGRATIONS,
    _apply_migrations,
    _seed_atomic_se,
    _seed_c60_structure,
    _seed_plain_se,
)


def _png_chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    out, pos = [], 8
    while pos < len(png):
        (n,) = struct.unpack(">I", png[pos : pos + 4])
        kind, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + n]
        (crc,) = struct.unpack(">I", png[pos + 8 + n : pos + 12 + n])
        assert crc == zlib.crc32(kind + data)
        out.append((kind, data))
        pos += 12 + n
    return out


def test_text_chunks_land_after_ihdr_and_read_back_as_utf8() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (4, 3), "white").save(buf, format="PNG")
    png = with_text_chunks(buf.getvalue(), {"Title": "Å θ", "Empty": ""})
    kinds = [k for k, _d in _png_chunks(png)]
    assert kinds[:2] == [b"IHDR", b"iTXt"]
    assert kinds.count(b"iTXt") == 1  # empty values are skipped
    img = Image.open(io.BytesIO(png))
    assert img.size == (4, 3)
    assert img.text["Title"] == "Å θ"
    with pytest.raises(ValueError):
        with_text_chunks(b"not a png", {"Title": "x"})


@pytest.fixture
def web(store, runtime_with_store, tmp_path) -> TestClient:
    _apply_migrations(store, _SE_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_se_block_package_png_carries_caption_in_metadata(
    web, runtime_with_store
) -> None:
    _seed_c60_structure(runtime_with_store, "pkg-c60")
    StructureHandler(hub=runtime_with_store.hub).edit(
        id="pkg-c60", ops=[{"op": "relax", "fidelity": "clean"}]
    )
    _seed_atomic_se(runtime_with_store, slug="pkg-design", structure_slug="pkg-c60")

    r = web.get("/se/pkg-design/package/hub.png")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert "inline" in r.headers["content-disposition"]
    img = Image.open(io.BytesIO(r.content))
    assert img.size[0] == 1520  # 760 user units at zoom 2
    assert img.size[1] > (500 + 30) * 2 + 100  # picture + legend + caption band
    text = img.text
    assert text["Title"].startswith("hub — ")
    assert text["Source"].endswith("/structure/pkg-c60")
    desc = text["Description"]
    assert "ref: se:pkg-design~hub · structure:pkg-c60" in desc
    assert "atoms: C60 · 60 atoms" in desc
    assert "relax: geometry repair only · clean" in desc
    record = json.loads(text["Comment"])
    assert record["design"] == "pkg-design"
    assert record["block"] == "hub"
    assert record["structure"] == "pkg-c60"
    assert record["relax"]["state"] == "repaired"

    svg = web.get("/se/pkg-design/package/hub.svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert "<desc>" in svg.text and "geometry repair only" in svg.text
    assert 'stroke-dasharray="6 4"' in svg.text  # the crop rule


def test_structure_package_and_links(web, runtime_with_store) -> None:
    _seed_c60_structure(runtime_with_store, "pkg-bare")
    r = web.get("/structure/pkg-bare/package.png")
    assert r.status_code == 200
    assert (
        "relax: no relax recorded"
        in Image.open(io.BytesIO(r.content)).text["Description"]
    )
    assert (
        'href="/structure/pkg-bare/package.png"' in web.get("/structure/pkg-bare").text
    )

    _seed_atomic_se(runtime_with_store, slug="pkg-links", structure_slug="pkg-bare")
    assert 'href="/se/pkg-links/package/hub.png"' in web.get("/se/pkg-links").text


def test_package_404s(web, runtime_with_store) -> None:
    _seed_plain_se(runtime_with_store, "pkg-plain")
    assert web.get("/se/pkg-plain/package/hub.png").status_code == 404  # unbound
    assert web.get("/se/pkg-plain/package/nope.png").status_code == 404
    assert web.get("/se/pkg-plain/package/hub.gif").status_code == 404
    assert web.get("/se/nope/package/hub.png").status_code == 404
    assert web.get("/structure/nope/package.png").status_code == 404
    _seed_c60_structure(runtime_with_store, "pkg-fmt")
    assert web.get("/structure/pkg-fmt/package.jpg").status_code == 404
