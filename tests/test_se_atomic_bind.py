"""se **atomic mode** — the store-aware half: bindings, generate, readouts.

docs/backlog/nm-se-merge.md's first in-scope item, second half. The three
ops that genuinely need the store (``bind_structure``/
``unbind_structure``/``generate``) and the two views that hydrate one
(``view='mechanics'``/``view='literature'``) came across from
``precis_nm.handler`` into :mod:`precis_se.atomic` — bind/generate/apply/
render — plus the chemistry findings in
:mod:`precis_se.atomic.validate`. This file is nm's own suite for that
material, ported onto :class:`~precis_se.handler.SeHandler`.

Three things differ from the nm originals, deliberately, and are pinned as
such:

1. **Envelopes are canonical metres.** nm required a unit on every
   hand-authored envelope (``sphere:r2Å``); se's ``add_block`` speaks
   canonical/storage mode, so an authored envelope here is bare metres
   (``sphere:r2e-10``). The Å text a *generator* emits still exists and
   still converts — at the one boundary
   (:func:`precis_se.atomic.generate.ingest_envelope`), pinned below.
2. **A connect states its ``kind`` explicitly** — nm defaulted to
   ``'bond'``; an se connect with no ``kind`` is the ordinary structural
   edge, so every bond here says so.
3. **One finding per problem.** The rules nm's validator shared with se's
   own stayed in :mod:`precis_se.validate` (its
   ``undeclared_interpenetration`` supersedes nm's ``envelope_overlap``,
   its ``block_without_envelope`` nm's ``blocks_without_envelope``), so
   the ported assertions name se's rules.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

import precis_se
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.structure import StructureHandler
from precis.store import ChunkInsert, Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_se import persist
from precis_se.atomic import render as se_atomic_render
from precis_se.atomic import validate as se_atomic_validate
from precis_se.atomic.apply import HANDLER_LEVEL_OPS, all_op_names
from precis_se.atomic.generators import GENERATORS
from precis_se.atomic.generators.sp2 import VDW_MARGIN_A
from precis_se.handler import SeHandler
from precis_se.ops import ConnectSpec, PortSpec, SeBlock, SeTree, known_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


def _seed_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _seed_se_migrations(store)
    return SeHandler(hub=hub)


@pytest.fixture
def lit_handler(store: Store) -> SeHandler:
    """A handler wired to a hub with **no embedder** — the
    ``test_structure_literature_provenance.py`` precedent: search degrades
    to lexical only, so a literature-view assertion never depends on
    mock-embedding cosine noise."""
    _seed_se_migrations(store)
    return SeHandler(hub=Hub(store=store))


@pytest.fixture
def structure(store: Store) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


def _mol_cell_payload(size: float = 20.0) -> dict[str, object]:
    return {"a": size, "b": size, "c": size, "pbc": [False, False, False]}


def _make_structure(
    structure: StructureHandler, slug: str, *, carts: list[list[float]] | None = None
) -> list[str]:
    """A tiny all-carbon structure design at the given ``carts`` (default:
    two atoms comfortably near the origin) — returns the minted atom labels
    in insertion order."""
    carts = carts if carts is not None else [[0.0, 0.0, 0.0], [1.3, 0.0, 0.0]]
    structure.put(
        id=slug,
        text=json.dumps(
            {
                "cell": _mol_cell_payload(),
                "ops": [
                    {"op": "add_atom", "element": "C", "cart": cart} for cart in carts
                ],
            }
        ),
    )
    return [f"aC{i}" for i in range(1, len(carts) + 1)]


def _make_cn_structure(structure: StructureHandler, slug: str) -> tuple[str, str]:
    """A two-atom C/N design; returns its two atom labels (the element gate
    needs a scene where two atoms differ)."""
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


def _seed_paper(store: Store, *, slug: str, title: str, body: str) -> None:
    ref = store.insert_ref(kind="paper", slug=slug, title=title, meta={})
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=body)])


def _assert_no_error_findings(body: str) -> None:
    """``view='validate'`` renders "✓ no validator findings" when the list
    is empty outright, or a "# N error(s), M warning(s)" header otherwise
    — both are "clean" as long as N is 0. A generated+bound block is
    expected to carry warn-tier findings (``unconnected_port`` on an
    as-yet-unwired rim port), never error-tier ones."""
    assert "✓ no validator findings" in body or "# 0 error(s)" in body, body


_TREE = json.dumps(
    {
        "description": "a rotaxane axle with a threaded crown macrocycle",
        "ops": [
            {
                "op": "add_block",
                "name": "axle",
                "envelope": "cyl:r2e-10h2e-09",
                "desc": "the threading rod",
            },
            {
                "op": "add_block",
                "name": "hub",
                "parent": "axle",
                "envelope": "sphere:r3e-10",
                "use": "stopper",
            },
        ],
    }
)


# ── the op roster: the 3 store-aware ops are dispatchable and named ──────


def test_the_three_store_aware_ops_are_in_the_roster_but_not_the_pure_table() -> None:
    """They never reach ``ops.apply_ops`` (it is store-free), so the pure
    table can't see them — the union is what ``put``/``edit`` accept."""
    assert set(HANDLER_LEVEL_OPS) == {
        "bind_structure",
        "unbind_structure",
        "generate",
    }
    assert set(HANDLER_LEVEL_OPS).isdisjoint(known_ops())
    assert set(HANDLER_LEVEL_OPS) <= all_op_names()


def test_unknown_op_roster_includes_every_registered_op(handler: SeHandler) -> None:
    """gripe 334767: the roster used to come from ``known_ops()`` alone,
    silently omitting the 3 store-aware ops from what was accepted."""
    handler.put(
        id="roster1", text=json.dumps({"ops": [{"op": "add_block", "name": "a"}]})
    )
    with pytest.raises(BadInput, match="unknown op") as exc_info:
        handler.edit(id="roster1", ops=[{"op": "levitate"}])
    msg = str(exc_info.value)
    for name in all_op_names():
        assert name in msg, f"{name!r} missing from unknown-op roster: {msg}"


def test_an_op_dict_with_no_op_key_is_rejected_before_dispatch(
    handler: SeHandler,
) -> None:
    """``apply_ops_with_atomic`` vets every op dict against the FULL roster
    up front (gripe 334767's own guard, module docstring) — an op missing
    its ``'op'`` key entirely must be rejected by name, not silently pass
    through to a KeyError on ``op["op"]``."""
    handler.put(
        id="noopkey1", text=json.dumps({"ops": [{"op": "add_block", "name": "a"}]})
    )
    with pytest.raises(BadInput, match="op missing 'op' key"):
        handler.edit(id="noopkey1", ops=[{"name": "b"}])


# ── gripe 334766: unknown args= keys are a loud reject, per view ─────────


def test_view_args_state_key_rejected_on_unsupported_view(handler: SeHandler) -> None:
    """``state`` poses declared block states (blocktree slice 2) only on
    view='tree'|'block'|'clearance' — on any other view it still gets its
    own pointed rejection, naming which views DO support it, rather than
    the generic "unknown args key" message."""
    handler.put(id="rotax1", text=_TREE)
    with pytest.raises(BadInput, match="state is not supported on view='drc'"):
        handler.get(id="rotax1", view="drc", args={"state": {"hub": "open"}})


def test_view_args_state_must_be_a_dict(handler: SeHandler) -> None:
    """A non-object ``state`` is now accepted syntactically by the views
    that support posing — and rejected for its OWN shape, not the old
    blanket "unshipped" message."""
    handler.put(id="rotax1", text=_TREE)
    with pytest.raises(BadInput, match="args.state must be a JSON object"):
        handler.get(id="rotax1", args={"state": "open"})
    with pytest.raises(BadInput, match="args.state must be a JSON object"):
        handler.get(id="rotax1", view="block", args={"name": "hub", "state": "open"})


