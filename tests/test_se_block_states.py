"""Blocktree slice 2: `se`'s ``declare_states``/``declare_transitions``/
``set_current_state`` ops (:mod:`precis_se.ops`), delegating entirely to
the shared design-core store (:mod:`precis.design.states`,
docs/backlog/blocktree-library-build-plan.md §Slice 2).

Round 1 (declare + read), round 2 (posing) and round 3 (``view='sweep'``)
all live here now. Posing has two surfaces: ``get(..., args={'state':
{block: state_name}})`` is a TRANSIENT override for one read
(:func:`precis_se.handler._apply_state_arg` — never written back), and
``set_current_state`` is the PERSISTENT op. ``view='sweep'`` (round 3)
checks the cross product of every state-carrying block's declared states
at once, reusing :func:`precis_se.validate.envelope_overlaps` per
combination — this is the round that completes the slice's "Done when":
declare, pose in either, probe for clash in each, and swept across all
states. Per-state clash wiring into ``drc.py`` itself stays unshipped —
deliberately out of scope (handler.py's ``_STATE_VIEWS`` docstring).

Fixture shape lifted from ``test_se_block_uid.py`` — the shared test DB
template carries only core migrations, so the plugin's own are seeded here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import precis_se
from precis.design import states as design_states
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import SeTree

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


def _block_uid(store: Store, slug: str, block: str) -> int:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    uid = tree.blocks[block].uid
    assert uid is not None
    return int(uid)


# ── declare + read ─────────────────────────────────────────────────────────


def test_declare_states_trans_cis_round_trips(handler: SeHandler) -> None:
    handler.put(
        id="switch1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "dye",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "declare_states",
                        "block": "dye",
                        "states": [
                            {"name": "trans", "descr": "extended"},
                            {"name": "cis", "descr": "bent"},
                        ],
                    },
                ]
            }
        ),
    )
    body = handler.get(id="switch1", view="block", args={"name": "dye"}).body
    assert "## states" in body
    assert "trans" in body
    assert "cis" in body
    assert "extended" in body and "bent" in body


def test_declare_states_loaded_bonded_round_trips(handler: SeHandler) -> None:
    """The plan's other named example shape (docs/backlog/
    blocktree-library-build-plan.md §Slice 2 "Done when")."""
    handler.put(
        id="clip1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "latch",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "declare_states",
                        "block": "latch",
                        "states": [{"name": "loaded"}, {"name": "bonded"}],
                    },
                ]
            }
        ),
    )
    body = handler.get(id="clip1", view="block", args={"name": "latch"}).body
    assert "loaded" in body and "bonded" in body


def test_stateless_block_render_is_unchanged(handler: SeHandler) -> None:
    """A block that never declared a state gets NO states/transitions
    section at all — the invariant the whole round exists to preserve."""
    handler.put(
        id="plain1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "bracket",
                        "envelope": "box:w0.01d0.01h0.01",
                    }
                ]
            }
        ),
    )
    body = handler.get(id="plain1", view="block", args={"name": "bracket"}).body
    assert "## states" not in body
    assert "## transitions" not in body


def test_stateful_and_stateless_blocks_coexist(handler: SeHandler) -> None:
    """Declaring states on one block must not alter the render of a
    sibling block that declared none."""
    handler.put(
        id="mixed1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "switch",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "add_block",
                        "name": "frame",
                        "envelope": "box:w0.02d0.02h0.02",
                    },
                    {
                        "op": "declare_states",
                        "block": "switch",
                        "states": [{"name": "trans"}, {"name": "cis"}],
                    },
                ]
            }
        ),
    )
    frame_body = handler.get(id="mixed1", view="block", args={"name": "frame"}).body
    assert "## states" not in frame_body
    switch_body = handler.get(id="mixed1", view="block", args={"name": "switch"}).body
    assert "## states" in switch_body


# ── driver_kind is closed ────────────────────────────────────────────────


def test_declare_transitions_unknown_driver_kind_rejected(handler: SeHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.put(
            id="switch2",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "dye",
                            "envelope": "box:w0.01d0.01h0.01",
                        },
                        {
                            "op": "declare_states",
                            "block": "dye",
                            "states": [{"name": "trans"}, {"name": "cis"}],
                        },
                        {
                            "op": "declare_transitions",
                            "block": "dye",
                            "transitions": [
                                {
                                    "from_state": "trans",
                                    "to_state": "cis",
                                    "driver_kind": "gamma_ray",
                                }
                            ],
                        },
                    ]
                }
            ),
        )
    msg = str(exc.value)
    assert "gamma_ray" in msg
    for kind in ("light", "reaction", "redox", "ph", "thermal", "mechanical"):
        assert kind in msg


def test_declare_transitions_rejects_self_edge(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="itself"):
        handler.put(
            id="switch3",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "dye",
                            "envelope": "box:w0.01d0.01h0.01",
                        },
                        {
                            "op": "declare_states",
                            "block": "dye",
                            "states": [{"name": "trans"}, {"name": "cis"}],
                        },
                        {
                            "op": "declare_transitions",
                            "block": "dye",
                            "transitions": [
                                {
                                    "from_state": "trans",
                                    "to_state": "trans",
                                    "driver_kind": "light",
                                }
                            ],
                        },
                    ]
                }
            ),
        )


def test_declare_states_rejects_duplicate_name(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="twice"):
        handler.put(
            id="switch4",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "dye",
                            "envelope": "box:w0.01d0.01h0.01",
                        },
                        {
                            "op": "declare_states",
                            "block": "dye",
                            "states": [{"name": "trans"}, {"name": "trans"}],
                        },
                    ]
                }
            ),
        )


# ── transitions are directed edges ──────────────────────────────────────


def test_directed_transition_pair_survives_round_trip_with_different_params(
    handler: SeHandler, store: Store
) -> None:
    """Forward and reverse are two rows, each carrying its own params — a
    ratchet's barriers differ by direction, and neither may collapse into
    (or overwrite) the other."""
    handler.put(
        id="ratchet1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "pawl",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "declare_states",
                        "block": "pawl",
                        "states": [{"name": "loaded"}, {"name": "bonded"}],
                    },
                    {
                        "op": "declare_transitions",
                        "block": "pawl",
                        "transitions": [
                            {
                                "from_state": "loaded",
                                "to_state": "bonded",
                                "driver_kind": "reaction",
                                "driver_ref": "click-cu1",
                                "params": {"barrier_ev": 0.3},
                            },
                            {
                                "from_state": "bonded",
                                "to_state": "loaded",
                                "driver_kind": "reaction",
                                "driver_ref": "click-cu1",
                                "params": {"barrier_ev": 1.8},
                            },
                        ],
                    },
                ]
            }
        ),
    )
    uid = _block_uid(store, "ratchet1", "pawl")
    ref = store.get_ref(kind="se", id="ratchet1")
    assert ref is not None
    by_edge = {
        (t.from_state, t.to_state): t.params["barrier_ev"]
        for t in design_states.transitions_for(store, ref.id, uid)
    }
    assert by_edge == {("loaded", "bonded"): 0.3, ("bonded", "loaded"): 1.8}
    # Rendered, too — the human-facing half of the same invariant.
    body = handler.get(id="ratchet1", view="block", args={"name": "pawl"}).body
    assert "## transitions" in body
    assert "0.3" in body and "1.8" in body


def test_transitions_precede_dependent_states_in_the_same_call(
    handler: SeHandler, store: Store
) -> None:
    """declare_states and declare_transitions for the same block in ONE
    call: the transition's endpoints must resolve against the states
    declared in that same call, not fail as if they didn't exist yet."""
    handler.put(
        id="oneshot1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "dye",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "declare_states",
                        "block": "dye",
                        "states": [{"name": "trans"}, {"name": "cis"}],
                    },
                    {
                        "op": "declare_transitions",
                        "block": "dye",
                        "transitions": [
                            {
                                "from_state": "trans",
                                "to_state": "cis",
                                "driver_kind": "light",
                                "driver_ref": "365nm",
                            }
                        ],
                    },
                ]
            }
        ),
    )
    uid = _block_uid(store, "oneshot1", "dye")
    ref = store.get_ref(kind="se", id="oneshot1")
    assert ref is not None
    transitions = design_states.transitions_for(store, ref.id, uid)
    assert len(transitions) == 1
    assert transitions[0].driver_ref == "365nm"


