"""Stable block identity for `se` — the uid cutover (migration
``0009_se_block_uid.sql``, ``precis_se.persist``, ``precis_se.identity``)
plus se's first rental of the shared design core's scenarios
(docs/backlog/design-state-core.md items 2 and 3, the thin rental proof).

What these pin down, in one line each: a block's uid survives the
retire-all/reinsert-all save AND a full ``put`` replace; every in-design
cross-reference is stored keyed by that uid; a load follows the uid, not
the name, so a relabelled block keeps its edges; a dangling reference
still round-trips through its name text; and validate/DRC say which
scenario governed the run.

Fixture shape lifted from ``test_se_plugin.py`` — the shared test DB
template carries only core migrations, so the plugin's own are seeded
here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.blocktree.types import OpError
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.identity import AmbiguousLabel, parse_uid, resolve_block
from precis_se.measures import MeasureSpec
from precis_se.ops import SeBlock, SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: A caster with two blocks, a connect over their ports, a measure, a BOM
#: line, an instance (local template) and a threading invariant — one of
#: every cross-reference the cutover touches.
_ALL_REFS = json.dumps(
    {
        "ops": [
            {"op": "add_block", "name": "hub", "envelope": "cyl:r0.008h0.03"},
            {"op": "add_block", "name": "wheel", "envelope": "cyl:r0.04h0.02"},
            {"op": "add_port", "block": "hub", "name": "shaft", "roles": ["mates"]},
            {"op": "add_port", "block": "wheel", "name": "bore", "roles": ["mates"]},
            {"op": "connect", "a": "hub.shaft", "b": "wheel.bore"},
            {"op": "add_measure", "block": "hub", "name": "od_d", "value": 0.016},
            {
                "op": "add_bom",
                "block": "wheel",
                "item_kind": "component",
                "item": "bearing-608",
            },
            {"op": "instance_block", "name": "wheel2", "template": "wheel"},
            {"op": "declare_threading", "a": "hub", "b": "wheel"},
        ]
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


def _ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return int(ref.id)


def _uids(store: Store, slug: str) -> dict[str, int]:
    """``name → uid`` over the design's live block rows."""
    with store.pool.connection() as c:
        rows = c.execute(
            "SELECT name, uid FROM se_blocks WHERE ref_id = %s AND retired_at IS NULL",
            (_ref_id(store, slug),),
        ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _row(store: Store, sql: str, slug: str) -> Any:
    with store.pool.connection() as c:
        return c.execute(sql, (_ref_id(store, slug),)).fetchone()


# ── the uid itself ───────────────────────────────────────────────────────


def test_every_saved_block_carries_a_uid(handler: SeHandler, store: Store) -> None:
    handler.put(id="caster1", text=_ALL_REFS)
    uids = _uids(store, "caster1")
    assert set(uids) == {"hub", "wheel", "wheel2"}
    assert all(u > 0 for u in uids.values())
    assert len(set(uids.values())) == 3


def test_uid_survives_an_edit(handler: SeHandler, store: Store) -> None:
    handler.put(id="caster1", text=_ALL_REFS)
    before = _uids(store, "caster1")
    handler.edit(
        id="caster1", ops=[{"op": "set_pose", "block": "hub", "pose": [0, 0, 1]}]
    )
    assert _uids(store, "caster1") == before


def test_uid_survives_a_full_put_replace(handler: SeHandler, store: Store) -> None:
    """``put`` builds a FRESH tree that has never seen the stored rows —
    the block that keeps its label keeps its identity anyway, because save
    adopts the live row's uid rather than minting a second one."""
    handler.put(id="caster1", text=_ALL_REFS)
    before = _uids(store, "caster1")
    handler.put(
        id="caster1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "hub", "envelope": "cyl:r0.009h0.03"},
                    {"op": "add_block", "name": "fork", "envelope": "box:w1d1h1"},
                ]
            }
        ),
    )
    after = _uids(store, "caster1")
    assert after["hub"] == before["hub"]
    # A block that wasn't there before is a new block, new uid — never a
    # recycled one (uids are never reused).
    assert after["fork"] not in before.values()


