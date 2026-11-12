"""Shadow lane decisions (td572552 S4b): the decide() shadow beside the
lane picks — lane-table offerings, the capability/pool gates and the
agree verdict — and the run_slice/plan_dispatch stamp sites' helpers.
"""

from __future__ import annotations

import dataclasses

import pytest

from precis.plans import lanes as lanes_mod
from precis.utils.llm import decide
from precis.workers.job_types import _lane_decide as ld
from tests._lane_rows import SEED_LANES, seed_lane, use_lanes


@pytest.fixture(autouse=True)
def _shadow_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shadow mode on, SEED table stood: every test starts where the
    week-long shadow window does — decide() beside pick_lane."""
    monkeypatch.setattr(decide, "mode_for", lambda kind: "shadow")
    use_lanes(monkeypatch)


def _stamp(**kw):
    """shadow_stamp against a store-shaped stub: the account seam is
    patched and every other read is injected or fails closed to {}."""
    return ld.shadow_stamp(object(), table=SEED_LANES, **kw)


def test_offerings_map_the_lane_table() -> None:
    ofs = ld.offerings_for(SEED_LANES, {})
    assert [(o.lane, o.model) for o in ofs] == [
        ("devin", "swe-2-medium"),
        ("devin", "swe-2-high"),
        ("devin", "swe-2-max"),
        ("codex", ""),
        ("claude-sub", "claude-haiku-5-5"),
        ("claude-sub", "claude-sonnet-5-5"),
        ("claude-sub", "claude-opus-5-5"),
        ("kimi", ""),
    ]
    devin = next(o for o in ofs if o.lane == "devin" and o.model == "swe-2-high")
    assert devin.billing == "free" and devin.provider == "cognition"
    assert devin.capability["task:code"] == 1 and devin.capability["mcp"] == 0
    sub = next(
        o for o in ofs if o.lane == "claude-sub" and o.model == "claude-sonnet-5-5"
    )
    assert sub.capability["task:*"] == 1 and sub.capability["mcp"] == 1
    assert next(o for o in ofs if o.lane == "kimi").enabled is False


def test_stamp_agrees_with_the_free_first_pick() -> None:
    """pick_lane takes the free lane for code; decide() ranks it first
    too — the steady-state agreement the shadow window measures."""
    stamp = _stamp(
        task_class="code",
        site="submit",
        picked=("devin", "swe-2-high"),
        counts={},
        backoffs={},
    )
    assert stamp is not None
    assert stamp["site"] == "submit" and stamp["executed"] == "legacy"
    assert stamp["task_class"] == "code"
    assert stamp["chosen"]["lane"] == "devin"
    assert stamp["chosen"]["model"] == "swe-2-high"
    assert stamp["legacy"]["chosen"]["lane"] == "devin"
    assert stamp["agree"] is True and stamp["lane_match"] is True


def test_a_lane_at_its_cap_rejects_and_the_pick_agrees() -> None:
    """A full lane paces ``exhausted`` on its cap pool — the same
    availability rejection an account pool produces — so the shadow
    takes the subscription lane exactly like pick_lane does."""
    stamp = _stamp(
        task_class="code",
        site="submit",
        picked=("claude-sub", "claude-sonnet-5-5"),
        counts={"devin": 3},
        backoffs={},
    )
    assert stamp is not None
    assert stamp["chosen"]["lane"] == "claude-sub"
    assert stamp["agree"] is True
    reasons = {r["offering_key"]: r["reason"] for r in stamp["rejected"]}
    assert reasons["swe-2-high@devin_tree/devin"] == "pool exhausted"


def test_disabled_and_parked_lanes_reject() -> None:
    parked = dataclasses.replace(
        seed_lane("claude-sub"), parked_reason="outage:timeout"
    )
    stamp = ld.shadow_stamp(
        object(),
        task_class="code",
        table=(seed_lane("devin"), seed_lane("codex"), parked, seed_lane("kimi")),
        site="submit",
        picked=("devin", "swe-2-high"),
        counts={},
        backoffs={},
    )
    assert stamp is not None
    reasons = {r["offering_key"]: r["reason"] for r in stamp["rejected"]}
    assert reasons["claude-sonnet-5-5@claude_p/claude-sub"] == "pool exhausted"
    assert reasons["@kimi_tree/kimi"] == "offering or account disabled"


def test_vendor_exclusion_rejects_the_builder_vendor() -> None:
    """The review rule: the shadow sees every lane but the builder's
    vendor is rejected by the request's own constraint."""
    stamp = _stamp(
        task_class="review",
        site="review",
        picked=("codex", None),
        counts={},
        backoffs={},
        exclude_vendor=("cognition",),
    )
    assert stamp is not None
    assert stamp["chosen"]["lane"] == "codex"
    assert stamp["agree"] is True
    reasons = {r["offering_key"]: r["reason"] for r in stamp["rejected"]}
    assert (
        reasons["swe-2-high@devin_tree/devin"] == "vendor cognition excluded by request"
    )


