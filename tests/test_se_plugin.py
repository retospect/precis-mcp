"""precis_se `se` kind — slices 1-3 (plugin scaffold + L0/L1 block tree +
the L2 invariant tier: joints/measures/loads/stack-up/DRC,
docs/backlog/se-kind.md "Ship order").

The shared test DB template carries only core migrations, so this module
seeds the plugin's own migration directly — same fixture shape as
``test_nm_plugin.py``'s ``handler``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import precis_se
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.store import Store
from precis_se import drc as se_drc
from precis_se import persist
from precis_se.handler import SeHandler, _render_tree
from precis_se.measures import MeasureSpec, stackup
from precis_se.ops import OpError, SeTree, apply_ops, effective_envelope

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: A caster: fork holds a hub; the wheel comes later (suggestive by
#: contract — half-specified is a legal, honest state).
_CASTER = json.dumps(
    {
        "description": "a swivel caster for a cart",
        "ops": [
            {
                "op": "add_block",
                "name": "fork",
                "envelope": "box:w0.04d0.02h0.08",
                "desc": "the load-bearing fork",
            },
            {
                "op": "add_block",
                "name": "hub",
                "parent": "fork",
                "envelope": "cyl:r0.008h0.03",
                "use": "axle seat",
            },
            {"op": "add_block", "name": "cap", "parent": "hub", "pose": [0, 0, 0.02]},
        ],
    }
)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


def _indent_of(body: str, name: str) -> int:
    """The leading-space depth of the tree line naming block ``name``."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:].split("  ")[0] == name:
            return len(line) - len(line.lstrip())
    raise AssertionError(f"block {name!r} not found in:\n{body}")


# ── dark flag gates the registry only, not direct construction ──────────


def test_direct_construction_ignores_dark_flag(hub: Hub) -> None:
    # No PRECIS_SE_ENABLED set anywhere in this test — matches
    # test_nm_plugin.py's no-exception assertion for the same shape.
    SeHandler(hub=hub)


# ── create + tree view ───────────────────────────────────────────────────


def test_create_design_with_three_level_tree(handler: SeHandler) -> None:
    resp = handler.put(id="caster1", text=_CASTER)
    assert "created" in resp.body
    assert "fork" in resp.body and "hub" in resp.body and "cap" in resp.body


