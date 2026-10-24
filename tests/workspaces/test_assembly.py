"""``precis_geom.assembly`` — structure-workspaces slice 2a.1 acceptance.

The row's four claims: ``array_block`` and a packed ``pga.tile`` on one
template mint the same blocktree (same array node, byte-for-byte);
``tile(fused=True, n=3)`` of a 2-period prism equals the 6-period
prism's ``structure_sha``; mirroring a chiral tube flips its chiral
index and emits the ``chirality.flipped`` INFO; a nested assembly
places through its parent's port and ``frame:`` on it selects every
child atom.
"""

from __future__ import annotations

import json
import math
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.structure.cache import structure_sha
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_geom import assembly as pga
from precis_geom import out as geom_out
from precis_geom import trace as _trace


def _scene(parts: list[tuple[str, tuple[float, float, float]]]) -> Scene:
    """A Scene at cartesian Å positions (identity 50 Å cell, no bonds)."""
    cell = Cell(np.eye(3) * 50.0, (False, False, False))
    atoms = {}
    for i, (el, cart) in enumerate(parts):
        la = f"a{el}{i + 1}"
        atoms[la] = Atom(label=la, element=el, frac=np.asarray(cart) / 50.0)
    return Scene(cell=cell, atoms=atoms)


def _subtree_ids(doc: dict[str, Any], key: str) -> set[int]:
    """Every run-local frame id ``key``'s subtree covers — the set
    ``frame:<key>`` resolves ``maker`` against (traceview's rule)."""
    kids: dict[str, list[str]] = {}
    by_key = {f["key"]: f for f in doc["frames"]}
    for f in doc["frames"]:
        if f.get("parent") is not None:
            kids.setdefault(f["parent"], []).append(f["key"])
    out: set[int] = set()
    stack = [key]
    while stack:
        k = stack.pop()
        if k in by_key:
            out.add(by_key[k]["id"])
        stack += kids.get(k, [])
    return out


def test_packed_tile_mints_the_same_array_node() -> None:
    """The DRY check that needs no DB: ``pga.tile``'s canonical array
    spec is what ``array_block``'s ``_parse_array_spec`` normalises to,
    so a script-authored packed tile and a hand-authored array store
    the identical node."""
    from precis_se.ops import _parse_array_spec

    g = pga.tile("p", 3, pitch=[0.0, 0.0, 4.0])  # Å — atomic native
    spec = g.array
    assert spec == _parse_array_spec(
        {"linear": {"count": 3, "pitch": 4e-10, "axis": [0, 0, 1]}}
    )


def test_packed_tile_tree_matches_array_block(store: Any, tmp_path: Path) -> None:
    """End-to-end: an ``array_block`` op and a ``pga.tile`` manifest
    instance on the same template give the same blocktree (block names,
    template/parent links, poses and the stored array spec — the
    geometry-bound fields legitimately differ: the script path carries
    the payload the op path leaves bare)."""
    from precis_se import persist
    from precis_se.ops import SeTree, apply_ops
    from precis_se.workspace_import import import_workspace_run

    linear = {"count": 3, "pitch": 4e-10, "axis": [0, 0, 1]}
    ops_tree = apply_ops(
        SeTree(),
        [
            {"op": "add_block", "name": "p"},
            {"op": "array_block", "name": "arr", "template": "p", "linear": linear},
        ],
    )

    run = geom_out.Run(tmp_path)
    with _trace.run():
        run.block("p", _scene([("C", (0.0, 0.0, 0.0))]))
        run.instance("arr", template=pga.tile("p", 3, pitch=[0.0, 0.0, 4.0]))
    run.finish()
    manifest = tmp_path / "manifest.json"
    assert manifest.is_file()

    ref = store.insert_ref(kind="se", slug="ws-2a-test", title="ws 2a", meta={})
    job = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    import_workspace_run(
        store,
        workspace="hcol-test",
        design="ws-2a-test",
        out_dir=tmp_path,
        run_job=int(job.id),
        script_sha="deadbee5",
        lock_sha="1ock",
        image_digest="sha256:img",
        params={},
        requested_by="agent:test",
    )
    imp_tree = persist.load_tree(store, int(ref.id))

    def projection(tree: Any) -> dict[str, tuple[Any, ...]]:
        return {
            name: (b.template, b.parent, b.array, tuple(b.pose), tuple(b.rot))
            for name, b in tree.blocks.items()
        }

    assert projection(imp_tree) == projection(ops_tree)
    assert imp_tree.blocks["arr"].array == {
        "kind": "linear",
        "count": 3,
        "pitch": 4e-10,
        "axis": [0.0, 0.0, 1.0],
    }


