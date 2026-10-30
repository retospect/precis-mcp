"""molecular-workspace-code slice 0 — ``precis_geom.steps``, the build-step
cache at all three scales (atomic Geom, cad SceneSpec, pcb Board).

Acceptance clauses pinned here (the spec's slice 0 row):

- two runs of the worked-example shape with one recipe edit: the first miss
  is the edited step, every step below it is a hit, wall time drops by the
  hits' ``wall_s`` (± 10 %);
- a run killed mid-build resumes with every completed step a hit;
- a hit atom's trace chain equals the fresh build's plus the ``step …
  (hit)`` tag;
- a step cached from one call site and hit from another grafts keys valid
  under the second parent (the ``mol.lib('COOH')`` clause, with a
  test-local cached step standing in for ``mol``);
- a Board and a cad step round-trip byte-identical;
- editing a step function's body misses;
- a trace-schema mismatch is a miss.

Plus the key discipline (parents, content keys, mutation), the LRU cap,
the in-process fallback, ``ls``/``load``/``recipe``/``clear`` and the run
header + echo.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import precis_geom.assembly as pga
from hexfold.prism import prism_tube
from hexfold.stick import stick
from precis.cad.scene import NodeSpec, SceneSpec
from precis.errors import BadInput
from precis.structure.scene import Atom
from precis_geom import extxyz, out, steps, trace, trace_schema
from precis_geom import pcb as pcbmod
from precis_se.traceview import Trace, chain_line

pytestmark = pytest.mark.filterwarnings("ignore:steps:UserWarning")

#: The test module poses as ``/work`` user code for the user-frame walk.
_WORK_ROOT = Path(__file__).resolve().parent.parent

CALLS: list[str] = []


@pytest.fixture(autouse=True)
def cache(tmp_path: Path):
    """Every test gets its own host cache dir and a clean call log."""
    CALLS.clear()
    steps.configure(tmp_path / "cache")
    try:
        yield tmp_path / "cache"
    finally:
        steps.configure()


# ── test-local steps ─────────────────────────────────────────────────────


@steps.step
def tube(periods: int, rows: int, strip_rows: int, seams: str) -> Any:
    """``pga.geom`` over a hexfold Net — the costly atomic step."""
    CALLS.append("tube")
    net, _t = prism_tube(periods, rows, strip_rows, seams)
    return pga.geom(net, coords=stick(net))


@steps.step
def mark(g: Any, every: int) -> Any:
    """Relabel every ``every``-th atom N; the rest keep the parent's makers."""
    CALLS.append("mark")
    sc = copy.deepcopy(g.scene)
    for i, a in enumerate(sc.atoms.values()):
        if i % every == 0:
            a.element = "N"
    return dataclasses.replace(g, scene=sc)


@steps.step
def cap(g: Any) -> Any:
    """Append one H made inside this step (its maker is the step's own)."""
    CALLS.append("cap")
    sc = copy.deepcopy(g.scene)
    label = sc.next_label("H")
    frac = np.array([0.1, 0.1, 0.1])
    sc.atoms[label] = Atom(label, "H", frac=frac, maker=trace.leaf_id())
    return dataclasses.replace(g, scene=sc)


def _worked(every: int, *, periods: int = 4) -> Any:
    """The §Worked example's shape: tube → decorate → cap."""
    p = tube(periods, 4, 2, "++++++")
    p = mark(p, every)
    return cap(p)


def worked_a(every: int) -> Any:
    return _worked(every)


def _scene_of(n: int) -> Any:

    from precis_geom.assembly import _scene_from_parts

    return _scene_from_parts(
        ["C"] * n,
        np.array([[1.4 * i, 0.0, 0.0] for i in range(n)]),
        [(i, i + 1, 1.0) for i in range(n - 1)],
        maker=trace.leaf_id(),
    )


@steps.step
def lib(name: str) -> Any:
    """Stand-in for ``mol.lib('COOH')``: a small fragment built under a
    named frame, atoms stamped with the frame that made them."""
    CALLS.append(f"lib:{name}")
    with trace.trace("embed", kind="core"):
        sc = _scene_of(4)
    return pga.geom(sc, ports={"a1": {"atom": "aC1"}})


@steps.step
def graft_on(g: Any, tag: str) -> Any:
    CALLS.append(f"graft_on:{tag}")
    return dataclasses.replace(g, scene=copy.deepcopy(g.scene))


def site_a() -> Any:
    return graft_on(lib("COOH"), "x")


def site_b() -> Any:
    pad = lib("COOH")  # a different parent chain than site_a
    return graft_on(pad, "x")


def _run(fn: Any, *a: Any) -> tuple[Any, dict[str, Any], trace.Tracer]:
    with trace.run(work_root=_WORK_ROOT) as tr:
        res = fn(*a)
        doc = tr.dump()
    return res, doc, tr


def _twice(fn: Any, *a: Any) -> tuple[Any, dict[str, Any], Any, dict[str, Any]]:
    """Two runs of ``fn`` from the SAME source line — a script re-run, so
    call-site lines (part of a frame's rendering) cannot differ."""
    runs = [_run(fn, *a) for _ in range(2)]
    return runs[0][0], runs[0][1], runs[1][0], runs[1][1]


def _text(g: Any) -> str:
    body, bonds = out._atomic_texts(g.scene)
    return body + (bonds or "")


# ── key + hit/miss ───────────────────────────────────────────────────────


