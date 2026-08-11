"""Slice 7 part 2: local serving-slot reservation around a dispatch.

Covers both faces:
* **dark** — no store bound, or a model this host doesn't serve → ``acquire``
  returns ``None`` (dispatch proceeds unreserved, as today).
* **active** — a model with a seeded ``llm:<model>`` slot on this host reserves
  (decrementing ``free``), refuses (``paused``) when full, and refunds on
  ``release``. This is the path that lights up the moment ``served_by`` is
  populated — no flag, so "we switch shortly" is just seeding the catalog.

Real-PG (the ``store`` fixture) so the reserve/release SQL runs for real.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.budget import meter
from precis.utils.llm import local_serving


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Each test: a stable host name, a clean cache, and an unbound store after."""
    monkeypatch.setenv("PRECIS_HOST_NAME", "testnode")
    local_serving.reset_cache()
    yield
    meter.bind_store(None)
    local_serving.reset_cache()


def _serve(store: Any, host: str, model: str, cap: int) -> None:
    store.reconcile_llm_served_slots({(host, f"llm:{model}"): cap})
    local_serving.reset_cache()  # slot set changed


# ── dark path ─────────────────────────────────────────────────────────────


def test_acquire_none_without_store() -> None:
    meter.bind_store(None)
    assert local_serving.acquire("any-model") is None


def test_acquire_none_when_model_not_served(store) -> None:
    meter.bind_store(store)
    # nothing seeded for testnode → dark no-op
    assert local_serving.acquire("unserved-model") is None


def test_acquire_none_for_empty_model(store) -> None:
    meter.bind_store(store)
    assert local_serving.acquire("") is None


def test_no_warning_when_host_serves_nothing(store, caplog) -> None:
    """Fully dark host (no `llm:` resource_slots rows at all) — no warning."""
    meter.bind_store(store)
    with caplog.at_level("WARNING", logger="precis.utils.llm.local_serving"):
        assert local_serving.acquire("unserved-model") is None
    assert not caplog.records


def test_warning_when_host_serves_others_but_not_this_one(store, caplog) -> None:
    """Host serves ``qwen`` but the dispatch asked for a differently-named
    resource — a real misconfiguration, must be logged (not fully dark)."""
    meter.bind_store(store)
    _serve(store, "testnode", "qwen", 2)
    with caplog.at_level("WARNING", logger="precis.utils.llm.local_serving"):
        slot = local_serving.acquire("qwen-alias-mismatch")
    assert slot is None
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "testnode" in msg
    assert "llm:qwen-alias-mismatch" in msg
    assert "llm:qwen" in msg


def test_no_warning_for_local_only_alias_mismatch(store, caplog) -> None:
    """The SMALL-tier loopback aliases (``summarizer`` / ``rake-lemma``) are
    local-only by design — they route through the loopback local transport, not a
    reserved llama-swap slot — so a served host being asked for one is the
    *intended* path, not a misconfiguration, and must NOT warn (gr178498: the
    false-alarm flood, 3907 hits/48h on melchior, all ``summarizer``)."""
    meter.bind_store(store)
    _serve(store, "testnode", "qwen", 2)
    with caplog.at_level("WARNING", logger="precis.utils.llm.local_serving"):
        assert local_serving.acquire("summarizer") is None
        assert local_serving.acquire("rake-lemma") is None
    assert not caplog.records


def test_no_warning_for_cloud_model_mismatch(store, caplog) -> None:
    """A legitimately-cloud model (frontier tier) shares no family with the OSS
    models a host serves locally, so falling back to local/cloud is CORRECT,
    not a served_by misconfiguration — it must NOT warn (gr178888: the same
    false-alarm class as the summarizer aliases, but for frontier models like
    ``claude-opus-4-8`` observed on melchior)."""
    meter.bind_store(store)
    _serve(store, "testnode", "qwen3-next-80b-a3b-q4_k_m", 2)
    with caplog.at_level("WARNING", logger="precis.utils.llm.local_serving"):
        assert local_serving.acquire("claude-opus-4-8") is None
        assert local_serving.acquire("gpt-5") is None
    assert not caplog.records