def test_fused_tile_equals_longer_prism() -> None:
    """``tile(fused=True, n=3)`` of a 2-period prism at its own period
    step is the 6-period prism atom-for-atom (guard-column overlap —
    the k2 fuse across each boundary)."""
    from hexfold.lattice import Lattice
    from hexfold.prism import prism_tube

    a = math.sqrt(3.0) * Lattice().sigma_A
    net2, _ = prism_tube(periods=2, rows=4, strip_rows=2, seams="++++++")
    net6, _ = prism_tube(periods=6, rows=4, strip_rows=2, seams="++++++")
    with pytest.warns(UserWarning, match="unrelaxed seed3"):
        g2, g6 = pga.geom(net2), pga.geom(net6)
    with _trace.run():
        fused = pga.tile(g2, 3, period=[0.0, 0.0, 2.0 * a], fused=True)
    flat = fused.flatten()
    assert structure_sha(flat) == structure_sha(g6.scene)
    assert len(flat.bonds) == len(g6.scene.bonds)


def _carts(scene: Scene) -> np.ndarray:
    return np.asarray([scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()])


def _bond_lengths(scene: Scene) -> np.ndarray:
    return np.asarray(
        [
            np.linalg.norm(
                scene.cell.frac_to_cart(scene.atoms[b.i].frac)
                - scene.cell.frac_to_cart(scene.atoms[b.j].frac)
            )
            for b in scene.bonds
        ]
    )


def test_geom_net_coords_kwarg() -> None:
    """gr514053 — ``pga.geom(net, coords=xyz)`` exports ``xyz`` in
    ``Net.atoms`` ord order instead of the unrelaxed ``seed3``; a
    wrong-shaped array refuses with the fix; ``meta`` records where the
    positions came from."""
    from hexfold.prism import prism_tube
    from hexfold.stick import stick

    net, _ = prism_tube(4, 4, 2, "++++++")
    xyz = stick(net)
    g = pga.geom(net, coords=xyz)
    assert g.meta["coords"] == "relaxed"
    np.testing.assert_allclose(_carts(g.scene), xyz, atol=1e-6)
    # the relaxed export's bonds spread around 1.42 A; the seed's are
    # all exactly 1.420 (the silent export the gripe hit)
    lengths = _bond_lengths(g.scene)
    assert np.std(lengths) > 1e-3
    with pytest.warns(UserWarning, match="unrelaxed seed3"):
        g_seed = pga.geom(net)
    assert g_seed.meta["coords"] == "seed"
    np.testing.assert_allclose(_bond_lengths(g_seed.scene), 1.420)
    with pytest.raises(ValueError, match=rf"\({len(net.atoms)},3\)"):
        pga.geom(net, coords=xyz[:-1])


def test_geom_net_seed_export_warns() -> None:
    """gr514053 — a bare ``pga.geom(net)`` still exports ``seed3`` but
    warns once per call, naming the unrelaxed seed and the
    ``coords=stick(net)`` fix; ``coords=`` and a ``seed_kind='relaxed'``
    Net stay quiet."""
    from dataclasses import replace

    from hexfold.prism import prism_tube
    from hexfold.stick import stick

    net, _ = prism_tube(4, 4, 2, "++++++")
    with pytest.warns(UserWarning, match="unrelaxed seed3.*coords=stick"):
        pga.geom(net)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pga.geom(net)
    assert len(caught) == 1
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        pga.geom(net, coords=stick(net))
        pga.geom(replace(net, seed_kind="relaxed"))