def test_new_block_mints_a_fresh_uid(handler: SeHandler, store: Store) -> None:
    handler.put(id="caster1", text=_ALL_REFS)
    before = _uids(store, "caster1")
    handler.edit(id="caster1", ops=[{"op": "add_block", "name": "cap"}])
    after = _uids(store, "caster1")
    assert after["cap"] > max(before.values())


def test_retired_rows_keep_the_same_uid(handler: SeHandler, store: Store) -> None:
    """The save model retires and reinserts every row; the uid is what
    makes those rows one block's history rather than N blocks."""
    handler.put(id="caster1", text=_ALL_REFS)
    live = _uids(store, "caster1")["hub"]
    handler.edit(
        id="caster1", ops=[{"op": "set_pose", "block": "hub", "pose": [1, 0, 0]}]
    )
    with store.pool.connection() as c:
        rows = c.execute(
            "SELECT DISTINCT uid FROM se_blocks WHERE ref_id = %s AND name = 'hub'",
            (_ref_id(store, "caster1"),),
        ).fetchall()
    assert [int(r[0]) for r in rows] == [live]


def test_block_view_shows_the_uid(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_ALL_REFS)
    body = handler.get(id="caster1", view="block", args={"name": "hub"}).body
    assert "(uid #" in body.splitlines()[0]


# ── cross-references are stored by uid ───────────────────────────────────


def test_connect_endpoints_are_stored_by_uid(handler: SeHandler, store: Store) -> None:
    handler.put(id="caster1", text=_ALL_REFS)
    uids = _uids(store, "caster1")
    row = _row(
        store,
        "SELECT a_block, a_block_uid, b_block, b_block_uid FROM se_connects "
        "WHERE ref_id = %s AND retired_at IS NULL",
        "caster1",
    )
    assert row is not None
    stored = {row[0]: row[1], row[2]: row[3]}
    assert stored == {"hub": uids["hub"], "wheel": uids["wheel"]}


def test_measure_bom_threading_and_template_are_stored_by_uid(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="caster1", text=_ALL_REFS)
    uids = _uids(store, "caster1")
    measure = _row(
        store,
        "SELECT block_uid FROM se_measures WHERE ref_id = %s AND retired_at IS NULL",
        "caster1",
    )
    assert measure is not None and measure[0] == uids["hub"]
    bom = _row(
        store,
        "SELECT block_uid FROM se_bom WHERE ref_id = %s AND retired_at IS NULL",
        "caster1",
    )
    assert bom is not None and bom[0] == uids["wheel"]
    thread = _row(
        store,
        "SELECT subject_uid, object_uid FROM se_topology "
        "WHERE ref_id = %s AND retired_at IS NULL",
        "caster1",
    )
    assert thread is not None and list(thread) == [uids["hub"], uids["wheel"]]
    template = _row(
        store,
        "SELECT template_uid FROM se_blocks "
        "WHERE ref_id = %s AND retired_at IS NULL AND name = 'wheel2'",
        "caster1",
    )
    assert template is not None and template[0] == uids["wheel"]


def test_cross_design_template_keeps_its_name_text(
    handler: SeHandler, store: Store
) -> None:
    """A ``'slug#block'`` reference has no uid half yet — resolution lives
    in ``precis.blocktree`` and walks foreign trees by slug (persist's
    module docstring). It must still round-trip untouched."""
    handler.put(
        id="lib1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "wheel", "envelope": "cyl:r0.04h0.02"}
                ]
            }
        ),
    )
    handler.put(
        id="cart1",
        text=json.dumps(
            {"ops": [{"op": "instance_block", "name": "w1", "template": "lib1#wheel"}]}
        ),
    )
    row = _row(
        store,
        "SELECT template_ref, template_uid FROM se_blocks "
        "WHERE ref_id = %s AND retired_at IS NULL AND name = 'w1'",
        "cart1",
    )
    assert row is not None
    assert row[0] == "lib1#wheel"
    assert row[1] is None
    tree = persist.load_tree(store, _ref_id(store, "cart1"))
    assert tree.blocks["w1"].template == "lib1#wheel"


# ── the uid is what a load follows ───────────────────────────────────────


