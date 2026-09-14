"""se **atomic mode** — the L2 vocabulary the merged ``nm`` kind brought.

docs/backlog/nm-se-merge.md's first in-scope item, storage half: threading,
declared dof, the ports' expected chemistry, and the ``kind='bond'``
capability gate now live on se's own tree/ops/persist
(:mod:`precis_se.atomic.vocab` + :mod:`precis_se.ops`), with migration
``0007_se_atomic.sql`` holding the columns and ``se_topology``.

The rules transferred from ``precis_nm.ops`` unchanged, so the pins here
are the ported ones — what an agent is *refused* matters as much as what it
is allowed, and each refusal names its own fix. Two things are genuinely
new and pinned as such: a connect's ``kind`` is **explicit** (absent = se's
ordinary structural edge, whose L2 statement is its ``joint``, and the two
are mutually exclusive on one edge), and the atomic columns in a rendered
ports table appear only when something fills them (mode-scoped help).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import precis_se
from precis.dispatch import Hub
from precis.store import Store
from precis_se import drc as se_drc
from precis_se import persist
from precis_se.atomic.vocab import ThreadingSpec, connect_role
from precis_se.handler import SeHandler
from precis_se.ops import (
    OpError,
    SeTree,
    apply_ops,
    effective_dof,
    known_ops,
)

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: A rotaxane: a macrocycle threaded on an axle, the canonical atomic-mode
#: design (the same shape the nm kind's own tests used).
_ROTAXANE_OPS: list[dict[str, object]] = [
    {"op": "add_block", "name": "axle", "envelope": "cyl:r2.5e-10h5e-09"},
    {"op": "add_block", "name": "ring", "envelope": "torus:R1e-09r3e-10"},
    {
        "op": "add_port",
        "block": "axle",
        "name": "head",
        "roles": ["covalent"],
        "expected_element": "C",
        "expected_hybridization": "sp2",
    },
    {"op": "add_port", "block": "axle", "name": "tail", "roles": ["covalent"]},
    {"op": "add_port", "block": "ring", "name": "hook", "roles": ["covalent"]},
    {"op": "connect", "a": "axle.head", "b": "ring.hook", "kind": "bond"},
    {"op": "declare_threading", "a": "ring", "b": "axle"},
    {
        "op": "declare_dof",
        "block": "axle",
        "kind": "rotational",
        "axis_ports": ["head", "tail"],
    },
]


def _tree(*extra: dict[str, object]) -> SeTree:
    return apply_ops(SeTree(), [*_ROTAXANE_OPS, *extra])


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


# ── the op roster ────────────────────────────────────────────────────────


def test_the_four_atomic_ops_are_in_the_dispatch_roster() -> None:
    """The roster the unknown-op error reads is the dispatch table itself,
    so an op that exists is always offered and vice versa."""
    assert {
        "declare_threading",
        "remove_threading",
        "declare_dof",
        "clear_dof",
    } <= known_ops()


def test_an_unknown_op_names_the_atomic_ops_among_the_alternatives() -> None:
    with pytest.raises(OpError) as exc:
        apply_ops(SeTree(), [{"op": "thread_it", "a": "x", "b": "y"}])
    assert "declare_threading" in str(exc.value)


# ── threading ────────────────────────────────────────────────────────────


def test_threading_is_directional_and_stored_as_declared() -> None:
    assert _tree().threading == [ThreadingSpec(a="ring", b="axle")]


def test_threading_a_block_through_itself_is_refused() -> None:
    with pytest.raises(OpError) as exc:
        _tree({"op": "declare_threading", "a": "axle", "b": "axle"})
    assert "must differ" in str(exc.value)


def test_threading_a_block_that_does_not_exist_lists_what_does() -> None:
    with pytest.raises(OpError) as exc:
        _tree({"op": "declare_threading", "a": "ghost", "b": "axle"})
    assert "axle" in str(exc.value)


def test_threading_the_same_pair_twice_is_refused() -> None:
    with pytest.raises(OpError) as exc:
        _tree({"op": "declare_threading", "a": "ring", "b": "axle"})
    assert "already declared threaded through" in str(exc.value)


def test_mutual_threading_is_refused_as_physically_impossible() -> None:
    """Each would be inside the other — the refusal says so and names
    remove_threading as the fix for a wrong-direction declaration."""
    with pytest.raises(OpError) as exc:
        _tree({"op": "declare_threading", "a": "axle", "b": "ring"})
    assert "physically impossible" in str(exc.value)
    assert "remove_threading" in str(exc.value)


def test_remove_threading_drops_exactly_the_named_pair() -> None:
    tree = _tree({"op": "remove_threading", "a": "ring", "b": "axle"})
    assert tree.threading == []


def test_remove_threading_that_is_not_live_lists_what_is() -> None:
    with pytest.raises(OpError) as exc:
        _tree({"op": "remove_threading", "a": "axle", "b": "ring"})
    assert "ring→axle" in str(exc.value)


def test_removing_a_block_drops_the_threading_that_touched_it() -> None:
    """The vacancy rule connects/measures/BOM already follow — a threading
    pair naming a vanished block would otherwise dangle."""
    tree = _tree(
        {"op": "disconnect", "a": "axle.head", "b": "ring.hook"},
        {"op": "clear_dof", "block": "axle"},
        {"op": "remove_block", "block": "axle"},
    )
    assert tree.threading == []


# ── declared dof ─────────────────────────────────────────────────────────


def test_declare_dof_stores_the_canonical_payload() -> None:
    assert _tree().blocks["axle"].dof == {
        "kind": "rotational",
        "axis_ports": ["head", "tail"],
    }


def test_dof_rejects_an_unregistered_key_instead_of_dropping_it() -> None:
    """gripe 334765: ``states``/``driver`` used to ride along unread."""
    with pytest.raises(OpError) as exc:
        _tree(
            {
                "op": "declare_dof",
                "block": "ring",
                "kind": "rotational",
                "axis_ports": ["hook", "hook"],
                "driver": "light",
            }
        )
    assert "unknown key(s) ['driver']" in str(exc.value)


def test_dof_kind_is_a_closed_vocabulary() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            {
                "op": "declare_dof",
                "block": "ring",
                "kind": "helical",
                "axis_ports": ["hook", "hook"],
            }
        )
    assert "'kind' must be one of ('rotational', 'translational')" in str(exc.value)


def test_dof_needs_exactly_two_axis_ports() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            {
                "op": "declare_dof",
                "block": "ring",
                "kind": "rotational",
                "axis_ports": ["hook"],
            }
        )
    assert "exactly 2 port names" in str(exc.value)


def test_dof_axis_ports_must_resolve_on_the_blocks_own_ports() -> None:
    """gripe 334765's repro: a block accepted axis_ports naming ports that
    existed nowhere."""
    with pytest.raises(OpError) as exc:
        _tree(
            {
                "op": "declare_dof",
                "block": "ring",
                "kind": "rotational",
                "axis_ports": ["hook", "nowhere"],
            }
        )
    assert "no such port on block 'ring': 'nowhere'" in str(exc.value)
    assert "Available ports: hook" in str(exc.value)


def test_clear_dof_removes_it() -> None:
    tree = _tree({"op": "clear_dof", "block": "axle"})
    assert tree.blocks["axle"].dof is None


def test_add_block_takes_a_nested_dof_payload() -> None:
    tree = _tree(
        {
            "op": "add_block",
            "name": "stopper",
            "dof": {"kind": "translational", "axis_ports": ["a", "b"]},
        }
    )
    assert tree.blocks["stopper"].dof == {
        "kind": "translational",
        "axis_ports": ["a", "b"],
    }


def test_a_bad_nested_dof_rolls_the_new_block_back_out() -> None:
    """Half a block is worse than none: the mint is undone, not left
    behind for the next op to trip over."""
    tree = SeTree()
    with pytest.raises(OpError):
        apply_ops(tree, [{"op": "add_block", "name": "half", "dof": {"kind": "spin"}}])
    assert "half" not in tree.blocks


def test_dof_on_an_instance_is_refused_and_points_at_the_template() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            {"op": "instance_block", "name": "axle2", "template": "axle"},
            {
                "op": "declare_dof",
                "block": "axle2",
                "kind": "rotational",
                "axis_ports": ["head", "tail"],
            },
        )
    assert "the dof lives on the template" in str(exc.value)


def test_instance_block_refuses_a_dof_key_rather_than_swallowing_it() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            {
                "op": "instance_block",
                "name": "axle2",
                "template": "axle",
                "dof": {"kind": "rotational", "axis_ports": ["head", "tail"]},
            }
        )
    assert "instance_block does not take 'dof'" in str(exc.value)
    assert "'axle'" in str(exc.value)


def test_array_block_refuses_a_dof_key_too() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            {
                "op": "array_block",
                "name": "axles",
                "template": "axle",
                "linear": {"count": 3, "pitch": 1e-09, "axis": [0, 0, 1]},
                "dof": {"kind": "rotational", "axis_ports": ["head", "tail"]},
            }
        )
    assert "array_block does not take 'dof'" in str(exc.value)


def test_an_instance_resolves_its_templates_dof_at_read_time() -> None:
    tree = _tree({"op": "instance_block", "name": "axle2", "template": "axle"})
    assert effective_dof(tree, tree.blocks["axle2"]) == tree.blocks["axle"].dof
    assert tree.blocks["axle2"].dof is None


def test_removing_a_port_the_dof_axis_names_is_refused() -> None:
    tree = _tree({"op": "disconnect", "a": "axle.head", "b": "ring.hook"})
    with pytest.raises(OpError) as exc:
        apply_ops(tree, [{"op": "remove_port", "block": "axle", "name": "tail"}])
    assert "used by declared dof" in str(exc.value)
    assert "clear_dof first" in str(exc.value)


def test_a_port_outside_the_dof_axis_still_removes() -> None:
    tree = _tree({"op": "add_port", "block": "axle", "name": "spare"})
    apply_ops(tree, [{"op": "remove_port", "block": "axle", "name": "spare"}])
    assert "spare" not in tree.blocks["axle"].ports


# ── ports: the expected chemistry ────────────────────────────────────────


def test_add_port_stores_the_expected_element_and_hybridization() -> None:
    port = _tree().blocks["axle"].ports["head"]
    assert (port.expected_element, port.expected_hybridization) == ("C", "sp2")
    # the atom-side projection stays empty until bind_structure fills it
    assert (port.bound_design, port.bound_atom) == (None, None)


def test_a_port_with_no_chemistry_keeps_both_fields_empty() -> None:
    port = _tree().blocks["axle"].ports["tail"]
    assert port.expected_element is None
    assert port.expected_hybridization is None


# ── connect: kind + the bond capability gate ─────────────────────────────


def test_a_bond_connect_records_its_kind() -> None:
    assert _tree().connects[0].kind == "bond"


def test_a_structural_connect_has_no_kind_and_is_not_gated() -> None:
    """se's ordinary edge: no ``kind``, no role to afford — a bolted
    bracket's ports carry no chemistry capabilities at all."""
    tree = apply_ops(
        SeTree(),
        [
            {"op": "add_block", "name": "plate"},
            {"op": "add_block", "name": "rail"},
            {"op": "add_port", "block": "plate", "name": "p"},
            {"op": "add_port", "block": "rail", "name": "q"},
            {"op": "connect", "a": "plate.p", "b": "rail.q"},
        ],
    )
    assert tree.connects[0].kind is None


