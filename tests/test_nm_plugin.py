"""precis_nm `nm` kind — slice 3 (plugin skeleton + block tree + ports +
connects + envelope clearance + bind_structure + topology,
docs/backlog/nm-kind.md "Slice 3 design").

The shared test DB template carries only core migrations (``tests/
conftest.py``'s ``_initialise_test_db``), so this module seeds the plugin's
own migration directly — same fixture shape as ``test_route_plugin.py``'s
``route_store`` / ``test_estimate_plugin.py``'s ``handler``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import precis_nm
import precis_nm.validate as nm_validate
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis_nm import persist
from precis_nm.handler import NmHandler, _render_clearance, _render_tree
from precis_nm.ops import BlockNode, BlockTree, ConnectSpec, PortSpec, ThreadingSpec

_MIGRATIONS_DIR = Path(precis_nm.__file__).parent / "migrations"

_TREE = json.dumps(
    {
        "description": "a rotaxane axle with a threaded crown macrocycle",
        "ops": [
            {
                "op": "add_block",
                "name": "axle",
                "envelope": "cyl:r2h20",
                "desc": "the threading rod",
            },
            {
                "op": "add_block",
                "name": "hub",
                "parent": "axle",
                "envelope": "sphere:r3",
                "use": "stopper",
            },
            {"op": "add_block", "name": "rim", "parent": "hub", "pose": [0, 0, 5]},
        ],
    }
)


@pytest.fixture
def handler(hub: Hub, store: Store) -> NmHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return NmHandler(hub=hub)


def _indent_of(body: str, name: str) -> int:
    """The leading-space depth of the tree line naming block ``name``."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:].split("  ")[0] == name:
            return len(line) - len(line.lstrip())
    raise AssertionError(f"block {name!r} not found in:\n{body}")


# ── dark flag gates the registry only, not direct construction ──────────


def test_direct_construction_ignores_dark_flag(hub: Hub, store: Store) -> None:
    # No PRECIS_NM_ENABLED set anywhere in this test — matches
    # test_estimate_plugin.py's `EstimateHandler(hub=Hub(store=store))`
    # no-exception assertion for the same dark-ship shape.
    NmHandler(hub=hub)


# ── create + tree view ───────────────────────────────────────────────────


def test_create_design_with_three_level_tree(handler: NmHandler) -> None:
    resp = handler.put(id="rotax1", text=_TREE)
    assert "created" in resp.body
    assert "axle" in resp.body and "hub" in resp.body and "rim" in resp.body


def test_tree_view_renders_nesting(handler: NmHandler) -> None:
    handler.put(id="rotax1", text=_TREE)
    toc = handler.get(id="rotax1")
    axle_depth = _indent_of(toc.body, "axle")
    hub_depth = _indent_of(toc.body, "hub")
    rim_depth = _indent_of(toc.body, "rim")
    assert hub_depth > axle_depth
    assert rim_depth > hub_depth
    assert "the threading rod" in toc.body  # desc shows on the tree line
    assert "a rotaxane axle with a threaded crown macrocycle" in toc.body


def test_get_lists_designs(handler: NmHandler) -> None:
    handler.put(id="rotax1", text=_TREE)
    listing = handler.get()
    assert "rotax1" in listing.body


def test_get_block_view(handler: NmHandler) -> None:
    handler.put(id="rotax1", text=_TREE)
    block = handler.get(id="rotax1", view="block", args={"name": "hub"})
    assert "parent: axle" in block.body
    assert "envelope: sphere:r3" in block.body
    assert "use: stopper" in block.body


def test_get_block_view_unknown_name_raises(handler: NmHandler) -> None:
    handler.put(id="rotax1", text=_TREE)
    with pytest.raises(NotFound, match="no such block"):
        handler.get(id="rotax1", view="block", args={"name": "ghost"})


# ── validation ───────────────────────────────────────────────────────────


def test_duplicate_name_per_design_rejected(handler: NmHandler) -> None:
    ops = json.dumps(
        {
            "ops": [
                {"op": "add_block", "name": "a"},
                {"op": "add_block", "name": "a"},
            ]
        }
    )
    with pytest.raises(BadInput, match="duplicate"):
        handler.put(id="dup1", text=ops)


def test_unknown_parent_rejected(handler: NmHandler) -> None:
    ops = json.dumps({"ops": [{"op": "add_block", "name": "a", "parent": "ghost"}]})
    with pytest.raises(BadInput, match="no such block"):
        handler.put(id="badparent1", text=ops)


def test_envelope_good_config_accepted(handler: NmHandler) -> None:
    ops = json.dumps(
        {"ops": [{"op": "add_block", "name": "a", "envelope": "cyl:r5h2"}]}
    )
    resp = handler.put(id="env1", text=ops)
    assert "env=cyl:r5h2" in resp.body


def test_envelope_bad_config_names_valid_shapes(handler: NmHandler) -> None:
    ops = json.dumps({"ops": [{"op": "add_block", "name": "a", "envelope": "blob:x"}]})
    with pytest.raises(BadInput) as excinfo:
        handler.put(id="env2", text=ops)
    msg = str(excinfo.value)
    assert "blob" in msg
    assert "cyl" in msg and "box" in msg  # DslError names the known shapes


# ── instance_block / remove_block ────────────────────────────────────────

_TEMPLATE_OPS = [
    {
        "op": "add_block",
        "name": "sugar",
        "envelope": "sphere:r2",
        "desc": "one sugar unit",
    },
    {"op": "add_block", "name": "ring_atom", "parent": "sugar"},
]


def test_instance_block_marks_and_resolves_subtree_at_read_time(
    handler: NmHandler,
) -> None:
    ops = _TEMPLATE_OPS + [
        {
            "op": "instance_block",
            "name": "sugar2",
            "template": "sugar",
            "pose": [5, 0, 0],
        }
    ]
    resp = handler.put(id="crown1", text=json.dumps({"ops": ops}))
    assert "instance of sugar" in resp.body
    # the instance's subtree is the template's, resolved here — not copied:
    # ring_atom shows nested under BOTH the template and the instance.
    toc = handler.get(id="crown1")
    lines = [ln for ln in toc.body.splitlines() if "ring_atom" in ln]
    assert len(lines) == 2

    block = handler.get(id="crown1", view="block", args={"name": "sugar2"})
    assert "instance of: sugar" in block.body