def test_load_follows_the_uid_not_the_name(handler: SeHandler, store: Store) -> None:
    """THE cutover test. Relabel the block row behind persist's back (a
    stand-in for the relabel op this makes possible): every
    cross-reference follows the uid, so nothing is left pointing at a name
    that no longer exists."""
    handler.put(id="caster1", text=_ALL_REFS)
    ref_id = _ref_id(store, "caster1")
    with store.tx() as c:
        c.execute(
            "UPDATE se_blocks SET name = 'hub_v2' "
            "WHERE ref_id = %s AND retired_at IS NULL AND name = 'hub'",
            (ref_id,),
        )
    tree = persist.load_tree(store, ref_id)
    assert set(tree.blocks) == {"hub_v2", "wheel", "wheel2"}
    (connect,) = tree.connects
    assert {connect.a_block, connect.b_block} == {"hub_v2", "wheel"}
    assert [m.block for m in tree.measures] == ["hub_v2"]
    assert [t.a for t in tree.threading] == ["hub_v2"]


def test_dangling_reference_round_trips_through_its_name(
    handler: SeHandler, store: Store
) -> None:
    """A reference to a block that isn't there gets no uid (there is none
    to get) and keeps its name text — it is a read-time DRC finding, and a
    finding needs a subject to name."""
    handler.put(
        id="caster1",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "hub", "envelope": "cyl:r0.008h0.03"}]}
        ),
    )
    ref_id = _ref_id(store, "caster1")
    tree = persist.load_tree(store, ref_id)
    tree.measures.append(MeasureSpec(block="ghost", name="od_d", value=0.01))
    persist.save_tree(store, ref_id=ref_id, tree=tree, card_text="x")
    row = _row(
        store,
        "SELECT block, block_uid FROM se_measures "
        "WHERE ref_id = %s AND retired_at IS NULL",
        "caster1",
    )
    assert row is not None and row[0] == "ghost" and row[1] is None
    assert [m.block for m in persist.load_tree(store, ref_id).measures] == ["ghost"]


def test_branch_style_copy_preserves_uids(handler: SeHandler, store: Store) -> None:
    """The MECHANISM a branch would be built on: copying a design's tree
    into another ref keeps every uid, which is what would make the two
    diff block-by-block (design-state-core.md item 5 — the ``pin``→
    ``branch`` verb itself lands with the design-history wiring). Two live
    designs sharing a uid is therefore legal: uniqueness is per
    ``(ref_id, uid)``."""
    handler.put(id="caster1", text=_ALL_REFS)
    source = persist.load_tree(store, _ref_id(store, "caster1"))
    handler.put(id="caster1-b", text=json.dumps({"ops": []}))
    persist.save_tree(
        store, ref_id=_ref_id(store, "caster1-b"), tree=source, card_text="branch"
    )
    assert _uids(store, "caster1-b") == _uids(store, "caster1")


# ── mint vs adopt: which uid a uid-less node gets ────────────────────────


def test_edit_remove_then_re_add_mints_a_new_uid(
    handler: SeHandler, store: Store
) -> None:
    """gr339743. An in-memory ``remove_block`` never retires the DB row —
    the retire pass runs at save, over the tree the whole edit produced —
    so a same-label ``add_block`` in the SAME call would inherit the dead
    block's identity if a uid-less node could adopt by label on the edit
    path. It is a different block: different uid, and the old one gone."""
    handler.put(id="caster1", text=_ALL_REFS)
    handler.edit(id="caster1", ops=[{"op": "add_block", "name": "cap"}])
    before = _uids(store, "caster1")["cap"]
    handler.edit(
        id="caster1",
        ops=[
            {"op": "remove_block", "block": "cap"},
            {"op": "add_block", "name": "cap", "envelope": "box:w0.01d0.01h0.01"},
        ],
    )
    after = _uids(store, "caster1")
    assert after["cap"] != before
    assert before not in after.values()


def test_clear_and_rebuild_edit_still_mints(handler: SeHandler, store: Store) -> None:
    """The mint-vs-adopt decision keys off the tree's ORIGIN
    (``from_persistence``), never its shape: an edit that removes EVERY
    uid-carrying block before re-adding a same-named one leaves the tree
    wholly uid-less, and a shape test (``all(uid is None)``) would flip
    it back to adopt-by-label — resurrecting gr339743 exactly there."""
    handler.put(id="caster1", text=_ALL_REFS)
    before = _uids(store, "caster1")
    # Reverse creation order: an instance must go before its template.
    rebuild: list[dict[str, Any]] = [
        {"op": "remove_block", "block": name} for name in reversed(list(before))
    ]
    rebuild.append(
        {"op": "add_block", "name": "hub", "envelope": "box:w0.01d0.01h0.01"}
    )
    handler.edit(id="caster1", ops=rebuild)
    after = _uids(store, "caster1")
    assert set(after) == {"hub"}
    assert after["hub"] != before["hub"]
    assert after["hub"] not in before.values()