def test_tree_view_renders_nesting_and_metre_units(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    toc = handler.get(id="caster1")
    assert "units: metres" in toc.body
    fork_depth = _indent_of(toc.body, "fork")
    hub_depth = _indent_of(toc.body, "hub")
    cap_depth = _indent_of(toc.body, "cap")
    assert hub_depth > fork_depth
    assert cap_depth > hub_depth
    assert "the load-bearing fork" in toc.body  # desc shows on the tree line
    assert "a swivel caster for a cart" in toc.body


def test_get_lists_designs(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    listing = handler.get()
    assert "caster1" in listing.body


def test_empty_design_reads_as_unfilled(handler: SeHandler) -> None:
    handler.put(id="blank1", text="{}")
    toc = handler.get(id="blank1")
    assert "unfilled" in toc.body


def test_get_block_view(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    block = handler.get(id="caster1", view="block", args={"name": "hub"})
    assert "parent: fork" in block.body
    assert "envelope: cyl:r0.008h0.03" in block.body
    assert "use: axle seat" in block.body
    assert "] m" in block.body  # pose is labelled metres


def test_get_block_view_unknown_name_raises(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    with pytest.raises(NotFound, match="no such block"):
        handler.get(id="caster1", view="block", args={"name": "wheel"})


def test_unknown_view_names_the_valid_ones(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    with pytest.raises(BadInput, match="unknown se view"):
        handler.get(id="caster1", view="mechanics")


# ── op validation ────────────────────────────────────────────────────────


def test_duplicate_name_per_design_rejected(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="duplicate block name"):
        handler.put(
            id="dup1",
            text=json.dumps(
                {
                    "ops": [
                        {"op": "add_block", "name": "fork"},
                        {"op": "add_block", "name": "fork"},
                    ]
                }
            ),
        )


def test_unknown_parent_rejected(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="no such block"):
        handler.put(
            id="orphan1",
            text=json.dumps(
                {"ops": [{"op": "add_block", "name": "hub", "parent": "fork"}]}
            ),
        )


def test_envelope_bad_config_rejected_via_real_dsl(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="bad envelope"):
        handler.put(
            id="bad1",
            text=json.dumps(
                {"ops": [{"op": "add_block", "name": "x", "envelope": "blob:r5"}]}
            ),
        )


def test_set_envelope_revises_and_clears(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    resp = handler.edit(
        id="caster1",
        ops=[{"op": "set_envelope", "block": "cap", "envelope": "cyl:r0.01h0.004"}],
    )
    assert "cyl:r0.01h0.004" in resp.body
    cleared = handler.edit(
        id="caster1", ops=[{"op": "set_envelope", "block": "cap", "envelope": None}]
    )
    assert "cyl:r0.01h0.004" not in cleared.body


def test_set_envelope_on_instance_rejected(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    handler.edit(
        id="caster1",
        ops=[{"op": "instance_block", "name": "hub2", "template": "hub"}],
    )
    with pytest.raises(BadInput, match="lives on the template"):
        handler.edit(
            id="caster1",
            ops=[{"op": "set_envelope", "block": "hub2", "envelope": "sphere:r0.01"}],
        )


def test_set_pose_moves_a_block(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    resp = handler.edit(
        id="caster1", ops=[{"op": "set_pose", "block": "cap", "pose": [0, 0, 0.05]}]
    )
    assert "0.05" in resp.body


# ── instancing (the nm guards, transferred) ──────────────────────────────


def test_instance_resolves_envelope_from_template(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    resp = handler.edit(
        id="caster1",
        ops=[
            {
                "op": "instance_block",
                "name": "hub2",
                "template": "hub",
                "pose": [0.02, 0, 0],
            }
        ],
    )
    assert "(instance of hub)" in resp.body
    assert "env=cyl:r0.008h0.03 (from hub)" in resp.body


def test_instance_of_instance_rejected(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    handler.edit(
        id="caster1",
        ops=[{"op": "instance_block", "name": "hub2", "template": "hub"}],
    )
    with pytest.raises(BadInput, match="itself an instance"):
        handler.edit(
            id="caster1",
            ops=[{"op": "instance_block", "name": "hub3", "template": "hub2"}],
        )


def test_instance_block_rejects_template_metadata(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    with pytest.raises(BadInput, match="does not take 'envelope'"):
        handler.edit(
            id="caster1",
            ops=[
                {
                    "op": "instance_block",
                    "name": "hub2",
                    "template": "hub",
                    "envelope": "sphere:r0.01",
                }
            ],
        )


def test_instance_cannot_nest_under_its_own_template(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    with pytest.raises(BadInput, match="nest the template"):
        handler.edit(
            id="caster1",
            ops=[
                {
                    "op": "instance_block",
                    "name": "hub2",
                    "template": "hub",
                    "parent": "cap",  # cap is inside hub's subtree
                }
            ],
        )


def test_indirect_two_way_instance_cycle_rejected(handler: SeHandler) -> None:
    # A hosts an instance of B, then B hosting an instance of A must fail —
    # the expansion-graph search, not the local guards, catches this.
    handler.put(
        id="cyc1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "A"},
                    {"op": "add_block", "name": "B"},
                    {
                        "op": "instance_block",
                        "name": "b_in_a",
                        "template": "B",
                        "parent": "A",
                    },
                ]
            }
        ),
    )
    with pytest.raises(BadInput, match="instance cycle"):
        handler.edit(
            id="cyc1",
            ops=[
                {
                    "op": "instance_block",
                    "name": "a_in_b",
                    "template": "A",
                    "parent": "B",
                }
            ],
        )


def test_remove_block_refuses_when_template_in_use(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    handler.edit(
        id="caster1",
        ops=[{"op": "instance_block", "name": "hub2", "template": "hub"}],
    )
    with pytest.raises(BadInput, match="used as a template"):
        handler.edit(id="caster1", ops=[{"op": "remove_block", "block": "hub"}])


def test_remove_block_drops_subtree(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    resp = handler.edit(id="caster1", ops=[{"op": "remove_block", "block": "hub"}])
    assert "hub" not in resp.body.split("edited", 1)[1]
    assert "cap" not in resp.body


# ── arrays (first-class block-level structure, se-kind.md "Hierarchy") ──


def test_array_block_linear_renders_multiplicity(handler: SeHandler) -> None:
    handler.put(id="rack1", text="{}")
    resp = handler.edit(
        id="rack1",
        ops=[
            {"op": "add_block", "name": "tooth", "envelope": "box:w0.002d0.004h0.006"},
            {
                "op": "array_block",
                "name": "teeth",
                "template": "tooth",
                "linear": {"count": 12, "pitch": 0.005, "axis": [1, 0, 0]},
            },
        ],
    )
    assert "(array of tooth ×12 linear pitch=0.005 axis=[1, 0, 0])" in resp.body
    assert "env=box:w0.002d0.004h0.006 (from tooth)" in resp.body


def test_array_block_polar_axis_defaults_to_z(handler: SeHandler) -> None:
    handler.put(id="wheel1", text="{}")
    resp = handler.edit(
        id="wheel1",
        ops=[
            {"op": "add_block", "name": "spoke", "envelope": "box:w0.03d0.004h0.01"},
            {
                "op": "array_block",
                "name": "spokes",
                "template": "spoke",
                "polar": {"count": 6, "radius": 0.02},
            },
        ],
    )
    assert "(array of spoke ×6 polar r=0.02 axis=[0, 0, 1])" in resp.body


def test_array_block_count_one_names_instance_block(handler: SeHandler) -> None:
    handler.put(id="wheel1", text="{}")
    handler.edit(id="wheel1", ops=[{"op": "add_block", "name": "spoke"}])
    with pytest.raises(BadInput, match="just an instance"):
        handler.edit(
            id="wheel1",
            ops=[
                {
                    "op": "array_block",
                    "name": "spokes",
                    "template": "spoke",
                    "polar": {"count": 1, "radius": 0.02},
                }
            ],
        )


def test_array_block_fractional_count_rejected_not_truncated(
    handler: SeHandler,
) -> None:
    # int() would silently truncate 2.9 → 2 — a fat-fingered count must
    # reject, never mint a differently-sized array (reviewer finding).
    handler.put(id="wheel1", text="{}")
    handler.edit(id="wheel1", ops=[{"op": "add_block", "name": "spoke"}])
    with pytest.raises(BadInput, match="whole number"):
        handler.edit(
            id="wheel1",
            ops=[
                {
                    "op": "array_block",
                    "name": "spokes",
                    "template": "spoke",
                    "polar": {"count": 2.9, "radius": 0.02},
                }
            ],
        )


def test_array_block_requires_exactly_one_pattern(handler: SeHandler) -> None:
    handler.put(id="wheel1", text="{}")
    handler.edit(id="wheel1", ops=[{"op": "add_block", "name": "spoke"}])
    with pytest.raises(BadInput, match="exactly one of"):
        handler.edit(
            id="wheel1",
            ops=[{"op": "array_block", "name": "spokes", "template": "spoke"}],
        )
    with pytest.raises(BadInput, match="exactly one of"):
        handler.edit(
            id="wheel1",
            ops=[
                {
                    "op": "array_block",
                    "name": "spokes",
                    "template": "spoke",
                    "linear": {"count": 3, "pitch": 0.01, "axis": [1, 0, 0]},
                    "polar": {"count": 3, "radius": 0.01},
                }
            ],
        )


def test_array_block_overrides_rejected_loudly_not_swallowed(
    handler: SeHandler,
) -> None:
    # The **_kw lesson: an unhonoured field must error, never silently drop.
    handler.put(id="wheel1", text="{}")
    handler.edit(id="wheel1", ops=[{"op": "add_block", "name": "spoke"}])
    with pytest.raises(BadInput, match="does not take 'overrides' yet"):
        handler.edit(
            id="wheel1",
            ops=[
                {
                    "op": "array_block",
                    "name": "spokes",
                    "template": "spoke",
                    "polar": {"count": 6, "radius": 0.02},
                    "overrides": {"3": {"pose": [0, 0, 0.01]}},
                }
            ],
        )


def test_array_block_zero_axis_rejected(handler: SeHandler) -> None:
    handler.put(id="rack1", text="{}")
    handler.edit(id="rack1", ops=[{"op": "add_block", "name": "tooth"}])
    with pytest.raises(BadInput, match="nonzero vector"):
        handler.edit(
            id="rack1",
            ops=[
                {
                    "op": "array_block",
                    "name": "teeth",
                    "template": "tooth",
                    "linear": {"count": 4, "pitch": 0.005, "axis": [0, 0, 0]},
                }
            ],
        )


def test_array_template_in_use_blocks_removal(handler: SeHandler) -> None:
    handler.put(id="wheel1", text="{}")
    handler.edit(
        id="wheel1",
        ops=[
            {"op": "add_block", "name": "spoke"},
            {
                "op": "array_block",
                "name": "spokes",
                "template": "spoke",
                "polar": {"count": 6, "radius": 0.02},
            },
        ],
    )
    with pytest.raises(BadInput, match="used as a template"):
        handler.edit(id="wheel1", ops=[{"op": "remove_block", "block": "spoke"}])


def test_array_spec_survives_persistence_roundtrip(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="wheel1", text="{}")
    handler.edit(
        id="wheel1",
        ops=[
            {"op": "add_block", "name": "spoke"},
            {
                "op": "array_block",
                "name": "spokes",
                "template": "spoke",
                "linear": {"count": 5, "pitch": 0.012, "axis": [0, 1, 0]},
            },
        ],
    )
    ref = store.get_ref(kind="se", id="wheel1")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    node = tree.blocks["spokes"]
    assert node.template == "spoke"
    assert node.array == {
        "kind": "linear",
        "count": 5,
        "pitch": 0.012,
        "axis": [0.0, 1.0, 0.0],
    }


# ── persistence: re-put replaces, edit accumulates, delete retires ──────


def test_reput_replaces_tree(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    resp = handler.put(
        id="caster1",
        text=json.dumps({"ops": [{"op": "add_block", "name": "solo"}]}),
    )
    assert "replaced" in resp.body
    toc = handler.get(id="caster1")
    assert "solo" in toc.body and "fork" not in toc.body


def test_edit_accumulates_on_live_tree(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    handler.edit(
        id="caster1",
        ops=[{"op": "add_block", "name": "wheel", "envelope": "cyl:r0.04h0.03"}],
    )
    toc = handler.get(id="caster1")
    assert "wheel" in toc.body and "fork" in toc.body


def test_edit_requires_ops(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    with pytest.raises(BadInput, match="requires ops="):
        handler.edit(id="caster1")


def test_delete_retires_design_and_blocks(handler: SeHandler, store: Store) -> None:
    handler.put(id="caster1", text=_CASTER)
    resp = handler.delete(id="caster1")
    assert "3 block(s)" in resp.body
    assert store.get_ref(kind="se", id="caster1") is None
    with pytest.raises(NotFound):
        handler.get(id="caster1")


def test_missing_design_lists_nothing_helpful(handler: SeHandler) -> None:
    with pytest.raises(NotFound, match="not found"):
        handler.get(id="nope")
    listing = handler.get()
    assert "no se designs yet" in listing.body


# ── ports + connects ─────────────────────────────────────────────────────


def _wheel_on_hub(handler: SeHandler) -> None:
    handler.put(
        id="cart1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "hub",
                        "envelope": "cyl:r0.008h0.03",
                    },
                    {
                        "op": "add_block",
                        "name": "wheel",
                        "envelope": "cyl:r0.04h0.02",
                        "pose": [0.2, 0, 0],
                    },
                    {
                        "op": "add_port",
                        "block": "hub",
                        "name": "shaft",
                        "roles": ["mates"],
                        "direction": [0, 0, 1],
                    },
                    {
                        "op": "add_port",
                        "block": "wheel",
                        "name": "bore",
                        "roles": ["mates"],
                        "annotations": {"finish": "as-printed"},
                    },
                ]
            }
        ),
    )


def test_ports_view_lists_roles_and_annotations(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    resp = handler.get(id="cart1", view="ports")
    assert "hub" in resp.body and "shaft" in resp.body
    assert "mates" in resp.body
    assert "as-printed" in resp.body  # annotations render (descriptive tier)


def test_ports_survive_persistence_roundtrip(handler: SeHandler, store: Store) -> None:
    _wheel_on_hub(handler)
    ref = store.get_ref(kind="se", id="cart1")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    port = tree.blocks["wheel"].ports["bore"]
    assert port.roles == ["mates"]
    assert port.annotations == {"finish": "as-printed"}
    assert tree.blocks["hub"].ports["shaft"].direction == [0.0, 0.0, 1.0]


def test_add_port_on_instance_rejected(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1", ops=[{"op": "instance_block", "name": "hub2", "template": "hub"}]
    )
    with pytest.raises(BadInput, match="resolves its ports from its template"):
        handler.edit(
            id="cart1", ops=[{"op": "add_port", "block": "hub2", "name": "extra"}]
        )


def test_dotted_port_name_rejected(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    with pytest.raises(BadInput, match="must not contain"):
        handler.edit(
            id="cart1", ops=[{"op": "add_port", "block": "hub", "name": "a.b"}]
        )


def test_connect_wires_ports_and_roundtrips(handler: SeHandler, store: Store) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1",
        ops=[
            {
                "op": "connect",
                "a": "wheel.bore",
                "b": "hub.shaft",
                # slice 3: joint/objectives go through the real schemas —
                # the slice-2 free-form dicts would now reject.
                "joint": {"class": "revolute", "axis": [0, 0, 2]},
                "objectives": {"force": [0, 0, -40]},
            }
        ],
    )
    ref = store.get_ref(kind="se", id="cart1")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    assert len(tree.connects) == 1
    c = tree.connects[0]
    # axis unit-normalized at write time
    assert c.joint == {"class": "revolute", "axis": [0.0, 0.0, 1.0]}
    assert c.objectives == {"force": [0.0, 0.0, -40.0]}
    block = handler.get(id="cart1", view="block", args={"name": "hub"})
    assert "revolute" in block.body


def test_connect_resolves_port_via_instance_template(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1",
        ops=[
            {"op": "instance_block", "name": "hub2", "template": "hub"},
            {"op": "connect", "a": "wheel.bore", "b": "hub2.shaft"},
        ],
    )
    ports = handler.get(id="cart1", view="ports")
    assert "hub2 (via hub)" in ports.body


def test_duplicate_connect_rejected(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1", ops=[{"op": "connect", "a": "wheel.bore", "b": "hub.shaft"}]
    )
    with pytest.raises(BadInput, match="already exists"):
        handler.edit(
            id="cart1",
            ops=[{"op": "connect", "a": "hub.shaft", "b": "wheel.bore"}],
        )


def test_connect_unknown_port_names_roster(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    with pytest.raises(BadInput, match="Available ports: shaft"):
        handler.edit(
            id="cart1",
            ops=[{"op": "connect", "a": "wheel.bore", "b": "hub.axle"}],
        )


def test_remove_port_blocked_by_live_connect(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1", ops=[{"op": "connect", "a": "wheel.bore", "b": "hub.shaft"}]
    )
    with pytest.raises(BadInput, match="disconnect first"):
        handler.edit(
            id="cart1", ops=[{"op": "remove_port", "block": "hub", "name": "shaft"}]
        )


def test_remove_port_blocked_by_connect_via_instance(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1",
        ops=[
            {"op": "instance_block", "name": "hub2", "template": "hub"},
            {"op": "connect", "a": "wheel.bore", "b": "hub2.shaft"},
        ],
    )
    with pytest.raises(BadInput, match="disconnect first"):
        handler.edit(
            id="cart1", ops=[{"op": "remove_port", "block": "hub", "name": "shaft"}]
        )


def test_disconnect_then_remove_port(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1", ops=[{"op": "connect", "a": "wheel.bore", "b": "hub.shaft"}]
    )
    handler.edit(
        id="cart1",
        ops=[
            {"op": "disconnect", "a": "hub.shaft", "b": "wheel.bore"},
            {"op": "remove_port", "block": "hub", "name": "shaft"},
        ],
    )
    ports = handler.get(id="cart1", view="ports")
    assert "shaft" not in ports.body


def test_remove_block_drops_touching_connects(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1", ops=[{"op": "connect", "a": "wheel.bore", "b": "hub.shaft"}]
    )
    handler.edit(id="cart1", ops=[{"op": "remove_block", "block": "wheel"}])
    validate_view = handler.get(id="cart1", view="validate")
    assert "dangling_connect" not in validate_view.body


# ── validate: filled-fraction honesty + findings ─────────────────────────


def test_validate_empty_design_reads_unfilled_not_done(handler: SeHandler) -> None:
    handler.put(id="blankv", text="{}")
    resp = handler.get(id="blankv", view="validate")
    assert "0/0 block(s)" in resp.body
    assert "unfilled" in resp.body


def test_validate_unenveloped_scaffold_is_loud(handler: SeHandler) -> None:
    handler.put(
        id="bare1",
        text=json.dumps({"ops": [{"op": "add_block", "name": "fork"}]}),
    )
    resp = handler.get(id="bare1", view="validate")
    assert "0/1 block(s)" in resp.body
    assert "UNFILLED scaffold" in resp.body


def test_validate_counts_envelopes_on_ordinary_blocks_only(
    handler: SeHandler,
) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1", ops=[{"op": "instance_block", "name": "hub2", "template": "hub"}]
    )
    resp = handler.get(id="cart1", view="validate")
    assert "2/2 block(s) have envelopes" in resp.body


def test_validate_flags_unconnected_port_as_warn(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    resp = handler.get(id="cart1", view="validate")
    assert "unconnected_port" in resp.body
    assert "warn" in resp.body


def test_validate_flags_ports_without_envelope(handler: SeHandler) -> None:
    handler.put(
        id="stub1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "arm"},
                    {"op": "add_port", "block": "arm", "name": "tip"},
                ]
            }
        ),
    )
    resp = handler.get(id="stub1", view="validate")
    assert "block_without_envelope" in resp.body


def test_validate_flags_undeclared_interpenetration(handler: SeHandler) -> None:
    # two root blocks fully overlapping at the origin, no connect — warn.
    handler.put(
        id="clash1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "a", "envelope": "cyl:r0.01h0.02"},
                    {"op": "add_block", "name": "b", "envelope": "cyl:r0.009h0.02"},
                ]
            }
        ),
    )
    resp = handler.get(id="clash1", view="validate")
    assert "undeclared_interpenetration" in resp.body
    assert "a—b" in resp.body


def test_validate_connect_sanctions_the_overlap(handler: SeHandler) -> None:
    # same clash, but a connect between the two blocks declares it a fit.
    handler.put(
        id="fit1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "a", "envelope": "cyl:r0.01h0.02"},
                    {"op": "add_block", "name": "b", "envelope": "cyl:r0.009h0.02"},
                    {"op": "add_port", "block": "a", "name": "od"},
                    {"op": "add_port", "block": "b", "name": "bore"},
                    {"op": "connect", "a": "a.od", "b": "b.bore"},
                ]
            }
        ),
    )
    resp = handler.get(id="fit1", view="validate")
    assert "undeclared_interpenetration" not in resp.body


def test_validate_parent_child_overlap_is_containment_not_finding(
    handler: SeHandler,
) -> None:
    handler.put(
        id="nest1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "case", "envelope": "box:w0.1d0.1h0.1"},
                    {
                        "op": "add_block",
                        "name": "board",
                        "parent": "case",
                        "envelope": "box:w0.08d0.08h0.002",
                    },
                ]
            }
        ),
    )
    resp = handler.get(id="nest1", view="validate")
    assert "undeclared_interpenetration" not in resp.body


