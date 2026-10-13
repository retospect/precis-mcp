"""Work-class axis of the quota taper (gr347551, Reto 2026-10-12).

The taper decides per (work class, tier) from ``quota.TAPER_FLOORS``;
callers pick a class explicitly or inherit the ambient scope (MCP tool
call, web request), else background.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from precis.budget import breaker as breaker_mod
from precis.budget import outcomes as outcomes_mod
from precis.budget import quota as quota_mod
from precis.budget.work_class import WorkClass, resolve, work_class_scope
from precis.utils.llm.router import DispatchClient, LlmRequest, Tier, _route_features

BG, DEL, INT = WorkClass.BACKGROUND, WorkClass.DELIVERABLE, WorkClass.INTERACTIVE
NONE: frozenset[str] = frozenset()
BIG_UP = frozenset({"big", "frontier"})
MED_UP = frozenset({"medium", "big", "frontier"})


class _Row:
    def __init__(self, windows: object) -> None:
        self.data = {"windows": windows}


class _Store:
    """Quota snapshot only; ``tx()`` is absent so the outcome tally is dark."""

    def __init__(self, windows: object) -> None:
        self._windows = windows

    def read_claude_quota(self, scope: str = "unified") -> _Row:
        return _Row(self._windows)


def _taper(used: float | None = None, status: str = "allowed") -> quota_mod.Taper:
    bucket: dict[str, Any] = {"status": status}
    if used is not None:
        bucket["used_percentage"] = used
    return quota_mod.taper_state(cast("Any", _Store({"seven_day": bucket})))


# ── the table ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("used", "status", "expected"),
    [
        (50, "allowed", {BG: NONE, DEL: NONE, INT: NONE}),
        (98, "allowed", {BG: BIG_UP, DEL: NONE, INT: NONE}),
        (98.5, "allowed", {BG: BIG_UP, DEL: NONE, INT: NONE}),
        (99, "allowed", {BG: MED_UP, DEL: BIG_UP, INT: NONE}),
        (99.5, "allowed", {BG: MED_UP, DEL: MED_UP, INT: NONE}),
        # Warning band far above the floors: ignored once a percentage
        # is visible — the percentages alone decide.
        (50, "allowed_warning", {BG: NONE, DEL: NONE, INT: NONE}),
        (97, "warning", {BG: NONE, DEL: NONE, INT: NONE}),
        # Blind fallback: warning status with no percentage still stops
        # BIG so a blind taper stays conservative.
        (None, "allowed_warning", {BG: BIG_UP, DEL: BIG_UP, INT: NONE}),
        (None, "allowed", {BG: NONE, DEL: NONE, INT: NONE}),
    ],
)
def test_taper_table_per_class(
    used: float | None, status: str, expected: dict[WorkClass, frozenset[str]]
) -> None:
    taper = _taper(used, status)
    for cls, blocked in expected.items():
        assert taper.blocked(cls) == blocked, (used, status, cls)


def test_floor_table_shape() -> None:
    floors = quota_mod.TAPER_FLOORS
    assert set(floors) == set(WorkClass)
    assert floors[INT].stop_big_pct is None and floors[INT].stop_medium_pct is None
    assert not floors[INT].warning_stops_big
    bg, de = floors[BG], floors[DEL]
    assert bg.stop_big_pct is not None and de.stop_big_pct is not None
    # Deliverables taper later than background, never earlier.
    assert de.stop_big_pct > bg.stop_big_pct
    assert quota_mod.TAPER_STOP_BIG_PCT == bg.stop_big_pct == 98.0
    assert quota_mod.TAPER_STOP_MEDIUM_PCT == bg.stop_medium_pct == 99.0


def test_level_and_reason_are_the_background_reading() -> None:
    assert _taper(98).level == 1
    assert _taper(99).level == 2
    assert _taper(50).level == 0
    assert "seven_day" in _taper(None, "allowed_warning").reason
    assert _taper(None, "allowed_warning").level == 1
    # A warning alongside a visible percentage reads as no taper.
    assert _taper(50, "allowed_warning").level == 0


def _gate(tier: Tier, windows: object, **kw: Any) -> str | None:
    return breaker_mod.gate_tier(
        tier, transport="claude_agent", store=cast("Any", _Store(windows)), **kw
    )


def test_gate_interactive_never_tapered_but_parked() -> None:
    warn = {"seven_day": {"status": "allowed_warning", "used_percentage": 99}}
    for tier in (Tier.MEDIUM, Tier.BIG, Tier.FRONTIER):
        assert _gate(tier, warn, work_class="interactive") is None
    parked = {"seven_day": {"status": "rejected"}}
    assert _gate(Tier.FRONTIER, parked, work_class="interactive") is not None


def test_gate_deliverable_tapers_later_than_background() -> None:
    windows = {"seven_day": {"status": "allowed", "used_percentage": 98.5}}
    reason = _gate(Tier.FRONTIER, windows)
    assert reason is not None and "background" in reason and "under 2%" in reason
    assert _gate(Tier.FRONTIER, windows, work_class="deliverable") is None
    windows = {"seven_day": {"status": "allowed", "used_percentage": 99.2}}
    reason = _gate(Tier.FRONTIER, windows, work_class="deliverable")
    assert reason is not None and "deliverable" in reason
    assert _gate(Tier.MEDIUM, windows, work_class="deliverable") is None


def test_gate_ambient_scope_applies_when_request_names_none() -> None:
    windows = {"seven_day": {"status": "allowed_warning"}}
    assert _gate(Tier.BIG, windows) is not None
    with work_class_scope("interactive"):
        assert _gate(Tier.BIG, windows) is None
        # An explicit class still wins over the scope.
        assert _gate(Tier.BIG, windows, work_class="background") is not None


def test_gate_tallies_served_and_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[WorkClass, str]] = []
    monkeypatch.setattr(
        outcomes_mod, "record", lambda store, cls, outcome: seen.append((cls, outcome))
    )
    windows = {"seven_day": {"status": "allowed_warning"}}
    _gate(Tier.BIG, windows, tally=True)
    _gate(Tier.BIG, windows, work_class="interactive", tally=True)
    _gate(Tier.BIG, windows, local=True, tally=True)  # free local: not paid
    _gate(Tier.BIG, windows)  # a selection probe: not counted
    _gate(Tier.MEDIUM, windows, work_class="interactive", tally=True, degraded=True)
    assert seen == [
        (BG, "paused"),
        (INT, "served"),
        (INT, "served"),
        (INT, "degraded"),
    ]


# ── defaults ─────────────────────────────────────────────────────────


def test_resolve_defaults() -> None:
    assert resolve(None) is BG
    assert resolve("deliverable") is DEL
    assert resolve("nonsense") is BG
    with work_class_scope(INT):
        assert resolve(None) is INT
        assert resolve("background") is BG
    assert resolve(None) is BG


def test_request_and_client_default_to_unset() -> None:
    assert LlmRequest(tier=Tier.SMALL).work_class is None
    assert DispatchClient().work_class is None
    feats = _route_features(LlmRequest(tier=Tier.BIG))
    assert feats["work_class"] == "background"
    feats = _route_features(LlmRequest(tier=Tier.BIG, work_class="deliverable"))
    assert feats["work_class"] == "deliverable"


def test_asa_turns_are_interactive() -> None:
    from pathlib import Path

    src = Path("src/asa_bot/claude_invoke.py").read_text(encoding="utf-8")
    assert src.count("LlmRequest(") == src.count('work_class="interactive"') >= 1


def test_mcp_tool_wrapper_runs_interactive() -> None:
    import anyio

    from precis import server

    seen: list[WorkClass] = []

    def tool() -> str:
        seen.append(resolve(None))
        return "ok"

    async def main() -> Any:
        wrapped = server._offload_sync(tool, semaphore=anyio.Semaphore(1))
        return await wrapped()

    assert anyio.run(main) == "ok"
    assert seen == [INT]
    assert resolve(None) is BG  # the scope did not leak out of the call


def test_briefing_compose_is_deliverable(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.workers import briefing

    classes: list[str | None] = []

    def complete(self: DispatchClient, messages: Any, **kw: Any) -> Any:
        classes.append(self.work_class)
        return type("R", (), {"text": "brief", "model": "m"})()

    monkeypatch.setattr(DispatchClient, "complete", complete)
    briefing._compose(cast("Any", None), [{"role": "user", "content": "x"}])
    assert classes == [WorkClass.DELIVERABLE]
