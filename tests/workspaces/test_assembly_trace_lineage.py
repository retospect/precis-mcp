"""Assembly rewrites retain creator/placement stamps (gr521586)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from precis.cad.vec import Transform
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Bond, Scene
from precis_geom import assembly as pga
from precis_geom import trace
from precis_se.traceview import Trace


@trace.traced(name="source_atom")
def _atom(label: str, x: float) -> Atom:
    trace.record(made={"atoms": 1})
    return Atom(
        label=label, element="C", frac=np.array([x / 20, 0, 0]), maker=trace.leaf_id()
    )


@trace.traced(name="source_bond")
def _bond() -> Bond:
    trace.record(made={"bonds": 1})
    return Bond(i="aC1", j="aC2", maker=trace.leaf_id())


def _source() -> pga.Geom:
    atoms = [_atom("aC1", 0), _atom("aC2", 1)]
    return pga.geom(
        Scene(
            cell=Cell(np.eye(3) * 20, (False, False, False)),
            atoms={a.label: a for a in atoms},
            bonds=[_bond()],
        )
    )


@pytest.mark.parametrize("operation", ["translate", "rotate", "mirror"])
@pytest.mark.parametrize("posed", [False, True])
def test_transform_preserves_creator_selection(operation: str, posed: bool) -> None:
    with trace.run() as tr:
        source = _source()
        if posed:
            source = replace(source, pose=Transform(np.eye(3), np.array([2.0, 0, 0])))
        before = source.flatten()
        if operation == "translate":
            result = pga.translate(source, [0, 3, 0])
        elif operation == "rotate":
            result = pga.rotate(source, [0, 0, 1], 90)
        else:
            result = pga.mirror(source)
        flat = result.flatten()
    index = Trace(tr.dump())
    creators = [f for f in index.frames if f["qual"] == "source_atom"]
    assert [a.maker for a in flat.atoms.values()] == [f["id"] for f in creators]
    # The actual subtree lookup used by frame: selection still reaches each atom.
    for creator in creators:
        assert (
            sum(
                a.maker in index.subtree_ids(creator["key"])
                for a in flat.atoms.values()
            )
            == 1
        )
    assert flat.bonds[0].maker == source.scene.bonds[0].maker
    bond_frame = index.maker_frame(flat.bonds[0].maker)
    assert bond_frame is not None and bond_frame["qual"] == "source_bond"
    operation_frame = next(
        f
        for f in index.frames
        if f["qual"] == ("mirror" if operation == "mirror" else "pose")
    )
    assert operation_frame["touched"]["atoms"] == 2
    assert operation_frame["made"] == {}
    before_carts = np.array(
        [before.cell.frac_to_cart(a.frac) for a in before.atoms.values()]
    )
    after_carts = np.array(
        [flat.cell.frac_to_cart(a.frac) for a in flat.atoms.values()]
    )
    assert not np.allclose(before_carts, after_carts)
    assert [a.maker for a in source.scene.atoms.values()] == [f["id"] for f in creators]


def test_fused_tile_keeps_representative_source_makers() -> None:
    with trace.run() as tr:
        source = _source()
        fused = pga.tile(source, 2, period=[1, 0, 0], fused=True).flatten()
    source_makers = [a.maker for a in source.scene.atoms.values()]
    # x=1 occurs in both copies; its earlier-copy source atom wins.
    assert [a.maker for a in fused.atoms.values()] == [*source_makers, source_makers[1]]
    assert [b.maker for b in fused.bonds] == [source.scene.bonds[0].maker] * 2
    index = Trace(tr.dump())
    for atom in fused.atoms.values():
        frame = index.maker_frame(atom.maker)
        assert frame is not None and frame["qual"] == "source_atom"
    tile_frame = next(f for f in index.frames if f["qual"] == "tile")
    assert tile_frame["made"] == {"atoms": 3, "bonds": 2}


def test_fuse_and_transform_retain_packed_copy_frames() -> None:
    with trace.run() as tr:
        packed = pga.tile(_source(), 2, pitch=[3, 0, 0])
        copy_ids = [fid for _child, _pose, fid in packed.placements]
        fused = pga.tile(packed, 2, period=[10, 0, 0], fused=True)
        flat = pga.translate(fused, [0, 4, 0]).flatten()
    assert [a.maker for a in flat.atoms.values()] == [copy_ids[0]] * 2 + [
        copy_ids[1]
    ] * 2 + [copy_ids[0]] * 2 + [copy_ids[1]] * 2
    index = Trace(tr.dump())
    for maker in copy_ids:
        frame = index.maker_frame(maker)
        assert frame is not None and frame["qual"] == "tile"
    assert [b.maker for b in flat.bonds] == [copy_ids[0], copy_ids[1]] * 2
