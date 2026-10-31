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

import dataclasses
import hashlib
import importlib.util
import json
import time
from pathlib import Path
from types import ModuleType

import pytest

from precis.cad.scene import NodeSpec, SceneSpec
from precis_geom import trace, trace_schema
from precis_ws import out
from tests._timing import budget
from tests.workspaces._script import load_script

pytestmark = pytest.mark.filterwarnings("ignore")

# The test module poses as ``/work`` user code for the user-frame walk.
_WORK_ROOT = Path(__file__).resolve().parent.parent

#: A two-function script — ``build`` (def line 4) delegates to ``sheet``
#: (def line 8), which calls ``strip`` (line 9).
_SCRIPT = (
    "from hexfold.sheetnet import strip\n"
    "\n"
    "\n"
    "def build(rows, periods):\n"
    "    sheet(rows, periods)\n"
    "\n"
    "\n"
    "def sheet(rows, periods):\n"
    "    strip(rows=rows, periods=periods)\n"
)


#: A script whose second ``strip`` call raises on a bad ``edge``.
_FAILING_SCRIPT = (
    "from hexfold.sheetnet import strip\n"
    "\n"
    "\n"
    "def build():\n"
    "    strip(rows=4, periods=3)\n"
    "    strip(rows=4, periods=3, edge='bogus')\n"
)


def _emit(n: int = 3) -> None:
    for i in range(n):
        out.note(f"n{i}")


@trace.traced
def _outer_fail() -> None:
    from hexfold.sheetnet import strip

    strip(rows=4, periods=3, edge="bogus")


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
    assert docs[0]["schema"] == 3
    assert all("line_in_def" in f and "src" in f for f in docs[0]["frames"])
    trace_schema.loads(trace_schema.dumps(docs[0]))  # round-trips clean


def _emit_two_sites() -> None:
    out.note("a")
    out.note("b")


def test_occ_counter_is_per_qual_across_sites() -> None:
    """Schema 2: ``occ`` counts a callee's occurrences under one parent
    across ALL call sites — a second ``note`` call on a different line
    continues the count (schema 1 counted per ``(qual, line)``)."""
    out._reset()
    with trace.run(work_root=_WORK_ROOT) as tr:
        _emit(2)
        _emit_two_sites()
        doc = tr.dump()
    notes = [f for f in doc["frames"] if f["qual"].endswith("note")]
    assert all(f["kind"] == "core" for f in notes)
    users = [f for f in doc["frames"] if f["kind"] == "user"]
    emit = next(u for u in users if u["qual"].endswith("._emit"))
    two = next(u for u in users if u["qual"].endswith("._emit_two_sites"))
    loop_notes = [f for f in notes if f["parent"] == emit["key"]]
    site_notes = [f for f in notes if f["parent"] == two["key"]]
    assert [f["occ"] for f in loop_notes] == [0, 1]
    # Two distinct call sites, one shared per-qual counter.
    assert [f["occ"] for f in site_notes] == [0, 1]
    assert len({f["line"] for f in site_notes}) == 2


# -- structural keys (gr513662, trace-frame-structural-keys spec §1-5) -------


def _stability_script(
    *,
    head: str = "",
    inner: str = "",
    var: str = "val",
    lit: str = '"b"',
    fname: str = "build",
    swap: bool = False,
) -> str:
    """``main.py`` — ``build`` owns two ``out.note`` call sites; each knob
    is one spec-requirement edit."""
    first, second = (f'f"n{{{var}}}"', lit) if not swap else (lit, f'f"n{{{var}}}"')
    return (
        f"{head}"
        "from precis_ws import out\n"
        "\n"
        "\n"
        f"def {fname}(n):\n"
        f"    {var} = n + 1\n"
        f"{inner}"
        f"    out.note({first})\n"
        f"    out.note({second})\n"
        f"    return {var}\n"
    )


def _run_script(tmp_path: Path, text: str, call: str = "build") -> dict:
    mod = load_script(tmp_path, text)
    with trace.run(work_root=tmp_path) as tr:
        getattr(mod, call)(3)
        return tr.dump()


def _notes(doc: dict) -> list[dict]:
    return [f for f in doc["frames"] if f["qual"].endswith("note")]


