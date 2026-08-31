"""precis_nm `nm` kind — slice 3 round 1 (plugin skeleton + block tree,
docs/backlog/nm-kind.md "Slice 3 design"). Ports/topology/clearance/
bind_structure are unwired this round; only add_block/instance_block/
set_pose/remove_block + the tree TOC + search are exercised here.

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
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.store import Store
from precis_nm.handler import NmHandler, _render_tree
from precis_nm.ops import BlockNode, BlockTree

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