def test_view_args_arbitrary_junk_key_rejected(handler: SeHandler) -> None:
    handler.put(id="rotax1", text=_TREE)
    with pytest.raises(BadInput, match="unknown args key"):
        handler.get(id="rotax1", args={"bogus": "1"})
    with pytest.raises(BadInput, match="unknown args key"):
        handler.get(id="rotax1", view="topology", args={"bogus": "1"})


def test_view_args_a_clearance_key_is_rejected_on_a_view_that_ignores_it(
    handler: SeHandler,
) -> None:
    """The gripe's own shape: a key a DIFFERENT view accepts is still junk
    here, and used to be dropped silently."""
    handler.put(id="rotax1", text=_TREE)
    with pytest.raises(BadInput, match="unknown args key"):
        handler.get(id="rotax1", view="drc", args={"a": "axle", "b": "hub"})


def test_view_args_legitimate_keys_still_work(handler: SeHandler) -> None:
    handler.put(id="rotax1", text=_TREE)
    assert "hub" in handler.get(id="rotax1", view="block", args={"name": "hub"}).body
    assert handler.get(id="rotax1", view="ports").body
    assert handler.get(id="rotax1", view="validate").body
    assert handler.get(id="rotax1", view="topology").body
    assert handler.get(id="rotax1", view="mechanics").body
    assert handler.get(id="rotax1").body


def test_the_two_atomic_views_are_declared_and_named_in_the_unknown_hint(
    handler: SeHandler,
) -> None:
    assert {"mechanics", "literature"} <= set(SeHandler.spec.views)
    handler.put(id="rotax1", text=_TREE)
    with pytest.raises(BadInput) as exc:
        handler.get(id="rotax1", view="nope")
    hint = str(exc.value.next or "")
    assert "view='mechanics'" in hint
    assert "view='literature'" in hint


# ── bind_structure / unbind_structure ────────────────────────────────────


def test_bind_structure_happy_path(
    handler: SeHandler, structure: StructureHandler
) -> None:
    c_label, _n_label = _make_cn_structure(structure, "frag1")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
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
    # the binding lands on se's OWN L3 pair, not a second column
    assert "realization: structure:frag1" in block.body
    assert f"frag1:{c_label}" in block.body


def test_bind_structure_port_atom_map_persists_across_a_second_save(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """The landmine: ``se_blocks.id`` is rebuilt on every ``save_tree``, so
    the binding has to survive keyed by name/design/atom, never by row
    id."""
    c_label, _n = _make_cn_structure(structure, "frag2")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
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
    assert "realization: structure:frag2" in block.body
    assert f"frag2:{c_label}" in block.body


def test_bind_structure_rebind_to_a_different_design_clears_stale_ports(
    handler: SeHandler, structure: StructureHandler, store: Store
) -> None:
    """Since ``structure``'s label minting restarts at ``aC1`` for every
    fresh design, fragB genuinely has an atom labelled the same as fragA's
    — so a stale binding wouldn't even raise, it would silently resolve to
    the WRONG atom. This test relies on exactly that collision."""
    c_a, _n_a = _make_cn_structure(structure, "fragA")
    c_b, _n_b = _make_cn_structure(structure, "fragB")
    assert c_a == c_b  # the label-collision precondition this test needs

    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
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

    ref = store.get_ref(kind="se", id="bind10")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    hub = tree.blocks["hub"]
    assert (hub.bound_kind, hub.bound) == ("structure", "fragB")
    assert hub.ports["p2"].bound_design == "fragB"
    assert hub.ports["p2"].bound_atom == c_b
    assert hub.ports["p1"].bound_design is None
    assert hub.ports["p1"].bound_atom is None

    resp = handler.get(id="bind10", view="validate")
    assert "dangling_binding" not in resp.body
    assert "binding_element_mismatch" not in resp.body


def test_bind_structure_to_the_same_design_is_incremental(
    handler: SeHandler, structure: StructureHandler, store: Store
) -> None:
    c_label, n_label = _make_cn_structure(structure, "fragC")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
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
    ref = store.get_ref(kind="se", id="bind11")
    assert ref is not None
    hub = persist.load_tree(store, ref.id).blocks["hub"]
    assert hub.ports["p1"].bound_atom == c_label
    assert hub.ports["p2"].bound_atom == n_label


def test_bind_structure_expected_element_mismatch_rejected(
    handler: SeHandler, structure: StructureHandler
) -> None:
    c_label, _n = _make_cn_structure(structure, "frag3")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
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


def test_bind_structure_missing_block_key_raises(handler: SeHandler) -> None:
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "bind_structure", "design": "no-such-design"},
    ]
    with pytest.raises(BadInput, match="bind_structure needs 'block'"):
        handler.put(id="bindnoblock", text=json.dumps({"ops": ops}))


def test_bind_structure_unknown_design_raises(handler: SeHandler) -> None:
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "bind_structure", "block": "hub", "design": "no-such-design"},
    ]
    with pytest.raises(NotFound, match="no structure design"):
        handler.put(id="bind4", text=json.dumps({"ops": ops}))


def test_bind_structure_unknown_port_raises(
    handler: SeHandler, structure: StructureHandler
) -> None:
    c_label, _n = _make_cn_structure(structure, "frag5")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
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
    handler: SeHandler, structure: StructureHandler
) -> None:
    _make_cn_structure(structure, "frag6")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
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


def test_bind_structure_unknown_block_raises(
    handler: SeHandler, structure: StructureHandler
) -> None:
    _make_cn_structure(structure, "frag6b")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "bind_structure", "block": "ghost", "design": "frag6b"},
    ]
    with pytest.raises(NotFound, match="no such block") as exc:
        handler.put(id="bind6b", text=json.dumps({"ops": ops}))
    assert "hub" in str(exc.value)  # the roster names what DOES exist


def test_bind_structure_whitespace_block_is_a_missing_arg(
    handler: SeHandler,
) -> None:
    """A blank 'block' is a missing argument, not a lookup miss — the
    agent is told to supply the arg, never shown a roster for ''."""
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "bind_structure", "block": "   ", "design": "whatever"},
    ]
    with pytest.raises(BadInput, match="needs 'block'"):
        handler.put(id="bind6w", text=json.dumps({"ops": ops}))


def test_bind_structure_on_an_instance_points_at_its_template(
    handler: SeHandler, structure: StructureHandler
) -> None:
    _make_cn_structure(structure, "frag7")
    ops = [
        {"op": "add_block", "name": "tmpl", "envelope": "sphere:r2e-10"},
        {"op": "instance_block", "name": "inst", "template": "tmpl"},
        {"op": "bind_structure", "block": "inst", "design": "frag7"},
    ]
    with pytest.raises(BadInput, match="instance") as exc:
        handler.put(id="bind7", text=json.dumps({"ops": ops}))
    assert "tmpl" in str(exc.value)


def test_unbind_structure_clears_the_block_and_every_port(
    handler: SeHandler, structure: StructureHandler
) -> None:
    c_label, _n = _make_cn_structure(structure, "frag8")
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag8",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="bind8", text=json.dumps({"ops": ops}))
    resp = handler.edit(id="bind8", ops=[{"op": "unbind_structure", "block": "hub"}])
    assert "1 port binding(s) cleared" in resp.body
    block = handler.get(id="bind8", view="block", args={"name": "hub"})
    assert "realization: — (envelope only)" in block.body
    assert "frag8" not in block.body


def test_unbind_structure_refuses_a_binding_that_is_not_chemistry(
    handler: SeHandler,
) -> None:
    """se blocks bind five kinds; the atomic verb only ever clears the
    atomistic one — silently dropping a ``component`` binding through
    ``unbind_structure`` would be a surprising write."""
    ops = [
        {"op": "add_block", "name": "brg", "envelope": "cyl:r0.011h0.007"},
        {
            "op": "set_binding",
            "block": "brg",
            "kind": "component",
            "design": "6001-2rs",
        },
    ]
    handler.put(id="bind9", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="not a structure"):
        handler.edit(id="bind9", ops=[{"op": "unbind_structure", "block": "brg"}])


