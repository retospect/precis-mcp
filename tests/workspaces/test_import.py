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
from precis_geom.trace_schema import MAX_FRAMES as TRACE_MAX_FRAMES
from precis_geom.trace_schema import frame_key
from precis_se import persist
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
                "key": frame_key(root["key"], "spin", 9, i),
                "parent": root["key"],
                "qual": "spin",
                "file": "main.py",
                "line": 9,
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
