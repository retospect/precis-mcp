"""Editable canvas raster, stable element handles and draft inclusion."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile

from precis.dispatch import Hub
from precis.export.docx import export_docx
from precis.export.latex import export_draft
from precis.figure.raster import render_png
from precis.handlers.figure import FigureHandler
from precis.store.store import Store
from precis.workers.job_types import get_job_type

_CIRCLE = '<circle id="face" cx="8" cy="6" r="4" fill="green"/>'
_RECT = '<rect id="base" y="10" width="20" height="2"/>'


def _svg(*elements: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 12">'
        + "".join(elements)
        + "</svg>"
    )


def test_static_png_is_deterministic() -> None:
    svg = _svg(_CIRCLE, _RECT, '<text x="1" y="6">Fig</text>')
    png = render_png(svg)
    assert png is not None and png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png == render_png(svg)
    assert render_png(_svg()) is None


def test_worker_and_draft_exports_share_the_current_asset(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    figure = FigureHandler(hub=hub)
    figure.put(id="canvas", text=_svg(_CIRCLE))
    canvas = store.get_ref(kind="figure", id="canvas")
    assert canvas is not None
    job = store.insert_ref(
        kind="job", slug=None, title="figure_render", parent_id=canvas.id
    )
    spec = get_job_type("figure_render")
    assert spec is not None and spec.dispatch is not None
    ctx = Mock(store=store, ref_id=job.id)
    spec.dispatch(ctx, spec)
    ctx.record_failure.assert_not_called()
    asset = store.drafts.figure_png(canvas.id)
    assert asset is not None
    png, sha, width, height = asset
    assert (sha, width, height) == (hashlib.sha256(png).hexdigest(), 60, 36)
    ctx.set_meta.assert_called_once_with(png={"sha256": sha, "width": 60, "height": 36})

    project = store.insert_ref(kind="todo", slug=None, title="Project")
    draft, _ = store.drafts.create_draft(
        name="paper", title="Paper", project_ref_id=project.id
    )
    first = store.drafts.add_chunks(
        ref_id=draft.id, chunk_kind="paragraph", text="Before the drawing."
    )[0]
    anchor = store.drafts.add_chunks(
        ref_id=draft.id,
        chunk_kind="figure",
        text="Canvas caption",
        at={"after": first.dc},
        split=False,
    )[0]
    store.drafts.add_chunks(
        ref_id=draft.id,
        chunk_kind="paragraph",
        text="After the drawing.",
        at={"after": anchor.dc},
    )
    figure.link(id="canvas", rel="figure-in", target=anchor.dc)
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_chunk_id = %s "
            "AND relation = 'figure-in'",
            (canvas.id, anchor.chunk_id),
        ).fetchone()

    latex = export_draft(store, draft, target_dir=tmp_path / "latex")
    assert (latex.main_tex.parent / "pics" / f"{anchor.dc}.png").read_bytes() == png
    tex = latex.main_tex.read_text(encoding="utf-8")
    assert tex.index("Before the drawing.") < tex.index(f"pics/{anchor.dc}.png")
    assert tex.index(f"pics/{anchor.dc}.png") < tex.index("After the drawing.")

    figure.edit(id="canvas", text=_svg(_CIRCLE.replace("green", "red")))
    docx = tmp_path / "paper.docx"
    export_docx(store, draft, target_path=docx)
    refreshed = store.drafts.figure_png(canvas.id)
    assert refreshed is not None and refreshed[1] != sha
    with ZipFile(docx) as archive:
        assert archive.read("word/media/image1.png") == refreshed[0]


def test_top_level_handles_survive_edits_and_reordering(store: Store, hub: Hub) -> None:
    figure = FigureHandler(hub=hub)
    figure.put(id="canvas", text=_svg(_CIRCLE, _RECT))
    ref = store.get_ref(kind="figure", id="canvas")
    assert ref is not None
    source, face, base = store.drafts.reading_order(ref.id, kind="figure")
    assert face.parent_chunk_id == base.parent_chunk_id == source.chunk_id
    assert face.dc in figure.get(id="canvas").body
    assert 'id="face"' in figure.get(id=face.dc).body
    figure.edit(id="canvas", text=_svg(_RECT, _CIRCLE.replace("green", "red")))
    assert [c.dc for c in store.drafts.reading_order(ref.id, kind="figure")] == [
        source.dc,
        base.dc,
        face.dc,
    ]
    assert 'fill="red"' in figure.get(id=face.dc).body
    figure.edit(id="canvas", text=_svg(_RECT))
    removed = store.drafts.get_draft_chunk(face.dc, kind="figure")
    assert removed is not None and removed.retired