def test_a_bond_needs_both_ports_to_afford_the_role() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            {"op": "add_port", "block": "ring", "name": "bare"},
            {"op": "connect", "a": "axle.tail", "b": "ring.bare", "kind": "bond"},
        )
    assert "does not afford 'covalent'" in str(exc.value)
    assert "add_port" in str(exc.value)


def test_the_role_can_be_overridden_through_objectives() -> None:
    tree = _tree(
        {"op": "add_port", "block": "axle", "name": "face", "roles": ["pi_stack"]},
        {"op": "add_port", "block": "ring", "name": "ring_face", "roles": ["pi_stack"]},
        {
            "op": "connect",
            "a": "axle.face",
            "b": "ring.ring_face",
            "kind": "bond",
            "objectives": {"role": "pi_stack"},
        },
    )
    assert tree.connects[-1].objectives == {"role": "pi_stack"}


def test_the_overridden_role_is_the_one_gated_on() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            {"op": "add_port", "block": "axle", "name": "face", "roles": ["pi_stack"]},
            {
                "op": "connect",
                "a": "axle.face",
                "b": "ring.hook",
                "kind": "bond",
                "objectives": {"role": "pi_stack"},
            },
        )
    assert "does not afford 'pi_stack'" in str(exc.value)


def test_an_interaction_connect_is_not_capability_gated() -> None:
    tree = _tree(
        {"op": "add_port", "block": "ring", "name": "bare"},
        {"op": "connect", "a": "axle.tail", "b": "ring.bare", "kind": "interaction"},
    )
    assert tree.connects[-1].kind == "interaction"


