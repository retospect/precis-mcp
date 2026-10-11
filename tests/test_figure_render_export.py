"""A ``render_pending`` graph figure is rendered at export time (gr343055):
the export embeds the rendered image, not the deferred placeholder, and a
failing render surfaces as an export warning instead of a silent fallback."""

from __future__ import annotations

import hashlib
from pathlib import Path

import docx

from precis.dispatch import Hub
from precis.export.docx import export_docx
from precis.handlers.draft import _PLACEHOLDER_PNG, DraftHandler
from precis.handlers.todo import TodoHandler

# A valid 2x1 PNG (differs from the 1x1 placeholder), written by the recipe
# so the test needs no matplotlib.
_GOOD_SRC = (
    "import base64, struct, zlib\n"
    "def chunk(t, d):\n"
    "    c = struct.pack('>I', len(d)) + t + d\n"
    "    return c + struct.pack('>I', zlib.crc32(t + d))\n"
    "png = (b'\\x89PNG\\r\\n\\x1a\\n'\n"
    "       + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 1, 8, 0, 0, 0, 0))\n"
    "       + chunk(b'IDAT', zlib.compress(b'\\x00\\x10\\xf0'))\n"
    "       + chunk(b'IEND', b''))\n"
    "open(out, 'wb').write(png)\n"
)


def _pending_figure(hub: Hub, src: str):
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=pid)
    d.put(
        id="g",
        chunk_kind="table",
        table={"header": ["x", "y"], "rows": [[1, 2], [3, 4]]},
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="g")
    assert ref is not None
    table = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "table"
    )
    d.put(
        id="g",
        chunk_kind="figure",
        text="Fig 1. Graph.",
        render=src,
        plots=[table.dc],
        at={"last": True},
    )
    fig = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "figure"
    )
    return ref, fig


def _docx_image_shas(path: Path) -> list[str]:
    doc = docx.Document(str(path))
    return [
        hashlib.sha256(r.target_part.blob).hexdigest()
        for r in doc.part.rels.values()
        if "image" in r.reltype
    ]


def test_docx_export_renders_pending_graph_figure(hub: Hub, tmp_path: Path) -> None:
    ref, fig = _pending_figure(hub, _GOOD_SRC)
    assert fig.meta["figure"]["render_pending"] is True
    blob = hub.live_store.drafts.get_chunk_blob(fig.handle)
    assert blob is not None and blob[0] == _PLACEHOLDER_PNG

    res = export_docx(hub.live_store, ref, target_path=tmp_path / "g.docx")

    shas = _docx_image_shas(res.path)
    placeholder_sha = hashlib.sha256(_PLACEHOLDER_PNG).hexdigest()
    assert len(shas) == 1 and shas[0] != placeholder_sha
    assert not [w for w in res.warnings if "render" in w]
    # persisted: blob replaced, flag cleared, so a second export reuses it
    blob = hub.live_store.drafts.get_chunk_blob(fig.handle)
    assert blob is not None and hashlib.sha256(blob[0]).hexdigest() == shas[0]
    meta = hub.live_store.drafts.draft_chunk_meta(fig.handle)
    assert "render_pending" not in meta["figure"]


def test_docx_export_surfaces_render_failure(hub: Hub, tmp_path: Path) -> None:
    ref, fig = _pending_figure(hub, "raise ValueError('bad recipe')\n")
    res = export_docx(hub.live_store, ref, target_path=tmp_path / "g.docx")

    assert _docx_image_shas(res.path) == []  # placeholder not embedded
    assert any(
        fig.dc in w and "render failed" in w and "bad recipe" in w for w in res.warnings
    ), res.warnings
    # still pending, so the next export retries
    meta = hub.live_store.drafts.draft_chunk_meta(fig.handle)
    assert meta["figure"]["render_pending"] is True


def test_latex_export_renders_pending_graph_figure(hub: Hub, tmp_path: Path) -> None:
    from precis.export.latex import export_draft

    ref, fig = _pending_figure(hub, _GOOD_SRC)
    res = export_draft(hub.live_store, ref, target_dir=tmp_path)
    pics = list(tmp_path.rglob(f"{fig.dc}.png"))
    assert len(pics) == 1
    assert pics[0].read_bytes() != _PLACEHOLDER_PNG
    assert not [w for w in res.warnings if "render" in w]


def test_latex_export_surfaces_render_failure(hub: Hub, tmp_path: Path) -> None:
    from precis.export.latex import export_draft

    ref, fig = _pending_figure(hub, "raise ValueError('bad recipe')\n")
    res = export_draft(hub.live_store, ref, target_dir=tmp_path)
    assert not list(tmp_path.rglob(f"{fig.dc}.png"))
    assert any(
        fig.dc in w and "render failed" in w and "bad recipe" in w for w in res.warnings
    ), res.warnings


def _web_client(runtime_with_store, tmp_path: Path):
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_blob_route_renders_pending_figure(
    hub: Hub, runtime_with_store, tmp_path: Path
) -> None:
    _ref, fig = _pending_figure(hub, _GOOD_SRC)
    r = _web_client(runtime_with_store, tmp_path).get(f"/drafts/blob/{fig.handle}")
    assert r.status_code == 200
    assert r.content != _PLACEHOLDER_PNG and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "x-render-error" not in r.headers
    meta = hub.live_store.drafts.draft_chunk_meta(fig.handle)
    assert "render_pending" not in meta["figure"]


def test_blob_route_failed_render_serves_placeholder_with_error(
    hub: Hub, runtime_with_store, tmp_path: Path
) -> None:
    _ref, fig = _pending_figure(hub, "raise ValueError('bad recipe')\n")
    r = _web_client(runtime_with_store, tmp_path).get(f"/drafts/blob/{fig.handle}")
    assert r.status_code == 200
    assert r.content == _PLACEHOLDER_PNG
    assert "render failed" in r.headers["x-render-error"]
    assert r.headers["cache-control"] == "no-store"


def test_graph_rerenders_after_plotted_table_edit(hub: Hub, tmp_path: Path) -> None:
    """Editing a plotted table after the first render makes the figure stale:
    the next export re-renders (cached_key no longer matches)."""
    ref, fig = _pending_figure(hub, _GOOD_SRC)
    drafts = hub.live_store.drafts
    export_docx(hub.live_store, ref, target_path=tmp_path / "a.docx")
    key1 = drafts.draft_chunk_meta(fig.handle)["render"]["cached_key"]

    # unchanged data: no re-render, key stable
    export_docx(hub.live_store, ref, target_path=tmp_path / "b.docx")
    assert drafts.draft_chunk_meta(fig.handle)["render"]["cached_key"] == key1

    table = next(c for c in drafts.reading_order(ref.id) if c.chunk_kind == "table")
    DraftHandler(hub=hub).edit(
        id=table.dc, table={"header": ["x", "y"], "rows": [[1, 9], [3, 4]]}
    )
    from precis.render.figure import needs_render

    fig2 = drafts.get_draft_chunk(fig.dc)
    assert needs_render(hub.live_store, fig2)
    export_docx(hub.live_store, ref, target_path=tmp_path / "c.docx")
    key2 = drafts.draft_chunk_meta(fig.handle)["render"]["cached_key"]
    assert key2 != key1
    assert not needs_render(hub.live_store, drafts.get_draft_chunk(fig.dc))
