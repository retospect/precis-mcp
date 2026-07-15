"""Figure source resolver — the medium axis (ADR 0057).

blob / canvas / graph / none, and the medium-aware clearance roll-up.
"""

from __future__ import annotations

import base64

import pytest

from precis.dispatch import Hub
from precis.handlers.figure import FigureHandler
from precis.store.store import Store
from precis.utils.figure_clearance import draft_figure_clearance
from precis.utils.figure_source import figure_export_asset, resolve_figure_source

# A real 1×1 PNG (the Pillow dimension probe has something valid to parse).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "C0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_DRAWN = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<circle cx="50" cy="50" r="30" fill="green"/></svg>'
)


def _draft(store: Store):
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    return store.create_draft(name="nt", title="T", project_ref_id=proj)


def _placeholder_figure(store: Store, ref_id: int, after: str):
    """A caption-only figure chunk with no blob (the deck-hook shape)."""
    return store.add_chunks(
        ref_id=ref_id,
        chunk_kind="figure",
        text="FIG. 1 a perspective view",
        at={"after": after},
        meta={"short": "FIG. 1", "registry": "figures"},
        split=False,
    )[0]


# ── medium resolution ────────────────────────────────────────────────────


def test_none_medium_is_placeholder_and_uncleared(store: Store) -> None:
    ref, title = _draft(store)
    fig = _placeholder_figure(store, ref.id, title.handle)
    src = resolve_figure_source(store, fig)
    assert src.medium == "none"
    assert src.render.mode == "placeholder"
    assert not src.cleared
    assert "no image" in src.reason


def test_blob_medium_original_cleared(store: Store) -> None:
    ref, title = _draft(store)
    fig = store.add_figure(
        ref_id=ref.id,
        caption="Fig 1. A widget.",
        origin="original",
        image=_PNG,
        mime="image/png",
        at={"after": title.handle},
    )
    src = resolve_figure_source(store, fig)
    assert src.medium == "blob"
    assert src.render.mode == "image"
    assert src.render.url == f"/drafts/blob/{fig.handle}"
    assert src.cleared


def test_blob_third_party_permission_gates_clearance(store: Store) -> None:
    ref, title = _draft(store)
    ungranted = store.add_figure(
        ref_id=ref.id,
        caption="Fig 2. Borrowed.",
        origin="third_party",
        image=_PNG,
        mime="image/png",
        at={"after": title.handle},
        figure_meta={"permission": {"status": "requested"}},
    )
    src = resolve_figure_source(store, ungranted)
    assert src.medium == "blob"
    assert not src.cleared  # rights axis: permission not granted

    granted = store.add_figure(
        ref_id=ref.id,
        caption="Fig 3. Licensed.",
        origin="third_party",
        image=_PNG,
        mime="image/png",
        at={"after": ungranted.handle},
        figure_meta={"permission": {"status": "granted"}},
    )
    assert resolve_figure_source(store, granted).cleared


def test_graph_medium_labelled_from_own_graph(store: Store) -> None:
    ref, title = _draft(store)
    fig = store.add_figure(
        ref_id=ref.id,
        caption="Fig 4. A plot.",
        origin="own_graph",
        image=_PNG,
        mime="image/png",
        at={"after": title.handle},
    )
    src = resolve_figure_source(store, fig)
    assert src.medium == "graph"  # blob-backed, but labelled by production
    assert src.render.mode == "image"
    assert src.cleared


def test_canvas_medium_drawn_is_cleared(store: Store, hub: Hub) -> None:
    ref, title = _draft(store)
    fig = _placeholder_figure(store, ref.id, title.handle)
    FigureHandler(hub=hub).put(id="c1", title="Fig 1", text=_DRAWN)
    canvas = store.get_ref(kind="figure", id="c1")
    store.link_figure_canvas(fig.chunk_id, canvas.id)

    src = resolve_figure_source(store, fig)
    assert src.medium == "canvas"
    assert src.render.mode == "canvas"
    assert src.render.url == "/figure/c1/source.svg"
    assert src.canvas_slug == "c1"
    assert src.cleared  # ours + actually drawn


def test_canvas_medium_empty_is_uncleared(store: Store, hub: Hub) -> None:
    ref, title = _draft(store)
    fig = _placeholder_figure(store, ref.id, title.handle)
    FigureHandler(hub=hub).put(id="c2", title="Empty")  # default empty canvas
    canvas = store.get_ref(kind="figure", id="c2")
    store.link_figure_canvas(fig.chunk_id, canvas.id)

    src = resolve_figure_source(store, fig)
    assert src.medium == "canvas"
    assert not src.cleared
    assert "drawn" in src.reason


# ── clearance roll-up ────────────────────────────────────────────────────


def test_clearance_counts_assetless_figures(store: Store, hub: Hub) -> None:
    ref, title = _draft(store)
    f1 = _placeholder_figure(store, ref.id, title.handle)
    _placeholder_figure(store, ref.id, f1.handle)

    summary = draft_figure_clearance(store, ref.id)
    assert summary.total == 2
    assert len(summary.uncleared) == 2  # both asset-less → not shippable
    assert not summary.all_clear

    # draw one → it clears, the other stays flagged.
    FigureHandler(hub=hub).put(id="drawn", title="Drawn", text=_DRAWN)
    canvas = store.get_ref(kind="figure", id="drawn")
    store.link_figure_canvas(f1.chunk_id, canvas.id)

    summary2 = draft_figure_clearance(store, ref.id)
    assert summary2.total == 2
    assert len(summary2.uncleared) == 1


# ── export asset (ADR 0057 slice 4) ──────────────────────────────────────

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def test_export_asset_raster_passthrough(store: Store) -> None:
    ref, title = _draft(store)
    fig = store.add_figure(
        ref_id=ref.id,
        caption="c",
        origin="original",
        image=_PNG,
        mime="image/png",
        at={"after": title.handle},
    )
    assert figure_export_asset(store, fig) == (_PNG, "png")


def test_export_asset_none_for_placeholder(store: Store) -> None:
    ref, title = _draft(store)
    fig = _placeholder_figure(store, ref.id, title.handle)
    assert figure_export_asset(store, fig) is None


def test_export_asset_canvas_rasterises_to_png(store: Store, hub: Hub) -> None:
    pytest.importorskip("resvg_py")
    ref, title = _draft(store)
    fig = _placeholder_figure(store, ref.id, title.handle)
    FigureHandler(hub=hub).put(id="c1", title="F", text=_DRAWN)
    canvas = store.get_ref(kind="figure", id="c1")
    store.link_figure_canvas(fig.chunk_id, canvas.id)

    asset = figure_export_asset(store, fig)
    assert asset is not None
    data, ext = asset
    assert ext == "png" and data[:8] == _PNG_SIG


def test_export_asset_svg_blob_rasterises(store: Store) -> None:
    pytest.importorskip("resvg_py")
    ref, title = _draft(store)
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<rect width="10" height="10"/></svg>'
    )
    fig = store.add_figure(
        ref_id=ref.id,
        caption="c",
        origin="original",
        image=svg,
        mime="image/svg+xml",
        at={"after": title.handle},
    )
    asset = figure_export_asset(store, fig)
    assert asset is not None
    data, ext = asset
    assert ext == "png" and data[:8] == _PNG_SIG