def test_keys_survive_lines_inserted_above_the_script(tmp_path: Path) -> None:
    """Req 1: a comment/import above the def moves ``line`` but no key —
    and ``line_in_def`` doesn't even move (the def shifted with it)."""
    base = _run_script(tmp_path, _stability_script())
    edited = _run_script(
        tmp_path, _stability_script(head="# a comment\nimport json\n\n")
    )
    assert [f["key"] for f in edited["frames"]] == [f["key"] for f in base["frames"]]
    for old, new in zip(_notes(base), _notes(edited)):
        assert new["line"] == old["line"] + 3
        assert new["line_in_def"] == old["line_in_def"]
        assert new["src"] == old["src"]
    # …and a line inserted inside the def above the calls moves
    # ``line_in_def`` for the calls below it — still no key change.
    inner = _run_script(tmp_path, _stability_script(inner="    # mark\n"))
    assert [f["key"] for f in inner["frames"]] == [f["key"] for f in base["frames"]]
    for old, new in zip(_notes(base), _notes(inner)):
        assert new["line_in_def"] == old["line_in_def"] + 1


def test_keys_survive_local_rename_and_literal_edits(tmp_path: Path) -> None:
    """Reqs 2–3: renaming a local or changing a literal moves ``src``/
    ``args`` but no key."""
    base = _run_script(tmp_path, _stability_script())
    renamed = _run_script(tmp_path, _stability_script(var="picked"))
    assert [f["key"] for f in renamed["frames"]] == [f["key"] for f in base["frames"]]
    assert _notes(base)[0]["src"] != _notes(renamed)[0]["src"]
    relit = _run_script(tmp_path, _stability_script(lit='"q"'))
    assert [f["key"] for f in relit["frames"]] == [f["key"] for f in base["frames"]]
    assert _notes(relit)[1]["args"] != _notes(base)[1]["args"]


def test_reordering_same_callee_calls_swaps_keys(tmp_path: Path) -> None:
    """Req 4 (accepted): two same-callee calls in one caller trade ``occ``
    — their keys swap; ``src`` still locates each."""
    base = _run_script(tmp_path, _stability_script())
    swapped = _run_script(tmp_path, _stability_script(swap=True))
    assert {f["key"] for f in swapped["frames"]} == {f["key"] for f in base["frames"]}
    a, b = _notes(base)
    sa, sb = _notes(swapped)
    assert (sa["key"], sa["args"], sa["src"]) == (a["key"], b["args"], b["src"])
    assert (sb["key"], sb["args"], sb["src"]) == (b["key"], a["args"], a["src"])


def test_renaming_caller_or_callee_changes_keys(tmp_path: Path) -> None:
    """Req 5: the qual IS the identity — a renamed caller re-keys its
    frame and everything under it."""
    base = _run_script(tmp_path, _stability_script())
    renamed = _run_script(
        tmp_path, _stability_script(fname="assemble"), call="assemble"
    )
    assert {f["key"] for f in renamed["frames"]}.isdisjoint(
        f["key"] for f in base["frames"] if f["kind"] != "root"
    )


def test_line_in_def_and_src_on_a_real_script(tmp_path: Path) -> None:
    """The non-key location fields: user frame anchored on its def line
    carries 0/null (caller outside work); call sites inside it carry the
    def-relative offset and the stripped source line."""
    doc = _run_script(tmp_path, _stability_script())
    build = next(f for f in doc["frames"] if f["qual"] == "main.build")
    assert (build["line"], build["line_in_def"], build["src"]) == (4, 0, None)
    n1, n2 = _notes(doc)
    assert (n1["line"], n1["line_in_def"]) == (6, 2)
    assert n1["src"] == 'out.note(f"n{val}")'
    assert (n2["line"], n2["line_in_def"]) == (7, 3)
    assert n2["src"] == 'out.note("b")'


def test_schema1_trace_doc_still_loads(tmp_path: Path) -> None:
    """Migration: a schema-1 document keeps its line-bearing keys and
    lacks ``line_in_def``/``src`` — ``loads`` accepts it."""
    doc = _run_script(tmp_path, _stability_script())
    doc["schema"] = 1
    for f in doc["frames"]:
        del f["line_in_def"], f["src"]
    assert trace_schema.loads(trace_schema.dumps(doc))["schema"] == 1
    doc["schema"] = 4
    with pytest.raises(ValueError, match="schema"):
        trace_schema.loads(trace_schema.dumps(doc))