# ── states/transitions persist across an unrelated edit ────────────────


def test_states_survive_an_edit_that_does_not_touch_them(
    handler: SeHandler, store: Store
) -> None:
    handler.put(
        id="persist1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "dye",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "declare_states",
                        "block": "dye",
                        "states": [{"name": "trans"}, {"name": "cis"}],
                    },
                ]
            }
        ),
    )
    handler.edit(
        id="persist1",
        ops=[
            {
                "op": "add_block",
                "name": "frame",
                "envelope": "box:w0.02d0.02h0.02",
            }
        ],
    )
    body = handler.get(id="persist1", view="block", args={"name": "dye"}).body
    assert "trans" in body and "cis" in body


def test_declare_states_by_uid_addresses_the_same_block(
    handler: SeHandler, store: Store
) -> None:
    """Authors write a block NAME; the resolved uid is what the shared
    tables are keyed by, exactly as ``declare_dof``/``set_mode`` resolve
    the same way."""
    handler.put(
        id="uidref1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "dye",
                        "envelope": "box:w0.01d0.01h0.01",
                    }
                ]
            }
        ),
    )
    uid = _block_uid(store, "uidref1", "dye")
    handler.edit(
        id="uidref1",
        ops=[
            {
                "op": "declare_states",
                "block": f"#{uid}",
                "states": [{"name": "on"}, {"name": "off"}],
            }
        ],
    )
    ref = store.get_ref(kind="se", id="uidref1")
    assert ref is not None
    names = {s.name for s in design_states.states_for(store, ref.id, uid)}
    assert names == {"on", "off"}


