"""``get(kind='job', id='/facts')`` — the storage/health facts block (gr593053).

The weekly report's "quick facts" line wanted numbers no MCP surface
carried: live refs per kind, exact chunk/embedding counts with
coverage, PDF bytes by role, DB size and the served build. This view
answers them in one read-only call; the backup line reports the
``app_state`` ``backup.*`` contract when a deploy-side writer has
stamped it and says so plainly when nothing has.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.store import Store
from precis.store.types import ChunkInsert

_BACKUP_KEYS = ("backup.last_run_at", "backup.last_size_bytes", "backup.last_kind")


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _drop_backup_keys(store: Store) -> None:
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM app_state WHERE key = ANY(%s)", (list(_BACKUP_KEYS),))
        conn.commit()


def _pdf(store: Store, sha: str, size_bytes: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, size_bytes, "
            "storage_path) VALUES (%s, %s, 1, %s, %s)",
            (sha, sha, size_bytes, f"/corpus/test/{sha}.pdf"),
        )
        conn.commit()


def _ref_with_pdf(store: Store, kind: str, sha: str) -> int:
    ref = store.insert_ref(
        kind=kind,
        slug=f"{kind}-{uuid4().hex[:8]}",
        title=f"t-{kind}-{uuid4().hex[:6]}",
        meta={},
    )
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id))
        conn.commit()
    return ref.id


def test_facts_renders_storage_block(jobs: JobHandler, store: Store, hub: Hub) -> None:
    _drop_backup_keys(store)
    sha_a = "a" * 64
    sha_b = "b" * 64
    _pdf(store, sha_a, 1000)
    _pdf(store, sha_b, 3000)
    # 2 live papers on one PDF, 1 pres on another, 1 datasheet sharing
    # the paper PDF, 1 todo with none — plus a retired pres that must
    # not count anywhere.
    _ref_with_pdf(store, "paper", sha_a)
    _ref_with_pdf(store, "paper", sha_a)
    _ref_with_pdf(store, "pres", sha_b)
    _ref_with_pdf(store, "datasheet", sha_a)
    todo_id = store.insert_ref(kind="todo", slug=None, title="t-todo", meta={}).id
    retired = store.insert_ref(
        kind="pres", slug=f"pres-{uuid4().hex[:8]}", title="t-old", meta={}
    )
    store.retire_ref(retired.id)

    # 4 live chunks on the todo, 2 embedded (one ok vector each), 1
    # failed embedding row.
    blocks = store.chunks.insert_chunks(
        todo_id,
        [
            ChunkInsert(ord=i, text=f"body {i}", meta={"chunk_kind": "paragraph"})
            for i in range(4)
        ],
    )
    vec = hub.embed_one("test")
    with store.pool.connection() as conn:
        for block in blocks[:2]:
            conn.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
                "VALUES (%s, 'bge-m3', %s, 'ok')",
                (block.id, vec),
            )
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, status) "
            "VALUES (%s, 'bge-m3', 'failed')",
            (blocks[2].id,),
        )
        conn.commit()

    body = jobs.get(id="/facts").body
    assert "## refs per kind (5 live, 4 kinds)" in body
    # The retired pres is excluded; each live kind is listed.
    for kind in ("paper", "pres", "datasheet", "todo"):
        assert kind in body
    assert "## chunks and embeddings" in body
    assert "chunks: 4 live" in body
    assert "2 ok vectors" in body
    assert "2 distinct chunks embedded, 50.0% of live chunks" in body
    assert "1 failed rows" in body
    # PDFs by role: paper dedups to one sha, pres one, datasheet one —
    # 1,000 + 3,000 + 1,000 = 5,000 B = 4.9 KiB total.
    assert "## PDFs by role (3 kinds, 4.9 KiB total)" in body
    assert "1,000 B" in body
    assert "2.9 KiB" in body
    assert "## database" in body
    assert "db size:" in body
    # No deploy-side writer has stamped the backup contract.
    assert "no in-DB record" in body
    assert "backup.last_run_at" in body
    assert body.startswith("# facts")
    assert "served: " in body
    assert "precis-status" in body


def test_facts_renders_recorded_backup(jobs: JobHandler, store: Store) -> None:
    _drop_backup_keys(store)
    try:
        store.set_setting("backup.last_run_at", "2026-11-09T03:30:00Z")
        store.set_setting("backup.last_size_bytes", str(22 * 1024**3))
        body = jobs.get(id="/facts").body
        assert "backup: 22.0 GiB" in body
        assert "2026-11-09T03:30:00Z" in body
        assert "no in-DB record" not in body
    finally:
        _drop_backup_keys(store)


def test_facts_rejects_unknown_filters(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="unknown /facts filter"):
        jobs.get(id="/facts?kind=paper")
