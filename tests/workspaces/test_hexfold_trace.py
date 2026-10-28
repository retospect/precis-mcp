"""hexfold builder trace frames + per-atom ``maker`` column (gr514054).

A traced ``prism_tube``/``build``/``stick`` mints ``kind="core"`` frames
the way the CAD/pcb builders already do: the wrapper frame names the
user's call site, the ``.hx`` internals mint ``build.<kind>`` /
``build.<verb>`` frames for each minted patch and connect (refused
connects get findings, not frames), and every atom carries the id of the
frame that minted it — which ``pga.geom`` forwards into the scene so
``to_extxyz`` emits the ``maker:I:1`` column.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from precis_geom import assembly as pga
from precis_geom import extxyz, trace

# The test module poses as ``/work`` user code for the callsite fields.
_WORK_ROOT = Path(__file__).resolve().parent.parent

_HX_SPEC = (
    "hexfold 0.2\norigin t\n"
    "t: tube(5,0, len=3)\n"
    "u: tube(5,0, len=3)\n"
    "v: tube(5,0, len=3)\n"
    "w: tube(5,0, len=3)\n"
    "t.out --fuse k=0--> u.in\n"
    "seam foot: t.in == v.in == w.in\n"
    "terminate: u.out = H\n"
)


def _by_id(doc: dict) -> dict[int, dict]:
    return {f["id"]: f for f in doc["frames"]}


def test_prism_tube_frames_parent_children_and_stamp_makers() -> None:
    from hexfold.prism import prism_tube

    with trace.run(work_root=_WORK_ROOT) as tr:
        call_line = sys._getframe().f_lineno + 1
        net, _pt = prism_tube(4, 4, 2, "++++++")
        doc = tr.dump()
    frames = _by_id(doc)

    prism = next(f for f in doc["frames"] if f["qual"] == "prism.prism_tube")
    assert prism["kind"] == "core"
    # The wrapper frame names the user's call site, not library internals.
    assert (prism["file"], prism["line"]) == (
        "workspaces/test_hexfold_trace.py",
        call_line,
    )
    children = [f for f in doc["frames"] if f["parent"] == prism["key"]]
    child_quals = {f["qual"] for f in children}
    assert {"sheetnet.strip", "sheetnet.junction"} <= child_quals
    assert all(f["kind"] == "core" for f in children)

    # Every atom's maker resolves to the builder frame that minted it:
    # block-part atoms to a strip call, seam-column atoms to a junction.
    makers = {a.maker for a in net.atoms}
    assert None not in makers
    assert {frames[m]["qual"] for m in makers} == {
        "sheetnet.strip",
        "sheetnet.junction",
    }


def test_build_mints_instance_and_connect_frames() -> None:
    from hexfold.build import build

    with trace.run(work_root=_WORK_ROOT) as tr:
        net = build(_HX_SPEC)
        doc = tr.dump()
    frames = _by_id(doc)

    build_frame = next(f for f in doc["frames"] if f["qual"] == "build.build")
    assert build_frame["kind"] == "core"

    tubes = [f for f in doc["frames"] if f["qual"] == "build.tube"]
    assert len(tubes) == 4
    assert all(f["parent"] == build_frame["key"] for f in tubes)
    assert all(f["made"] == {"atoms": 60} for f in tubes)
    assert all(f["args"]["instance"] == n for f, n in zip(tubes, "tuvw"))

    fuse = next(f for f in doc["frames"] if f["qual"] == "build.fuse")
    assert fuse["made"] == {"bonds": 5}
    seam = next(f for f in doc["frames"] if f["qual"] == "build.seam")
    assert seam["made"] == {"atoms": 5, "bonds": 15}
    term = next(f for f in doc["frames"] if f["qual"] == "build.terminate")
    assert term["made"] == {"atoms": 5}

    # Patch atoms cite their build.<kind> frame, seam atoms the
    # build.seam frame, termination atoms build.terminate.
    for a in net.atoms:
        qual = frames[a.maker]["qual"] if a.maker is not None else None
        if a.instance == "foot":
            assert qual == "build.seam"
        elif a.hyb == "s":
            assert qual == "build.terminate"
        else:
            assert qual == "build.tube"


def test_refused_connect_mints_no_frame() -> None:
    """Frame iff made: an unresolved fuse reports a finding, no frame."""
    from hexfold.build import build
    from hexfold.report import BuildError

    spec = (
        "hexfold 0.2\norigin t\n"
        "t: tube(5,0, len=3)\n"
        "u: tube(5,0, len=3)\n"
        "t.out --fuse k=0--> u.nope\n"
    )
    with trace.run(work_root=_WORK_ROOT) as tr:
        with pytest.raises(BuildError, match="port.unknown"):
            build(spec)
        doc = tr.dump()
    assert not any(f["qual"] == "build.fuse" for f in doc["frames"])


def test_geom_and_extxyz_carry_the_maker_column() -> None:
    from hexfold.build import build
    from hexfold.stick import stick

    spec = "hexfold 0.2\norigin t\nt: tube(5,0, len=3)\n"
    with trace.run(work_root=_WORK_ROOT) as tr:
        net = build(spec)
        scene = pga.geom(net, coords=stick(net)).flatten()
        doc = tr.dump()
    frames = _by_id(doc)

    assert any(f["qual"] == "stick.stick" for f in doc["frames"])
    makers = {a.maker for a in scene.atoms.values()}
    tube = next(f for f in doc["frames"] if f["qual"] == "build.tube")
    assert makers == {tube["id"]}
    # The whole maker column joins to live frames.
    for a in scene.atoms.values():
        assert frames[a.maker]["qual"] == "build.tube"

    header = extxyz.to_extxyz(scene).split("\n")[1]
    assert "maker:I:1" in header


def test_untraced_build_leaves_maker_none() -> None:
    """No tracer → no stamps: standalone hexfold stays byte-identical."""
    from hexfold.build import build

    net = build("hexfold 0.2\norigin t\nt: tube(5,0, len=3)\n")
    assert all(a.maker is None for a in net.atoms)