# ── round 2: get-time posing (args={'state': {...}}) ────────────────────


_SWITCH_OPS = [
    {"op": "add_block", "name": "switch", "envelope": "box:w0.002d0.002h0.002"},
    {
        "op": "declare_states",
        "block": "switch",
        "states": [
            {"name": "trans", "envelope": "box:w0.002d0.002h0.002"},
            {"name": "cis", "envelope": "box:w0.05d0.05h0.05"},
        ],
    },
]


def test_state_arg_poses_different_states_to_different_geometry(
    handler: SeHandler,
) -> None:
    """The plan's own words: ``{trans, cis}`` posed must yield genuinely
    different geometry — here, a different rendered envelope."""
    handler.put(id="posegeo1", text=json.dumps({"ops": _SWITCH_OPS}))
    default_body = handler.get(
        id="posegeo1", view="block", args={"name": "switch"}
    ).body
    trans_body = handler.get(
        id="posegeo1",
        view="block",
        args={"name": "switch", "state": {"switch": "trans"}},
    ).body
    cis_body = handler.get(
        id="posegeo1",
        view="block",
        args={"name": "switch", "state": {"switch": "cis"}},
    ).body
    assert "box:w0.002d0.002h0.002" in default_body
    assert "box:w0.002d0.002h0.002" in trans_body
    assert "box:w0.05d0.05h0.05" in cis_body
    assert cis_body != trans_body


