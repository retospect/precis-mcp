"""Real-PG tests for the ``resource_slots`` store ops (slice 6b).

The heartbeat self-probe hands :meth:`sync_host_resource_slots` a
``{resource: capacity|None}`` verdict; these prove the three-way sync
discipline (present→upsert, absent→delete, unknown→leave), the
capacity-delta free adjustment, and the read helpers.
"""

from __future__ import annotations


def test_sync_upserts_present_capabilities(store) -> None:
    store.sync_host_resource_slots("melchior", {"gpu": 1, "podman": 2})
    slots = {s.resource: s for s in store.resource_slots_for_host("melchior")}
    assert slots["gpu"].capacity == 1
    assert slots["gpu"].free == 1  # nothing reserved yet
    assert slots["gpu"].kind == "hard"
    assert slots["podman"].capacity == 2
    assert slots["podman"].free == 2


def test_sync_deletes_absent_capabilities(store) -> None:
    """A capacity of 0 retracts the row (host definitively can't)."""
    store.sync_host_resource_slots("caspar", {"gpu": 1})
    assert any(s.resource == "gpu" for s in store.resource_slots_for_host("caspar"))
    # next cycle: gpu gone (0), podman appears
    store.sync_host_resource_slots("caspar", {"gpu": 0, "podman": 2})
    resources = {s.resource for s in store.resource_slots_for_host("caspar")}
    assert resources == {"podman"}


def test_sync_leaves_unknown_untouched(store) -> None:
    """A None verdict (probe couldn't tell) must not retract a live row."""
    store.sync_host_resource_slots("spark", {"gpu": 1})
    # a transient probe failure reports gpu=None; the row must survive
    store.sync_host_resource_slots("spark", {"gpu": None})
    slots = store.resource_slots_for_host("spark")
    assert [s.resource for s in slots] == ["gpu"]
    assert slots[0].capacity == 1


def test_embedder_probe_timeout_leaves_row_untouched(store, monkeypatch) -> None:
    """A configured-but-timing-out embedder probe reads ``unknown`` (None),
    not ``absent`` (0) — the capacity-1 row must survive a transient
    ``/readyz`` miss (e.g. a 3s timeout while the daemon is busy serving a
    batch), not get deleted out from under the claim path's reservation
    gate."""
    from precis.workers import capability_probe as cap

    store.sync_host_resource_slots("melchior", {"embedder": 1})
    monkeypatch.setenv("PRECIS_EMBEDDER_URL", "http://127.0.0.1:8181")
    monkeypatch.delenv("PRECIS_EMBEDDER_SLOTS", raising=False)
    monkeypatch.setattr(cap, "_embedder_ready", lambda url: False)

    verdict = cap._probe_embedder()
    assert verdict is None
    store.sync_host_resource_slots("melchior", {"embedder": verdict})

    slots = store.resource_slots_for_host("melchior")
    assert [s.resource for s in slots] == ["embedder"]
    assert slots[0].capacity == 1


def test_capacity_change_adjusts_free_by_delta(store) -> None:
    """Growing capacity grows free by the same delta (no stomp)."""
    store.sync_host_resource_slots("melchior", {"podman": 2})
    # simulate slice-6c reserving one slot out of band
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE resource_slots SET free = 1 "
            "WHERE host = 'melchior' AND resource = 'podman'"
        )
        conn.commit()
    # capacity bumped 2 → 4: free should follow the +2 delta (1 → 3),
    # NOT reset to capacity (which would drop the live reservation).
    store.sync_host_resource_slots("melchior", {"podman": 4})
    slot = store.resource_slots_for_host("melchior")[0]
    assert slot.capacity == 4
    assert slot.free == 3


def test_capacity_shrink_never_exceeds_capacity(store) -> None:
    """Shrinking capacity keeps the free<=capacity invariant."""
    store.sync_host_resource_slots("melchior", {"gpu": 4})
    store.sync_host_resource_slots("melchior", {"gpu": 1})
    slot = store.resource_slots_for_host("melchior")[0]
    assert slot.capacity == 1
    assert slot.free <= slot.capacity


def test_all_resource_slots_orders_by_host_then_resource(store) -> None:
    store.sync_host_resource_slots("melchior", {"tts": 1, "gpu": 1})
    store.sync_host_resource_slots("caspar", {"podman": 2})
    rows = [(s.host, s.resource) for s in store.all_resource_slots()]
    # both hosts present, sorted
    assert ("caspar", "podman") in rows
    assert ("melchior", "gpu") in rows
    assert ("melchior", "tts") in rows
    assert rows == sorted(rows)


def test_sync_kinds_override(store) -> None:
    store.sync_host_resource_slots("melchior", {"mem": 64}, kinds={"mem": "soft"})
    slot = store.resource_slots_for_host("melchior")[0]
    assert slot.resource == "mem"
    assert slot.kind == "soft"


