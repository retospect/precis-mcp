"""Workspace import — structure-workspaces slice 0 acceptance
(docs/backlog/structure-workspaces.md, Slices row 0): importing the
fixture into a dev design makes blocks with full ``generated`` (incl.
``trace``); a re-import that drops a block retires it and
``restore_ref`` brings it back; a manifest with an unknown key refuses
naming it; a 25k-frame trace imports with ``trace.truncated``; goldens
pass on main.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.errors import BadInput
from precis.store import Store
from precis.utils.units import UnitRequiredError
from precis_geom.trace_schema import MAX_FRAMES as TRACE_MAX_FRAMES
from precis_geom.trace_schema import frame_key
from precis_se import persist
from precis_se.ops import apply_ops
from precis_se.workspace_import import MAX_WS_BLOCKS, import_workspace_run

DATA = Path(__file__).resolve().parent / "data"
FIXTURE = DATA / "two_blocks" / "out"
DROPPED = DATA / "dropped" / "out"
GOLDEN = DATA / "golden.json"


def _design(store: Store, slug: str = "ws-test") -> int:
    ref = store.insert_ref(kind="se", slug=slug, title="ws test", meta={})
    return int(ref.id)


def _job(store: Store) -> int:
    ref = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    return int(ref.id)


def _import(store: Store, out: Path, **kw: Any):
    return import_workspace_run(
        store,
        workspace="hcol-test",
        design="ws-test",
        out_dir=out,
        run_job=_job(store),
        script_sha="deadbee5",
        lock_sha="1ock",
        image_digest="sha256:img",
        params={"R": 4},
        requested_by="agent:test",
        **kw,
    )


def _trace_doc(sites: dict[int, tuple[str, int]]) -> dict[str, Any]:
    """A minimal schema-1 trace.json: a root plus one ``core`` frame per
    ``sites`` entry (frame id → its call-site file:line) — enough for the
    importer's frame→script-location join."""
    frames = [
        {
            "id": 0,
            "key": "root0000",
            "parent": None,
            "qual": "main",
            "file": "main.py",
            "line": 1,
            "occ": 0,
            "kind": "root",
            "args": {},
            "made": {},
            "touched": {},
        }
    ]
    for fid, (file, line) in sorted(sites.items()):
        frames.append(
            {
                "id": fid,
                "key": f"frame{fid:04d}",
                "parent": "root0000",
                "qual": "out.call",
                "file": file,
                "line": line,
                "occ": 0,
                "kind": "core",
                "args": {},
                "made": {},
                "touched": {},
            }
        )
    return {"schema": 1, "run": {"workspace": "hcol-test"}, "frames": frames}


def _write_out(
    tmp_path: Path,
    *,
    manifest: dict[str, Any],
    files: dict[str, str],
    trace: dict[str, Any] | None = None,
) -> Path:
    """Write a minimal ``out/`` tree: manifest.json + named files +
    optional trace.json. A rewrite replaces the whole tree — nothing
    from a previous call survives (gr514501)."""
    out = tmp_path / "out"
    if out.is_dir():
        shutil.rmtree(out)
    texts = {"manifest.json": json.dumps(manifest), **files}
    if trace is not None:
        texts["trace.json"] = json.dumps(trace)
    for rel, text in texts.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return out


def test_macro_bad_unit_names_block_dsl_line_and_script(
    store: Store, tmp_path: Path
) -> None:
    """gr514246 acceptance 1+6: a bare number in a macro block's DSL fails
    as UnitRequiredError naming the block, the DSL line and the script
    site — and keeps its ``next=`` unit hint."""
    _design(store)
    out = _write_out(
        tmp_path,
        manifest={
            "schema": 1,
            "blocks": [
                {
                    "name": "t_crank",
                    "file": "blocks/t_crank.cad",
                    "scale": "macro",
                    "frame": 3,
                }
            ],
        },
        files={
            "blocks/t_crank.cad": (
                "# a crank\nbase add box:w40mmd20mmh10mm\npin cut cyl:r7h24mm\n"
            )
        },
        trace=_trace_doc({3: ("main.py", 9)}),
    )
    with pytest.raises(BadInput) as exc:
        _import(store, out)
    assert isinstance(exc.value, UnitRequiredError)
    msg = str(exc.value)
    assert "block 't_crank'" in msg
    assert "line 3" in msg and "pin cut cyl:r7h24mm" in msg
    assert "at main.py:9 (frame 3)" in msg
    assert exc.value.next and "state units" in str(exc.value.next)