def test_needs_mcp_rejects_the_mcp_less_lanes() -> None:
    """A generator (or a ``lane_capability: mcp`` mint) may only run
    where a write MCP exists — today the claude_p lane alone."""
    stamp = _stamp(
        task_class="code",
        site="submit",
        picked=("claude-sub", "claude-sonnet-5-5"),
        counts={},
        backoffs={},
        needs_mcp=True,
    )
    assert stamp is not None
    assert stamp["chosen"]["lane"] == "claude-sub"
    assert stamp["agree"] is True
    reasons = {r["offering_key"]: r["reason"] for r in stamp["rejected"]}
    assert reasons["swe-2-high@devin_tree/devin"] == "lane offers no write MCP"


def test_model_pin_prefers_the_accepting_lane() -> None:
    """A ``params.model`` pin is honored by decide()'s pin path — the
    free ranking still lands beside it as evidence."""
    stamp = _stamp(
        task_class="code",
        site="submit",
        picked=("claude-sub", "claude-opus-5-5"),
        counts={},
        backoffs={},
        model_pin="claude-opus-5-5",
    )
    assert stamp is not None
    assert stamp["pinned"] is True and stamp["reason"] == "pinned"
    assert stamp["chosen"]["lane"] == "claude-sub"
    assert stamp["chosen"]["model"] == "claude-opus-5-5"
    assert stamp["agree"] is True
    # The free ranking ran anyway: devin still ranks first in it.
    assert stamp["ranked"][0]["lane"] == "devin"


def test_escalate_stamp_compares_against_the_ladder() -> None:
    """ladder() is the legacy chain, compared against decide()'s ranked
    list the same way route() compares its rungs."""
    rungs = lanes_mod.ladder("code", lanes=SEED_LANES)
    assert [(r.lane, r.model) for r in rungs] == [
        ("devin", "swe-2-high"),
        ("devin", "swe-2-max"),
        ("claude-sub", "claude-sonnet-5-5"),
        ("claude-sub", "claude-opus-5-5"),
    ]
    stamp = _stamp(
        task_class="code",
        site="escalate",
        picked=("devin", "swe-2-max"),
        rungs=rungs,
        chosen_rung=1,
        counts={},
        backoffs={},
    )
    assert stamp is not None
    assert stamp["legacy"]["keys"] == [
        "swe-2-high@devin_tree/devin",
        "swe-2-max@devin_tree/devin",
        "claude-sonnet-5-5@claude_p/claude-sub",
        "claude-opus-5-5@claude_p/claude-sub",
    ]
    assert stamp["legacy"]["chosen"]["model"] == "swe-2-max"
    assert stamp["agree"] is True


def test_a_different_shadow_pick_reads_disagree() -> None:
    """The shadow preferring a lane the legacy pick did not take is the
    signal the stamp exists to record."""
    stamp = _stamp(
        task_class="code",
        site="submit",
        picked=("claude-sub", "claude-sonnet-5-5"),
        counts={},
        backoffs={},
    )
    assert stamp is not None
    assert stamp["chosen"]["lane"] == "devin"
    assert stamp["agree"] is False and stamp["lane_match"] is False


def test_legacy_mode_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decide, "mode_for", lambda kind: "legacy")
    assert (
        _stamp(
            task_class="code",
            site="submit",
            picked=("devin", "swe-2-high"),
            counts={},
            backoffs={},
        )
        is None
    )


def test_shadow_failure_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken shadow is a miss, never a dispatch failure — the same
    fail-open the router's _decision_probe keeps."""
    monkeypatch.setattr(decide, "decide", lambda *a, **k: 1 / 0)
    assert (
        _stamp(
            task_class="code",
            site="submit",
            picked=("devin", "swe-2-high"),
            counts={},
            backoffs={},
        )
        is None
    )
