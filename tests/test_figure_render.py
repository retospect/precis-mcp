"""Render orchestration: a computed `figure` chunk —
`meta.render` recipe + `plots`-linked data — rendered to a PNG in `chunk_blobs`.

The non-matplotlib cases prove the *wiring* (bundle → engine → blob → key) and
run anywhere; one gated case renders a real matplotlib chart end-to-end.
"""

from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.render.figure import invalidation_key, render_figure_chunk


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="P").id


def _ord(hub: Hub, chunk_id: int) -> int:
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ord FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _set_render(hub: Hub, chunk_id: int, src: str) -> None:
    """Stamp a figure's `meta.render` recipe (the handler `render=` surface
    isn't built yet — this is what it will write)."""
    with hub.live_store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET meta = meta || %s::jsonb WHERE chunk_id = %s",
            (
                Jsonb({"render": {"kind": "code", "lang": "python", "src": src}}),
                chunk_id,
            ),
        )
        conn.commit()


def _graph_figure(hub: Hub, src: str) -> tuple[int, int]:
    """A draft with a data (table) chunk and a figure that `plots` it,
    carrying the given render `src`. Returns (figure_chunk_id, draft_ref_id)."""
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=_proj(hub))
    ref = hub.live_store.get_ref(kind="draft", id="g")
    assert ref is not None
    d.put(
        id="g",
        chunk_kind="table",
        table={"header": ["x", "y"], "rows": [[1, 2], [3, 4]]},
        at={"last": True},
    )
    data_c = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "table"
    )
    fig = hub.live_store.drafts.add_figure(
        ref_id=ref.id,
        caption="Fig 1.",
        origin="own_graph",
        image=b"\x89PNG\r\n\x1a\nstub",  # deferred placeholder
        mime="image/png",
        at={"last": True},
    )
    hub.live_store.add_link(
        src_ref_id=ref.id,
        src_pos=_ord(hub, fig.chunk_id),
        dst_ref_id=ref.id,
        dst_pos=_ord(hub, data_c.chunk_id),
        relation="plots",
    )
    _set_render(hub, fig.chunk_id, src)
    return fig.chunk_id, ref.id


def test_render_writes_png_and_stamps_key(hub: Hub) -> None:
    # render code that proves the plotted data arrived, then writes a PNG
    src = (
        "t = data['tables'][0]\n"
        "assert t['rows'] == [[1, 2], [3, 4]], t\n"
        "open(out, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + bytes(len(t['rows'])))\n"
    )
    fig_id, _ref = _graph_figure(hub, src)
    fig = hub.live_store.drafts.get_draft_chunk(f"dc{fig_id}")
    assert fig is not None

    out = render_figure_chunk(hub.live_store, fig_id)
    assert out.ok, out.detail
    # the deferred stub was overwritten with the rendered bytes
    blob = hub.live_store.drafts.get_chunk_blob(fig.handle)
    assert blob is not None and blob[0].startswith(b"\x89PNG\r\n\x1a\n")
    assert blob[1] == "image/png"
    # invalidation key stamped at meta.render.cached_key
    meta = hub.live_store.drafts.draft_chunk_meta(fig.handle)
    assert meta["render"]["cached_key"] == out.cached_key
    assert out.cached_key is not None and len(out.cached_key) == 64


def test_render_failure_bubbles_reason(hub: Hub) -> None:
    fig_id, _ref = _graph_figure(hub, "raise ValueError('bad recipe')\n")
    out = render_figure_chunk(hub.live_store, fig_id)
    assert not out.ok
    assert out.error is not None and out.error.startswith("exit:")
    assert "bad recipe" in out.detail


def test_not_a_graph_is_reported(hub: Hub) -> None:
    # a plain uploaded image figure (no meta.render) is not renderable
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=_proj(hub))
    ref = hub.live_store.get_ref(kind="draft", id="g")
    assert ref is not None
    fig = hub.live_store.drafts.add_figure(
        ref_id=ref.id,
        caption="photo",
        origin="original",
        image=b"\x89PNG\r\n\x1a\nimg",
        mime="image/png",
        at={"last": True},
    )
    out = render_figure_chunk(hub.live_store, fig.chunk_id)
    assert not out.ok and out.error == "not-a-graph"


def test_invalidation_key_is_order_independent() -> None:
    assert invalidation_key(["a", "b"]) == invalidation_key(["b", "a"])
    assert invalidation_key(["a", "b"]) != invalidation_key(["a", "c"])


# ── the put surface: render=/plots= through the verbs ─────────────────


