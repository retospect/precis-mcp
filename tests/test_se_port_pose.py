"""The port pose slot (gr342026; docs/backlog/
port-pose-and-composition-search.md §Decision 1): a port carries its own
``pose``/``rot`` in the block's local frame, provenance-tagged, nullable
by design.

Three claims this file pins, in order:

1. **The slot round-trips and is honest about being empty.** ``add_port``/
   ``set_port_pose`` write it, persist stores it, the ports table grows a
   ``pose`` column ONLY when something fills it — a pose-less design must
   render exactly as it did before the column existed.
2. **A state override is a rigid DELTA, and only where it can mean
   something.** ``port_pose_overrides``'s ``pose``/``rot`` add to / compose
   on the port's own; on a port with no pose of its own they change nothing
   (the override stays visible as declared intent, but no origin is
   invented), and a sweep never lets combo *i*'s delta leak into *i+1*.
3. **The geometry consumer says which measurement it used.**
   ``bond_length_sanity`` reports the exact port-to-port distance when both
   ports carry a pose, and names the envelope-extent approximation when
   they don't — the same rule name and severity either way.

The core ops (:mod:`precis.blocktree.ops`) are exercised through se's
handler rather than a bare tree: that is the path an agent actually takes,
and it covers se's ``PortSpec`` rewrap at the same time.

Fixture shape lifted from ``test_se_block_states.py`` — the shared test DB
template carries only core migrations, so the plugin's own are seeded here.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.cad.vec import euler_rad_from_matrix, rotation
from precis.design import states as design_states
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler, _pose_sweep_combo, _snapshot_sweep_domain
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


def _tree(store: Store, slug: str) -> SeTree:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(store, ref.id)


def _put(handler: SeHandler, slug: str, ops: list[dict[str, Any]]) -> None:
    handler.put(id=slug, text=json.dumps({"ops": ops}))


# ── the slot itself: write, persist, render ────────────────────────────────


def test_add_port_pose_round_trips_and_renders_with_its_provenance(
    handler: SeHandler, store: Store
) -> None:
    """put → DB → get: the origin, the frame and the ``declared`` stamp all
    survive, and the ports table shows them."""
    _put(
        handler,
        "portpose1",
        [
            {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
            {
                "op": "add_port",
                "block": "arm",
                "name": "tip",
                "roles": ["covalent"],
                "pose": [0.004, 0.0, 0.0],
                "rot": [0.0, 1.5708, 0.0],
            },
        ],
    )
    port = _tree(store, "portpose1").blocks["arm"].ports["tip"]
    assert port.pose == [0.004, 0.0, 0.0]
    assert port.rot == [0.0, 1.5708, 0.0]
    assert port.pose_source == "declared"

    body = handler.get(id="portpose1", view="block", args={"name": "arm"}).body
    assert "pose" in body
    assert "[0.004, 0, 0]" in body
    assert "rot [0, 1.5708, 0]" in body
    assert "declared" in body
    assert "[0.004, 0, 0]" in handler.get(id="portpose1", view="ports").body


def test_add_port_pose_without_rot_renders_no_rot_clause(
    handler: SeHandler,
) -> None:
    """Unrotated and "no rotation stated" are the same claim — the cell
    omits the clause rather than printing a zeros triple that would read
    like a measured frame."""
    _put(
        handler,
        "portpose2",
        [
            {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
            {"op": "add_port", "block": "arm", "name": "tip", "pose": [0.004, 0, 0]},
        ],
    )
    body = handler.get(id="portpose2", view="block", args={"name": "arm"}).body
    assert "[0.004, 0, 0] · declared" in body
    assert "rot [" not in body


def test_add_port_rot_without_pose_is_refused_naming_the_fix(
    handler: SeHandler,
) -> None:
    """A rotation with no origin is meaningless — refuse it rather than
    storing half a frame."""
    with pytest.raises(BadInput, match="needs 'pose'"):
        _put(
            handler,
            "portposebad1",
            [
                {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
                {"op": "add_port", "block": "arm", "name": "tip", "rot": [0, 1, 0]},
            ],
        )


def test_add_port_scalar_pose_is_refused(handler: SeHandler) -> None:
    """A bare scalar is not a position, however plausible the number."""
    with pytest.raises(BadInput, match="3-vector"):
        _put(
            handler,
            "portposebad2",
            [
                {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
                {"op": "add_port", "block": "arm", "name": "tip", "pose": 0.004},
            ],
        )


def test_a_design_with_no_port_poses_grows_no_pose_column(
    handler: SeHandler,
) -> None:
    """The slot is nullable by design, so the overwhelmingly common design
    must render byte-identically to before it existed — no column of
    dashes (the mode-scoped rule the atomic columns already follow)."""
    _put(
        handler,
        "portposenone1",
        [
            {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
            {"op": "add_port", "block": "arm", "name": "tip", "direction": [1, 0, 0]},
        ],
    )
    block_body = handler.get(
        id="portposenone1", view="block", args={"name": "arm"}
    ).body
    ports_body = handler.get(id="portposenone1", view="ports").body
    for body in (ports_body, block_body.split("## ports", 1)[1]):
        header = next(line for line in body.splitlines() if "annotations" in line)
        assert "pose" not in header, header


# ── set_port_pose ──────────────────────────────────────────────────────────


def test_set_port_pose_sets_updates_and_clears(
    handler: SeHandler, store: Store
) -> None:
    """The three things the op does, in the order an agent does them: fill
    the slot after the fact, correct it, and — when the number turns out to
    be a guess — take it back out rather than leave a fiction in place."""
    _put(
        handler,
        "setpose1",
        [
            {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
            {"op": "add_port", "block": "arm", "name": "tip"},
        ],
    )
    assert _tree(store, "setpose1").blocks["arm"].ports["tip"].pose is None

    handler.edit(
        id="setpose1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "set_port_pose",
                        "block": "arm",
                        "name": "tip",
                        "pose": [0.005, 0, 0],
                    }
                ]
            }
        ),
    )
    port = _tree(store, "setpose1").blocks["arm"].ports["tip"]
    assert port.pose == [0.005, 0.0, 0.0]
    assert port.pose_source == "declared"

    handler.edit(
        id="setpose1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "set_port_pose",
                        "block": "arm",
                        "name": "tip",
                        "pose": [0.006, 0, 0],
                        "rot": [0, 0, 0.5],
                    }
                ]
            }
        ),
    )
    port = _tree(store, "setpose1").blocks["arm"].ports["tip"]
    assert port.pose == [0.006, 0.0, 0.0]
    assert port.rot == [0.0, 0.0, 0.5]

    handler.edit(
        id="setpose1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "set_port_pose",
                        "block": "arm",
                        "name": "tip",
                        "clear": True,
                    }
                ]
            }
        ),
    )
    port = _tree(store, "setpose1").blocks["arm"].ports["tip"]
    assert (port.pose, port.rot, port.pose_source) == (None, None, None)


def test_set_port_pose_keeps_the_se_port_spec(handler: SeHandler, store: Store) -> None:
    """The core op mutates the port in place — se's atomic fields on that
    same port must not be dropped on the floor by the write."""
    _put(
        handler,
        "setpose2",
        [
            {"op": "add_block", "name": "arm", "envelope": "sphere:r5e-10"},
            {
                "op": "add_port",
                "block": "arm",
                "name": "tip",
                "roles": ["covalent"],
                "expected_element": "C",
            },
        ],
    )
    handler.edit(
        id="setpose2",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "set_port_pose",
                        "block": "arm",
                        "name": "tip",
                        "pose": [1e-10, 0, 0],
                    }
                ]
            }
        ),
    )
    port = _tree(store, "setpose2").blocks["arm"].ports["tip"]
    assert port.expected_element == "C"
    assert port.pose == [1e-10, 0.0, 0.0]


def test_set_port_pose_refuses_an_instance_block(handler: SeHandler) -> None:
    """An instance resolves its ports from its template — posing one there
    would be a per-instance override the read path never looks for."""
    with pytest.raises(BadInput, match="is an instance"):
        _put(
            handler,
            "setposebad1",
            [
                {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
                {"op": "add_port", "block": "arm", "name": "tip"},
                {"op": "instance_block", "name": "arm2", "template": "arm"},
                {
                    "op": "set_port_pose",
                    "block": "arm2",
                    "name": "tip",
                    "pose": [0.001, 0, 0],
                },
            ],
        )


def test_set_port_pose_on_an_unknown_port_names_the_roster(
    handler: SeHandler,
) -> None:
    with pytest.raises(BadInput, match="Available ports: tip"):
        _put(
            handler,
            "setposebad2",
            [
                {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
                {"op": "add_port", "block": "arm", "name": "tip"},
                {
                    "op": "set_port_pose",
                    "block": "arm",
                    "name": "typo",
                    "pose": [0.001, 0, 0],
                },
            ],
        )


def test_set_port_pose_rot_alone_on_a_poseless_port_is_refused(
    handler: SeHandler,
) -> None:
    """``rot`` alone is legal only as a correction to an origin that is
    already there — never as the first thing written to the slot, and the
    refusal is worded exactly as ``add_port``'s."""
    with pytest.raises(BadInput, match="a rotation with no origin is meaningless"):
        _put(
            handler,
            "setposebad3",
            [
                {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
                {"op": "add_port", "block": "arm", "name": "tip"},
                {
                    "op": "set_port_pose",
                    "block": "arm",
                    "name": "tip",
                    "rot": [0, 0, 1],
                },
            ],
        )


def test_set_port_pose_with_no_fields_at_all_is_refused(handler: SeHandler) -> None:
    """Neither a write nor an explicit clear — say so, rather than succeed
    as a silent no-op."""
    with pytest.raises(BadInput, match="carries no origin yet"):
        _put(
            handler,
            "setposebad4",
            [
                {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
                {"op": "add_port", "block": "arm", "name": "tip"},
                {"op": "set_port_pose", "block": "arm", "name": "tip"},
            ],
        )


def test_set_port_pose_rot_alone_rewrites_the_frame_over_a_stored_origin(
    handler: SeHandler, store: Store
) -> None:
    _put(
        handler,
        "setpose3",
        [
            {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
            {"op": "add_port", "block": "arm", "name": "tip", "pose": [0.004, 0, 0]},
            {"op": "set_port_pose", "block": "arm", "name": "tip", "rot": [0, 0, 0.25]},
        ],
    )
    port = _tree(store, "setpose3").blocks["arm"].ports["tip"]
    assert port.pose == [0.004, 0.0, 0.0]
    assert port.rot == [0.0, 0.0, 0.25]


# ── state overrides as a rigid delta ───────────────────────────────────────

_DELTA_OPS: list[dict[str, Any]] = [
    {"op": "add_block", "name": "switch", "envelope": "box:w0.002d0.002h0.002"},
    {
        "op": "add_port",
        "block": "switch",
        "name": "far",
        "pose": [0.001, 0.0, 0.0],
        "rot": [0.0, 0.0, 0.0],
    },
    {
        "op": "declare_states",
        "block": "switch",
        "states": [
            {"name": "trans"},
            {
                "name": "cis",
                "port_pose_overrides": {
                    "far": {"pose": [0.0009, 0.0, 0.0], "rot": [0.0, 0.0, 0.5]}
                },
            },
        ],
    },
]


def test_state_pose_delta_moves_the_port_origin_and_composes_its_rot(
    handler: SeHandler,
) -> None:
    """The requirement language is a delta ("in this state the far port
    moves 9 Å along x"), so the stored override adds to the port's own
    origin rather than replacing it."""
    _put(handler, "delta1", _DELTA_OPS)
    default_body = handler.get(id="delta1", view="block", args={"name": "switch"}).body
    cis_body = handler.get(
        id="delta1",
        view="block",
        args={"name": "switch", "state": {"switch": "cis"}},
    ).body
    assert "[0.001, 0, 0]" in default_body
    assert "[0.0019, 0, 0]" in cis_body
    assert "rot [0, 0, 0.5]" in cis_body


def test_state_delta_on_a_poseless_port_invents_no_origin(
    handler: SeHandler,
) -> None:
    """No origin to displace means no pose to report — the approximation
    stays, and the state row still carries the raw override so the intent
    is not lost, only unapplied."""
    _put(
        handler,
        "delta2",
        [
            {"op": "add_block", "name": "switch", "envelope": "box:w0.002d0.002h0.002"},
            {"op": "add_port", "block": "switch", "name": "far"},
            {
                "op": "declare_states",
                "block": "switch",
                "states": [
                    {"name": "trans"},
                    {
                        "name": "cis",
                        "port_pose_overrides": {"far": {"pose": [0.0009, 0, 0]}},
                    },
                ],
            },
        ],
    )
    cis_body = handler.get(
        id="delta2",
        view="block",
        args={"name": "switch", "state": {"switch": "cis"}},
    ).body
    # No port in the design carries a pose, so the column never appears —
    # and certainly not with a manufactured [0.0009, 0, 0] in it.
    assert "0.0009, 0, 0" not in cis_body.split("## ports", 1)[1]
    # The declared intent is still on the states table.
    assert "0.0009" in cis_body.split("## states", 1)[1].split("## ports", 1)[0]


def test_a_sweep_combination_never_inherits_the_previous_delta(
    handler: SeHandler, store: Store
) -> None:
    """A ``pose`` override ACCUMULATES, unlike ``direction``, which merely
    overwrites — so a missing snapshot/restore field here would compound
    across the cross product instead of leaking once."""
    _put(handler, "delta3", _DELTA_OPS)
    tree = _tree(store, "delta3")
    uid = tree.blocks["switch"].uid
    assert uid is not None
    trans = design_states.BlockState(block_uid=uid, name="trans")
    cis = design_states.BlockState(
        block_uid=uid,
        name="cis",
        port_pose_overrides={"far": {"pose": [0.0009, 0.0, 0.0]}},
    )
    originals = _snapshot_sweep_domain(tree, ["switch"])

    _pose_sweep_combo(tree, originals, ["switch"], (cis,))
    once = list(tree.blocks["switch"].ports["far"].pose or [])
    _pose_sweep_combo(tree, originals, ["switch"], (cis,))
    assert list(tree.blocks["switch"].ports["far"].pose or []) == once

    _pose_sweep_combo(tree, originals, ["switch"], (trans,))
    assert tree.blocks["switch"].ports["far"].pose == [0.001, 0.0, 0.0]


def test_state_rot_delta_composes_rather_than_replacing(
    handler: SeHandler, store: Store
) -> None:
    """Composed on the port's OWN frame: a port already turned 0.25 rad
    about z, told to turn 0.5 more, ends at 0.75 — not at 0.5."""
    _put(
        handler,
        "delta4",
        [
            {"op": "add_block", "name": "switch", "envelope": "box:w0.002d0.002h0.002"},
            {
                "op": "add_port",
                "block": "switch",
                "name": "far",
                "pose": [0.001, 0, 0],
                "rot": [0, 0, 0.25],
            },
            {
                "op": "declare_states",
                "block": "switch",
                "states": [
                    {"name": "trans"},
                    {
                        "name": "cis",
                        "port_pose_overrides": {"far": {"rot": [0, 0, 0.5]}},
                    },
                ],
            },
        ],
    )
    tree = _tree(store, "delta4")
    uid = tree.blocks["switch"].uid
    assert uid is not None
    cis = design_states.BlockState(
        block_uid=uid, name="cis", port_pose_overrides={"far": {"rot": [0, 0, 0.5]}}
    )
    _pose_sweep_combo(
        tree, _snapshot_sweep_domain(tree, ["switch"]), ["switch"], (cis,)
    )
    rot = tree.blocks["switch"].ports["far"].rot
    assert rot is not None
    assert math.isclose(rot[2], 0.75, abs_tol=1e-9)


def test_state_rot_delta_is_applied_in_the_block_frame(
    handler: SeHandler, store: Store
) -> None:
    """Off-axis base and delta do not commute, so this pins the order the
    single-axis tests cannot see: the delta is a rotation about a BLOCK
    axis applied on top of the port's own frame (``R_delta @ R_port``),
    not a port-local spin (``R_port @ R_delta``)."""
    base = [0.3, 0.0, 0.0]
    delta = [0.0, 0.5, 0.0]
    _put(
        handler,
        "delta5",
        [
            {"op": "add_block", "name": "switch", "envelope": "box:w0.002d0.002h0.002"},
            {
                "op": "add_port",
                "block": "switch",
                "name": "far",
                "pose": [0.001, 0, 0],
                "rot": base,
            },
            {
                "op": "declare_states",
                "block": "switch",
                "states": [
                    {"name": "trans"},
                    {"name": "cis", "port_pose_overrides": {"far": {"rot": delta}}},
                ],
            },
        ],
    )
    tree = _tree(store, "delta5")
    uid = tree.blocks["switch"].uid
    assert uid is not None
    cis = design_states.BlockState(
        block_uid=uid, name="cis", port_pose_overrides={"far": {"rot": delta}}
    )
    _pose_sweep_combo(
        tree, _snapshot_sweep_domain(tree, ["switch"]), ["switch"], (cis,)
    )
    rot = tree.blocks["switch"].ports["far"].rot
    assert rot is not None
    block_frame = euler_rad_from_matrix(rotation(*delta).compose(rotation(*base)).R)
    port_local = euler_rad_from_matrix(rotation(*base).compose(rotation(*delta)).R)
    assert all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(rot, block_frame))
    assert not all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(rot, port_local))


def test_set_port_pose_origin_nudge_keeps_the_stored_rot(
    handler: SeHandler, store: Store
) -> None:
    """Each key rewrites only itself: re-measuring the origin must not
    silently unrotate the port — dropping a rotation is ``clear=True``'s
    job, never a side effect."""
    _put(
        handler,
        "setpose2",
        [
            {"op": "add_block", "name": "arm", "envelope": "box:w0.01d0.01h0.01"},
            {
                "op": "add_port",
                "block": "arm",
                "name": "tip",
                "pose": [0.004, 0, 0],
                "rot": [0, 0, 0.25],
            },
        ],
    )
    handler.edit(
        id="setpose2",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "set_port_pose",
                        "block": "arm",
                        "name": "tip",
                        "pose": [0.0041, 0, 0],
                    }
                ]
            }
        ),
    )
    port = _tree(store, "setpose2").blocks["arm"].ports["tip"]
    assert port.pose == [0.0041, 0.0, 0.0]
    assert port.rot == [0.0, 0.0, 0.25]
    assert port.pose_source == "declared"


