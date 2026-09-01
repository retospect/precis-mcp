"""precis_nm slice 4b (read-side) — docs/backlog/nm-kind.md "4b — LLM fill
loop": ``view='literature'`` (the ``structure`` precedent, no LLM), the
``envelope_fit`` L1↔L5 agreement check (bind preflight + ``view='validate'``
warn-tier finding), and the ``view='validate'`` filled-fraction honesty
header (the maze.py lesson: zero findings on an empty design must read as
unfilled, not done).

Same shared-test-DB-seeds-its-own-migration fixture shape as
``test_nm_plugin.py``/``test_nm_mechanics.py`` (the plugin's migrations
aren't in the core template)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import precis_nm
import precis_nm.validate as nm_validate
from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers.structure import StructureHandler
from precis.store import ChunkInsert, Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_nm.generators.sp2 import VDW_MARGIN_A
from precis_nm.handler import NmHandler
from precis_nm.ops import BlockNode, BlockTree

_MIGRATIONS_DIR = Path(precis_nm.__file__).parent / "migrations"


def _seed_nm_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> NmHandler:
    _seed_nm_migrations(store)
    return NmHandler(hub=hub)


@pytest.fixture
def lit_handler(store: Store) -> NmHandler:
    """A separate handler wired to a hub with **no embedder** — the
    ``test_structure_literature_provenance.py`` precedent: search degrades
    to lexical only, so a literature-view assertion never depends on
    mock-embedding cosine noise."""
    _seed_nm_migrations(store)
    return NmHandler(hub=Hub(store=store))


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


def _seed_paper(store: Store, *, slug: str, title: str, body: str) -> None:
    ref = store.insert_ref(kind="paper", slug=slug, title=title, meta={})
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=body)])


# ── envelope_fit — the pure agreement-check function ────────────────────


def _cell() -> Cell:
    return Cell.from_lengths_angles(30.0, 30.0, 30.0, pbc=(False, False, False))


def test_envelope_fit_atom_inside_margin_is_none() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    assert nm_validate.envelope_fit("sphere:r2", scene) is None


def test_envelope_fit_atom_outside_margin_names_worst_offender() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    far_frac = scene.cell.cart_to_frac(np.array([10.0, 0.0, 0.0]))
    scene.atoms["b"] = Atom(label="b", element="C", frac=scene.cell.wrap(far_frac))
    result = nm_validate.envelope_fit("sphere:r2", scene)
    assert result is not None
    label, protrusion = result
    assert label == "b"
    # sdf at [10,0,0] against sphere:r2 is 10-2=8; protrusion = 8 - VDW_MARGIN_A
    assert protrusion == pytest.approx(8.0 - VDW_MARGIN_A, abs=1e-6)


def test_envelope_fit_posed_at_identity_not_block_world_pose() -> None:
    """The atom sits at the ORIGIN, matching a cyl envelope's own local
    frame (z=0..h, radially centered on the axis) — a block's own
    pose/rot must never leak into this check (module docstring)."""
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    assert nm_validate.envelope_fit("cyl:r3h10", scene) is None


def test_envelope_fit_malformed_envelope_returns_none_not_raise() -> None:
    scene = Scene(cell=_cell())
    scene.atoms["a"] = Atom(label="a", element="C", frac=np.zeros(3))
    assert nm_validate.envelope_fit("not-a-real-shape:x1", scene) is None


def test_envelope_fit_custom_margin() -> None:
    scene = Scene(cell=_cell())
    far_frac = scene.cell.cart_to_frac(np.array([5.0, 0.0, 0.0]))
    scene.atoms["a"] = Atom(label="a", element="C", frac=scene.cell.wrap(far_frac))
    # sphere:r2 -> sdf=3 at [5,0,0]; comfortably inside a generous 10 A margin...
    assert nm_validate.envelope_fit("sphere:r2", scene, margin_A=10.0) is None
    # ...but a tight zero margin reports it.
    result = nm_validate.envelope_fit("sphere:r2", scene, margin_A=0.0)
    assert result == ("a", pytest.approx(3.0))


# ── envelope_fit — bind preflight (handler._bind_structure) ─────────────


def test_bind_structure_preflight_warns_on_gross_protrusion(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_far", carts=[[20.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
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


def test_bind_structure_preflight_silent_when_within_margin(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_near", carts=[[0.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5"},
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


def test_bind_structure_preflight_never_blocks_the_bind(
    handler: NmHandler, structure: StructureHandler
) -> None:
    """Advisory only (docstring: "a hand-authored envelope is often a
    rough first guess") — the block is bound regardless of the warning."""
    c_label = _make_structure(structure, "frag_far2", carts=[[50.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
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
    assert "bound_design: frag_far2" in block.body


# ── envelope_fit — view='validate' warn-tier finding ─────────────────────


def test_validate_envelope_fit_warns_on_a_protruding_bound_scene(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_drift", carts=[[30.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r2"},
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


def test_validate_envelope_fit_clean_when_atoms_fit(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_fits", carts=[[0.0, 0.0, 0.0]])[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_fits",
            "ports": {"p1": c_label},
        },
    ]
    handler.put(id="envfit5", text=json.dumps({"ops": ops}))
    resp = handler.get(id="envfit5", view="validate")
    assert "envelope_fit" not in resp.body


def test_validate_envelope_fit_skips_block_with_no_envelope(
    handler: NmHandler, structure: StructureHandler
) -> None:
    """A bound block with no declared envelope has nothing to check
    against — ``blocks_without_envelope`` already flags the missing
    geometry (when the block has ports); ``envelope_fit`` must not also
    raise/misfire on a ``None`` envelope."""
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
    validation = handler.get(id="envfit6", view="validate")
    assert "envelope_fit" not in validation.body


# ── filled-fraction honesty (view='validate' header) ─────────────────────


def test_validate_header_reads_unfilled_on_an_empty_bindingless_design(
    handler: NmHandler,
) -> None:
    ops = [
        {"op": "add_block", "name": "axle", "envelope": "cyl:r2h20"},
        {"op": "add_block", "name": "hub", "parent": "axle", "envelope": "sphere:r3"},
    ]
    handler.put(id="unfilled1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="unfilled1", view="validate")
    # zero findings alone would misread as "done" — the header must say
    # otherwise (the maze.py lesson, restated at the header's scale).
    assert "no validator findings" in resp.body
    assert "0/2 block(s) filled" in resp.body
    assert "UNFILLED" in resp.body


def test_validate_header_counts_partial_fill(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_partial")[0]
    ops = [
        {"op": "add_block", "name": "axle", "envelope": "sphere:r5"},
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5"},
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
    assert "1/2 block(s) filled" in resp.body
    assert "UNFILLED" not in resp.body


def test_validate_header_counts_full_fill(
    handler: NmHandler, structure: StructureHandler
) -> None:
    c_a = _make_structure(structure, "frag_full_a")[0]
    c_b = _make_structure(structure, "frag_full_b")[0]
    ops = [
        {"op": "add_block", "name": "axle", "envelope": "sphere:r5"},
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5"},
        {"op": "add_port", "block": "axle", "name": "p1"},
        {"op": "add_port", "block": "hub", "name": "p1"},
        {
            "op": "bind_structure",
            "block": "axle",
            "design": "frag_full_a",
            "ports": {"p1": c_a},
        },
        {
            "op": "bind_structure",
            "block": "hub",
            "design": "frag_full_b",
            "ports": {"p1": c_b},
        },
    ]
    handler.put(id="full1", text=json.dumps({"ops": ops}))
    resp = handler.get(id="full1", view="validate")
    assert "2/2 block(s) filled" in resp.body
    assert "UNFILLED" not in resp.body


def test_validate_header_excludes_instances_from_the_count(
    handler: NmHandler, structure: StructureHandler
) -> None:
    """An instance never owns a binding of its own (bind via the template)
    — the fraction counts ORDINARY blocks only, so one bound template with
    N instances reads as ``1/1``, never inflated by the instance count nor
    silently missing the instances as always-unfilled."""
    c_label = _make_structure(structure, "frag_tmpl")[0]
    ops = [
        {"op": "add_block", "name": "sugar", "envelope": "sphere:r5"},
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
    resp = handler.get(id="inst_fill1", view="validate")
    assert "1/1 block(s) filled" in resp.body


def test_fill_fraction_helper_no_blocks_reads_unfilled() -> None:
    from precis_nm.handler import _fill_fraction_line

    assert _fill_fraction_line(BlockTree()) == (
        "0/0 block(s) filled — no blocks declared yet (unfilled)"
    )


def test_fill_fraction_helper_all_unbound() -> None:
    from precis_nm.handler import _fill_fraction_line

    tree = BlockTree()
    tree.blocks["a"] = BlockNode(name="a")
    line = _fill_fraction_line(tree)
    assert line.startswith("0/1 block(s) filled")
    assert "UNFILLED" in line


# ── view='literature' — deterministic (no-LLM) query + search ────────────


def test_literature_view_whole_design_query_from_description_and_blocks(
    lit_handler: NmHandler,
) -> None:
    ops = [
        {
            "op": "add_block",
            "name": "axle",
            "envelope": "cyl:r2h20",
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


def test_literature_view_targets_one_block(lit_handler: NmHandler) -> None:
    ops = [
        {"op": "add_block", "name": "axle", "desc": "threading rod"},
        {"op": "add_block", "name": "crown", "desc": "macrocycle rim"},
    ]
    lit_handler.put(id="lit2", text=json.dumps({"ops": ops}))
    resp = lit_handler.get(id="lit2", view="literature", args={"block": "crown"})
    assert "macrocycle rim" in resp.body
    assert "threading rod" not in resp.body
    assert "block 'crown'" in resp.body


def test_literature_view_unknown_block_raises(lit_handler: NmHandler) -> None:
    lit_handler.put(id="lit3", text=json.dumps({"ops": []}))
    with pytest.raises(NotFound, match="no such block"):
        lit_handler.get(id="lit3", view="literature", args={"block": "ghost"})


def test_literature_query_is_deterministic(lit_handler: NmHandler) -> None:
    ops = [{"op": "add_block", "name": "axle", "desc": "threading rod"}]
    lit_handler.put(id="lit4a", text=json.dumps({"ops": ops}))
    lit_handler.put(id="lit4b", text=json.dumps({"ops": ops}))
    ref_a = lit_handler.store.get_ref(kind="nm", id="lit4a")
    ref_b = lit_handler.store.get_ref(kind="nm", id="lit4b")
    assert ref_a is not None
    assert ref_b is not None
    from precis_nm import persist

    tree_a = persist.load_tree(lit_handler.store, ref_a.id)
    tree_b = persist.load_tree(lit_handler.store, ref_b.id)
    q_a = lit_handler._literature_query(tree_a, ref_a)
    q_b = lit_handler._literature_query(tree_b, ref_b)
    assert q_a == q_b
    # re-derived again on a fresh load gives the same string
    tree_a_again = persist.load_tree(lit_handler.store, ref_a.id)
    assert lit_handler._literature_query(tree_a_again, ref_a) == q_a


def test_literature_query_includes_objective_vocabulary_on_connects(
    lit_handler: NmHandler,
) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "sphere:r2"},
        {"op": "add_block", "name": "b", "envelope": "sphere:r2"},
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
    resp = lit_handler.get(id="lit5", view="literature", args={"block": "a"})
    assert "pi_stack" in resp.body
    # the untouched sibling block's connect vocabulary is out of scope too
    resp_b_only = lit_handler.get(id="lit5", view="literature", args={"block": "b"})
    assert "pi_stack" in resp_b_only.body  # b is the OTHER endpoint of that connect


def test_literature_query_includes_bound_composition_fair_game(
    lit_handler: NmHandler, structure: StructureHandler
) -> None:
    c_label = _make_structure(structure, "frag_lit")[0]
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "sphere:r5"},
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
    lit_handler: NmHandler, store: Store
) -> None:
    ops = [
        {
            "op": "add_block",
            "name": "crown",
            "desc": "cyclodextrin macrocycle rim",
        }
    ]
    lit_handler.put(id="lit7", text=json.dumps({"ops": ops}))
    _seed_paper(
        store,
        slug="cd_paper",
        title="Cyclodextrin rotaxane macrocycles",
        # the lexical search leg is a strict AND (structure precedent's
        # own doc note) — every generated query token ("crown"
        # included, the block's own name) must appear in the body.
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


def test_literature_view_no_match_gives_a_recovery_hint(lit_handler: NmHandler) -> None:
    ops = [{"op": "add_block", "name": "axle", "desc": "threading rod"}]
    lit_handler.put(id="lit8", text=json.dumps({"ops": ops}))
    resp = lit_handler.get(id="lit8", view="literature")
    assert "no matching papers" in resp.body
    assert "search(kind='paper'" in resp.body


def test_literature_query_falls_back_to_block_names_when_nothing_else(
    lit_handler: NmHandler,
) -> None:
    """No description, no desc/use, no connects, no binding — the query
    must still never be empty."""
    ops = [{"op": "add_block", "name": "axle"}]
    lit_handler.put(id="lit9", text=json.dumps({"ops": ops}))
    ref = lit_handler.store.get_ref(kind="nm", id="lit9")
    assert ref is not None
    from precis_nm import persist

    tree = persist.load_tree(lit_handler.store, ref.id)
    query = lit_handler._literature_query(tree, ref)
    assert query.strip()
    assert "axle" in query