def test_warning_for_same_family_quant_mismatch(store, caplog) -> None:
    """A genuine served_by near-miss — same model family, a dropped quant/suffix
    (``qwen3-next-80b`` vs the served ``qwen3-next-80b-a3b-q4_k_m``) — is exactly
    what the warning exists to catch, and must still fire (gr178888 must not
    over-suppress real mismatches)."""
    meter.bind_store(store)
    _serve(store, "testnode", "qwen3-next-80b-a3b-q4_k_m", 2)
    with caplog.at_level("WARNING", logger="precis.utils.llm.local_serving"):
        assert local_serving.acquire("qwen3-next-80b") is None
    assert len(caplog.records) == 1
    assert "llm:qwen3-next-80b" in caplog.records[0].getMessage()


def test_mismatch_warning_not_repeated_within_cache_window(store, caplog) -> None:
    """Same (host, resource) mismatch on a second call within the same 60s
    cache window logs only once — rate-limited, not silent-forever."""
    meter.bind_store(store)
    _serve(store, "testnode", "qwen", 2)
    with caplog.at_level("WARNING", logger="precis.utils.llm.local_serving"):
        assert local_serving.acquire("qwen-alias-mismatch") is None
        assert local_serving.acquire("qwen-alias-mismatch") is None
    assert len(caplog.records) == 1


# ── active path ───────────────────────────────────────────────────────────


def test_acquire_reserves_a_served_model(store) -> None:
    meter.bind_store(store)
    _serve(store, "testnode", "qwen", 2)
    slot = local_serving.acquire("qwen")
    assert slot is not None and slot.reserved and not slot.paused
    # free dropped from 2 → 1
    free = {s.resource: s.free for s in store.resource_slots_for_host("testnode")}
    assert free["llm:qwen"] == 1
    # release refunds
    local_serving.release(slot)
    free = {s.resource: s.free for s in store.resource_slots_for_host("testnode")}
    assert free["llm:qwen"] == 2


def test_acquire_pauses_when_all_slots_busy(store) -> None:
    meter.bind_store(store)
    _serve(store, "testnode", "solo", 1)
    first = local_serving.acquire("solo")
    assert first is not None and first.reserved
    # capacity 1, now full → next acquire is a pause, not a reservation
    second = local_serving.acquire("solo")
    assert second is not None and second.paused and not second.reserved
    # releasing the pause is a no-op (nothing was reserved); free stays 0
    local_serving.release(second)
    free = {s.resource: s.free for s in store.resource_slots_for_host("testnode")}
    assert free["llm:solo"] == 0
    # releasing the real reservation frees it back up
    local_serving.release(first)
    free = {s.resource: s.free for s in store.resource_slots_for_host("testnode")}
    assert free["llm:solo"] == 1


def test_release_none_and_unreserved_are_noops(store) -> None:
    meter.bind_store(store)
    local_serving.release(None)  # must not raise
    paused = local_serving.LocalSlot("testnode", "llm:x", reserved=False, paused=True)
    local_serving.release(paused)  # unreserved → no-op, must not raise


def test_only_this_host_slots_count(store) -> None:
    """A model served on ANOTHER host is not served *here* → dark no-op."""
    meter.bind_store(store)
    store.reconcile_llm_served_slots({("otherhost", "llm:remote"): 4})
    local_serving.reset_cache()
    assert local_serving.acquire("remote") is None


# ── served_locally: read-only membership test (no reservation) ─────────────


def test_served_locally_true_when_host_serves_the_model(store) -> None:
    meter.bind_store(store)
    _serve(store, "testnode", "qwen", 2)
    assert local_serving.served_locally("qwen") is True


def test_served_locally_false_for_a_different_model(store) -> None:
    meter.bind_store(store)
    _serve(store, "testnode", "qwen", 2)
    assert local_serving.served_locally("summarizer") is False


def test_served_locally_false_without_store() -> None:
    meter.bind_store(None)
    assert local_serving.served_locally("qwen") is False