# ── the consumer: bond_length_sanity says which measurement it used ────────


def test_bond_length_uses_the_exact_port_to_port_distance_when_both_posed(
    handler: SeHandler,
) -> None:
    """Two 1 nm blocks 2 nm apart whose ports face each other across a
    1.5 Å gap: the block-pose approximation alone would flag this, the real
    port-to-port distance must not."""
    _put(
        handler,
        "portbond1",
        [
            {"op": "add_block", "name": "a", "envelope": "sphere:r5e-10"},
            {
                "op": "add_block",
                "name": "b",
                "envelope": "sphere:r5e-10",
                "pose": [2e-9, 0, 0],
            },
            {
                "op": "add_port",
                "block": "a",
                "name": "p1",
                "roles": ["covalent"],
                "pose": [9.25e-10, 0, 0],
            },
            {
                "op": "add_port",
                "block": "b",
                "name": "p1",
                "roles": ["covalent"],
                "pose": [-9.25e-10, 0, 0],
            },
            {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "bond"},
        ],
    )
    assert "bond_length_sanity" not in handler.get(id="portbond1", view="validate").body


def test_bond_length_flags_a_long_port_to_port_span_with_the_new_wording(
    handler: SeHandler,
) -> None:
    """Same two blocks, ports pointing the wrong way — the finding names
    the port-to-port measurement and both provenance stamps, so nobody
    reads it as the older approximation."""
    _put(
        handler,
        "portbond2",
        [
            {"op": "add_block", "name": "a", "envelope": "sphere:r5e-10"},
            {
                "op": "add_block",
                "name": "b",
                "envelope": "sphere:r5e-10",
                "pose": [2e-9, 0, 0],
            },
            {
                "op": "add_port",
                "block": "a",
                "name": "p1",
                "roles": ["covalent"],
                "pose": [-9e-10, 0, 0],
            },
            {
                "op": "add_port",
                "block": "b",
                "name": "p1",
                "roles": ["covalent"],
                "pose": [9e-10, 0, 0],
            },
            {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "bond"},
        ],
    )
    body = handler.get(id="portbond2", view="validate").body
    assert "bond_length_sanity" in body
    assert "port-to-port distance" in body
    assert "both ports carry a stored pose: a=declared, b=declared" in body