def test_state_arg_poses_block_for_clearance_probe(handler: SeHandler) -> None:
    """ "posed in either, probed for clash in each" — the same
    ``view='clearance'`` probe that already exists, now run against a
    posed block. No per-state drc.py wiring; the caller poses, then asks."""
    handler.put(
        id="poseclear1",
        text=json.dumps(
            {
                "ops": [
                    *_SWITCH_OPS,
                    {
                        "op": "add_block",
                        "name": "wall",
                        "envelope": "box:w0.01d0.01h0.01",
                        "pose": [0.01, 0.0, 0.0],
                    },
                ]
            }
        ),
    )
    default_body = handler.get(
        id="poseclear1", view="clearance", args={"a": "switch", "b": "wall"}
    ).body
    assert "clear" in default_body
    cis_body = handler.get(
        id="poseclear1",
        view="clearance",
        args={"a": "switch", "b": "wall", "state": {"switch": "cis"}},
    ).body
    assert "interference" in cis_body


def test_state_arg_rejects_undeclared_state_name(handler: SeHandler) -> None:
    handler.put(id="posebad1", text=json.dumps({"ops": _SWITCH_OPS}))
    with pytest.raises(BadInput, match="no state 'bogus'") as exc:
        handler.get(
            id="posebad1",
            view="block",
            args={"name": "switch", "state": {"switch": "bogus"}},
        )
    msg = str(exc.value)
    assert "trans" in msg and "cis" in msg


def test_state_arg_rejects_unknown_block_name(handler: SeHandler) -> None:
    handler.put(id="posebad2", text=json.dumps({"ops": _SWITCH_OPS}))
    with pytest.raises(NotFound):
        handler.get(
            id="posebad2",
            view="block",
            args={"name": "switch", "state": {"nope": "trans"}},
        )


def test_state_arg_rejects_instance_targeting(handler: SeHandler) -> None:
    """Declared states live on the template; posing the instance by name
    is refused, not silently ignored."""
    handler.put(
        id="poseinst1",
        text=json.dumps(
            {
                "ops": [
                    *_SWITCH_OPS,
                    {"op": "instance_block", "template": "switch", "name": "switch2"},
                ]
            }
        ),
    )
    with pytest.raises(BadInput, match="is an instance"):
        handler.get(
            id="poseinst1",
            view="block",
            args={"name": "switch2", "state": {"switch2": "cis"}},
        )


def test_state_arg_rejected_on_a_view_that_does_not_support_it(
    handler: SeHandler,
) -> None:
    handler.put(id="poseview1", text=json.dumps({"ops": _SWITCH_OPS}))
    with pytest.raises(BadInput, match="state is not supported on view='drc'"):
        handler.get(id="poseview1", view="drc", args={"state": {"switch": "trans"}})


def test_stateless_blocks_posed_render_matches_unposed(handler: SeHandler) -> None:
    """Posing an unrelated stateful block must not change a sibling
    stateless block's render — the invariant round 1 pinned for the
    argless case, now checked for the posed case too."""
    handler.put(
        id="posemixed1",
        text=json.dumps(
            {
                "ops": [
                    *_SWITCH_OPS,
                    {
                        "op": "add_block",
                        "name": "frame",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                ]
            }
        ),
    )
    before = handler.get(id="posemixed1", view="block", args={"name": "frame"}).body
    after = handler.get(
        id="posemixed1",
        view="block",
        args={"name": "frame", "state": {"switch": "cis"}},
    ).body
    assert before == after


def test_port_pose_overrides_move_the_port_direction(handler: SeHandler) -> None:
    """The only pose-like field a port carries today: ``direction``. A
    posed state's ``port_pose_overrides`` must actually change what
    renders, not just round-trip as inert JSON."""
    handler.put(
        id="poseport1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "switch",
                        "envelope": "box:w0.002d0.002h0.002",
                    },
                    {
                        "op": "add_port",
                        "block": "switch",
                        "name": "p1",
                        "direction": [1, 0, 0],
                    },
                    {
                        "op": "declare_states",
                        "block": "switch",
                        "states": [
                            {"name": "trans"},
                            {
                                "name": "cis",
                                "port_pose_overrides": {"p1": {"direction": [0, 1, 0]}},
                            },
                        ],
                    },
                ]
            }
        ),
    )
    default_body = handler.get(
        id="poseport1", view="block", args={"name": "switch"}
    ).body
    cis_body = handler.get(
        id="poseport1",
        view="block",
        args={"name": "switch", "state": {"switch": "cis"}},
    ).body
    assert "[1, 0, 0]" in default_body
    assert "[0, 1, 0]" in cis_body