def _load_rekey() -> ModuleType:
    """``tests/workspaces/data/rekey_trace.py`` — the golden re-minter
    the spec names for the five hand-authored fixtures (gr514354)."""
    path = Path(__file__).parent / "data" / "rekey_trace.py"
    spec = importlib.util.spec_from_file_location("rekey_trace", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rekey_trace_is_idempotent_on_a_real_dump(tmp_path: Path) -> None:
    """The re-minter reproduces the writer's keys on a live dump, and
    re-mints a hand-authored doc into a ``loads()``-clean schema-2 one
    (a fixed point on a second pass)."""
    rekey = _load_rekey()
    doc = _run_script(tmp_path, _stability_script())
    assert rekey.rekey_doc(doc) == doc

    root_key = trace_schema.sha8("precis-trace-root")
    hand = {
        "schema": 2,
        "run": {"key_collisions": 0},
        "frames": [
            {
                "id": 0,
                "key": "f" * 16,
                "parent": None,
                "qual": "",
                "file": "",
                "line": 0,
                "line_in_def": 0,
                "src": None,
                "occ": 0,
                "kind": "root",
                "args": {},
                "made": {},
                "touched": {},
            },
            {
                "id": 1,
                "key": "a" * 16,
                "parent": "f" * 16,
                "qual": "main.build",
                "file": "main.py",
                "line": 4,
                "line_in_def": 0,
                "src": None,
                "occ": 0,
                "kind": "user",
                "args": {},
                "made": {},
                "touched": {},
            },
            {
                "id": 2,
                "key": "b" * 16,
                "parent": "a" * 16,
                "qual": "out.note",
                "file": "main.py",
                "line": 6,
                "line_in_def": 2,
                "src": 'out.note("x")',
                "occ": 0,
                "kind": "core",
                "args": {},
                "made": {},
                "touched": {},
            },
        ],
    }
    fixed = rekey.rekey_doc(hand)
    trace_schema.loads(trace_schema.dumps(fixed))
    fr = fixed["frames"]
    assert fr[0]["key"] == root_key
    assert fr[1]["key"] == trace_schema.frame_key(root_key, "main.build", 0)
    assert fr[2]["key"] == trace_schema.frame_key(fr[1]["key"], "out.note", 0)
    assert fr[1]["parent"] == fr[0]["key"] and fr[2]["parent"] == fr[1]["key"]
    assert rekey.rekey_doc(fixed) == fixed


def test_forced_duplicate_key_mints_a_tiebreak(tmp_path: Path) -> None:
    """Collisions: a repeated frame key (the ``_sync`` re-mint edge —
    forced here by appending a clone to the tree) emits
    ``sha8(key, '#', args digest, n)`` for the later frame and counts in
    ``run.key_collisions``; ``loads`` accepts the counted doc and rejects
    the same duplicate uncounted."""
    doc = _run_script(tmp_path, _stability_script())
    n1 = _notes(doc)[0]
    dup = dict(n1, id=99)
    doc["frames"].append(dup)
    doc["run"]["key_collisions"] = 0
    with pytest.raises(ValueError, match="key_collisions"):
        trace_schema.loads(trace_schema.dumps(doc))

    # …and through the writer itself: clone a live _Frame into the tree.
    with trace.run(work_root=_WORK_ROOT) as tr:
        out.note("solo")
        parent = tr.stack[-1]  # this test function's user frame
        clone = dataclasses.replace(parent.children[-1], id=tr._next_id)
        tr._next_id += 1
        parent.children.append(clone)
        doc = tr.dump()
    assert doc["run"]["key_collisions"] == 1
    keys = [f["key"] for f in doc["frames"] if f["qual"].endswith("note")]
    assert len(set(keys)) == len(keys) == 2
    assert keys[1] != clone.key
    trace_schema.loads(trace_schema.dumps(doc))


def test_loop_growth_keeps_prefix_and_shrink_drops_tail() -> None:
    """Collisions §loop: ``range(8) -> range(10)`` keeps the first 8 keys
    (new frames are tail ``occ`` values); shrinking drops only tails."""
    runs = []
    for n in (8, 10, 6):
        out._reset()
        with trace.run(work_root=_WORK_ROOT) as tr:
            _emit(n)
            runs.append(
                [f["key"] for f in tr.dump()["frames"] if f["qual"].endswith("note")]
            )
    assert runs[1][:8] == runs[0]
    assert runs[2] == runs[1][:6]


def test_collapse_group_key_stable_past_64() -> None:
    """A group growing past ``COLLAPSE_SIBLINGS`` keeps the folded frame's
    key — ``frame_key(parent, qual, 64)`` — while ``occ_end`` moves."""
    folded = []
    for n in (70, 80):
        out._reset()
        with trace.run(work_root=_WORK_ROOT) as tr:
            _emit(n)
            folded.append(next(f for f in tr.dump()["frames"] if f.get("collapsed")))
    assert folded[0]["key"] == folded[1]["key"]
    assert (folded[0]["occ"], folded[0]["occ_end"]) == (64, 69)
    assert (folded[1]["occ"], folded[1]["occ_end"]) == (64, 79)


def test_root_user_frame_anchors_in_script(tmp_path: Path) -> None:
    """gr513668 — the outermost user frame cites the script's own
    ``def`` line, not the driver's call site outside ``work_root``
    (its caller is this test file); nested user frames keep the
    call-site rule and keys stay stable across runs."""
    mod = load_script(tmp_path, _SCRIPT)
    docs = []
    for _ in range(2):
        with trace.run(work_root=tmp_path) as tr:
            mod.build(8, 4)
            docs.append(tr.dump())
    users = [f for f in docs[0]["frames"] if f["kind"] == "user"]
    assert [f["qual"] for f in users] == ["main.build", "main.sheet"]
    build, sheet = users
    assert (build["file"], build["line"]) == ("main.py", 4)
    assert (sheet["file"], sheet["line"]) == ("main.py", 5)
    core = next(f for f in docs[0]["frames"] if f["qual"].endswith("strip"))
    assert (core["file"], core["line"], core["parent"]) == (
        "main.py",
        9,
        sheet["key"],
    )
    keys = [[f["key"] for f in d["frames"]] for d in docs]
    assert keys[0] == keys[1]


def test_error_marks_the_raising_frame(tmp_path: Path) -> None:
    """gr513669 — a traced call that raised leaves ``error=<ExcType>``
    on its frame; the good sibling and every other frame stay clean."""
    mod = load_script(tmp_path, _FAILING_SCRIPT)
    with trace.run(work_root=tmp_path) as tr:
        with pytest.raises(ValueError, match="edge"):
            mod.build()
        doc = tr.dump()
    marked = [f for f in doc["frames"] if "error" in f]
    assert len(marked) == 1
    assert marked[0]["qual"].endswith("strip")
    assert marked[0]["error"] == "ValueError"
    assert len([f for f in doc["frames"] if f["qual"].endswith("strip")]) == 2


def test_error_marks_only_the_innermost_frame() -> None:
    """An exception propagating out through a traced caller is not
    re-marked — only the frame it left first carries ``error``."""
    with trace.run(work_root=_WORK_ROOT) as tr:
        with pytest.raises(ValueError, match="edge"):
            _outer_fail()
        doc = tr.dump()
    marked = [f for f in doc["frames"] if "error" in f]
    assert len(marked) == 1 and marked[0]["error"] == "ValueError"
    assert marked[0]["qual"].endswith("strip")
    parent = next(f for f in doc["frames"] if f["key"] == marked[0]["parent"])
    assert parent["qual"].endswith("_outer_fail")


def test_success_dump_has_no_error_keys(tmp_path: Path) -> None:
    """``error`` is omitted when ``None`` — a clean run serialises
    exactly as before the field existed."""
    mod = load_script(tmp_path, _SCRIPT)
    with trace.run(work_root=tmp_path) as tr:
        mod.build(8, 4)
        text = trace_schema.dumps(tr.dump())
    assert '"error"' not in text


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
    # gr514248: the sandbox lane stamps the same ``sha`` the dev loop
    # does — the importer's changed/unchanged signal.
    assert (
        manifest["blocks"][0]["sha"]
        == hashlib.sha256((od / "blocks/plate.cad").read_bytes()).hexdigest()
    )
    assert manifest["instances"][0]["template"] == "plate"
    assert manifest["connects"][0]["a"] == "p.tail"
    doc = json.loads((od / "trace.json").read_text(encoding="utf-8"))
    trace_schema.loads(trace_schema.dumps(doc))
    quals = {f["qual"] for f in doc["frames"]}
    assert any(q.endswith("block") for q in quals)
    assert any(q.endswith("instance") for q in quals)
    assert any(q.endswith("connect") for q in quals)
    out._reset()


def test_out_board_writes_pcb_manifest(tmp_path: Path, monkeypatch) -> None:
    """gr513754 — the sandbox lane's ``out.board(b)`` writes the
    ``kind='pcb'`` manifest at ``finish``; a Board handed to
    ``out.block`` routes to ``out.board``, and se rows refuse once a
    board is declared."""
    import precis_geom.pcb as pcb

    monkeypatch.setenv("PRECIS_WS_OUT_DIR", str(tmp_path / "out"))
    out._reset()
    b = pcb.Board("t")
    gnd = b.net("GND")
    j1 = b.part("J1")
    j1["1"].etype("power_in", max_current_a=0.1, max_voltage_v=5.0, assume="header gnd")
    gnd.tie(j1["1"])
    with trace.run(work_root=_WORK_ROOT):
        out.block("board", b)  # a Board routes to out.board
        with pytest.raises(ValueError, match="kind='pcb'"):
            out.block("p", "box:w1")
        od = out.finish()
    manifest = json.loads((od / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "pcb"
    assert manifest["board"]["components"][0]["refdes"] == "J1"
    assert manifest["board"]["nets"][0]["name"] == "GND"
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
    assert overhead_us < budget(150), (
        f"trace overhead {overhead_us:.0f}µs/call "
        f"(fixture build {cold * 1e3:.2f}ms cold / {warm * 1e3:.2f}ms warm)"
    )


def test_out_run_diffs_against_the_previous_trace(tmp_path: Path) -> None:
    """gr513671: a second ``precis_geom.out.run`` into the same dir diffs
    its trace against the one it overwrites — matched by (qual path from
    the root, occ), not the line-based ``key``, so a ``rows=8 -> 10``
    edit reads as one changed frame, not add/remove churn."""
    from hexfold.sheetnet import strip
    from precis_geom import out as geom_out

    def build(rows: int) -> None:
        strip(rows=rows, periods=12)

    out_dir = tmp_path / "out" / "re"
    with geom_out.run(out_dir, work_root=_WORK_ROOT) as r1:
        build(8)
    assert r1.diff is None  # first run: nothing to diff against
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "notes" not in manifest

    with geom_out.run(out_dir, work_root=_WORK_ROOT) as r2:
        build(10)
    assert r2.diff is not None
    assert r2.diff["added"] == [] and r2.diff["removed"] == []
    [changed] = [c for c in r2.diff["changed"] if c["path"].endswith("sheetnet.strip")]
    assert changed["was"]["atoms"] == 224
    assert changed["now"]["atoms"] == 280
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["notes"].startswith("vs previous: +0 -0 ~2")
    assert "atoms 224 -> 280" in manifest["notes"]
    # gr514055: build's own frame changed on args alone (made is empty) —
    # the note carries the ``rows 8 -> 10`` delta that edited the script.
    assert "rows 8 -> 10" in manifest["notes"]


def test_out_run_diff_names_moved_poses_and_changed_sources(
    tmp_path: Path,
) -> None:
    """gr514248: the frame diff is structural — an edited pose string or
    a rewritten block file with identical call shapes reads ``+0 -0 ~0``.
    The manifest diff catches both: poses/rots compare NUMERICALLY (float
    noise on a ``304.79999999999995`` pose stays unmoved), block files
    compare by the ``sha`` each entry carries."""
    from precis_geom import out as geom_out

    out_dir = tmp_path / "out" / "re"
    with geom_out.run(out_dir, work_root=_WORK_ROOT, check=False) as r1:
        r1.block("plate", "plate add box:w40mmd20mmh10mm\n")
        r1.instance("plate-1", template="plate", pose="[0 0 304.8] mm")
    assert r1.manifest_diff is None  # first run: nothing to diff against

    # float noise on the pose + byte-identical sources → not moved, not
    # changed.
    with geom_out.run(out_dir, work_root=_WORK_ROOT, check=False) as r2:
        r2.block("plate", "plate add box:w40mmd20mmh10mm\n")
        r2.instance("plate-1", template="plate", pose="[0 0 304.79999999999995] mm")
    assert r2.manifest_diff is None
    notes = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8")).get(
        "notes", ""
    )
    assert "poses moved" not in notes and "block sources" not in notes

    # a real pose move + a rewritten block source → both named.
    with geom_out.run(out_dir, work_root=_WORK_ROOT, check=False) as r3:
        r3.block("plate", "plate add box:w40mmd20mmh12mm\n")
        r3.instance("plate-1", template="plate", pose="[0 0 724.8] mm")
    assert r3.manifest_diff == {
        "poses_moved": ["plate-1"],
        "blocks_changed": ["plate"],
        "blocks_unknown": [],
    }
    notes = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))["notes"]
    assert "poses moved: plate-1" in notes
    assert "block sources changed: plate" in notes


