"""se's rental of the shared design core's **design history**
(docs/backlog/design-state-core.md item 5, :mod:`precis.design.history`).

Envelope revisions: a save that replaces a surviving block's envelope
mints one revision per block, a new block or a cleared envelope mints
none, validate/DRC say which revision they were scored under, and the
``rev-N`` snapshot written by the same save records it.

Checkpoints and branches: a named ``checkpoint``/``restore`` round-trips
the tree (uids included); ``pin`` copies the design into a new slug,
applies its ops to the copy only, carries the core rows (scenario,
revision log, states) along, and answers with the BranchResult
comparison.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.design import history
from precis.design import scenarios as design_scenarios
from precis.design import states as design_states
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import SeTree
from precis_se.validate import ValidationIssue

_CASTER = json.dumps(
    {
        "ops": [
            {"op": "add_block", "name": "fork", "envelope": "box:w0.04d0.02h0.08"},
            {
                "op": "add_block",
                "name": "hub",
                "parent": "fork",
                "envelope": "cyl:r0.008h0.03",
            },
        ]
    }
)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return int(ref.id)


# ── envelope revisions ───────────────────────────────────────────────────


def test_fresh_design_is_at_revision_zero(handler: SeHandler, store: Store) -> None:
    handler.put(id="caster1", text=_CASTER)
    assert history.current_revision(store, _ref_id(store, "caster1")) == 0
    body = handler.get(id="caster1", view="validate").body
    assert "envelope revision: 0" in body


def test_set_envelope_on_a_surviving_block_mints_a_revision(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="caster1", text=_CASTER)
    reply = handler.edit(
        id="caster1",
        ops=[{"op": "set_envelope", "block": "hub", "envelope": "cyl:r0.006h0.03"}],
    ).body
    ref_id = _ref_id(store, "caster1")
    assert history.current_revision(store, ref_id) == 1
    assert "envelope revision 1" in reply
    (row,) = history.revision_log(store, ref_id)
    assert "hub: cyl:r0.008h0.03 -> cyl:r0.006h0.03" in row["reason"]
    assert row["block_uid"] is not None
    assert row["set_by"] == "se.edit"
    # Present on the result the same save stored: the rev-N snapshot.
    (latest,) = [
        c for c in history.list_checkpoints(store, ref_id) if c.label == "rev-2"
    ]
    assert latest.envelope_revision == 1
    assert "envelope revision: 1 — last: hub:" in (
        handler.get(id="caster1", view="validate").body
    )
    assert "envelope revision: 1" in handler.get(id="caster1", view="drc").body


def test_unchanged_new_or_cleared_envelopes_mint_nothing(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="caster1", text=_CASTER)
    handler.edit(
        id="caster1",
        ops=[
            # same value — not a change
            {"op": "set_envelope", "block": "fork", "envelope": "box:w0.04d0.02h0.08"},
            # a block added in this save — nothing was scored against it
            {"op": "add_block", "name": "axle", "envelope": "cyl:r0.004h0.05"},
            # a cleared envelope loosens, it doesn't tighten
            {"op": "set_envelope", "block": "hub", "envelope": None},
        ],
    )
    assert history.current_revision(store, _ref_id(store, "caster1")) == 0


def test_two_tightenings_in_one_edit_mint_one_revision_each(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="caster1", text=_CASTER)
    reply = handler.edit(
        id="caster1",
        ops=[
            {"op": "set_envelope", "block": "fork", "envelope": "box:w0.03d0.02h0.08"},
            {"op": "set_envelope", "block": "hub", "envelope": "cyl:r0.006h0.03"},
        ],
    ).body
    ref_id = _ref_id(store, "caster1")
    assert [r["revision"] for r in history.revision_log(store, ref_id)] == [1, 2]
    assert "minted: 1, 2" in reply


def test_a_replacing_put_measures_tightening_against_the_stored_tree(
    handler: SeHandler, store: Store
) -> None:
    """A full ``put`` keeps a block's uid by label, so replacing the
    design with a tighter hub envelope is a tightening of THAT block."""
    handler.put(id="caster1", text=_CASTER)
    tighter = json.loads(_CASTER)
    tighter["ops"][1]["envelope"] = "cyl:r0.007h0.03"
    reply = handler.put(id="caster1", text=json.dumps(tighter)).body
    ref_id = _ref_id(store, "caster1")
    (row,) = history.revision_log(store, ref_id)
    assert row["set_by"] == "se.put"
    assert "hub:" in row["reason"]
    assert "envelope revision 1" in reply


def test_restore_of_a_tighter_revision_counts_as_a_tightening(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="caster1", text=_CASTER)  # rev 1: hub r0.008
    handler.edit(
        id="caster1",
        ops=[{"op": "set_envelope", "block": "hub", "envelope": None}],
    )  # rev 2: hub cleared — loosening, no mint
    ref_id = _ref_id(store, "caster1")
    assert history.current_revision(store, ref_id) == 0
    # Unbounded -> bounded is a tightening: the restore mints.
    reply = handler.edit(id="caster1", ops=[{"op": "restore_revision", "rev": 1}])
    assert history.current_revision(store, ref_id) == 1
    assert "envelope revision 1" in reply.body
    handler.edit(
        id="caster1",
        ops=[{"op": "set_envelope", "block": "hub", "envelope": "cyl:r0.009h0.03"}],
    )
    handler.edit(id="caster1", ops=[{"op": "restore_revision", "rev": 1}])
    assert history.current_revision(store, ref_id) == 3


# ── named checkpoint / restore ───────────────────────────────────────────


def test_checkpoint_then_restore_round_trips(handler: SeHandler, store: Store) -> None:
    handler.put(id="caster1", text=_CASTER)
    ref_id = _ref_id(store, "caster1")
    before = persist.tree_to_json(persist.load_tree(store, ref_id))
    reply = handler.edit(
        id="caster1",
        ops=[{"op": "checkpoint", "label": "two-blocks", "reason": "baseline"}],
    ).body
    assert "checkpoint 'two-blocks' saved" in reply
    assert "blocks 2" in reply
    # A checkpoint changes nothing in the tree, so it records no revision.
    assert len(history.list_revisions(store, ref_id)) == 1
    handler.edit(
        id="caster1",
        ops=[
            {"op": "remove_block", "block": "hub"},
            {"op": "add_block", "name": "axle", "envelope": "cyl:r0.004h0.05"},
        ],
    )
    restored = handler.edit(
        id="caster1", ops=[{"op": "restore", "label": "two-blocks"}]
    ).body
    assert "restored to checkpoint 'two-blocks' (recorded as rev 3)" in restored
    after = persist.tree_to_json(persist.load_tree(store, ref_id))
    assert after == before  # uids included
    revisions = handler.get(id="caster1", view="revisions").body
    assert "## named checkpoints (1)" in revisions
    assert "'two-blocks'" in revisions and "baseline" in revisions


def test_restore_unknown_label_names_the_checkpoints(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    handler.edit(id="caster1", ops=[{"op": "checkpoint", "label": "a"}])
    with pytest.raises(NotFound, match="checkpoints: a"):
        handler.edit(id="caster1", ops=[{"op": "restore", "label": "b"}])


def test_checkpoint_label_cannot_take_the_revision_namespace(
    handler: SeHandler,
) -> None:
    handler.put(id="caster1", text=_CASTER)
    with pytest.raises(BadInput, match="reserved"):
        handler.edit(id="caster1", ops=[{"op": "checkpoint", "label": "rev-1"}])


def test_history_ops_ride_alone(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    with pytest.raises(BadInput, match="must be the only op"):
        handler.edit(
            id="caster1",
            ops=[
                {"op": "checkpoint", "label": "a"},
                {"op": "add_block", "name": "axle"},
            ],
        )


# ── pin -> branch ────────────────────────────────────────────────────────


def _pin_axle(handler: SeHandler) -> str:
    return handler.edit(
        id="caster1",
        ops=[
            {
                "op": "pin",
                "as": "caster1-axle",
                "reason": "try a separate axle",
                "ops": [{"op": "add_block", "name": "axle", "parent": "fork"}],
            }
        ],
    ).body


def test_pin_returns_a_branch_result_and_keeps_both_designs_live(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="caster1", text=_CASTER)
    parent_id = _ref_id(store, "caster1")
    parent_uids = {
        n: b.uid for n, b in persist.load_tree(store, parent_id).blocks.items()
    }
    reply = _pin_axle(handler)
    assert "pinned → branch 'caster1-axle'" in reply
    assert "reason: try a separate axle" in reply
    assert "| blocks | 2 | 3 | 1 (+50.0%) |" in reply
    assert "feasible: the pin introduced no new validate error" in reply

    # The parent is untouched; the branch is a full design of its own.
    assert set(persist.load_tree(store, parent_id).blocks) == {"fork", "hub"}
    branch_id = _ref_id(store, "caster1-axle")
    branch_tree = persist.load_tree(store, branch_id)
    assert set(branch_tree.blocks) == {"fork", "hub", "axle"}
    # Copied blocks keep their uids (block-by-block diffable); the new one mints.
    for name, uid in parent_uids.items():
        assert branch_tree.blocks[name].uid == uid
    assert branch_tree.blocks["axle"].uid not in parent_uids.values()
    assert "fork" in handler.get(id="caster1-axle", view="tree").body

    rec = history.get_branch(store, branch_id)
    assert rec is not None and rec.parent_ref_id == parent_id
    assert rec.headline["blocks"] == 3
    (child,) = history.list_branches(store, parent_ref_id=parent_id)
    assert child.ref_id == branch_id
    assert "'caster1-axle'" in handler.get(id="caster1", view="branches").body
    assert "branch of 'caster1'" in (
        handler.get(id="caster1-axle", view="branches").body
    )
    # The branch's own history starts at the pin.
    assert len(history.list_revisions(store, branch_id)) == 1


def test_pin_carries_scenario_revisions_and_states_to_the_branch(
    handler: SeHandler, store: Store
) -> None:
    text = json.loads(_CASTER)
    text["scenario"] = "prototype"
    text["ops"] += [
        {
            "op": "declare_states",
            "block": "hub",
            "states": [{"name": "open"}, {"name": "latched"}],
        },
        {
            "op": "declare_transitions",
            "block": "hub",
            "transitions": [
                {
                    "from_state": "open",
                    "to_state": "latched",
                    "driver_kind": "mechanical",
                }
            ],
        },
    ]
    handler.put(id="latch1", text=json.dumps(text))
    handler.edit(
        id="latch1",
        ops=[
            {"op": "set_current_state", "block": "hub", "state": "latched"},
            {"op": "set_envelope", "block": "hub", "envelope": "cyl:r0.007h0.03"},
        ],
    )
    handler.edit(
        id="latch1",
        ops=[
            {
                "op": "pin",
                "as": "latch1-b",
                "reason": "tighter fork",
                "ops": [
                    {
                        "op": "set_envelope",
                        "block": "fork",
                        "envelope": "box:w0.03d0.02h0.08",
                    }
                ],
            }
        ],
    )
    parent_id = _ref_id(store, "latch1")
    branch_id = _ref_id(store, "latch1-b")
    scenario = design_scenarios.design_scenario(store, branch_id)
    assert scenario is not None and scenario.scenario_id == "prototype"
    # Inherited revision 1 (the parent's hub), then the branch's own fork
    # tightening -> 2; the parent stays at 1.
    assert history.current_revision(store, branch_id) == 2
    assert history.current_revision(store, parent_id) == 1
    hub_uid = persist.load_tree(store, branch_id).blocks["hub"].uid
    assert hub_uid is not None
    assert design_states.current_state(store, branch_id, hub_uid) == "latched"
    (t,) = design_states.transitions_for(store, branch_id, hub_uid)
    assert t.driver_kind == "mechanical"
    # Independent per design: re-posing the branch leaves the parent alone.
    handler.edit(
        id="latch1-b",
        ops=[{"op": "set_current_state", "block": "hub", "state": "open"}],
    )
    assert design_states.current_state(store, parent_id, hub_uid) == "latched"


def test_pin_reports_what_broke(
    handler: SeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin whose branch gains a validate ERROR its parent lacked answers
    ``infeasible`` with that rule; an error both sides share is not one the
    pin broke. The finding is injected — se's write-time op guards keep a
    real error from being authored through ops."""
    handler.put(id="caster1", text=_CASTER)
    real = SeHandler._validate_findings

    def findings(
        self: SeHandler, tree: SeTree, ref_id: int, load_cases: Any = None
    ) -> list[Any]:
        out = list(real(self, tree, ref_id, load_cases))
        out.append(ValidationIssue(rule="shared", subject="fork", detail="old"))
        if "axle" in tree.blocks:
            out.append(
                ValidationIssue(rule="axle_clash", subject="axle", detail="hits hub")
            )
        return out

    monkeypatch.setattr(SeHandler, "_validate_findings", findings)
    reply = _pin_axle(handler)
    assert "infeasible: axle: hits hub (constraint: axle_clash)" in reply
    assert "| validate_errors | 1 | 2 | 1 (+100.0%) |" in reply