def test_remove_block_refuses_when_template_in_use(handler: NmHandler) -> None:
    ops = _TEMPLATE_OPS + [
        {"op": "instance_block", "name": "sugar2", "template": "sugar"}
    ]
    handler.put(id="crown1", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="sugar2"):
        handler.edit(id="crown1", ops=[{"op": "remove_block", "block": "sugar"}])


def test_instance_of_instance_rejected(handler: NmHandler) -> None:
    ops = _TEMPLATE_OPS + [
        {"op": "instance_block", "name": "sugar2", "template": "sugar"},
        {"op": "instance_block", "name": "sugar3", "template": "sugar2"},
    ]
    with pytest.raises(BadInput, match="itself an instance"):
        handler.put(id="crown2", text=json.dumps({"ops": ops}))


def test_instance_cannot_nest_under_its_own_template(handler: NmHandler) -> None:
    ops = _TEMPLATE_OPS + [
        {
            "op": "instance_block",
            "name": "sugar2",
            "template": "sugar",
            "parent": "ring_atom",
        }
    ]
    with pytest.raises(BadInput, match="descendants"):
        handler.put(id="crown3", text=json.dumps({"ops": ops}))


def test_instance_block_rejects_template_metadata(handler: NmHandler) -> None:
    for key, value in (
        ("envelope", "cyl:r1h1"),
        ("desc", "a copy"),
        ("use", "spacer"),
        ("dof", {"kind": "rotational"}),
    ):
        ops = [
            {"op": "add_block", "name": "a", "envelope": "sphere:r1"},
            {"op": "instance_block", "name": "b", "template": "a", key: value},
        ]
        with pytest.raises(BadInput, match=key):
            handler.put(id="reject1", text=json.dumps({"ops": ops}))


def test_instance_block_indirect_two_way_cycle_rejected(handler: NmHandler) -> None:
    # The exact repro: two roots that instance each other. Neither local
    # guard (instance-of-instance, nest-under-own-template) sees this —
    # only the expansion-graph search does.
    ops = [
        {"op": "add_block", "name": "A"},
        {"op": "add_block", "name": "B"},
        {"op": "instance_block", "name": "A_in_B", "template": "A", "parent": "B"},
        {"op": "instance_block", "name": "B_in_A", "template": "B", "parent": "A"},
    ]
    with pytest.raises(BadInput, match="instance cycle"):
        handler.put(id="cyclic1", text=json.dumps({"ops": ops}))


def test_instance_block_indirect_three_way_cycle_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "A"},
        {"op": "add_block", "name": "B"},
        {"op": "add_block", "name": "C"},
        {"op": "instance_block", "name": "A_in_B", "template": "A", "parent": "B"},
        {"op": "instance_block", "name": "B_in_C", "template": "B", "parent": "C"},
        {"op": "instance_block", "name": "C_in_A", "template": "C", "parent": "A"},
    ]
    with pytest.raises(BadInput, match="instance cycle") as excinfo:
        handler.put(id="cyclic2", text=json.dumps({"ops": ops}))
    msg = str(excinfo.value)
    assert "A" in msg and "B" in msg and "C" in msg


def test_render_tree_terminates_and_warns_on_injected_cycle() -> None:
    # Defense in depth: build a cyclic tree directly (bypassing ops.py's
    # validation entirely) and confirm the renderer still terminates and
    # marks the cycle instead of a RecursionError.
    tree = BlockTree()
    tree.blocks["A"] = BlockNode(name="A", parent=None, template=None)
    tree.blocks["B"] = BlockNode(name="B", parent=None, template=None)
    tree.blocks["A_in_B"] = BlockNode(name="A_in_B", parent="B", template="A")
    tree.blocks["B_in_A"] = BlockNode(name="B_in_A", parent="A", template="B")

    body = _render_tree(tree, "cyclic", "")

    assert "⚠" in body
    assert "instance cycle" in body


# ── set_pose / edit / re-put / delete ───────────────────────────────────


def test_set_pose_round_trips(handler: NmHandler) -> None:
    handler.put(id="rotax1", text=_TREE)
    handler.edit(
        id="rotax1",
        ops=[{"op": "set_pose", "block": "hub", "pose": [1, 2, 3], "rot": [0, 90, 0]}],
    )
    block = handler.get(id="rotax1", view="block", args={"name": "hub"})
    assert "pose: [1, 2, 3]" in block.body
    assert "rot: [0, 90, 0]" in block.body


def test_reput_replaces_old_blocks(handler: NmHandler, store: Store) -> None:
    handler.put(id="rotax1", text=_TREE)
    ref = store.get_ref(kind="nm", id="rotax1")
    assert ref is not None
    with store.pool.connection() as c:
        before = c.execute(
            "SELECT count(*) FROM nm_blocks WHERE ref_id = %s", (ref.id,)
        ).fetchone()
        assert before is not None and before[0] == 3

    handler.put(
        id="rotax1",
        text=json.dumps({"ops": [{"op": "add_block", "name": "solo"}]}),
    )
    with store.pool.connection() as c:
        live = c.execute(
            "SELECT count(*) FROM nm_blocks WHERE ref_id = %s AND retired_at IS NULL",
            (ref.id,),
        ).fetchone()
        retired = c.execute(
            "SELECT count(*) FROM nm_blocks "
            "WHERE ref_id = %s AND retired_at IS NOT NULL",
            (ref.id,),
        ).fetchone()
        assert live is not None and live[0] == 1
        assert retired is not None and retired[0] == 3
    toc = handler.get(id="rotax1")
    assert "solo" in toc.body and "axle" not in toc.body


def test_soft_delete_retires_ref_and_blocks(handler: NmHandler, store: Store) -> None:
    handler.put(id="rotax1", text=_TREE)
    resp = handler.delete(id="rotax1")
    assert "retired" in resp.body and "3 block" in resp.body
    with pytest.raises(NotFound):
        handler.get(id="rotax1")
    assert store.get_ref(kind="nm", id="rotax1") is None