def test_hit_skips_the_function_and_equals_the_fresh_build() -> None:
    fresh, doc1, _ = _run(worked_a, 3)
    assert CALLS == ["tube", "mark", "cap"]
    CALLS.clear()
    hit, doc2, _ = _run(worked_a, 3)
    assert CALLS == []  # all three served from the cache
    # extxyz INCLUDING the maker column and the bonds sidecar, byte for byte
    assert _text(hit) == _text(fresh)
    for k in hit.meta.keys() | fresh.meta.keys():
        if k != "_net":  # the NetRef is compared in its own test below
            assert hit.meta[k] == fresh.meta[k], k  # census, coords, units, step
    assert hit.meta["step"] == fresh.meta["step"]
    assert doc1["run"]["steps"]["hits"] == 0
    assert doc2["run"]["steps"]["hits"] == 3
    assert doc2["run"]["steps"]["rebuilt"] == 0


def test_net_built_geom_keeps_its_net_across_a_hit() -> None:
    """``pga.geom(net)`` carries the Net in ``meta['_net']`` (``mol.cap`` /
    ``mol.sites`` read it): a hit must restore it, makers remapped, so
    downstream chemistry behaves as after a fresh build."""
    import precis_geom.mol as mol
    from precis_geom.mol import selector

    fresh, _d1, hit, _d2 = _twice(lambda: tube(4, 4, 2, "++++++"))
    assert CALLS == ["tube"]
    ref_f, ref_h = fresh.meta["_net"], hit.meta["_net"]
    assert ref_h is not ref_f and ref_h.net == ref_f.net  # equal Net, own object
    assert ref_h.scene is hit.scene  # pristine stays pristine (identity)
    assert selector.net_pristine(hit) is not None
    assert selector.hyb_map(hit) == selector.hyb_map(fresh)
    capped_f, capped_h = mol.cap(fresh), mol.cap(hit)
    assert [a.element for a in capped_h.scene.atoms.values()] == [
        a.element for a in capped_f.scene.atoms.values()
    ]
    assert len(capped_h.scene.atoms) > len(hit.scene.atoms)  # H were added
    cf = [
        capped_f.scene.cell.frac_to_cart(a.frac) for a in capped_f.scene.atoms.values()
    ]
    ch = [
        capped_h.scene.cell.frac_to_cart(a.frac) for a in capped_h.scene.atoms.values()
    ]
    assert np.allclose(cf, ch, atol=1e-9)


def test_net_atom_makers_are_remapped_into_the_live_run() -> None:
    _f, _d1, hit, doc = _twice(lambda: tube(4, 4, 2, "++++++"))
    tv = Trace(doc)
    makers = {a.maker for a in hit.meta["_net"].net.atoms}
    assert makers and None not in makers
    assert all(tv.maker_frame(m) is not None for m in makers)


def test_hit_frames_carry_step_key_and_hit_flag() -> None:
    fresh, doc1, hit, doc2 = _twice(worked_a, 3)
    for doc, flag in ((doc1, False), (doc2, True)):
        top = [f for f in doc["frames"] if f["qual"].startswith("step.")]
        # the three steps this run called; no step nests inside another here
        assert [f["qual"] for f in top] == ["step.tube", "step.mark", "step.cap"]
        assert all(f["step"]["hit"] is flag for f in top)
        assert len(top[0]["step"]["key"]) == 64
    assert fresh.meta["step"] == hit.meta["step"]
    keys1 = [f["step"]["key"] for f in doc1["frames"] if "step" in f]
    keys2 = [f["step"]["key"] for f in doc2["frames"] if "step" in f]
    assert keys1 == keys2
    assert hit.meta["step"] == keys2[-1]


def test_recipe_edit_first_miss_is_the_edited_step_and_below_are_hits() -> None:
    _run(worked_a, 3)
    CALLS.clear()
    _res, doc, _ = _run(worked_a, 4)  # edit the decorate step's parameter
    assert CALLS == ["mark", "cap"]  # tube hit; mark + its descendant rebuilt
    s = doc["run"]["steps"]
    assert s["hits"] == 1 and s["rebuilt"] == 2
    assert s["first_miss"] == "mark#0"
    assert s["wall_saved_s"] > 0
    # an upstream edit rebuilds everything below it
    CALLS.clear()
    _res, doc, _ = _run(worked_a, 3)  # back: all three still cached
    assert CALLS == [] and doc["run"]["steps"]["first_miss"] is None


@steps.step
def slow_a(x: float) -> Any:
    time.sleep(0.4)
    return pga.geom(_scene_of(2), meta={"x": x})


@steps.step
def slow_b(g: Any, y: float) -> Any:
    time.sleep(0.4)
    return dataclasses.replace(g, meta={**g.meta, "y": y})


@steps.step
def slow_c(g: Any, z: float) -> Any:
    time.sleep(0.4)
    return dataclasses.replace(g, meta={**g.meta, "z": z})


@steps.step
def slow_d(g: Any) -> Any:
    time.sleep(0.4)
    return dataclasses.replace(g, meta={**g.meta, "d": 1})


def slow_chain(z: float) -> Any:
    return slow_d(slow_c(slow_b(slow_a(1.0), 2.0), z))


def _timed_pair() -> tuple[float, float, dict[str, Any]]:
    t0 = time.perf_counter()
    _run(slow_chain, 3.0)
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    _res, doc, _ = _run(slow_chain, 4.0)  # edit step 3; steps 1, 2 hit
    warm = time.perf_counter() - t0
    return cold, warm, doc["run"]["steps"]