def test_net_with_stick_marks_coords_relaxed() -> None:
    """``net.with_stick()`` re-seats ``seed3`` on the relaxed geometry
    and marks it, so ``pga.geom`` exports it without the seed warning."""
    from hexfold.prism import prism_tube
    from hexfold.stick import stick

    net, _ = prism_tube(4, 4, 2, "++++++")
    relaxed = net.with_stick()
    assert relaxed.seed_kind == "relaxed"
    assert relaxed.seed3 is not None
    np.testing.assert_allclose(np.asarray(relaxed.seed3, dtype=np.float64), stick(net))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        g = pga.geom(relaxed)
    assert g.meta["coords"] == "relaxed"  # seed3 IS the relaxed array now
    assert np.std(_bond_lengths(g.scene)) > 1e-3
    with pytest.raises(ValueError, match="with_stick coords"):
        net.with_stick(np.zeros((3, 3)))


def test_mirror_flips_chiral_index() -> None:
    """A chiral tube through ``mirror`` swaps (n, m) → (m, n), emits
    the ``chirality.flipped`` INFO, and actually reflects the
    geometry — through ``flatten`` and a nested ``assembly`` too, not
    just the raw scene (a 180°-rotation stand-in is a different
    transform and fails here)."""
    from precis_se.atomic.generators.sp2 import build_cnt

    gen = build_cnt({"n": 6, "m": 4, "length_A": 30.0})
    g = pga.geom(gen, meta={"chiral_index": list(gen.topology["chiral_index"])})
    before = _carts(g.scene)
    with _trace.run():
        m = pga.mirror(g, "yz")
        nested = pga.assembly("a", children=[m]).flatten()
    assert m.meta["chiral_index"] == [4, 6]
    assert any("chirality.flipped" in f for f in m.findings)
    for name, carts in (
        ("scene", _carts(m.scene)),
        ("flatten", _carts(m.flatten())),
        ("assembly", _carts(nested)),
    ):
        assert np.allclose(carts[:, 0], -before[:, 0]), name
        assert np.allclose(carts[:, 1], before[:, 1]), name
        assert np.allclose(carts[:, 2], before[:, 2]), name


def test_nested_assembly_places_through_parent_port() -> None:
    """``place`` lands the sub-assembly's ``rim`` port on the parent's
    ``dock`` — through a parent that is itself rotated AND translated,
    with a rim direction that needs a real rotation in the solve
    (dock +x → rim +z mated, not the anti-parallel shortcut that makes
    R a free identity). The flattened world positions are asserted —
    a ``place`` that drops rotation, an ``assembly`` that drops a
    child's pose, or a swapped pose compose each moves atoms visibly;
    ``frame:`` on the sub's ``assembly`` frame selects every child
    atom."""
    from precis.cad.vec import Transform

    # parent: rotated 90° about z, then translated +10 y — its dock
    # port sits at world (0, 15, 0) pointing +y.
    rz = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    parent_g = pga.geom(_scene([("C", (0.0, 0.0, 0.0)), ("C", (5.0, 0.0, 0.0))]))
    parent = pga.assembly(
        "p",
        children=[parent_g],
        pose=Transform(R=rz, t=np.array([0.0, 10.0, 0.0])),
        ports={
            "dock": pga.Port(
                name="dock", pos=(5.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)
            )
        },
    )
    leaf_a = pga.geom(_scene([("C", (0.0, 0.0, 0.0))]))
    leaf_b = pga.geom(_scene([("O", (1.0, 1.0, 0.0))]))
    t_inner = Transform(R=np.eye(3), t=np.array([0.0, 1.0, 0.0]))
    with _trace.run():
        sub = pga.assembly(
            "sub",
            children=[leaf_a, (leaf_b, t_inner)],
            ports={
                "rim": pga.Port(
                    name="rim", pos=(1.0, 0.0, 0.0), direction=(0.0, 0.0, 1.0)
                )
            },
        )
        placed = pga.place(sub, at=parent.port("dock"), align=sub.port("rim"))
        outer = pga.assembly("outer", children=[parent, placed])
        flat = outer.flatten()
        doc = _trace.dump()
    assert doc is not None

    # world positions — parents' atoms, then the sub's placed atoms.
    carts = {tuple(np.round(c, 6)) for c in _carts(flat)}
    assert carts == {
        (0.0, 10.0, 0.0),  # parent C(0,0,0) under the posed parent
        (0.0, 15.0, 0.0),  # parent C(5,0,0) — the dock's world spot
        (-1.0, 15.0, 0.0),  # sub leaf_a, placed
        (0.0, 15.0, 2.0),  # sub leaf_b, inner-offset then placed
    }
    # the rim lands on the dock, mated anti-parallel in world frame
    assert np.allclose(placed.pose.apply(np.array([1.0, 0.0, 0.0])), [0.0, 15.0, 0.0])
    assert np.allclose(
        placed.pose.apply_dir(np.array([0.0, 0.0, 1.0])), [0.0, -1.0, 0.0]
    )

    # frame: on the sub assembly selects its own atoms — 'sub' is the
    # second 'assembly' frame in doc order ('p' was built first,
    # 'outer' last); occ is per call-line, not global.
    asm_frames = [f for f in doc["frames"] if f["qual"] == "assembly"]
    sub_key = asm_frames[1]["key"]
    sub_ids = _subtree_ids(doc, sub_key)
    sub_atoms = [a for a in flat.atoms.values() if a.maker in sub_ids]
    assert len(sub_atoms) == 2  # leaf_a + leaf_b, both under sub's subtree
    assert len(flat.atoms) == 4
    other = [a for a in flat.atoms.values() if a.maker not in sub_ids]
    assert len(other) == 2 and all(a.maker is not None for a in other)