def test_edit_unknown_design_raises(handler: NmHandler) -> None:
    with pytest.raises(NotFound):
        handler.edit(id="ghost", ops=[{"op": "add_block", "name": "a"}])


# ── search ────────────────────────────────────────────────────────────


def test_search_finds_design_by_description_word(handler: NmHandler) -> None:
    handler.put(id="rotax1", text=_TREE)
    resp = handler.search(q="rotaxane")
    assert "rotax1" in resp.body


def test_search_requires_q(handler: NmHandler) -> None:
    with pytest.raises(BadInput):
        handler.search()


# ── round 2: ports + connects + validate ─────────────────────────────────
# docs/backlog/nm-kind.md "Slice 3 design" / "Round-2 constraint" /
# "Transferred from pcb-component-model.md". Ports/connects persist in
# lockstep with the block tree (persist.py's module docstring) — the
# id-rebuild landmine tests below exercise that directly.


def test_migration_0002_creates_connects_table(
    handler: NmHandler, store: Store
) -> None:
    with store.pool.connection() as c:
        row = c.execute("SELECT to_regclass('public.nm_connects')").fetchone()
    assert row is not None and row[0] is not None


_BOND_TREE_OPS: list[dict[str, object]] = [
    {"op": "add_block", "name": "a", "envelope": "sphere:r2"},
    {"op": "add_block", "name": "b", "envelope": "sphere:r2"},
    {"op": "add_port", "block": "a", "name": "p1", "roles": ["covalent"]},
    {"op": "add_port", "block": "b", "name": "p1", "roles": ["covalent"]},
    {"op": "connect", "a": "a.p1", "b": "b.p1"},
]


def test_add_port_on_instance_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "sphere:r2"},
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
        {"op": "add_port", "block": "inst", "name": "p1", "roles": ["covalent"]},
    ]
    with pytest.raises(BadInput, match="instance"):
        handler.put(id="portinst1", text=json.dumps({"ops": ops}))


def test_duplicate_port_name_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a"},
        {"op": "add_port", "block": "a", "name": "p1", "roles": []},
        {"op": "add_port", "block": "a", "name": "p1", "roles": []},
    ]
    with pytest.raises(BadInput, match="duplicate"):
        handler.put(id="portdup1", text=json.dumps({"ops": ops}))


def test_zero_direction_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a"},
        {
            "op": "add_port",
            "block": "a",
            "name": "p1",
            "roles": [],
            "direction": [0, 0, 0],
        },
    ]
    with pytest.raises(BadInput, match="nonzero"):
        handler.put(id="portzero1", text=json.dumps({"ops": ops}))


def test_connect_round_trips_through_a_second_save(
    handler: NmHandler, store: Store
) -> None:
    # The id-rebuild landmine: save_tree retires+reinserts every nm_blocks
    # row on *every* save, so ports/connects must survive a SECOND save
    # keyed by name, not by the block id minted on the first save.
    handler.put(id="bond1", text=json.dumps({"ops": _BOND_TREE_OPS}))
    handler.edit(id="bond1", ops=[{"op": "add_block", "name": "unrelated"}])

    block_a = handler.get(id="bond1", view="block", args={"name": "a"})
    assert "p1" in block_a.body and "covalent" in block_a.body
    assert "b.p1" in block_a.body  # the connect touching 'a' is listed

    ports = handler.get(id="bond1", view="ports")
    assert "a" in ports.body and "b" in ports.body and "p1" in ports.body

    validation = handler.get(id="bond1", view="validate")
    assert "dangling_connect" not in validation.body
    assert "port_capability" not in validation.body


def test_connect_onto_instance_endpoint_resolves_template_port(
    handler: NmHandler,
) -> None:
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "tmpl", "name": "p1", "roles": ["covalent"]},
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
        {"op": "add_block", "name": "other", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "other", "name": "p1", "roles": ["covalent"]},
        {"op": "connect", "a": "inst.p1", "b": "other.p1"},
    ]
    resp = handler.put(id="instconn1", text=json.dumps({"ops": ops}))
    assert "created" in resp.body

    inst_block = handler.get(id="instconn1", view="block", args={"name": "inst"})
    assert "p1" in inst_block.body  # resolved via the template


def test_connect_missing_role_rejected_naming_actual_roles(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a"},
        {"op": "add_block", "name": "b"},
        {"op": "add_port", "block": "a", "name": "p1", "roles": ["coordination"]},
        {"op": "add_port", "block": "b", "name": "p1", "roles": ["coordination"]},
        {"op": "connect", "a": "a.p1", "b": "b.p1"},  # default kind='bond'
    ]
    with pytest.raises(BadInput) as excinfo:
        handler.put(id="capgate1", text=json.dumps({"ops": ops}))
    msg = str(excinfo.value)
    assert "covalent" in msg
    assert "coordination" in msg


def test_connect_objectives_role_override_gates_on_named_role(
    handler: NmHandler,
) -> None:
    ops = [
        {"op": "add_block", "name": "a"},
        {"op": "add_block", "name": "b"},
        {"op": "add_port", "block": "a", "name": "p1", "roles": ["pi_stack"]},
        {"op": "add_port", "block": "b", "name": "p1", "roles": ["pi_stack"]},
        {
            "op": "connect",
            "a": "a.p1",
            "b": "b.p1",
            "objectives": {"role": "pi_stack"},
        },
    ]
    resp = handler.put(id="capgate2", text=json.dumps({"ops": ops}))
    assert "created" in resp.body


def test_disconnect_removes_the_connect(handler: NmHandler) -> None:
    handler.put(id="disc1", text=json.dumps({"ops": _BOND_TREE_OPS}))
    handler.edit(id="disc1", ops=[{"op": "disconnect", "a": "a.p1", "b": "b.p1"}])
    block_a = handler.get(id="disc1", view="block", args={"name": "a"})
    assert "b.p1" not in block_a.body