def test_validate_dangling_connect_over_corrupted_tree() -> None:
    # Fabricate what ops.py forbids: a connect whose endpoint vanished.
    from precis_se.ops import ConnectSpec
    from precis_se.validate import validate

    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "a"},
            {"op": "add_port", "block": "a", "name": "p"},
        ],
    )
    tree.connects.append(
        ConnectSpec(a_block="a", a_port="p", b_block="gone", b_port="x")
    )
    findings = validate(tree)
    assert any(f.rule == "dangling_connect" and f.severity == "error" for f in findings)


# ── clearance view ───────────────────────────────────────────────────────


def test_clearance_reports_gap_in_metres(handler: SeHandler) -> None:
    _wheel_on_hub(handler)  # wheel at x=0.2, hub at origin — clear
    resp = handler.get(id="cart1", view="clearance", args={"a": "hub", "b": "wheel"})
    assert "gap:" in resp.body and " m " in resp.body
    assert "clear" in resp.body


def test_clearance_requires_envelopes(handler: SeHandler) -> None:
    handler.put(
        id="bare2",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "a"},
                    {"op": "add_block", "name": "b", "envelope": "sphere:r0.01"},
                ]
            }
        ),
    )
    with pytest.raises(BadInput, match="no effective envelope"):
        handler.get(id="bare2", view="clearance", args={"a": "a", "b": "b"})


