"""``join`` — the se-side block joiner op (docs/backlog/
hexfold-integration.md step 5 slice 1): :mod:`precis_se.atomic.join`'s
store-aware wrapper around :mod:`hexfold.join`'s pure ``compose``.

Conventions follow :mod:`tests.test_se_hexfold_generator`'s adapter
round-trip section: call :func:`~precis_se.atomic.generate.prepare_generate`/
``finish_generate`` directly (no handler) to mint each side, then the same
prepare/finish pair for ``join``.
"""

from __future__ import annotations

import pytest

from hexfold.join import SEAM_RADIUS
from precis.errors import BadInput
from precis.store import Store
from precis_se.atomic.generate import finish_generate, prepare_generate
from precis_se.atomic.join import finish_join, prepare_join
from precis_se.handler import _render_block
from precis_se.ops import SeTree, apply_ops

#: zigzag N=8 tube -- the same shape `tests/hexfold/test_join.py`'s
#: `test_two_tube_fuse_matches_whole_spec_build` pins at the stick rung.
_TUBE_Z8 = "hexfold 0.2\na: tube(8,0, len=3)\n"
#: pure-z (N=12) onto pure-a (N=12): the grain-boundary adapter motif.
_TUBE_Z12 = "hexfold 0.2\na: tube(12,0, len=2)\n"
_TUBE_A12 = "hexfold 0.2\nb: tube(6,6, len=2)\n"


def _generate(
    store: Store, tree: SeTree, name: str, spec: str, design_slug: str
) -> None:
    _echo, pending = prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": spec},
            "name": name,
        },
        design_slug,
    )
    assert pending is not None
    finish_generate(store, tree, pending)


def _join(
    store: Store, tree: SeTree, design_slug: str, **op: object
) -> tuple[str, SeTree]:
    op.setdefault("op", "join")
    echo, pending = prepare_join(store, tree, op, design_slug)
    assert pending is not None
    finish_join(store, tree, pending)
    return echo, tree


def test_join_mints_composite_with_ports_bond_and_record(store: Store) -> None:
    tree = SeTree()
    design_slug = "hx-join1"
    _generate(store, tree, "tube_a", _TUBE_Z8, design_slug)
    _generate(store, tree, "tube_b", _TUBE_Z8, design_slug)

    echo, tree = _join(
        store,
        tree,
        design_slug,
        name="composite",
        a="tube_a.out",
        b="tube_b.in",
    )
    assert "composite" in tree.blocks
    node = tree.blocks["composite"]
    assert node.bound_kind == "structure"
    assert node.bound == f"{design_slug}-composite"
    assert "composite" in echo

    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    scene, _handles = store.structure_load(ref.id)
    n_a = 96  # tube(8,0,len=3): 96 atoms (test_hexfold_tube_counts_and_ports sibling)
    assert len(scene.atoms) == 2 * n_a

    assert set(node.ports) == {"tube_a_in", "tube_b_out"}
    for port_name in ("tube_a_in", "tube_b_out"):
        port = node.ports[port_name]
        assert port.annotations.get("lattice") == "sp2-hex"
        assert port.annotations.get("payload", {}).get("type") == "z8"
        atoms = port.annotations.get("atoms")
        assert isinstance(atoms, list) and len(atoms) == 8
        assert all(label in scene.atoms for label in atoms)
        assert port.bound_design == node.bound
        assert port.bound_atom in scene.atoms

    # the seam bond
    conn = [
        c
        for c in tree.connects
        if {(c.a_block, c.a_port), (c.b_block, c.b_port)}
        == {("tube_a", "out"), ("tube_b", "in")}
    ]
    assert len(conn) == 1
    assert conn[0].kind == "bond"

    # b was re-posed and re-parented under the composite
    b_node = tree.blocks["tube_b"]
    assert b_node.parent == "composite"
    assert tree.blocks["tube_a"].parent == "composite"
    assert b_node.local_pose != [0.0, 0.0, 0.0]
    assert b_node.pose != [0.0, 0.0, 0.0]

    rec = (ref.meta or {})["generated"]
    assert rec["generator"] == "join"
    assert rec["lattice"] == "sp2-hex"
    assert len(rec["parts"]) == 2
    assert {p["block"] for p in rec["parts"]} == {"tube_a", "tube_b"}
    # jsonb round-trip: a ring-size census's int keys come back as strings.
    assert rec["seam"]["rings"] == {"6": 8}
    assert rec["n_atoms"] == 2 * n_a

    body = _render_block(tree, node, store, ref_id=0)
    assert "## generated (join)" in body