def test_connect_role_is_none_for_everything_but_a_bond() -> None:
    assert connect_role("bond", {}) == "covalent"
    assert connect_role("bond", {"role": " pi_stack "}) == "pi_stack"
    assert connect_role("interaction", {"role": "pi_stack"}) is None
    assert connect_role(None, {}) is None


def test_connect_kind_is_a_closed_vocabulary() -> None:
    with pytest.raises(OpError) as exc:
        _tree(
            {"op": "add_port", "block": "ring", "name": "bare"},
            {"op": "connect", "a": "axle.tail", "b": "ring.bare", "kind": "vdw"},
        )
    assert "must be bond | interaction" in str(exc.value)


def test_a_kind_and_a_joint_on_one_edge_are_mutually_exclusive() -> None:
    """A kinematic class and a bond are different claims about the same
    pair, and nothing reads both."""
    with pytest.raises(OpError) as exc:
        _tree(
            {"op": "add_port", "block": "ring", "name": "bare"},
            {
                "op": "connect",
                "a": "axle.tail",
                "b": "ring.bare",
                "kind": "interaction",
                "joint": {"class": "revolute", "axis": [0, 0, 1]},
            },
        )
    assert "mutually exclusive" in str(exc.value)


def test_set_joint_refuses_to_reach_the_same_contradiction_the_long_way() -> None:
    tree = _tree()
    with pytest.raises(OpError) as exc:
        apply_ops(
            tree,
            [
                {
                    "op": "set_joint",
                    "a": "axle.head",
                    "b": "ring.hook",
                    "joint": {"class": "revolute", "axis": [0, 0, 1]},
                }
            ],
        )
    assert "is an atomic bond connect" in str(exc.value)
    assert tree.connects[0].joint is None