def test_transform_on_a_posed_object_moves_it_in_world_axes() -> None:
    """``translate``/``rotate`` on an object with ``pose`` composes the
    pose — the move is in the parent frame, not the object's local
    axes: a rotated object translated +z ends up shifted along world
    +z, and its local scene is untouched."""
    target = pga.geom(
        _scene([("C", (5.0, 0.0, 0.0))]),
        ports={
            "dock": pga.Port(
                name="dock", pos=(5.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0)
            )
        },
    )
    child = pga.geom(
        _scene([("N", (0.0, 0.0, 0.0))]),
        ports={
            "rim": pga.Port(name="rim", pos=(0.0, 0.0, 0.0), direction=(0.0, 0.0, 1.0))
        },
    )
    posed = pga.place(child, at=target.port("dock"), align=child.port("rim"))
    moved = pga.translate(posed, [0.0, 0.0, 7.0])
    assert posed.pose is not None and moved.pose is not None
    assert np.allclose(np.asarray(moved.pose.t), np.asarray(posed.pose.t) + [0, 0, 7])
    # local geometry is a recipe the pose now composes over — untouched
    assert np.allclose(_carts(moved.scene), _carts(child.scene))
    flat = _carts(moved.flatten())
    assert np.allclose(flat, _carts(posed.flatten()) + [0.0, 0.0, 7.0])


def test_rotated_symbolic_instance_round_trips(store: Any, tmp_path: Path) -> None:
    """A rotated symbolic instance lands pose AND rot (with
    ``rot_unit='rad'``) in the manifest and imports through the op's
    unit parser; an improper (det −1) placement refuses instead."""
    import pytest

    from precis.cad.vec import Transform, reflection
    from precis_se import persist
    from precis_se.workspace_import import import_workspace_run

    rx90 = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
    inst = pga.instance("p", pose=Transform(R=rx90, t=np.array([5.0, 0.0, 0.0])))
    run = geom_out.Run(tmp_path)
    run.block("p", _scene([("C", (0.0, 0.0, 0.0))]))
    run.instance("ri", template=inst)
    run.finish()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    row = manifest["instances"][0]
    assert row["rot_unit"] == "rad" and row["pose_unit"] == "Å"

    ref = store.insert_ref(kind="se", slug="ws-2a-rot", title="x", meta={})
    job = store.insert_ref(kind="job", slug=None, title="x", meta={})
    import_workspace_run(
        store,
        workspace="hcol-test",
        design="ws-2a-rot",
        out_dir=tmp_path,
        run_job=int(job.id),
        script_sha="x",
        lock_sha="x",
        image_digest="sha256:i",
        params={},
        requested_by="agent:test",
    )
    node = persist.load_tree(store, int(ref.id)).blocks["ri"]
    assert node.local_pose[0] == pytest.approx(5e-10)  # 5 Å in metres
    assert node.local_rot[0] == pytest.approx(math.pi / 2)

    bad = pga.instance("p", pose=reflection(np.array([1.0, 0.0, 0.0])))
    with pytest.raises(ValueError, match="reflection|improper"):
        geom_out.Run(tmp_path / "bad").instance("m", template=bad)