def test_join_composite_takes_over_as_pre_join_pose_net_a_unchanged(
    store: Store,
) -> None:
    """Design call 2: the composite inherits a's pre-join pose/parent
    frame, a itself goes to identity under the composite -- net effect,
    a's world placement is bit-for-bit unchanged by a join, and b's world
    placement is exactly the join's own seam transform shifted by that
    same pre-existing offset. b's own pre-join pose (also non-identity
    here) is dropped -- b is placed by the seam, never by whatever pose
    it carried before -- and that is reported, not silent
    (``join.pose_dropped``)."""
    baseline_tree = SeTree()
    baseline_slug = "hx-join-pose-baseline"
    _generate(store, baseline_tree, "tube_a", _TUBE_Z8, baseline_slug)
    _generate(store, baseline_tree, "tube_b", _TUBE_Z8, baseline_slug)
    _join(
        store,
        baseline_tree,
        baseline_slug,
        name="composite",
        a="tube_a.out",
        b="tube_b.in",
    )
    baseline_b = baseline_tree.blocks["tube_b"]

    tree = SeTree()
    design_slug = "hx-join-pose"
    _generate(store, tree, "tube_a", _TUBE_Z8, design_slug)
    _generate(store, tree, "tube_b", _TUBE_Z8, design_slug)
    # a: a pure translation (no rotation) pre-join pose -- arithmetically
    # simple to compose by hand (world = local when top-level, and a
    # translation-only parent composes additively onto its child).
    p = [1e-9, 2e-9, 3e-9]
    apply_ops(
        tree,
        [{"op": "set_pose", "block": "tube_a", "pose": p, "rot": [0.0, 0.0, 0.0]}],
    )
    a_world_before = list(tree.blocks["tube_a"].pose)
    assert a_world_before == p
    # b: a non-identity pre-join pose too -- must be dropped, and noted.
    apply_ops(
        tree,
        [
            {
                "op": "set_pose",
                "block": "tube_b",
                "pose": [9e-9, 0.0, 0.0],
                "rot": [0.0, 0.0, 0.0],
            }
        ],
    )

    _join(store, tree, design_slug, name="composite", a="tube_a.out", b="tube_b.in")

    a_node = tree.blocks["tube_a"]
    assert list(a_node.pose) == pytest.approx(a_world_before)
    assert list(a_node.local_pose) == pytest.approx([0.0, 0.0, 0.0])

    b_node = tree.blocks["tube_b"]
    assert list(b_node.pose) == pytest.approx(
        [baseline_b.pose[i] + p[i] for i in range(3)]
    )
    assert list(b_node.rot) == pytest.approx(list(baseline_b.rot))

    composite = tree.blocks["composite"]
    assert list(composite.local_pose) == pytest.approx(p)
    assert composite.parent is None

    assert composite.bound is not None
    ref = store.get_ref(kind="structure", id=composite.bound)
    assert ref is not None
    rec = (ref.meta or {})["generated"]
    codes = {f["code"] for f in rec["report"]["findings"]}
    assert "join.pose_dropped" in codes


