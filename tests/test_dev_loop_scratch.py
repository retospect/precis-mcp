"""dev-loop-scratch slice D0 — the scratch dir, the host-run import
surface, and the ``precis_geom.out`` writer
(docs/backlog/dev-loop-scratch.md §D0).

Acceptance: a scratch script on the Mac host writes an ``out/`` tree that
``precis_se.workspace_import`` accepts; the host-run surface (``precis_geom``
+ the atom-tiler path ``precis_se.atomic``) imports with no container-only
dep; ``scratch/`` exists in every worktree and nothing under it is ever
committed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from precis.store import Store
from precis.structure import Scene, apply_ops
from precis.structure.cell import Cell
from precis.structure.importers.extxyz import bonds_from_json, extxyz_to_scene
from precis_geom import out, trace_schema
from precis_se.workspace_import import (
    _spec_from_cad_json,
    _vet_manifest,
    import_workspace_run,
)

ROOT = Path(__file__).resolve().parent.parent


def _strip_scene() -> Scene:
    """A 4-carbon strip in a big non-periodic box — the shape of the
    ``two_blocks`` fixture's block ``p``."""
    scene = Scene(cell=Cell.from_lengths_angles(30, 30, 30, pbc=(False, False, False)))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "C", "cart": [float(1.3 * i), 0.0, 0.0]}
            for i in range(4)
        ]
        + [
            {"op": "add_bond", "i": f"aC{i}", "j": f"aC{i + 1}", "order": 1.0}
            for i in range(1, 4)
        ],
    )
    return scene


# -- scratch/ dir ------------------------------------------------------------


def test_scratch_dir_exists_and_is_untracked(tmp_path: Path) -> None:
    """Every worktree carries ``scratch/`` (via the committed ``.keep``);
    everything else under it is ignored — a run never becomes source.

    The ignore rules are exercised in a throwaway repo holding the
    worktree's own ``.gitignore``: the gate container runs on a synced
    copy of the tree, so the worktree itself is not a git repo there."""
    assert (ROOT / "scratch" / ".keep").is_file()
    repo = tmp_path / "rig"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(
        (ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8"
    )

    def _ignored(path: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(repo), "check-ignore", "-q", path],
                check=False,
            ).returncode
            == 0
        )

    assert _ignored("scratch/probe.py")
    assert _ignored("scratch/out/r1/manifest.json")
    assert not _ignored("scratch/.keep")


# -- host-run import surface ---------------------------------------------------

#: Backends that exist only inside the container extras — a host-run
#: surface that pulls one in at import time has broken the lazy-import
#: rule (see ``precis.structure.relax``'s module docstring).
_CONTAINER_ONLY = {"torch", "mace", "tblite", "gpaw"}

#: The modules a scratch script on the host reaches for. Probed in a
#: fresh subprocess: an earlier test in this session may already have
#: imported (or failed to import) pieces of the surface.
_HOST_RUN_SURFACE = (
    "precis_geom",
    "precis_geom.trace",
    "precis_geom.trace_schema",
    "precis_geom.extxyz",
    "precis_geom.out",
    "precis_se.atomic",
    "precis_se.atomic.apply",
    "precis_se.atomic.bind",
    "precis_se.atomic.generate",
    "precis_se.atomic.generators",
    "precis_se.atomic.join",
    "precis_se.atomic.relax_block",
    "precis.structure.scene",
    "precis.structure.relax",
    "precis.structure.export",
    "precis.structure.importers.extxyz",
    "precis.cad.graph",
    "precis.cad.scene",
    "hexfold.prism",
    "hexfold.build",
    "hexfold.stick",
)


def test_host_run_surface_imports() -> None:
    """The host-run surface imports cleanly *and* drags no container-only
    dep into ``sys.modules`` — the second half matters even where the
    backend happens to be installed (a dev Mac)."""
    probe = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in _HOST_RUN_SURFACE)
        + "print(','.join(sorted(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    leaked = set(result.stdout.strip().split(",")) & _CONTAINER_ONLY
    assert not leaked, (
        f"importing the host-run surface pulled in {sorted(leaked)} — move the "
        "import inside the function that needs it"
    )


# -- precis_geom.out -----------------------------------------------------------