def test_disconnect_missing_pair_raises(handler: NmHandler) -> None:
    handler.put(
        id="disc2",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "a"},
                    {"op": "add_block", "name": "b"},
                ]
            }
        ),
    )
    with pytest.raises(BadInput, match="no such connect"):
        handler.edit(id="disc2", ops=[{"op": "disconnect", "a": "a.p1", "b": "b.p1"}])


def test_remove_port_with_live_connect_refused(handler: NmHandler) -> None:
    handler.put(id="rmport1", text=json.dumps({"ops": _BOND_TREE_OPS}))
    with pytest.raises(BadInput, match="a.p1"):
        handler.edit(
            id="rmport1", ops=[{"op": "remove_port", "block": "a", "name": "p1"}]
        )


def test_remove_port_blocked_by_instance_mediated_connect(handler: NmHandler) -> None:
    # The reviewer's exact repro: a connect stored against an INSTANCE's
    # block name resolves to the TEMPLATE's port at connect time
    # (effective_ports) — remove_port on the template must still see it.
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "tmpl", "name": "p1", "roles": ["covalent"]},
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
        {"op": "add_block", "name": "other", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "other", "name": "p1", "roles": ["covalent"]},
        {"op": "connect", "a": "inst.p1", "b": "other.p1"},
    ]
    handler.put(id="rmport2", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="inst.p1") as excinfo:
        handler.edit(
            id="rmport2", ops=[{"op": "remove_port", "block": "tmpl", "name": "p1"}]
        )
    assert "inst.p1" in str(excinfo.value)


def test_add_port_dotted_name_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a"},
        {"op": "add_port", "block": "a", "name": "foo.bar", "roles": []},
    ]
    with pytest.raises(BadInput, match=r"\."):
        handler.put(id="dotport1", text=json.dumps({"ops": ops}))


# ── remove_block auto-retires touching connects (vacancy precedent) ─────


def test_remove_block_drops_touching_connects(handler: NmHandler) -> None:
    handler.put(id="rmblk1", text=json.dumps({"ops": _BOND_TREE_OPS}))
    handler.edit(id="rmblk1", ops=[{"op": "remove_block", "block": "b"}])
    validation = handler.get(id="rmblk1", view="validate")
    assert "dangling_connect" not in validation.body
    remaining = handler.get(id="rmblk1", view="block", args={"name": "a"})
    assert "b.p1" not in remaining.body


def test_remove_block_on_instance_endpoint_drops_touching_connects(
    handler: NmHandler,
) -> None:
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "tmpl", "name": "p1", "roles": ["covalent"]},
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
        {"op": "add_block", "name": "other", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "other", "name": "p1", "roles": ["covalent"]},
        {"op": "connect", "a": "inst.p1", "b": "other.p1"},
    ]
    handler.put(id="rmblk2", text=json.dumps({"ops": ops}))
    # removing the INSTANCE (not the template) — the connect's stored
    # endpoint is the instance's own name, so its removal is what must
    # trigger the drop.
    handler.edit(id="rmblk2", ops=[{"op": "remove_block", "block": "inst"}])
    validation = handler.get(id="rmblk2", view="validate")
    assert "dangling_connect" not in validation.body


# ── instance envelope inheritance (rendering) ────────────────────────────


def test_instance_tree_line_shows_inherited_envelope(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "cyl:r5h2"},
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
    ]
    handler.put(id="envinherit1", text=json.dumps({"ops": ops}))
    toc = handler.get(id="envinherit1")
    inst_line = next(ln for ln in toc.body.splitlines() if "inst" in ln and "- " in ln)
    assert "cyl:r5h2" in inst_line
    assert "from tmpl" in inst_line

    block = handler.get(id="envinherit1", view="block", args={"name": "inst"})
    assert "cyl:r5h2" in block.body
    assert "from tmpl" in block.body


# ── validate view ─────────────────────────────────────────────────────


def test_validate_view_clean_design(handler: NmHandler) -> None:
    handler.put(id="clean1", text=json.dumps({"ops": _BOND_TREE_OPS}))
    resp = handler.get(id="clean1", view="validate")
    assert "no validator findings" in resp.body