def test_second_join_onto_composite_port(store: Store) -> None:
    tree = SeTree()
    design_slug = "hx-join2"
    _generate(store, tree, "tube_a", _TUBE_Z8, design_slug)
    _generate(store, tree, "tube_b", _TUBE_Z8, design_slug)
    _join(store, tree, design_slug, name="composite", a="tube_a.out", b="tube_b.in")

    _generate(store, tree, "tube_c", _TUBE_Z8, design_slug)
    echo, tree = _join(
        store,
        tree,
        design_slug,
        name="composite2",
        a="composite.tube_b_out",
        b="tube_c.in",
    )
    assert "composite2" in tree.blocks
    node = tree.blocks["composite2"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    scene, _handles = store.structure_load(ref.id)
    assert len(scene.atoms) == 3 * 96
    assert "composite2" in echo


def test_join_k_fit_surfaces_alternatives(store: Store) -> None:
    tree = SeTree()
    design_slug = "hx-join-fit"
    _generate(store, tree, "tube_a", _TUBE_Z12, design_slug)
    _generate(store, tree, "tube_b", _TUBE_A12, design_slug)

    echo, tree = _join(
        store,
        tree,
        design_slug,
        name="adapter",
        a="tube_a.out",
        b="tube_b.in",
        k="fit",
    )
    node = tree.blocks["adapter"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    rec = (ref.meta or {})["generated"]
    codes = {f["code"] for f in rec["report"]["findings"]}
    assert "fit.alternatives" in codes
    assert "seam.adapter" in codes
    assert rec["seam"]["motif"] == "adapter"
    assert "adapter" in echo


def test_join_port_mismatch_mints_no_orphan(store: Store) -> None:
    tree = SeTree()
    design_slug = "hx-join-mismatch"
    _generate(store, tree, "tube_a", _TUBE_Z8, design_slug)
    _generate(store, tree, "tube_b", _TUBE_Z12, design_slug)

    with pytest.raises(BadInput, match="port.mismatch"):
        prepare_join(
            store,
            tree,
            {"op": "join", "name": "bad", "a": "tube_a.out", "b": "tube_b.in"},
            design_slug,
        )
    assert "bad" not in tree.blocks
    assert store.get_ref(kind="structure", id=f"{design_slug}-bad") is None


def test_join_seam_radius_override_threads_through_the_op(store: Store) -> None:
    """The op's ``seam_radius`` key (`hexfold.join.compose`'s own
    per-side override, `tests/hexfold/test_join.py`'s
    ``test_seam_radius_override_fires_leak_silent_at_table_radius`` at the
    pure-numpy layer) makes it all the way from the op dict to `compose` --
    silent at the table radius (the default `_join` calls above never pass
    one), ``seam.leak`` fires once tightened."""
    tree = SeTree()
    design_slug = "hx-join-radius"
    _generate(store, tree, "tube_a", _TUBE_Z8, design_slug)
    _generate(store, tree, "tube_b", _TUBE_Z8, design_slug)

    echo, tree = _join(
        store,
        tree,
        design_slug,
        name="composite",
        a="tube_a.out",
        b="tube_b.in",
        seam_radius={"a": 1},
    )
    node = tree.blocks["composite"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    rec = (ref.meta or {})["generated"]
    assert rec["seam_radius"] == {"a": 1, "b": SEAM_RADIUS["z"]}
    leaks = [f for f in rec["report"]["findings"] if f["code"] == "seam.leak"]
    assert len(leaks) == 1, rec["report"]["findings"]
    assert leaks[0]["data"]["block"] == "a"
    assert leaks[0]["data"]["r"] == 1
    assert "composite" in echo


def test_join_lattice_absent_names_the_port_and_the_fix(store: Store) -> None:
    """gr456201: the overwhelmingly common ``join.lattice`` case is a
    block that predates the ``lattice`` annotation being minted at
    generate time (measured in prod: all 77 active ports had none) --
    ``plain.p`` here is a stand-in for exactly that (a hand-added port
    never carries one either). The message has to name the empty side
    and say what to do about it, not just report a disagreement."""
    tree = SeTree()
    design_slug = "hx-join-lattice"
    _generate(store, tree, "tube_a", _TUBE_Z8, design_slug)
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "plain", "envelope": "cyl:r2e-10h2e-09"},
            {"op": "add_port", "block": "plain", "name": "p", "roles": ["covalent"]},
        ],
    )
    with pytest.raises(BadInput, match="join.lattice") as excinfo:
        prepare_join(
            store,
            tree,
            {"op": "join", "name": "bad", "a": "tube_a.out", "b": "plain.p"},
            design_slug,
        )
    assert "bad" not in tree.blocks
    message = str(excinfo.value)
    assert "plain.p" in message
    assert "tube_a.out" not in message  # only the EMPTY side is named
    assert "no lattice annotation" in message
    assert "regenerate" in message.lower()
    assert "generate" in message


def test_join_lattice_mismatch_names_both_values(store: Store) -> None:
    """Both sides present but genuinely different -- a distinct message
    from the absent case, naming both values (regenerating fixes nothing
    here; the two ports are typed for different joiners)."""
    tree = SeTree()
    design_slug = "hx-join-lattice-mismatch"
    _generate(store, tree, "tube_a", _TUBE_Z8, design_slug)
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "other", "envelope": "cyl:r2e-10h2e-09"},
            {
                "op": "add_port",
                "block": "other",
                "name": "p",
                "roles": ["covalent"],
                "annotations": {"lattice": "sp3-diamond"},
            },
        ],
    )
    with pytest.raises(BadInput, match="join.lattice") as excinfo:
        prepare_join(
            store,
            tree,
            {"op": "join", "name": "bad", "a": "tube_a.out", "b": "other.p"},
            design_slug,
        )
    assert "bad" not in tree.blocks
    message = str(excinfo.value)
    assert "'sp2-hex'" in message
    assert "'sp3-diamond'" in message
    assert "don't share a joinable lattice" in message


