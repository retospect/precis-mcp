"""Slice 7 (dark): ``served_by`` on ``llm`` cards → ``resource_slots``.

Proves the three moving parts of 7-code part 1:

* ``llm_served_slots_from_cards`` — the pure extraction (card-level +
  offering-level ``served_by`` → ``(host, "llm:<model>") → max_parallel``).
* ``Store.reconcile_llm_served_slots`` — the namespace-scoped full sync
  (upsert declared, reap stale, never touch hardware rows, reservation-safe).
* ``run_llm_reconcile_pass`` — seeds the slots on a locked pass (dark: nothing
  reserves them yet, litellm routing untouched).

Real-PG (the ``store`` fixture) so the SQL is exercised end to end.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.workers.llm_reconcile import llm_served_slots_from_cards


def _card(model_id: str | None, meta_extra: dict[str, Any]) -> Any:
    meta: dict[str, Any] = {}
    if model_id is not None:
        meta["model_id"] = model_id
    meta.update(meta_extra)
    return SimpleNamespace(meta=meta)


# ── extraction ────────────────────────────────────────────────────────────


def test_extract_card_level_served_by() -> None:
    cards = [
        _card("qwen-heavy", {"served_by": [{"host": "melchior", "max_parallel": 4}]})
    ]
    assert llm_served_slots_from_cards(cards) == {("melchior", "llm:qwen-heavy"): 4}


def test_extract_offering_level_served_by() -> None:
    cards = [
        _card(
            "qwen-heavy",
            {
                "offerings": [
                    {
                        "transport": "openai_compat",
                        "served_by": [
                            {"host": "melchior", "max_parallel": 4},
                            {"host": "balthazar", "max_parallel": 2},
                        ],
                    }
                ]
            },
        )
    ]
    assert llm_served_slots_from_cards(cards) == {
        ("melchior", "llm:qwen-heavy"): 4,
        ("balthazar", "llm:qwen-heavy"): 2,
    }


def test_extract_defaults_missing_max_parallel_to_one() -> None:
    cards = [_card("m", {"served_by": [{"host": "h"}]})]
    assert llm_served_slots_from_cards(cards) == {("h", "llm:m"): 1}


def test_extract_dedups_taking_the_larger_capacity() -> None:
    cards = [
        _card(
            "m",
            {
                "served_by": [{"host": "h", "max_parallel": 2}],
                "offerings": [{"served_by": [{"host": "h", "max_parallel": 5}]}],
            },
        )
    ]
    assert llm_served_slots_from_cards(cards) == {("h", "llm:m"): 5}


def test_extract_skips_cards_without_model_id_or_host() -> None:
    cards = [
        _card(None, {"served_by": [{"host": "h", "max_parallel": 3}]}),  # no model_id
        _card("m", {"served_by": [{"max_parallel": 3}]}),  # no host
        _card("m2", {}),  # no served_by
    ]
    assert llm_served_slots_from_cards(cards) == {}


# ── store reconcile ───────────────────────────────────────────────────────


def test_reconcile_upserts_and_is_reported(store) -> None:
    up, deleted = store.reconcile_llm_served_slots(
        {("melchior", "llm:qwen"): 4, ("balthazar", "llm:qwen"): 2}
    )
    assert (up, deleted) == (2, 0)
    slots = {s.resource: s for s in store.resource_slots_for_host("melchior")}
    assert slots["llm:qwen"].capacity == 4
    assert slots["llm:qwen"].free == 4
    assert slots["llm:qwen"].kind == "hard"


def test_reconcile_reaps_undeclared_llm_rows(store) -> None:
    store.reconcile_llm_served_slots({("h", "llm:a"): 1, ("h", "llm:b"): 1})
    # 'b' stops being served; 'a' stays
    up, deleted = store.reconcile_llm_served_slots({("h", "llm:a"): 1})
    assert (up, deleted) == (1, 1)
    resources = {s.resource for s in store.resource_slots_for_host("h")}
    assert resources == {"llm:a"}


def test_reconcile_never_touches_hardware_rows(store) -> None:
    """The llm: namespace scope must leave gpu/podman/tts/mem rows alone."""
    store.sync_host_resource_slots("gpuhost", {"gpu": 1, "podman": 2})
    store.reconcile_llm_served_slots({("gpuhost", "llm:m"): 3})
    # a later reconcile that drops the llm row must NOT reap the hardware rows
    store.reconcile_llm_served_slots({})
    resources = {s.resource for s in store.resource_slots_for_host("gpuhost")}
    assert resources == {"gpu", "podman"}  # llm:m reaped, hardware intact


def test_reconcile_is_reservation_safe(store) -> None:
    """A capacity change preserves an outstanding slice-6c reservation."""
    from precis.store._resource_slots_ops import reserve_resource_slots

    store.reconcile_llm_served_slots({("h", "llm:m"): 4})
    with store.pool.connection() as conn:
        with conn.transaction():
            assert reserve_resource_slots(conn, "h", {"llm:m": 1}) is True
    # free is now 3; re-declaring the SAME capacity must not stomp it
    store.reconcile_llm_served_slots({("h", "llm:m"): 4})
    slot = {s.resource: s for s in store.resource_slots_for_host("h")}["llm:m"]
    assert slot.capacity == 4 and slot.free == 3


# ── validation ────────────────────────────────────────────────────────────


def test_upsert_card_accepts_served_by_offering(store) -> None:
    from precis import llm_catalog

    rid, _ = llm_catalog.upsert_card(
        store,
        model_id="served-model",
        text="Local tier, served on two hosts.",
        offerings=[
            {
                "transport": "openai_compat",
                "served_by": [
                    {"host": "melchior", "endpoint": ":8080/v1", "max_parallel": 4}
                ],
            }
        ],
    )
    ref = store.get_ref(kind="llm", id=rid)
    assert ref.meta["offerings"][0]["served_by"][0]["host"] == "melchior"


def test_upsert_card_rejects_bad_served_by(store) -> None:
    from precis import llm_catalog
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        llm_catalog.upsert_card(
            store,
            model_id="bad1",
            text="x",
            offerings=[{"served_by": [{"max_parallel": 2}]}],  # no host
        )
    with pytest.raises(BadInput):
        llm_catalog.upsert_card(
            store,
            model_id="bad2",
            text="x",
            offerings=[{"served_by": [{"host": "h", "max_parallel": 0}]}],  # <1
        )
    with pytest.raises(BadInput):
        llm_catalog.upsert_card(
            store,
            model_id="bad3",
            text="x",
            offerings=[{"served_by": [{"host": "h", "bogus": 1}]}],  # unknown key
        )


# ── pass integration ──────────────────────────────────────────────────────


def test_reconcile_pass_seeds_slots_from_catalog(store) -> None:
    from precis import llm_catalog
    from precis.workers.llm_reconcile import run_llm_reconcile_pass

    llm_catalog.upsert_card(
        store,
        model_id="qwen-served",
        text="Local big tier served on the gateway.",
        offerings=[
            {
                "transport": "openai_compat",
                "served_by": [{"host": "melchior", "max_parallel": 3}],
            }
        ],
    )
    # force past the throttle; no network (_fetch=False) so only seeding runs.
    res = run_llm_reconcile_pass(store, force=True, _fetch=False)
    assert res.ok >= 1  # at least the one seeded slot counted as work
    slots = {s.resource: s for s in store.resource_slots_for_host("melchior")}
    assert "llm:qwen-served" in slots
    assert slots["llm:qwen-served"].capacity == 3