def test_array_block_spec_is_pga_canonical() -> None:
    """The op's stored array spec is the canonical dict
    ``pga.array_spec`` produces — one number path shared by the op and
    the script (structure-workspaces 2a.2)."""
    from precis_se.ops import SeTree, apply_ops

    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "t"},
            {
                "op": "array_block",
                "name": "arr",
                "template": "t",
                "linear": {"count": 3, "pitch": 1.2e-9, "axis": [0, 0, 1]},
            },
        ],
    )
    assert tree.blocks["arr"].array == pga.array_spec(
        {"kind": "linear", "count": 3, "pitch": 1.2e-9, "axis": [0, 0, 1]}
    )


def test_construction_ops_mint_op_frames() -> None:
    """Each construction op records one ``op`` frame (``line`` = the
    tool_ledger call id — ``0`` outside an MCP session); bookkeeping
    ops mint none."""
    from precis_se.ops import SeTree, apply_ops

    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "t"},
            {"op": "instance_block", "name": "i1", "template": "t"},
            {
                "op": "set_pose",
                "block": "i1",
                "pose": [0.01, 0.0, 0.0],
                "pose_unit": "m",
            },
        ],
    )
    assert [f["qual"] for f in tree.op_frames] == [
        "instance_block",
        "set_pose",
    ]
    assert all(
        f["kind"] == "op" and f["parent"] == "op" and isinstance(f["line"], int)
        for f in tree.op_frames
    )


def test_op_trace_flushes_to_design_and_bound_refs(store: Any) -> None:
    """On ``save_tree`` the minted op frames land in a ``ws_trace``
    chunk on the design ref (root ``'op'``), and a touched bound
    block's ``meta['frames']``/``generated.trace`` point at the op
    frame — the record ``made-by`` reads for ``op set_pose``."""
    from precis_se import persist
    from precis_se.ops import SeTree, apply_ops

    bref = store.insert_ref(
        kind="structure", slug="bnd-s", title="x", meta={"generated": {}}
    )
    design = store.insert_ref(kind="se", slug="d-op1", title="x", meta={})
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "b"},
            {
                "op": "set_pose",
                "block": "b",
                "pose": [0.01, 0.0, 0.0],
                "pose_unit": "m",
            },
        ],
    )
    tree.blocks["b"].bound_kind = "structure"
    tree.blocks["b"].bound = "bnd-s"
    persist.save_tree(store, ref_id=int(design.id), tree=tree, card_text="x")

    doc = persist._read_ws_trace(store, int(design.id), "op")
    assert doc is not None
    ops = [
        f
        for f in doc["frames"]
        if f.get("kind") == "op" and f.get("qual") == "set_pose"
    ]
    assert len(ops) == 1
    bref2 = store.get_ref(kind="structure", id="bnd-s")
    assert bref2.meta["frames"]["b"] == ops[0]["id"]
    assert bref2.meta["generated"]["trace"] == {
        "run_job": int(design.id),
        "root": "op",
    }
    assert tree.op_frames == []  # consumed by the flush


