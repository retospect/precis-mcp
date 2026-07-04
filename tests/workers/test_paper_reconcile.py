"""The scheduled paper-dedup reconcile pass — merge + cadence throttle."""

from __future__ import annotations

from precis.store import Store
from precis.workers.paper_reconcile import _STATE_KEY, run_paper_reconcile_pass


def _held(store: Store, *, n: int, slug: str, title: str, year: int = 2017) -> int:
    ref = store.insert_ref(
        kind="paper", slug=slug, title=title, authors=[{"name": "A"}], year=year
    )
    sha = f"{n:064x}"
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '') "
                "ON CONFLICT (pdf_sha256) DO NOTHING",
                (sha, sha),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id)
            )
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'paragraph', %s)",
                (ref.id, f"body of {title}"),
            )
    return ref.id


def _deleted(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT deleted_at IS NOT NULL FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return bool(row[0])


def test_pass_merges_when_due_then_throttles(store: Store) -> None:
    held = _held(store, n=1, slug="vaswani17", title="Attention Is All You Need")
    stub = store.insert_ref(
        kind="paper", slug="attention17", title="Attention Is All You Need", year=2017
    ).id

    # Force "due" regardless of any marker left by a prior test.
    store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

    r1 = run_paper_reconcile_pass(store)
    assert r1.claimed >= 1
    assert _deleted(store, stub) is True
    assert _deleted(store, held) is False

    # The pass just stamped the marker to now → the next tick is throttled.
    r2 = run_paper_reconcile_pass(store)
    assert r2.claimed == 0
