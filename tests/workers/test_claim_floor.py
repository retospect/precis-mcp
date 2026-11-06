"""Unit tests for the per-process claim frontier floor (gr573350).

No DB: the floor is plain module state behind an injectable clock —
``_frontier_clock`` is monkeypatched so the 300s TTL can be crossed
without sleeping.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.workers import _claim


@pytest.fixture(autouse=True)
def _floors() -> Any:
    _claim.invalidate_claim_frontiers()
    yield
    _claim.invalidate_claim_frontiers()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A controllable ``_frontier_clock``: advance with ``clock[0] += s``."""
    now = [1000.0]
    monkeypatch.setattr(_claim, "_frontier_clock", lambda: now[0])
    return now


def test_unset_floor_returns_none() -> None:
    assert _claim.claim_frontier("pass", "tier") is None


def test_floor_is_the_min_returned_key() -> None:
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=5)
    assert _claim.claim_frontier("pass", "tier") == 5


def test_floor_returned_again_within_ttl(clock: list[float]) -> None:
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=5)
    clock[0] += 299.0
    assert _claim.claim_frontier("pass", "tier") == 5


def test_floor_expires_past_ttl(clock: list[float]) -> None:
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=5)
    clock[0] += 301.0
    assert _claim.claim_frontier("pass", "tier") is None


def test_empty_with_floor_raises_to_high_water() -> None:
    """An empty claim never clears the floor — it rises to the table's
    current max key (only the TTL clears a floor)."""
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=5)
    _claim.note_claim_frontier("pass", "tier", floor=5, min_key=None, high_water=42)
    assert _claim.claim_frontier("pass", "tier") == 42


def test_empty_with_floor_never_lowers_it() -> None:
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=None, high_water=50)
    _claim.note_claim_frontier("pass", "tier", floor=50, min_key=None, high_water=42)
    assert _claim.claim_frontier("pass", "tier") == 50


def test_advancing_a_live_floor_keeps_its_first_set_timestamp(
    clock: list[float],
) -> None:
    """The TTL counts from when the floor was FIRST set — an advance
    does not re-stamp, so every floor dies one TTL after its creation
    and a row eligible below it waits at most one TTL (gr573350 review)."""
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=5)
    clock[0] += 200.0
    _claim.note_claim_frontier("pass", "tier", floor=5, min_key=9)
    assert _claim.claim_frontier("pass", "tier") == 9
    clock[0] += 150.0  # 350s since the first set — past the 300s TTL
    assert _claim.claim_frontier("pass", "tier") is None


def test_reparking_a_live_floor_keeps_its_first_set_timestamp(
    clock: list[float],
) -> None:
    """An empty claim re-parking the floor at a new high-water keeps the
    original timestamp too — a drained queue still gets its one full
    scan per TTL, so a re-pended row below the floor cannot starve."""
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=None, high_water=40)
    clock[0] += 200.0
    _claim.note_claim_frontier("pass", "tier", floor=40, min_key=None, high_water=45)
    assert _claim.claim_frontier("pass", "tier") == 45
    clock[0] += 150.0
    assert _claim.claim_frontier("pass", "tier") is None


def test_empty_full_scan_parks_at_high_water() -> None:
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=None, high_water=42)
    assert _claim.claim_frontier("pass", "tier") == 42


def test_non_empty_result_overrides_parked_floor() -> None:
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=None, high_water=42)
    _claim.note_claim_frontier("pass", "tier", floor=42, min_key=50)
    assert _claim.claim_frontier("pass", "tier") == 50


def test_empty_with_floor_and_no_high_water_is_a_noop() -> None:
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=5)
    _claim.note_claim_frontier("pass", "tier", floor=5, min_key=None)
    assert _claim.claim_frontier("pass", "tier") == 5


def test_empty_without_floor_or_high_water_is_a_noop() -> None:
    _claim.note_claim_frontier("pass", "tier", floor=None, min_key=None)
    assert _claim.claim_frontier("pass", "tier") is None


def test_keys_are_isolated_per_pass_and_tier() -> None:
    _claim.note_claim_frontier("pass-a", "walk", floor=None, min_key=5)
    _claim.note_claim_frontier("pass-a", "rest", floor=None, min_key=9)
    assert _claim.claim_frontier("pass-a", "walk") == 5
    assert _claim.claim_frontier("pass-a", "rest") == 9
    assert _claim.claim_frontier("pass-b", "walk") is None
    assert _claim.claim_frontier("pass-a") is None
