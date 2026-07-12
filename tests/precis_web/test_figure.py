"""Figure web editor routes — FakeStore degradation + real-store integration."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.figure.turn import TurnResult
from precis.handlers.figure import FigureHandler
from precis_web.app import create_app
from precis_web.config import WebConfig

_CIRCLE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<circle id="face" cx="50" cy="50" r="30" fill="green"/></svg>'
)


# ── FakeStore degradation ────────────────────────────────────────────────


def test_figure_list_empty(client: TestClient) -> None:
    r = client.get("/figure")
    assert r.status_code == 200
    assert "No figures yet" in r.text


def test_figure_detail_404(client: TestClient) -> None:
    r = client.get("/figure/nope")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


def test_figure_source_404(client: TestClient) -> None:
    r = client.get("/figure/nope/source.svg")
    assert r.status_code == 404


# ── real-store integration ───────────────────────────────────────────────


@pytest.fixture
def fig_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed(runtime_with_store, slug: str = "web_fig") -> None:
    FigureHandler(hub=runtime_with_store.hub).put(
        id=slug, title="Web Fig", text=_CIRCLE, vocab="green circles are foos"
    )


def test_detail_renders_canvas_and_vocab(fig_client, runtime_with_store) -> None:
    _seed(runtime_with_store)
    r = fig_client.get("/figure/web_fig")
    assert r.status_code == 200
    assert 'id="fig-canvas"' in r.text  # the inline-SVG canvas (not an <img>)
    assert 'id="face"' in r.text  # SVG is inlined into the page so animation plays
    assert "green circles are foos" in r.text  # the vocab pane
    assert "100×100" in r.text  # the viewBox caption
    # both doc tabs present
    assert "Shared vocabulary" in r.text
    assert "Implementation notes" in r.text


def test_source_svg_served_and_sanitized(fig_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_src")
    r = fig_client.get("/figure/web_src/source.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "circle" in r.text
    assert "script" not in r.text.lower()


def test_list_shows_seeded_figure(fig_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_listed")
    r = fig_client.get("/figure")
    assert r.status_code == 200
    assert "web_listed" in r.text


def test_turn_route_returns_json(fig_client, runtime_with_store, monkeypatch) -> None:
    _seed(runtime_with_store, slug="web_turn")

    def fake_run_turn(store, ref, message, **kw):
        return TurnResult(
            reply=f"drew: {message}",
            svg=_CIRCLE,
            findings=[],
            changed=True,
            healed=False,
            vocab="a green face",
            notes="face = circle#face",
        )

    monkeypatch.setattr("precis_web.routes.figure.run_turn", fake_run_turn)
    r = fig_client.post("/figure/web_turn/turn", data={"message": "draw a face"})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True
    assert "drew: draw a face" in body["reply"]
    assert "circle" in body["svg"]
    # docs come back so the panes can reload
    assert body["vocab"] == "a green face"
    assert body["notes"] == "face = circle#face"


def test_turn_route_rejects_empty(fig_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_empty")
    r = fig_client.post("/figure/web_empty/turn", data={"message": "   "})
    assert r.status_code == 400


# ── creation from the UI (Drive "+ New" + the /figure button) ────────────


def test_list_has_new_figure_button(client: TestClient) -> None:
    r = client.get("/figure")
    assert r.status_code == 200
    assert "New figure" in r.text
    assert 'action="/drive/new"' in r.text  # the DRY create path


def test_drive_dropdown_offers_figure(fig_client) -> None:
    r = fig_client.get("/drive")
    assert r.status_code == 200
    assert 'value="figure"' in r.text


def test_drive_new_creates_figure_and_redirects(fig_client, runtime_with_store) -> None:
    from precis.handlers.figure import FigureHandler

    r = fig_client.post(
        "/drive/new",
        data={"kind": "figure", "title": "My Sketch"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    loc = r.headers["location"]
    assert loc.startswith("/figure/")
    slug = loc.rsplit("/", 1)[-1]
    # the figure now really exists with a default canvas
    body = FigureHandler(hub=runtime_with_store.hub).get(id=slug).body
    assert "SVG source" in body
