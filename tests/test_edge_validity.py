"""gr568170 edge validity — the Doorstop-style suspect link.

A memory-sourced edge stamps the target's ``precis_target_sha`` at link
write (``meta.asserted_fingerprint``); the eye ring derives *suspect*
lazily when the stored sha no longer matches the target's current one —
nothing is written back onto edges when a target is edited. A reviewed
re-link refreshes the fingerprint and records who cleared the suspect
state (``suspect_cleared_by`` / ``suspect_cleared_at``).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store.types import ChunkInsert, Tag
from precis.utils import handle_registry
from precis.utils.eye_render import render_eye


def _mem(store, title: str) -> int:
    return store.insert_ref(kind="memory", slug=None, title=title).id


def _add_body_chunk(store, ref_id: int, text: str, ord_: int = 0) -> int:
    return store.chunks.insert_chunks(
        ref_id,
        [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": "memory_body"})],
    )[0].id


def test_memory_link_stamps_target_fingerprint(hub: Hub) -> None:
    store = hub.live_store
    src, dst = _mem(store, "section note"), _mem(store, "ruling note")
    link = store.add_link(src_ref_id=src, dst_ref_id=dst, relation="qualifies")
    assert link.meta["asserted_fingerprint"] == store.target_sha("ref", dst)


def test_nonmemory_link_carries_no_fingerprint(hub: Hub) -> None:
    store = hub.live_store
    todo = store.insert_ref(kind="todo", slug=None, title="plan item").id
    mem = _mem(store, "a note")
    link = store.add_link(src_ref_id=todo, dst_ref_id=mem, relation="related-to")
    assert "asserted_fingerprint" not in (link.meta or {})


def test_chunk_target_stamps_the_chunks_sha(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "section note")
    paper = store.insert_ref(kind="paper", slug="p1", title="Evidence paper").id
    chunk = store.chunks.insert_chunks(
        paper, [ChunkInsert(ord=0, text="the quoted passage")]
    )[0]
    link = store.add_link(src_ref_id=src, dst_ref_id=paper, dst_pos=0, relation="cites")
    assert link.meta["asserted_fingerprint"] == store.target_sha("chunk", chunk.id)


def test_caller_may_not_forge_edge_validity_keys(hub: Hub) -> None:
    store = hub.live_store
    src, dst = _mem(store, "a"), _mem(store, "b")
    for key in ("asserted_fingerprint", "suspect_cleared_by", "suspect_cleared_at"):
        with pytest.raises(BadInput):
            store.add_link(src_ref_id=src, dst_ref_id=dst, meta={key: "forged"})


def test_caller_meta_survives_the_stamp(hub: Hub) -> None:
    store = hub.live_store
    src, dst = _mem(store, "a"), _mem(store, "b")
    link = store.add_link(src_ref_id=src, dst_ref_id=dst, meta={"note": "why linked"})
    assert link.meta["note"] == "why linked"
    assert link.meta["asserted_fingerprint"] == store.target_sha("ref", dst)


def test_target_drift_marks_the_ring_row_suspect(hub: Hub) -> None:
    store = hub.live_store
    src = _mem(store, "section note")
    dst = _mem(store, "ruling note")
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation="qualifies")

    eye = render_eye(
        store, handle_registry.format_handle("memory", src), "fisheye+1hop"
    )
    assert "· suspect" not in eye

    _add_body_chunk(store, dst, "the ruling changed")
    eye = render_eye(
        store, handle_registry.format_handle("memory", src), "fisheye+1hop"
    )
    row = next(ln for ln in eye.splitlines() if f"me{dst}" in ln)
    assert row.rstrip().endswith("· suspect")

    # The drifted edge reads suspect from the target's ring too — the
    # inbound edge's fingerprint asserts about this side.
    dst_eye = render_eye(
        store, handle_registry.format_handle("memory", dst), "fisheye+1hop"
    )
    row = next(ln for ln in dst_eye.splitlines() if f"me{src}" in ln)
    assert "· suspect" in row


def test_relink_refreshes_fingerprint_and_records_clear(hub: Hub) -> None:
    store = hub.live_store
    src, dst = _mem(store, "section"), _mem(store, "ruling")
    store.add_link(src_ref_id=src, dst_ref_id=dst)
    _add_body_chunk(store, dst, "premise moved")

    link = store.add_link(src_ref_id=src, dst_ref_id=dst, set_by="agent")
    meta = link.meta
    assert meta["asserted_fingerprint"] == store.target_sha("ref", dst)
    assert meta["suspect_cleared_by"] == "agent"
    assert meta["suspect_cleared_at"]

    eye = render_eye(
        store, handle_registry.format_handle("memory", src), "fisheye+1hop"
    )
    assert "· suspect" not in eye


def test_relink_without_drift_records_no_clear(hub: Hub) -> None:
    store = hub.live_store
    src, dst = _mem(store, "section"), _mem(store, "ruling")
    store.add_link(src_ref_id=src, dst_ref_id=dst)
    link = store.add_link(src_ref_id=src, dst_ref_id=dst)
    assert "suspect_cleared_by" not in link.meta
    assert "suspect_cleared_at" not in link.meta


def test_hub_contains_row_marks_suspect(hub: Hub) -> None:
    """A ``section:index`` hub's member listing carries the same marker —
    the spec-graph case gr568170 was filed for."""
    store = hub.live_store
    hub_mem = _mem(store, "spec root")
    member = _mem(store, "a section")
    store.add_tag(hub_mem, Tag.open("section:index"), set_by="agent")
    store.add_link(src_ref_id=member, dst_ref_id=hub_mem, relation="part-of")
    _add_body_chunk(store, hub_mem, "the root premise moved")

    eye = render_eye(
        store, handle_registry.format_handle("memory", hub_mem), "fisheye+1hop"
    )
    assert "· suspect" in eye