def test_validate_unconnected_port_warns_then_clears(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "a", "name": "p1", "roles": ["covalent"]},
    ]
    handler.put(id="loose1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="loose1", view="validate")
    assert "unconnected_port" in resp.body
    assert "a.p1" in resp.body

    handler.edit(
        id="loose1",
        ops=[
            {"op": "add_block", "name": "b", "envelope": "sphere:r2"},
            {"op": "add_port", "block": "b", "name": "p1", "roles": ["covalent"]},
            {"op": "connect", "a": "a.p1", "b": "b.p1"},
        ],
    )
    resp2 = handler.get(id="loose1", view="validate")
    assert "unconnected_port" not in resp2.body


def test_validate_blocks_without_envelope_warns(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a"},  # no envelope
        {"op": "add_port", "block": "a", "name": "p1", "roles": ["covalent"]},
    ]
    handler.put(id="noenv1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="noenv1", view="validate")
    assert "blocks_without_envelope" in resp.body


def test_validate_dangling_connect_from_hand_corrupted_tree() -> None:
    # Same shape as test_render_tree_terminates_and_warns_on_injected_cycle:
    # build a tree directly, bypassing ops.py's validation entirely — a
    # connect whose block was hand-removed after the fact (or corrupted
    # data / a future bug elsewhere) must still be caught here, loudly.
    tree = BlockTree()
    tree.blocks["a"] = BlockNode(
        name="a", ports={"p1": PortSpec(name="p1", roles=["covalent"])}
    )
    tree.connects.append(
        ConnectSpec(a_block="a", a_port="p1", b_block="ghost", b_port="p1")
    )
    findings = nm_validate.validate(tree)
    rules = {f.rule for f in findings}
    assert "dangling_connect" in rules
    dangling = next(f for f in findings if f.rule == "dangling_connect")
    assert dangling.severity == "error"
    assert "ghost" in dangling.detail


def test_validate_port_capability_defense_in_depth() -> None:
    # A stored connect that violates the capability gate despite never
    # having gone through ops.py's _op_connect (which would have rejected
    # it) — the same "op-time gate + read-time re-check" pattern as the
    # instance-cycle render guard.
    tree = BlockTree()
    tree.blocks["a"] = BlockNode(name="a", ports={"p1": PortSpec(name="p1", roles=[])})
    tree.blocks["b"] = BlockNode(name="b", ports={"p1": PortSpec(name="p1", roles=[])})
    tree.connects.append(
        ConnectSpec(a_block="a", a_port="p1", b_block="b", b_port="p1", kind="bond")
    )
    findings = nm_validate.validate(tree)
    rules = {f.rule for f in findings}
    assert "port_capability" in rules


# ── ports view ────────────────────────────────────────────────────────


def test_ports_view_renders(handler: NmHandler) -> None:
    handler.put(id="portsview1", text=json.dumps({"ops": _BOND_TREE_OPS}))
    resp = handler.get(id="portsview1", view="ports")
    assert "a" in resp.body and "b" in resp.body
    assert "p1" in resp.body
    assert "covalent" in resp.body


def test_ports_view_marks_instance_rows(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "tmpl", "name": "p1", "roles": ["covalent"]},
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
    ]
    handler.put(id="portsview2", text=json.dumps({"ops": ops}))
    resp = handler.get(id="portsview2", view="ports")
    assert "via" in resp.body and "tmpl" in resp.body


def test_ports_view_empty_design(handler: NmHandler) -> None:
    handler.put(
        id="portsview3", text=json.dumps({"ops": [{"op": "add_block", "name": "a"}]})
    )
    resp = handler.get(id="portsview3", view="ports")
    assert "no ports" in resp.body.lower()


def test_tree_view_shows_port_count_suffix(handler: NmHandler) -> None:
    handler.put(id="portcount1", text=json.dumps({"ops": _BOND_TREE_OPS}))
    toc = handler.get(id="portcount1")
    assert "[1 port]" in toc.body


# ── round 3: envelope clearance (cad kernel) ─────────────────────────────
# docs/backlog/nm-kind.md "Slice 3 design" — get(view='clearance',
# args={'a':..., 'b':...}) builds a two-component cad Design in memory
# (each block's effective envelope, placed at its pose) and reuses
# precis.cad.relate.clearance (the exact-sign CSG SDF, Å).


def test_clearance_separated_boxes_positive_gap(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "box:w2d2h2"},
        {
            "op": "add_block",
            "name": "b",
            "envelope": "box:w2d2h2",
            "pose": [10, 0, 0],
        },
    ]
    handler.put(id="clr1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="clr1", view="clearance", args={"a": "a", "b": "b"})
    # boxes are 2 wide (±1 from center); centers 10 apart → gap = 10-1-1 = 8
    gap_line = next(ln for ln in resp.body.splitlines() if ln.startswith("gap:"))
    gap_val = float(gap_line.split()[1])
    assert gap_val == pytest.approx(8.0, abs=1e-2)
    assert "(clear)" in resp.body


def test_clearance_overlapping_boxes_negative_gap(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "box:w4d4h4"},
        {
            "op": "add_block",
            "name": "b",
            "envelope": "box:w4d4h4",
            "pose": [1, 0, 0],
        },
    ]
    handler.put(id="clr2", text=json.dumps({"ops": ops}))
    resp = handler.get(id="clr2", view="clearance", args={"a": "a", "b": "b"})
    assert "interference" in resp.body
    gap_line = next(ln for ln in resp.body.splitlines() if ln.startswith("gap:"))
    gap_val = float(gap_line.split()[1])
    assert gap_val < 0


def test_clearance_missing_envelope_raises_badinput(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "box:w2d2h2"},
        {"op": "add_block", "name": "b"},  # no envelope
    ]
    handler.put(id="clr3", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="no effective envelope"):
        handler.get(id="clr3", view="clearance", args={"a": "a", "b": "b"})


def test_clearance_instance_uses_template_envelope(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "box:w2d2h2"},
        {
            "op": "instance_block",
            "name": "inst",
            "template": "tmpl",
            "pose": [10, 0, 0],
        },
        {"op": "add_block", "name": "other", "envelope": "box:w2d2h2"},
    ]
    handler.put(id="clr4", text=json.dumps({"ops": ops}))
    resp = handler.get(id="clr4", view="clearance", args={"a": "inst", "b": "other"})
    gap_line = next(ln for ln in resp.body.splitlines() if ln.startswith("gap:"))
    gap_val = float(gap_line.split()[1])
    assert gap_val == pytest.approx(8.0, abs=1e-2)


def test_clearance_children_note_appears(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "box:w2d2h2"},
        {"op": "add_block", "name": "child", "parent": "a", "envelope": "sphere:r1"},
        {
            "op": "add_block",
            "name": "b",
            "envelope": "box:w2d2h2",
            "pose": [10, 0, 0],
        },
    ]
    handler.put(id="clr5", text=json.dumps({"ops": ops}))
    resp = handler.get(id="clr5", view="clearance", args={"a": "a", "b": "b"})
    assert "note:" in resp.body
    assert "'a'" in resp.body and "child" in resp.body


def test_clearance_unknown_block_raises(handler: NmHandler) -> None:
    handler.put(id="clr6", text=json.dumps({"ops": [{"op": "add_block", "name": "a"}]}))
    with pytest.raises(NotFound):
        handler.get(id="clr6", view="clearance", args={"a": "a", "b": "ghost"})


def test_clearance_same_block_rejected(handler: NmHandler) -> None:
    handler.put(
        id="clr7",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "a", "envelope": "sphere:r1"}]}
        ),
    )
    with pytest.raises(BadInput, match="must differ"):
        handler.get(id="clr7", view="clearance", args={"a": "a", "b": "a"})


def test_clearance_hand_corrupted_envelope_raises_badinput() -> None:
    # A stored-but-now-invalid envelope (bypassing ops.py's write-time
    # cad_dsl.parse validation entirely — hand-corrupted data, or a future
    # bug elsewhere) must surface as a legible BadInput, not a raw
    # traceback from the cad kernel's DslError.
    tree = BlockTree()
    tree.blocks["a"] = BlockNode(name="a", envelope="cyl:r2h5")
    tree.blocks["b"] = BlockNode(name="b", envelope="not-a-real-shape")
    with pytest.raises(BadInput, match="invalid envelope"):
        _render_clearance(tree, {"a": "a", "b": "b"})