def test_bond_length_falls_back_and_says_the_ports_have_no_pose(
    handler: SeHandler,
) -> None:
    """The pre-existing path, reworded: when the slot is empty the finding
    must SAY it is an approximation about blocks, not ports."""
    _put(
        handler,
        "portbond3",
        [
            {"op": "add_block", "name": "a", "envelope": "sphere:r5e-10"},
            {
                "op": "add_block",
                "name": "b",
                "envelope": "sphere:r5e-10",
                "pose": [48.5, 0, 0],
            },
            {"op": "add_port", "block": "a", "name": "p1", "roles": ["covalent"]},
            {"op": "add_port", "block": "b", "name": "p1", "roles": ["covalent"]},
            {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "bond"},
        ],
    )
    body = handler.get(id="portbond3", view="validate").body
    assert "bond_length_sanity" in body
    assert "these ports carry no stored pose of their own" in body
    assert "port-to-port distance" not in body


def test_bond_length_falls_back_when_only_one_end_is_posed(
    handler: SeHandler,
) -> None:
    """Half a measurement is not a measurement — one stored pose does not
    buy the exact distance."""
    _put(
        handler,
        "portbond4",
        [
            {"op": "add_block", "name": "a", "envelope": "sphere:r5e-10"},
            {
                "op": "add_block",
                "name": "b",
                "envelope": "sphere:r5e-10",
                "pose": [48.5, 0, 0],
            },
            {
                "op": "add_port",
                "block": "a",
                "name": "p1",
                "roles": ["covalent"],
                "pose": [1e-10, 0, 0],
            },
            {"op": "add_port", "block": "b", "name": "p1", "roles": ["covalent"]},
            {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "bond"},
        ],
    )
    body = handler.get(id="portbond4", view="validate").body
    assert "these ports carry no stored pose of their own" in body