def test_clearance_requires_both_args(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    with pytest.raises(BadInput, match="requires"):
        handler.get(id="cart1", view="clearance", args={"a": "hub"})


# ── search ───────────────────────────────────────────────────────────────


def test_search_finds_design_by_description_word(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_CASTER)
    resp = handler.search(q="caster")
    assert "caster1" in resp.body


def test_search_requires_q(handler: SeHandler) -> None:
    with pytest.raises(BadInput):
        handler.search()


# ── pure-ops corner: render cycle guard is total over corrupt data ──────


def test_render_survives_hand_corrupted_instance_cycle() -> None:
    # Bypass apply_ops to fabricate the cycle ops.py would reject — the
    # walk must terminate with a visible marker, never recurse forever.
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "A"},
            {"op": "add_block", "name": "B"},
            {
                "op": "instance_block",
                "name": "b_in_a",
                "template": "B",
                "parent": "A",
            },
        ],
    )
    corrupt = SeTree()
    corrupt.blocks = dict(tree.blocks)
    apply_ops(corrupt, [{"op": "add_block", "name": "a_in_b_shim", "parent": "B"}])
    corrupt.blocks["a_in_b_shim"].template = "A"  # the write ops.py forbids
    body = _render_tree(corrupt, "corrupt", "")
    assert "instance cycle" in body


