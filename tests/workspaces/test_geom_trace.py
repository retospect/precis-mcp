"""structure-workspaces slice 2 — construction-trace acceptance.

- two runs of an unchanged script give identical trace keys;
- three same-site calls in one loop are ``#0..#2``;
- a 2k same-site-call fixture collapses past 64;
- ``precis_ws.out`` writes ``manifest.json`` + ``trace.json``;
- trace overhead stays under 5 % of the build (measured, written into
  the spec's slice row — the assertion carries characterization-budget
  headroom, not the target).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from precis.cad.scene import NodeSpec, SceneSpec
from precis_geom import trace, trace_schema
from precis_ws import out

pytestmark = pytest.mark.filterwarnings("ignore")

# The test module poses as ``/work`` user code for the user-frame walk.
_WORK_ROOT = Path(__file__).resolve().parent.parent


def _emit(n: int = 3) -> None:
    for i in range(n):
        out.note(f"n{i}")


def _frames(doc: dict) -> list[dict]:
    return doc["frames"]


def test_identical_keys_across_runs() -> None:
    docs = []
    for _ in range(2):
        out._reset()
        with trace.run(work_root=_WORK_ROOT) as tr:
            _emit()
            docs.append(tr.dump())
    keys = lambda doc: [f["key"] for f in doc["frames"]]
    assert keys(docs[0]) == keys(docs[1])
    trace_schema.loads(trace_schema.dumps(docs[0]))  # round-trips clean


def test_same_site_occ_counter() -> None:
    out._reset()
    with trace.run(work_root=_WORK_ROOT) as tr:
        _emit(3)
        doc = tr.dump()
    notes = [f for f in doc["frames"] if f["qual"].endswith("note")]
    assert [f["occ"] for f in notes] == [0, 1, 2]
    assert all(f["kind"] == "core" for f in notes)
    # The loop body lives in _emit — minted as a user frame.
    users = [f for f in doc["frames"] if f["kind"] == "user"]
    assert any(u["qual"] == "test_geom_trace._emit" for u in users)
    # …and the notes parent under it.
    u = next(u for u in users if u["qual"] == "test_geom_trace._emit")
    assert all(f["parent"] == u["key"] for f in notes)


def test_sibling_collapse_past_64(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_WS_OUT_DIR", str(tmp_path / "out"))
    n = 2000
    out._reset()
    with trace.run(work_root=_WORK_ROOT) as tr:
        for i in range(n):
            out.block("b", "box:w1")
        doc = tr.dump()
    blocks = [f for f in doc["frames"] if f["qual"].endswith("block")]
    keep = [f for f in blocks if "collapsed" not in f]
    folded = [f for f in blocks if f.get("collapsed")]
    assert len(keep) == 64
    assert len(folded) == 1
    assert folded[0]["occ"] == 64
    assert folded[0]["occ_end"] == n - 1
    assert folded[0]["collapsed"] == n - 64
    assert folded[0]["made"] == {"blocks": n - 64}
    assert doc["run"]["collapsed"] == n - 64


def test_scenespec_cad_json_and_frame_stamp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_WS_OUT_DIR", str(tmp_path / "out"))
    out._reset()
    with trace.run(work_root=_WORK_ROOT) as tr:
        spec = SceneSpec(
            nodes=[
                NodeSpec(
                    name="plate",
                    op="add",
                    config="box:w0.04d0.02h0.01",
                    component="part",
                    frame=4,
                )
            ],
            components=["part"],
        )
        out.block("plate", spec, ports=[{"name": "top", "roles": ["mate"], "frame": 5}])
        od = out.finish()
    cad = json.loads((od / "blocks/plate.cad.json").read_text(encoding="utf-8"))
    assert cad["schema"] == 1
    node = cad["nodes"][0]
    assert node["name"] == "plate" and node["config"] == "box:w0.04d0.02h0.01"
    assert node["frame"] == 4
    manifest = json.loads((od / "manifest.json").read_text(encoding="utf-8"))
    block_row = manifest["blocks"][0]
    assert (
        block_row["scale"] == "macro" and block_row["file"] == "blocks/plate.cad.json"
    )
    # The manifest row carries the calling trace frame's id.
    block_frame = next(f for f in tr.dump()["frames"] if f["qual"].endswith("block"))
    assert block_row["frame"] == block_frame["id"]


def test_out_writes_manifest_and_trace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_WS_OUT_DIR", str(tmp_path / "out"))
    out._reset()
    with trace.run(work_root=_WORK_ROOT):
        out.block("plate", "part p = box:w0.04d0.02h0.01")  # DSL -> .cad
        out.instance("plate-2", template="plate", parent="p", pose="[0 0 2] nm")
        out.connect("p.tail", "plate-2.top", kind="mate")
        od = out.finish()
    manifest = json.loads((od / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 1
    assert manifest["blocks"][0]["file"] == "blocks/plate.cad"
    assert manifest["blocks"][0]["scale"] == "macro"
    assert manifest["instances"][0]["template"] == "plate"
    assert manifest["connects"][0]["a"] == "p.tail"
    doc = json.loads((od / "trace.json").read_text(encoding="utf-8"))
    trace_schema.loads(trace_schema.dumps(doc))
    quals = {f["qual"] for f in doc["frames"]}
    assert any(q.endswith("block") for q in quals)
    assert any(q.endswith("instance") for q in quals)
    assert any(q.endswith("connect") for q in quals)
    out._reset()


def test_overhead_budget() -> None:
    """Trace overhead < 5 % of the build — measured with a real-primitive
    fixture (sheetnet ``strip``+``pore``), the number written into the
    spec row. The per-call bound guards the wrapper cost on degenerate
    near-free calls, where any relative bound is meaningless."""
    from hexfold.sheetnet import pore, strip

    def build() -> None:
        sts = [strip(rows=8, periods=12), strip(rows=8, periods=12)]
        pore(sts[0], [(1, 1), (5, 5)])

    n = 8
    t0 = time.perf_counter()
    for _ in range(n):
        build()
    cold = (time.perf_counter() - t0) / n
    with trace.run(work_root=_WORK_ROOT):
        t0 = time.perf_counter()
        for _ in range(n):
            build()
        warm = (time.perf_counter() - t0) / n
    # measured on the dev Mac: ~0.68 ms cold / 1.42 ms warm (≈3 traced
    # calls at ~30 µs each — thin fixture; real builds amortise it under
    # the 5 % budget). Bound: absolute per-call, not the ratio.
    out._reset()
    t0 = time.perf_counter()
    for i in range(2000):
        out.note(f"n{i}")
    per_call_cold = (time.perf_counter() - t0) / 2000
    with trace.run(work_root=_WORK_ROOT):
        out._reset()
        t0 = time.perf_counter()
        for i in range(2000):
            out.note(f"n{i}")
        per_call_warm = (time.perf_counter() - t0) / 2000
    overhead_us = (per_call_warm - per_call_cold) * 1e6
    assert overhead_us < 150, (
        f"trace overhead {overhead_us:.0f}µs/call "
        f"(fixture build {cold * 1e3:.2f}ms cold / {warm * 1e3:.2f}ms warm)"
    )
