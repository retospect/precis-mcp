"""Stored per-ref body bytes (migration 0266) — a ``ref_body_bytes``
side table the ``ref_body_bytes`` worker pass recomputes off the write
path (no triggers: a chunk write never locks ``refs``); ring rows
render it via ``size_cell`` (no row = not yet computed = no size); the
batched ``precis backfill-ref-bytes`` fills the missing rows."""

from __future__ import annotations

from precis.cli.ref_bytes_backfill import backfill_ref_bytes
from precis.dispatch import Hub
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.utils import handle_registry
from precis.utils.eye_render import render_eye
from precis.utils.refeye import collect_ring
from precis.workers.ref_body_bytes import run_ref_body_bytes_pass


def _put_body(store: Store, ref_id: int, texts: list[str], start: int = 0) -> None:
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(ord=start + i, text=t, meta={"chunk_kind": "memory_body"})
            for i, t in enumerate(texts)
        ],
    )


def _body_bytes(store: Store, ref_id: int) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT body_bytes FROM ref_body_bytes WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return int(row[0]) if row else None


def test_incremental_pass_covers_create_append_replace_retire(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="byte-counted")
    assert _body_bytes(store, mem.id) is None  # no row until the pass runs

    _put_body(store, mem.id, ["a" * 100, "b" * 50])
    run_ref_body_bytes_pass(store)
    assert _body_bytes(store, mem.id) == 150

    _put_body(store, mem.id, ["c" * 40], start=2)
    run_ref_body_bytes_pass(store)
    assert _body_bytes(store, mem.id) == 190

    # Replace (DELETE + INSERT, the append-only body path) — the INSERT
    # half is what the incremental scan sees.
    store.chunks.insert_chunks(
        mem.id,
        [ChunkInsert(ord=0, text="d" * 700, meta={"chunk_kind": "memory_body"})],
        replace=True,
    )
    run_ref_body_bytes_pass(store)
    assert _body_bytes(store, mem.id) == 700

    # Retire (UPDATE retired_at) — the draft/soft-delete path moves it too.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE ref_id = %s", (mem.id,)
        )
    run_ref_body_bytes_pass(store)
    assert _body_bytes(store, mem.id) == 0


def test_verify_pass_covers_pure_delete(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="doomed")
    _put_body(store, mem.id, ["x" * 100])
    run_ref_body_bytes_pass(store)
    assert _body_bytes(store, mem.id) == 100

    # A pure DELETE leaves no created_at/retired_at anywhere — the
    # incremental pass can't see it; only the daily verify re-walk can.
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE ref_id = %s", (mem.id,))
    run_ref_body_bytes_pass(store)
    assert _body_bytes(store, mem.id) == 100

    run_ref_body_bytes_pass(store, verify=True)
    assert _body_bytes(store, mem.id) == 0


def test_fisheye_ring_row_shows_size(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="focus")
    near = store.insert_ref(kind="memory", slug=None, title="neighbour")
    _put_body(store, near.id, ["x" * 2048])
    run_ref_body_bytes_pass(store)
    store.add_link(src_ref_id=mem.id, dst_ref_id=near.id, relation="related-to")

    out = render_eye(
        store, handle_registry.format_handle("memory", mem.id), "fisheye+1hop"
    )
    ring_lines = [ln for ln in out.split("\n") if "neighbour" in ln]
    assert ring_lines and any("2kB" in ln for ln in ring_lines)


def test_collect_ring_row_shows_size(hub: Hub) -> None:
    store = hub.live_store
    near = store.insert_ref(kind="memory", slug=None, title="ring neighbour")
    _put_body(store, near.id, ["y" * 2048])
    run_ref_body_bytes_pass(store)
    nh = handle_registry.format_handle("memory", near.id)
    draft = store.insert_ref(kind="draft", slug="ringfocus-rb", title="ring focus")
    rows = store.chunks.insert_chunks(
        draft.id, [ChunkInsert(ord=0, text=f"see [[{nh}]] here")]
    )
    from types import SimpleNamespace

    chunks = [
        SimpleNamespace(
            chunk_id=r.id, ref_id=r.ref_id, text=r.text, parent_chunk_id=None
        )
        for r in rows
    ]
    groups = collect_ring(store, chunks[0], chunks)
    labels = [lbl for g in groups.values() for _rid, lbl in g]
    assert any("2kB" in lbl for lbl in labels), groups


def test_missing_side_row_renders_no_size(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="focus")
    near = store.insert_ref(kind="memory", slug=None, title="neighbour")
    _put_body(store, near.id, ["x" * 2048])
    # No pass run — ``near`` has body chunks but no ref_body_bytes row.
    store.add_link(src_ref_id=mem.id, dst_ref_id=near.id, relation="related-to")
    out = render_eye(
        store, handle_registry.format_handle("memory", mem.id), "fisheye+1hop"
    )
    assert "2kB" not in out
    # The row-less ref still answers body_byte_counts via the page-
    # bounded live-sum fallback.
    assert store.chunks.body_byte_counts([near.id]) == {near.id: 2048}
    assert store.chunks.stored_body_byte_counts([near.id]) == {}


def test_chunk_insert_takes_no_refs_row_lock(hub: Hub) -> None:
    """0266's reason for existing — a body-chunk write must not touch the
    refs row. Two proofs: no ``*body_bytes*`` trigger sits on ``chunks``
    (or ``refs``) at all, and an uncommitted chunk INSERT leaves the ref
    writable — under the trigger design this UPDATE hit lock_timeout."""
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="victim")
    with store.pool.connection() as conn:
        names = [
            str(t[0])
            for t in conn.execute(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid IN ('chunks'::regclass, 'refs'::regclass) "
                "  AND NOT tgisinternal"
            ).fetchall()
        ]
        assert not [t for t in names if "body_bytes" in t], names

    conn_a = store.pool.getconn()
    conn_b = store.pool.getconn()
    try:
        conn_a.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'memory_body', 'lock probe')",
            (mem.id,),
        )
        conn_b.execute("SELECT set_config('lock_timeout', '1s', true)")
        # Uncommitted INSERT in conn_a — a refs-row lock taken by a
        # trigger would make this raise QueryCanceled at 1 s.
        conn_b.execute(
            "UPDATE refs SET updated_at = now() WHERE ref_id = %s", (mem.id,)
        )
    finally:
        conn_a.rollback()
        conn_b.rollback()
        store.pool.putconn(conn_a)
        store.pool.putconn(conn_b)


def test_backfill_dry_run_apply_and_refresh(hub: Hub) -> None:
    store = hub.live_store
    ids = []
    for i in range(3):
        ref = store.insert_ref(kind="memory", slug=None, title=f"bf{i}")
        _put_body(store, ref.id, ["z" * (10 * (i + 1))])
        ids.append(ref.id)
    with store.pool.connection() as conn:
        dry = backfill_ref_bytes(conn, apply=False)
        assert dry.unfilled_before >= 3
        assert dry.updated == 0
        res = backfill_ref_bytes(conn, apply=True, batch=2)
        assert res.updated >= 3
        assert res.unfilled_after == 0
        # --refresh recomputes rows that already exist (a default run
        # would skip them all).
        conn.execute(
            "UPDATE ref_body_bytes SET body_bytes = 0 WHERE ref_id = ANY(%s)",
            (ids,),
        )
        stale = backfill_ref_bytes(conn, apply=True, batch=2)
        assert stale.updated == 0
        refreshed = backfill_ref_bytes(conn, apply=True, refresh=True, batch=2)
        assert refreshed.updated >= 3
    for i, rid in enumerate(ids):
        assert _body_bytes(store, rid) == 10 * (i + 1)