def test_macro_bad_unit_no_trace_same_error_no_location(
    store: Store, tmp_path: Path
) -> None:
    """gr514246 acceptance 5: without trace.json the same refusal keeps
    the same type and text minus the location suffix."""
    _design(store)
    out = _write_out(
        tmp_path,
        manifest={
            "schema": 1,
            "blocks": [
                {
                    "name": "t_crank",
                    "file": "blocks/t_crank.cad",
                    "scale": "macro",
                    "frame": 3,
                }
            ],
        },
        files={"blocks/t_crank.cad": "pin cut cyl:r7h24mm\n"},
    )
    with pytest.raises(UnitRequiredError) as exc:
        _import(store, out)
    msg = str(exc.value)
    assert "block 't_crank'" in msg and "cyl:r7h24mm" in msg
    assert " at main.py" not in msg
    assert exc.value.next


def test_write_out_rewrite_drops_stale_files(tmp_path: Path) -> None:
    """gr514501: a second ``_write_out`` on the same ``tmp_path`` reflects
    only the new args — a trace.json or block file from the first call
    cannot linger."""
    manifest: dict[str, Any] = {"schema": 1, "blocks": []}
    out = _write_out(
        tmp_path,
        manifest=manifest,
        files={"blocks/a.cad": "a add cyl:r1mmh1mm\n"},
        trace=_trace_doc({}),
    )
    assert (out / "trace.json").is_file() and (out / "blocks/a.cad").is_file()
    out = _write_out(tmp_path, manifest=manifest, files={})
    assert (out / "manifest.json").is_file()
    assert not (out / "trace.json").exists()
    assert not (out / "blocks").exists()


def _two_block_manifest(**kw: Any) -> dict[str, Any]:
    """A t_-templates + instances manifest for the connect/instance
    error tests — ``o.block`` + ``o.instance`` + ``o.connect`` as the
    unicycle script would emit them."""
    manifest: dict[str, Any] = {
        "schema": 1,
        "blocks": [
            {
                "name": "t_axle",
                "file": "blocks/t_axle.cad",
                "scale": "macro",
                "frame": 1,
                "ports": [{"name": "hub", "frame": 2}],
            },
            {
                "name": "t_wheel",
                "file": "blocks/t_wheel.cad",
                "scale": "macro",
                "frame": 3,
                "ports": [{"name": "hub", "frame": 4}],
            },
        ],
        "instances": [
            {"name": "axle", "template": "t_axle", "frame": 5},
            {"name": "wheel", "template": "t_wheel", "frame": 6},
        ],
    }
    manifest.update(kw)
    return manifest


_CAD_FILES = {
    "blocks/t_axle.cad": "axle add cyl:r2mmh40mm\n",
    "blocks/t_wheel.cad": "rim add cyl:r20mmh5mm\n",
}


def test_connect_bad_port_names_script_line_and_ports(
    store: Store, tmp_path: Path
) -> None:
    """gr514247 acceptance 2: a bad connect endpoint names the script
    line and the block's available ports."""
    _design(store)
    out = _write_out(
        tmp_path,
        manifest=_two_block_manifest(
            connects=[{"a": "axle.hubb", "b": "wheel.hub", "frame": 7}]
        ),
        files=_CAD_FILES,
        trace=_trace_doc({7: ("main.py", 30)}),
    )
    with pytest.raises(BadInput) as exc:
        _import(store, out)
    msg = str(exc.value)
    assert "connect axle.hubb—wheel.hub" in msg
    assert "no such port on block 'axle'" in msg
    assert "Available ports: hub" in msg
    assert "at main.py:30 (frame 7)" in msg