# ── endpoint enrichment (the Phase-2 litellm-retire flip) ──────────────────


def _serve_card(
    store: Any,
    host: str,
    model_id: str,
    cap: int,
    *,
    endpoint: str | None = None,
    served_model: str | None = None,
) -> None:
    """Seed BOTH a real ``llm`` card (with ``served_by`` — the endpoint source)
    and its ``resource_slots`` counter row, so ``acquire`` reserves *and* can
    enrich with the declared endpoint."""
    from precis import llm_catalog

    rid, _ = llm_catalog.upsert_card(store, model_id=model_id, text=f"{model_id} card.")
    entry: dict[str, Any] = {"host": host, "max_parallel": cap}
    if endpoint is not None:
        entry["endpoint"] = endpoint
    if served_model is not None:
        entry["model"] = served_model
    store.update_ref(rid, meta_patch={"served_by": [entry]})
    store.reconcile_llm_served_slots({(host, f"llm:{model_id}"): cap})
    local_serving.reset_cache()


def test_endpoint_enriches_a_reserved_slot(store) -> None:
    """A ``served_by.endpoint`` on the card → the reserved slot carries the direct
    llama-swap URL + server-side model name (what the router routes to)."""
    meter.bind_store(store)
    _serve_card(
        store,
        "testnode",
        "qwen-local",
        2,
        endpoint="http://127.0.0.1:11445/v1",
        served_model="qwen3-next-80b-a3b-q4_k_m",
    )
    slot = local_serving.acquire("qwen-local")
    assert slot is not None and slot.reserved
    assert slot.endpoint == "http://127.0.0.1:11445/v1"
    assert slot.served_model == "qwen3-next-80b-a3b-q4_k_m"


def test_served_by_without_endpoint_stays_slot_only(store) -> None:
    """No ``endpoint`` declared → the reserved slot's endpoint is ``None`` (today's
    slot-only behavior; the call still goes to the litellm proxy). The served
    model defaults to the card's ``model_id``."""
    meter.bind_store(store)
    _serve_card(store, "testnode", "qwen-noep", 1)
    slot = local_serving.acquire("qwen-noep")
    assert slot is not None and slot.reserved
    assert slot.endpoint is None
    assert slot.served_model == "qwen-noep"


# ── cluster-scoped serving (routable served_by on another host) ────────────


def test_remote_routable_endpoint_is_acquirable(store) -> None:
    """A model served on ANOTHER host behind a LAN-routable endpoint is
    acquirable from here: the slot debits the DECLARED host's row (one
    fleet-wide semaphore) and carries the remote endpoint for direct dispatch
    — the path that sends every node's BIG-chain local rung to the DGX-pair
    llama-server instead of the hosted cloud fallback."""
    meter.bind_store(store)
    _serve_card(
        store,
        "otherhost",
        "remote-big",
        2,
        endpoint="http://192.168.6.197:8080/v1",
        served_model="deepseek-v4-flash-0731",
    )
    slot = local_serving.acquire("remote-big")
    assert slot is not None and slot.reserved and not slot.paused
    assert slot.host == "otherhost"
    assert slot.endpoint == "http://192.168.6.197:8080/v1"
    assert slot.served_model == "deepseek-v4-flash-0731"
    free = {s.resource: s.free for s in store.resource_slots_for_host("otherhost")}
    assert free["llm:remote-big"] == 1
    # release refunds the REMOTE host's row (slot.host is the accounting key)
    local_serving.release(slot)
    free = {s.resource: s.free for s in store.resource_slots_for_host("otherhost")}
    assert free["llm:remote-big"] == 2


def test_remote_loopback_endpoint_stays_host_private(store) -> None:
    """A served_by entry on another host whose endpoint is loopback
    (melchior's llama-swap at 127.0.0.1) is NOT reachable from here — the
    cluster-scoped path must skip it and stay dark."""
    meter.bind_store(store)
    _serve_card(
        store,
        "otherhost",
        "remote-loopback",
        2,
        endpoint="http://127.0.0.1:11445/v1",
    )
    assert local_serving.acquire("remote-loopback") is None