def test_a_structure_binding_and_atomic_mode_agree_in_drc(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """The mode↔binding coupling the merge built, exercised through the
    real bind op rather than ``set_binding``: an atomic-mode block bound to
    a structure design yields no ``mode_binding_mismatch``, and the same
    bind with no mode yields the warn-tier one."""
    c_label, _n = _make_cn_structure(structure, "frag_mode")
    base: list[dict[str, object]] = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_mode",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="mode_no", text=json.dumps({"ops": base}))
    assert "mode_binding_mismatch" in handler.get(id="mode_no", view="drc").body

    handler.put(
        id="mode_yes",
        text=json.dumps(
            {"ops": [*base, {"op": "set_mode", "block": "hub", "mode": "atomic"}]}
        ),
    )
    assert "mode_binding_mismatch" not in handler.get(id="mode_yes", view="drc").body


# ── generate: the deterministic fill path, end to end ────────────────────


def test_the_generator_registry_is_reachable_from_se() -> None:
    assert set(GENERATORS) == {"cnt", "fullerene", "cone", "cyclodextrin", "hexfold"}


def test_unknown_generator_names_the_known_ones(handler: SeHandler) -> None:
    ops = [{"op": "generate", "generator": "nanohorn", "params": {}, "name": "axle"}]
    with pytest.raises(BadInput, match="unknown generator") as exc_info:
        handler.put(id="gen-unknown", text=json.dumps({"ops": ops}))
    assert "cnt" in str(exc_info.value)
    assert "fullerene" in str(exc_info.value)


def test_generate_cnt_end_to_end(
    handler: SeHandler, structure: StructureHandler, store: Store
) -> None:
    ops = [
        {
            "op": "generate",
            "generator": "cnt",
            "params": {"n": 6, "m": 6, "length_A": 10.0},
            "name": "axle",
        }
    ]
    resp = handler.put(id="gentube", text=json.dumps({"ops": ops}))
    assert "gentube-axle" in resp.body
    assert "chiral_index" in resp.body

    block = handler.get(id="gentube", view="block", args={"name": "axle"})
    assert "realization: structure:gentube-axle" in block.body
    assert "sp2-rim" in block.body

    assert store.get_ref(kind="structure", id="gentube-axle") is not None
    _assert_no_error_findings(handler.get(id="gentube", view="validate").body)
    _assert_no_error_findings(structure.get(id="gentube-axle", view="validate").body)


def test_generate_fullerene_end_to_end(
    handler: SeHandler, structure: StructureHandler, store: Store
) -> None:
    ops = [
        {
            "op": "generate",
            "generator": "fullerene",
            "params": {"atoms": 60},
            "name": "cage",
        }
    ]
    resp = handler.put(id="genball", text=json.dumps({"ops": ops}))
    assert "genball-cage" in resp.body
    assert "pentagons=12" in resp.body
    assert "hexagons=20" in resp.body

    struct_ref = store.get_ref(kind="structure", id="genball-cage")
    assert struct_ref is not None
    scene, _handles = store.structure_load(struct_ref.id)
    assert len(scene.atoms) == 60
    assert len(scene.bonds) == 90

    block = handler.get(id="genball", view="block", args={"name": "cage"})
    assert "realization: structure:genball-cage" in block.body
    _assert_no_error_findings(handler.get(id="genball", view="validate").body)
    _assert_no_error_findings(structure.get(id="genball-cage", view="validate").body)


def test_generate_cone_end_to_end(
    handler: SeHandler, structure: StructureHandler, store: Store
) -> None:
    """The third convex generator, ported from nm's own e2e when the kind
    retired: worth its own case because a cone's envelope is a ``tcone``
    — THREE Å-suffixed lengths crossing the ingest boundary (cnt's ``cyl``
    has two, fullerene's ``sphere`` one), and its apex/base orientation is
    the one generator bug that shipped (gripe 286160)."""
    ops = [
        {
            "op": "generate",
            "generator": "cone",
            "params": {"pentagons": 3, "length_A": 20.0},
            "name": "horn",
        }
    ]
    resp = handler.put(id="gencone", text=json.dumps({"ops": ops}))
    assert "gencone-horn" in resp.body
    assert "cone_half_angle_deg" in resp.body

    block = handler.get(id="gencone", view="block", args={"name": "horn"})
    assert "realization: structure:gencone-horn" in block.body
    assert "sp2-rim" in block.body

    ref = store.get_ref(kind="se", id="gencone")
    assert ref is not None
    env = persist.load_tree(store, ref.id).blocks["horn"].envelope
    assert env is not None and env.startswith("tcone:") and "Å" not in env

    assert store.get_ref(kind="structure", id="gencone-horn") is not None
    _assert_no_error_findings(handler.get(id="gencone", view="validate").body)
    _assert_no_error_findings(structure.get(id="gencone-horn", view="validate").body)


def test_a_generated_envelope_is_stored_as_canonical_metres(
    handler: SeHandler, store: Store
) -> None:
    """The units trap this round had to route around (nm-se-merge.md): a
    generator emits Å-SUFFIXED text, which se's own ``add_block`` would
    reject outright — ``generate`` converts at the one boundary, so what
    lands in storage is bare metres at nanoscale, like every other se
    envelope."""
    ops = [
        {
            "op": "generate",
            "generator": "fullerene",
            "params": {"atoms": 60},
            "name": "cage",
        }
    ]
    handler.put(id="genunits", text=json.dumps({"ops": ops}))
    ref = store.get_ref(kind="se", id="genunits")
    assert ref is not None
    env = persist.load_tree(store, ref.id).blocks["cage"].envelope
    assert env is not None
    assert "Å" not in env
    radius = float(env.partition(":r")[2])
    assert 1e-11 < radius < 1e-8, env  # a real nanoscale cage, in metres


def test_a_bare_metres_envelope_still_reaches_add_block_unconverted(
    handler: SeHandler, store: Store
) -> None:
    """The other half of the same pin: ``generate``'s Å boundary must not
    have leaked into se's ``add_block`` contract (an agent-facing change
    the merge doesn't get to make)."""
    handler.put(
        id="genunits2",
        text=json.dumps(
            {"ops": [{"op": "add_block", "name": "a", "envelope": "sphere:r3e-10"}]}
        ),
    )
    ref = store.get_ref(kind="se", id="genunits2")
    assert ref is not None
    assert persist.load_tree(store, ref.id).blocks["a"].envelope == "sphere:r3e-10"


def test_generate_duplicate_block_name_rejected(handler: SeHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle", "envelope": "sphere:r2e-10"},
        {
            "op": "generate",
            "generator": "fullerene",
            "params": {"atoms": 60},
            "name": "axle",
        },
    ]
    with pytest.raises(BadInput, match="duplicate"):
        handler.put(id="gendup", text=json.dumps({"ops": ops}))


def test_generate_followed_by_a_failing_op_creates_no_orphan(
    handler: SeHandler, store: Store
) -> None:
    """The "orphan on partial failure" finding: ``structure_save`` commits
    on its own, so the mint is deferred past the WHOLE ops list."""
    ops = [
        {
            "op": "generate",
            "generator": "fullerene",
            "params": {"atoms": 60},
            "name": "cage",
        },
        # deliberately invalid — 'ghost' was never added, so this op fails
        # AFTER generate's pure half already ran; the mint must not happen.
        {"op": "connect", "a": "ghost.p1", "b": "ghost2.p2"},
    ]
    with pytest.raises(BadInput):
        handler.put(id="genfail", text=json.dumps({"ops": ops}))
    assert store.get_ref(kind="structure", id="genfail-cage") is None
    assert store.get_ref(kind="se", id="genfail") is None


def test_generate_refuses_to_overwrite_an_existing_structure_design(
    handler: SeHandler, structure: StructureHandler, store: Store
) -> None:
    """``structure_save`` is create-or-replace, and "axle"-style names are
    exactly the shared vocabulary a hand-authored design might already
    use."""
    structure.put(
        id="gencollide-axle",
        text=json.dumps(
            {
                "cell": _mol_cell_payload(),
                "ops": [{"op": "add_atom", "element": "N", "cart": [0.0, 0.0, 0.0]}],
            }
        ),
    )
    ops = [
        {
            "op": "generate",
            "generator": "cnt",
            "params": {"n": 4, "m": 4, "length_A": 8.0},
            "name": "axle",
        }
    ]
    with pytest.raises(BadInput, match="gencollide-axle"):
        handler.put(id="gencollide", text=json.dumps({"ops": ops}))

    struct_ref = store.get_ref(kind="structure", id="gencollide-axle")
    assert struct_ref is not None
    scene, _handles = store.structure_load(struct_ref.id)
    assert len(scene.atoms) == 1
    (only_atom,) = scene.atoms.values()
    assert only_atom.element == "N"


# ── envelope_fit: the pure L1↔L5 agreement check ─────────────────────────


def _cell() -> Cell:
    return Cell.from_lengths_angles(30.0, 30.0, 30.0, pbc=(False, False, False))


def test_envelope_fit_atom_inside_margin_is_none() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    # `envelope` is STORED (design-space canonical) text — bare metres, so
    # a 2 Å sphere is authored here pre-converted.
    assert se_atomic_validate.envelope_fit("sphere:r2e-10", scene) is None


def test_envelope_fit_atom_outside_margin_names_the_worst_offender() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    far_frac = scene.cell.cart_to_frac(np.array([10.0, 0.0, 0.0]))
    scene.atoms["b"] = Atom(label="b", element="C", frac=scene.cell.wrap(far_frac))
    result = se_atomic_validate.envelope_fit("sphere:r2e-10", scene)
    assert result is not None
    # atom "a" sits inside, so this can never be the gr334764 refusal.
    assert not isinstance(result, se_atomic_validate.FrameMismatch)
    label, protrusion = result
    assert label == "b"
    # sdf at [10,0,0] Å against a 2 Å sphere is 8 Å; the protrusion is
    # reported back in Å, the atomistic scale this finding is about.
    assert protrusion == pytest.approx(8.0 - VDW_MARGIN_A, abs=1e-6)


def test_envelope_fit_is_posed_at_identity_not_the_blocks_world_pose() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    assert se_atomic_validate.envelope_fit("cyl:r3e-10h1e-9", scene) is None


def test_envelope_fit_on_a_malformed_envelope_returns_none_not_raises() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    assert se_atomic_validate.envelope_fit("not-a-real-shape:x1", scene) is None


def test_envelope_fit_frame_mismatch_when_whole_scene_far_away() -> None:
    """gripe 334764 repro shape: an imported (from_smiles-style) scene's
    atoms sit near (10,10,10) in their own cell while the envelope spans
    z=0..9 at identity — the frames never corresponded, so the answer is a
    refusal, not a protrusion whose "widen the envelope" advice would
    destroy a correct envelope."""
    scene = Scene(cell=_cell())
    for i, cart in enumerate([[10.0, 10.0, 10.0], [11.0, 10.0, 10.0]]):
        label = "ab"[i]
        frac = scene.cell.cart_to_frac(np.array(cart))
        scene.atoms[label] = Atom(label=label, element="C", frac=frac)
    result = se_atomic_validate.envelope_fit("cyl:r3.5e-10h9e-10", scene)
    assert isinstance(result, se_atomic_validate.FrameMismatch)
    assert result.nearest_label == "a"
    # definitional: mismatch only fires when even the nearest atom clears
    # the margin by more than half the envelope's own bbox diagonal.
    assert result.envelope_diag_A == pytest.approx(math.sqrt(49 + 49 + 81), rel=1e-6)
    assert (
        result.clearance_A - VDW_MARGIN_A
        > se_atomic_validate.FRAME_MISMATCH_CLEARANCE_FRACTION * result.envelope_diag_A
    )


def test_envelope_fit_near_drift_stays_a_protrusion() -> None:
    """All atoms slightly outside is genuine drift (an envelope shrunk
    after the bind), NOT a frame mismatch — the scale-relative gate only
    reclassifies a scene sitting an envelope-width away."""
    scene = Scene(cell=_cell())
    frac = scene.cell.cart_to_frac(np.array([4.5, 0.0, 0.0]))
    scene.atoms["a"] = Atom(label="a", element="C", frac=frac)
    result = se_atomic_validate.envelope_fit("sphere:r2e-10", scene)
    assert result == ("a", pytest.approx(4.5 - 2.0 - VDW_MARGIN_A, abs=1e-6))


def test_envelope_fit_honours_a_custom_margin() -> None:
    scene = Scene(cell=_cell())
    far_frac = scene.cell.cart_to_frac(np.array([5.0, 0.0, 0.0]))
    scene.atoms["a"] = Atom(label="a", element="C", frac=scene.cell.wrap(far_frac))
    assert (
        se_atomic_validate.envelope_fit("sphere:r2e-10", scene, margin_A=10.0) is None
    )
    result = se_atomic_validate.envelope_fit("sphere:r2e-10", scene, margin_A=0.0)
    assert result == ("a", pytest.approx(3.0))


# ── envelope_fit: the bind preflight (advisory, never blocking) ──────────


def test_bind_preflight_warns_on_a_genuine_protrusion(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """An atom just past the margin (5 Å out on a 2 Å sphere — within the
    frame-mismatch gate's half-diagonal) is a real protrusion: the classic
    warning, with the widen-the-envelope advice."""
    c_label = _make_structure(structure, "frag_far", carts=[[5.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "add_port", "block": "hub", "name": "p1", "expected_element": "C"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_far",
            "ports": {"p1": c_label},
        },
    ]
    resp = handler.put(id="envfit1", text=json.dumps({"ops": ops}))
    assert "bound block 'hub' to structure 'frag_far'" in resp.body
    assert "⚠ envelope_fit" in resp.body
    assert c_label in resp.body
    assert "protrudes" in resp.body


def test_bind_preflight_refuses_on_frame_mismatch(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """gripe 334764: a fragment sitting an envelope-width away (an imported
    scene with no local-frame alignment) gets the loud refusal, not a
    protrusion — and never the destructive widen-the-envelope advice."""
    c_label = _make_structure(structure, "frag_off_frame", carts=[[20.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "add_port", "block": "hub", "name": "p1", "expected_element": "C"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_off_frame",
            "ports": {"p1": c_label},
        },
    ]
    resp = handler.put(id="envfitfm1", text=json.dumps({"ops": ops}))
    assert "bound block 'hub' to structure 'frag_off_frame'" in resp.body
    assert "⚠ envelope_fit: cannot check — frames do not correspond" in resp.body
    assert c_label in resp.body
    assert "do NOT widen the envelope" in resp.body
    assert "protrudes" not in resp.body


def test_bind_preflight_is_silent_within_the_margin(
    handler: SeHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_near", carts=[[0.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5e-10"},
        {"op": "add_port", "block": "hub", "name": "p1", "expected_element": "C"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_near",
            "ports": {"p1": c_label},
        },
    ]
    resp = handler.put(id="envfit2", text=json.dumps({"ops": ops}))
    assert "⚠ envelope_fit" not in resp.body


def test_bind_preflight_never_blocks_the_bind(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """Advisory only — a hand-authored envelope is often a rough first
    guess, so the block is bound regardless of the warning."""
    c_label = _make_structure(structure, "frag_far2", carts=[[50.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "add_port", "block": "hub", "name": "p1", "expected_element": "C"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_far2",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="envfit3", text=json.dumps({"ops": ops}))
    block = handler.get(id="envfit3", view="block", args={"name": "hub"})
    assert "realization: structure:frag_far2" in block.body


# ── view='validate': the atomic findings ─────────────────────────────────


def test_validate_envelope_fit_warns_on_a_protruding_bound_scene(
    handler: SeHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_drift", carts=[[5.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_drift",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="envfit4", text=json.dumps({"ops": ops}))
    resp = handler.get(id="envfit4", view="validate")
    assert "envelope_fit" in resp.body
    assert "warn" in resp.body
    assert "hub" in resp.body
    assert "protrudes" in resp.body


def test_validate_envelope_fit_refuses_on_frame_mismatch(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """gripe 334764's standing-finding half: the read-time re-check emits
    the refusal (still warn-tier — the agreement is unverifiable until the
    scene is re-authored in the block's frame), never the drifted-apart
    protrusion text."""
    c_label = _make_structure(structure, "frag_off_frame2", carts=[[30.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2e-10"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_off_frame2",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="envfitfm2", text=json.dumps({"ops": ops}))
    resp = handler.get(id="envfitfm2", view="validate")
    assert "envelope_fit" in resp.body
    assert "warn" in resp.body
    assert "cannot check — frames do not correspond" in resp.body
    assert "do NOT widen the envelope" in resp.body
    assert "protrudes" not in resp.body


def test_validate_envelope_fit_is_clean_when_the_atoms_fit(
    handler: SeHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_fits", carts=[[0.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5e-10"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_fits",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="envfit5", text=json.dumps({"ops": ops}))
    assert "envelope_fit" not in handler.get(id="envfit5", view="validate").body


def test_validate_envelope_fit_skips_a_block_with_no_envelope(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """A bound block with no declared envelope has nothing to check
    against — ``block_without_envelope`` already flags the missing
    geometry; ``envelope_fit`` must not misfire on a ``None``."""
    c_label = _make_structure(structure, "frag_noenv", carts=[[999.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub"},  # no envelope
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_noenv",
            "ports": {"p1": c_label},
        },
    ]
    resp = handler.put(id="envfit6", text=json.dumps({"ops": ops}))
    assert "⚠ envelope_fit" not in resp.body
    assert "envelope_fit" not in handler.get(id="envfit6", view="validate").body


def test_validate_dangling_binding_when_the_bound_design_is_deleted(
    handler: SeHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_gone")[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5e-10"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_gone",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="dang1", text=json.dumps({"ops": ops}))
    assert "dangling_binding" not in handler.get(id="dang1", view="validate").body
    structure.delete(id="frag_gone")
    dirty = handler.get(id="dang1", view="validate")
    assert "dangling_binding" in dirty.body
    assert "no longer resolves" in dirty.body


def test_validate_dangling_binding_when_the_bound_atom_is_gone() -> None:
    """Direct-function precision check: a stored ``bound_atom`` that no
    longer exists in a scene the caller DID hydrate."""
    tree = SeTree()
    tree.blocks["hub"] = SeBlock(
        name="hub",
        bound_kind="structure",
        bound="frag",
        ports={"p1": PortSpec(name="p1", bound_design="frag", bound_atom="ghost")},
    )
    findings = se_atomic_validate.validate_atomic(
        tree, bound_scenes={"frag": {"aC1": "C"}}
    )
    finding = next(f for f in findings if f.rule == "dangling_binding")
    assert finding.severity == "error"
    assert "ghost" in finding.detail


def test_validate_binding_element_mismatch_is_defense_in_depth() -> None:
    """The bind-time gate already refuses this; a row that got here some
    other way still has to surface (warn tier — the atom is real, the
    label on the port is the thing that drifted)."""
    tree = SeTree()
    tree.blocks["hub"] = SeBlock(
        name="hub",
        bound_kind="structure",
        bound="frag",
        ports={
            "p1": PortSpec(
                name="p1",
                expected_element="O",
                bound_design="frag",
                bound_atom="aC1",
            )
        },
    )
    findings = se_atomic_validate.validate_atomic(
        tree, bound_scenes={"frag": {"aC1": "C"}}
    )
    finding = next(f for f in findings if f.rule == "binding_element_mismatch")
    assert finding.severity == "warn"
    assert "'O'" in finding.detail and "'C'" in finding.detail


def test_validate_skips_a_slug_the_caller_never_hydrated() -> None:
    """Never guess: an un-hydrated slug yields no binding finding at all,
    rather than a false ``dangling_binding``."""
    tree = SeTree()
    tree.blocks["hub"] = SeBlock(name="hub", bound_kind="structure", bound="frag")
    assert se_atomic_validate.validate_atomic(tree) == []


def test_validate_port_capability_is_defense_in_depth() -> None:
    """A stored bond connect that violates the capability gate despite
    never having gone through ``ops.py``'s connect op."""
    tree = SeTree()
    tree.blocks["a"] = SeBlock(name="a", ports={"p1": PortSpec(name="p1", roles=[])})
    tree.blocks["b"] = SeBlock(name="b", ports={"p1": PortSpec(name="p1", roles=[])})
    tree.connects.append(
        ConnectSpec(a_block="a", a_port="p1", b_block="b", b_port="p1", kind="bond")
    )
    finding = next(
        f
        for f in se_atomic_validate.validate_atomic(tree)
        if f.rule == "port_capability"
    )
    assert finding.severity == "error"
    assert "missing 'covalent'" in finding.detail


def test_validate_port_capability_stays_quiet_on_a_structural_edge() -> None:
    """An se connect with no ``kind`` has no chemistry to gate — the merge
    must not start demanding roles of a bolted joint."""
    tree = SeTree()
    tree.blocks["a"] = SeBlock(name="a", ports={"p1": PortSpec(name="p1", roles=[])})
    tree.blocks["b"] = SeBlock(name="b", ports={"p1": PortSpec(name="p1", roles=[])})
    tree.connects.append(
        ConnectSpec(a_block="a", a_port="p1", b_block="b", b_port="p1")
    )
    assert not [
        f
        for f in se_atomic_validate.validate_atomic(tree)
        if f.rule == "port_capability"
    ]


def test_validate_port_capability_leaves_a_dangling_endpoint_to_its_own_rule() -> None:
    """One finding per problem: there is no ``PortSpec`` to read roles off,
    and ``precis_se.validate``'s ``dangling_connect`` already reports it."""
    tree = SeTree()
    tree.blocks["a"] = SeBlock(name="a", ports={"p1": PortSpec(name="p1", roles=[])})
    tree.connects.append(
        ConnectSpec(a_block="a", a_port="p1", b_block="ghost", b_port="p1", kind="bond")
    )
    assert not [
        f
        for f in se_atomic_validate.validate_atomic(tree)
        if f.rule == "port_capability"
    ]


def test_validate_connect_cycle_warns_with_the_path(handler: SeHandler) -> None:
    """A 5-block ring closed head-to-tail (gripe 334768's dogfood): a cycle
    in the connect graph even though the block tree has no nesting."""
    names = ["r1", "r2", "r3", "r4", "r5"]
    radius, sphere_r = 1e-9, 4e-10
    ops: list[dict[str, object]] = []
    for i, n in enumerate(names):
        angle = math.radians(72 * i)
        pos = [radius * math.cos(angle), radius * math.sin(angle), 0.0]
        ops.append(
            {
                "op": "add_block",
                "name": n,
                "envelope": f"sphere:r{sphere_r:g}",
                "pose": pos,
            }
        )
        ops.append({"op": "add_port", "block": n, "name": "p1", "roles": ["covalent"]})
        ops.append({"op": "add_port", "block": n, "name": "p2", "roles": ["covalent"]})
    for i in range(len(names)):
        a, b = names[i], names[(i + 1) % len(names)]
        ops.append({"op": "connect", "a": f"{a}.p2", "b": f"{b}.p1", "kind": "bond"})
    handler.put(id="cycle1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="cycle1", view="validate")
    assert "connect_cycle" in resp.body
    assert (
        "verify this is an intended macrocycle, not an accidental closure" in resp.body
    )
    for n in names:
        assert n in resp.body


def test_validate_connect_cycle_ignores_a_ring_of_structural_edges(
    handler: SeHandler,
) -> None:
    """The merge's own narrowing: a closed loop of se connects is a truss,
    the most ordinary thing in the kind — only a ring of chemistry edges
    asserts the macrocycle this rule asks about."""
    ops: list[dict[str, object]] = []
    for n in ("t1", "t2", "t3"):
        ops.append({"op": "add_block", "name": n, "envelope": "box:w0.01d0.01h0.01"})
        ops.append({"op": "add_port", "block": n, "name": "p1"})
        ops.append({"op": "add_port", "block": n, "name": "p2"})
    for a, b in (("t1", "t2"), ("t2", "t3"), ("t3", "t1")):
        ops.append({"op": "connect", "a": f"{a}.p2", "b": f"{b}.p1"})
    handler.put(id="truss1", text=json.dumps({"ops": ops}))
    assert "connect_cycle" not in handler.get(id="truss1", view="validate").body


def test_validate_bond_length_sanity_warns_on_a_wildly_long_bond(
    handler: SeHandler,
) -> None:
    ops = [
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
    ]
    handler.put(id="longbond1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="longbond1", view="validate")
    assert "bond_length_sanity" in resp.body
    assert "48.5" in resp.body


def test_validate_bond_length_sanity_stays_quiet_on_a_plausible_bond(
    handler: SeHandler,
) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "sphere:r3e-10"},
        {
            "op": "add_block",
            "name": "b",
            "envelope": "sphere:r3e-10",
            "pose": [8e-10, 0, 0],
        },
        {"op": "add_port", "block": "a", "name": "p1", "roles": ["covalent"]},
        {"op": "add_port", "block": "b", "name": "p1", "roles": ["covalent"]},
        {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "bond"},
    ]
    handler.put(id="okbond1", text=json.dumps({"ops": ops}))
    assert "bond_length_sanity" not in handler.get(id="okbond1", view="validate").body


def _vector_ops(
    a_dir: list[float], b_dir: list[float], *, b_pose: list[float], b_rot: list[float]
) -> list[dict[str, object]]:
    return [
        {"op": "add_block", "name": "a", "envelope": "sphere:r2e-10"},
        {
            "op": "add_block",
            "name": "b",
            "envelope": "sphere:r2e-10",
            "pose": b_pose,
            "rot": b_rot,
        },
        {
            "op": "add_port",
            "block": "a",
            "name": "p1",
            "roles": ["covalent"],
            "direction": a_dir,
        },
        {
            "op": "add_port",
            "block": "b",
            "name": "p1",
            "roles": ["covalent"],
            "direction": b_dir,
        },
        {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "bond"},
    ]


def test_validate_bond_vector_alignment_warns_when_not_antiparallel(
    handler: SeHandler,
) -> None:
    """Both ports point the SAME way — the dogfood's proof that these
    vectors were pure decoration until something read them."""
    ops = _vector_ops([1, 0, 0], [1, 0, 0], b_pose=[4e-10, 0, 0], b_rot=[0, 0, 0])
    handler.put(id="badvec1", text=json.dumps({"ops": ops}))
    assert "bond_vector_alignment" in handler.get(id="badvec1", view="validate").body


def test_validate_bond_vector_alignment_is_clean_when_antiparallel(
    handler: SeHandler,
) -> None:
    ops = _vector_ops([1, 0, 0], [-1, 0, 0], b_pose=[4e-10, 0, 0], b_rot=[0, 0, 0])
    handler.put(id="goodvec1", text=json.dumps({"ops": ops}))
    assert (
        "bond_vector_alignment" not in handler.get(id="goodvec1", view="validate").body
    )


def test_validate_bond_vector_alignment_rotates_into_world_frame_before_judging(
    handler: SeHandler,
) -> None:
    """``direction`` is LOCAL (the same convention ``envelope`` uses).
    Comparing RAW stored vectors would see local [1,0,0] vs [-1,0,0] here —
    apparently antiparallel — and miss a genuinely 90°-off pair."""
    ops = _vector_ops(
        [1, 0, 0], [-1, 0, 0], b_pose=[4e-10, 0, 0], b_rot=[0, 0, math.radians(90)]
    )
    handler.put(id="rotvec2", text=json.dumps({"ops": ops}))
    assert "bond_vector_alignment" in handler.get(id="rotvec2", view="validate").body


def test_validate_bond_vector_alignment_clean_after_a_rotation_that_aligns_them(
    handler: SeHandler,
) -> None:
    """The inverse: a rot=[0,0,90] block's local [1,0,0] points world
    [0,1,0], so raw comparison would raise a spurious warn."""
    ops: list[dict[str, object]] = [
        {
            "op": "add_block",
            "name": "a",
            "envelope": "sphere:r2e-10",
            "rot": [0, 0, math.radians(90)],
        },
        {
            "op": "add_block",
            "name": "b",
            "envelope": "sphere:r2e-10",
            "pose": [0, 4e-10, 0],
        },
        {
            "op": "add_port",
            "block": "a",
            "name": "p1",
            "roles": ["covalent"],
            "direction": [1, 0, 0],
        },
        {
            "op": "add_port",
            "block": "b",
            "name": "p1",
            "roles": ["covalent"],
            "direction": [0, -1, 0],
        },
        {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "bond"},
    ]
    handler.put(id="rotvec1", text=json.dumps({"ops": ops}))
    assert (
        "bond_vector_alignment" not in handler.get(id="rotvec1", view="validate").body
    )


def test_validate_undeclared_interpenetration_supersedes_nms_envelope_overlap(
    handler: SeHandler,
) -> None:
    """nm's ``envelope_overlap`` did not move: se's own geometry-tier rule
    covers the same dogfood repro (two unrelated blocks sharing material),
    with an AABB broad phase and a time budget nm's never had."""
    ops = [
        {"op": "add_block", "name": "a", "envelope": "sphere:r5e-10"},
        {
            "op": "add_block",
            "name": "b",
            "envelope": "sphere:r5e-10",
            "pose": [1e-10, 0, 0],
        },
    ]
    handler.put(id="overlap1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="overlap1", view="validate")
    assert "undeclared_interpenetration" in resp.body
    assert "envelope_overlap" not in resp.body
    assert "a—b" in resp.body


def test_validate_external_port_skips_the_warn_with_an_info_line(
    handler: SeHandler,
) -> None:
    """gripe 334769, transferred: before this the ONLY way to silence
    ``unconnected_port`` was to author a connect — even a fake one, which
    the nm dogfood proved an LLM will happily do."""
    ops = [
        {"op": "add_block", "name": "a", "envelope": "sphere:r2e-10"},
        {
            "op": "add_port",
            "block": "a",
            "name": "ext1",
            "roles": ["covalent"],
            "annotations": {"external": True},
        },
        # A second external port makes the info/warn counts ASYMMETRIC (2
        # info, 1 warn) — a header that counted the wrong tier, or
        # double-counted, would still land on a number that matches a
        # symmetric split; this shape can't hide that.
        {
            "op": "add_port",
            "block": "a",
            "name": "ext2",
            "roles": ["covalent"],
            "annotations": {"external": True},
        },
        {"op": "add_port", "block": "a", "name": "plain1", "roles": ["covalent"]},
    ]
    handler.put(id="ext1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="ext1", view="validate")
    assert "external by design" in resp.body
    plain_rows = [ln for ln in resp.body.splitlines() if "a.plain1" in ln]
    assert plain_rows and all("warn" in ln for ln in plain_rows)
    ext_rows = [ln for ln in resp.body.splitlines() if "a.ext1" in ln]
    assert ext_rows and all("warn" not in ln for ln in ext_rows)
    assert "2 info" in resp.body.splitlines()[0]


def test_validate_external_annotation_requires_the_literal_true() -> None:
    """An LLM-authored ``{"external": "false"}`` (a JSON STRING) is truthy
    in Python — it must NOT silently read as external."""
    from precis_se import validate as se_validate

    tree = SeTree()
    tree.blocks["a"] = SeBlock(
        name="a",
        envelope="sphere:r2e-10",
        ports={
            "p1": PortSpec(
                name="p1", roles=["covalent"], annotations={"external": "false"}
            )
        },
    )
    finding = next(
        f for f in se_validate.validate(tree) if f.rule == "unconnected_port"
    )
    assert finding.severity == "warn"


# ── the atomic filled-fraction line (mode-scoped: atomic designs only) ───


def test_the_atomic_fill_line_is_absent_from_a_design_with_no_chemistry(
    handler: SeHandler,
) -> None:
    """Mode-scoped both ways: a caster design must not grow a "bound to
    real chemistry" readout."""
    handler.put(
        id="caster1",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "fork",
                        "envelope": "box:w0.04d0.02h0.08",
                    }
                ]
            }
        ),
    )
    body = handler.get(id="caster1", view="validate").body
    assert "real chemistry" not in body
    assert "block(s) have envelopes" in body  # se's own L1 line still leads


def test_the_atomic_fill_line_reads_unfilled_on_a_fresh_scaffold(
    handler: SeHandler,
) -> None:
    """The maze.py lesson: zero findings on an unfilled design must read as
    "not started", never as "done"."""
    ops = [
        {"op": "add_block", "name": "axle", "envelope": "cyl:r2e-10h2e-09"},
        {"op": "set_mode", "block": "axle", "mode": "atomic"},
        {"op": "add_block", "name": "hub", "envelope": "sphere:r3e-10"},
        {"op": "set_mode", "block": "hub", "mode": "atomic"},
    ]
    handler.put(id="unfilled1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="unfilled1", view="validate")
    assert "0/2 atomic block(s) filled" in resp.body
    assert "UNFILLED" in resp.body


def test_the_atomic_fill_line_counts_partial_fill(
    handler: SeHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_partial")[0]
    ops = [
        {"op": "add_block", "name": "axle", "envelope": "sphere:r5e-10"},
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5e-10"},
        {"op": "set_mode", "block": "hub", "mode": "atomic"},
        {"op": "add_port", "block": "axle", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "axle",
            "design": "frag_partial",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="partial1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="partial1", view="validate")
    assert "1/2 atomic block(s) filled" in resp.body
    assert "UNFILLED" not in resp.body


def test_the_atomic_fill_line_excludes_instances_from_the_count(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """An instance never owns a binding of its own (bind via the template),
    so one bound template with N instances reads ``1/1`` — never inflated,
    never silently counting the instances as forever-unfilled."""
    c_label = _make_structure(structure, "frag_tmpl")[0]
    ops = [
        {"op": "add_block", "name": "sugar", "envelope": "sphere:r5e-10"},
        {"op": "add_port", "block": "sugar", "name": "p1"},
        {"op": "instance_block", "name": "sugar2", "template": "sugar"},
        {
            "op": "bind_structure",
            "block": "sugar",
            "design": "frag_tmpl",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="inst_fill1", text=json.dumps({"ops": ops}))
    assert (
        "1/1 atomic block(s) filled"
        in handler.get(id="inst_fill1", view="validate").body
    )


def test_the_atomic_fill_line_helper_returns_none_for_an_empty_tree() -> None:
    assert se_atomic_render.atomic_fill_line(SeTree()) is None


def test_the_atomic_fill_line_helper_marks_an_all_unbound_design() -> None:
    tree = SeTree()
    tree.blocks["a"] = SeBlock(name="a", mode="atomic")
    line = se_atomic_render.atomic_fill_line(tree)
    assert line is not None
    assert line.startswith("0/1 atomic block(s) filled")
    assert "UNFILLED" in line


# ── view='mechanics' ─────────────────────────────────────────────────────


def test_mechanics_view_renders_unfilled_for_an_unbound_design(
    handler: SeHandler,
) -> None:
    from precis_se.atomic import mechanics

    handler.put(
        id="mech-empty",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "scaffold", "envelope": "sphere:r3e-10"}
                ]
            }
        ),
    )
    body = handler.get(id="mech-empty", view="mechanics").body
    assert "atomic mechanics" in body
    assert mechanics.HONESTY_NOTE in body
    assert "unfilled" in body
    assert "no bond connects declared" in body


def test_mechanics_view_renders_buckling_for_a_generated_cnt_block(
    handler: SeHandler,
) -> None:
    ops = [
        {
            "op": "generate",
            "generator": "cnt",
            "params": {"n": 6, "m": 6, "length_A": 15.0},
            "name": "axle",
        }
    ]
    handler.put(id="mech-cnt", text=json.dumps({"ops": ops}))
    body = handler.get(id="mech-cnt", view="mechanics").body
    assert "not a tube envelope" not in body
    lines = [line for line in body.splitlines() if line.strip().startswith("axle")]
    assert lines, body
    assert "unfilled" not in lines[0]


def test_mechanics_view_says_not_fused_for_ports_in_two_designs(
    handler: SeHandler, structure: StructureHandler
) -> None:
    """The unfilled-not-zero rule's third state: "0" would read as
    "measured and found to have zero tensile capacity", but two ports bound
    to two SEPARATE designs were never fused into one bond graph at all."""
    c_a = _make_structure(structure, "fuse_a")[0]
    c_b = _make_structure(structure, "fuse_b")[0]
    ops = [
        {"op": "add_block", "name": "a", "envelope": "sphere:r5e-10"},
        {
            "op": "add_block",
            "name": "b",
            "envelope": "sphere:r5e-10",
            "pose": [1e-9, 0, 0],
        },
        {"op": "add_port", "block": "a", "name": "p1", "roles": ["covalent"]},
        {"op": "add_port", "block": "b", "name": "p1", "roles": ["covalent"]},
        {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "bond"},
        {
            "op": "bind_structure",
            "block": "a",
            "design": "fuse_a",
            "ports": {"p1": c_a},
        },
        {
            "op": "bind_structure",
            "block": "b",
            "design": "fuse_b",
            "ports": {"p1": c_b},
        },
    ]
    handler.put(id="mech-split", text=json.dumps({"ops": ops}))
    body = handler.get(id="mech-split", view="mechanics").body
    assert "not fused" in body
    assert "never fused into one bond graph" in body


def test_mechanics_view_reports_unfilled_for_an_unbound_bond(
    handler: SeHandler,
) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "sphere:r5e-10"},
        {
            "op": "add_block",
            "name": "b",
            "envelope": "sphere:r5e-10",
            "pose": [1e-9, 0, 0],
        },
        {"op": "add_port", "block": "a", "name": "p1", "roles": ["covalent"]},
        {"op": "add_port", "block": "b", "name": "p1", "roles": ["covalent"]},
        {"op": "connect", "a": "a.p1", "b": "b.p1", "kind": "bond"},
    ]
    handler.put(id="mech-unbound", text=json.dumps({"ops": ops}))
    body = handler.get(id="mech-unbound", view="mechanics").body
    assert "one or both ports unbound" in body


# ── view='literature': a deterministic (no-LLM) query ────────────────────


def test_literature_view_builds_the_whole_design_query(
    lit_handler: SeHandler,
) -> None:
    ops = [
        {
            "op": "add_block",
            "name": "axle",
            "envelope": "cyl:r2e-10h2e-09",
            "desc": "a rigid threading rod",
            "use": "photoswitchable axle",
        },
    ]
    lit_handler.put(
        id="lit1",
        text=json.dumps(
            {"description": "rotaxane with alternating rim charges", "ops": ops}
        ),
    )
    resp = lit_handler.get(id="lit1", view="literature")
    assert "rotaxane with alternating rim charges" in resp.body
    assert "a rigid threading rod" in resp.body
    assert "photoswitchable axle" in resp.body
    assert "no matching papers" in resp.body  # nothing seeded yet


def test_literature_view_targets_one_block(lit_handler: SeHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle", "desc": "threading rod"},
        {"op": "add_block", "name": "crown", "desc": "macrocycle rim"},
    ]
    lit_handler.put(id="lit2", text=json.dumps({"ops": ops}))
    resp = lit_handler.get(id="lit2", view="literature", args={"block": "crown"})
    assert "macrocycle rim" in resp.body
    assert "threading rod" not in resp.body
    assert "block 'crown'" in resp.body


def test_literature_view_unknown_block_raises(lit_handler: SeHandler) -> None:
    lit_handler.put(id="lit3", text=json.dumps({"ops": []}))
    with pytest.raises(NotFound, match="no such block"):
        lit_handler.get(id="lit3", view="literature", args={"block": "ghost"})


def test_literature_query_is_deterministic(lit_handler: SeHandler) -> None:
    ops = [{"op": "add_block", "name": "axle", "desc": "threading rod"}]
    lit_handler.put(id="lit4a", text=json.dumps({"ops": ops}))
    lit_handler.put(id="lit4b", text=json.dumps({"ops": ops}))
    ref_a = lit_handler.store.get_ref(kind="se", id="lit4a")
    ref_b = lit_handler.store.get_ref(kind="se", id="lit4b")
    assert ref_a is not None and ref_b is not None
    tree_a = persist.load_tree(lit_handler.store, ref_a.id)
    tree_b = persist.load_tree(lit_handler.store, ref_b.id)
    q_a = se_atomic_render.literature_query(lit_handler.store, tree_a, ref_a)
    q_b = se_atomic_render.literature_query(lit_handler.store, tree_b, ref_b)
    assert q_a == q_b
    tree_a_again = persist.load_tree(lit_handler.store, ref_a.id)
    assert (
        se_atomic_render.literature_query(lit_handler.store, tree_a_again, ref_a) == q_a
    )


def test_literature_query_includes_the_connect_objective_vocabulary(
    lit_handler: SeHandler,
) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "sphere:r2e-10"},
        {"op": "add_block", "name": "b", "envelope": "sphere:r2e-10"},
        {"op": "add_port", "block": "a", "name": "p1"},
        {"op": "add_port", "block": "b", "name": "p1"},
        {
            "op": "connect",
            "a": "a.p1",
            "b": "b.p1",
            "kind": "interaction",
            "objectives": {"role": "pi_stack"},
        },
    ]
    lit_handler.put(id="lit5", text=json.dumps({"ops": ops}))
    assert (
        "pi_stack"
        in lit_handler.get(id="lit5", view="literature", args={"block": "a"}).body
    )
    # b is the OTHER endpoint of that same connect, so it is in scope too
    assert (
        "pi_stack"
        in lit_handler.get(id="lit5", view="literature", args={"block": "b"}).body
    )


def test_literature_query_includes_the_bound_composition(
    lit_handler: SeHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_lit")[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5e-10"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_lit",
            "ports": {"p1": c_label},
        },
    ]
    lit_handler.put(id="lit6", text=json.dumps({"ops": ops}))
    resp = lit_handler.get(id="lit6", view="literature", args={"block": "hub"})
    assert "C" in resp.body  # the bound structure's element composition


def test_literature_view_surfaces_a_seeded_matching_paper(
    lit_handler: SeHandler, store: Store
) -> None:
    ops = [{"op": "add_block", "name": "crown", "desc": "cyclodextrin macrocycle rim"}]
    lit_handler.put(id="lit7", text=json.dumps({"ops": ops}))
    _seed_paper(
        store,
        slug="cd_paper",
        title="Cyclodextrin rotaxane macrocycles",
        # the lexical leg is a strict AND — every generated query token
        # ("crown" included, the block's own name) must appear in the body.
        body="a study of crown cyclodextrin macrocycle rim chemistry for rotaxanes",
    )
    _seed_paper(
        store,
        slug="unrelated_paper",
        title="Unrelated topic",
        body="a completely unrelated discussion of tectonic plates",
    )
    resp = lit_handler.get(id="lit7", view="literature")
    assert "Cyclodextrin rotaxane macrocycles" in resp.body
    assert "Unrelated topic" not in resp.body


def test_literature_view_no_match_gives_a_recovery_hint(
    lit_handler: SeHandler,
) -> None:
    ops = [{"op": "add_block", "name": "axle", "desc": "threading rod"}]
    lit_handler.put(id="lit8", text=json.dumps({"ops": ops}))
    resp = lit_handler.get(id="lit8", view="literature")
    assert "no matching papers" in resp.body
    assert "search(kind='paper'" in resp.body


def test_literature_query_falls_back_to_the_block_names(
    lit_handler: SeHandler,
) -> None:
    """No description, no desc/use, no connects, no binding — the query
    must still never be empty."""
    lit_handler.put(
        id="lit9", text=json.dumps({"ops": [{"op": "add_block", "name": "axle"}]})
    )
    ref = lit_handler.store.get_ref(kind="se", id="lit9")
    assert ref is not None
    tree = persist.load_tree(lit_handler.store, ref.id)
    query = se_atomic_render.literature_query(lit_handler.store, tree, ref)
    assert query.strip()
    assert "axle" in query


# ── the deferred add_block dof axis-port check (gripe 334765) ────────────


def test_add_block_dof_is_valid_when_its_ports_arrive_later_in_the_same_call(
    handler: SeHandler,
) -> None:
    """A block minted by ``add_block`` owns no ports at that instant, so the
    port-existence half of dof vetting runs once the whole list is walked —
    the handler's interception, not the op."""
    ops = [
        {
            "op": "add_block",
            "name": "axle",
            "envelope": "cyl:r2e-10h2e-09",
            "dof": {"kind": "rotational", "axis_ports": ["head", "tail"]},
        },
        {"op": "add_port", "block": "axle", "name": "head"},
        {"op": "add_port", "block": "axle", "name": "tail"},
    ]
    handler.put(id="dof_late1", text=json.dumps({"ops": ops}))
    assert "rot" in handler.get(id="dof_late1", view="topology").body


def test_add_block_dof_naming_ports_that_never_arrive_is_refused(
    handler: SeHandler, store: Store
) -> None:
    ops = [
        {
            "op": "add_block",
            "name": "axle",
            "envelope": "cyl:r2e-10h2e-09",
            "dof": {"kind": "rotational", "axis_ports": ["head", "ghost"]},
        },
        {"op": "add_port", "block": "axle", "name": "head"},
    ]
    with pytest.raises(BadInput, match="no such port") as exc:
        handler.put(id="dof_late2", text=json.dumps({"ops": ops}))
    assert "add_block" in str(exc.value)
    assert store.get_ref(kind="se", id="dof_late2") is None


def test_a_deferred_dof_check_is_skipped_when_a_later_op_undoes_it(
    handler: SeHandler,
) -> None:
    """Never re-validate what a later op in the same call already undid."""
    ops = [
        {
            "op": "add_block",
            "name": "axle",
            "envelope": "cyl:r2e-10h2e-09",
            "dof": {"kind": "rotational", "axis_ports": ["head", "tail"]},
        },
        {"op": "clear_dof", "block": "axle"},
    ]
    handler.put(id="dof_late3", text=json.dumps({"ops": ops}))
    assert "rot" not in handler.get(id="dof_late3", view="topology").body
