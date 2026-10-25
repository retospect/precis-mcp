"""Structure-workspaces slice 2r.2 acceptance — observation rays, macro leg.

The bracket fixture (``data/bracket``, numbers only; ``make_fixture.py``
regenerates it): a 6.2 mm stack (6 mm bracket + 0.2 mm gap) over four
tapped M5 holes 8, 8, 10 (through) and 6 mm deep.

- ``bolt-a1``: clearance hole with ⌀ and creator frame, gap, the threaded
  hole, and no ISO 4762 length fits (12 short by 1.7, 16 bottoms out by
  1.8) — said plainly; ``bolt-a3`` fits M5 × 16, engagement 9.8;
  ``bolt-a4``'s 6 mm hole fails 1.5 d;
- holes are not material: a tool down a hole is clear; a bundle whose
  off-centre rays reach the hole wall is blocked;
- a cad-bound block is probed through its bound cad design, an
  envelope-only block through its envelope;
- port cylinders are intersected in closed form (enter 1.2 / leave 17.2);
- moving ``base.h2`` 1 mm and re-importing is ``ray.changed`` on
  ``bolt-a2`` only (the hole's axis offset), with both lists; imports keep
  the digest ring and ``against='last'`` reads the previous import;
- radial clearance takes the tightest hole; the threaded hole is the
  threaded block's own;
- ``pga.find_holes`` reads ISO metric threads and ISO 273 clearances;
- walls are solid stretches (``min_wall`` flags a thin one); a block
  re-entered along the axis is an undercut.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.workspace_import import import_workspace_run

DATA = Path(__file__).resolve().parent / "data" / "bracket"
FIXTURE = DATA / "out"


def _mount(store: Store, slug: str = "mount", out: Path = FIXTURE) -> SeHandler:
    if store.get_ref(kind="se", id=slug) is None:
        store.insert_ref(kind="se", slug=slug, title="mount", meta={})
    import_workspace_run(store, workspace="mount", design=slug, out_dir=out)
    return SeHandler(hub=Hub(store=store))


def _ray(se: SeHandler, slug: str, **args: Any) -> str:
    return se.get(id=slug, view="ray", args=args).body


def _lines(body: str) -> list[str]:
    return [ln.strip() for ln in body.splitlines()]


def _tree(store: Store, slug: str) -> Any:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(store, int(ref.id))


def test_bolt_axis_no_standard_length_fits(store: Store) -> None:
    se = _mount(store)
    ls = _lines(_ray(se, "mount", name="bolt-a1"))
    assert ls[0].startswith("ray bolt-a1 · block bracket · port:a1")
    hole = next(ln for ln in ls if ln.startswith("3.0 mm  void bracket.hole1"))
    assert "⌀5.5 mm · 6.0 mm" in hole
    assert "build.py:44 cut#5 · frame:19c5c89540c81454" in hole
    # a through hole the bolt runs down is no material: no enter/exit
    assert not any(
        ln.startswith(("3.0 mm  enter bracket", "9.0 mm  exit")) for ln in ls
    )
    assert "9.0 mm  gap 0.2 mm" in ls
    assert any(ln.startswith("9.2 mm  void base.h1 ⌀5.0 mm · 8.0 mm") for ln in ls)
    bolt = next(ln for ln in ls if ln.startswith("bolt:"))
    assert "stack 6.2 mm · thread depth 8.0 mm (≥ 7.5 mm = 1.5 d ✓)" in bolt
    assert "radial clearance 0.25 mm" in bolt and "offset" not in bolt
    assert (
        "screw: no standard M5 length fits — 12 short by 1.7 mm, 16 bottoms out "
        "by 1.8 mm ✗"
    ) in ls
    s = [float(ln.split()[0]) for ln in ls[2:] if re.match(r"^[\d.]+ mm  ", ln)]
    assert s == sorted(s)


def test_bolt_axis_fit_and_short_hole(store: Store) -> None:
    se = _mount(store)
    a3 = _lines(_ray(se, "mount", name="bolt-a3"))
    assert (
        "screw: M5 × 16 · engagement 9.8 mm (stack 6.2 + 1.5 d ≤ L ≤ stack + "
        "depth 10.0)"
    ) in a3
    a4 = next(
        ln for ln in _lines(_ray(se, "mount", name="bolt-a4")) if ln.startswith("bolt:")
    )
    # 6 mm of thread: short of 1.5 d (7.5 mm), though it would pass at 1 d
    assert "thread depth 6.0 mm (≥ 7.5 mm = 1.5 d ✗)" in a4


def test_pick_screw_is_the_shortest_that_fits() -> None:
    from precis_se.rays_macro import pick_screw_mm, screw_lengths_mm

    m5 = screw_lengths_mm(5.0)
    # 16, 20 and 25 all fit a 20 mm hole under 6.2 mm: the shortest wins
    assert pick_screw_mm(m5, 6.2, 20.0, 7.5) == (16.0, "")
    assert pick_screw_mm(m5, 6.2, 8.0, 7.5) == (
        None,
        "12 short by 1.7 mm, 16 bottoms out by 1.8 mm",
    )


def test_port_cylinder_closed_form(store: Store) -> None:
    ls = _lines(_ray(_mount(store), "mount", name="bolt-a1"))
    assert any(ln.startswith("1.2 mm  port.enter base.h1 M5x0.8") for ln in ls)
    assert "17.2 mm  port.leave base.h1" in ls


def test_cad_bound_vs_envelope(store: Store) -> None:
    """The same block through its bound cad design sees the hole; with
    the binding cleared and only an envelope it is solid."""
    se = _mount(store)
    assert "void bracket.hole1" in _ray(se, "mount", name="bolt-a1")
    se.edit(
        id="mount",
        ops=[
            {"op": "set_binding", "block": "bracket", "clear": True},
            {
                "op": "set_envelope",
                "block": "bracket",
                "envelope": "box:w0.04d0.04h0.0122",
            },
        ],
    )
    body = _ray(se, "mount", name="bolt-a1")
    assert "void bracket" not in body and "enter bracket" in body


def _down_hole(se: SeHandler, radius: float) -> str:
    return _ray(
        se,
        "mount",
        block="bracket",
        frame="port:a1",
        origin=[0, 0, -0.003],
        direction=[0, 0, 1],
        length=0.0055,
        kind="tool_access",
        bundle={"radius": radius, "count": 8},
    )


def test_tool_access_holes_are_not_material(store: Store) -> None:
    se = _mount(store)
    body = _ray(se, "mount", name="tool-a1")
    head = body.splitlines()[0]
    assert "bundle 8×r 4 mm" in head and head.endswith("· 0 hits")
    assert "tool access: clear ✓" in body
    # a 1 mm key down the ⌀5.5 hole: every ray stays in the hole — clear
    assert "tool access: clear ✓" in _down_hole(se, 0.001)
    # a 4 mm one: the centre ray is in the hole, the off-centre rays hit
    # the bracket around it — the bundle is blocked
    assert "tool access: blocked by bracket at 3.0 mm ✗" in _down_hole(se, 0.004)
    # the bundle ceiling: 64 rays per probe
    with pytest.raises(BadInput, match="count ≤ 64"):
        _ray(
            se,
            "mount",
            block="bracket",
            origin=[0, 0, 0],
            direction=[0, 0, 1],
            length=0.01,
            bundle={"radius": 0.001, "count": 65},
        )


def _regen(out: Path, dx_mm: float) -> None:
    subprocess.run(
        [
            sys.executable,
            str(DATA / "make_fixture.py"),
            str(out),
            "--h2-dx",
            str(dx_mm),
        ],
        check=True,
    )


def test_moving_base_h2_is_ray_changed_on_bolt_a2(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "out"
    shutil.copytree(FIXTURE, out)
    se = _mount(store, "mount-mv", out)
    _regen(out, 1.0)
    res = import_workspace_run(store, workspace="mount", design="mount-mv", out_dir=out)
    changed = [f for f in res.findings if f.startswith("ray.changed:")]
    assert len(changed) == 1 and changed[0].startswith(
        "ray.changed: bolt-a2 on bracket"
    )
    text = "\n".join(res.findings)
    before = text.split("before:")[1].split("after:")[0]
    after = text.split("after:")[1]
    assert "axis offset" not in before.split("void base.h2")[1].split("\n")[0]
    assert "void base.h2 ⌀5.0 mm · 8.0 mm · axis offset 1.0 mm" in after
    assert "axis offset 1.0 mm from base.h2 ✗" in after
    rays = _tree(store, "mount-mv").blocks["bracket"].rays
    assert rays["bolt-a2"].digests[-1].get("changed")
    assert not rays["bolt-a1"].digests[-1].get("changed")
    findings = se.get(id="mount-mv", view="validate").body
    assert "bracket.bolt-a2" in findings and "bracket.bolt-a1" not in findings
    # observation only: the port the manifest moved is NOT synced on
    # re-import (that is import sync, a follow-up) — the hole moved, so
    # the ray read it through the cad geometry
    assert _tree(store, "mount-mv").blocks["base"].ports["h2"].pose[0] == pytest.approx(
        -0.005
    )
    # an unchanged third import: validate reads the NEWEST digest, clean
    import_workspace_run(store, workspace="mount", design="mount-mv", out_dir=out)
    assert "ray.changed" not in se.get(id="mount-mv", view="validate").body


def test_imports_keep_the_digest_ring(store: Store) -> None:
    for i in range(3):
        job = store.insert_ref(kind="job", slug=None, title=f"run {i}", meta={})
        store.insert_ref(
            kind="se", slug="mount-ring", title="m", meta={}
        ) if i == 0 else None
        import_workspace_run(
            store,
            workspace="mount",
            design="mount-ring",
            out_dir=FIXTURE,
            run_job=int(job.id),
        )
        if i == 1:
            second = int(job.id)
    dg = _tree(store, "mount-ring").blocks["bracket"].rays["bolt-a1"].digests
    assert len(dg) == 3
    assert [d["service"]["import"] for d in dg][1] == second
    se = SeHandler(hub=Hub(store=store))
    body = _ray(se, "mount-ring", name="bolt-a1", against="last")
    # 'last' is the previous import, not the one that just wrote "now"
    assert f"import {second}" in body.splitlines()[1]
    assert "0 moved · 0 added · 0 removed" in body


def test_port_annotations_are_validated(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "out"
    shutil.copytree(FIXTURE, out)
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    man["blocks"][0]["ports"][0]["annotations"] = {"thread": "M5x0.8", "depth": "8 mm"}
    (out / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    store.insert_ref(kind="se", slug="mount-bad", title="m", meta={})
    with pytest.raises(BadInput) as exc:
        import_workspace_run(store, workspace="mount", design="mount-bad", out_dir=out)
    assert "'h1'" in str(exc.value) and "annotations.depth" in str(exc.value)
    assert exc.value.next is not None
    man["blocks"][0]["ports"][0]["annotations"] = ["M5"]
    (out / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    with pytest.raises(BadInput, match="must be an object"):
        import_workspace_run(store, workspace="mount", design="mount-bad", out_dir=out)


def _cad_block(store: Store, slug: str, src: str) -> None:
    from precis.cad.scene import parse_source

    store.cad_save(slug=slug, title=slug, spec=parse_source(src), card_text=slug)


def _stack_design(store: Store, slug: str, *, base_hole: bool) -> SeHandler:
    """top (⌀5.5) / spacer (⌀6.4) / base (M5, 8 mm — or solid) / under
    (a ⌀5 through hole on the same axis), cad-bound, stacked on z."""
    _cad_block(
        store,
        f"{slug}-top",
        "component p\np add box:w20mmd20mmh6mm @0mm,0mm,20mm\nh cut cyl:r2.75mmh8mm @0mm,0mm,19mm\n",
    )
    _cad_block(
        store,
        f"{slug}-sp",
        "component p\np add box:w20mmd20mmh6mm @0mm,0mm,14mm\nh cut cyl:r3.2mmh8mm @0mm,0mm,13mm\n",
    )
    base = "component p\np add box:w20mmd20mmh10mm @0mm,0mm,4mm\n"
    if base_hole:
        base += "h cut cyl:r2.5mmh9mm @0mm,0mm,6mm\n"
    _cad_block(store, f"{slug}-base", base)
    _cad_block(
        store,
        f"{slug}-under",
        "component p\np add box:w20mmd20mmh4mm\nh cut cyl:r2.5mmh6mm @0mm,0mm,-1mm\n",
    )
    ops: list[dict[str, Any]] = []
    for blk in ("top", "sp", "base", "under"):
        ops += [
            {"op": "add_block", "name": blk, "envelope": "box:w0.02d0.02h0.03"},
            {
                "op": "set_binding",
                "block": blk,
                "kind": "cad",
                "design": f"{slug}-{blk}",
            },
        ]
    ops.append(
        {
            "op": "add_port",
            "block": "base",
            "name": "h",
            "roles": ["fastener"],
            "direction": [0, 0, -1],
            "pose": "[0 0 14] mm",
            "annotations": {"thread": "M5x0.8", "depth": 0.008},
        }
    )
    se = SeHandler(hub=Hub(store=store))
    se.put(id=slug, text=json.dumps({"ops": ops}))
    return se


_DOWN = {
    "block": "top",
    "frame": "world",
    "origin": [0, 0, 0.029],
    "direction": [0, 0, -1],
    "length": 0.035,
    "kind": "bolt_axis",
}


def test_radial_clearance_is_the_tightest_hole(store: Store) -> None:
    se = _stack_design(store, "stack2r", base_hole=True)
    bolt = next(
        ln for ln in _lines(_ray(se, "stack2r", **_DOWN)) if ln.startswith("bolt:")
    )
    assert "stack 12.0 mm" in bolt
    assert "radial clearance 0.25 mm" in bolt  # ⌀5.5, not the spacer's ⌀6.4 (0.70)


def test_threaded_hole_is_the_threaded_blocks_own(store: Store) -> None:
    """A solid base under its threaded port: no thread there, even though
    the block below has a hole on the same axis."""
    se = _stack_design(store, "stack2r-solid", base_hole=False)
    ls = _lines(_ray(se, "stack2r-solid", **_DOWN))
    assert any(
        ln.startswith(("void under.h",)) or " void under.h " in f" {ln}" for ln in ls
    )
    bolt = next(ln for ln in ls if ln.startswith("bolt:"))
    assert "thread depth 0.0 mm (≥ 7.5 mm = 1.5 d ✗)" in bolt


def test_find_holes_threads_and_iso273_clearances() -> None:
    from precis_geom import assembly as pga

    src = (
        "component plate\n"
        "plate add box:w90mmd20mmh8mm\n"
        "h1 cut cyl:r2.5mmh10mm @-30mm,0mm,-1mm\n"
        "h2 cut cyl:r2.5mmh10mm @-20mm,0mm,-1mm\n"
        "h3 cut cyl:r2.5mmh10mm @-10mm,0mm,-1mm\n"
        "c5 cut cyl:r2.9mmh10mm @0mm,0mm,-1mm\n"
        "c6 cut cyl:r3.2mmh10mm @10mm,0mm,-1mm\n"
        "tap cut cyl:r2.1mmh10mm @20mm,0mm,-1mm\n"
        "gap cut cyl:r3.1mmh10mm @30mm,0mm,-1mm\n"
        "big cut cyl:r20mmh10mm @0mm,-30mm,-1mm\n"
    )
    ports = pga.find_holes(src)
    by_feature = {p.annotations["feature"]: p for p in ports}
    assert {
        f: p.annotations.get("thread")
        for f, p in by_feature.items()
        if f.startswith("h")
    } == {
        "h1": "M5x0.8",
        "h2": "M5x0.8",
        "h3": "M5x0.8",
    }
    assert by_feature["c5"].annotations["clearance_for"] == "M5"  # ⌀5.8 coarse
    assert by_feature["c6"].annotations["clearance_for"] == "M6"  # ⌀6.4 fine
    # the ⌀4.2 tap drill and ⌀6.2 (between M5 coarse and M6 fine) are no
    # clearance hole; the 40 mm bore no fastener
    assert {"tap", "gap", "big"}.isdisjoint(by_feature)
    threaded = [by_feature[f] for f in ("h1", "h2", "h3")]
    pos = [p.pos for p in threaded if p.pos is not None]
    axes = [p.direction for p in threaded if p.direction is not None]
    assert [round(x, 6) for x, _y, _z in pos] == [-0.03, -0.02, -0.01]
    assert len(axes) == 3 and all(abs(abs(z) - 1.0) < 1e-12 for _x, _y, z in axes)
    assert all(p.roles == ("fastener",) for p in ports)
    assert [p.name for p in ports] == ["h1", "h2", "h3", "h4", "h5"]


def test_bundle_round_trips_the_manifest(tmp_path: Path) -> None:
    from precis_geom import assembly as pga
    from precis_geom import out as geom_out
    from precis_geom import trace as _trace
    from precis_se.workspace_import import _vet_manifest

    run = geom_out.Run(tmp_path)
    with _trace.run():
        run.block("b", "component b\nb add box:w10mmd10mmh10mm\n", scale="macro")
        r = pga.ray("t", [0, 0, 0.02], [0, 0, 1], "30 mm", block="b")
        run.ray(pga.ray.bundle(r, "4 mm", 8))
    run.finish()
    doc = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    row = doc["rays"][0]
    assert row["bundle"] == {"radius": pytest.approx(0.004), "count": 8}
    assert row["kind"] == "tool_access"
    assert _vet_manifest(doc)["rays"][0]["bundle"]["count"] == 8


def test_unset_bundle_is_absent_from_snapshots(store: Store) -> None:
    """2r.1-shaped rays keep their tree_to_json bytes: no ``bundle: null``."""
    _mount(store)
    doc = persist.tree_to_json(_tree(store, "mount"))
    rays = next(b for b in doc["blocks"] if b["name"] == "bracket")["rays"]
    assert "bundle" not in rays["bolt-a1"] and "bundle" in rays["tool-a1"]
    tree = _tree(store, "mount")
    assert "bundle" not in tree.blocks["bracket"].rays["bolt-a1"].definition()


def _two_box_design(
    store: Store, *, thin_mm: float = 2.0, mode: str | None = None
) -> SeHandler:
    """A cad-bound block whose one component is two boxes 4 mm apart —
    a tool axis through both re-enters the block."""
    _cad_block(
        store,
        "twobox",
        "component part\n"
        "a add box:w10mmd10mmh2mm\n"
        f"b add box:w10mmd10mmh{thin_mm:g}mm @0mm,0mm,6mm\n",
    )
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "u", "envelope": "box:w0.01d0.01h0.01"},
        {"op": "set_binding", "block": "u", "kind": "cad", "design": "twobox"},
    ]
    if mode is not None:
        ops.append({"op": "set_mode", "block": "u", "mode": mode})
    se = SeHandler(hub=Hub(store=store))
    se.put(id="twobox-se", text=json.dumps({"ops": ops}))
    return se


_UP = {"block": "u", "origin": [0, 0, -0.005], "direction": [0, 0, 1], "length": 0.02}


def test_manufacturability_undercut_and_load_path(store: Store) -> None:
    se = _two_box_design(store)
    body = _ray(se, "twobox-se", kind="manufacturability", **_UP)
    assert "enter u#2" in body
    assert "undercut: u re-entered along the tool axis" in body
    walls = [ln for ln in _lines(body) if ln.startswith("wall ")]
    assert walls == ["wall u: 2.0 mm", "wall u#2: 2.0 mm"]
    lp = _ray(se, "twobox-se", kind="load_path", **_UP)
    assert "load path: thinnest crossing 2.0 mm in u" in lp
    assert "no stored field outcome" in lp
    side = dict(_UP, origin=[0.02, 0, -0.005])
    assert "undercut: none along this axis" in _ray(
        se, "twobox-se", kind="manufacturability", **side
    )


def test_thin_wall_is_flagged_and_holes_are_not_walls(store: Store) -> None:
    se = _two_box_design(store, thin_mm=0.4, mode="fdm/asa")
    walls = [
        ln
        for ln in _lines(_ray(se, "twobox-se", kind="manufacturability", **_UP))
        if ln.startswith("wall ")
    ]
    assert walls[0].startswith("wall u: 2.0 mm (min_wall") and walls[0].endswith("✓)")
    assert walls[1].startswith("wall u#2: 0.4 mm (min_wall") and walls[1].endswith("✗)")
    # down the bracket's clearance hole: the hole is no 0 mm wall
    mount = _mount(store)
    down = _ray(
        mount,
        "mount",
        block="bracket",
        frame="port:a1",
        origin=[0, 0, -0.003],
        direction=[0, 0, 1],
        length=0.028,
        kind="manufacturability",
    )
    assert "wall bracket" not in down
    assert "wall base: 2.0 mm" in down  # the base under the 8 mm blind hole
