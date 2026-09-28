"""``se`` block ``pose``/``rot`` composition — the engine fix making
``precis-se-help.md``'s "pose is the block origin in the PARENT frame"
true: :func:`precis_se.ops.compose_world_pose` composes each block's
stored, parent-relative :attr:`~precis_se.ops.SeBlock.local_pose`/
``local_rot`` up its parent chain into the WORLD-frame ``pose``/``rot``
every geometry consumer (validate/fasten/toolaccess/stability/datums/
measures/printing/the renderers) already assumed. :func:`precis_se.persist.
load_tree` runs it once after reading a design's rows; ``save_tree`` writes
``local_pose``/``local_rot`` back, never the composed ``pose``/``rot`` —
this module pins both halves plus the round-trip stability that split
makes possible.

Fixture shape lifted from ``test_se_plugin.py`` — the shared test DB
template carries only core migrations, so the plugin's own are seeded
here.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import precis_se
from precis.dispatch import Hub
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import SeBlock, SeTree, compose_world_pose, local_position_from_world

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


def _ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return int(ref.id)


# ── pure composition math — no store ─────────────────────────────────────


def test_unicycle_c1_regression_bolt_under_offset_parent() -> None:
    """The exact live-prod arithmetic (evidence for this fix): a pinch
    bolt at ``[0, 0, 0.016]`` under a parent whose own (root) pose is
    ``[0, 0.1, 0.254]`` composes to ``[0, 0.1, 0.270]`` — dead centre of
    the parent's boss, not the floor a bare (uncomposed) read would give."""
    tree = SeTree()
    tree.blocks["crank_left"] = SeBlock(name="crank_left", local_pose=[0.0, 0.1, 0.254])
    tree.blocks["crank_bolt_left"] = SeBlock(
        name="crank_bolt_left", parent="crank_left", local_pose=[0.0, 0.0, 0.016]
    )
    compose_world_pose(tree)
    assert tree.blocks["crank_bolt_left"].pose == pytest.approx([0.0, 0.1, 0.270])
    # Uncomposed (the pre-fix reading) would have parked it on the floor —
    # pinned so a regression that drops composition entirely still fails
    # this the moment the corrected value stops matching.
    assert tree.blocks["crank_bolt_left"].pose != pytest.approx(
        [0.0, 0.0, 0.016], abs=1e-6
    )


def test_compose_two_levels_deep_with_rotated_parent() -> None:
    """A grandchild under a 90°-about-z root: rotation composes (``rz``
    carries down both levels) and translation is rotated before it's
    added, not just summed — the general rigid-transform case, not the
    unicycle's axis-aligned one."""
    tree = SeTree()
    tree.blocks["base"] = SeBlock(name="base", local_rot=[0.0, 0.0, math.pi / 2])
    tree.blocks["mid"] = SeBlock(name="mid", parent="base", local_pose=[1.0, 0.0, 0.0])
    tree.blocks["leaf"] = SeBlock(name="leaf", parent="mid", local_pose=[0.0, 0.0, 0.5])
    compose_world_pose(tree)

    # base: root, so world == local.
    assert tree.blocks["base"].pose == pytest.approx([0.0, 0.0, 0.0])
    assert tree.blocks["base"].rot == pytest.approx([0.0, 0.0, math.pi / 2])

    # mid: [1,0,0] rotated 90° about z lands on [0,1,0]; its own rot
    # inherits the parent's 90° (its local_rot is zero).
    assert tree.blocks["mid"].pose == pytest.approx([0.0, 1.0, 0.0], abs=1e-9)
    assert tree.blocks["mid"].rot == pytest.approx([0.0, 0.0, math.pi / 2])

    # leaf: [0,0,0.5] is unaffected by a pure-z rotation, so it just adds
    # onto mid's world position; rot carries the same 90° down again.
    assert tree.blocks["leaf"].pose == pytest.approx([0.0, 1.0, 0.5], abs=1e-9)
    assert tree.blocks["leaf"].rot == pytest.approx([0.0, 0.0, math.pi / 2])


def test_compose_parent_cycle_terminates() -> None:
    """A stored parent cycle (hand-corrupted data — ``add_block``/
    ``instance_block`` refuse one at write time, but nothing stops a
    hand-edited row) must not hang the walk. Completing at all — within
    the test's normal time budget — is the assertion; a regression to a
    plain unmemoized recursion would hang this test instead of failing
    it, which is why the values themselves are only checked for
    finiteness rather than pinned to one arbitrary resolution."""
    tree = SeTree()
    tree.blocks["x"] = SeBlock(name="x", parent="y", local_pose=[1.0, 0.0, 0.0])
    tree.blocks["y"] = SeBlock(name="y", parent="x", local_pose=[0.0, 1.0, 0.0])
    compose_world_pose(tree)  # must return, not hang
    for name in ("x", "y"):
        pose = tree.blocks[name].pose
        assert len(pose) == 3
        assert all(math.isfinite(v) for v in pose)


def test_local_position_from_world_inverts_compose() -> None:
    """:func:`~precis_se.ops.local_position_from_world` — the decompose
    ``formfind`` uses to keep ``local_pose`` in sync with a solved WORLD
    position — is the exact inverse of composing that same local position
    back up against the (unmoved) parent."""
    tree = SeTree()
    tree.blocks["base"] = SeBlock(
        name="base", local_pose=[0.2, -0.1, 0.05], local_rot=[0.0, 0.0, math.pi / 3]
    )
    tree.blocks["child"] = SeBlock(
        name="child", parent="base", local_pose=[0.03, 0.0, 0.0]
    )
    compose_world_pose(tree)
    world_before = tree.blocks["child"].pose

    new_world = [0.5, 0.25, 0.1]
    recovered_local = local_position_from_world(tree, "child", new_world)
    tree.blocks["child"].local_pose = recovered_local
    compose_world_pose(tree)
    assert tree.blocks["child"].pose == pytest.approx(new_world, abs=1e-9)
    assert tree.blocks["child"].pose != pytest.approx(world_before, abs=1e-6)


