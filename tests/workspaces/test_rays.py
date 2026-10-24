"""Structure-workspaces slice 2r.1 acceptance — observation rays, atomic leg.

- a ray along the A2 sheet renders ≤ 600 tokens, its ``pass`` hits'
  ``s``/``d`` equal ``view='line'``'s along/offset, ``∠`` the POAV normal;
- a ray through a ``from_smiles`` benzene fires ``pi`` enter/leave once
  each with the ring's ``pi:`` selector;
- a ray across the ``join`` seam fires one ``frame`` hit naming the
  junction; ``relax`` is absent until 4a records per-atom levels;
- without stored charges ``charge`` is absent from the header;
- a mixed envelope + structure design concatenates macro and atomic hits
  in ``s`` order;
- a block-frame ray reports the same hits after ``set_pose``;
- ``observe_ray`` then a relaxed re-save: ``against='last'`` lists moved
  ``pass`` hits with ``Δd`` and folds the unchanged;
- ``out.ray`` imports as a block child with ``made-by:``;
- the A2 atom's eye lists the ray; ``~ray:<name>`` rungs contain each
  other; ``/eye/`` links the handle.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis.structure import probe as st_probe
from precis.utils.llm.admit import estimate_tokens
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.workspace_import import import_workspace_run

DATA = Path(__file__).resolve().parent / "data"
FIXTURE = DATA / "a2_eye" / "out"
SLUG = "hcol-a2r"


def _import_a2(store: Store, out_dir: Path = FIXTURE, slug: str = SLUG) -> SeHandler:
    if store.get_ref(kind="se", id=slug) is None:
        store.insert_ref(kind="se", slug=slug, title="a2 rays", meta={})
    job = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    import_workspace_run(
        store, workspace="hcol", design=slug, out_dir=out_dir, run_job=int(job.id)
    )
    return SeHandler(hub=Hub(store=store))


@pytest.fixture
def a2(store: Store) -> SeHandler:
    return _import_a2(store)


def _se_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return int(ref.id)


def _edit(se: SeHandler, slug: str, ops: list[dict[str, Any]]) -> str:
    return se.edit(id=slug, ops=ops).body


def _ray(se: SeHandler, slug: str, **args: Any) -> str:
    return se.get(id=slug, view="ray", args=args).body


#: The A2 row at y = 0 Å (aC4, aC14, aC15, aC25 sit on it), in metres.
AXIS = {
    "op": "add_ray",
    "block": "p",
    "name": "axis",
    "origin": [0.0, 0.0, 0.0],
    "direction": [1, 0, 0],
    "length": 12e-10,
    "reach": 1.5,
}


def test_axis_ray_pass_matches_line(a2: SeHandler, store: Store) -> None:
    """The "(4, 6) A2 axis" ray of the row's acceptance, on the a2_eye
    fixture: that fixture is a FLAT A2 sheet (z = 0), so the ray runs
    in-plane along its y = 0 atom row rather than down a tube axis."""
    _edit(a2, SLUG, [AXIS])
    body = _ray(a2, SLUG, name="axis")
    assert estimate_tokens(len(body)) <= 600, estimate_tokens(len(body))
    head = body.splitlines()[0]
    assert head.startswith("ray axis · block p") and "+x" in head
    passes = {
        m.group(2): (float(m.group(1)), float(m.group(3)), m.group(4))
        for m in re.finditer(
            r"^\s+([\d.]+) Å  pass p#(aC\d+)  d ([\d.]+)  ∠ (\S+)", body, re.M
        )
    }
    assert passes
    sh = StructureHandler(hub=Hub(store=store))
    line = sh.get(
        id=f"{SLUG}-p",
        view="line",
        args={"origin": [0, 0, 0], "direction": [1, 0, 0], "radius": 1.5},
    ).body
    rows = {
        m.group(1): (float(m.group(2)), float(m.group(3)))
        for m in re.finditer(r"(aC\d+)\tC\t(-?[\d.]+)\t([\d.]+)", line)
    }
    in_range = {k: v for k, v in rows.items() if 0 <= v[0] <= 12.0}
    assert set(passes) == set(in_range)
    for label, (s, d, _ang) in passes.items():
        assert abs(s - in_range[label][0]) < 0.06  # s renders at 0.1 Å
        assert abs(d - in_range[label][1]) < 0.006  # d renders at 0.01 Å
    # in-plane ray on a flat sheet: every 3-coordinate atom's POAV normal
    # is ±z, so the angle to the ray is 90°
    ref = store.get_ref(kind="structure", id=f"{SLUG}-p")
    assert ref is not None
    scene, _h = store.structure_load(ref.id)
    three = [la for la in passes if len(st_probe._adjacency(scene)[la]) == 3]
    assert three
    for la in three:
        assert passes[la][2] == "90°", (la, passes[la])


def _benzene_design(
    store: Store, slug: str
) -> tuple[SeHandler, np.ndarray, np.ndarray]:
    sh = StructureHandler(hub=Hub(store=store))
    sh.put(
        id=f"{slug}-bz",
        text=json.dumps(
            {
                "cell": {"a": 30, "b": 30, "c": 30, "pbc": [False, False, False]},
                "ops": [{"op": "from_smiles", "smiles": "c1ccccc1"}],
            }
        ),
    )
    se = SeHandler(hub=Hub(store=store))
    se.put(
        id=slug,
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "b", "envelope": "sphere:r2e-9"},
                    {"op": "bind_structure", "block": "b", "design": f"{slug}-bz"},
                ]
            }
        ),
    )
    ref = store.get_ref(kind="structure", id=f"{slug}-bz")
    assert ref is not None
    scene, _h = store.structure_load(ref.id)
    carts = np.asarray(
        [
            scene.cell.frac_to_cart(a.frac)
            for a in scene.atoms.values()
            if a.element == "C"
        ]
    )
    c = carts.mean(axis=0)
    n = np.linalg.svd(carts - c)[2][2]
    return se, c, n


def test_benzene_pi_enter_leave_once(store: Store) -> None:
    se, c, n = _benzene_design(store, "bz-ray")
    origin = (c - 4.0 * n) * 1e-10
    _edit(
        se,
        "bz-ray",
        [
            {
                "op": "add_ray",
                "block": "b",
                "name": "through",
                "origin": origin.tolist(),
                "direction": n.tolist(),
                "length": 8e-10,
            }
        ],
    )
    body = _ray(se, "bz-ray", name="through")
    enters = [ln for ln in body.splitlines() if "pi.enter" in ln]
    leaves = [ln for ln in body.splitlines() if "pi.leave" in ln]
    assert len(enters) == 1 and len(leaves) == 1, body
    assert "(6, aromatic) · pi:b#0" in enters[0]
    s_in = float(enters[0].split()[0])
    s_out = float(leaves[0].split()[0])
    # the cylinder is the ring plane ± 1.7 Å along the normal
    assert abs(s_in - (4.0 - 1.7)) < 0.05 and abs(s_out - (4.0 + 1.7)) < 0.05
    # charge has no stored data → absent from the header, other hits render
    head = body.splitlines()[0]
    fields = head.split("fields ")[1].split(" ·")[0].split(",")
    assert "charge" not in fields and "pass" in fields and "pi" in fields
    assert "q " not in body


def test_seam_ray_one_junction_frame_hit(a2: SeHandler) -> None:
    # along the y = −4.919 Å row: aC26 (strip), aC30 (junction), then u
    _edit(
        a2,
        SLUG,
        [
            {
                "op": "add_ray",
                "block": "p",
                "name": "seam",
                "origin": [3.0e-10, -4.919024e-10, 0.0],
                "direction": [1, 0, 0],
                "length": 10e-10,
                "reach": 0.8,
                "fields": ["pass", "frame", "relax"],
            }
        ],
    )
    body = _ray(a2, SLUG, name="seam")
    frames = [ln for ln in body.splitlines() if " frame " in f" {ln.split('Å', 1)[-1]}"]
    junction = [ln for ln in frames if "junction#" in ln]
    assert len(junction) == 1, body
    assert "frame:a200000000000008" in junction[0]
    assert any("fetch#" in ln for ln in frames)  # crossed into block u
    # the pass list crosses the seam: p's aC30 then u's atoms
    assert "pass p#aC30" in body and "pass u#aC1" in body
    # per-atom relax levels are 4a's — the field is absent, never faked
    head = body.splitlines()[0]
    assert "relax" not in head.split("fields ")[1].split(" ·")[0]
    assert not any(
        ln.split("Å", 1)[-1].strip().startswith("relax") for ln in body.splitlines()
    )


def test_relax_field_fires_on_level_change() -> None:
    """The 4a contract at the kernel: once a relax_of answers levels, one
    hit per change of the nearest atom's value."""
    from precis.structure.cell import Cell
    from precis.structure.scene import Atom, Scene

    scene = Scene(Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False)))
    for i in range(4):
        scene.atoms[f"aC{i + 1}"] = Atom(
            f"aC{i + 1}", "C", np.array([0.1 + 0.07 * i, 0.5, 0.5])
        )
    # alternating levels: a hit per change, coalesced to one per value
    level = {
        "aC1": "unit gfnff",
        "aC2": "interfaces xtb",
        "aC3": "unit gfnff",
        "aC4": "interfaces xtb",
    }
    o = scene.cell.frac_to_cart(np.array([0.0, 0.5, 0.5]))
    tr = st_probe.ray_hits(
        scene,
        o,
        np.array([1.0, 0, 0]),
        length=10.0,
        fields=["relax"],
        relax_of=level.get,
    )
    assert tr.fields == ["relax"]
    assert [h.subject for h in tr.hits] == ["unit gfnff", "interfaces xtb"]
    none = st_probe.ray_hits(
        scene,
        o,
        np.array([1.0, 0, 0]),
        length=10.0,
        fields=["relax"],
        relax_of=lambda _l: None,
    )
    assert none.fields == [] and none.hits == []


