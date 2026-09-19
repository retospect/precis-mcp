"""Design revision record — design-workbench build, slice 2 ("Record"),
acceptance **S2 record** and the store half of **S2 scrubber**.

* ``StructureHandler.put``/``.edit``/``.derive`` write one
  ``design_revisions`` row per version, ``ops`` verbatim, no checkpoint
  (atoms are versioned in rows).
* ``SeHandler.put``/``.edit`` write one row per save whose snapshot is a
  ``design_checkpoints`` row (``rev-<N>``) that round-trips through
  :func:`precis_se.persist.tree_from_json` to the tree ``load_tree``
  returns — block uids included.
* ``structure_load(ref_id, version=N)`` is the scene as of save N for
  every N; ``version=None`` is unchanged (live).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import precis_se
from precis.design import history
from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler

_SE_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
            {"op": "add_bond", "i": "aPd1", "j": "aPd2", "order": 1},
        ],
    }
)

_CASTER_OPS = [
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
    {"op": "add_port", "block": "fork", "name": "p1"},
    {"op": "add_port", "block": "hub", "name": "p2"},
    # Endpoints deliberately NOT in canonical order — save_tree sorts
    # them, and the snapshot must show the rows, not the submitted tree.
    {"op": "connect", "a": "hub.p2", "b": "fork.p1"},
    {"op": "add_measure", "block": "hub", "name": "od", "min": 4e-3, "max": 8e-3},
    {
        "op": "add_note",
        "name": "q-bore",
        "kind": "question",
        "text": "what bearing bore?",
        "about": ["hub"],
    },
]


@pytest.fixture
def structure(store: Store) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


@pytest.fixture
def se(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_SE_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


def _ref_id(store: Store, kind: str, slug: str) -> int:
    ref = store.get_ref(kind=kind, id=slug)
    assert ref is not None
    return int(ref.id)


# ── S2 record: structure ───────────────────────────────────────────────────


def test_structure_edit_records_exactly_one_revision_with_ops_verbatim(
    structure: StructureHandler, store: Store
) -> None:
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store, "structure", "pd_pair")
    before = history.list_revisions(store, ref_id)
    # put itself is revision 1, carrying the ops that built the scene.
    assert [r.rev for r in before] == [1]
    assert before[0].ops == json.loads(_PD)["ops"]
    assert before[0].checkpoint_id is None

    ops = [{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}]
    structure.edit(id="pd_pair", ops=ops)

    after = history.list_revisions(store, ref_id)
    assert len(after) == len(before) + 1
    new = after[-1]
    assert new.rev == store.structure_version(ref_id) == 2
    assert new.ops == ops
    assert new.checkpoint_id is None  # atoms are versioned in rows
    assert new.turn is None  # filled by slice 3
    assert history.revision(store, ref_id, 2) == new
    assert history.revision(store, ref_id, 99) is None


def test_structure_derive_records_revision_one_of_the_child(
    structure: StructureHandler, store: Store
) -> None:
    structure.put(id="pd_pair", text=_PD)
    ops = [{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}]
    structure.derive(id="pd_pair", to="pd_pair_o", ops=ops)
    child = _ref_id(store, "structure", "pd_pair_o")
    revs = history.list_revisions(store, child)
    assert [(r.rev, r.ops) for r in revs] == [(1, ops)]
    # The parent's record is untouched by a derive.
    assert [
        r.rev
        for r in history.list_revisions(store, _ref_id(store, "structure", "pd_pair"))
    ] == [1]


# ── S2 record: se ──────────────────────────────────────────────────────────


def test_se_edit_records_one_revision_whose_snapshot_round_trips(
    se: SeHandler, store: Store
) -> None:
    se.put(id="caster1", text=json.dumps({"ops": _CASTER_OPS}))
    ref_id = _ref_id(store, "se", "caster1")
    before = history.list_revisions(store, ref_id)
    assert [r.rev for r in before] == [1]
    assert before[0].ops == _CASTER_OPS

    ops = [{"op": "add_block", "name": "cap", "parent": "hub", "pose": [0, 0, 0.02]}]
    se.edit(id="caster1", ops=ops)

    after = history.list_revisions(store, ref_id)
    assert len(after) == len(before) + 1
    new = after[-1]
    assert new.rev == 2
    assert new.ops == ops
    assert new.checkpoint_id is not None

    # The snapshot is the design_checkpoints row labelled rev-2 …
    checkpoint = history.load_checkpoint(store, ref_id, "rev-2")
    assert checkpoint is not None and checkpoint.id == new.checkpoint_id
    assert checkpoint.headline == {"rev": 2, "blocks": 3}
    # … and it round-trips to exactly what load_tree returns — uids,
    # canonicalised connect endpoints, stamped note timestamps and all.
    restored = persist.tree_from_json(checkpoint.payload, store=store)
    live = persist.load_tree(store, ref_id)
    assert restored == live
    assert {n.uid for n in restored.blocks.values()} == {
        n.uid for n in live.blocks.values()
    }
    assert all(n.uid is not None for n in restored.blocks.values())

    # rev-1 still describes the pre-edit tree (two blocks, no cap).
    first = history.load_checkpoint(store, ref_id, "rev-1")
    assert first is not None
    assert set(persist.tree_from_json(first.payload).blocks) == {"fork", "hub"}


def test_se_snapshot_rejects_a_foreign_payload() -> None:
    with pytest.raises(ValueError, match="not an se tree snapshot"):
        persist.tree_from_json({"blocks": []})


# ── S2 scrubber (store): structure_load(version=N) ─────────────────────────


def test_structure_load_at_version_returns_the_atom_count_of_that_save(
    structure: StructureHandler, store: Store
) -> None:
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store, "structure", "pd_pair")
    counts: dict[int, int] = {1: 2}
    # v2: +O (3 atoms); v3: +H (4 atoms); v4: -aPd2 (3 atoms) — a removal
    # so the filter's retired_version half is exercised, not only the
    # added_version half.
    structure.edit(
        id="pd_pair", ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}]
    )
    counts[2] = 3
    structure.edit(
        id="pd_pair", ops=[{"op": "add_atom", "element": "H", "frac": [0.5, 0.5, 0.6]}]
    )
    counts[3] = 4
    structure.edit(id="pd_pair", ops=[{"op": "vacancy", "atom": "aPd2"}])
    counts[4] = 3
    assert store.structure_version(ref_id) == 4

    for n, expected in counts.items():
        scene, handles = store.structure_load(ref_id, version=n)
        assert len(scene.atoms) == expected, f"version {n}"
        assert len(handles) == expected
    # Which atoms, not just how many.
    assert "aPd2" in store.structure_load(ref_id, version=3)[0].atoms
    assert "aPd2" not in store.structure_load(ref_id, version=4)[0].atoms
    # Bonds follow the same interval: the Pd-Pd bond exists at 1..3, gone at 4.
    assert len(store.structure_load(ref_id, version=3)[0].bonds) == 1
    assert len(store.structure_load(ref_id, version=4)[0].bonds) == 0

    # version=None is the live design, unchanged.
    live, _ = store.structure_load(ref_id)
    assert set(live.atoms) == set(store.structure_load(ref_id, version=None)[0].atoms)
    assert len(live.atoms) == counts[4]
    # Every version has its revision row, ops verbatim.
    assert [r.rev for r in history.list_revisions(store, ref_id)] == [1, 2, 3, 4]
    rev4 = history.revision(store, ref_id, 4)
    assert rev4 is not None
    assert rev4.ops == [{"op": "vacancy", "atom": "aPd2"}]