def test_array_block_axis_bytes_match_main() -> None:
    """Off-axis specs store the same floats ``_unit_vec`` produced on
    main — the rewire normalises once with identical math, so existing
    designs' SHAs don't drift on resave (2a.2 review)."""
    from precis.blocktree.ops import _unit_vec
    from precis_se.ops import SeTree, apply_ops

    for axis in ([0.0, 0.0, 1.0], [1.0, 1.0, 0.0], [-3.0, -3.0, -1.0]):
        for kind, raw in (
            ("linear", {"count": 3, "pitch": 1.2e-9, "axis": axis}),
            ("polar", {"count": 4, "radius": 2e-9, "axis": axis}),
        ):
            tree = SeTree()
            apply_ops(
                tree,
                [
                    {"op": "add_block", "name": "t"},
                    {
                        "op": "array_block",
                        "name": "arr",
                        "template": "t",
                        kind: raw,
                    },
                ],
            )
            spec = tree.blocks["arr"].array
            assert spec is not None
            assert spec["axis"] == _unit_vec(axis, what="x")
            if axis == [1.0, 1.0, 0.0]:
                assert spec["axis"] == [
                    0.7071067811865475,
                    0.7071067811865475,
                    0.0,
                ]
            assert spec == pga.array_spec(
                {
                    "kind": kind,
                    **{k: v for k, v in raw.items() if k != "axis"},
                    "axis": axis,
                }
            )


def _ws_chunk(store: Any, ref_id: int, root: str) -> None:
    """A minimal ``ws_trace`` chunk carrying one root frame — stands in
    for a containerless import's run trace."""
    from psycopg.types.json import Jsonb

    from precis_geom import trace_schema as _ts

    doc = {
        "schema": _ts.SCHEMA_VERSION,
        "run": {},
        "frames": [
            {
                "id": 0,
                "key": root,
                "parent": None,
                "qual": "main",
                "file": "x.py",
                "line": 0,
                "occ": 0,
                "kind": "root",
            },
        ],
    }
    with store.pool.connection() as c:
        row = c.execute(
            "SELECT COALESCE(MAX(ord), -1) + 1 FROM chunks WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
        c.execute(
            "INSERT INTO chunks (ref_id, ord, text, meta, chunk_kind) "
            "VALUES (%s, %s, %s, %s, 'ws_trace')",
            (ref_id, int(row[0]), _ts.dumps(doc), Jsonb({"chunk_kind": "ws_trace"})),
        )


def test_op_save_shadows_containerless_import_trace(store: Any) -> None:
    """The op-trace chunk (newest ord) must not hide a containerless
    import's run trace on the same ref — ``_read_ws_trace`` scans every
    ws_trace chunk for the matching root (2a.2 review repro)."""
    from precis_se import persist
    from precis_se.ops import SeTree, apply_ops

    design = store.insert_ref(kind="se", slug="d-shadow", title="x", meta={})
    rid = int(design.id)
    _ws_chunk(store, rid, "runroot1")
    assert persist._read_ws_trace(store, rid, "runroot1") is not None
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "t"},
            {"op": "instance_block", "name": "i", "template": "t"},
        ],
    )
    persist.save_tree(store, ref_id=rid, tree=tree, card_text="x")
    assert persist._read_ws_trace(store, rid, "op") is not None
    # the workspace run's trace on the same home must still resolve
    assert persist._read_ws_trace(store, rid, "runroot1") is not None


def test_two_saves_append(store: Any) -> None:
    """Op frames accumulate into ONE ws_trace chunk across saves, with
    unique keys even outside a session (line=0)."""
    from precis_se import persist
    from precis_se.ops import SeTree, apply_ops

    design = store.insert_ref(kind="se", slug="d-two", title="x", meta={})
    rid = int(design.id)
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "t"},
            {"op": "instance_block", "name": "i", "template": "t"},
        ],
    )
    persist.save_tree(store, ref_id=rid, tree=tree, card_text="x")
    apply_ops(
        tree,
        [
            {
                "op": "set_pose",
                "block": "i",
                "pose": [0.01, 0, 0],
                "pose_unit": "m",
            },
            {
                "op": "instance_block",
                "name": "i2",
                "template": "t",
            },
        ],
    )
    persist.save_tree(store, ref_id=rid, tree=tree, card_text="x")
    doc = persist._read_ws_trace(store, rid, "op")
    assert doc is not None
    quals = [f["qual"] for f in doc["frames"]]
    assert quals == ["ops", "instance_block", "set_pose", "instance_block"]
    keys = [f["key"] for f in doc["frames"]]
    assert len(set(keys)) == len(keys)
    with store.pool.connection() as c:
        n = c.execute(
            "SELECT count(*) FROM chunks WHERE ref_id=%s AND chunk_kind='ws_trace'",
            (rid,),
        ).fetchone()[0]
    assert n == 1


