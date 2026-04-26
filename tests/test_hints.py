"""HintBus collector behaviour."""

from __future__ import annotations

import pytest

from precis.hints import Hint, HintBus


@pytest.fixture
def bus() -> HintBus:
    return HintBus(ring_size=20, max_per_response=2)


def test_emit_outside_scope_is_noop(bus: HintBus) -> None:
    bus.emit(Hint("hi", topic="x"))
    # no exception, no leak; collect outside scope returns []
    assert bus.collect() == []


def test_collect_inside_scope(bus: HintBus) -> None:
    with bus.request():
        bus.emit(Hint("a", topic="t1"))
        bus.emit(Hint("b", topic="t2"))
        out = bus.collect()
    assert [h.text for h in out] == ["a", "b"]


def test_dedup_by_topic_within_cooldown(bus: HintBus) -> None:
    with bus.request():
        bus.emit(Hint("a", topic="t", cooldown=5))
        bus.collect()
    with bus.request():
        bus.emit(Hint("a-again", topic="t", cooldown=5))
        out = bus.collect()
    assert out == [], "same topic within cooldown should be suppressed"


def test_max_per_response_caps(bus: HintBus) -> None:
    with bus.request():
        for i in range(10):
            bus.emit(Hint(f"h{i}", topic=f"t{i}"))
        out = bus.collect()
    assert len(out) == 2  # max_per_response


def test_collect_is_idempotent_within_a_request() -> None:
    """Calling collect() twice in one scope shouldn't double-emit."""
    bus = HintBus(max_per_response=5)
    with bus.request():
        bus.emit(Hint("a", topic="t1"))
        first = bus.collect()
        second = bus.collect()
    assert len(first) == 1
    assert second == []


def test_topic_can_refire_after_cooldown_elapses() -> None:
    bus = HintBus(ring_size=20, max_per_response=5)
    with bus.request():
        bus.emit(Hint("a", topic="t", cooldown=2))
        bus.collect()
    # advance 3 quiet requests
    for _ in range(3):
        with bus.request():
            bus.collect()
    with bus.request():
        bus.emit(Hint("a-fresh", topic="t", cooldown=2))
        out = bus.collect()
    assert len(out) == 1, "topic should re-fire once cooldown elapsed"


def test_request_id_increments() -> None:
    bus = HintBus()
    with bus.request() as r1:
        pass
    with bus.request() as r2:
        pass
    assert r2 == r1 + 1