def test_wall_time_drops_by_the_hits_wall_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance arithmetic on a virtual clock (``sleep`` advances it),
    so a loaded CI box cannot skew a 0.4 s sleep into a failure."""
    clock = [1000.0]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda d: clock.__setitem__(0, clock[0] + d))
    cold, warm, s = _timed_pair()
    assert (s["hits"], s["rebuilt"], s["first_miss"]) == (2, 2, "slow_c#0")
    assert s["wall_saved_s"] == pytest.approx(0.8)  # the two 0.4 s hits
    assert (cold - warm) == pytest.approx(s["wall_saved_s"], rel=0.10)


def test_wall_time_drop_matches_on_the_real_clock() -> None:
    cold, warm, s = _timed_pair()
    assert (s["hits"], s["rebuilt"], s["first_miss"]) == (2, 2, "slow_c#0")
    saved = s["wall_saved_s"]
    assert saved >= 0.8  # two real 0.4 s sleeps, never less
    # ± 10 %, plus a floor for scheduler jitter on a shared box
    assert abs((cold - warm) - saved) <= 0.10 * saved + 0.30


# ── resume after a crash ─────────────────────────────────────────────────


@steps.step
def flaky(g: Any, boom: bool) -> Any:
    CALLS.append("flaky")
    if boom:
        raise RuntimeError("killed mid-build")
    return dataclasses.replace(g, meta={**g.meta, "ok": True})


def crashing(boom: bool) -> Any:
    p = tube(4, 4, 2, "++++++")
    p = mark(p, 3)
    return flaky(p, boom)


def test_run_that_raised_mid_build_resumes_with_completed_steps_as_hits() -> None:
    with pytest.raises(RuntimeError):
        _run(crashing, True)
    assert CALLS == ["tube", "mark", "flaky"]
    CALLS.clear()
    _res, doc, _ = _run(crashing, False)
    assert CALLS == ["flaky"]
    s = doc["run"]["steps"]
    assert (s["hits"], s["rebuilt"], s["first_miss"]) == (2, 1, "flaky#0")


def test_run_killed_without_cleanup_resumes(tmp_path: Path) -> None:
    """``os._exit`` mid-build — no ``finally``, no flush: the entries the
    first process finished are whole (temp dir + rename)."""
    script = tmp_path / "main.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, sys, dataclasses
            import precis_geom.assembly as pga
            from precis_geom import steps, trace
            from precis.structure.scene import Scene
            from precis_geom.assembly import _scene_from_parts
            import numpy as np

            @steps.step
            def base(n):
                sc = _scene_from_parts(["C"] * n, np.array([[1.4 * i, 0, 0] for i in range(n)]), [])
                return pga.geom(sc)

            @steps.step
            def grow(g, k):
                if os.environ.get("KILL") == "1":
                    os._exit(9)
                return dataclasses.replace(g, meta={**g.meta, "k": k})

            with trace.run(work_root=os.getcwd()) as tr:
                grow(base(5), 2)
                s = tr.dump()["run"]["steps"]
                print(s["hits"], s["rebuilt"], s["first_miss"])
            """
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "PRECIS_STEPS_CACHE": str(tmp_path / "kc")}
    killed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env={**env, "KILL": "1"},
        capture_output=True,
        encoding="utf-8",
    )
    assert killed.returncode == 9
    ok = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env={**env, "KILL": "0"},
        capture_output=True,
        encoding="utf-8",
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.split() == ["1", "1", "grow#0"]  # base hit, grow rebuilt


# ── trace graft + maker remap ────────────────────────────────────────────


def _chains(doc: dict[str, Any], g: Any) -> list[str]:
    """Per atom (in atom order): the rendered root→creator chain."""
    tv = Trace(doc)
    out_: list[str] = []
    for a in g.scene.atoms.values():
        fr = tv.maker_frame(a.maker)
        assert fr is not None, f"maker {a.maker} cites no frame"
        out_.append(chain_line(tv.path(fr), fr))
    return out_


def test_hit_atom_chain_equals_fresh_plus_step_tag() -> None:
    fresh, doc1, hit, doc2 = _twice(worked_a, 3)
    c1, c2 = _chains(doc1, fresh), _chains(doc2, hit)
    assert len(c1) == len(c2) > 50
    tag = " · step "
    for a, b in zip(c1, c2, strict=True):
        assert "(built)" in a or tag not in a  # the step frames are tagged
        assert b == a.replace("(built)", "(hit)")
    # at least the cap's own H cites the step.cap frame, tagged as a hit
    assert any("step.cap" in c and "(hit)" in c for c in c2)
    # every frame key is unique and every parent present — loads validates
    trace_schema.loads(trace_schema.dumps(doc2))
    # identical keys frame for frame
    assert [f["key"] for f in doc1["frames"]] == [f["key"] for f in doc2["frames"]]