# ── round 3: bind_structure / unbind_structure ───────────────────────────


@pytest.fixture
def structure(store: Store) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


def _mol_cell_payload(size: float = 20.0) -> dict[str, object]:
    return {"a": size, "b": size, "c": size, "pbc": [False, False, False]}


def _make_structure(structure: StructureHandler, slug: str) -> tuple[str, str]:
    """A tiny two-carbon structure design; returns its two atom labels."""
    structure.put(
        id=slug,
        text=json.dumps(
            {
                "cell": _mol_cell_payload(),
                "ops": [
                    {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0]},
                    {"op": "add_atom", "element": "N", "cart": [1.3, 0.0, 0.0]},
                ],
            }
        ),
    )
    return "aC1", "aN1"


def test_bind_structure_happy_path(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label, n_label = _make_structure(structure, "frag1")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "hub", "name": "p1", "expected_element": "C"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag1",
            "ports": {"p1": c_label},
        },
    ]
    resp = handler.put(id="bind1", text=json.dumps({"ops": ops}))
    assert "bound block 'hub' to structure 'frag1'" in resp.body
    assert f"p1→{c_label}" in resp.body

    block = handler.get(id="bind1", view="block", args={"name": "hub"})
    assert "bound_design: frag1" in block.body
    assert f"frag1:{c_label}" in block.body

    toc = handler.get(id="bind1")
    assert "⇒ st:frag1" in toc.body


def test_bind_structure_port_atom_map_persists_across_second_save(
    handler: NmHandler, structure: StructureHandler
) -> None:
    # The landmine test (mirrors test_connect_round_trips_through_a_second_
    # save): nm_blocks.id is rebuilt every save_tree, so the binding must
    # survive a second save keyed by name/design/atom, not by row id.
    c_label, _n = _make_structure(structure, "frag2")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag2",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="bind2", text=json.dumps({"ops": ops}))
    handler.edit(id="bind2", ops=[{"op": "add_block", "name": "unrelated"}])

    block = handler.get(id="bind2", view="block", args={"name": "hub"})
    assert "bound_design: frag2" in block.body
    assert f"frag2:{c_label}" in block.body


def test_bind_structure_rebind_different_design_clears_stale_ports(
    handler: NmHandler, structure: StructureHandler, store: Store
) -> None:
    # The reviewer's bug: rebinding to a DIFFERENT design used to leave
    # other ports' bindings pointing at the OLD design's atom labels. Since
    # structure's label minting restarts at "aC1" for every fresh design,
    # fragB genuinely has an atom labelled the same as fragA's — so a
    # stale binding wouldn't even raise, it would silently resolve to the
    # WRONG atom. This test relies on exactly that label collision.
    c_a, _n_a = _make_structure(structure, "fragA")
    c_b, _n_b = _make_structure(structure, "fragB")
    assert c_a == c_b  # the label-collision precondition this test needs

    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {"op": "add_port", "block": "hub", "name": "p2"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "fragA",
            "ports": {"p1": c_a},
        },
    ]
    handler.put(id="bind10", text=json.dumps({"ops": ops}))
    handler.edit(
        id="bind10",
        ops=[
            {
                "op": "bind_structure",
                "block": "hub",
                "design": "fragB",
                "ports": {"p2": c_b},
            }
        ],
    )

    ref = store.get_ref(kind="nm", id="bind10")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    hub = tree.blocks["hub"]
    assert hub.bound_design == "fragB"
    assert hub.ports["p2"].bound_design == "fragB"
    assert hub.ports["p2"].bound_atom == c_b
    # p1's stale fragA binding is cleared, not left pointing at a label
    # that (by minting convention) collides with a real fragB atom.
    assert hub.ports["p1"].bound_design is None
    assert hub.ports["p1"].bound_atom is None

    resp = handler.get(id="bind10", view="validate")
    assert "dangling_binding" not in resp.body
    assert "binding_element_mismatch" not in resp.body


def test_bind_structure_same_design_rebind_is_incremental(
    handler: NmHandler, structure: StructureHandler, store: Store
) -> None:
    c_label, n_label = _make_structure(structure, "fragC")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {"op": "add_port", "block": "hub", "name": "p2"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "fragC",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="bind11", text=json.dumps({"ops": ops}))
    handler.edit(
        id="bind11",
        ops=[
            {
                "op": "bind_structure",
                "block": "hub",
                "design": "fragC",
                "ports": {"p2": n_label},
            }
        ],
    )
    ref = store.get_ref(kind="nm", id="bind11")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    hub = tree.blocks["hub"]
    # a same-design rebind is incremental — the earlier call's mapping
    # survives a later call that only maps additional ports.
    assert hub.ports["p1"].bound_design == "fragC"
    assert hub.ports["p1"].bound_atom == c_label
    assert hub.ports["p2"].bound_design == "fragC"
    assert hub.ports["p2"].bound_atom == n_label


def test_bind_structure_expected_element_mismatch_rejected(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label, _n = _make_structure(structure, "frag3")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "hub", "name": "p1", "expected_element": "O"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag3",
            "ports": {"p1": c_label},
        },
    ]
    with pytest.raises(BadInput, match="expects element"):
        handler.put(id="bind3", text=json.dumps({"ops": ops}))


def test_bind_structure_unknown_design_raises(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {"op": "bind_structure", "block": "hub", "design": "no-such-design"},
    ]
    with pytest.raises(NotFound, match="no structure design"):
        handler.put(id="bind4", text=json.dumps({"ops": ops}))


def test_bind_structure_unknown_port_raises(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label, _n = _make_structure(structure, "frag5")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag5",
            "ports": {"ghost": c_label},
        },
    ]
    with pytest.raises(NotFound, match="no such port"):
        handler.put(id="bind5", text=json.dumps({"ops": ops}))