def test_declare_states_rejects_malformed_port_pose_overrides(
    handler: SeHandler,
) -> None:
    with pytest.raises(BadInput, match="direction"):
        handler.put(
            id="poseportbad1",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "switch",
                            "envelope": "box:w0.002d0.002h0.002",
                        },
                        {
                            "op": "declare_states",
                            "block": "switch",
                            "states": [
                                {
                                    "name": "cis",
                                    "port_pose_overrides": {"p1": {"xyz": [0, 1, 0]}},
                                }
                            ],
                        },
                    ]
                }
            ),
        )


def test_declare_states_rejects_an_empty_port_pose_override(
    handler: SeHandler,
) -> None:
    """``{}`` states nothing about the port — a write that changes nothing
    at read time should fail at write time, not look applied."""
    with pytest.raises(BadInput, match="at least one of"):
        handler.put(
            id="poseportbad2",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "switch",
                            "envelope": "box:w0.002d0.002h0.002",
                        },
                        {
                            "op": "declare_states",
                            "block": "switch",
                            "states": [
                                {"name": "cis", "port_pose_overrides": {"p1": {}}}
                            ],
                        },
                    ]
                }
            ),
        )


def test_port_pose_overrides_accept_direction_pose_and_rot_together(
    handler: SeHandler, store: Store
) -> None:
    """All three keys in one entry, stored verbatim (bar ``direction``'s
    unit-normalization) — the pose slot's arrival did not cost the
    direction-only shape that already worked."""
    handler.put(
        id="poseportmix1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "switch",
                        "envelope": "box:w0.002d0.002h0.002",
                    },
                    {
                        "op": "add_port",
                        "block": "switch",
                        "name": "p1",
                        "direction": [1, 0, 0],
                        "pose": [0.001, 0, 0],
                    },
                    {
                        "op": "declare_states",
                        "block": "switch",
                        "states": [
                            {
                                "name": "cis",
                                "port_pose_overrides": {
                                    "p1": {
                                        "direction": [0, 2, 0],
                                        "pose": [0.0005, 0, 0],
                                        "rot": [0, 0, 0.5],
                                    }
                                },
                            }
                        ],
                    },
                ]
            }
        ),
    )
    ref = store.get_ref(kind="se", id="poseportmix1")
    assert ref is not None
    uid = _block_uid(store, "poseportmix1", "switch")
    state = design_states.states_for(store, ref.id, uid)[0]
    assert state.port_pose_overrides is not None
    override = state.port_pose_overrides["p1"]
    assert override["direction"] == [0.0, 1.0, 0.0]  # normalized at write time
    assert override["pose"] == [0.0005, 0.0, 0.0]
    assert override["rot"] == [0.0, 0.0, 0.5]

    body = handler.get(
        id="poseportmix1",
        view="block",
        args={"name": "switch", "state": {"switch": "cis"}},
    ).body
    assert "[0, 1, 0]" in body  # direction replaced outright
    assert "[0.0015, 0, 0]" in body  # pose added to the port's own


# ── round 2: set_current_state (persistent) vs args.state (transient) ────


def test_state_arg_is_transient_set_current_state_is_persistent(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="posepersist1", text=json.dumps({"ops": _SWITCH_OPS}))
    ref = store.get_ref(kind="se", id="posepersist1")
    assert ref is not None
    uid = _block_uid(store, "posepersist1", "switch")
    assert design_states.current_state(store, ref.id, uid) is None

    # A transient get-time pose never writes the current-state pointer.
    handler.get(
        id="posepersist1",
        view="block",
        args={"name": "switch", "state": {"switch": "cis"}},
    )
    assert design_states.current_state(store, ref.id, uid) is None

    # set_current_state does.
    handler.edit(
        id="posepersist1",
        ops=[{"op": "set_current_state", "block": "switch", "state": "cis"}],
    )
    assert design_states.current_state(store, ref.id, uid) == "cis"