def test_unknown_template_lists_templates_not_instances(
    store: Store, tmp_path: Path
) -> None:
    """gr514247 acceptance 3: an unknown template lists the design's
    template-able blocks — never the instances — names the omitted
    count, and cites the script line."""
    _design(store)
    blocks = [
        {
            "name": f"t_p{i}",
            "file": "blocks/t_p0.cad",
            "scale": "macro",
            "frame": 1,
        }
        for i in range(10)
    ]
    out = _write_out(
        tmp_path,
        manifest={
            "schema": 1,
            "blocks": blocks,
            "instances": [
                {"name": "wheel", "template": "t_p0", "frame": 5},
                {"name": "ghost", "template": "t_nope", "frame": 6},
            ],
        },
        files={"blocks/t_p0.cad": "p add box:w10mmd10mmh10mm\n"},
        trace=_trace_doc({6: ("main.py", 31)}),
    )
    with pytest.raises(BadInput) as exc:
        _import(store, out)
    msg = str(exc.value)
    assert "instance 'ghost'" in msg
    assert "no such block (template): 't_nope'" in msg
    assert "Available templates: t_p0" in msg
    assert "… (2 more)" in msg
    assert "'wheel'" not in msg  # instances are not templates
    assert "at main.py:31 (frame 6)" in msg


def test_connect_instance_errors_no_trace_no_location(
    store: Store, tmp_path: Path
) -> None:
    """gr514247 acceptance 5: without trace.json the same refusals keep
    the same types and text minus the location suffix."""
    _design(store)
    out = _write_out(
        tmp_path,
        manifest=_two_block_manifest(
            connects=[{"a": "axle.hubb", "b": "wheel.hub", "frame": 7}]
        ),
        files=_CAD_FILES,
    )
    with pytest.raises(BadInput) as exc:
        _import(store, out)
    assert "Available ports: hub" in str(exc.value)
    assert " at main.py" not in str(exc.value)

    out = _write_out(
        tmp_path,
        manifest=_two_block_manifest(
            instances=[{"name": "ghost", "template": "t_nope", "frame": 6}]
        ),
        files=_CAD_FILES,
    )
    with pytest.raises(BadInput) as exc:
        _import(store, out)
    assert "Available templates: t_axle, t_wheel" in str(exc.value)
    assert " at main.py" not in str(exc.value)


def test_instance_named_like_block_is_vet_refused_with_sites(
    store: Store, tmp_path: Path
) -> None:
    """gr514240 acceptance 4+5: an instance sharing a block's name is a
    manifest-vet refusal naming both entries and their script lines —
    never the generic 'duplicate block name' — and stands without a
    trace too."""
    _design(store)
    manifest = {
        "schema": 1,
        "blocks": [
            {
                "name": "wheel",
                "file": "blocks/wheel.cad",
                "scale": "macro",
                "frame": 1,
            }
        ],
        "instances": [{"name": "wheel", "template": "wheel", "frame": 2}],
    }
    files = {"blocks/wheel.cad": "rim add cyl:r20mmh5mm\n"}
    out = _write_out(
        tmp_path,
        manifest=manifest,
        files=files,
        trace=_trace_doc({1: ("main.py", 5), 2: ("main.py", 11)}),
    )
    with pytest.raises(BadInput) as exc:
        _import(store, out)
    msg = str(exc.value)
    assert "duplicate block name" not in msg
    assert "instances[0]" in msg and "blocks[0]" in msg
    assert "main.py:5" in msg and "main.py:11" in msg
    assert "t_wheel" in str(exc.value.next)

    out = _write_out(tmp_path, manifest=manifest, files=files)
    with pytest.raises(BadInput) as exc:
        _import(store, out)
    assert "collides" in str(exc.value)
    assert "script:" not in str(exc.value)