def test_join_stale_when_stored_ref_disagrees_with_its_spec(store: Store) -> None:
    tree = SeTree()
    design_slug = "hx-join-stale"
    _generate(store, tree, "tube_a", _TUBE_Z8, design_slug)
    _generate(store, tree, "tube_b", _TUBE_Z8, design_slug)

    tube_a_bound = tree.blocks["tube_a"].bound
    assert tube_a_bound is not None
    ref_a = store.get_ref(kind="structure", id=tube_a_bound)
    assert ref_a is not None
    generated = dict(ref_a.meta["generated"])
    # a DIFFERENT spec (different atom count) in place of the real one --
    # the stored atoms no longer agree with a rebuild of "their own" spec.
    generated["spec"] = "hexfold 0.2\na: tube(8,0, len=1)\n"
    store.stamp_ref_meta(ref_a.id, {"generated": generated})

    with pytest.raises(BadInput, match="join.stale"):
        prepare_join(
            store,
            tree,
            {"op": "join", "name": "bad", "a": "tube_a.out", "b": "tube_b.in"},
            design_slug,
        )
    assert "bad" not in tree.blocks


def test_join_reparent_out_of_an_existing_composite_warns(store: Store) -> None:
    """gr456213 (2026-09-29 prod dogfood repro): joining ``tube_c`` (already
    a part of ``chain3``) into a THIRD composite is accepted -- re-parenting
    it silently would leave ``chain3``'s block tree not listing the part its
    own build record and exposed port still claim. No hard refusal (the
    ordinary chained-join flow re-parents a fresh, never-parented block on
    every join -- that must stay silent), but the second re-parent gets a
    named WARN."""
    tree = SeTree()
    design_slug = "hx-join-reparent"
    _generate(store, tree, "tube_a", _TUBE_Z8, design_slug)
    _generate(store, tree, "tube_b", _TUBE_Z8, design_slug)
    _generate(store, tree, "tube_c", _TUBE_Z8, design_slug)
    _generate(store, tree, "tube_fat", _TUBE_Z8, design_slug)

    _join(store, tree, design_slug, name="composite", a="tube_a.out", b="tube_b.in")
    _join(
        store,
        tree,
        design_slug,
        name="chain3",
        a="composite.tube_b_out",
        b="tube_c.in",
    )
    # ordinary chained-join re-parenting (composite, tube_c: both freshly
    # unparented beforehand) must NOT warn.
    chain3_bound = tree.blocks["chain3"].bound
    assert chain3_bound is not None
    chain3_ref = store.get_ref(kind="structure", id=chain3_bound)
    assert chain3_ref is not None
    chain3_rec = (chain3_ref.meta or {})["generated"]
    chain3_codes = {f["code"] for f in chain3_rec["report"]["findings"]}
    assert "join.reparented" not in chain3_codes

    echo, tree = _join(
        store,
        tree,
        design_slug,
        name="mixed_sigma",
        a="tube_fat.out",
        b="tube_c.out",
    )

    # tube_c was silently pulled out of chain3 -- structural consequence:
    # chain3's own block tree no longer lists it as a child...
    chain3_children = {n.name for n in tree.blocks.values() if n.parent == "chain3"}
    assert chain3_children == {"composite"}
    assert "tube_c" not in chain3_children
    # ...while chain3's build record still names tube_c as a part and its
    # port is still there.
    assert {p["block"] for p in chain3_rec["parts"]} == {"composite", "tube_c"}
    assert "chain3" in tree.blocks
    assert "tube_c_out" in tree.blocks["chain3"].ports

    node = tree.blocks["mixed_sigma"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    rec = (ref.meta or {})["generated"]
    findings = [f for f in rec["report"]["findings"] if f["code"] == "join.reparented"]
    assert len(findings) == 1, rec["report"]["findings"]
    finding = findings[0]
    assert finding["severity"] == "WARN"
    assert finding["data"]["block"] == "tube_c"
    assert finding["data"]["old_composite"] == "chain3"
    assert finding["data"]["new_composite"] == "mixed_sigma"
    assert "tube_c" in finding["message"]
    assert "chain3" in finding["message"]
    assert "mixed_sigma" in finding["message"]
    assert tree.blocks["tube_c"].parent == "mixed_sigma"
    assert "mixed_sigma" in echo
