"""``precis.nanopub.demote`` — walking a claim back down the freeze ladder.

The policy half (:func:`plan_demotion`) is pure and tested as a table; the
write half is DB-backed via the ``store`` fixture (real hub, real publish
row), no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.nanopub.demote import (
    ACTION_NONE,
    ACTION_REOPEN,
    ACTION_SUPERSEDE_REQUIRED,
    ALERT_SOURCE,
    DemotionRequest,
    demote_hub,
    plan_demotion,
    run_demotions,
)
from precis.nanopub.state import STATES, frozen_rung
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub

# ── policy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "action"),
    [
        (None, ACTION_NONE),
        ("candidate", ACTION_NONE),
        ("reviewed", ACTION_REOPEN),
        ("signed", ACTION_REOPEN),
        ("anchored", ACTION_SUPERSEDE_REQUIRED),
        ("published", ACTION_SUPERSEDE_REQUIRED),
        ("superseded", ACTION_NONE),
        ("retracted", ACTION_NONE),
        ("rejected", ACTION_NONE),
    ],
)
def test_plan_demotion_ladder(state: str | None, action: str) -> None:
    """The freeze line decides: unfrozen is a no-op, the reversible frozen
    rungs reopen, and anything past the anchor needs a human."""
    assert plan_demotion(state) == action


def test_every_state_has_a_planned_action() -> None:
    """No publish state falls through the policy unhandled — a new state in
    ``state.STATES`` must be given a demotion answer, not silently treated
    as unfrozen."""
    for state in STATES:
        assert plan_demotion(state) in (
            ACTION_NONE,
            ACTION_REOPEN,
            ACTION_SUPERSEDE_REQUIRED,
        )


def test_anchored_is_frozen_by_bytes_but_never_reopened() -> None:
    """The one place rung and reversibility disagree: ``anchored`` sits on
    the same ``bytes`` rung as ``signed``, but signing is local and
    anchoring is a calendar attestation that cannot be taken back."""
    assert frozen_rung("anchored") == frozen_rung("signed")
    assert plan_demotion("signed") == ACTION_REOPEN
    assert plan_demotion("anchored") == ACTION_SUPERSEDE_REQUIRED


# ── the write ────────────────────────────────────────────────────────


def _hub_at(store: Any, state: str | None) -> int:
    """A minted hub whose publish row sits in ``state`` (``None`` = no row)."""
    hub = mint_hub(store, CanonicalClaim(sentence=f"a claim at {state}", scope={}))
    if state is None:
        return hub
    row = store.nanopub_create_publish_row(hub)
    if state != "candidate":
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE nanopub_publish SET state = %s, approved_title = %s, "
                "claim_sha = %s WHERE id = %s",
                (state, "a frozen title", "deadbeef", row.id),
            )
            conn.commit()
    return hub


def test_demote_unminted_hub_is_a_no_op(store: Any) -> None:
    hub = _hub_at(store, None)

    result = demote_hub(store, hub, reason="test")

    assert result.action == ACTION_NONE
    assert result.applied is False
    assert store.nanopub_publish_row(hub) is None


def test_demote_candidate_leaves_the_row_alone(store: Any) -> None:
    """Nothing is frozen at ``candidate`` and the publish gates already
    block on a live contradicts edge — there is no posture to lower."""
    hub = _hub_at(store, "candidate")

    result = demote_hub(store, hub, reason="test")

    assert result.action == ACTION_NONE
    row = store.nanopub_publish_row(hub)
    assert row is not None and row.state == "candidate"


@pytest.mark.parametrize("state", ["reviewed", "signed"])
def test_demote_reopens_a_pre_anchor_row(store: Any, state: str) -> None:
    """Below the anchor the claim reopens to ``candidate`` and the frozen
    fields are discarded — it must earn approval again against the new
    evidence."""
    hub = _hub_at(store, state)

    result = demote_hub(store, hub, reason="a contradicts edge landed")

    assert result.action == ACTION_REOPEN
    assert result.from_state == state
    assert result.applied is True
    row = store.nanopub_publish_row(hub)
    assert row is not None
    assert row.state == "candidate"
    assert row.approved_title is None
    assert row.claim_sha is None


@pytest.mark.parametrize("state", ["anchored", "published"])
def test_demote_above_the_anchor_alerts_and_changes_nothing(
    store: Any, state: str
) -> None:
    """The bytes are out: the row is untouched and a human is told to
    adjudicate a supersede/retract instead."""
    hub = _hub_at(store, state)

    result = demote_hub(store, hub, reason="a contradicts edge landed")

    assert result.action == ACTION_SUPERSEDE_REQUIRED
    assert result.applied is False
    row = store.nanopub_publish_row(hub)
    assert row is not None and row.state == state

    from precis.alerts import list_open_alerts

    alerts = [a for a in list_open_alerts(store) if a.get("source") == ALERT_SOURCE]
    assert len(alerts) == 1
    assert str(hub) in alerts[0]["title"]


def test_run_demotions_dedups_and_survives_a_bad_id(store: Any) -> None:
    """Several contradicting edges in one pass demote the hub once, and a
    demotion that blows up never fails the pass that produced the
    evidence."""
    hub = _hub_at(store, "reviewed")

    results = run_demotions(
        store,
        [
            DemotionRequest(hub_ref_id=hub, reason="edge 1"),
            DemotionRequest(hub_ref_id=hub, reason="edge 2"),
            DemotionRequest(hub_ref_id=-1, reason="a ref that does not exist"),
        ],
    )

    applied = [r for r in results if r.applied]
    assert len(applied) == 1
    assert applied[0].hub_ref_id == hub


def test_run_demotions_on_an_empty_queue(store: Any) -> None:
    assert run_demotions(store, []) == []
    assert run_demotions(store, None) == []