def test_instance_named_like_existing_design_block(
    store: Store, tmp_path: Path
) -> None:
    """gr514240 design-side backstop: an instance name colliding with a
    block already in the design (vet only sees the manifest) names the
    namespace rule and the script line."""
    _design(store)
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    apply_ops(tree, [{"op": "add_block", "name": "wheel"}])
    persist.save_tree(store, ref_id=se_ref.id, tree=tree, card_text="ws-test")
    out = _write_out(
        tmp_path,
        manifest={
            "schema": 1,
            "blocks": [
                {
                    "name": "t_wheel",
                    "file": "blocks/wheel.cad",
                    "scale": "macro",
                    "frame": 1,
                }
            ],
            "instances": [{"name": "wheel", "template": "t_wheel", "frame": 2}],
        },
        files={"blocks/wheel.cad": "rim add cyl:r20mmh5mm\n"},
        trace=_trace_doc({2: ("main.py", 11)}),
    )
    with pytest.raises(BadInput) as exc:
        _import(store, out)
    msg = str(exc.value)
    assert "duplicate block name" not in msg
    assert "share one namespace" in msg
    assert "at main.py:11 (frame 2)" in msg


def test_import_makes_blocks_with_generated(store: Store) -> None:
    _design(store)
    res = _import(store, FIXTURE)
    assert sorted(res.imported) == ["p", "plate"]
    assert "trace.missing" not in " ".join(res.findings)

    sref = store.get_ref(kind="structure", id="ws-test-p")
    assert sref is not None
    gen = sref.meta["generated"]
    assert gen["generator"] == "workspace"
    assert gen["workspace"] == "hcol-test"
    assert gen["script_sha"] == "deadbee5"
    assert gen["requested_by"] == "agent:test"
    assert gen["trace"]["root"] == res.trace.root_key
    assert gen["frame"] == 1

    scene, _h = store.structure_load(sref.id)
    assert len(scene.atoms) == 4
    assert all(a.maker == 1 for a in scene.atoms.values())
    assert [b.maker for b in scene.bonds] == [1, 2, 3]

    cref = store.get_ref(kind="cad", id="ws-test-plate")
    assert cref is not None
    assert cref.meta["generated"]["workspace"] == "hcol-test"
    assert cref.meta["frames"] == {"plate": 4}

    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    assert set(tree.blocks) == {"p", "plate", "pin"}
    assert tree.blocks["p"].bound_kind == "structure"
    assert tree.blocks["plate"].bound_kind == "cad"
    assert len(tree.connects) == 1

    # trace + manifest stored once on the run job
    with store.pool.connection() as conn:
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT chunk_kind FROM chunks WHERE ref_id = %s",
                (res.run_ref,),
            ).fetchall()
        ]
    assert "ws_trace" in kinds and "ws_manifest" in kinds


def test_replace_retire_restore(store: Store) -> None:
    _design(store)
    _import(store, FIXTURE)
    plate_ref = store.get_ref(kind="cad", id="ws-test-plate")
    assert plate_ref is not None

    res2 = _import(store, DROPPED)
    assert "plate" in [r["name"] for r in res2.retired]
    assert store.get_ref(kind="cad", id="ws-test-plate") is None  # retired
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    assert "plate" not in tree.blocks and "pin" not in tree.blocks

    assert store.restore_ref(plate_ref.id) is True
    assert store.get_ref(kind="cad", id="ws-test-plate") is not None


