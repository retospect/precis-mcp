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