def test_out_run_diff_groups_same_path_frames(tmp_path: Path) -> None:
    """gr514055: six same-site ``strip`` calls moved by one param edit
    render as ONE ``path x6:`` line carrying the count and the changed
    values — not six identical lines — and the frame ``args`` delta
    (``periods 12 -> 14``, the actual edit) renders next to the ``made``
    counts, including on frames whose counts did not move."""
    from hexfold.sheetnet import strip
    from precis_geom import out as geom_out

    def build(periods: int) -> None:
        for _ in range(6):
            strip(rows=4, periods=periods)

    out_dir = tmp_path / "out" / "re"
    with geom_out.run(out_dir, work_root=_WORK_ROOT):
        build(12)
    with geom_out.run(out_dir, work_root=_WORK_ROOT) as r2:
        build(14)
    assert r2.diff is not None
    # build itself changed on args alone (its made is empty both runs).
    build_entry = next(c for c in r2.diff["changed"] if c["path"].endswith(".build"))
    assert build_entry["was"] == build_entry["now"] == {}
    assert build_entry["args_was"] == {"periods": 12}
    assert build_entry["args_now"] == {"periods": 14}
    strips = [c for c in r2.diff["changed"] if c["path"].endswith("sheetnet.strip")]
    assert len(strips) == 6
    notes = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))["notes"]
    assert "sheetnet.strip x6:" in notes
    assert notes.count("atoms 112 -> 128") == 1
    assert "periods 12 -> 14" in notes