def test_charge_kernel_esp_and_q() -> None:
    """Stored charges (slice 4) feed q on passes and esp hits; without
    them the field is absent."""
    from precis.structure.cell import Cell
    from precis.structure.scene import Atom, Scene

    scene = Scene(Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False)))
    scene.atoms["aN1"] = Atom("aN1", "N", np.array([0.3, 0.5, 0.5]))
    scene.atoms["aO1"] = Atom("aO1", "O", np.array([0.6, 0.5, 0.5]))
    o = scene.cell.frac_to_cart(np.array([0.0, 0.52, 0.5]))
    with_q = st_probe.ray_hits(
        scene, o, np.array([1.0, 0, 0]), length=18.0, charges={"aN1": -0.4, "aO1": 0.4}
    )
    assert "charge" in with_q.fields
    assert all("q" in h.data for h in with_q.hits if h.field == "pass")
    esp = [h for h in with_q.hits if h.event == "esp"]
    assert any(h.data["tag"] == "sign" for h in esp)
    assert any(h.data["tag"] == "min" for h in esp)
    bare = st_probe.ray_hits(scene, o, np.array([1.0, 0, 0]), length=18.0)
    assert "charge" not in bare.fields


def test_mixed_design_macro_and_atomic_in_s_order(store: Store) -> None:
    se, c, n = _benzene_design(store, "bz-mix")
    # a 1 nm cube block 2 nm out along +x from the benzene, envelope-only.
    # Named "aslab" so block-name order ("aslab" < "b") is the REVERSE of
    # s order (benzene first): a concatenation sorted by block reads wrong.
    _edit(
        se,
        "bz-mix",
        [
            {"op": "add_block", "name": "aslab", "envelope": "box:w1e-9d1e-9h1e-9"},
            {
                "op": "set_pose",
                "block": "aslab",
                "pose": f"[{c[0] + 25.0} {c[1]} {c[2]}] Å",
            },
            {
                "op": "add_ray",
                "block": "b",
                "name": "x",
                "origin": [(c[0] - 3.0) * 1e-10, c[1] * 1e-10, c[2] * 1e-10],
                "direction": [1, 0, 0],
                "length": 4e-9,
                "fields": ["pass"],
            },
        ],
    )
    body = _ray(se, "bz-mix", name="x")
    lines = [
        ln
        for ln in body.splitlines()[1:]
        if not ln.strip().startswith(("end", "made-by:"))
    ]
    s_m = []
    for ln in lines:
        val, unit = ln.split()[0], ln.split()[1]
        s_m.append(float(val) * {"Å": 1e-10, "nm": 1e-9, "µm": 1e-6, "mm": 1e-3}[unit])
    assert s_m == sorted(s_m)
    kinds = ["macro" if "slab" in ln else "atomic" for ln in lines]
    assert "macro" in kinds and "atomic" in kinds
    assert kinds.index("macro") > kinds.index("atomic")  # benzene first, then the slab
    assert any("enter aslab" in ln for ln in lines) and any(
        "exit aslab" in ln for ln in lines
    )
    assert "solid" in body.splitlines()[0]