def test_diff_joins_parent_and_branch_on_uid(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    handler.edit(
        id="caster1",
        ops=[
            {
                "op": "pin",
                "as": "caster1-b",
                "reason": "slimmer hub, plus an axle",
                "ops": [
                    {
                        "op": "set_envelope",
                        "block": "hub",
                        "envelope": "cyl:r0.006h0.03",
                    },
                    {"op": "add_block", "name": "axle", "parent": "fork"},
                ],
            }
        ],
    )
    body = handler.get(id="caster1-b", view="diff", args={"against": "caster1"}).body
    assert "'axle' added" in body
    assert "'hub': envelope: 'cyl:r0.008h0.03' -> 'cyl:r0.006h0.03'" in body
    assert "'fork'" not in body  # unchanged blocks are not listed
    assert "2 block(s) differ; 2 uid(s) shared" in body
    with pytest.raises(BadInput, match="against"):
        handler.get(id="caster1-b", view="diff")


def test_pin_refuses_an_existing_slug_and_needs_a_reason(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    handler.put(id="other", text=_CASTER)
    with pytest.raises(BadInput, match="never overwrites"):
        handler.edit(id="caster1", ops=[{"op": "pin", "as": "other", "reason": "x"}])
    with pytest.raises(BadInput, match="reason"):
        handler.edit(id="caster1", ops=[{"op": "pin", "as": "caster1-b"}])
