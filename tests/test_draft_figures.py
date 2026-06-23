"""Draft figures — image blobs in the DB + provenance meta (ADR 0034)."""

from __future__ import annotations

import base64

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.store.store import Store

# A real 1×1 PNG, so the Pillow dimension probe (best-effort) has something
# valid to parse on hosts that have it.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "C0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG).decode()

_PERM = {
    "publisher": "Springer Nature",
    "permission_id": "SNCSC-2026-0451",
    "status": "granted",
    "source_paper": "smith19",
}


# ── store ops ────────────────────────────────────────────────────────


def _draft(store: Store):
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    return store.create_draft(name="nt", title="T", project_ref_id=proj)


def test_add_figure_stores_blob_and_meta(store: Store) -> None:
    ref, title = _draft(store)
    fig = store.add_figure(
        ref_id=ref.id,
        caption="Fig 1. A widget.",
        origin="original",
        image=_PNG,
        mime="image/png",
        at={"after": title.handle},
    )
    assert fig.chunk_kind == "figure"
    assert fig.text == "Fig 1. A widget."  # caption is the face, not split
    assert fig.meta["figure"]["origin"] == "original"

    # bytes round-trip with their mime
    blob = store.get_chunk_blob(fig.handle)
    assert blob is not None
    data, mime = blob
    assert data == _PNG and mime == "image/png"

    # appears in reading order carrying its meta
    order = store.reading_order(ref.id)
    assert order[-1].handle == fig.handle
    assert order[-1].meta["figure"]["origin"] == "original"


def test_add_figure_keeps_permission_in_meta(store: Store) -> None:
    ref, title = _draft(store)
    fig = store.add_figure(
        ref_id=ref.id,
        caption="Fig 2. Borrowed.",
        origin="third_party",
        image=_PNG,
        mime="image/png",
        at={"after": title.handle},
        figure_meta={"permission": _PERM},
    )
    got = store.get_draft_chunk(fig.handle)
    assert got.meta["figure"]["origin"] == "third_party"
    assert got.meta["figure"]["permission"]["permission_id"] == "SNCSC-2026-0451"


def test_get_chunk_blob_none_for_text_chunk(store: Store) -> None:
    _ref, title = _draft(store)
    assert store.get_chunk_blob(title.handle) is None


def test_blob_row_count_and_size(store: Store) -> None:
    ref, title = _draft(store)
    fig = store.add_figure(
        ref_id=ref.id,
        caption="c",
        origin="original",
        image=_PNG,
        mime="image/png",
        at={"after": title.handle},
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT size_bytes, mime FROM chunk_blobs WHERE chunk_id = %s",
            (fig.chunk_id,),
        ).fetchone()
    assert row == (len(_PNG), "image/png")


# ── set_figure_provenance (edit) ─────────────────────────────────────


def _events(store: Store, chunk_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT event_kind FROM chunk_events WHERE chunk_id=%s ORDER BY event_id",
            (chunk_id,),
        ).fetchall()
    return [r[0] for r in rows]


def test_set_figure_provenance_updates_meta_and_logs(store: Store) -> None:
    ref, title = _draft(store)
    fig = store.add_figure(
        ref_id=ref.id,
        caption="c",
        origin="original",
        image=_PNG,
        mime="image/png",
        at={"after": title.handle},
    )
    upd = store.set_figure_provenance(
        fig.handle, origin="third_party", permission=_PERM
    )
    assert upd.meta["figure"]["origin"] == "third_party"
    assert upd.meta["figure"]["permission"]["permission_id"] == "SNCSC-2026-0451"
    # bytes untouched, history shows the edit
    assert store.get_chunk_blob(fig.handle)[0] == _PNG
    assert _events(store, fig.chunk_id) == ["created", "edited"]


def test_set_figure_provenance_rejects_non_figure(store: Store) -> None:
    _ref, title = _draft(store)  # title is a heading, not a figure
    from precis.errors import BadInput

    with pytest.raises(BadInput, match="not a figure"):
        store.set_figure_provenance(title.handle, permission=_PERM)


# ── handler put path ─────────────────────────────────────────────────


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="P").id


def _figures(hub: Hub, slug: str) -> list:
    ref = hub.store.get_ref(kind="draft", id=slug)
    return [c for c in hub.store.reading_order(ref.id) if c.chunk_kind == "figure"]


def test_put_figure_original(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="nt", title="T", project=_proj(hub))
    r = draft.put(
        id="nt", chunk_kind="figure", text="Fig 1.", image=_PNG_B64, origin="original"
    )
    assert "added figure" in r.body and "[original]" in r.body
    figs = _figures(hub, "nt")
    assert len(figs) == 1 and figs[0].meta["figure"]["origin"] == "original"
    assert hub.store.get_chunk_blob(figs[0].handle)[0] == _PNG


def test_put_figure_bad_origin(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="nt", title="T", project=_proj(hub))
    with pytest.raises(BadInput, match="origin="):
        draft.put(
            id="nt", chunk_kind="figure", text="c", image=_PNG_B64, origin="bogus"
        )


def test_put_figure_caption_required(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="nt", title="T", project=_proj(hub))
    with pytest.raises(BadInput, match="caption"):
        draft.put(
            id="nt", chunk_kind="figure", text="  ", image=_PNG_B64, origin="original"
        )


def test_put_figure_bad_base64(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="nt", title="T", project=_proj(hub))
    with pytest.raises(BadInput, match="base64"):
        draft.put(
            id="nt", chunk_kind="figure", text="c", image="not!!b64", origin="original"
        )


def test_third_party_requires_permission(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="nt", title="T", project=_proj(hub))
    with pytest.raises(BadInput, match="permission"):
        draft.put(
            id="nt",
            chunk_kind="figure",
            text="c",
            image=_PNG_B64,
            origin="third_party",
        )
    # with the paper-trail → stored in meta
    draft.put(
        id="nt",
        chunk_kind="figure",
        text="c",
        image=_PNG_B64,
        origin="third_party",
        permission=_PERM,
    )
    fig = _figures(hub, "nt")[0]
    assert fig.meta["figure"]["permission"]["status"] == "granted"


def test_mime_sniffed_when_omitted(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="nt", title="T", project=_proj(hub))
    draft.put(id="nt", chunk_kind="figure", text="c", image=_PNG_B64, origin="original")
    fig = _figures(hub, "nt")[0]
    _data, mime = hub.store.get_chunk_blob(fig.handle)
    assert mime == "image/png"


def test_edit_permission_via_handler(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="nt", title="T", project=_proj(hub))
    draft.put(id="nt", chunk_kind="figure", text="c", image=_PNG_B64, origin="original")
    fig = _figures(hub, "nt")[0]
    r = draft.edit(id=f"¶{fig.handle}", origin="third_party", permission=_PERM)
    assert "updated figure provenance" in r.body
    upd = hub.store.get_draft_chunk(fig.handle)
    assert upd.meta["figure"]["origin"] == "third_party"
    assert upd.meta["figure"]["permission"]["permission_id"] == "SNCSC-2026-0451"


def test_edit_bad_origin_rejected(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="nt", title="T", project=_proj(hub))
    draft.put(id="nt", chunk_kind="figure", text="c", image=_PNG_B64, origin="original")
    fig = _figures(hub, "nt")[0]
    with pytest.raises(BadInput, match="origin="):
        draft.edit(id=f"¶{fig.handle}", origin="bogus")