# ── persist round trip — real store ──────────────────────────────────────

_NESTED = json.dumps(
    {
        "ops": [
            {
                "op": "add_block",
                "name": "crank_left",
                "envelope": "box:w0.02d0.02h0.03",
                "pose": [0.0, 0.1, 0.254],
            },
            {
                "op": "add_block",
                "name": "crank_bolt_left",
                "parent": "crank_left",
                "envelope": "cyl:r0.002h0.01",
                "pose": [0.0, 0.0, 0.016],
                "rot": [0.1, 0.0, 0.3],
            },
        ]
    }
)


def test_pose_round_trip_byte_identical_across_two_saves(
    handler: SeHandler, store: Store
) -> None:
    """Build a nested tree, save, load, save again: the STORED (parent-
    relative) pose must be byte-identical across the two saves — nothing
    ever decomposes a composed WORLD value back into Euler angles for
    storage, so nothing has the chance to drift."""
    handler.put(id="rt1", text=_NESTED)
    ref_id = _ref_id(store, "rt1")

    first_load = persist.load_tree(store, ref_id)
    persist.save_tree(store, ref_id=ref_id, tree=first_load, card_text="x")
    second_load = persist.load_tree(store, ref_id)

    for name in ("crank_left", "crank_bolt_left"):
        assert second_load.blocks[name].local_pose == first_load.blocks[name].local_pose
        assert second_load.blocks[name].local_rot == first_load.blocks[name].local_rot
    # And the relative values themselves are exactly what was authored —
    # a load→save with no edit never touches them at all.
    assert first_load.blocks["crank_bolt_left"].local_pose == [0.0, 0.0, 0.016]
    assert first_load.blocks["crank_bolt_left"].local_rot == [0.1, 0.0, 0.3]


def test_loaded_tree_reads_world_frame_pose(handler: SeHandler, store: Store) -> None:
    """The bug this whole fix is for: a design where a parent has a
    non-zero pose must read back its child's pose in WORLD frame — the
    unicycle-c1 arithmetic, but through the real ``put``/``load_tree``
    path rather than direct dataclass construction."""
    handler.put(id="rt2", text=_NESTED)
    tree = persist.load_tree(store, _ref_id(store, "rt2"))
    assert tree.blocks["crank_bolt_left"].pose == pytest.approx([0.0, 0.1, 0.270])


def test_edit_set_pose_stores_relative_not_world(
    handler: SeHandler, store: Store
) -> None:
    """``set_pose`` in an ``edit`` takes its ``pose=``/``rot=`` argument as
    parent-relative (the doc's contract) even though the loaded tree's
    ``pose``/``rot`` are WORLD at that point — a regression that stored
    the composed world value here would silently walk every offset design
    further from the origin on every edit."""
    handler.put(id="rt3", text=_NESTED)
    ref_id = _ref_id(store, "rt3")
    handler.edit(
        id="rt3",
        ops=[{"op": "set_pose", "block": "crank_bolt_left", "pose": [0.0, 0.0, 0.02]}],
    )
    tree = persist.load_tree(store, ref_id)
    node = tree.blocks["crank_bolt_left"]
    # Stored relative to crank_left, exactly the op's argument.
    assert node.local_pose == [0.0, 0.0, 0.02]
    # Read back composed against the parent's [0, 0.1, 0.254].
    assert node.pose == pytest.approx([0.0, 0.1, 0.274])


# ── render layer: the copy-paste trap (gr454563-adjacent) ───────────────


def test_tree_view_renders_authored_pose_not_composed(
    handler: SeHandler, store: Store
) -> None:
    """``view='tree'`` (the default) must print each block's AUTHORED,
    parent-relative pose — the value ``set_pose`` round-trips — never the
    composed WORLD value. A child at ``[0, 0, 0.016]`` under a parent at
    ``[0, 0.1, 0.254]`` composes to ``[0, 0.1, 0.27]`` (the exact
    unicycle-c1 arithmetic); the tree view must show the former, or a
    reader who copies the printed number back into ``set_pose`` moves the
    block a second time."""
    handler.put(id="rt4", text=_NESTED)
    body = handler.get(id="rt4").body
    assert "pose=[0, 0, 0.016]" in body
    assert "pose=[0, 0.1, 0.27]" not in body


def test_block_view_renders_authored_pose_and_labelled_world_pose(
    handler: SeHandler, store: Store
) -> None:
    """``view='block'`` (the detailed single-block view) must also lead
    with the AUTHORED pose, and — since this block's parent has a
    non-zero pose, so the two genuinely differ — additionally surface the
    composed WORLD placement, clearly labelled so the two numbers can
    never be mistaken for each other."""
    handler.put(id="rt5", text=_NESTED)
    body = handler.get(id="rt5", view="block", args={"name": "crank_bolt_left"}).body
    assert "pose: [0, 0, 0.016] m" in body
    assert "authored" in body
    assert "world pose: [0, 0.1, 0.27]" in body


def test_block_view_omits_world_pose_when_it_matches_authored(
    handler: SeHandler, store: Store
) -> None:
    """A root block has no parent to compose against — world and authored
    are identical — so the block view must not grow a redundant
    ``world pose`` line for it."""
    handler.put(id="rt6", text=_NESTED)
    body = handler.get(id="rt6", view="block", args={"name": "crank_left"}).body
    assert "pose: [0, 0.1, 0.254] m" in body
    assert "world pose" not in body