def test_clearing_a_joint_on_an_atomic_edge_is_still_allowed() -> None:
    """``joint=null`` states nothing, so it contradicts nothing."""
    tree = _tree()
    apply_ops(
        tree,
        [{"op": "set_joint", "a": "axle.head", "b": "ring.hook", "joint": None}],
    )
    assert tree.connects[0].joint is None


# ── the 'role' objective is edge-only ────────────────────────────────────


def test_role_is_a_registered_objective_on_a_connect() -> None:
    from precis_se.joints import OBJECTIVE_KEYS, validate_objectives

    assert "role" in OBJECTIVE_KEYS
    assert validate_objectives({"role": " covalent "}) == {"role": "covalent"}


def test_an_empty_role_is_refused_rather_than_stored_blank() -> None:
    from precis_se.joints import JointError, validate_objectives

    with pytest.raises(JointError) as exc:
        validate_objectives({"role": "  "})
    assert "non-empty capability" in str(exc.value)


def test_role_on_a_block_is_refused_the_way_fixed_is_on_a_connect() -> None:
    with pytest.raises(OpError) as exc:
        _tree({"op": "set_load", "block": "axle", "role": "covalent"})
    assert "has no meaning on a block" in str(exc.value)
    assert "its ports' roles" in str(exc.value)