def test_bind_structure_unknown_atom_raises(
    handler: NmHandler, structure: StructureHandler
) -> None:
    _c, _n = _make_structure(structure, "frag6")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag6",
            "ports": {"p1": "ghost_atom"},
        },
    ]
    with pytest.raises(NotFound, match="no such atom"):
        handler.put(id="bind6", text=json.dumps({"ops": ops}))


def test_bind_structure_on_instance_rejected(
    handler: NmHandler, structure: StructureHandler
) -> None:
    _make_structure(structure, "frag7")
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "sphere:r2"},
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
        {"op": "bind_structure", "block": "inst", "design": "frag7"},
    ]
    with pytest.raises(BadInput, match="instance"):
        handler.put(id="bind7", text=json.dumps({"ops": ops}))


def test_unbind_structure_clears(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label, _n = _make_structure(structure, "frag8")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag8",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="bind8", text=json.dumps({"ops": ops}))
    handler.edit(id="bind8", ops=[{"op": "unbind_structure", "block": "hub"}])
    block = handler.get(id="bind8", view="block", args={"name": "hub"})
    assert "bound_design: —" in block.body
    assert "frag8" not in block.body


# ── round 3: topology (threading + dof) ──────────────────────────────────


def test_declare_and_remove_threading(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle", "envelope": "cyl:r2h20"},
        {"op": "add_block", "name": "ring", "envelope": "torus:R5r1"},
        {"op": "declare_threading", "a": "ring", "b": "axle"},
    ]
    handler.put(id="topo1", text=json.dumps({"ops": ops}))
    topo = handler.get(id="topo1", view="topology")
    assert "ring threaded through axle" in topo.body

    handler.edit(id="topo1", ops=[{"op": "remove_threading", "a": "ring", "b": "axle"}])
    topo2 = handler.get(id="topo1", view="topology")
    assert "ring threaded through axle" not in topo2.body


def test_declare_threading_self_rejected(handler: NmHandler) -> None:
    handler.put(
        id="topo2", text=json.dumps({"ops": [{"op": "add_block", "name": "a"}]})
    )
    with pytest.raises(BadInput, match="must differ"):
        handler.edit(id="topo2", ops=[{"op": "declare_threading", "a": "a", "b": "a"}])


def test_declare_threading_duplicate_pair_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a"},
        {"op": "add_block", "name": "b"},
        {"op": "declare_threading", "a": "a", "b": "b"},
    ]
    with pytest.raises(BadInput, match="already declared"):
        handler.put(
            id="topo3",
            text=json.dumps(
                {"ops": [*ops, {"op": "declare_threading", "a": "a", "b": "b"}]}
            ),
        )


def test_declare_threading_mutual_rejected_both_orders(handler: NmHandler) -> None:
    # Mutual threading is physically impossible: a threaded through b AND
    # b threaded through a at once would mean each is inside the other.
    ops_ab = [
        {"op": "add_block", "name": "a"},
        {"op": "add_block", "name": "b"},
        {"op": "declare_threading", "a": "a", "b": "b"},
    ]
    with pytest.raises(BadInput, match="mutual threading is physically impossible"):
        handler.put(
            id="topo3b",
            text=json.dumps(
                {"ops": [*ops_ab, {"op": "declare_threading", "a": "b", "b": "a"}]}
            ),
        )

    ops_ba = [
        {"op": "add_block", "name": "a"},
        {"op": "add_block", "name": "b"},
        {"op": "declare_threading", "a": "b", "b": "a"},
    ]
    with pytest.raises(BadInput, match="mutual threading is physically impossible"):
        handler.put(
            id="topo3c",
            text=json.dumps(
                {"ops": [*ops_ba, {"op": "declare_threading", "a": "a", "b": "b"}]}
            ),
        )


def test_remove_threading_missing_pair_raises(handler: NmHandler) -> None:
    ops = [{"op": "add_block", "name": "a"}, {"op": "add_block", "name": "b"}]
    handler.put(id="topo4", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="no such threading"):
        handler.edit(id="topo4", ops=[{"op": "remove_threading", "a": "a", "b": "b"}])


def test_threading_persists_across_second_save(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle"},
        {"op": "add_block", "name": "ring"},
        {"op": "declare_threading", "a": "ring", "b": "axle"},
    ]
    handler.put(id="topo5", text=json.dumps({"ops": ops}))
    handler.edit(id="topo5", ops=[{"op": "add_block", "name": "unrelated"}])
    topo = handler.get(id="topo5", view="topology")
    assert "ring threaded through axle" in topo.body


def test_declare_dof_and_view(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle", "envelope": "cyl:r2h20"},
        {"op": "add_port", "block": "axle", "name": "p1"},
        {"op": "add_port", "block": "axle", "name": "p2"},
        {
            "op": "declare_dof",
            "block": "axle",
            "kind": "rotational",
            "axis_ports": ["p1", "p2"],
        },
    ]
    handler.put(id="dof1", text=json.dumps({"ops": ops}))
    toc = handler.get(id="dof1")
    assert "[rot]" in toc.body

    block = handler.get(id="dof1", view="block", args={"name": "axle"})
    assert "rotational" in block.body

    topo = handler.get(id="dof1", view="topology")
    assert "axle" in topo.body and "rotational" in topo.body

    handler.edit(id="dof1", ops=[{"op": "clear_dof", "block": "axle"}])
    block2 = handler.get(id="dof1", view="block", args={"name": "axle"})
    assert "dof: —" in block2.body


def test_declare_dof_bad_axis_port_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle"},
        {"op": "add_port", "block": "axle", "name": "p1"},
    ]
    handler.put(id="dof2", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="no such port"):
        handler.edit(
            id="dof2",
            ops=[
                {
                    "op": "declare_dof",
                    "block": "axle",
                    "kind": "rotational",
                    "axis_ports": ["p1", "ghost"],
                }
            ],
        )


def test_declare_dof_bad_kind_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle"},
        {"op": "add_port", "block": "axle", "name": "p1"},
        {"op": "add_port", "block": "axle", "name": "p2"},
    ]
    handler.put(id="dof3", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="rotational"):
        handler.edit(
            id="dof3",
            ops=[
                {
                    "op": "declare_dof",
                    "block": "axle",
                    "kind": "wobbly",
                    "axis_ports": ["p1", "p2"],
                }
            ],
        )