def test_block_frame_ray_survives_set_pose(store: Store) -> None:
    se, c, n = _benzene_design(store, "bz-pose")
    origin = (c - 4.0 * n) * 1e-10
    _edit(
        se,
        "bz-pose",
        [
            {
                "op": "add_ray",
                "block": "b",
                "name": "through",
                "origin": origin.tolist(),
                "direction": n.tolist(),
                "length": 8e-10,
                "fields": ["pass", "pi"],
            }
        ],
    )
    before = _ray(se, "bz-pose", name="through").splitlines()[1:]
    _edit(
        se,
        "bz-pose",
        [
            {
                "op": "set_pose",
                "block": "b",
                "pose": "[3 -1 2] nm",
                "rot": "[23 0 63] deg",
            }
        ],
    )
    after = _ray(se, "bz-pose", name="through").splitlines()[1:]
    assert before == after and any("pi.enter" in ln for ln in after)
    # a world-frame ray at the old coordinates now misses
    _edit(
        se,
        "bz-pose",
        [
            {
                "op": "add_ray",
                "block": "b",
                "name": "fixed",
                "origin": origin.tolist(),
                "direction": n.tolist(),
                "length": 8e-10,
                "fields": ["pass", "pi"],
                "frame": "world",
            }
        ],
    )
    fixed = _ray(se, "bz-pose", name="fixed")
    assert "pi.enter" not in fixed