def test_role_still_reaches_a_connects_loads() -> None:
    tree = _tree(
        {"op": "set_load", "a": "axle.head", "b": "ring.hook", "role": "covalent"}
    )
    assert tree.connects[0].objectives == {"role": "covalent"}


# ── DRC: the threading graph re-checked over stored data ─────────────────


def test_a_threading_pair_naming_a_vanished_block_is_a_drc_error() -> None:
    """Defense in depth: ``remove_block`` already cascades, so a live
    finding here means the row arrived some other way."""
    tree = _tree()
    tree.threading.append(ThreadingSpec(a="ring", b="ghost"))
    findings = {
        (f.rule, f.severity) for f in se_drc.drc(tree).findings if "ghost" in f.detail
    }
    assert findings == {("dangling_threading", "error")}


def test_threading_without_an_envelope_is_a_warn_not_an_error() -> None:
    tree = _tree(
        {"op": "add_block", "name": "cap"},
        {"op": "declare_threading", "a": "cap", "b": "axle"},
    )
    findings = [
        f for f in se_drc.drc(tree).findings if f.rule == "threaded_without_envelope"
    ]
    assert [(f.subject, f.severity) for f in findings] == [("cap→axle", "warn")]
    assert "cap" in findings[0].detail


def test_a_fully_enveloped_threading_pair_yields_no_topology_finding() -> None:
    rules = {f.rule for f in se_drc.drc(_tree()).findings}
    assert "dangling_threading" not in rules
    assert "threaded_without_envelope" not in rules


# ── persistence: the round trip through migration 0007's storage ─────────


def test_the_atomic_facts_survive_a_save_and_reload(handler: SeHandler) -> None:
    handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    ref = handler.store.get_ref(kind="se", id="rotaxane1")
    assert ref is not None
    tree = persist.load_tree(handler.store, ref.id)
    assert tree.threading == [ThreadingSpec(a="ring", b="axle")]
    assert tree.blocks["axle"].dof == {
        "kind": "rotational",
        "axis_ports": ["head", "tail"],
    }
    port = tree.blocks["axle"].ports["head"]
    assert (port.expected_element, port.expected_hybridization) == ("C", "sp2")
    assert tree.connects[0].kind == "bond"


def test_a_re_put_does_not_accumulate_threading_rows(handler: SeHandler) -> None:
    """The retire-all/reinsert-all save model applies to ``se_topology``
    like every other side table — twice-saved must not mean twice-stored."""
    handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    ref = handler.store.get_ref(kind="se", id="rotaxane1")
    assert ref is not None
    tree = persist.load_tree(handler.store, ref.id)
    assert tree.threading == [ThreadingSpec(a="ring", b="axle")]


def test_deleting_the_design_retires_its_threading(handler: SeHandler) -> None:
    handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    ref = handler.store.get_ref(kind="se", id="rotaxane1")
    assert ref is not None
    handler.delete(id="rotaxane1")
    with handler.store.pool.connection() as c:
        live = c.execute(
            "SELECT count(*) FROM se_topology WHERE ref_id = %s AND retired_at IS NULL",
            (ref.id,),
        ).fetchone()
    assert live is not None and live[0] == 0


