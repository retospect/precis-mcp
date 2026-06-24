"""Render orchestration (ADR 0035 §2/§3): a computed `figure` chunk —
`meta.render` recipe + `plots`-linked data — rendered to a PNG in `chunk_blobs`.

The non-matplotlib cases prove the *wiring* (bundle → engine → blob → key) and
run anywhere; one gated case renders a real matplotlib chart end-to-end.
"""

from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.render.figure import invalidation_key, render_figure_chunk


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="P").id


def _ord(hub: Hub, chunk_id: int) -> int:
    with hub.store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ord FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _set_render(hub: Hub, chunk_id: int, src: str) -> None:
    """Stamp a figure's `meta.render` recipe (the handler `render=` surface
    isn't built yet — this is what it will write)."""
    with hub.store.pool.connection() as conn:
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
    ref = hub.store.get_ref(kind="draft", id="g")
    d.put(
        id="g",
        chunk_kind="table",
        table={"header": ["x", "y"], "rows": [[1, 2], [3, 4]]},
        at={"last": True},
    )
    data_c = next(c for c in hub.store.reading_order(ref.id) if c.chunk_kind == "table")
    fig = hub.store.add_figure(
        ref_id=ref.id,
        caption="Fig 1.",
        origin="own_graph",
        image=b"\x89PNG\r\n\x1a\nstub",  # deferred placeholder
        mime="image/png",
        at={"last": True},
    )
    hub.store.add_link(
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
    fig = hub.store.get_draft_chunk(f"dc{fig_id}")

    out = render_figure_chunk(hub.store, fig_id)
    assert out.ok, out.detail
    # the deferred stub was overwritten with the rendered bytes
    blob = hub.store.get_chunk_blob(fig.handle)
    assert blob is not None and blob[0].startswith(b"\x89PNG\r\n\x1a\n")
    assert blob[1] == "image/png"
    # invalidation key stamped at meta.render.cached_key
    meta = hub.store.draft_chunk_meta(fig.handle)
    assert meta["render"]["cached_key"] == out.cached_key
    assert out.cached_key is not None and len(out.cached_key) == 64


def test_render_failure_bubbles_reason(hub: Hub) -> None:
    fig_id, _ref = _graph_figure(hub, "raise ValueError('bad recipe')\n")
    out = render_figure_chunk(hub.store, fig_id)
    assert not out.ok
    assert out.error is not None and out.error.startswith("exit:")
    assert "bad recipe" in out.detail


def test_not_a_graph_is_reported(hub: Hub) -> None:
    # a plain uploaded image figure (no meta.render) is not renderable
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=_proj(hub))
    ref = hub.store.get_ref(kind="draft", id="g")
    fig = hub.store.add_figure(
        ref_id=ref.id,
        caption="photo",
        origin="original",
        image=b"\x89PNG\r\n\x1a\nimg",
        mime="image/png",
        at={"last": True},
    )
    out = render_figure_chunk(hub.store, fig.chunk_id)
    assert not out.ok and out.error == "not-a-graph"


def test_invalidation_key_is_order_independent() -> None:
    assert invalidation_key(["a", "b"]) == invalidation_key(["b", "a"])
    assert invalidation_key(["a", "b"]) != invalidation_key(["a", "c"])


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
    out = render_figure_chunk(hub.store, fig_id)
    assert out.ok, out.detail
    fig = hub.store.get_draft_chunk(f"dc{fig_id}")
    blob = hub.store.get_chunk_blob(fig.handle)
    assert blob is not None and blob[0].startswith(b"\x89PNG\r\n\x1a\n")
    assert len(blob[0]) > 1000  # a real chart, not the stub