def test_reimport_reports_changed_unchanged_and_moved(
    store: Store, tmp_path: Path
) -> None:
    """gr514249: a re-import names the replaced blocks whose bound source
    actually changed (prior ``meta.generated.sha`` vs the manifest's
    ``sha``), the unchanged ones and the sha-less unknowns, and the
    instances whose pose moved numerically — where before the echo only
    said ``replaced: t_a, t_b`` for all of them."""
    _design(store)
    cad_a = "pa add box:w10mmd10mmh10mm\n"
    cad_a2 = "pa add box:w10mmd10mmh12mm\n"
    cad_b = "pb add box:w10mmd10mmh10mm\n"

    def write(cad_a_text: str, pose_a: str, pose_b: str, *, sha: bool) -> Path:
        blocks: list[dict[str, Any]] = [
            {"name": "t_a", "file": "blocks/a.cad", "scale": "macro"},
            {"name": "t_b", "file": "blocks/b.cad", "scale": "macro"},
        ]
        if sha:
            for blk, text in ((blocks[0], cad_a_text), (blocks[1], cad_b)):
                blk["sha"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return _write_out(
            tmp_path,
            manifest={
                "schema": 1,
                "blocks": blocks,
                "instances": [
                    {"name": "a", "template": "t_a", "pose": pose_a},
                    {"name": "b", "template": "t_b", "pose": pose_b},
                ],
            },
            files={"blocks/a.cad": cad_a_text, "blocks/b.cad": cad_b},
        )

    res1 = _import(store, write(cad_a, "[0 0 10] mm", "[0 0 10] mm", sha=True))
    assert res1.changed == [] and res1.moved == []
    # the manifest's sha landed on the bound ref for the NEXT import
    cref = store.get_ref(kind="cad", id="ws-test-t_a")
    assert cref is not None
    assert (
        cref.meta["generated"]["sha"]
        == hashlib.sha256(cad_a.encode("utf-8")).hexdigest()
    )

    res2 = _import(
        store,
        write(cad_a2, "[0 0 20] mm", "[0 0 10.000000000000002] mm", sha=True),
    )
    assert res2.changed == ["t_a"]
    assert res2.unchanged == ["t_b"]
    assert res2.sha_unknown == []
    assert res2.moved == ["a"]  # b's pose is float noise — not a move
    echo = res2.echo()
    assert "block sources changed: t_a" in echo
    assert "block sources unchanged: t_b" in echo
    assert "poses moved: a" in echo

    # a sha-less manifest can't be classified — reported, not silent.
    res3 = _import(store, write(cad_a, "[0 0 10] mm", "[0 0 10] mm", sha=False))
    assert res3.sha_unknown == ["t_a", "t_b"]
    assert "block sources unknown (no prior sha): t_a, t_b" in res3.echo()


def test_unknown_key_refuses_naming_it(store: Store, tmp_path: Path) -> None:
    _design(store)
    bad = tmp_path / "out"
    shutil.copytree(FIXTURE, bad)
    m = json.loads((bad / "manifest.json").read_text(encoding="utf-8"))
    m["blocks"][0]["bogus"] = 1
    (bad / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(BadInput, match="bogus"):
        _import(store, bad)


def test_manifest_top_level_unknown_key_refuses(store: Store, tmp_path: Path) -> None:
    _design(store)
    bad = tmp_path / "out"
    shutil.copytree(FIXTURE, bad)
    m = json.loads((bad / "manifest.json").read_text(encoding="utf-8"))
    m["bogus"] = 1
    (bad / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(BadInput, match="bogus"):
        _import(store, bad)


def test_failed_run_manifest_refuses_naming_the_failure(
    store: Store, tmp_path: Path
) -> None:
    """gr514210 — a ``status="failed"`` manifest (``Run.fail``'s record
    shape) is refused as a failed run, not as an "unknown key" — and
    writes nothing."""
    _design(store)
    bad = tmp_path / "out"
    shutil.copytree(FIXTURE, bad)
    m = json.loads((bad / "manifest.json").read_text(encoding="utf-8"))
    m["status"] = "failed"
    m["error"] = {"type": "ValueError", "message": "rows must be >= 1"}
    (bad / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(BadInput, match="run failed") as exc:
        _import(store, bad)
    assert "ValueError" in str(exc.value)
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    assert persist.load_tree(store, se_ref.id).blocks == {}


def test_block_cap_refuses_before_any_write(store: Store, tmp_path: Path) -> None:
    _design(store)
    big = tmp_path / "out"
    shutil.copytree(FIXTURE, big)
    m = json.loads((big / "manifest.json").read_text(encoding="utf-8"))
    m["blocks"] = [
        {"name": f"b{i}", "file": "blocks/p.extxyz", "scale": "atomic"}
        for i in range(MAX_WS_BLOCKS + 1)
    ]
    (big / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(BadInput, match="blocks-per-run cap"):
        _import(store, big)
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    assert persist.load_tree(store, se_ref.id).blocks == {}


def test_trace_truncated(store: Store, tmp_path: Path) -> None:
    _design(store)
    big = tmp_path / "out"
    shutil.copytree(FIXTURE, big)
    trace = json.loads((big / "trace.json").read_text(encoding="utf-8"))
    root = trace["frames"][0]
    frames = [root]
    for i in range(TRACE_MAX_FRAMES + 5000 - 1):
        frames.append(
            {
                "id": i + 1,
                "key": frame_key(root["key"], "spin", i),
                "parent": root["key"],
                "qual": "spin",
                "file": "main.py",
                "line": 9,
                "line_in_def": 2,
                "src": "spin()",
                "occ": i,
                "kind": "user",
                "args": {},
                "made": {},
                "touched": {},
            }
        )
    trace["frames"] = frames
    (big / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    res = _import(store, big)
    assert any(f.startswith("trace.truncated") for f in res.findings)
    assert res.trace.truncated and len(res.trace.frames) == TRACE_MAX_FRAMES
    assert "p" in res.imported


def test_golden(store: Store) -> None:
    _design(store)
    res = _import(store, FIXTURE)
    sref = store.get_ref(kind="structure", id="ws-test-p")
    assert sref is not None
    scene, _ = store.structure_load(sref.id)
    carts = np.array(
        [scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()],
        dtype=np.float64,
    )
    got = {
        "blocks": {
            "p": {
                "atoms": len(scene.atoms),
                "bonds": len(scene.bonds),
                "coord_sha256": hashlib.sha256(carts.tobytes()).hexdigest(),
                "makers": sorted(a.maker or -1 for a in scene.atoms.values()),
            },
            "plate": {"nodes": 1},
        },
        "findings": sorted(res.findings),
        "trace_keys": res.trace.key_set(),
    }
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert got == golden


def test_macro_block_envelope_derived_from_cad_aabb(
    store: Store, tmp_path: Path
) -> None:
    """gr514242: a macro block gets a render envelope at import — a
    sphere about the block origin containing the design's analytic AABB
    (metres), so clearance/drc/validate stop skipping it."""
    _design(store)
    out = _write_out(
        tmp_path,
        manifest={
            "schema": 1,
            "blocks": [{"name": "plate", "file": "blocks/plate.cad", "scale": "macro"}],
        },
        files={"blocks/plate.cad": "p add box:w40mmd20mmh10mm @30mm,0mm,0mm\n"},
    )
    _import(store, out)
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    env = tree.blocks["plate"].envelope
    assert env is not None and env.startswith("sphere:r")
    # AABB x∈[10,50] y∈[-10,10] z∈[0,10] mm → far corner at 50/10/10 mm.
    assert float(env.removeprefix("sphere:r")) == pytest.approx(
        float(np.linalg.norm([0.05, 0.01, 0.01]))
    )
    assert tree.blocks["plate"].origins.get("envelope") == "generated"


def test_macro_block_envelope_refused_when_unboundable(
    store: Store, tmp_path: Path
) -> None:
    """gr514242: a macro block whose cad has no finite AABB (no finite
    solid node) is a named refusal — it never lands envelope-less."""
    _design(store)
    out = _write_out(
        tmp_path,
        manifest={
            "schema": 1,
            "blocks": [
                {
                    "name": "ghost",
                    "file": "blocks/ghost.cad.json",
                    "scale": "macro",
                    "frame": 1,
                }
            ],
        },
        files={
            "blocks/ghost.cad.json": json.dumps(
                {"schema": 1, "nodes": [], "meta": {"units": "mm"}}
            )
        },
        trace=_trace_doc({1: ("main.py", 4)}),
    )
    with pytest.raises(BadInput) as exc:
        _import(store, out)
    assert "block 'ghost'" in str(exc.value)
    assert "at main.py:4 (frame 1)" in str(exc.value)
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    assert persist.load_tree(store, se_ref.id).blocks == {}


def _facet_manifest(**kw: Any) -> dict[str, Any]:
    """One macro block declaring mode/measures/loads + a design
    scenario — gr514245's manifest keys."""
    manifest: dict[str, Any] = {
        "schema": 1,
        "blocks": [
            {
                "name": "axle",
                "file": "blocks/axle.cad",
                "scale": "macro",
                "mode": "stock-cut/steel-1045",
                "measures": [
                    {"name": "bore", "value": 0.02, "strength": "hard"},
                    {"name": "weight", "max": 0.5},
                ],
                "loads": {"force": [0.0, 0.0, -800.0], "fixed": True},
            }
        ],
        "scenario": "prototype",
    }
    manifest.update(kw)
    return manifest


def test_block_facets_and_scenario_import(store: Store, tmp_path: Path) -> None:
    """gr514245: mode/measures/loads on a block + top-level scenario
    replay as ops — the block lands with its manufacturing mode, named
    measures and objectives, and the design's scenario link is set."""
    from precis.design import scenarios

    _design(store)
    out = _write_out(
        tmp_path,
        manifest=_facet_manifest(),
        files={"blocks/axle.cad": "a add cyl:r10mmh100mm\n"},
    )
    _import(store, out)
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    node = tree.blocks["axle"]
    assert node.mode == "stock-cut/steel-1045"
    assert node.objectives["force"] == [0.0, 0.0, -800.0]
    assert node.objectives["fixed"] == ["x", "y", "z"]
    got = {(m.block, m.name) for m in tree.measures}
    assert got == {("axle", "bore"), ("axle", "weight")}
    bore = next(m for m in tree.measures if m.name == "bore")
    assert bore.value == pytest.approx(0.02) and bore.strength == "hard"
    sc = scenarios.design_scenario(store, se_ref.id)
    assert sc is not None and sc.scenario_id == "prototype"


def test_block_facets_dropped_on_reimport(store: Store, tmp_path: Path) -> None:
    """gr514245: the manifest is the workspace's state — a mode/loads/
    measure the re-run no longer declares is cleared, not left stale."""
    from precis.design import scenarios

    _design(store)
    files = {"blocks/axle.cad": "a add cyl:r10mmh100mm\n"}
    _import(store, _write_out(tmp_path, manifest=_facet_manifest(), files=files))
    out = _write_out(
        tmp_path,
        manifest=_facet_manifest(
            blocks=[
                {
                    "name": "axle",
                    "file": "blocks/axle.cad",
                    "scale": "macro",
                    "measures": [{"name": "bore", "value": 0.021}],
                }
            ],
            scenario=None,
        ),
        files=files,
    )
    _import(store, out)
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    node = tree.blocks["axle"]
    assert node.mode is None
    assert node.objectives == {}
    assert [(m.block, m.name) for m in tree.measures] == [("axle", "bore")]
    assert next(m for m in tree.measures).value == pytest.approx(0.021)
    # scenario=None is simply absent: the previous link stands (the
    # manifest records what the RUN declared, and this run said nothing).
    sc = scenarios.design_scenario(store, se_ref.id)
    assert sc is not None and sc.scenario_id == "prototype"


def test_manifest_facet_vet_errors(store: Store, tmp_path: Path) -> None:
    """gr514245 vet shape: stray measure keys and a bad scenario id
    refuse before any write."""
    _design(store)
    out = _write_out(
        tmp_path,
        manifest=_facet_manifest(
            blocks=[
                {
                    "name": "axle",
                    "file": "blocks/axle.cad",
                    "scale": "macro",
                    "measures": [{"name": "bore", "valu": 0.02}],
                }
            ],
            scenario="no_such_scenario",
        ),
        files={"blocks/axle.cad": "a add cyl:r10mmh100mm\n"},
    )
    with pytest.raises(BadInput, match="unknown key 'valu'"):
        _import(store, out)
    out = _write_out(
        tmp_path,
        manifest=_facet_manifest(scenario="no_such_scenario"),
        files={"blocks/axle.cad": "a add cyl:r10mmh100mm\n"},
    )
    with pytest.raises(BadInput, match="unknown scenario") as exc:
        _import(store, out)
    assert "prototype" in str(exc.value)
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    assert persist.load_tree(store, se_ref.id).blocks == {}


def _library_manifest(**kw: Any) -> dict[str, Any]:
    """A flagged template (never instanced), an instance-named template
    and one ordinary block — gr514241's manifest surface."""
    manifest: dict[str, Any] = {
        "schema": 1,
        "blocks": [
            {
                "name": "t_wheel",
                "file": "blocks/t_wheel.cad",
                "scale": "macro",
                "template": True,
            },
            {
                "name": "t_axle",
                "file": "blocks/t_axle.cad",
                "scale": "macro",
            },
            {
                "name": "frame",
                "file": "blocks/frame.cad",
                "scale": "macro",
            },
        ],
        "instances": [
            {
                "name": "wheel",
                "template": "t_wheel",
                "pose": [0.4, 0, 0.1],
                "pose_unit": "m",
            },
            {
                "name": "axle",
                "template": "t_axle",
                "pose": [0, 0, 0.1],
                "pose_unit": "m",
            },
        ],
    }
    manifest.update(kw)
    return manifest


def _library_files() -> dict[str, str]:
    return {
        "blocks/t_wheel.cad": "w add cyl:r30mmh20mm\n",
        "blocks/t_axle.cad": "a add cyl:r10mmh100mm\n",
        "blocks/frame.cad": "f add box:w200mmd100mmh300mm\n",
    }


def test_template_flag_marks_library_parts(store: Store, tmp_path: Path) -> None:
    """gr514241: a ``template: true`` block and every instance-named
    block are library parts — out of the part walk, the fill fraction
    and the envelope pair scan, listed under "templates"."""
    from precis_se import validate as se_validate
    from precis_se.handler import _fill_fraction_line, _render_tree
    from precis_se.ops import template_names

    _design(store)
    _import(
        store,
        _write_out(tmp_path, manifest=_library_manifest(), files=_library_files()),
    )
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    # The flag is surfaced from the workspaces record (flag-carrier);
    # t_axle needs no flag — an instance names it.
    assert tree.blocks["t_wheel"].template_only is True
    assert tree.blocks["t_axle"].template_only is False
    assert template_names(tree) == {"t_wheel", "t_axle"}

    body = _render_tree(tree, "ws-test", "")
    part_walk = body.split("templates (library parts")[0]
    assert "- t_wheel" not in part_walk and "- t_axle" not in part_walk
    assert "- wheel" in part_walk and "- frame" in part_walk
    assert "- t_wheel" in body and "instanced by: wheel" in body

    # No template is a finding subject, and no template pairs in the
    # overlap scan.
    findings = se_validate.validate(tree)
    assert not [f for f in findings if "t_" in f.subject]

    assert "1/1 block(s) have envelopes" in _fill_fraction_line(tree)


def test_template_flag_reimport_drop_and_vet(store: Store, tmp_path: Path) -> None:
    """gr514241: the record is replace semantics — a re-import that no
    longer flags the block un-marks it; a non-boolean refuses."""
    from precis_se.ops import template_names

    _design(store)
    _import(
        store,
        _write_out(tmp_path, manifest=_library_manifest(), files=_library_files()),
    )
    # Re-import with t_wheel still instanced but no longer flagged: the
    # instance keeps it a template anyway (instance-named needs no flag).
    # Flag an extra block instead, and check a plain block returns to
    # part status when its instance is dropped.
    _import(
        store,
        _write_out(
            tmp_path,
            manifest=_library_manifest(
                blocks=[
                    {
                        "name": "t_axle",
                        "file": "blocks/t_axle.cad",
                        "scale": "macro",
                    },
                    {
                        "name": "frame",
                        "file": "blocks/frame.cad",
                        "scale": "macro",
                        "template": True,
                    },
                ],
                instances=[
                    {
                        "name": "axle",
                        "template": "t_axle",
                        "pose": [0, 0, 0.1],
                        "pose_unit": "m",
                    },
                ],
            ),
            files={
                "blocks/t_axle.cad": "a add cyl:r10mmh100mm\n",
                "blocks/frame.cad": "f add box:w200mmd100mmh300mm\n",
            },
        ),
    )
    se_ref = store.get_ref(kind="se", id="ws-test")
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    assert tree.blocks["frame"].template_only is True
    assert tree.blocks["t_axle"].template_only is False
    assert template_names(tree) == {"frame", "t_axle"}

    out = _write_out(
        tmp_path,
        manifest=_library_manifest(
            blocks=[
                {
                    "name": "t_wheel",
                    "file": "blocks/t_wheel.cad",
                    "scale": "macro",
                    "template": "yes",
                }
            ],
            instances=[],
        ),
        files={"blocks/t_wheel.cad": "w add cyl:r30mmh20mm\n"},
    )
    with pytest.raises(BadInput, match="'template' must be true or false"):
        _import(store, out)