def test_remote_endpointless_served_by_stays_dark(store) -> None:
    """A served_by on another host with NO endpoint has nothing to route to
    from here (slot-only serving is host-private) — dark no-op."""
    meter.bind_store(store)
    _serve_card(store, "otherhost", "remote-noep", 2)
    assert local_serving.acquire("remote-noep") is None


def test_remote_acquire_pauses_when_fleet_cap_full(store) -> None:
    """The remote host's max_parallel caps the whole fleet: when its row is
    exhausted, a cluster-scoped acquire pauses (back off / hosted escape),
    never oversubscribes the server."""
    meter.bind_store(store)
    _serve_card(
        store,
        "otherhost",
        "remote-solo",
        1,
        endpoint="http://192.168.6.197:8080/v1",
    )
    first = local_serving.acquire("remote-solo")
    assert first is not None and first.reserved
    second = local_serving.acquire("remote-solo")
    assert second is not None and second.paused and not second.reserved
    assert second.endpoint is None  # a paused slot routes nowhere
    local_serving.release(first)


def test_local_serving_wins_over_remote(store) -> None:
    """When THIS host serves the model too, the local entry wins — the remote
    path is only consulted on a local-serve miss."""
    meter.bind_store(store)
    from precis import llm_catalog

    rid, _ = llm_catalog.upsert_card(store, model_id="both-hosts", text="both.")
    store.update_ref(
        rid,
        meta_patch={
            "served_by": [
                {
                    "host": "otherhost",
                    "max_parallel": 4,
                    "endpoint": "http://192.168.6.197:8080/v1",
                },
                {
                    "host": "testnode",
                    "max_parallel": 2,
                    "endpoint": "http://127.0.0.1:11445/v1",
                },
            ]
        },
    )
    store.reconcile_llm_served_slots(
        {("otherhost", "llm:both-hosts"): 4, ("testnode", "llm:both-hosts"): 2}
    )
    local_serving.reset_cache()
    slot = local_serving.acquire("both-hosts")
    assert slot is not None and slot.reserved
    assert slot.host == "testnode"
    assert slot.endpoint == "http://127.0.0.1:11445/v1"


def test_endpoint_is_host_scoped(store) -> None:
    """A card served on two hosts with different endpoints → *this* host's slot
    carries *this* host's endpoint, never the other's."""
    meter.bind_store(store)
    from precis import llm_catalog

    rid, _ = llm_catalog.upsert_card(store, model_id="qwen-multi", text="multi.")
    store.update_ref(
        rid,
        meta_patch={
            "served_by": [
                {
                    "host": "otherhost",
                    "max_parallel": 4,
                    "endpoint": "http://otherhost:11445/v1",
                },
                {
                    "host": "testnode",
                    "max_parallel": 2,
                    "endpoint": "http://127.0.0.1:11445/v1",
                },
            ]
        },
    )
    store.reconcile_llm_served_slots(
        {("otherhost", "llm:qwen-multi"): 4, ("testnode", "llm:qwen-multi"): 2}
    )
    local_serving.reset_cache()
    slot = local_serving.acquire("qwen-multi")
    assert slot is not None and slot.reserved
    assert slot.endpoint == "http://127.0.0.1:11445/v1"