def test_set_current_state_rejects_undeclared_state_name(handler: SeHandler) -> None:
    handler.put(id="posepersistbad1", text=json.dumps({"ops": _SWITCH_OPS}))
    with pytest.raises(BadInput, match="no state 'bogus'"):
        handler.edit(
            id="posepersistbad1",
            ops=[{"op": "set_current_state", "block": "switch", "state": "bogus"}],
        )


def test_edit_rolls_back_the_whole_unit_when_one_op_fails(
    handler: SeHandler, store: Store
) -> None:
    """``edit()`` wraps ``save_tree`` + ``_materialize_states`` in one
    ``self.store.tx()`` (round 2) — an undeclared-state-name rejection is
    raised only INSIDE that transaction (``_materialize_states``, after
    ``save_tree`` already staged its writes: :func:`_op_set_current_state`
    stores the pending name unchecked, and the undeclared-name check
    itself lives past ``save_tree`` in the same ``with`` block), so a
    rollback here is the only thing standing between one bad op and a
    half-written design. Bundle an unrelated, individually-valid
    ``add_block`` with the failing ``set_current_state`` in one ``edit()``
    call: if the transaction boundary were wrong (e.g. ``save_tree``
    committing on its own connection before ``_materialize_states`` runs),
    the new block would survive the raise — it must not."""
    handler.put(id="posepersistrollback1", text=json.dumps({"ops": _SWITCH_OPS}))
    ref = store.get_ref(kind="se", id="posepersistrollback1")
    assert ref is not None
    uid = _block_uid(store, "posepersistrollback1", "switch")
    assert design_states.current_state(store, ref.id, uid) is None

    with pytest.raises(BadInput, match="no state 'bogus'"):
        handler.edit(
            id="posepersistrollback1",
            ops=[
                {
                    "op": "add_block",
                    "name": "bracket",
                    "envelope": "box:w0.01d0.01h0.01",
                },
                {"op": "set_current_state", "block": "switch", "state": "bogus"},
            ],
        )

    # Neither half of the bundled edit landed: the unrelated add_block is
    # gone, and the state pointer is still unset — not the bogus name, and
    # not silently left half-applied either.
    reloaded = persist.load_tree(store, ref.id)
    assert "bracket" not in reloaded.blocks
    assert design_states.current_state(store, ref.id, uid) is None


def test_set_current_state_rejects_instance(handler: SeHandler) -> None:
    handler.put(
        id="posepersistinst1",
        text=json.dumps(
            {
                "ops": [
                    *_SWITCH_OPS,
                    {"op": "instance_block", "template": "switch", "name": "switch2"},
                ]
            }
        ),
    )
    with pytest.raises(BadInput, match="is an instance"):
        handler.edit(
            id="posepersistinst1",
            ops=[{"op": "set_current_state", "block": "switch2", "state": "cis"}],
        )


# ── round 3: view='sweep' — swept across all declared states ────────────


def test_sweep_finds_the_collision_only_in_the_state_that_has_one(
    handler: SeHandler,
) -> None:
    """``_SWITCH_OPS``' own shape: 'trans' keeps the switch's small box,
    'cis' grows it into the neighbouring wall — the sweep must report the
    'cis' combination as colliding and 'trans' as clean, not smear one
    verdict across both."""
    handler.put(
        id="sweepclash1",
        text=json.dumps(
            {
                "ops": [
                    *_SWITCH_OPS,
                    {
                        "op": "add_block",
                        "name": "wall",
                        "envelope": "box:w0.01d0.01h0.01",
                        "pose": [0.01, 0.0, 0.0],
                    },
                ]
            }
        ),
    )
    body = handler.get(id="sweepclash1", view="sweep").body
    assert "2/2 combination(s) checked" in body
    assert "⚠" in body
    assert "switch=cis" in body
    # The colliding row names the cis combination and the pair; the clean
    # trans combination must not appear as a hit.
    hit_lines = [line for line in body.splitlines() if "switch ↔ wall" in line]
    assert hit_lines, body
    assert all("cis" in line for line in hit_lines)


