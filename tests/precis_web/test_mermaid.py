"""Mermaid web editor routes — FakeStore degradation + real-store integration.

The render path degrades to the source text when ``mermaidx`` is absent (the
gate does not bake the extra), so these assert on the source / structure, not
on a rendered SVG."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.diagram.turn import TurnResult
from precis_web.app import create_app
from precis_web.config import WebConfig

_FLOW = "flowchart TD\n  intake[Intake] --> ship[Ship]"


# ── FakeStore degradation ────────────────────────────────────────────────


def test_mermaid_list_empty(client: TestClient) -> None:
    r = client.get("/mermaid")
    assert r.status_code == 200
    assert "No mermaid diagrams yet" in r.text


def test_mermaid_detail_404(client: TestClient) -> None:
    r = client.get("/mermaid/nope")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


def test_mermaid_render_404(client: TestClient) -> None:
    r = client.get("/mermaid/nope/render.svg")
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


def test_list_shows_seeded_diagram(mm_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_listed")
    r = mm_client.get("/mermaid")
    assert r.status_code == 200
    assert "web_listed" in r.text


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