def test_a_structure_binding_is_storable(handler: SeHandler) -> None:
    """Migration 0007 grew ``bound_kind`` the atomistic realization — an
    atomic block's L3 IS a structure design."""
    handler.put(
        id="rotaxane1",
        text=json.dumps(
            {
                "ops": [
                    *_ROTAXANE_OPS,
                    {"op": "set_mode", "block": "axle", "mode": "atomic"},
                    {
                        "op": "set_binding",
                        "block": "axle",
                        "kind": "structure",
                        "design": "axle-c60",
                    },
                ]
            }
        ),
    )
    ref = handler.store.get_ref(kind="se", id="rotaxane1")
    assert ref is not None
    tree = persist.load_tree(handler.store, ref.id)
    assert (tree.blocks["axle"].bound_kind, tree.blocks["axle"].bound) == (
        "structure",
        "axle-c60",
    )
    # and the coupling reads as agreement, not as a finding
    assert not [
        f for f in se_drc.drc(tree).findings if f.rule == "mode_binding_mismatch"
    ]


# ── the rendered surface ─────────────────────────────────────────────────


def test_the_topology_view_shows_threading_and_dof_together(
    handler: SeHandler,
) -> None:
    handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    body = handler.get(id="rotaxane1", view="topology").body
    assert "ring threaded through axle" in body
    assert "rotational" in body
    assert "head, tail" in body


def test_the_topology_view_says_none_rather_than_nothing(handler: SeHandler) -> None:
    handler.put(
        id="plain1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "plate"},
                ]
            }
        ),
    )
    body = handler.get(id="plain1", view="topology").body
    assert body.count("(none)") == 2


def test_the_tree_line_marks_a_declared_dof(handler: SeHandler) -> None:
    handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    body = handler.get(id="rotaxane1").body
    axle_line = next(
        line for line in body.splitlines() if line.strip().startswith("- axle")
    )
    assert "[rot]" in axle_line


def test_the_ports_view_shows_the_chemistry_columns_when_filled(
    handler: SeHandler,
) -> None:
    handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    body = handler.get(id="rotaxane1", view="ports").body
    assert "expected" in body
    assert "C sp2" in body


def test_a_design_with_no_chemistry_gets_no_chemistry_columns(
    handler: SeHandler,
) -> None:
    """Mode-scoped help: a frame of bolted extrusions must not grow two
    columns of dashes it can never fill."""
    handler.put(
        id="frame1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "plate", "envelope": "box:w1d1h0.01"},
                    {"op": "add_port", "block": "plate", "name": "p"},
                ]
            }
        ),
    )
    body = handler.get(id="frame1", view="ports").body
    assert "expected" not in body
    assert "bound" not in body


def test_the_block_record_shows_dof_threading_and_the_bond_kind(
    handler: SeHandler,
) -> None:
    handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    body = handler.get(id="rotaxane1", view="block", args={"name": "axle"}).body
    assert '"kind": "rotational"' in body
    assert "axle threaded through" in body or "ring threaded through axle" in body
    assert "bond" in body


def test_a_plain_block_record_carries_no_atomic_lines(handler: SeHandler) -> None:
    handler.put(
        id="frame1",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "plate", "envelope": "box:w1d1h0.01"}]}
        ),
    )
    body = handler.get(id="frame1", view="block", args={"name": "plate"}).body
    assert "dof:" not in body
    assert "## threading" not in body


def test_topology_is_in_the_kinds_declared_views_and_the_unknown_view_hint(
    handler: SeHandler,
) -> None:
    from precis.errors import BadInput

    assert "topology" in SeHandler.spec.views
    handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    with pytest.raises(BadInput) as exc:
        handler.get(id="rotaxane1", view="nope")
    assert "view='topology'" in str(exc.value.next or "")