def test_set_pose_on_instance_keeps_template_provenance(store: Any) -> None:
    """set_pose on an instance must not rewrite the template's bound
    provenance — moving i1 doesn't build t (2a.2 review)."""
    from precis_se import persist
    from precis_se.ops import SeTree, apply_ops

    store.insert_ref(
        kind="structure",
        slug="tpl-s",
        title="x",
        meta={
            "generated": {"trace": {"run_job": None, "root": "run1"}},
            "frames": {"t": 3},
        },
    )
    design = store.insert_ref(kind="se", slug="d-inst", title="x", meta={})
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "t"},
            {"op": "instance_block", "name": "i1", "template": "t"},
            {
                "op": "set_pose",
                "block": "i1",
                "pose": [0.01, 0, 0],
                "pose_unit": "m",
            },
        ],
    )
    tree.blocks["t"].bound_kind = "structure"
    tree.blocks["t"].bound = "tpl-s"
    persist.save_tree(store, ref_id=int(design.id), tree=tree, card_text="x")
    bref2 = store.get_ref(kind="structure", id="tpl-s")
    assert bref2.meta["frames"] == {"t": 3}
    assert bref2.meta["generated"]["trace"] == {"run_job": None, "root": "run1"}


def test_two_sessions_same_seq_unique_keys(store: Any, monkeypatch: Any) -> None:
    """Two sessions minting call seq 5 still get unique frame keys —
    the sid is what separates them, not the occ bump (2a.2 review)."""
    from precis import tool_ledger
    from precis_se import persist
    from precis_se.ops import SeTree, apply_ops

    design = store.insert_ref(kind="se", slug="d-2s", title="x", meta={})
    rid = int(design.id)
    tree = SeTree()
    apply_ops(tree, [{"op": "add_block", "name": "t"}])
    for sid in ("sessA", "sessB"):
        monkeypatch.setattr(tool_ledger, "current_session_key", lambda s=sid: (s, 5))
        apply_ops(
            tree,
            [{"op": "instance_block", "name": f"i-{sid}", "template": "t"}],
        )
        persist.save_tree(store, ref_id=rid, tree=tree, card_text="x")
    doc = persist._read_ws_trace(store, rid, "op")
    assert doc is not None
    keys = [f["key"] for f in doc["frames"]]
    assert len(set(keys)) == len(keys) == 3
    assert all(f["occ"] == 0 for f in doc["frames"])


def test_op_frame_line_is_the_ledger_seq(store: Any, monkeypatch: Any) -> None:
    """``line`` (``call N``) names the seq the tool ledger records for
    the call — the peek reads it without consuming, so the ledger
    write takes the same number (2a.2 review)."""
    from precis import serve_ledger, tool_ledger
    from precis_se import persist
    from precis_se.ops import SeTree, apply_ops

    fake_session = object()
    monkeypatch.setattr(serve_ledger, "current_session", lambda: fake_session)
    design = store.insert_ref(kind="se", slug="d-seq", title="x", meta={})
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "t"},
            {"op": "instance_block", "name": "i", "template": "t"},
        ],
    )
    persist.save_tree(store, ref_id=int(design.id), tree=tree, card_text="x")
    # what the ledger's own record for this call consumes — same seq.
    _sid, seq = tool_ledger._mint_session_key()
    assert seq is not None
    doc = persist._read_ws_trace(store, int(design.id), "op")
    assert doc is not None
    op_frames = [f for f in doc["frames"] if f["qual"] == "instance_block"]
    assert op_frames and all(f["line"] == seq for f in op_frames)