def test_bonds_to_json_is_the_importers_write_side() -> None:
    """``export.bonds_to_json`` round-trips through ``bonds_from_json``;
    importer defaults (order=1.0, kind='pairwise', no maker) are omitted
    from the JSON."""
    from precis.structure.export import bonds_to_json
    from precis.structure.scene import Bond

    bonds = [
        Bond(i="aC1", j="aC2", order=1.0, kind="pairwise", maker=None),
        Bond(i="aC2", j="aC3", order=1.5, kind="aromatic", maker=7),
    ]
    doc = json.loads(bonds_to_json(bonds))
    assert doc["schema"] == 1
    assert doc["bonds"][0] == {"i": "aC1", "j": "aC2"}
    assert doc["bonds"][1] == {
        "i": "aC2",
        "j": "aC3",
        "order": 1.5,
        "kind": "aromatic",
        "maker": 7,
    }
    back = bonds_from_json(bonds_to_json(bonds))
    assert [(b.i, b.j, b.order, b.kind, b.maker) for b in back] == [
        ("aC1", "aC2", 1.0, "pairwise", None),
        ("aC2", "aC3", 1.5, "aromatic", 7),
    ]


def test_out_run_writes_the_importer_layout(tmp_path: Path) -> None:
    """A ``with out.run(...)`` block produces ``manifest.json`` +
    ``trace.json`` + ``blocks/…`` the importer's own validators accept."""
    out_dir = tmp_path / "out" / "r1"
    with out.run(out_dir, work_root=tmp_path) as o:
        o.block(
            "p",
            _strip_scene(),
            desc="carbon strip",
            ports=[
                {
                    "name": "tail",
                    "atom": "aC4",
                    "roles": ["join"],
                    "expected_element": "C",
                }
            ],
        )
        o.block("plate", "plate add box:w40mmd20mmh10mm", desc="plate part")
        o.instance("pin", template="plate", parent="p", pose="[0 0 10] nm")
        o.connect("p.tail", "pin.top")

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 1
    _vet_manifest(manifest)  # the importer's own contract — raises naming a key
    assert [b["name"] for b in manifest["blocks"]] == ["p", "plate"]
    assert manifest["blocks"][0]["file"] == "blocks/p.extxyz"
    assert manifest["blocks"][0]["scale"] == "atomic"
    assert manifest["blocks"][1]["file"] == "blocks/plate.cad"
    assert manifest["instances"][0]["name"] == "pin"
    assert manifest["connects"][0]["a"] == "p.tail"
    assert manifest["connects"][0]["b"] == "pin.top"

    # the geometry files the manifest names round-trip through the readers
    scene = extxyz_to_scene((out_dir / "blocks/p.extxyz").read_text(encoding="utf-8"))
    assert len(scene.atoms) == 4 and set(scene.atoms) == {"aC1", "aC2", "aC3", "aC4"}
    bonds = bonds_from_json(
        (out_dir / "blocks/p.bonds.json").read_text(encoding="utf-8")
    )
    assert [(b.i, b.j) for b in bonds] == [
        ("aC1", "aC2"),
        ("aC2", "aC3"),
        ("aC3", "aC4"),
    ]

    # trace.json is a schema-1 document; the out.* calls are core frames and
    # the manifest frame fields cite their run-local ids
    doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    by_id = {f["id"]: f for f in doc["frames"]}
    quals = {f["qual"] for f in doc["frames"]}
    assert {"out.block", "out.instance", "out.connect"} <= quals
    for entry in manifest["blocks"] + manifest["instances"] + manifest["connects"]:
        assert entry["frame"] in by_id


def test_out_run_mints_user_frames_for_script_code(tmp_path: Path) -> None:
    """With ``work_root`` covering this file, the test function mints as a
    ``user`` frame and the ``out.*`` core frames record this file as their
    call site — the same script→call shape the sandbox's /work capture
    produces."""
    out_dir = tmp_path / "out" / "u1"
    with out.run(out_dir, work_root=ROOT) as o:
        o.block("p", _strip_scene())
    doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    frames = doc["frames"]
    assert any(f["kind"] == "user" for f in frames)
    core = [f for f in frames if f["qual"] == "out.block"]
    assert core and core[0]["file"] == "tests/test_dev_loop_scratch.py"


def test_out_run_default_target_is_scratch_out(tmp_path: Path) -> None:
    assert out.default_root("myrun") == Path("scratch") / "out" / "myrun"


def test_out_block_writes_cad_json_for_a_scenespec(tmp_path: Path) -> None:
    """A macro block may be a SceneSpec — serialised as ``.cad.json`` the
    importer's ``_spec_from_cad_json`` reads back."""
    from precis.cad.scene import NodeSpec, SceneSpec

    spec = SceneSpec(
        nodes=[
            NodeSpec(
                name="plate",
                op="add",
                config="box:w0.04d0.02h0.01",
                component="part",
            )
        ],
        components=["part"],
    )
    out_dir = tmp_path / "out" / "m1"
    with out.run(out_dir, work_root=tmp_path) as o:
        o.block("plate", spec)
    entry = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))[
        "blocks"
    ][0]
    assert entry["file"] == "blocks/plate.cad.json" and entry["scale"] == "macro"
    back = _spec_from_cad_json(
        (out_dir / "blocks/plate.cad.json").read_text(encoding="utf-8"),
        what="plate.cad.json",
    )
    assert len(back.nodes) == 1 and back.nodes[0].config == "box:w0.04d0.02h0.01"