def test_seeded_slullama_card_reserves_and_caps(store: Any) -> None:
    """The static remote-cluster tunnel seeder (:func:`llm_catalog.seed_slullama_card`)
    produces a card whose ``served_by`` gives ``acquire`` the tunnel endpoint +
    server-side model, and whose ``max_parallel`` is the real concurrency cap —
    a 3rd concurrent acquire pauses rather than reserving."""
    from precis import llm_catalog

    meter.bind_store(store)
    _ref_id, created = llm_catalog.seed_slullama_card(
        store,
        endpoint="http://127.0.0.1:11500/v1",
        model="qwen3-235b-a22b",
        host="testnode",
        max_parallel=2,
        model_id="qwen-hpc-test",
    )
    assert created
    store.reconcile_llm_served_slots({("testnode", "llm:qwen-hpc-test"): 2})
    local_serving.reset_cache()

    first = local_serving.acquire("qwen-hpc-test")
    assert first is not None and first.reserved
    assert first.endpoint == "http://127.0.0.1:11500/v1"
    assert first.served_model == "qwen3-235b-a22b"

    second = local_serving.acquire("qwen-hpc-test")
    assert second is not None and second.reserved  # cap is 2, still free

    third = local_serving.acquire("qwen-hpc-test")
    assert third is not None and third.paused and not third.reserved  # cap hit

    local_serving.release(first)
    local_serving.release(second)
    local_serving.release(third)
    free = {s.resource: s.free for s in store.resource_slots_for_host("testnode")}
    assert free["llm:qwen-hpc-test"] == 2


# ── crash-safe reclaim (resource_slot_holds, migration 0118) ───────────────


def test_acquire_creates_a_hold_carried_on_the_slot(store) -> None:
    meter.bind_store(store)
    _serve(store, "testnode", "qwen", 2)
    slot = local_serving.acquire("qwen")
    assert slot is not None and slot.reserved
    assert slot.hold_id is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT host, resource, units FROM resource_slot_holds WHERE id = %s",
            (slot.hold_id,),
        ).fetchone()
    assert row == ("testnode", "llm:qwen", 1)


def test_release_deletes_the_hold_and_refunds(store) -> None:
    meter.bind_store(store)
    _serve(store, "testnode", "qwen", 2)
    slot = local_serving.acquire("qwen")
    assert slot is not None and slot.hold_id is not None
    local_serving.release(slot)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM resource_slot_holds WHERE id = %s", (slot.hold_id,)
        ).fetchone()
    assert row is None  # hold closed
    free = {s.resource: s.free for s in store.resource_slots_for_host("testnode")}
    assert free["llm:qwen"] == 2  # refunded


def test_release_after_sweep_already_reclaimed_does_not_double_refund(store) -> None:
    """The heartbeat sweep beats a late ``release`` to an expired hold — the
    refund already happened, so ``release`` must not refund a second time.

    Capacity (5) deliberately stays above the reservation count so a stray
    double refund is NOT masked by the ``LEAST(capacity, ...)`` clamp — it
    would show up as an extra unit of ``free``."""
    meter.bind_store(store)
    _serve(store, "testnode", "qwen", 5)
    first = local_serving.acquire("qwen")
    second = local_serving.acquire("qwen")
    third = local_serving.acquire("qwen")
    assert first and second and third
    assert first.hold_id is not None and second.hold_id is not None

    # backdate only `second`'s hold so the sweep treats it as expired
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE resource_slot_holds SET expires_at = now() - interval '1 second' "
            "WHERE id = %s",
            (second.hold_id,),
        )
        conn.commit()
    n = store.reclaim_expired_slot_holds()
    assert n == 1
    free = {s.resource: s.free for s in store.resource_slots_for_host("testnode")}
    assert free["llm:qwen"] == 3  # 5 - 3 reserved + 1 (second's unit) refunded

    # the "leaked" process finally calls release — must be a no-op, not a
    # second refund (which would push free to 4, headroom that isn't real).
    local_serving.release(second)
    free = {s.resource: s.free for s in store.resource_slots_for_host("testnode")}
    assert free["llm:qwen"] == 3  # unchanged

    local_serving.release(first)
    local_serving.release(third)
    free = {s.resource: s.free for s in store.resource_slots_for_host("testnode")}
    assert free["llm:qwen"] == 5  # everything legitimately released lands at full


def test_paused_acquire_creates_no_hold(store) -> None:
    meter.bind_store(store)
    _serve(store, "testnode", "solo", 1)
    first = local_serving.acquire("solo")
    assert first is not None and first.reserved
    second = local_serving.acquire("solo")
    assert second is not None and second.paused and second.hold_id is None
    local_serving.release(first)