def test_observe_then_relax_against_last(a2: SeHandler, store: Store) -> None:
    _edit(a2, SLUG, [AXIS, {"op": "observe_ray", "name": "axis"}])
    tree = persist.load_tree(store, _se_id(store, SLUG))
    spec = tree.blocks["p"].rays["axis"]
    assert len(spec.digests) == 1 and spec.digests[0]["hash"]
    sref = store.get_ref(kind="structure", id=f"{SLUG}-p")
    assert sref is not None
    v0 = spec.digests[0]["structures"][f"{SLUG}-p"]
    # a relax write-back: the same atoms, two moved off the line
    scene, _h = store.structure_load(sref.id)
    for la in ("aC14", "aC15"):
        cart = scene.cell.frac_to_cart(scene.atoms[la].frac) + np.array([0.0, 0.2, 0.0])
        scene.atoms[la].frac = scene.cell.cart_to_frac(cart)
    store.structure_save(
        slug=f"{SLUG}-p",
        title=str(sref.title),
        scene=scene,
        version=int(v0) + 1,
        card_text="relaxed",
        meta_extra={"generated": (sref.meta or {}).get("generated")},
    )
    body = _ray(a2, SLUG, name="axis", against="last")
    head = body.splitlines()[2]
    m = re.match(r"(\d+) moved · (\d+) added · (\d+) removed · (\d+) unchanged", head)
    assert m, body
    assert int(m.group(4)) > 0
    moved = [
        ln
        for ln in body.splitlines()
        if ln.strip().startswith("moved") and " pass " in ln
    ]
    assert {m.group(1) for ln in moved if (m := re.search(r"p#(aC\d+)", ln))} == {
        "aC14",
        "aC15",
    }
    assert all("Δd +0.200 Å" in ln for ln in moved)
    assert not any("aC4" in ln for ln in body.splitlines()[3:])  # unchanged folded
    rays = a2.get(id=SLUG, view="rays").body
    assert "Δ changed since last observe" in rays


