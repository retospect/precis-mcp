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


# ── skill breadcrumb (_maybe_add_skill_hint) ─────────────────────────────


class _StubHub:
    def __init__(self, kinds: list[str]) -> None:
        self.kinds = kinds


class _StubRuntime:
    """Bare carrier for HintsMixin's unbound method — only ``hub`` is read."""

    def __init__(self, kinds: list[str]) -> None:
        self.hub = _StubHub(kinds)


def _breadcrumb(kind: str | None, verb: str = "link") -> str | list[str] | None:
    from typing import cast

    from precis.errors import PrecisError
    from precis.runtime.hints import HintsMixin

    err = PrecisError("boom")
    args = {"kind": kind} if kind is not None else {}
    # A bare duck-typed carrier (only .hub is read) stands in for the full
    # runtime — cast for the unbound-method call.
    stub = cast(HintsMixin, _StubRuntime([kind] if kind else []))
    HintsMixin._maybe_add_skill_hint(stub, err, verb, args)
    return err.next


def test_skill_breadcrumb_points_at_existing_kind_skill() -> None:
    """A kind whose precis-<kind>-help ships still gets the kind hint."""
    assert _breadcrumb("draft") == "get(kind='skill', id='precis-draft-help')"


def test_skill_breadcrumb_skips_nonexistent_kind_skill() -> None:
    """gr332020 item 2: a live kind with no shipped help skill must fall
    back to the per-verb hint, not fabricate a dead-end slug."""
    hint = _breadcrumb("no-such-kind-xyz")
    assert hint == "get(kind='skill', id='precis-link-help')"


def test_skill_breadcrumb_overview_fallback_for_odd_verb() -> None:
    hint = _breadcrumb("no-such-kind-xyz", verb="frobnicate")
    assert hint == "get(kind='skill', id='precis-overview')"