def test_sweep_no_state_carrying_blocks_is_a_clean_non_error_result(
    handler: SeHandler,
) -> None:
    """A design that never declared 2+ states on anything must read as a
    sensible 'nothing to sweep', never raise."""
    handler.put(
        id="sweepempty1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "bracket",
                        "envelope": "box:w0.01d0.01h0.01",
                    }
                ]
            }
        ),
    )
    body = handler.get(id="sweepempty1", view="sweep").body
    assert "no state-carrying blocks" in body


def test_sweep_two_state_carrying_blocks_is_the_cross_product(
    handler: SeHandler,
) -> None:
    """Two independently-switchable blocks — 2 states × 3 states — sweeps
    all 6 combinations, not 2+3."""
    handler.put(
        id="sweepcross1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "a",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "declare_states",
                        "block": "a",
                        "states": [{"name": "on"}, {"name": "off"}],
                    },
                    {
                        "op": "add_block",
                        "name": "b",
                        "envelope": "box:w0.01d0.01h0.01",
                        "pose": [1.0, 0.0, 0.0],
                    },
                    {
                        "op": "declare_states",
                        "block": "b",
                        "states": [
                            {"name": "x"},
                            {"name": "y"},
                            {"name": "z"},
                        ],
                    },
                ]
            }
        ),
    )
    body = handler.get(id="sweepcross1", view="sweep").body
    assert "2 state-carrying block(s)" in body
    assert "6/6 combination(s) checked" in body
    assert "no interference in any checked state" in body


def test_sweep_single_declared_state_does_not_enter_the_product(
    handler: SeHandler,
) -> None:
    """A block that declared exactly ONE state has no A9 hysteresis to
    worry about (:func:`precis.design.states.state_carrying_uids` — 'more
    than one state') and must contribute nothing to the combination count
    — a design with one 2-state block and one 1-state block sweeps 2
    combinations, not 2."""
    handler.put(
        id="sweepsingle1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "a",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "declare_states",
                        "block": "a",
                        "states": [{"name": "only"}],
                    },
                    {
                        "op": "add_block",
                        "name": "b",
                        "envelope": "box:w0.01d0.01h0.01",
                        "pose": [1.0, 0.0, 0.0],
                    },
                    {
                        "op": "declare_states",
                        "block": "b",
                        "states": [{"name": "on"}, {"name": "off"}],
                    },
                ]
            }
        ),
    )
    body = handler.get(id="sweepsingle1", view="sweep").body
    assert "1 state-carrying block(s)" in body
    assert "2/2 combination(s) checked" in body


