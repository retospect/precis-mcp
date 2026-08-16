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


# ── draw-from-a-draft-figure (the canvas medium) ───────────────


def _draft_with_placeholder(store):
    """A draft carrying one asset-less figure chunk (the deck-hook shape)."""
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    ref, title = store.drafts.create_draft(
        name="deckhook", title="Deck Hook", project_ref_id=proj
    )
    fig = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="figure",
        text="FIG. 1 a perspective view",
        at={"after": title.handle},
        split=False,
    )[0]
    return ref, fig


def test_create_drawing_mints_canvas_and_links(fig_client, runtime_with_store) -> None:
    store = runtime_with_store.store
    ref, fig = _draft_with_placeholder(store)
    r = fig_client.post(
        f"/drafts/{ref.slug}/figure/{fig.handle}/draw", follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/figure/")
    # a canvas was minted, seeded from the caption, and linked to the chunk.
    canvas_ref_id = store.drafts.figure_canvas_ref(fig.chunk_id)
    assert canvas_ref_id is not None
    canvas = store.get_ref(kind="figure", id=canvas_ref_id)
    assert canvas.title == "FIG. 1 a perspective view"


def test_create_drawing_is_idempotent(fig_client, runtime_with_store) -> None:
    store = runtime_with_store.store
    ref, fig = _draft_with_placeholder(store)
    url = f"/drafts/{ref.slug}/figure/{fig.handle}/draw"
    loc1 = fig_client.post(url, follow_redirects=False).headers["location"]
    loc2 = fig_client.post(url, follow_redirects=False).headers["location"]
    assert loc1 == loc2  # same canvas, no duplicate
    links = store.links_for(ref.id, direction="out", relation="has-figure")
    assert len(links) == 1


# ── refresh (re-mint a data-package figure in place) ────────────────────

_OLD_PNG = b"\x89PNG\r\n\x1a\n-old-"


def _snapshot(ref_id: int, *, kind: str = "quest", rows: list | None = None) -> dict:
    from precis.utils import handle_registry

    return {
        "schema": 1,
        "source": {
            "kind": kind,
            "ref_id": ref_id,
            "handle": handle_registry.try_format(kind, ref_id),
            "title": "Q1",
        },
        "generated_at": "2026-01-01T00:00:00+00:00",
        "autocatpath_version": "0.1.0",
        "precis": {"version": "0.0.0", "sha": None},
        "params": {},
        "columns": ["handle"],
        "rows": rows if rows is not None else [{"handle": "a1"}],
    }


def _draft_with_data_package_figure(store, quest_ref_id: int):
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    ref, title = store.drafts.create_draft(
        name="datapkg", title="Data Pkg", project_ref_id=proj
    )
    fig = store.drafts.add_figure(
        ref_id=ref.id,
        caption="Pareto frontier for Q1",
        origin="own_graph",
        image=_OLD_PNG,
        mime="image/png",
        at={"after": title.handle},
        figure_meta={"data_package": _snapshot(quest_ref_id)},
    )
    return ref, fig


def test_refresh_swaps_blob_and_snapshot(
    fig_client, runtime_with_store, monkeypatch
) -> None:
    import precis.quest.figures as figures_mod

    store = runtime_with_store.store
    quest_ref = store.insert_ref(kind="quest", slug=None, title="Q1")
    ref, fig = _draft_with_data_package_figure(store, quest_ref.id)

    new_png = b"\x89PNG\r\n\x1a\n-new-"
    new_snapshot = _snapshot(quest_ref.id, rows=[{"handle": "a2"}])

    def fake_quest_pareto_figure(store_arg, target):
        assert target.id == quest_ref.id
        return new_png, new_snapshot

    monkeypatch.setattr(figures_mod, "quest_pareto_figure", fake_quest_pareto_figure)

    r = fig_client.post(
        f"/drafts/{ref.slug}/figure/{fig.handle}/refresh", follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/drafts/{ref.slug}#c-{fig.handle}"

    blob = store.drafts.get_chunk_blob(fig.handle)
    assert blob is not None
    assert blob[0] == new_png

    refreshed = store.drafts.get_draft_chunk(fig.handle)
    assert refreshed is not None
    assert refreshed.meta["figure"]["data_package"] == new_snapshot
    assert refreshed.meta["figure"]["origin"] == "own_graph"  # untouched


def test_refresh_without_snapshot_rejected(fig_client, runtime_with_store) -> None:
    store = runtime_with_store.store
    ref, fig = _draft_with_placeholder(store)  # caption-only, no data_package
    r = fig_client.post(f"/drafts/{ref.slug}/figure/{fig.handle}/refresh")
    assert r.status_code == 400
    assert store.drafts.get_chunk_blob(fig.handle) is None


def test_refresh_source_ref_deleted(fig_client, runtime_with_store) -> None:
    store = runtime_with_store.store
    quest_ref = store.insert_ref(kind="quest", slug=None, title="Q1")
    ref, fig = _draft_with_data_package_figure(store, quest_ref.id)
    store.soft_delete_ref(quest_ref.id)

    r = fig_client.post(f"/drafts/{ref.slug}/figure/{fig.handle}/refresh")
    assert r.status_code == 404

    blob = store.drafts.get_chunk_blob(fig.handle)
    assert blob is not None
    assert blob[0] == _OLD_PNG


def test_refresh_renderer_value_error_no_write(
    fig_client, runtime_with_store, monkeypatch
) -> None:
    import precis.quest.figures as figures_mod

    store = runtime_with_store.store
    quest_ref = store.insert_ref(kind="quest", slug=None, title="Q1")
    ref, fig = _draft_with_data_package_figure(store, quest_ref.id)

    def fake_quest_pareto_figure(store_arg, target):
        raise ValueError("fewer than 2 plottable candidates")

    monkeypatch.setattr(figures_mod, "quest_pareto_figure", fake_quest_pareto_figure)

    r = fig_client.post(f"/drafts/{ref.slug}/figure/{fig.handle}/refresh")
    assert r.status_code == 409
    assert "fewer than 2 plottable candidates" in r.text

    blob = store.drafts.get_chunk_blob(fig.handle)
    assert blob is not None
    assert blob[0] == _OLD_PNG