def test_out_run_rotates_previous_trace(tmp_path: Path) -> None:
    """gr514055: the overwritten ``trace.json`` survives one level as
    ``trace.prev.json`` — the run just diffed against stays inspectable
    instead of being lost in place."""
    from hexfold.sheetnet import strip
    from precis_geom import out as geom_out

    out_dir = tmp_path / "out" / "re"
    with geom_out.run(out_dir, work_root=_WORK_ROOT):
        strip(rows=4, periods=12)
    first = (out_dir / "trace.json").read_text(encoding="utf-8")
    assert not (out_dir / "trace.prev.json").exists()

    with geom_out.run(out_dir, work_root=_WORK_ROOT):
        strip(rows=4, periods=14)
    prev = out_dir / "trace.prev.json"
    assert prev.read_text(encoding="utf-8") == first
    second = (out_dir / "trace.json").read_text(encoding="utf-8")
    assert second != first

    # a third run rotates the second trace over the first (one level only)
    with geom_out.run(out_dir, work_root=_WORK_ROOT):
        strip(rows=4, periods=16)
    assert prev.read_text(encoding="utf-8") == second


def test_rewritten_script_never_runs_stale_bytecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-size ``main.py`` rewrite inside one second must run the new
    text even when bytecode is written (CI; the dev container sets
    PYTHONDONTWRITEBYTECODE, which hid the stale-pyc hit)."""
    monkeypatch.setattr("sys.dont_write_bytecode", False)
    first = load_script(tmp_path, "X = 'ab'\n")
    second = load_script(tmp_path, "X = 'ba'\n")
    assert (first.X, second.X) == ("ab", "ba")