def test_sync_soft_signal_sets_free_directly(store) -> None:
    # A gauge write: free set to the measured headroom, not the hard delta path.
    store.sync_soft_signal("sh", "mem", 0, 2)
    by_res = {s.resource: s for s in store.resource_slots_for_host("sh")}
    assert by_res["mem"].kind == "soft"
    assert by_res["mem"].free == 0 and by_res["mem"].capacity == 2
    # None (unmeasurable) leaves the existing row untouched
    store.sync_soft_signal("sh", "mem", None, 2)
    assert {s.resource: s.free for s in store.resource_slots_for_host("sh")}["mem"] == 0
    # a later measurement of plenty updates free directly (no delta math)
    store.sync_soft_signal("sh", "mem", 2, 2)
    assert {s.resource: s.free for s in store.resource_slots_for_host("sh")}["mem"] == 2


def test_delete_soft_signal_retracts_row(store) -> None:
    """A soft gauge can be definitively retracted (host opted out) — idempotent."""
    store.sync_soft_signal("dh", "container_agent", 0, 1)
    assert store.resource_slots_for_host("dh")  # row present
    store.delete_soft_signal("dh", "container_agent")
    assert not store.resource_slots_for_host("dh")  # gone
    # idempotent — deleting an absent soft row is a no-op.
    store.delete_soft_signal("dh", "container_agent")


def test_delete_soft_signal_spares_hard_namesake(store) -> None:
    """The delete is scoped to ``kind='soft'`` — a hard capability row of the
    same name (were one ever present) is never collateral."""
    store.sync_host_resource_slots("dh2", {"podman": 3})
    store.delete_soft_signal("dh2", "podman")  # wrong-kind delete is a no-op
    rows = {s.resource: s for s in store.resource_slots_for_host("dh2")}
    assert rows["podman"].kind == "hard" and rows["podman"].capacity == 3


# ── crash-safe reclaim (resource_slot_holds, migration 0118) ───────────────


def _insert_hold(store, host, resource, units, *, ttl_s: float, holder="t:1") -> int:
    from precis.store._resource_slots_ops import insert_slot_hold

    with store.pool.connection() as conn:
        with conn.transaction():
            return insert_slot_hold(conn, host, resource, units, holder, ttl_s)


def test_insert_delete_hold_round_trip(store) -> None:
    from precis.store._resource_slots_ops import delete_slot_hold

    hold_id = _insert_hold(store, "hh", "llm:x", 1, ttl_s=60.0)
    with store.pool.connection() as conn:
        with conn.transaction():
            assert delete_slot_hold(conn, hold_id) is True
            # a second delete of the same id finds nothing
            assert delete_slot_hold(conn, hold_id) is False


def test_reclaim_refunds_only_expired_holds(store) -> None:
    store.sync_host_resource_slots("hh1", {"llm:x": 2})
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE resource_slots SET free = 0 WHERE host = 'hh1' AND resource = 'llm:x'"
        )
        conn.commit()
    _insert_hold(store, "hh1", "llm:x", 1, ttl_s=-1.0)  # already expired
    live_id = _insert_hold(store, "hh1", "llm:x", 1, ttl_s=3600.0)  # not expired

    n = store.reclaim_expired_slot_holds()
    assert n == 1
    free = {s.resource: s.free for s in store.resource_slots_for_host("hh1")}
    assert free["llm:x"] == 1  # only the expired hold's unit refunded

    # the live hold is still present (not swept)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM resource_slot_holds WHERE id = %s", (live_id,)
        ).fetchone()
    assert row is not None


def test_reclaim_sums_multiple_expired_holds(store) -> None:
    store.sync_host_resource_slots("hh2", {"llm:x": 4})
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE resource_slots SET free = 0 WHERE host = 'hh2' AND resource = 'llm:x'"
        )
        conn.commit()
    _insert_hold(store, "hh2", "llm:x", 1, ttl_s=-1.0)
    _insert_hold(store, "hh2", "llm:x", 2, ttl_s=-1.0)

    n = store.reclaim_expired_slot_holds()
    assert n == 2
    free = {s.resource: s.free for s in store.resource_slots_for_host("hh2")}
    assert free["llm:x"] == 3


def test_reclaim_caps_at_capacity(store) -> None:
    """A hold's refund never pushes ``free`` past ``capacity`` (belt and
    suspenders alongside the ordinary release path's cap)."""
    store.sync_host_resource_slots("hh3", {"llm:x": 1})
    _insert_hold(store, "hh3", "llm:x", 1, ttl_s=-1.0)  # free already == capacity

    n = store.reclaim_expired_slot_holds()
    assert n == 1
    free = {s.resource: s.free for s in store.resource_slots_for_host("hh3")}
    assert free["llm:x"] == 1  # not 2 — clamped


def test_reclaim_ignores_missing_slot_row(store) -> None:
    """A hold whose ``resource_slots`` row is gone (reseeded/deleted by the
    capability probe) is still swept + counted; there's simply nothing to
    refund."""
    _insert_hold(store, "ghost-host", "llm:gone", 1, ttl_s=-1.0)
    n = store.reclaim_expired_slot_holds()
    assert n == 1
    assert store.resource_slots_for_host("ghost-host") == []


def test_reclaim_returns_zero_when_nothing_expired(store) -> None:
    _insert_hold(store, "hh4", "llm:x", 1, ttl_s=3600.0)
    assert store.reclaim_expired_slot_holds() == 0
