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
