"""Conflict-safe reground memo writes (gr693791 residual 5).

Two-writer tests over the hub-row ``FOR UPDATE`` locks:
:func:`~precis.taproot.hub.append_reground_log` serializes concurrent
appends so both survive, :func:`~precis.taproot.hub.remove_evidence`'s
strand guard serializes concurrent removals so the pair can no longer
strand the hub, and ``hub_refine._memo_safe_update`` merges concurrent
seal patches instead of last-writer-wins. One writer blocks on the other's
lock (asserted with a short ``lock_timeout`` negative case), then the
merged end state is read back.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import psycopg
import pytest

from precis.store.types import ChunkInsert
from precis.taproot.canon import CanonicalClaim, claim_sha
from precis.taproot.hub import (
    WouldStrandHub,
    append_reground_log,
    attach_evidence,
    mint_hub,
    remove_evidence,
)
from precis.workers.hub_refine import _memo_safe_update


def _seed_hub(store: Any, *, sentence: str = "A testable claim about X.") -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))


def _seed_edge(store: Any, *, hub: int, n: int) -> tuple[int, int]:
    """Attach evidence edge ``n`` (a fresh paper + chunk) to ``hub``."""
    ref = store.insert_ref(
        kind="paper", slug=f"lock-p{n}", title=f"Lock paper {n}", meta={}
    )
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=f"Passage {n}.")])
    with store.pool.connection() as conn:
        chunk_id = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (ref.id,)
        ).fetchone()[0]
        title = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()[0]
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=ref.id,
        role="corroborates",
        meta={
            "support": "yes",
            "source_handle": f"pc{int(chunk_id)}",
            "verified_by": "test-seed",
            "verified_claim_sha": claim_sha(str(title)),
        },
        set_by="system",
        check_retraction=False,
    )
    return ref.id, int(chunk_id)


def _reground_log(store: Any, hub: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'reground_log' FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()
    return list(row[0] or [])


def _rejected(store: Any, hub: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'taproot_rejected' FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()
    return dict(row[0] or {})


def test_concurrent_appends_both_survive(store: Any) -> None:
    """Writer 2 blocks on writer 1's row lock and then extends the list
    writer 1 committed — no lost update."""
    hub = _seed_hub(store)
    done = threading.Event()
    err: list[BaseException] = []

    def _writer(entry: str, barrier: threading.Barrier) -> None:
        try:
            with store.pool.connection() as conn:
                barrier.wait(timeout=10)
                append_reground_log(
                    store, hub, [{"edge": entry, "verdict": "PRUNE"}], conn=conn
                )
                conn.commit()
        except BaseException as exc:
            err.append(exc)
        finally:
            done.set()

    # Writer 1 wins the race deterministically: it signals AFTER its lock
    # is taken (the append returned) but before committing; writer 2 only
    # starts once writer 1 holds the lock, so writer 2 must block.
    entered = threading.Event()
    with store.pool.connection() as c1:
        append_reground_log(store, hub, [{"edge": "first"}], conn=c1)
        entered.set()
        barrier = threading.Barrier(2)
        t2 = threading.Thread(target=_writer, args=("second", barrier), daemon=True)
        t2.start()
        barrier.wait(timeout=10)
        # Writer 2 is now blocked on the hub row lock (writer 1 uncommitted).
        assert not done.wait(timeout=0.5)
        c1.commit()
        t2.join(timeout=30)
        assert not t2.is_alive()
    assert not err, f"concurrent append raised: {err!r}"
    log = _reground_log(store, hub)
    assert [e["edge"] for e in log] == ["first", "second"]


def test_append_blocks_without_lock_holder_committing(store: Any) -> None:
    """The FOR UPDATE is real: a second writer with a short lock_timeout
    raises LockNotAvailable while the first holds the row uncommitted."""
    hub = _seed_hub(store)
    with store.pool.connection() as c1:
        append_reground_log(store, hub, [{"edge": "held"}], conn=c1)
        with store.pool.connection() as c2:
            c2.execute("SET lock_timeout = '300ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                append_reground_log(store, hub, [{"edge": "blocked"}], conn=c2)
            c2.rollback()
        c1.commit()
    assert [e["edge"] for e in _reground_log(store, hub)] == ["held"]


def test_concurrent_removals_cannot_strand_the_hub(store: Any) -> None:
    """Two removals of different edges on one hub serialize on the row
    lock: the second sees the first's committed delete and the strand
    guard refuses it."""
    hub = _seed_hub(store)
    p1, c1 = _seed_edge(store, hub=hub, n=1)
    p2, c2 = _seed_edge(store, hub=hub, n=2)

    held = threading.Event()
    done = threading.Event()
    err: list[BaseException] = []

    def _second_remover() -> None:
        try:
            with store.pool.connection() as conn:
                held.wait(timeout=10)
                time.sleep(0.2)  # ensure the first tx holds the lock first
                with pytest.raises(WouldStrandHub):
                    remove_evidence(
                        store,
                        hub_ref_id=hub,
                        src_ref_id=p2,
                        src_chunk_id=c2,
                        role="corroborates",
                        reason="second concurrent prune",
                    )
        except BaseException as exc:
            err.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=_second_remover, daemon=True)
    t.start()
    # First removal: its own transaction commits before returning.
    remove_evidence(
        store,
        hub_ref_id=hub,
        src_ref_id=p1,
        src_chunk_id=c1,
        role="corroborates",
        reason="first concurrent prune",
    )
    held.set()
    t.join(timeout=30)
    assert not t.is_alive()
    assert not err, f"second remover raised: {err!r}"
    assert done.is_set()


def test_remove_evidence_takes_the_hub_row_lock(store: Any) -> None:
    """A held (uncommitted) removal blocks a second remove_evidence's
    strand guard on the same row — LockNotAvailable, not a stale count."""
    hub = _seed_hub(store)
    p1, c1 = _seed_edge(store, hub=hub, n=1)
    p2, c2 = _seed_edge(store, hub=hub, n=2)
    with store.pool.connection() as conn:
        n = remove_evidence(
            store,
            hub_ref_id=hub,
            src_ref_id=p1,
            src_chunk_id=c1,
            role="corroborates",
            reason="held removal",
            conn=conn,
        )
        assert n == 1
        with store.pool.connection() as other:
            other.execute("SET lock_timeout = '300ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                remove_evidence(
                    store,
                    hub_ref_id=hub,
                    src_ref_id=p2,
                    src_chunk_id=c2,
                    role="corroborates",
                    reason="blocked removal",
                    conn=other,
                )
            other.rollback()
        conn.commit()
    # After the commit the guard sees the delete: one edge left, removal
    # of it would strand -> refused.
    with pytest.raises(WouldStrandHub):
        remove_evidence(
            store,
            hub_ref_id=hub,
            src_ref_id=p2,
            src_chunk_id=c2,
            role="corroborates",
            reason="would strand",
        )


def test_memo_safe_update_merges_concurrent_seal_patches(store: Any) -> None:
    """Two passes sealing different rejection-memo entries concurrently
    both land — union, not last-writer-wins."""
    hub = _seed_hub(store)
    store.update_ref(hub, meta_patch={"taproot_rejected": {"9": {"supports": "no"}}})
    entered = threading.Event()
    done = threading.Event()
    err: list[BaseException] = []

    def _writer(key: str) -> None:
        try:
            with store.pool.connection() as conn:
                entered.wait(timeout=10)
                time.sleep(0.2)  # hold the lock ordering: main thread first
                _memo_safe_update(
                    store,
                    conn,
                    hub,
                    {"taproot_rejected": {key: {"supports": "no"}}},
                    replace=False,
                )
                conn.commit()
        except BaseException as exc:
            err.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=_writer, args=("11",), daemon=True)
    t.start()
    with store.pool.connection() as conn:
        entered.set()
        _memo_safe_update(
            store,
            conn,
            hub,
            {"taproot_rejected": {"10": {"supports": "no"}}},
            replace=False,
        )
        conn.commit()
    t.join(timeout=30)
    assert not t.is_alive()
    assert not err, f"concurrent seal raised: {err!r}"
    assert sorted(_rejected(store, hub)) == ["10", "11", "9"]


def test_memo_safe_update_replace_clears_stale_entries(store: Any) -> None:
    """A sha-reopen (replace=True) keeps the clear wholesale even though
    the write now runs under the lock; a normal write unions."""
    hub = _seed_hub(store)
    store.update_ref(hub, meta_patch={"taproot_rejected": {"9": {"supports": "no"}}})
    with store.pool.connection() as conn:
        _memo_safe_update(store, conn, hub, {"taproot_rejected": {}}, replace=True)
        conn.commit()
    assert _rejected(store, hub) == {}
    with store.pool.connection() as conn:
        _memo_safe_update(
            store,
            conn,
            hub,
            {"taproot_rejected": {"10": {"supports": "no"}}},
            replace=False,
        )
        conn.commit()
    assert sorted(_rejected(store, hub)) == ["10"]