def test_put_graph_figure_creates_recipe_plots_and_deferred_image(hub: Hub) -> None:
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=_proj(hub))
    ref = hub.live_store.get_ref(kind="draft", id="g")
    assert ref is not None
    d.put(
        id="g",
        chunk_kind="table",
        table={"header": ["x", "y"], "rows": [[1, 2], [3, 4]]},
        at={"last": True},
    )
    data_c = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "table"
    )

    src = "open(out, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + bytes(len(data['tables'][0]['rows'])))\n"
    r = d.put(
        id="g",
        chunk_kind="figure",
        render=src,
        plots=[data_c.dc],
        text="Fig 1. y vs x.",
        at={"last": True},
    )
    assert "added graph figure dc" in r.body and "plots 1 data source" in r.body
    fig = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "figure"
    )
    meta = hub.live_store.drafts.draft_chunk_meta(fig.handle)
    # recipe stamped, origin own_graph, image deferred (placeholder present)
    assert meta["render"]["src"] == src
    assert meta["figure"]["origin"] == "own_graph"
    assert (
        hub.live_store.drafts.get_chunk_blob(fig.handle) is not None
    )  # placeholder seeded

    # the plots link the put created drives the render end-to-end
    out = render_figure_chunk(hub.live_store, fig.chunk_id)
    assert out.ok, out.detail
    blob = hub.live_store.drafts.get_chunk_blob(fig.handle)
    assert blob is not None and blob[0].startswith(b"\x89PNG\r\n\x1a\n")


def test_put_graph_figure_requires_render_plots_and_caption(hub: Hub) -> None:
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=_proj(hub))
    with pytest.raises(BadInput, match="requires render="):
        d.put(id="g", chunk_kind="figure", plots=["dc1"], text="c", at={"last": True})
    with pytest.raises(BadInput, match=r"requires plots="):
        d.put(id="g", chunk_kind="figure", render="x=1", text="c", at={"last": True})
    with pytest.raises(BadInput, match="requires text="):
        d.put(
            id="g", chunk_kind="figure", render="x=1", plots=["dc1"], at={"last": True}
        )


# ── the edit surface: render=/plots= rewrite the recipe in place ──────
# (gr592839 — the edit used to refuse outright, forcing a delete/re-put
# that lost the dc<id> handle and every prose cross-ref into it)


def test_edit_graph_figure_render_recipe_keeps_handle(hub: Hub) -> None:
    """``edit(id=dc<fig>, render=…)`` rewrites ``meta.render`` on the live
    chunk — the stamped ``cached_key`` drops, ``render_pending`` re-arms,
    and the next render pass re-rasterises under the new code."""
    fig_id, _ref_id = _graph_figure(
        hub, "open(out, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + b'1')"
    )
    fig = hub.live_store.drafts.get_draft_chunk(f"dc{fig_id}")
    assert fig is not None
    out = render_figure_chunk(hub.live_store, fig_id)
    assert out.ok, out.detail  # cached_key stamped

    d = DraftHandler(hub=hub)
    src2 = "open(out, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + b'22')"
    r = d.edit(id=fig.dc, render=src2)
    assert "updated graph figure" in r.body and str(fig.dc) in r.body

    meta = hub.live_store.drafts.draft_chunk_meta(fig.handle)
    assert meta["render"]["src"] == src2
    assert meta["render"]["kind"] == "code"
    assert meta["figure"]["render_pending"] is True
    assert "cached_key" not in meta["render"]
    assert meta["figure"]["origin"] == "own_graph"

    again = hub.live_store.drafts.get_draft_chunk(fig.dc)
    assert again is not None and again.chunk_id == fig_id
    out2 = render_figure_chunk(hub.live_store, fig_id)
    assert out2.ok, out2.detail
    assert out2.cached_key != out.cached_key


