"""Edited draft chunks re-derive via the content_sha claim (ADR 0033 §4)."""

from __future__ import annotations

from precis.store.store import Store
from precis.workers.summarize import RakeLemmaHandler


def _claimed_ids(store: Store, handler: RakeLemmaHandler) -> list[int]:
    with store.pool.connection() as conn:
        ids = [r.chunk_id for r in handler.claim_batch(conn, limit=50)]
        conn.rollback()  # release FOR UPDATE locks; claim only
    return ids


def test_edit_reclaims_chunk_for_rederive(store: Store) -> None:
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    ref, title = store.create_draft(name="nt", title="T", project_ref_id=proj)
    p = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="nanoscale transistor leakage current density",
        at={"after": title.handle},
    )[0]
    h = RakeLemmaHandler()

    # 1) a fresh chunk has no summary → claimed; derive + persist it.
    # Mirror the runner's phase 3: write_ok THEN release_claims in the same
    # txn, so no lingering chunk_claims row blocks the later re-claim (the
    # lease model — an unreleased claim keeps a chunk out of _claim_fresh
    # until the 20-min reclaim cooldown).
    with store.pool.connection() as conn:
        rows = h.claim_batch(conn, limit=50)
        assert p.chunk_id in [r.chunk_id for r in rows]
        row = next(r for r in rows if r.chunk_id == p.chunk_id)
        h.write_ok(conn, p.chunk_id, h.process(row))  # stamps content_sha
        h.release_claims(conn, [r.chunk_id for r in rows])
        conn.commit()

    # 2) already-derived, unchanged → NOT re-claimed (sha matches)
    assert p.chunk_id not in _claimed_ids(store, h)

    # 3) after an in-place edit → re-claimed (content_sha changed)
    store.edit_text(p.handle, "graphene channel mobility enhancement factor")
    assert p.chunk_id in _claimed_ids(store, h)