def test_edit_keeps_every_untouched_block_s_uid(
    handler: SeHandler, store: Store
) -> None:
    """The other half of the same rule: minting-not-adopting on the edit
    path must not churn the blocks the edit never mentioned (they arrive
    carrying their uids, so nothing about them is uid-less).

    There is no rename op today — a relabel that keeps the uid is exercised
    over the stored rows instead, by
    ``test_load_follows_the_uid_not_the_name``."""
    handler.put(id="caster1", text=_ALL_REFS)
    before = _uids(store, "caster1")
    handler.edit(id="caster1", ops=[{"op": "add_block", "name": "cap"}])
    after = _uids(store, "caster1")
    assert {k: v for k, v in after.items() if k != "cap"} == before


# ── addressing: uid always, label when unambiguous ───────────────────────


@pytest.mark.parametrize(
    ("token", "expected"),
    [(41, 41), ("#41", 41), ("uid:41", 41), (" #41 ", 41), ("hub", None), ("#", None)],
)
def test_parse_uid_token_forms(token: object, expected: int | None) -> None:
    assert parse_uid(token) == expected


def _resolved(tree: SeTree, token: object) -> SeBlock:
    node = resolve_block(tree, token)
    assert node is not None
    return node


def test_resolve_block_by_uid_and_by_label(handler: SeHandler, store: Store) -> None:
    handler.put(id="caster1", text=_ALL_REFS)
    tree = persist.load_tree(store, _ref_id(store, "caster1"))
    uid = _uids(store, "caster1")["hub"]
    assert _resolved(tree, "hub").name == "hub"
    assert _resolved(tree, f"#{uid}").name == "hub"
    assert _resolved(tree, uid).name == "hub"
    assert resolve_block(tree, "ghost") is None


def test_a_digit_label_is_a_label_first() -> None:
    """A block actually called '12' resolves to itself; only a label that
    matches nothing is re-read as a bare uid."""
    tree = SeTree()
    tree.blocks["12"] = SeBlock(name="12", uid=99)
    tree.blocks["rim"] = SeBlock(name="rim", uid=12)
    assert _resolved(tree, "12").name == "12"
    assert _resolved(tree, "#12").name == "rim"


def test_resolve_key_on_a_diverged_tree_returns_the_holding_key() -> None:
    """A paste or branch merge can leave a node under a mapping key that
    is not its name. Identity is the NODE: resolving its label must hand
    back the key actually holding it, and the stray key itself still
    resolves via the dict fallback."""
    tree = SeTree()
    tree.blocks["strut-2"] = SeBlock(name="zed", uid=9)
    assert tree.resolve_key("zed") == "strut-2"
    assert _resolved(tree, "strut-2").uid == 9


def test_ambiguous_label_lists_the_matching_uids() -> None:
    """Label uniqueness still holds in the DB, so two blocks under one
    label is an in-memory state (a paste, a branch merge) — the path that
    makes relaxing that index a migration rather than a redesign. Both
    blocks are *named* 'strut'; only their mapping keys differ."""
    tree = SeTree()
    tree.blocks["strut"] = SeBlock(name="strut", uid=7)
    tree.blocks["strut-2"] = SeBlock(name="strut", uid=9)
    with pytest.raises(AmbiguousLabel) as exc:
        resolve_block(tree, "strut")
    assert exc.value.uids == [7, 9]
    assert "#7" in str(exc.value) and "#9" in str(exc.value)
    # The by-uid example the message teaches with is the SMALLEST uid.
    assert "e.g. '#7'" in str(exc.value)
    # By uid it is never ambiguous — that is the whole point.
    assert _resolved(tree, "#9").uid == 9


def test_block_view_addresses_by_uid(handler: SeHandler, store: Store) -> None:
    handler.put(id="caster1", text=_ALL_REFS)
    uid = _uids(store, "caster1")["wheel"]
    body = handler.get(id="caster1", view="block", args={"name": f"#{uid}"}).body
    assert body.startswith("# block 'wheel'")