def _frame_checks(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every frame key re-derives from its parent chain: key ==
    frame_key(parent.key, qual, occ)."""
    by_key = {f["key"]: f for f in doc["frames"]}
    for f in doc["frames"]:
        if f["parent"] is None or f.get("collapsed"):
            continue
        assert f["key"] == trace_schema.frame_key(f["parent"], f["qual"], f["occ"])
        assert f["parent"] in by_key
    return by_key


def test_lib_cached_from_one_call_site_grafts_valid_keys_at_another() -> None:
    a, doc_a, _ = _run(site_a)
    assert CALLS == ["lib:COOH", "graft_on:x"]
    CALLS.clear()
    b, doc_b, _ = _run(site_b)
    assert CALLS == []  # both steps hit — lib and graft_on, across call sites
    # same geometry; only the maker column (run-local ids) may differ
    assert [x.element for x in b.scene.atoms.values()] == [
        x.element for x in a.scene.atoms.values()
    ]
    by_key = _frame_checks(doc_b)
    tv = Trace(doc_b)
    # every atom's maker resolves, and to a frame under site_b's step.lib /
    # step.graft_on — not under site_a (which does not exist in run b)
    for atom in b.scene.atoms.values():
        fr = tv.maker_frame(atom.maker)
        assert fr is not None
        path = [f["qual"] for f in tv.path(fr)]
        assert "test_steps.site_b" in path
        assert "test_steps.site_a" not in path
        assert any(q.startswith("step.") for q in path)
    # the grafted embed frame re-minted under site_b's lib step
    embed = [f for f in doc_b["frames"] if f["qual"] == "embed"]
    assert len(embed) == 1
    ancestors = [f["qual"] for f in tv.path(embed[0])]
    assert ancestors[-4:] == [
        "test_steps.site_b",
        "step.lib",
        "test_steps.lib",
        "embed",
    ]


def test_atoms_keep_the_parents_makers_through_a_child_hit() -> None:
    """``mark`` copies the parent's atoms (parent makers) — after a hit
    those makers must cite the parent step's frames in THIS run."""
    _fresh, _doc1, hit, doc2 = _twice(worked_a, 3)
    tv = Trace(doc2)
    parent_quals: set[str] = set()
    for a in hit.scene.atoms.values():
        fr = tv.maker_frame(a.maker)
        assert fr is not None
        parent_quals.update(f["qual"] for f in tv.path(fr))
    assert "step.tube" in parent_quals  # atoms made inside the tube step
    assert "step.cap" in parent_quals  # the H made inside cap


def test_nested_step_hit_marks_inner_frames_as_hits() -> None:
    @steps.step
    def outer(n: int) -> Any:
        return lib(f"n{n}")

    _run(lambda: outer(1))
    _res, doc, _ = _run(lambda: outer(1))
    assert all(f["step"]["hit"] for f in doc["frames"] if "step" in f)
    assert doc["run"]["steps"]["hits"] == 1  # only the call this run made


# ── schema mismatch / unresolvable graft = miss ──────────────────────────


def _entry_json(cache: Path, key: str) -> Path:
    return cache / key[:2] / key / "step.json"


def test_trace_schema_mismatch_is_a_miss(cache: Path) -> None:
    fresh, _d, _ = _run(site_a)
    key = fresh.meta["step"]
    p = _entry_json(cache, key)
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["trace"]["schema"] = 99
    p.write_text(json.dumps(doc), encoding="utf-8")
    CALLS.clear()
    _run(site_a)
    assert "graft_on:x" in CALLS  # rebuilt (and its parent lib still hits)
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["trace"]["schema"] == trace_schema.SCHEMA_VERSION  # overwritten


def test_unresolvable_external_maker_is_a_miss_not_a_wrong_maker(cache: Path) -> None:
    fresh, _d, _ = _run(site_a)
    key = fresh.meta["step"]
    p = _entry_json(cache, key)
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["ext"], "graft_on's atoms cite lib's frames"
    doc["ext"][0] = ["f", "0" * 16]  # a frame key no run has
    p.write_text(json.dumps(doc), encoding="utf-8")
    CALLS.clear()
    _run(site_a)
    assert CALLS == ["graft_on:x"]


def test_entry_without_a_trace_is_a_miss_under_a_tracer() -> None:
    first = lib("COOH")  # no tracer: stored without a trace
    assert CALLS == ["lib:COOH"]
    assert all(a.maker is None for a in first.scene.atoms.values())
    CALLS.clear()
    _run(lambda: lib("COOH"))  # a tracer is live: cannot graft → rebuild
    assert CALLS == ["lib:COOH"]
    CALLS.clear()
    _run(lambda: lib("COOH"))  # now stored with a trace → hit
    assert CALLS == []


def test_bad_maker_code_is_a_miss(cache: Path) -> None:
    fresh, _d, _ = _run(site_a)
    p = cache / fresh.meta["step"][:2] / fresh.meta["step"] / "scene.extxyz"
    text = p.read_text(encoding="utf-8").splitlines()
    text[2] = text[2].rsplit(" ", 1)[0] + " 99999"  # beyond the stored subtree
    p.write_text("\n".join(text) + "\n", encoding="utf-8")
    CALLS.clear()
    _run(site_a)
    assert "graft_on:x" in CALLS


# ── source sha, version, params ──────────────────────────────────────────


@steps.step(name="body")
def body_v1(x: int) -> Any:
    CALLS.append("v1")
    return pga.geom(_scene_of(3), meta={"x": x})


@steps.step(name="body")
def body_v2(x: int) -> Any:
    CALLS.append("v2")
    y = x + 0
    return pga.geom(_scene_of(3), meta={"x": y})


def test_editing_a_step_functions_body_misses() -> None:
    body_v1(1)
    body_v1(1)
    assert CALLS == ["v1"]
    body_v2(1)  # same name, same params, different source
    assert CALLS == ["v1", "v2"]
    body_v2(1)
    assert CALLS == ["v1", "v2"]


def test_precis_geom_version_is_part_of_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis_geom

    body_v1(1)
    monkeypatch.setattr(precis_geom, "__version__", "9.9.9")
    body_v1(1)
    assert CALLS == ["v1", "v1"]


def test_params_key_floats_arrays_and_defaults() -> None:

    @steps.step
    def p(a: Any, b: float = 1.0, *, c: Any = None) -> Any:
        CALLS.append("p")
        return pga.geom(_scene_of(2))

    p(1)
    p(1, 1.0)  # the explicit default is the same call
    p(1, c=None)
    assert CALLS == ["p"]
    p(1, 1.0000001)  # exact keys
    p(np.arange(3))
    p(np.arange(3))
    p([1, 2], c={"k": (1, 2)})
    p([1, 2], c={"k": (1, 2)})
    p([1, 2], c={"k": [1, 2]})  # tuple != list
    assert CALLS == ["p"] * 5


def test_unkeyable_parameter_refuses_with_a_next_step() -> None:
    net, _t = prism_tube(4, 4, 2, "++++++")
    with pytest.raises(BadInput) as ei:
        tube_of(net)
    assert "hexfold Net" in str(ei.value) or "pga.geom" in str(ei.value)


@steps.step
def tube_of(net: Any) -> Any:
    return pga.geom(net, coords=stick(net))


def test_content_key_for_inputs_built_outside_a_step() -> None:
    sc1, sc2 = _scene_of(3), _scene_of(4)
    graft_on(pga.geom(sc1), "a")
    graft_on(pga.geom(sc1), "a")  # same content → hit
    graft_on(pga.geom(sc2), "a")  # different content → miss
    assert CALLS == ["graft_on:a", "graft_on:a"]


def test_a_transformed_step_output_is_content_keyed_not_parent_keyed() -> None:
    base = lib("COOH")
    g1 = pga.translate(base, [1.0, 0.0, 0.0])
    g2 = pga.translate(base, [2.0, 0.0, 0.0])
    graft_on(g1, "t")
    graft_on(g2, "t")  # same stale meta['step'], different geometry
    assert CALLS.count("graft_on:t") == 2
    graft_on(pga.translate(base, [1.0, 0.0, 0.0]), "t")  # equal content → hit
    assert CALLS.count("graft_on:t") == 2


# ── the macro (cad) scale ────────────────────────────────────────────────


@steps.step
def plate(w: float, holes: int) -> Any:
    CALLS.append("plate")
    nodes = [
        NodeSpec(
            name="base",
            op="add",
            config=f"box:w{w}d20h10",
            component="part",
            frame=trace.leaf_id(),
        )
    ]
    for i in range(holes):
        nodes.append(
            NodeSpec(
                name=f"h{i}",
                op="cut",
                config="cyl:r2h12",
                component="part",
                loc=(5.0 * i, 0.0, 0.0),
                frame=trace.leaf_id(),
            )
        )
    return SceneSpec(nodes=nodes, components=["part"], meta={"units": "mm"})


@steps.step
def dsl_plate(w: float) -> Any:
    CALLS.append("dsl")
    return f"plate box:w{w}d20h10\n"


@steps.step
def stiffen(spec: Any) -> Any:
    CALLS.append("stiffen")
    return SceneSpec(
        nodes=[
            *spec.nodes,
            NodeSpec("rib", "add", "box:w2d20h4", "part", frame=trace.leaf_id()),
        ],
        components=list(spec.components),
        meta=dict(spec.meta),
    )


def plate_run(w: float) -> Any:
    return stiffen(plate(w, 2))


def _macro_bytes(spec: Any, tmp: Path) -> bytes:
    (tmp / "blocks").mkdir(parents=True, exist_ok=True)
    rel, _sha = out._write_macro(tmp, "m", spec)
    return (tmp / rel).read_bytes()


def test_cad_spec_step_round_trips_byte_identical(tmp_path: Path) -> None:
    fresh, _d, _ = _run(plate_run, 40.0)
    assert CALLS == ["plate", "stiffen"]
    CALLS.clear()
    hit, doc, _ = _run(plate_run, 40.0)
    assert CALLS == []
    assert _macro_bytes(hit, tmp_path / "h") == _macro_bytes(fresh, tmp_path / "f")
    assert [n.frame for n in hit.nodes] == [n.frame for n in fresh.nodes]
    tv = Trace(doc)
    quals: list[list[str]] = []
    for n in hit.nodes:
        fr = tv.maker_frame(n.frame)
        assert fr is not None
        quals.append([f["qual"] for f in tv.path(fr)])
    assert any("step.stiffen" in q for q in quals)


def test_dsl_text_step_round_trips() -> None:
    a = dsl_plate(30.0)
    b = dsl_plate(30.0)
    assert a == b == "plate box:w30.0d20h10\n"
    assert CALLS == ["dsl"]


def test_a_mutated_spec_loses_its_stamp() -> None:
    spec = plate(40.0, 1)
    stiffen(spec)
    stiffen(spec)
    assert CALLS == ["plate", "stiffen"]
    spec.nodes.append(NodeSpec("late", "cut", "cyl:r1h5", "part"))
    stiffen(spec)  # content changed since the step returned it → miss
    assert CALLS == ["plate", "stiffen", "stiffen"]


# ── the pcb scale ────────────────────────────────────────────────────────


@steps.step
def board(n: int) -> Any:
    CALLS.append("board")
    b = pcbmod.Board("steps-demo", profile="demo", stackup=4)
    b.net_class("pwr", clearance_mm=0.3)
    b.role("3v3", voltage=3.3)
    fp = pcbmod.Footprint(
        "0603",
        [
            pcbmod.pad("1", "rect", -0.8, 0, w=0.9, h=0.9),
            pcbmod.pad("2", "rect", 0.8, 0, w=0.9, h=0.9),
        ],
    )
    vcc = b.net("VCC", cls="pwr", voltage=3.3)
    gnd = b.net("GND")
    for i in range(n):
        r = b.part(f"R{i + 1}", footprint=fp, value="10k", lcsc=f"C{1000 + i}")
        r["1"].tag("sig").must_terminate()
        r["1"].etype(
            "power_in", max_current_a=0.001, max_voltage_v=3.3, assume="3v3 via 10k"
        )
        r["2"].etype(
            "power_in", max_current_a=0.001, max_voltage_v=3.3, assume="gnd return"
        )
        vcc.tie(r["1"])
        gnd.tie(r["2"], note="return")
        r.at(5.0 * i, 2.0)
    u = b.part("U1", value="mcu", pin_swap_groups=[[1, 2]])
    u.pin(any_of=[1, 2, 3], name="BTN")
    u.pin_functions({"1": ["GPIO"], "2": ["GPIO", "ADC"]}, source="ds:da1~c2")
    b.outline(pcbmod.rect(30, 20))
    b.meta["note"] = "meta survives"
    return b


@steps.step
def board_plus(b: Any) -> Any:
    CALLS.append("board_plus")
    b2 = pcbmod.Board.from_batch(b.to_batch(), b.cache_state())
    b2.net("EXTRA")
    return b2


def board_run(n: int) -> Any:
    return board_plus(board(n))


def test_board_step_round_trips_byte_identical() -> None:
    fresh, _d, _ = _run(board_run, 3)
    assert CALLS == ["board", "board_plus"]
    CALLS.clear()
    hit, doc, _ = _run(board_run, 3)
    assert CALLS == []
    assert pcbmod.canonical_json(hit.to_batch()) == pcbmod.canonical_json(
        fresh.to_batch()
    )
    # not only the batch: the check (sites included) and the manifest
    assert hit.check().summary() == fresh.check().summary()
    assert json.dumps(hit.manifest(), sort_keys=True) == json.dumps(
        fresh.manifest(), sort_keys=True
    )
    # parts/nets keep makers that cite frames of THIS run
    tv = Trace(doc)
    for row in hit.to_batch()["components"]:
        assert tv.maker_frame(row["frame"]) is not None
    assert hit.slug == fresh.slug == "steps-demo"
    assert hit._parts["R1"].site == fresh._parts["R1"].site


def test_board_to_batch_without_a_tracer_is_unchanged() -> None:
    b = board(2)  # miss, no tracer: no frame keys in the batch
    assert all("frame" not in r for r in b.to_batch()["components"])
    again = board(2)
    assert pcbmod.canonical_json(again.to_batch()) == pcbmod.canonical_json(
        b.to_batch()
    )


# ── LRU, backends, ls/load/recipe/clear ──────────────────────────────────


def test_lru_cap_evicts_the_least_recently_used(cache: Path) -> None:
    steps.configure(cache, max_bytes=1)  # smaller than any entry: keep only the newest
    body_v1(1)
    body_v1(2)
    keys = {r["key"] for r in steps.ls()}
    assert len(keys) == 1
    body_v1(3)
    assert [r["name"] for r in steps.ls()] == ["body"]
    CALLS.clear()
    body_v1(3)  # the newest survived
    assert CALLS == []
    body_v1(1)  # evicted earlier
    assert CALLS == ["v1"]


def test_lru_cap_keeps_recently_used_entries(cache: Path) -> None:
    body_v1(1)
    body_v1(2)
    sizes = sorted(r["bytes"] for r in steps.ls())
    steps.configure(cache, max_bytes=sizes[-1] * 2 + 10)  # room for two
    body_v1(1)  # hit: refreshes entry 1
    body_v1(3)  # third entry: evicts the least recently used (2)
    CALLS.clear()
    body_v1(1)
    body_v1(3)
    assert CALLS == []
    body_v1(2)
    assert CALLS == ["v1"]


def test_in_process_fallback_serves_hits_without_a_directory(tmp_path: Path) -> None:
    steps.configure("memory")
    body_v1(7)
    body_v1(7)
    assert CALLS == ["v1"]
    assert not (tmp_path / "cache").exists()
    assert steps.ls()[0]["hits"] == 1
    assert steps.clear() == 1
    body_v1(7)
    assert CALLS == ["v1", "v1"]


def test_sandbox_work_root_defaults_to_the_in_process_cache() -> None:
    steps.configure()  # no override
    with trace.run(work_root="/work"):
        mem = steps._backend()
        assert mem is not None and mem.kind == "memory"
    with trace.run(work_root=_WORK_ROOT):
        be = steps._backend()
        assert isinstance(be, steps._Disk)
        assert be.root == _WORK_ROOT / "cache"


def test_ls_and_load_and_clear() -> None:
    g = lib("COOH")
    rows = steps.ls()
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "lib" and r["hits"] == 0 and r["wall_s"] >= 0
    lib("COOH")
    assert steps.ls("lib")[0]["hits"] == 1
    assert steps.ls("nope") == []
    got = steps.load(r["key"][:12])  # unique prefix
    assert _text(got) == _text(g)
    assert got.meta["step"] == r["key"]
    # a loaded object is a valid step input (stamped with its key)
    CALLS.clear()
    graft_on(got, "L")
    graft_on(g, "L")
    assert CALLS == ["graft_on:L"]
    with pytest.raises(BadInput):
        steps.load("ffffffffffff")
    n = steps.clear()
    assert n >= 2 and steps.ls() == []


def test_load_under_a_tracer_grafts_a_load_frame() -> None:
    g, _d, _ = _run(lambda: lib("COOH"))  # stored with its trace
    with trace.run(work_root=_WORK_ROOT) as tr:
        got = steps.load(g.meta["step"])
        doc = tr.dump()
    assert "step.load" in [f["qual"] for f in doc["frames"]]
    tv = Trace(doc)
    assert all(tv.maker_frame(a.maker) is not None for a in got.scene.atoms.values())


def test_recipe_reads_json_beside_the_script(tmp_path: Path) -> None:
    (tmp_path / "recipe.json").write_text(
        '{"every": 3, "rows": [4, 4]}', encoding="utf-8"
    )
    prev = Path.cwd()
    os.chdir(tmp_path)
    try:
        assert steps.recipe() == {"every": 3, "rows": [4, 4]}
        (tmp_path / "r2.json").write_text("[1]", encoding="utf-8")
        with pytest.raises(BadInput):
            steps.recipe("r2.json")
        with pytest.raises(BadInput) as ei:
            steps.recipe("missing.json")
        assert "recipe.json" in str(ei.value.next)
    finally:
        os.chdir(prev)


def test_fresh_env_ignores_existing_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    body_v1(1)
    monkeypatch.setenv("PRECIS_STEPS_FRESH", "1")
    body_v1(1)
    assert CALLS == ["v1", "v1"]


def test_unstorable_output_runs_uncached_with_a_warning() -> None:
    @steps.step
    def odd() -> Any:
        CALLS.append("odd")
        return 42

    with pytest.warns(UserWarning, match="not a Geom"):
        odd()
    with pytest.warns(UserWarning):
        odd()
    assert CALLS == ["odd", "odd"]

    @steps.step
    def placed() -> Any:
        CALLS.append("placed")
        return pga.assembly("asm", children=[pga.geom(_scene_of(2))])

    with pytest.warns(UserWarning, match="flatten"):
        placed()
    with pytest.warns(UserWarning, match="flatten"):
        placed()
    assert CALLS[-2:] == ["placed", "placed"]  # uncached, and says why


# ── the run header and the echo ──────────────────────────────────────────


def test_run_finish_writes_steps_into_the_trace_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps.configure()  # the default: <work root>/cache
    root = tmp_path / "scratch" / "out" / "w"

    def build(o: out.Run, w: float) -> None:
        o.block("plate", stiffen(plate(w, 2)))
        o.block("t", lib("COOH"))

    for w in (40.0, 40.0, 50.0):
        with out.run(root, check=False) as o:
            build(o, w)
    doc = json.loads((root / "trace.json").read_text(encoding="utf-8"))
    s = doc["run"]["steps"]
    # run 3: lib hit, plate + stiffen rebuilt (the edited width)
    assert (s["hits"], s["rebuilt"], s["first_miss"]) == (1, 2, "plate#0")
    assert set(s) == {"hits", "rebuilt", "first_miss", "wall_saved_s"}
    echoed = capsys.readouterr().out
    assert "steps: 3 hits · 0 rebuilt" in echoed  # run 2
    assert "steps: 1 hit · 2 rebuilt (from plate#0)" in echoed  # run 3
    # the default cache sits under the run's work root (scratch/cache)
    entries = list((tmp_path / "scratch" / "cache").glob("??/*/step.json"))
    assert entries
    doc1 = json.loads(entries[0].read_text(encoding="utf-8"))
    assert {
        "key",
        "name",
        "params",
        "parents",
        "ports",
        "findings",
        "wall_s",
        "trace",
    } <= set(doc1)


def test_failed_run_still_carries_the_steps_digest(tmp_path: Path) -> None:
    root = tmp_path / "scratch" / "out" / "f"
    steps.configure()
    with pytest.raises(RuntimeError):
        with out.run(root, check=False) as o:
            o.block("t", lib("COOH"))
            raise RuntimeError("boom")
    doc = json.loads((root / "trace.json").read_text(encoding="utf-8"))
    assert doc["run"]["status"] == "failed"
    assert doc["run"]["steps"]["rebuilt"] == 1


def test_step_json_shape_and_content_addressed_layout(cache: Path) -> None:
    g = lib("COOH")
    key = g.meta["step"]
    d = cache / key[:2] / key
    assert (d / "step.json").is_file()
    assert (d / "scene.extxyz").is_file()
    doc = json.loads((d / "step.json").read_text(encoding="utf-8"))
    assert doc["key"] == key and doc["name"] == "lib"
    assert doc["params"] == {"name": "COOH"}
    assert doc["parents"] == []
    assert [p["name"] for p in doc["ports"]] == ["a1"]
    assert doc["trace"] is None  # stored without a tracer
    body = (d / "scene.extxyz").read_text(encoding="utf-8")
    assert body == extxyz.to_extxyz(g.scene)


@steps.step
def rich() -> Any:
    """Everything extxyz/bonds.json alone would drop."""

    sc = _scene_of(3)
    a = list(sc.atoms.values())
    a[0].charge, a[0].magmom = -1, 0.5
    a[1].hybridization, a[1].oxidation = "sp2", 2
    a[2].fixed = 3
    a[2].frac = a[2].frac + 1e-12  # below the extxyz 8-digit print
    sc.bonds[0].provenance = "inferred"
    sc.bonds[1].image, sc.bonds[1].kind, sc.bonds[1].order = (1, 0, 0), "aromatic", 1.5
    sc.label_hi = {"C": 40}
    g = pga.geom(
        sc,
        ports={
            "p": {
                "pos": (1.0, 2.0, 3.0),
                "direction": (0.0, 0.0, 1.0),
                "roles": ("join",),
                "atom": "aC1",
                "annotations": {"order": 1},
            }
        },
        meta={"t": (1, 2), "arr": np.arange(3.0), "chiral_index": [3, 3]},
    )
    return dataclasses.replace(g, findings=("chirality.flipped",))


def test_atomic_hit_is_bit_identical_in_everything_extxyz_drops() -> None:

    fresh = rich()
    hit = rich()
    assert CALLS == []  # (rich() does not log) — second call came from the cache
    assert steps.ls("rich")[0]["hits"] == 1
    fs, hs = fresh.scene, hit.scene
    assert list(fs.atoms) == list(hs.atoms)
    for la in fs.atoms:
        fa, ha = fs.atoms[la], hs.atoms[la]
        assert np.array_equal(fa.frac, ha.frac)  # bit for bit, not 1e-8
        assert (fa.element, fa.charge, fa.magmom, fa.hybridization) == (
            ha.element,
            ha.charge,
            ha.magmom,
            ha.hybridization,
        )
        assert (fa.oxidation, fa.fixed, fa.maker) == (ha.oxidation, ha.fixed, ha.maker)
    assert fs.bonds == hs.bonds  # provenance, image, kind, order all equal
    assert np.array_equal(fs.cell.lattice, hs.cell.lattice)
    assert fs.cell.pbc == hs.cell.pbc and fs.label_hi == hs.label_hi
    assert fresh.ports == hit.ports and fresh.findings == hit.findings
    assert hit.meta["t"] == (1, 2) and np.array_equal(hit.meta["arr"], np.arange(3.0))
    assert hit.meta["chiral_index"] == [3, 3]
    assert _text(hit) == _text(fresh)


def test_import_echo_prints_the_steps_digest_line() -> None:
    from precis_se.workspace_import import ImportResult, TraceView

    digest = {"hits": 1, "rebuilt": 3, "first_miss": "decorate#0", "wall_saved_s": 4.25}
    res = ImportResult(
        design_slug="d",
        imported=["p"],
        trace=TraceView(run={"steps": digest}, frames=[]),
    )
    assert "steps: 1 hit · 3 rebuilt (from decorate#0) · saved 4.2s" in res.echo()
    plain = ImportResult(design_slug="d", trace=TraceView(run={}, frames=[]))
    assert "steps:" not in plain.echo()


def test_checkpoint_outlives_a_killed_step_and_is_dropped_on_completion(
    cache: Path,
) -> None:
    """A step body's side file: saved as it goes, read back by a re-run of
    the same key after a kill, gone once the step completes (slice 3: the
    collapse check's resume)."""
    state = {"die": True}
    seen: list[Any] = []

    @steps.step
    def long_job(n: int) -> Any:
        ck = steps.checkpoint()
        assert ck is not None and steps.current_key() == ck.key
        prior = ck.load()
        seen.append(prior)
        for k in range((prior or {}).get("done", 0), n):
            ck.save({"done": k + 1})
            if state["die"] and k == 2:
                raise KeyboardInterrupt
        return pga.geom(_scene_of(3))

    assert steps.checkpoint() is None and steps.current_key() is None
    with pytest.raises(KeyboardInterrupt):
        long_job(5)
    [saved] = list(cache.glob("*/*/checkpoint.json"))
    assert json.loads(saved.read_text(encoding="utf-8")) == {"done": 3}
    assert steps.ls() == []  # a dir with only a checkpoint is no entry
    state["die"] = False
    long_job(5)
    assert seen == [None, {"done": 3}]  # the re-run resumed from step 3
    assert list(cache.glob("*/*/checkpoint.json")) == []
    assert [r["name"] for r in steps.ls()] == ["long_job"]
    long_job(5)  # a hit never enters the body
    assert len(seen) == 2


def test_checkpoint_is_ignored_by_fresh_runs_and_absent_with_the_cache_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got: list[Any] = []
    die = [True]

    @steps.step
    def job(n: int) -> Any:
        ck = steps.checkpoint()
        got.append(None if ck is None else ck.load())
        if die[0] and ck is not None:
            ck.save({"done": n})
            raise RuntimeError("die")
        return pga.geom(_scene_of(3))

    with pytest.raises(RuntimeError):
        job(1)  # leaves checkpoint.json behind
    die[0] = False
    monkeypatch.setenv("PRECIS_STEPS_FRESH", "1")
    job(1)
    assert got == [None, None]  # FRESH: the saved checkpoint is not read
    monkeypatch.delenv("PRECIS_STEPS_FRESH")
    steps.configure("off")
    job(2)
    assert got[-1] is None  # no cache, no checkpoint