def test_declare_dof_on_instance_rejected(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "tmpl"},
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
    ]
    handler.put(id="dof4", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="instance"):
        handler.edit(
            id="dof4",
            ops=[
                {
                    "op": "declare_dof",
                    "block": "inst",
                    "kind": "rotational",
                    "axis_ports": ["p1", "p2"],
                }
            ],
        )


def test_dof_persists_across_second_save(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle"},
        {"op": "add_port", "block": "axle", "name": "p1"},
        {"op": "add_port", "block": "axle", "name": "p2"},
        {
            "op": "declare_dof",
            "block": "axle",
            "kind": "translational",
            "axis_ports": ["p1", "p2"],
        },
    ]
    handler.put(id="dof5", text=json.dumps({"ops": ops}))
    handler.edit(id="dof5", ops=[{"op": "add_block", "name": "unrelated"}])
    block = handler.get(id="dof5", view="block", args={"name": "axle"})
    assert "translational" in block.body


def test_dof_instance_tree_line_shows_inherited_marker(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "tmpl"},
        {"op": "add_port", "block": "tmpl", "name": "p1"},
        {"op": "add_port", "block": "tmpl", "name": "p2"},
        {
            "op": "declare_dof",
            "block": "tmpl",
            "kind": "rotational",
            "axis_ports": ["p1", "p2"],
        },
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
    ]
    handler.put(id="dof6", text=json.dumps({"ops": ops}))
    toc = handler.get(id="dof6")
    inst_line = next(ln for ln in toc.body.splitlines() if "inst" in ln and "- " in ln)
    assert "[rot]" in inst_line
    assert "from tmpl" in inst_line


def test_remove_port_used_by_dof_refused(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle"},
        {"op": "add_port", "block": "axle", "name": "p1"},
        {"op": "add_port", "block": "axle", "name": "p2"},
        {
            "op": "declare_dof",
            "block": "axle",
            "kind": "rotational",
            "axis_ports": ["p1", "p2"],
        },
    ]
    handler.put(id="dof7", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="declared dof"):
        handler.edit(
            id="dof7", ops=[{"op": "remove_port", "block": "axle", "name": "p1"}]
        )


def test_remove_block_drops_touching_threading(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle"},
        {"op": "add_block", "name": "ring"},
        {"op": "declare_threading", "a": "ring", "b": "axle"},
    ]
    handler.put(id="topo6", text=json.dumps({"ops": ops}))
    handler.edit(id="topo6", ops=[{"op": "remove_block", "block": "ring"}])
    validation = handler.get(id="topo6", view="validate")
    assert "dangling_threading" not in validation.body
    topo = handler.get(id="topo6", view="topology")
    assert "threaded through" not in topo.body


# ── round 3: validate additions ───────────────────────────────────────


def test_validate_threaded_without_envelope_warns(handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle"},  # no envelope
        {"op": "add_block", "name": "ring"},  # no envelope
        {"op": "declare_threading", "a": "ring", "b": "axle"},
    ]
    handler.put(id="topo7", text=json.dumps({"ops": ops}))
    resp = handler.get(id="topo7", view="validate")
    assert "threaded_without_envelope" in resp.body


def test_validate_threaded_without_envelope_clean_via_instance_envelope(
    handler: NmHandler,
) -> None:
    # An instance's own envelope field is always None — threaded_without_
    # envelope must resolve it via the template (effective_envelope), the
    # same rule the tree/block views already apply, not just check the
    # instance's own (always-empty) envelope field.
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "cyl:r2h20"},
        {"op": "instance_block", "name": "axle", "template": "tmpl"},
        {"op": "add_block", "name": "ring", "envelope": "torus:R5r1"},
        {"op": "declare_threading", "a": "ring", "b": "axle"},
    ]
    handler.put(id="topo8", text=json.dumps({"ops": ops}))
    resp = handler.get(id="topo8", view="validate")
    assert "threaded_without_envelope" not in resp.body


def test_validate_dangling_threading_from_hand_corrupted_tree() -> None:
    tree = BlockTree()
    tree.blocks["a"] = BlockNode(name="a")
    tree.threading.append(ThreadingSpec(a="a", b="ghost"))
    findings = nm_validate.validate(tree)
    rules = {f.rule for f in findings}
    assert "dangling_threading" in rules
    finding = next(f for f in findings if f.rule == "dangling_threading")
    assert finding.severity == "error"
    assert "ghost" in finding.detail


def test_validate_binding_element_mismatch_defense_in_depth() -> None:
    tree = BlockTree()
    tree.blocks["hub"] = BlockNode(
        name="hub",
        bound_design="frag",
        ports={
            "p1": PortSpec(
                name="p1",
                expected_element="O",
                bound_design="frag",
                bound_atom="aC1",
            )
        },
    )
    findings = nm_validate.validate(tree, bound_scenes={"frag": {"aC1": "C"}})
    rules = {f.rule for f in findings}
    assert "binding_element_mismatch" in rules
    finding = next(f for f in findings if f.rule == "binding_element_mismatch")
    assert finding.severity == "warn"


def test_validate_dangling_binding_when_scene_slug_unresolved() -> None:
    tree = BlockTree()
    tree.blocks["hub"] = BlockNode(name="hub", bound_design="ghost_design")
    findings = nm_validate.validate(tree, bound_scenes={"ghost_design": None})
    rules = {f.rule for f in findings}
    assert "dangling_binding" in rules
    finding = next(f for f in findings if f.rule == "dangling_binding")
    assert finding.severity == "error"


def test_validate_dangling_binding_when_structure_deleted(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label, _n = _make_structure(structure, "frag9")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag9",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="bind9", text=json.dumps({"ops": ops}))
    clean = handler.get(id="bind9", view="validate")
    assert "dangling_binding" not in clean.body

    structure.delete(id="frag9")
    dirty = handler.get(id="bind9", view="validate")
    assert "dangling_binding" in dirty.body