# ── ops take a uid wherever they take a block ────────────────────────────


def test_ops_address_existing_blocks_by_uid(handler: SeHandler, store: Store) -> None:
    """Item 2's "ops accept uid always", end to end: one edit call whose
    every block reference is a uid token — including the block half of a
    ``'block.port'`` endpoint — and whose stored result names the blocks by
    their LABELS, because a uid token resolves to the block, not to a
    second way of spelling its name."""
    handler.put(id="caster1", text=_ALL_REFS)
    uids = _uids(store, "caster1")
    hub, wheel = uids["hub"], uids["wheel"]
    handler.edit(
        id="caster1",
        ops=[
            {"op": "set_pose", "block": f"#{hub}", "pose": [0, 0, 0.1]},
            {"op": "set_envelope", "block": f"uid:{hub}", "envelope": "cyl:r0.01h0.03"},
            {"op": "add_port", "block": hub, "name": "tip", "roles": ["mates"]},
            {"op": "add_port", "block": f"#{wheel}", "name": "rim", "roles": ["mates"]},
            {"op": "connect", "a": f"#{hub}.tip", "b": f"#{wheel}.rim"},
            {"op": "add_measure", "block": f"#{hub}", "name": "bore_d", "value": 0.004},
            {"op": "set_measure", "block": f"#{hub}", "name": "bore_d", "value": 0.005},
            {
                "op": "add_bom",
                "block": f"#{wheel}",
                "item_kind": "component",
                "item": "tire-1",
            },
            {"op": "remove_threading", "a": f"#{hub}", "b": f"#{wheel}"},
            {"op": "declare_threading", "a": f"#{wheel}", "b": f"#{hub}"},
        ],
    )
    # Nothing was re-minted: every op addressed a block that already existed.
    assert _uids(store, "caster1") == uids
    tree = persist.load_tree(store, _ref_id(store, "caster1"))
    assert tree.blocks["hub"].pose == [0.0, 0.0, 0.1]
    assert tree.blocks["hub"].envelope == "cyl:r0.01h0.03"
    assert "tip" in tree.blocks["hub"].ports
    new_connect = next(c for c in tree.connects if c.a_port == "tip")
    assert (new_connect.a_block, new_connect.b_block) == ("hub", "wheel")
    assert [m.value for m in tree.measures if m.name == "bore_d"] == [0.005]
    assert [line.block for line in tree.bom if line.item == "tire-1"] == ["wheel"]
    assert [(t.a, t.b) for t in tree.threading] == [("wheel", "hub")]


def test_set_binding_and_remove_block_take_a_uid(
    handler: SeHandler, store: Store
) -> None:
    """The two ops with their own cascades: a realization binding lands on
    the block the uid names, and removing by uid takes the subtree with
    it."""
    handler.put(id="caster1", text=_ALL_REFS)
    uids = _uids(store, "caster1")
    handler.edit(
        id="caster1",
        ops=[
            {
                "op": "set_binding",
                "block": f"#{uids['hub']}",
                "kind": "cad",
                "design": "hub-cad",
            },
            {"op": "remove_block", "block": f"#{uids['wheel2']}"},
        ],
    )
    tree = persist.load_tree(store, _ref_id(store, "caster1"))
    assert (tree.blocks["hub"].bound_kind, tree.blocks["hub"].bound) == (
        "cad",
        "hub-cad",
    )
    assert set(tree.blocks) == {"hub", "wheel"}


def test_instance_by_uid_template_stores_the_label(
    handler: SeHandler, store: Store
) -> None:
    """A LOCAL ``template`` is a block token too — and what lands in
    ``template_ref`` is the label, since that column is read back by name
    (a cross-design reference has no uid half yet)."""
    handler.put(id="caster1", text=_ALL_REFS)
    uids = _uids(store, "caster1")
    handler.edit(
        id="caster1",
        ops=[
            {
                "op": "array_block",
                "name": "spokes",
                "template": f"#{uids['hub']}",
                "polar": {"count": 4, "radius": 0.02},
            }
        ],
    )
    row = _row(
        store,
        "SELECT template_ref, template_uid FROM se_blocks "
        "WHERE ref_id = %s AND retired_at IS NULL AND name = 'spokes'",
        "caster1",
    )
    assert row is not None
    assert row[0] == "hub"
    assert row[1] == uids["hub"]