def test_sweep_reports_budget_exceeded_honestly(
    handler: SeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 combinations (2×2) against a budget of 2 must name the 2
    unchecked combinations rather than silently reporting only what it
    reached as if it were the whole sweep."""
    import precis_se.handler as se_handler_module

    monkeypatch.setattr(se_handler_module, "_SWEEP_COMBO_BUDGET", 2)
    handler.put(
        id="sweepbudget1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "a",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "declare_states",
                        "block": "a",
                        "states": [{"name": "on"}, {"name": "off"}],
                    },
                    {
                        "op": "add_block",
                        "name": "b",
                        "envelope": "box:w0.01d0.01h0.01",
                        "pose": [1.0, 0.0, 0.0],
                    },
                    {
                        "op": "declare_states",
                        "block": "b",
                        "states": [{"name": "x"}, {"name": "y"}],
                    },
                ]
            }
        ),
    )
    body = handler.get(id="sweepbudget1", view="sweep").body
    assert "2/4 combination(s) checked" in body
    assert "2 combination(s) UNCHECKED" in body


def test_sweep_reports_wall_clock_budget_exceeded_honestly(
    handler: SeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The combination CAP (:data:`_SWEEP_COMBO_BUDGET`, previous test)
    bounds how many combinations are even attempted; this pins the
    complementary wall-clock bound — the overall ``_SWEEP_WALL_BUDGET_S``
    budget shared across every attempted combination's
    ``envelope_overlaps`` call, not a fresh allowance handed out per call
    (pre-ship review, blocktree slice 2: 64 combos × a fresh 30s allowance
    each could run ~32 minutes, unbounded relative to every other se
    view). A fake clock advances past the deadline right after the first
    combination is checked, so only 1 of the 4 combinations is actually
    evaluated — the other 3 must come back UNCHECKED via the wall-clock
    reason, distinct from the combination-cap reason, never silently
    dropped and never read as 'no interference'."""
    import precis_se.handler as se_handler_module

    class _FakeClock:
        """Replaces the ``time`` name in ``precis_se.handler``'s own
        namespace only — ``precis_se.validate``'s independently-bound
        ``time`` import (and its real per-pair deadline check) stays the
        genuine wall clock, so the ONE combination that does get checked
        still runs a real (fast, tiny-geometry) ``envelope_overlaps``
        call rather than starving on the same fake sequence."""

        def __init__(self, values: list[float]) -> None:
            self._it = iter(values)

        def monotonic(self) -> float:
            return next(self._it)

    # call 1: deadline = 0.0 + _SWEEP_WALL_BUDGET_S
    # call 2: remaining check before combo 1 -> still 0.0, proceeds
    # call 3: remaining check before combo 2 -> far past the deadline, bail
    monkeypatch.setattr(se_handler_module, "time", _FakeClock([0.0, 0.0, 1_000_000.0]))

    calls: list[float | None] = []
    real_envelope_overlaps = se_handler_module.se_validate.envelope_overlaps

    def _spy_envelope_overlaps(
        tree: SeTree, *, budget_s: float | None = None
    ) -> tuple[
        list[tuple[str, str, float]], list[tuple[str, str]], list[tuple[str, str]]
    ]:
        calls.append(budget_s)
        return real_envelope_overlaps(tree, budget_s=budget_s)

    monkeypatch.setattr(
        se_handler_module.se_validate, "envelope_overlaps", _spy_envelope_overlaps
    )

    handler.put(
        id="sweepwallclock1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "a",
                        "envelope": "box:w0.01d0.01h0.01",
                    },
                    {
                        "op": "declare_states",
                        "block": "a",
                        "states": [{"name": "on"}, {"name": "off"}],
                    },
                    {
                        "op": "add_block",
                        "name": "b",
                        "envelope": "box:w0.01d0.01h0.01",
                        "pose": [1.0, 0.0, 0.0],
                    },
                    {
                        "op": "declare_states",
                        "block": "b",
                        "states": [{"name": "x"}, {"name": "y"}],
                    },
                ]
            }
        ),
    )
    body = handler.get(id="sweepwallclock1", view="sweep").body

    # Only the one combination the fake clock let through actually called
    # the geometry check — not all 4.
    assert calls == [se_handler_module._SWEEP_WALL_BUDGET_S]
    assert "1/4 combination(s) checked" in body
    assert "3 combination(s) UNCHECKED" in body
    assert "wall-clock budget" in body
    # The combination CAP never fired (4 <= 64) — this must read as a
    # distinct, time-caused reason, not the combination-budget line.
    assert "combination budget (" not in body


def test_sweep_rejects_state_arg(handler: SeHandler) -> None:
    """``view='sweep'`` already checks every state — ``args={'state':...}``
    on top of it is a caller reaching for a capability that doesn't apply
    here, and must be rejected loudly like every other unsupported
    ``args`` key, not silently ignored."""
    handler.put(id="sweepargs1", text=json.dumps({"ops": _SWITCH_OPS}))
    with pytest.raises(BadInput, match="state is not supported on view='sweep'"):
        handler.get(id="sweepargs1", view="sweep", args={"state": {"switch": "trans"}})
