"""Mermaid web editor routes — FakeStore degradation + real-store integration.

The render path degrades to the source text when ``mermaidx`` is absent (the
gate does not bake the extra), so these assert on the source / structure, not
on a rendered SVG."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.diagram.turn import TurnResult
from precis_web.app import create_app
from precis_web.config import WebConfig

_FLOW = "flowchart TD\n  intake[Intake] --> ship[Ship]"


# ── FakeStore degradation ────────────────────────────────────────────────


def test_mermaid_index_redirects_to_drive_kind_mermaid(client: TestClient) -> None:
    """``/mermaid`` (the list) is retired into the unified Drive surface —
    it redirects to the ``kind=mermaid`` facet preset, the same target
    ``_drive_back.html.j2``'s back-link uses. The workbench
    (``/mermaid/{slug}``) is unaffected — ``test_mermaid_detail_404`` below
    proves the ``{slug}`` route still resolves and isn't swallowed by this
    redirect."""
    r = client.get("/mermaid", follow_redirects=False)
    assert r.status_code in (302, 307, 308)
    assert r.headers["location"] == "/drive?k=mermaid&folder=*&sort=recency"


def test_mermaid_detail_404(client: TestClient) -> None:
    r = client.get("/mermaid/nope")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


def test_mermaid_render_404(client: TestClient) -> None:
    r = client.get("/mermaid/nope/render.svg")
    assert r.status_code == 404


def test_mermaid_export_404(client: TestClient) -> None:
    r = client.get("/mermaid/nope/export.svg")
    assert r.status_code == 404


# ── real-store integration ───────────────────────────────────────────────


@pytest.fixture
def mm_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed(runtime_with_store, slug: str = "web_mm") -> None:
    from precis.handlers.mermaid import MermaidHandler

    MermaidHandler(hub=runtime_with_store.hub).put(
        id=slug, title="Web Flow", text=_FLOW, vocab="a two-step pipeline"
    )


def test_detail_shows_source_and_vocab(mm_client, runtime_with_store) -> None:
    _seed(runtime_with_store)
    r = mm_client.get("/mermaid/web_mm")
    assert r.status_code == 200
    assert "intake[Intake]" in r.text  # source shown (render degrades w/o engine)
    assert "a two-step pipeline" in r.text
    assert "Shared vocabulary" in r.text
    assert "Implementation notes" in r.text
    # the two-pane workbench with the shared pcb-workbench splitter
    for marker in (
        'id="mm-workbench"',
        'id="mm-pane-diagram"',
        'id="mm-divider"',
        'id="mm-pane-info"',
        "/static/pane-split.js",
    ):
        assert marker in r.text, marker


def test_export_mermaid_source(mm_client, runtime_with_store) -> None:
    """``export.mermaid`` downloads the raw source text as ``<slug>.mermaid``."""
    _seed(runtime_with_store)
    r = mm_client.get("/mermaid/web_mm/export.mermaid")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    assert 'filename="web_mm.mermaid"' in cd
    assert "intake[Intake]" in r.text


def test_export_svg_downloads_attachment(mm_client, runtime_with_store) -> None:
    """gr498614 — ``export.svg`` serves the same sanitized render the page
    inlines, as an ``image/svg+xml`` attachment named ``<slug>.svg`` whose
    body parses as SVG. mermaidx is a core dep, so the render is real."""
    _seed(runtime_with_store)
    r = mm_client.get("/mermaid/web_mm/export.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    assert 'filename="web_mm.svg"' in cd
    assert ET.fromstring(r.content).tag.endswith("svg")


def test_export_svg_sanitizes_hostile_content(
    mm_client, runtime_with_store, monkeypatch
) -> None:
    """gr498614 — the download carries the sanitized render: a hostile payload
    (``<script>``, ``foreignObject``, a ``javascript:`` href) in the rendered
    SVG never reaches the file. mermaid.js already escapes label text, so the
    payload is injected at the render boundary to exercise the sanitizer."""
    _seed(runtime_with_store)
    monkeypatch.setattr(
        "precis_web.routes.mermaid.render_svg",
        lambda _s: (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            "<script>alert(1)</script>"
            '<foreignObject><body xmlns="http://www.w3.org/1999/xhtml">x</body>'
            "</foreignObject>"
            '<a href="javascript:alert(1)"><text>x</text></a></svg>'
        ),
    )
    r = mm_client.get("/mermaid/web_mm/export.svg")
    assert r.status_code == 200
    assert "<script" not in r.text
    assert "foreignObject" not in r.text
    assert "javascript:" not in r.text
    assert ET.fromstring(r.content).tag.endswith("svg")


@pytest.mark.parametrize(
    ("fmt", "media"), [("png", "image/png"), ("pdf", "application/pdf")]
)
def test_export_png_pdf(mm_client, runtime_with_store, fmt, media) -> None:
    _seed(runtime_with_store)
    r = mm_client.get(f"/mermaid/web_mm/export.{fmt}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(media)
    assert f'filename="web_mm.{fmt}"' in r.headers["content-disposition"]


def test_export_unknown_fmt_404(mm_client, runtime_with_store) -> None:
    _seed(runtime_with_store)
    r = mm_client.get("/mermaid/web_mm/export.tiff")
    assert r.status_code == 404


def test_turn_route_returns_json(mm_client, runtime_with_store, monkeypatch) -> None:
    _seed(runtime_with_store, slug="web_turn")

    def fake_run_turn(store, ref, message, **kw):
        return TurnResult(
            reply=f"drew: {message}",
            svg="flowchart TD\n  a --> b",
            findings=[],
            changed=True,
            healed=False,
            vocab="v",
            notes="n",
            bindings=[],
        )

    monkeypatch.setattr("precis_web.routes.mermaid.run_turn", fake_run_turn)
    r = mm_client.post("/mermaid/web_turn/turn", data={"message": "simplify"})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "drew: simplify"
    assert body["changed"] is True
    assert "a --> b" in body["source"]
