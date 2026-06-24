"""Storage/relation foundation for computed figures (ADR 0035 §2/§3):

- the `plots` chunk→chunk relation (migration 0037) — a figure chunk renders
  a data chunk, addressable as the reactive recompute edge;
- `Store.upsert_chunk_blob` — the render path that (re)fills a figure's image
  bytes, overwriting the deferred/previous render (regenerable artifact).

The render orchestration that ties code + data → PNG lands in a later slice;
this pins the substrate it writes through.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler

_PNG = b"\x89PNG\r\n\x1a\n" + b"stub"


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="P").id


def _ord(hub: Hub, chunk_id: int) -> int:
    with hub.store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ord FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def seeded(hub: Hub) -> tuple[Hub, int, object, object]:
    """A draft with one data (table) chunk and one figure chunk."""
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
        image=_PNG,
        mime="image/png",
        at={"last": True},
    )
    return hub, ref.id, data_c, fig


def test_plots_relation_links_figure_to_data(seeded: tuple) -> None:
    hub, ref_id, data_c, fig = seeded
    # figure --plots--> data, chunk→chunk within the one draft ref
    link = hub.store.add_link(
        src_ref_id=ref_id,
        src_pos=_ord(hub, fig.chunk_id),  # type: ignore[attr-defined]
        dst_ref_id=ref_id,
        dst_pos=_ord(hub, data_c.chunk_id),  # type: ignore[attr-defined]
        relation="plots",
    )
    assert link.relation == "plots"
    # round-trips through the projection as (fig ord → data ord)
    assert link.src_pos == _ord(hub, fig.chunk_id)  # type: ignore[attr-defined]
    assert link.dst_pos == _ord(hub, data_c.chunk_id)  # type: ignore[attr-defined]
    # the edge is discoverable on the draft (the stale-walk reads it back)
    found = hub.store.links_for(ref_id, relation="plots", direction="out")
    assert any(ln.relation == "plots" for ln in found)


def test_upsert_chunk_blob_replaces_deferred_image(seeded: tuple) -> None:
    hub, _ref_id, _data_c, fig = seeded
    # add_figure seeded the stub; a render overwrites it in place
    new = b"\x89PNG\r\n\x1a\n" + b"rendered"
    hub.store.upsert_chunk_blob(fig.chunk_id, new, "image/png")  # type: ignore[attr-defined]
    got = hub.store.get_chunk_blob(fig.handle)  # type: ignore[attr-defined]
    assert got == (new, "image/png")
    # a second render replaces again (not a second row)
    newer = b"\x89PNG\r\n\x1a\n" + b"rerendered"
    hub.store.upsert_chunk_blob(fig.chunk_id, newer, "image/png")  # type: ignore[attr-defined]
    assert hub.store.get_chunk_blob(fig.handle) == (newer, "image/png")  # type: ignore[attr-defined]