def test_effective_envelope_tolerates_dangling_template() -> None:
    tree = SeTree()
    apply_ops(tree, [{"op": "add_block", "name": "solo"}])
    node = tree.blocks["solo"]
    node.template = "vanished"  # hand-corrupted
    assert effective_envelope(tree, node) is None


# ── slice 3: joints (kinematic class × mechanism) ────────────────────────


def _l2_tree() -> SeTree:
    """hub + wheel (apart in x), one port each, one connect — the pure-ops
    seed for the L2 tests."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "hub", "envelope": "cyl:r0.008h0.03"},
            {
                "op": "add_block",
                "name": "wheel",
                "envelope": "cyl:r0.04h0.02",
                "pose": [0.2, 0, 0],
            },
            {"op": "add_port", "block": "hub", "name": "shaft"},
            {"op": "add_port", "block": "wheel", "name": "bore"},
            {"op": "connect", "a": "wheel.bore", "b": "hub.shaft"},
        ],
    )
    return tree


def test_joint_unknown_class_rejected() -> None:
    tree = _l2_tree()
    with pytest.raises(OpError, match="must be one of.*revolute"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_joint",
                    "a": "wheel.bore",
                    "b": "hub.shaft",
                    "joint": {"class": "spinny"},
                }
            ],
        )


def test_joint_unknown_key_rejected_loudly() -> None:
    # the swallowed-facet lesson: a stray key must reject, never be
    # silently dropped (mechanism-specific numbers belong under params).
    tree = _l2_tree()
    with pytest.raises(OpError, match="unknown joint key.*preload"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_joint",
                    "a": "wheel.bore",
                    "b": "hub.shaft",
                    "joint": {"class": "revolute", "preload": 3},
                }
            ],
        )


def test_joint_axis_on_axisless_class_rejected() -> None:
    tree = _l2_tree()
    with pytest.raises(OpError, match="takes no 'axis'"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_joint",
                    "a": "wheel.bore",
                    "b": "hub.shaft",
                    "joint": {"class": "rigid", "axis": [0, 0, 1]},
                }
            ],
        )


def test_joint_zero_axis_rejected() -> None:
    tree = _l2_tree()
    with pytest.raises(OpError, match="nonzero"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_joint",
                    "a": "wheel.bore",
                    "b": "hub.shaft",
                    "joint": {"class": "revolute", "axis": [0, 0, 0]},
                }
            ],
        )


def test_joint_unknown_mechanism_rejected() -> None:
    tree = _l2_tree()
    with pytest.raises(OpError, match="mechanism.*must be one of.*press"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_joint",
                    "a": "wheel.bore",
                    "b": "hub.shaft",
                    "joint": {"class": "revolute", "mechanism": "glue"},
                }
            ],
        )


def test_set_joint_replaces_and_clears() -> None:
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {
                "op": "set_joint",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {
                    "class": "revolute",
                    "axis": [0, 0, 2],
                    "mechanism": "bearing",
                },
            }
        ],
    )
    c = tree.connects[0]
    assert c.joint == {
        "class": "revolute",
        "axis": [0.0, 0.0, 1.0],  # unit-normalized
        "mechanism": "bearing",
    }
    apply_ops(
        tree,
        [{"op": "set_joint", "a": "wheel.bore", "b": "hub.shaft", "joint": None}],
    )
    assert tree.connects[0].joint is None


def test_set_joint_missing_connect_lists_live() -> None:
    tree = _l2_tree()
    with pytest.raises(OpError, match="Live connects: .*wheel.bore"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_joint",
                    "a": "wheel.rim",
                    "b": "hub.shaft",
                    "joint": {"class": "rigid"},
                }
            ],
        )


# ── slice 3: loads (objective vectors) ───────────────────────────────────


def test_set_load_on_block_vets_and_stores() -> None:
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {
                "op": "set_load",
                "block": "wheel",
                "force": [0, 0, -200],
                "duty": "pushed around a workshop daily",
                "cycles": 1e6,
            }
        ],
    )
    assert tree.blocks["wheel"].objectives == {
        "force": [0.0, 0.0, -200.0],
        "duty": "pushed around a workshop daily",
        "cycles": 1e6,
    }


def test_set_load_on_connect_and_clear() -> None:
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {
                "op": "set_load",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "torque": [0, 0, 1.5],
            }
        ],
    )
    assert tree.connects[0].objectives == {"torque": [0.0, 0.0, 1.5]}
    apply_ops(
        tree,
        [{"op": "set_load", "a": "wheel.bore", "b": "hub.shaft", "clear": True}],
    )
    assert tree.connects[0].objectives == {}


def test_set_load_stray_key_rejected_never_swallowed() -> None:
    tree = _l2_tree()
    with pytest.raises(OpError, match="unknown key.*radial_N"):
        apply_ops(tree, [{"op": "set_load", "block": "wheel", "radial_N": 40}])
    with pytest.raises(OpError, match="unknown key.*radial_N"):
        # even next to a valid key — never silently dropped
        apply_ops(
            tree,
            [
                {
                    "op": "set_load",
                    "block": "wheel",
                    "force": [0, 0, -1],
                    "radial_N": 40,
                }
            ],
        )
    with pytest.raises(OpError, match="needs at least one of"):
        apply_ops(tree, [{"op": "set_load", "block": "wheel"}])


def test_connect_objectives_vetted_through_registry() -> None:
    tree = _l2_tree()
    apply_ops(tree, [{"op": "add_port", "block": "hub", "name": "base"}])
    with pytest.raises(OpError, match="unknown objective key.*radial_N"):
        apply_ops(
            tree,
            [
                {
                    "op": "connect",
                    "a": "wheel.bore",
                    "b": "hub.base",
                    "objectives": {"radial_N": 40},
                }
            ],
        )


def test_set_load_needs_exactly_one_target() -> None:
    tree = _l2_tree()
    with pytest.raises(OpError, match="exactly one of"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_load",
                    "block": "wheel",
                    "a": "wheel.bore",
                    "b": "hub.shaft",
                    "force": [0, 0, -1],
                }
            ],
        )
    with pytest.raises(OpError, match="mutually exclusive"):
        apply_ops(
            tree,
            [{"op": "set_load", "block": "wheel", "clear": True, "cycles": 5}],
        )


# ── slice 3: measures + tolerance relations ─────────────────────────────


def test_add_measure_with_relation_and_duplicate() -> None:
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {"op": "add_measure", "block": "hub", "name": "od_d", "value": 0.016},
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "bore_d",
                "relation": {"source": "hub.od_d", "offset": 2e-4, "tol": 5e-5},
                "strength": "hard",
                "reason": "running clearance for the axle seat",
            },
        ],
    )
    assert len(tree.measures) == 2
    m = tree.measures[1]
    assert m.relation == {"source": "hub.od_d", "offset": 2e-4, "tol": 5e-5}
    assert m.strength == "hard"
    with pytest.raises(OpError, match="duplicate measure"):
        apply_ops(tree, [{"op": "add_measure", "block": "hub", "name": "od_d"}])


def test_add_measure_guards() -> None:
    tree = _l2_tree()
    with pytest.raises(OpError, match="must not contain '.'"):
        apply_ops(tree, [{"op": "add_measure", "block": "hub", "name": "od.d"}])
    with pytest.raises(OpError, match="no such block"):
        apply_ops(tree, [{"op": "add_measure", "block": "ghost", "name": "od_d"}])
    apply_ops(tree, [{"op": "instance_block", "name": "hub2", "template": "hub"}])
    with pytest.raises(OpError, match="measures live on the template"):
        apply_ops(tree, [{"op": "add_measure", "block": "hub2", "name": "od_d"}])
    with pytest.raises(OpError, match="unknown relation key"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_measure",
                    "block": "hub",
                    "name": "od_d",
                    "relation": {"source": "hub.x", "slack": 1},
                }
            ],
        )
    with pytest.raises(OpError, match="'tol' must be ≥ 0"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_measure",
                    "block": "hub",
                    "name": "od_d",
                    "relation": {"source": "hub.x", "tol": -1e-5},
                }
            ],
        )
    with pytest.raises(OpError, match="'strength' must be one of"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_measure",
                    "block": "hub",
                    "name": "od_d",
                    "strength": "firm",
                }
            ],
        )


def test_add_measure_forward_reference_accepted() -> None:
    # a relation source that doesn't exist YET is legal at write time
    # (spec: "unresolvable relations" is graph-DRC's read-time finding).
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "bore_d",
                "relation": {"source": "hub.od_d", "offset": 2e-4, "tol": 5e-5},
            }
        ],
    )
    assert tree.measures[0].relation is not None


def test_set_and_remove_measure() -> None:
    tree = _l2_tree()
    apply_ops(
        tree,
        [{"op": "add_measure", "block": "hub", "name": "od_d", "value": 0.016}],
    )
    apply_ops(
        tree,
        [
            {
                "op": "set_measure",
                "block": "hub",
                "name": "od_d",
                "value": 0.017,
                "strength": "soft",
            }
        ],
    )
    m = tree.measures[0]
    assert m.value == 0.017
    assert m.strength == "soft"
    with pytest.raises(OpError, match="at least one of"):
        apply_ops(tree, [{"op": "set_measure", "block": "hub", "name": "od_d"}])
    with pytest.raises(OpError, match="Measures on 'hub': od_d"):
        apply_ops(tree, [{"op": "set_measure", "block": "hub", "name": "id_d"}])
    apply_ops(tree, [{"op": "remove_measure", "block": "hub", "name": "od_d"}])
    assert tree.measures == []
    with pytest.raises(OpError, match="no such measure"):
        apply_ops(tree, [{"op": "remove_measure", "block": "hub", "name": "od_d"}])


def test_remove_block_drops_owned_measures_keeps_danglers() -> None:
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {"op": "add_measure", "block": "hub", "name": "od_d", "value": 0.016},
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "bore_d",
                "relation": {"source": "hub.od_d", "offset": 2e-4, "tol": 5e-5},
            },
            {"op": "disconnect", "a": "wheel.bore", "b": "hub.shaft"},
            {"op": "remove_block", "block": "hub"},
        ],
    )
    # hub's measure went with the block; wheel's survives with a dangling
    # relation — DRC's unresolvable_relation reports it.
    assert [f"{m.block}.{m.name}" for m in tree.measures] == ["wheel.bore_d"]
    report = se_drc.drc(tree)
    assert any(f.rule == "unresolvable_relation" for f in report.findings)


# ── slice 3: stack-up evaluation ─────────────────────────────────────────


def test_stackup_chain_sums_offsets_and_tols() -> None:
    ms = [
        MeasureSpec(block="hub", name="od", value=0.016),
        MeasureSpec(
            block="wheel",
            name="bore",
            relation={"source": "hub.od", "offset": 2e-4, "tol": 5e-5},
        ),
        MeasureSpec(
            block="cap",
            name="fit",
            relation={"source": "wheel.bore", "offset": 1e-4, "tol": 5e-5},
        ),
    ]
    results = {r.measure: r for r in stackup(ms)}
    assert results["wheel.bore"].derived == pytest.approx(0.0162)
    assert results["wheel.bore"].tol_accum == pytest.approx(5e-5)
    assert results["cap.fit"].derived == pytest.approx(0.0163)
    assert results["cap.fit"].tol_accum == pytest.approx(1e-4)
    assert results["cap.fit"].problem is None


def test_stackup_detects_cycle_and_dangling() -> None:
    ms = [
        MeasureSpec(
            block="a", name="x", relation={"source": "b.y", "offset": 0, "tol": 0}
        ),
        MeasureSpec(
            block="b", name="y", relation={"source": "a.x", "offset": 0, "tol": 0}
        ),
        MeasureSpec(
            block="c",
            name="z",
            relation={"source": "ghost.w", "offset": 0, "tol": 0},
        ),
    ]
    results = {r.measure: r for r in stackup(ms)}
    assert results["a.x"].problem_kind == "cycle"
    assert results["c.z"].problem_kind == "dangling"


def test_stackup_declared_vs_derived_mismatch() -> None:
    ms = [
        MeasureSpec(block="hub", name="od", value=0.016),
        MeasureSpec(
            block="wheel",
            name="bore",
            value=0.020,  # disagrees with 0.0162 beyond ±5e-5
            relation={"source": "hub.od", "offset": 2e-4, "tol": 5e-5},
        ),
    ]
    (res,) = stackup(ms)
    assert res.problem_kind == "mismatch"
    ms[1].value = 0.01623  # within ±5e-5 of 0.0162 — agreement
    (res,) = stackup(ms)
    assert res.problem is None


def test_stackup_valued_midchain_anchors_nearest() -> None:
    ms = [
        MeasureSpec(block="a", name="x", value=1.0),
        MeasureSpec(
            block="b",
            name="y",
            value=2.0,  # declared AND related — nearest anchor for c.z
            relation={"source": "a.x", "offset": 0.5, "tol": 0.1},
        ),
        MeasureSpec(
            block="c", name="z", relation={"source": "b.y", "offset": 0.1, "tol": 0.01}
        ),
    ]
    results = {r.measure: r for r in stackup(ms)}
    assert results["c.z"].derived == pytest.approx(2.1)  # from b.y=2.0, not a.x
    assert results["c.z"].tol_accum == pytest.approx(0.01)


# ── slice 3: graph-tier DRC ──────────────────────────────────────────────


def test_drc_flags_malformed_stored_joint() -> None:
    # a pre-slice-3 free-form joint (or hand-corrupted row) must surface
    # as a finding, never a crash.
    tree = _l2_tree()
    tree.connects[0].joint = {"kind": "revolute"}
    report = se_drc.drc(tree)
    assert any(
        f.rule == "malformed_joint" and f.severity == "error" for f in report.findings
    )


def test_drc_flags_rigid_vs_moving_contradiction() -> None:
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {"op": "add_port", "block": "hub", "name": "flange"},
            {"op": "add_port", "block": "wheel", "name": "face"},
            {
                "op": "set_joint",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {"class": "revolute"},
            },
            {
                "op": "connect",
                "a": "wheel.face",
                "b": "hub.flange",
                "joint": {"class": "rigid"},
            },
        ],
    )
    report = se_drc.drc(tree)
    contra = [f for f in report.findings if f.rule == "joint_contradiction"]
    assert len(contra) == 1
    assert contra[0].severity == "error"
    assert "revolute" in contra[0].detail


def test_drc_mechanism_demand_unmet_then_met() -> None:
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {
                "op": "set_joint",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {"class": "rigid", "mechanism": "press"},
            }
        ],
    )
    report = se_drc.drc(tree)
    demand = [f for f in report.findings if f.rule == "mechanism_demand"]
    assert len(demand) == 1
    assert "interference tolerance relation" in demand[0].detail
    # declaring the relation between the two blocks' measures satisfies it
    apply_ops(
        tree,
        [
            {"op": "add_measure", "block": "hub", "name": "od_d", "value": 0.016},
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "bore_d",
                "relation": {"source": "hub.od_d", "offset": -2e-5, "tol": 1e-5},
                "strength": "hard",
            },
        ],
    )
    report = se_drc.drc(tree)
    assert not [f for f in report.findings if f.rule == "mechanism_demand"]


def test_drc_flags_stray_stored_objective_key() -> None:
    tree = _l2_tree()
    tree.blocks["wheel"].objectives = {"radial_N": 40}  # pre-schema stray
    report = se_drc.drc(tree)
    assert any(f.rule == "unchecked_objective" for f in report.findings)


def test_drc_flags_measure_on_missing_block() -> None:
    tree = _l2_tree()
    tree.measures.append(MeasureSpec(block="ghost", name="w", value=1.0))
    report = se_drc.drc(tree)
    assert any(
        f.rule == "measure_on_missing_block" and f.severity == "error"
        for f in report.findings
    )


def test_drc_stackup_mismatch_is_warn_dangling_is_error() -> None:
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {"op": "add_measure", "block": "hub", "name": "od", "value": 0.016},
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "bore",
                "value": 0.02,
                "relation": {"source": "hub.od", "offset": 2e-4, "tol": 5e-5},
            },
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "rim",
                "relation": {"source": "fender.gap", "offset": 0, "tol": 0},
            },
        ],
    )
    report = se_drc.drc(tree)
    by_rule = {f.rule: f for f in report.findings}
    assert by_rule["tolerance_mismatch"].severity == "warn"
    assert by_rule["unresolvable_relation"].severity == "error"


# ── slice 3: declared-vs-derived DOF probe ───────────────────────────────


def test_dof_probe_revolute_unbounded_axis_travel_warns() -> None:
    # hub and wheel are 0.2 m apart in x — nothing bounds axial (z)
    # travel, so a declared revolute is geometrically unconstrained.
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {
                "op": "set_joint",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {"class": "revolute", "axis": [0, 0, 1]},
            }
        ],
    )
    report = se_drc.drc(tree)
    assert any(f.rule == "dof_disagreement" for f in report.findings)
    assert any("FINDING" in p.outcome for p in report.dof_probes)


def test_dof_probe_revolute_bounded_is_ok_prismatic_blocked_warns() -> None:
    # co-located (interpenetrating) envelopes: zero axis travel — bounded,
    # fine for revolute; a blocked slide for prismatic.
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "hub", "envelope": "cyl:r0.008h0.03"},
            {"op": "add_block", "name": "wheel", "envelope": "cyl:r0.04h0.02"},
            {"op": "add_port", "block": "hub", "name": "shaft"},
            {"op": "add_port", "block": "wheel", "name": "bore"},
            {
                "op": "connect",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {"class": "revolute", "axis": [0, 0, 1]},
            },
        ],
    )
    report = se_drc.drc(tree)
    assert not [f for f in report.findings if f.rule == "dof_disagreement"]
    assert any(p.outcome.startswith("ok") for p in report.dof_probes)
    apply_ops(
        tree,
        [
            {
                "op": "set_joint",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {"class": "prismatic", "axis": [0, 0, 1]},
            }
        ],
    )
    report = se_drc.drc(tree)
    assert any(f.rule == "dof_disagreement" for f in report.findings)
    assert "cannot move" in next(
        f.detail for f in report.findings if f.rule == "dof_disagreement"
    )


def test_dof_probe_honest_skips() -> None:
    tree = _l2_tree()
    # no axis declared
    apply_ops(
        tree,
        [
            {
                "op": "set_joint",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {"class": "prismatic"},
            }
        ],
    )
    report = se_drc.drc(tree)
    assert any("no axis declared" in p.outcome for p in report.dof_probes)
    # off-principal axis
    apply_ops(
        tree,
        [
            {
                "op": "set_joint",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {"class": "prismatic", "axis": [1, 1, 0]},
            }
        ],
    )
    report = se_drc.drc(tree)
    assert any("not principal-aligned" in p.outcome for p in report.dof_probes)
    assert not [f for f in report.findings if f.rule == "dof_disagreement"]


# ── slice 3: persistence + views over the L2 tier ────────────────────────


def _l2_design(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    handler.edit(
        id="cart1",
        ops=[
            {
                "op": "connect",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {
                    "class": "revolute",
                    "axis": [0, 0, 1],
                    "mechanism": "bearing",
                },
            },
            {"op": "set_load", "block": "wheel", "force": [0, 0, -200]},
            {"op": "add_measure", "block": "hub", "name": "od_d", "value": 0.016},
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "bore_d",
                "relation": {"source": "hub.od_d", "offset": 2e-4, "tol": 5e-5},
                "strength": "hard",
                "reason": "bearing seat clearance",
            },
        ],
    )


def test_l2_tier_roundtrips_through_store(handler: SeHandler, store: Store) -> None:
    _l2_design(handler)
    ref = store.get_ref(kind="se", id="cart1")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    assert tree.connects[0].joint == {
        "class": "revolute",
        "axis": [0.0, 0.0, 1.0],
        "mechanism": "bearing",
    }
    assert tree.blocks["wheel"].objectives == {"force": [0.0, 0.0, -200.0]}
    assert len(tree.measures) == 2
    hard = next(m for m in tree.measures if m.name == "bore_d")
    assert hard.strength == "hard"
    assert hard.relation == {"source": "hub.od_d", "offset": 2e-4, "tol": 5e-5}
    assert hard.reason == "bearing seat clearance"


def test_measures_view_renders_table_and_stackup(handler: SeHandler) -> None:
    _l2_design(handler)
    resp = handler.get(id="cart1", view="measures")
    assert "hub.od_d" in resp.body
    assert "= hub.od_d + 0.0002 ± 5e-05" in resp.body
    assert "stack-up" in resp.body
    assert "0.0162" in resp.body  # derived value
    assert "hard" in resp.body


def test_measures_view_empty_is_honest(handler: SeHandler) -> None:
    _wheel_on_hub(handler)
    resp = handler.get(id="cart1", view="measures")
    assert "no measures declared yet" in resp.body


def test_drc_view_renders_probes_and_findings(handler: SeHandler) -> None:
    _l2_design(handler)
    resp = handler.get(id="cart1", view="drc")
    # bearing's demand is met (the bore_d relation), so no mechanism
    # finding; the far-apart revolute IS a dof finding + probe row.
    assert "mechanism_demand" not in resp.body
    assert "dof_disagreement" in resp.body
    assert "declared-vs-derived DOF" in resp.body
    assert "block(s) have envelopes" in resp.body  # honesty header


def test_drc_view_clean_design_reads_unfilled_not_done(handler: SeHandler) -> None:
    handler.put(id="bare1", text=json.dumps({"ops": []}))
    resp = handler.get(id="bare1", view="drc")
    assert "✓ no DRC findings" in resp.body
    assert "no blocks declared yet (unfilled)" in resp.body


def test_block_view_shows_loads_and_measures(handler: SeHandler) -> None:
    _l2_design(handler)
    resp = handler.get(id="cart1", view="block", args={"name": "wheel"})
    assert '"force"' in resp.body
    assert "wheel.bore_d" in resp.body
    assert "bearing seat clearance" in resp.body


# ── reviewer findings: corrupt stored data + contradiction coverage ──────


def test_malformed_stored_relation_is_a_finding_never_a_crash() -> None:
    # a hand-corrected jsonb row (offset: null) must surface as a problem/
    # finding on every read path — the malformed_joint posture for
    # measures (reviewer finding 1).
    tree = _l2_tree()
    tree.measures.append(
        MeasureSpec(
            block="wheel",
            name="bore_d",
            value=0.016,
            relation={"source": "hub.od_d", "offset": None, "tol": 5e-5},
        )
    )
    tree.measures.append(MeasureSpec(block="hub", name="od_d", value=0.016))
    (res,) = stackup(tree.measures)
    assert res.problem_kind == "malformed"
    report = se_drc.drc(tree)
    assert any(
        f.rule == "malformed_relation" and f.severity == "error"
        for f in report.findings
    )


def test_malformed_relation_midchain_acts_as_anchor() -> None:
    # the broken measure's brokenness is ITS finding; a dependent chain
    # stops there (using its declared value) rather than crashing.
    ms = [
        MeasureSpec(
            block="a",
            name="x",
            value=1.0,
            relation={"source": "z.q", "offset": "bogus", "tol": 0},
        ),
        MeasureSpec(
            block="b", name="y", relation={"source": "a.x", "offset": 0.5, "tol": 0.1}
        ),
    ]
    results = {r.measure: r for r in stackup(ms)}
    assert results["a.x"].problem_kind == "malformed"
    assert results["b.y"].problem is None
    assert results["b.y"].derived == pytest.approx(1.5)


def test_measure_views_render_corrupt_relation_legibly(
    handler: SeHandler, store: Store
) -> None:
    _l2_design(handler)
    ref = store.get_ref(kind="se", id="cart1")
    assert ref is not None
    with store.pool.connection() as c:
        c.execute(
            "UPDATE se_measures SET relation = %s "
            "WHERE ref_id = %s AND name = 'bore_d' AND retired_at IS NULL",
            ('{"source": "hub.od_d", "offset": null, "tol": 5e-5}', ref.id),
        )
    # neither view crashes; drc names the malformed relation
    body = handler.get(id="cart1", view="measures").body
    assert "hub.od_d" in body
    drc_body = handler.get(id="cart1", view="drc").body
    assert "malformed_relation" in drc_body
    block_body = handler.get(id="cart1", view="block", args={"name": "wheel"}).body
    assert "wheel.bore_d" in block_body


def test_drc_flags_rigid_vs_compliant_contradiction() -> None:
    # compliant permits motion (with stiffness) — rigid+compliant between
    # the same pair contradicts (reviewer finding 2).
    tree = _l2_tree()
    apply_ops(
        tree,
        [
            {"op": "add_port", "block": "hub", "name": "flange"},
            {"op": "add_port", "block": "wheel", "name": "face"},
            {
                "op": "set_joint",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {"class": "compliant"},
            },
            {
                "op": "connect",
                "a": "wheel.face",
                "b": "hub.flange",
                "joint": {"class": "rigid"},
            },
        ],
    )
    report = se_drc.drc(tree)
    assert any(f.rule == "joint_contradiction" for f in report.findings)


def test_set_measure_explicit_null_pushes_back() -> None:
    # value=null must error, never silently no-op (reviewer finding 3).
    tree = _l2_tree()
    apply_ops(
        tree,
        [{"op": "add_measure", "block": "hub", "name": "od_d", "value": 0.016}],
    )
    with pytest.raises(OpError, match="cannot clear value"):
        apply_ops(
            tree,
            [{"op": "set_measure", "block": "hub", "name": "od_d", "value": None}],
        )
    assert tree.measures[0].value == 0.016  # untouched
