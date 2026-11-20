"""``read_receipts`` — durable owner-keyed exact-read receipts (gr660588).

The store mixin behind ``runtime.handle_deref``'s durable path: a
``text={'$ref': handle}`` receipt keyed on the authenticated owner so it
survives an MCP restart and works under ``stateless_http``.
"""

from __future__ import annotations

from precis.store import Store


def test_receipt_round_trip_expiry_and_prune(store: Store) -> None:
    """``has_receipt`` is true inside the TTL, false past it — and never
    for another owner — and ``prune_read_receipts`` deletes the expired."""
    owner, other = "oauth:7", "oauth:8"
    ident = "paper:10:20"

    store.record_receipt(owner_key=owner, identity=ident)
    assert store.has_receipt(owner_key=owner, identity=ident)
    assert not store.has_receipt(owner_key=other, identity=ident)
    assert not store.has_receipt(owner_key=owner, identity="paper:10:-")

    # Backdate past the TTL: the receipt lapses, then prune removes it.
    with store.pool.connection() as conn:
        conn.execute("UPDATE read_receipts SET expires_at = now() - interval '1 hour'")
        conn.commit()
    assert not store.has_receipt(owner_key=owner, identity=ident)

    assert store.prune_read_receipts() == 1
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM read_receipts").fetchone()
    assert row is not None and row[0] == 0


def test_receipt_rerecord_renews_and_write_sweeps_expired(store: Store) -> None:
    """A re-read renews the row (upsert), and each write sweeps the
    owner's expired rows so the table stays bounded without a sweeper."""
    owner = "personal:42"
    store.record_receipt(owner_key=owner, identity="memory:1:-")
    with store.pool.connection() as conn:
        conn.execute("UPDATE read_receipts SET expires_at = now() - interval '1 hour'")
        conn.commit()
    # The next write sweeps the stale row before upserting the new one.
    store.record_receipt(owner_key=owner, identity="memory:2:-")
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT identity FROM read_receipts WHERE owner_key = %s",
            (owner,),
        ).fetchall()
    assert rows == [("memory:2:-",)]
    assert store.has_receipt(owner_key=owner, identity="memory:2:-")