def test_out_block_refuses_duplicates_and_bad_names(tmp_path: Path) -> None:
    with out.run(tmp_path / "out" / "v1", work_root=tmp_path) as o:
        o.block("p", _strip_scene())
        with pytest.raises(ValueError, match="duplicate"):
            o.block("p", _strip_scene())
        with pytest.raises(ValueError):
            o.block("../escape", _strip_scene())
        with pytest.raises(ValueError):
            o.block("p2", object())


def test_out_finish_is_idempotent_guarded(tmp_path: Path) -> None:
    o = out.Run(tmp_path / "out" / "f1")
    o.finish()
    with pytest.raises(ValueError, match="finish"):
        o.finish()


def test_out_run_emits_a_pcb_manifest(tmp_path: Path) -> None:
    """gr513754 — a pcb Board is a whole run's product: ``o.board(b)``
    has ``finish()`` write the ``kind='pcb'`` manifest (board +
    requests); a Board handed to ``block`` routes to ``o.board``, and
    the se rows refuse to mix with a declared board either way."""
    import precis_geom.pcb as pcb

    b = pcb.Board("t")
    gnd = b.net("GND")
    j1 = b.part("J1", lcsc="C1")
    j1["1"].etype("power_in", max_current_a=0.1, max_voltage_v=5.0, assume="header gnd")
    gnd.tie(j1["1"])
    out_dir = tmp_path / "out" / "b1"
    with out.run(out_dir, work_root=tmp_path) as o:
        o.block("board", b)  # a Board routes to o.board
        with pytest.raises(ValueError, match="kind='pcb'"):
            o.block("p", _strip_scene())
        with pytest.raises(ValueError, match="kind='pcb'"):
            o.instance("i", template="p")
        with pytest.raises(ValueError, match="kind='pcb'"):
            o.connect("p.tail", "i.top")
        with pytest.raises(ValueError, match="kind='pcb'"):
            o.ray("r1", block="p", length="1 mm")
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "pcb"
    assert manifest["board"]["components"][0]["refdes"] == "J1"
    assert manifest["requests"] == []
    assert not (out_dir / "blocks").exists()  # routed, no block file
    doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    assert any(f["qual"] == "out.board" for f in doc["frames"])

    out_dir_req = tmp_path / "out" / "b1r"
    with out.run(out_dir_req, work_root=tmp_path) as o:
        o.board(b, requests=[{"op": "place", "key": "k"}])
    manifest = json.loads((out_dir_req / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requests"] == [{"op": "place", "key": "k"}]

    out_dir2 = tmp_path / "out" / "b2"
    with out.run(out_dir2, work_root=tmp_path) as o:
        o.block("p", _strip_scene())
        with pytest.raises(ValueError, match="cannot mix"):
            o.board(b)


# -- importer acceptance (the D0 acceptance line) ------------------------------


def test_written_out_imports(store: Store, tmp_path: Path) -> None:
    """A scratch-run ``out/`` the writer produced imports through
    :func:`import_workspace_run` — the dev-loop run and the sandboxed
    ``precis_ws.out`` run are the same tree."""
    store.insert_ref(kind="se", slug="ws-test", title="ws test", meta={})
    job = store.insert_ref(kind="job", slug=None, title="scratch run", meta={})
    out_dir = tmp_path / "out" / "r1"
    with out.run(out_dir, work_root=ROOT) as o:
        o.block(
            "p",
            _strip_scene(),
            desc="carbon strip",
            ports=[
                {
                    "name": "tail",
                    "atom": "aC4",
                    "roles": ["join"],
                    "expected_element": "C",
                }
            ],
        )
    res = import_workspace_run(
        store,
        workspace="scratch",
        design="ws-test",
        out_dir=out_dir,
        run_job=int(job.id),
    )
    assert res.imported == ["p"]
    assert not any(f.startswith("trace.missing") for f in res.findings)
    sref = store.get_ref(kind="structure", id="ws-test-p")
    assert sref is not None
    scene, _h = store.structure_load(sref.id)
    assert len(scene.atoms) == 4 and len(scene.bonds) == 3