def test_an_op_by_ambiguous_label_reports_both_uids(handler: SeHandler) -> None:
    """The ambiguity error is the ops layer's own :class:`OpError`, so it
    reaches the caller as a retryable ``BadInput`` with the uid list
    intact rather than as an internal error — and by uid the same op
    works."""
    tree = SeTree()
    tree.blocks["strut"] = SeBlock(name="strut", uid=7)
    tree.blocks["strut-2"] = SeBlock(name="strut", uid=9)
    with pytest.raises(AmbiguousLabel) as exc:
        apply_ops(tree, [{"op": "set_pose", "block": "strut", "pose": [1, 0, 0]}])
    assert isinstance(exc.value, OpError)
    assert exc.value.uids == [7, 9]
    apply_ops(tree, [{"op": "set_pose", "block": "#9", "pose": [1, 0, 0]}])
    assert tree.blocks["strut-2"].pose == [1.0, 0.0, 0.0]


def test_a_block_may_not_be_named_like_a_uid_token(handler: SeHandler) -> None:
    """The other side of the addressing bargain: a name that would parse
    as a uid is refused at mint time, at both places a name is minted —
    otherwise it could be created and never addressed again."""
    handler.put(id="caster1", text=_ALL_REFS)
    with pytest.raises(BadInput) as exc:
        handler.edit(id="caster1", ops=[{"op": "add_block", "name": "uid:41"}])
    assert "uid:" in str(exc.value)
    with pytest.raises(BadInput):
        handler.edit(
            id="caster1",
            ops=[{"op": "instance_block", "name": "UID:9", "template": "wheel"}],
        )
    # An ordinary name that merely CONTAINS the word is still fine.
    handler.edit(id="caster1", ops=[{"op": "add_block", "name": "guid:plate"}])


# ── the rental proof: which scenario governed ────────────────────────────


def test_put_records_the_governing_scenario(handler: SeHandler) -> None:
    handler.put(
        id="caster1", text=json.dumps({"scenario": "mass_production", "ops": []})
    )
    body = handler.get(id="caster1", view="validate").body
    assert "scenario: mass_production" in body
    assert "100000 off" in body
    assert "lifetime checks ON" in body


def test_presets_differ_in_weights_and_lifetime(handler: SeHandler) -> None:
    """The acceptance criterion: prototype vs small_batch store different
    objective weights and lifetime, and validate says which governed."""
    handler.put(id="p1", text=json.dumps({"scenario": "prototype", "ops": []}))
    handler.put(id="s1", text=json.dumps({"scenario": "small_batch", "ops": []}))
    proto = handler.get(id="p1", view="validate").body
    batch = handler.get(id="s1", view="validate").body
    assert "lifetime checks off" in proto and "lead_time 0.6" in proto
    assert "lifetime checks ON" in batch and "cost 0.4" in batch


def test_no_scenario_says_so_rather_than_defaulting(handler: SeHandler) -> None:
    handler.put(id="caster1", text=_ALL_REFS)
    assert "scenario: none chosen" in handler.get(id="caster1", view="validate").body


def test_drc_view_records_the_scenario_too(handler: SeHandler) -> None:
    handler.put(id="caster1", text=json.dumps({"scenario": "prototype", "ops": []}))
    assert "scenario: prototype" in handler.get(id="caster1", view="drc").body


def test_unknown_scenario_is_rejected_before_anything_is_written(
    handler: SeHandler, store: Store
) -> None:
    with pytest.raises(BadInput) as exc:
        handler.put(id="caster1", text=json.dumps({"scenario": "someday", "ops": []}))
    assert "mass_production" in str(exc.value)
    assert store.get_ref(kind="se", id="caster1") is None


def test_scenario_survives_a_later_put(handler: SeHandler) -> None:
    """``put`` replaces the block tree; the scenario is design-level
    context, not part of that tree, so an absent key leaves it standing."""
    handler.put(id="caster1", text=json.dumps({"scenario": "prototype", "ops": []}))
    handler.put(id="caster1", text=_ALL_REFS)
    assert "scenario: prototype" in handler.get(id="caster1", view="validate").body