def test_out_ray_imports_with_made_by(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "out"
    shutil.copytree(FIXTURE, out)
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    man["rays"] = [
        {
            "name": "axis",
            "block": "p",
            "origin": [0.0, 0.0, 0.0],
            "direction": [1, 0, 0],
            "length": 12e-10,
            "reach": 1.5,
            "frame": 3,
        }
    ]
    (out / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    se = _import_a2(store, out, "hcol-a2ws")
    tree = persist.load_tree(store, _se_id(store, "hcol-a2ws"))
    assert "axis" in tree.blocks["p"].rays
    body = _ray(se, "hcol-a2ws", name="axis")
    made = next(ln for ln in body.splitlines() if ln.startswith("made-by:"))
    assert "ws-hcol::main.layout (main.py:30)" in made
    # the A2 atom's fisheye lists the ray
    eye = se.eye(id="hcol-a2ws~p#aC14", extent="fisheye").body
    assert "Rays:" in eye and "hcol-a2ws~ray:axis" in eye
    # a re-import without the ray retires it (the replace rule)
    man.pop("rays")
    (out / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    _import_a2(store, out, "hcol-a2ws")
    tree = persist.load_tree(store, _se_id(store, "hcol-a2ws"))
    assert "axis" not in tree.blocks["p"].rays


def test_ray_eye_rungs(a2: SeHandler) -> None:
    _edit(a2, SLUG, [AXIS])
    bodies = {
        ext: a2.eye(id=f"{SLUG}~ray:axis", extent=ext).body
        for ext in ("kwd", "summary", "verbatim", "fisheye", "fisheye+1hop")
    }
    assert bodies["kwd"].startswith("ray axis")
    assert "hits:" in bodies["summary"] and "pass " in bodies["summary"]
    assert "pass p#aC14" in bodies["verbatim"]
    assert "crosses:" in bodies["fisheye"] and "block p" in bodies["fisheye"]
    order = ["kwd", "summary", "verbatim", "fisheye", "fisheye+1hop"]
    for lo, hi in itertools.pairwise(order):
        assert bodies[hi].startswith(bodies[lo]), (lo, hi)
    assert estimate_tokens(len(bodies["verbatim"])) <= 600


def test_linkify_ray_handle() -> None:
    from precis_web.routes.eye import linkify_handles

    out = linkify_handles("see hcol-a2~ray:axis next")
    assert out.count("<a") == 1 and "/eye/hcol-a2~ray:axis?" in out


def test_ops_export_carries_ray(a2: SeHandler) -> None:
    _edit(a2, SLUG, [AXIS])
    ops = a2.get(id=SLUG, view="ops").body
    assert '"add_ray"' in ops and '"axis"' in ops


def test_load_save_without_rays_table(store: Store) -> None:
    """Pre-0024 guard: with ``se_rays`` renamed away the design still
    loads and saves (rays dropped with a log line, not a failed save)."""
    se = _import_a2(store, FIXTURE, "hcol-a2nr")
    with store.pool.connection() as conn:
        conn.execute("ALTER TABLE se_rays RENAME TO se_rays_0024x")
    try:
        body = se.edit(
            id="hcol-a2nr", ops=[{"op": "set_desc", "block": "p", "desc": "x"}]
        ).body
        assert body
        assert persist.load_tree(store, _se_id(store, "hcol-a2nr")).blocks
    finally:
        with store.pool.connection() as conn:
            conn.execute("ALTER TABLE se_rays_0024x RENAME TO se_rays")


#: ``get(kind='cad', view='ray')`` on the flange as it rendered before 2r
#: — the macro kernel was extended (``cad.probe.ray_hits``), not forked,
#: so existing callers read these bytes unchanged.
_CAD_RAY_GOLDEN = (
    "probe ray\n{t_in (mm)\tt_out (mm)\tlen (mm)\tstate\tfeature}\n"
    "5\t9.5\t4.5\tsolid\tplate\n9.5\t14.5\t5\tvoid\tbolts#4\n"
    "14.5\t22\t7.5\tsolid\tplate\n22\t38\t16\tvoid\thub_bore\n"
    "38\t45.5\t7.5\tsolid\tplate\n45.5\t50.5\t5\tvoid\tbolts#1\n"
    "50.5\t55\t4.5\tsolid\tplate"
)


def test_cad_view_ray_bytes_unchanged(store: Store) -> None:
    from precis.handlers.cad import CadHandler

    flange = (
        "component flange\n"
        "plate     add  cyl:r25mmh8mm\n"
        "hub_bore  cut  cyl:r8mmh10mm    @0mm,0mm,-1mm\n"
        "bolts     cut  cyl:r2.5mmh10mm  @18mm,0mm,-1mm  polar:n6r18mm\n"
    )
    cad = CadHandler(hub=Hub(store=store))
    cad.put(id="flange-2r", text=flange)
    body = cad.get(
        id="flange-2r", view="ray", args={"o": ["-30mm", "0mm", "4mm"], "d": [1, 0, 0]}
    ).body
    assert body == _CAD_RAY_GOLDEN


def test_cad_ray_hits_clip_to_length() -> None:
    """The §5.4 wrapper over the same segments: clipped to [0, length],
    solid and void alike, attributed to the same node."""
    from precis.cad.probe import probe_ray, ray_hits
    from precis.cad.scene import build_design, parse_source
    from precis.cad.vec import vec3

    design = build_design(
        parse_source("plate add cyl:r25mmh8mm\nbore cut cyl:r8mmh10mm @0mm,0mm,-1mm")
    )
    o, d = vec3(-0.03, 0, 0.004), vec3(1, 0, 0)
    segs = probe_ray(design, o, d).segments
    hits = ray_hits(design, o, d, length=0.02, block_id="plate")
    assert [h.entry for h in hits] == [s.t_in for s in segs if s.t_in < 0.02]
    assert hits[-1].exit == 0.02 and hits[-1].state == segs[len(hits) - 1].state
    assert all(h.block_id == "plate" for h in hits)


def test_structure_kind_ray(store: Store) -> None:
    from precis.utils import handle_registry

    sh = StructureHandler(hub=Hub(store=store))
    sh.put(
        id="bz-st2r",
        text=json.dumps(
            {
                "cell": {"a": 30, "b": 30, "c": 30, "pbc": [False, False, False]},
                "ops": [{"op": "from_smiles", "smiles": "c1ccccc1"}],
            }
        ),
    )
    ref = store.get_ref(kind="structure", id="bz-st2r")
    assert ref is not None
    scene, _h = store.structure_load(ref.id)
    carts = np.asarray(
        [
            scene.cell.frac_to_cart(a.frac)
            for a in scene.atoms.values()
            if a.element == "C"
        ]
    )
    c = carts.mean(axis=0)
    n = np.linalg.svd(carts - c)[2][2]
    sh.edit(
        id="bz-st2r",
        ops=[
            {
                "op": "ray",
                "name": "up",
                "origin": (c - 4 * n).tolist(),
                "direction": n.tolist(),
                "length": 8,
            }
        ],
    )
    body = sh.get(id="bz-st2r", view="ray", args={"name": "up"}).body
    assert body.count("pi.enter") == 1 and body.count("pi.leave") == 1
    assert "(6, aromatic) · pi:0" in body
    # the ray persisted with its definition and reloads (struct_measures)
    scene2, _h = store.structure_load(ref.id)
    rays = [m for m in scene2.measures if m.kind == "ray"]
    assert len(rays) == 1 and rays[0].spec and rays[0].spec["length"] == 8.0
    st = handle_registry.format_handle("structure", ref.id)
    kwd = sh.eye(id=f"{st}~ray:up", extent="kwd").body
    summary = sh.eye(id=f"{st}~ray:up", extent="summary").body
    assert kwd.startswith("ray up") and summary.startswith(kwd)
    assert "hits: " in summary and "pi 2" in summary
    assert f"{st}~ray:up" in sh.get(id="bz-st2r", view="rays").body
    sh.edit(id="bz-st2r", ops=[{"op": "unmark", "name": "up"}])
    scene3, _h = store.structure_load(ref.id)
    assert not [m for m in scene3.measures if m.kind == "ray"]


def test_reobserve_after_relax_writeback(a2: SeHandler, store: Store) -> None:
    """The relax write-back hook: every ray on a block bound to the
    relaxed structure gets a fresh digest carrying the service record."""
    _edit(a2, SLUG, [AXIS, {"op": "observe_ray", "name": "axis"}])
    done = persist.reobserve_rays(
        store, f"{SLUG}-p", service={"job": 7, "version": 2, "rung": "hexfold:x"}
    )
    assert done == [(SLUG, "axis")]
    tree = persist.load_tree(store, _se_id(store, SLUG))
    dg = tree.blocks["p"].rays["axis"].digests
    assert len(dg) == 2 and dg[-1]["service"]["job"] == 7
    assert dg[-1]["hash"] == dg[0]["hash"]  # nothing moved
    # a structure no block binds touches nothing
    assert persist.reobserve_rays(store, f"{SLUG}-nope") == []


def test_hexfold_writeback_calls_reobserve(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from precis_se import hexfold_relax_job

    seen: list[tuple[Any, ...]] = []

    def fake(store: Any, slug: str, *, service: Any = None) -> list[Any]:
        seen.append((slug, service))
        return []

    monkeypatch.setattr(persist, "reobserve_rays", fake)
    hexfold_relax_job._reobserve_rays(
        SimpleNamespace(store=None, ref_id=11), "s1", 3, "r"
    )
    assert seen == [("s1", {"job": 11, "version": 3, "rung": "r"})]

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(persist, "reobserve_rays", boom)
    hexfold_relax_job._reobserve_rays(
        SimpleNamespace(store=None, ref_id=11), "s1", 3, "r"
    )


def test_viewer_ray_pointer(store: Store, runtime_with_store: Any) -> None:
    """``?ray=<name>`` reaches the structure page as a ``data-ray``
    pointer only when the design has that ray (pointer only — the eye
    text is the contract)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    sh = StructureHandler(hub=Hub(store=store))
    sh.put(
        id="bz-view2r",
        text=json.dumps(
            {
                "cell": {"a": 30, "b": 30, "c": 30, "pbc": [False, False, False]},
                "ops": [{"op": "from_smiles", "smiles": "c1ccccc1"}],
            }
        ),
    )
    sh.edit(
        id="bz-view2r",
        ops=[{"op": "ray", "name": "up", "direction": [0, 0, 1], "length": 8}],
    )
    client = TestClient(
        create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    )
    page = client.get("/structure/bz-view2r", params={"ray": "up"})
    assert page.status_code == 200, page.text[:400]
    assert 'data-ray="up"' in page.text
    miss = client.get("/structure/bz-view2r", params={"ray": "nope"})
    assert 'data-ray="nope"' not in miss.text


def test_cad_ray_hits_clip_origin_inside_solid() -> None:
    """A ray is a half-line: an origin inside (or past) a solid starts the
    first hit at t = 0, never at the solid's far-side negative entry."""
    from precis.cad.probe import probe_ray, ray_hits
    from precis.cad.scene import build_design, parse_source
    from precis.cad.vec import vec3

    design = build_design(parse_source("plate add cyl:r25mmh8mm"))
    o, d = vec3(0, 0, 0.004), vec3(1, 0, 0)  # the plate's centre
    assert probe_ray(design, o, d).segments[0].t_in < 0  # the bare line
    hits = ray_hits(design, o, d)
    assert len(hits) == 1
    assert hits[0].entry == 0.0 and abs(hits[0].exit - 0.025) < 1e-9
    assert abs(hits[0].length - 0.025) < 1e-9
    # past the solid: nothing behind the origin is a hit
    assert ray_hits(design, vec3(0.03, 0, 0.004), d) == []


def test_pass_clips_atoms_behind_origin() -> None:
    """Atoms within reach but behind the origin (s < 0) are not passed."""
    from precis.structure.cell import Cell
    from precis.structure.scene import Atom, Scene

    scene = Scene(Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False)))
    for i, x in enumerate((0.2, 0.3, 0.6, 0.7)):
        scene.atoms[f"aC{i + 1}"] = Atom(f"aC{i + 1}", "C", np.array([x, 0.5, 0.5]))
    o = scene.cell.frac_to_cart(np.array([0.5, 0.5, 0.5]))
    tr = st_probe.ray_hits(scene, o, np.array([1.0, 0, 0]), length=8.0, fields=["pass"])
    assert [h.subject for h in tr.hits] == ["aC3", "aC4"]
    assert all(h.s >= 0 for h in tr.hits)
    assert abs(tr.hits[0].s - 2.0) < 1e-9


def test_ray_eye_findings_match_block_exactly() -> None:
    from precis_se.eyeview import _subject_blocks

    assert _subject_blocks("pump") == {"pump"}
    assert "p" not in _subject_blocks("pump.inlet")
    assert _subject_blocks("p.seam—u.stem") == {"p", "u"}
    assert _subject_blocks("a, b") == {"a", "b"}
    assert _subject_blocks("p.port (state 'open')") == {"p"}