def test_edit_figure_plots_replaces_edges_wholesale(hub: Hub) -> None:
    """``plots=`` replaces the figure's data edge set rather than
    appending — the render pass then sees only the new table."""
    fig_id, ref_id = _graph_figure(hub, "x = 1")
    drafts = hub.live_store.drafts
    fig = drafts.get_draft_chunk(f"dc{fig_id}")
    assert fig is not None
    d = DraftHandler(hub=hub)
    d.put(
        id="g",
        chunk_kind="table",
        table={"header": ["a", "b"], "rows": [[5, 6]]},
        at={"last": True},
    )
    tables = [c for c in drafts.reading_order(ref_id) if c.chunk_kind == "table"]
    assert len(tables) == 2

    r = d.edit(id=fig.dc, plots=[tables[1].dc])
    assert "plots 1 data source" in r.body

    with hub.live_store.pool.connection() as conn:
        dsts = {
            int(row[0])
            for row in conn.execute(
                "SELECT dst_chunk_id FROM links "
                "WHERE src_chunk_id = %s AND relation = 'plots'",
                (fig_id,),
            ).fetchall()
        }
    assert dsts == {tables[1].chunk_id}
    bundle = drafts.figure_render_bundle(fig_id)
    assert bundle is not None and len(bundle["tables"]) == 1
    assert bundle["tables"][0]["rows"] == [[5, 6]]
    # render_pending re-armed even though the recipe itself is unchanged
    meta = drafts.draft_chunk_meta(fig.handle)
    assert meta["figure"]["render_pending"] is True


def test_edit_figure_recipe_refusals(hub: Hub) -> None:
    """The recipe args refuse loudly: off a figure chunk, an empty
    render=, view= mixed with render=, and a recipe arg combined with a
    text edit (the caption rewrites separately)."""
    fig_id, ref_id = _graph_figure(hub, "x = 1")
    drafts = hub.live_store.drafts
    fig = drafts.get_draft_chunk(f"dc{fig_id}")
    assert fig is not None
    d = DraftHandler(hub=hub)
    d.put(id="g", chunk_kind="paragraph", text="plain prose", at={"last": True})
    para = next(c for c in drafts.reading_order(ref_id) if c.chunk_kind == "paragraph")
    with pytest.raises(BadInput, match="is a paragraph"):
        d.edit(id=para.dc, render="x = 1")
    with pytest.raises(BadInput, match="non-empty"):
        d.edit(id=fig.dc, render="   ")
    with pytest.raises(BadInput, match="doesn't combine"):
        d.edit(id=fig.dc, view={"source": "structure:x"}, render="x = 1")
    with pytest.raises(BadInput, match="caption separately"):
        d.edit(id=fig.dc, render="x = 1", text="new caption")


def test_edit_figure_recipe_requires_a_graph_to_target(hub: Hub) -> None:
    """``plots=`` alone on a figure with no render recipe, and ``render=``
    alone with no plotted data, both refuse — the put-side requirements
    carried over to the edit path."""
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=_proj(hub))
    ref = hub.live_store.get_ref(kind="draft", id="g")
    assert ref is not None
    d.put(
        id="g",
        chunk_kind="table",
        table={"header": ["a", "b"], "rows": [[1, 2]]},
        at={"last": True},
    )
    data_c = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "table"
    )
    fig = hub.live_store.drafts.add_figure(
        ref_id=ref.id,
        caption="photo",
        origin="original",
        image=b"\x89PNG\r\n\x1a\nimg",
        mime="image/png",
        at={"last": True},
    )
    with pytest.raises(BadInput, match="no render recipe"):
        d.edit(id=fig.dc, plots=[data_c.dc])
    with pytest.raises(BadInput, match="requires plots"):
        d.edit(id=fig.dc, render="x = 1")
    # render= + plots= together DO convert the image figure into a graph
    r = d.edit(id=fig.dc, render="x = 1", plots=[data_c.dc])
    assert "updated graph figure" in r.body
    meta = hub.live_store.drafts.draft_chunk_meta(fig.handle)
    assert meta["render"]["kind"] == "code"
    assert meta["figure"]["origin"] == "own_graph"


def test_real_matplotlib_render(hub: Hub) -> None:
    import subprocess
    import sys

    have = subprocess.run(
        [sys.executable, "-c", "import matplotlib"], capture_output=True
    )
    if have.returncode != 0:
        pytest.skip("matplotlib not installed (the [plot] extra)")

    src = (
        "import matplotlib.pyplot as plt\n"
        "t = data['tables'][0]\n"
        "xs = [r[0] for r in t['rows']]\n"
        "ys = [r[1] for r in t['rows']]\n"
        "plt.plot(xs, ys)  # harness auto-saves to out\n"
    )
    fig_id, _ref = _graph_figure(hub, src)
    out = render_figure_chunk(hub.live_store, fig_id)
    assert out.ok, out.detail
    fig = hub.live_store.drafts.get_draft_chunk(f"dc{fig_id}")
    assert fig is not None
    blob = hub.live_store.drafts.get_chunk_blob(fig.handle)
    assert blob is not None and blob[0].startswith(b"\x89PNG\r\n\x1a\n")
    assert len(blob[0]) > 1000  # a real chart, not the stub
