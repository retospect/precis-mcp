"""pcb-workspace-code slice 4, offline program end-to-end
(docs/backlog/pcb-workspace-code.md, Slices row 4): ``main.py --place
--route`` on a laptop with no network writes the full §Q3 output set for
the §Q1 board (``examples/boards/usbc_attiny``) — ``--route`` runs the
topo route stage over the anneal's pose so the outputs describe real
copper again (gr513867) — and ``pcb.local`` wraps the same engines the
``pcb_place`` worker runs — no DB, no network, no store.
"""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

import precis_geom.pcb as pcb

ROOT = Path(__file__).resolve().parents[2]
BOARDS = ROOT / "examples" / "boards"

pytestmark = pytest.mark.slow


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _example(name: str) -> pcb.Board:
    return _load(BOARDS / name / "main.py", f"example_{name}").build()


def test_usbc_attiny_end_to_end_writes_every_q3_output(tmp_path: Path) -> None:
    out = pcb.run(
        _example("usbc_attiny"), tmp_path / "out", argv=["--place", "--route"]
    )
    expected = {
        "manifest.json",
        "check.txt",
        "netlist.net",
        "schematic.svg",
        "ratsnest.svg",
        "placement.json",
        "copper.json",
        "drc.txt",
        "gerbers.zip",
        "board.svg",
        "bom.csv",
        "cpl.csv",
    }
    assert expected <= {p.name for p in out.iterdir()}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "pcb" and manifest["schema"] == 1
    placement = json.loads((out / "placement.json").read_text(encoding="utf-8"))
    assert len(placement["poses"]) == 11
    copper = json.loads((out / "copper.json").read_text(encoding="utf-8"))
    # --route routes the anneal's pose through the topo stage (gr513867):
    # copper.json carries the route rows + digest it produced.
    assert copper["routes"] and set(copper) >= {
        "copper",
        "routes",
        "unrouted_reasons",
        "digest",
        "committed_round",
    }
    assert set(copper["digest"]) == {"failed", "constraints"}
    zf = zipfile.ZipFile(out / "gerbers.zip")
    assert {n for n in zf.namelist() if n.endswith((".gbr", ".drl"))}
    assert (out / "drc.txt").read_text(encoding="utf-8").startswith("usbc-attiny")


def _tiny_board() -> pcb.Board:
    """Three two-pin nets on a 30x20 board, one of them F↔B — the
    routable control: nothing crosses, so the honest expectation is
    3/3 realized and 0 DRC errors."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("tiny", stackup=4, outline=pcb.rect(30.0, 20.0))
    f = b.footprint(fp)
    a1 = b.part("A1", footprint=f, pins=["1", "2"]).at(5.0, 5.0)
    a2 = b.part("A2", footprint=f, pins=["1", "2"]).at(15.0, 5.0)
    c1 = b.part("C1", footprint=f, pins=["1", "2"]).at(5.0, 15.0)
    c2 = b.part("C2", footprint=f, pins=["1", "2"]).at(15.0, 15.0, side="bottom")
    e1 = b.part("E1", footprint=f, pins=["1", "2"]).at(25.0, 5.0)
    e2 = b.part("E2", footprint=f, pins=["1", "2"]).at(25.0, 15.0)
    for part in (a1, a2, c1, c2, e1, e2):
        for pin in ("1", "2"):
            part[pin].etype("bidi", fmax_hz=0.0, assume="header pin")
    # gr520545: place/route refuses a net without a current.
    b.net("N1", current=0.01).tie(a1["2"], a2["1"])
    b.net("N2", current=0.01).tie(c1["2"], c2["2"])
    b.net("N3", current=0.01).tie(e1["1"], e2["1"])
    return b


def test_local_route_authored_poses_returns_a_route_result() -> None:
    """gr513867: ``local.route`` is the in-script route call again —
    ``place=False`` routes the authored poses through the topo stage,
    DB-free, and hands back per-net status, geometry counts and the run
    digest (gr513871's produced half). The sanity board's authored pose
    is the router's honest hard case (LR×TB crossing cycles, a known
    gap), so the assertions pin the CONTRACT — every net accounted for,
    the digest mirroring the failed set — plus ``drc_errors == 0``: the
    widen oracle measures the same footprint-fed model ``drc()`` does
    (a rotated authored footprint's pads are not transposed)."""
    rt = pcb.local.route(_example("sanity"), place=False)
    assert rt.ready and rt.slug == "sanity"
    assert len(rt.nets) == 20
    assert sorted(rt.routed + rt.failed + rt.dangling + rt.unrouted) == sorted(rt.nets)
    assert rt.tracks == len(rt.result.tracks)
    assert rt.drc_errors == 0
    assert rt.digest["failed"] == rt.failed
    if rt.failed:
        assert rt.digest["constraints"]
        for c in rt.digest["constraints"]:
            assert c["reason"] and c["count"] >= 1 and c["nets"]


def test_local_route_routes_a_routable_board_clean() -> None:
    """The clean half of the contract: a board with no crossing wires
    routes every net, mints its via and reports an empty digest."""
    rt = pcb.local.route(_tiny_board(), place=False)
    assert rt.ready and rt.slug == "tiny"
    assert rt.routed == ["N1", "N2", "N3"] and rt.failed == []
    assert rt.tracks > 0 and rt.vias == 1 and rt.length_mm > 0
    assert rt.drc_errors == 0
    assert rt.digest == {"failed": [], "constraints": []}


def test_local_route_routes_a_given_placement() -> None:
    """``placement=`` routes the anneal's pose without a second solve —
    the same ``_route_stage``, the pose the caller already paid for."""
    board = _example("sanity")
    placement = pcb.local.place(board, iters=200)
    rt = pcb.local.route(board, placement=placement)
    assert rt.ready and len(rt.nets) == 20
    assert rt.poses == placement.poses


def test_local_route_placement_and_place_false_disagree() -> None:
    placement = pcb.local.place(_example("sanity"), iters=200)
    with pytest.raises(pcb.local.LocalError, match="disagree"):
        pcb.local.route(placement, placement=placement, place=False)


def test_local_route_refusal_surfaces_as_localerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route-stage refusal raises ``LocalError``, not the engine's
    ``BadInput`` — the docstring's contract on the ``place=False`` and
    ``placement=`` paths (``topo_place`` already wraps its own stage
    best-effort, so the ``place=True`` path reports the failure through
    the run instead)."""
    from precis.pcb.topoplace.wires import CorridorCut, CorridorOverflow

    cut = CorridorCut(
        edge=0,
        demand=3.0,
        cap=1.0,
        owners=("U1", "outline"),
        nets=("N1",),
        x=1.0,
        y=2.0,
    )

    def boom(*args: object, **kwargs: object) -> object:
        raise CorridorOverflow((cut,))

    monkeypatch.setattr("precis.pcb.local._route_stage", boom)
    with pytest.raises(pcb.local.LocalError, match="corridor overflow"):
        pcb.local.route(_tiny_board(), place=False)

    placement = pcb.local.place(_tiny_board(), iters=200)
    with pytest.raises(pcb.local.LocalError, match="corridor overflow"):
        pcb.local.route(_tiny_board(), placement=placement)


def test_board_route_is_the_traced_library_call(tmp_path: Path) -> None:
    """gr513867: ``board.route()`` is the script-side entry — one
    ``core`` frame on the trace like every other library call, and a
    ``LocalRoute`` result."""
    from precis_geom import trace as _trace

    board = _tiny_board()
    with _trace.run(work_root=tmp_path) as tr:
        rt = board.route(place=False)
    assert rt.routed
    quals = [f["qual"] for f in tr.dump()["frames"]]
    assert "pcb.Board.route" in quals


def test_local_route_feeds_the_routed_board_to_drc_and_exports(
    tmp_path: Path,
) -> None:
    """A ``LocalRoute`` is a valid input to ``drc``/``copper_json`` —
    the routed copper, not the empty placed set."""
    rt = pcb.local.route(_tiny_board(), place=False)
    findings = pcb.local.drc(rt)
    assert not [f for f in findings if f.severity == "error"]
    copper = pcb.local.copper_json(rt)
    assert copper["copper"] and len(copper["routes"]) == 3
    assert copper["digest"] == rt.digest


def test_topo_place_accepts_pcb_objective(tmp_path: Path) -> None:
    """``pcb.objective(...)`` (pcb-intent-api slice 1) is a plain shares
    mapping ``local.topo_place`` resolves the same way a hand-built dict
    always could — but unlike a partial hand-built dict, an unset term
    lands at 0.0, never the ``ObjectiveShares`` class default, so
    ``pcb.objective(ratsnest=1.0)`` really is all-ratsnest."""
    run = pcb.local.topo_place(
        _example("sanity"),
        objective=pcb.objective(ratsnest=1.0),
        effort=200,
        seed=1,
        out=str(tmp_path / "topo"),
    )
    assert run.poses
    manifest = json.loads((Path(run.out) / "manifest.json").read_text(encoding="utf-8"))
    # pcb-intent-api §Recording: the default ref is "seed" even when an
    # explicit ``pcb.objective(...)`` (not a plain dict) names it.
    assert manifest["result"]["ref"] == "seed"


def test_objective_shares_mapping_fills_unset_terms_with_zero() -> None:
    """A partial mapping names only what it means: unset terms are 0.0
    (before the share normalisation), not the 0.8/0.2 class defaults;
    an unknown key raises; ``None`` is the preset lookup unchanged."""
    from precis.pcb import topoplace as tp
    from precis.pcb.local import LocalError, _objective_shares

    only = _objective_shares({"ratsnest": 1.0, "margin": 1.0}, "compact")
    assert (only.ratsnest, only.margin, only.crossings) == (0.5, 0.5, 0.0)
    with pytest.raises(LocalError, match="unknown objective term"):
        _objective_shares({"wirelength": 1.0}, "compact")
    assert _objective_shares(None, "compact") is tp.PRESETS["compact"]


def test_objective_ref_values_resolves_the_named_run() -> None:
    """``_objective_ref_values`` (pcb-intent-api §Weights, ``ref=<run>``):
    ``ref="seed"`` (a plain mapping's implicit default, or an explicit
    one) is ``None`` — self-measure, today's behaviour; a
    ``pcb.objective``-built mapping's ``.ref`` resolves against the run
    object it names; a ref naming something without ``ref_values``
    raises rather than silently measuring from the seed."""
    from precis.pcb.local import LocalError, _objective_ref_values

    assert _objective_ref_values({"ratsnest": 1.0}) is None
    assert _objective_ref_values(pcb.objective(ratsnest=1.0)) is None

    class _Run:
        ref_values = {"ratsnest": 9.0, "crossings": 3.0}

    assert (
        _objective_ref_values(pcb.objective(ratsnest=1.0, ref=_Run()))
        == _Run.ref_values
    )

    class _NoRefValues:
        pass

    class _HandBuiltObjective(dict):
        """``pcb.objective`` itself refuses a ``ref=`` without
        ``ref_values`` (``test_objective_shares``) — this stands in for
        a mapping that reached ``_objective_ref_values`` some other way
        (hand-built, not through the builder), so the defensive raise
        here has a caller to catch."""

        ref = _NoRefValues()

    with pytest.raises(LocalError, match="ref="):
        _objective_ref_values(_HandBuiltObjective(ratsnest=1.0))


def test_objective_ref_records_seed_or_the_referenced_run() -> None:
    """``_objective_ref`` (pcb-intent-api §Recording, td598638 round 2):
    what the manifest's ``result.ref`` records for ``objective=``'s
    ``ref=`` argument — ``"seed"`` for a plain mapping or an explicit
    ``ref="seed"``; for a referenced run the identity dict
    ``{"run", "job", "label"?}`` where ``run`` is the referenced
    manifest's content hash (16 hex), ``job`` the ``jo<N>`` handle the
    run records (``None`` for a laptop run) and ``label`` present only
    when the run carries one."""
    from precis.pcb.local import _objective_ref

    assert _objective_ref({"ratsnest": 1.0}) == "seed"
    assert _objective_ref(pcb.objective(ratsnest=1.0)) == "seed"

    class _Run:
        ref_values = {"ratsnest": 1.0}
        files = {"manifest.json": {"run_hash": "a" * 16}}

    assert _objective_ref(pcb.objective(ratsnest=1.0, ref=_Run())) == {
        "run": "a" * 16,
        "job": None,
    }

    class _JobRun(_Run):
        job = "jo42"
        label = "nightly"

    assert _objective_ref(pcb.objective(ratsnest=1.0, ref=_JobRun())) == {
        "run": "a" * 16,
        "job": "jo42",
        "label": "nightly",
    }

    class _JobIdRun(_Run):
        job = 7  # an id records as its jo<N> handle

    assert _objective_ref(pcb.objective(ratsnest=1.0, ref=_JobIdRun()))["job"] == "jo7"

    class _ManifestJobRun:
        """A ``pcb_place``-produced run reached only by its stored
        manifest (gr599356): the ``jo<N>``/``label`` the /8 manifest
        itself stamps resolve where no ``.job``/``.slug`` attribute
        exists."""

        ref_values = {"ratsnest": 1.0}
        files = {
            "manifest.json": {
                "run_hash": "b" * 16,
                "job": "jo9",
                "label": "night",
            }
        }

    assert _objective_ref(pcb.objective(ratsnest=1.0, ref=_ManifestJobRun())) == {
        "run": "b" * 16,
        "job": "jo9",
        "label": "night",
    }

    class _BareRun:
        """A manifest-less stand-in (the /6 ``run:<TypeName>`` fallback):
        identifies by the only content it carries — ``ref_values``."""

        ref_values = {"ratsnest": 1.0}

    bare = _objective_ref(pcb.objective(ratsnest=1.0, ref=_BareRun()))
    assert set(bare) == {"run", "job"}
    assert bare["job"] is None and len(bare["run"]) == 16

    class _BareRun2:
        ref_values = {"ratsnest": 2.0}

    assert (
        _objective_ref(pcb.objective(ratsnest=1.0, ref=_BareRun2()))["run"]
        != bare["run"]
    )


def test_objective_ref_reads_a_6_manifest_run() -> None:
    """The /6 reader (td598638 round 2): a referenced run whose manifest
    is still schema ``/6`` — ``result.ref`` a bare string, no
    ``run_hash`` key — loads untouched and identifies by the content
    hash :func:`runstore.manifest_run_hash` computes from it (the /6
    manifest supplies no ``run_hash`` to reuse)."""
    from precis.pcb.local import _objective_ref
    from precis.pcb.topoplace import runstore

    manifest_v6 = {
        "schema": "precis.topo-run/6",
        "seed": 1,
        "effort": 200,
        "result": {
            "crossings": 4,
            "ref_values": {"ratsnest": 1.0},
            "ref": "run:out/earlier",
        },
    }

    class _OldRun:
        ref_values = {"ratsnest": 1.0}
        files = {"manifest.json": manifest_v6}

    ref = _objective_ref(pcb.objective(ratsnest=1.0, ref=_OldRun()))
    assert ref == {
        "run": runstore.manifest_run_hash(manifest_v6),
        "job": None,
    }


def test_topo_place_ref_run_reuses_its_reference_values(tmp_path: Path) -> None:
    """End to end (pcb-intent-api §Weights slice 1 remainder):
    ``pcb.objective(ref=<an earlier run>)`` makes THIS run's R_i the
    named run's recorded vector, not its own seed-pose measurement —
    exactly 42.0 for every term could never arise from self-measuring a
    real board, so a match proves the override actually reached the
    placer, not just the manifest."""

    class _FakeRun:
        ref_values = dict.fromkeys(pcb.OBJECTIVE_TERMS, 42.0)

    run = pcb.local.topo_place(
        _example("sanity"),
        objective=pcb.objective(ratsnest=0.8, crossings=0.2, ref=_FakeRun()),
        effort=200,
        seed=1,
        out=str(tmp_path / "topo"),
    )
    assert run.ref_values == _FakeRun.ref_values
    manifest = json.loads((Path(run.out) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["result"]["ref_values"] == _FakeRun.ref_values
    # pcb-intent-api §Recording round 2: a manifest-less stand-in still
    # records a content identity — 16 hex over its ref_values — not a
    # machine-local path.
    ref = manifest["result"]["ref"]
    assert ref["job"] is None and "label" not in ref
    assert len(ref["run"]) == 16 and ref["run"] != "seed"


def test_topo_place_ref_run_records_the_referenced_manifest(
    tmp_path: Path,
) -> None:
    """End to end (td598638 round 2): ``ref=<a real earlier run>``
    records ``result.ref.run`` as the referenced manifest's
    ``run_hash`` — the identity that survives a rerun on another
    machine, where the /6 ``run:<out dir>`` path did not — with
    ``job`` null (a laptop run) and the referenced run's label."""
    from precis.pcb.topoplace import runstore

    earlier = pcb.local.topo_place(
        _example("sanity"), effort=200, seed=1, out=str(tmp_path / "earlier")
    )
    run = pcb.local.topo_place(
        _example("sanity"),
        objective=pcb.objective(ratsnest=0.8, crossings=0.2, ref=earlier),
        effort=200,
        seed=1,
        out=str(tmp_path / "topo"),
    )
    manifest = json.loads((Path(run.out) / "manifest.json").read_text(encoding="utf-8"))
    earlier_manifest = earlier.files["manifest.json"]
    assert manifest["result"]["ref"] == {
        "run": earlier_manifest["run_hash"],
        "job": None,
        "label": earlier.slug,
    }
    # the recorded identity IS the referenced manifest's content hash
    assert runstore.manifest_run_hash(earlier_manifest) == earlier_manifest["run_hash"]


def test_board_default_stackup_is_four_routable_layers() -> None:
    """gr514459: ``Board()`` emits no stackup key and resolves to the
    semantic default — 4 routable copper layers
    (``DEFAULT_STACKUP``); ``stackup=4`` emits the same names verbatim."""
    from precis.pcb import local as kernel
    from precis.pcb.ir import layer_is_routable

    bare = kernel._stackup(pcb.Board("bare").to_batch())
    assert [e["name"] for e in bare] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert all(layer_is_routable(e) for e in bare)

    counted = kernel._stackup(pcb.Board("four", stackup=4).to_batch())
    assert [e["name"] for e in counted] == [e["name"] for e in bare]
    assert all(layer_is_routable(e) for e in counted)


def test_local_kernel_miss_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    def fake_module(name: str) -> ModuleType:
        raise ImportError("No module named 'precis.pcb.local'")

    monkeypatch.setattr(importlib, "import_module", fake_module)
    with pytest.raises(ImportError, match="pcb-kernel"):
        _ = pcb.local


def test_local_route_widen_oracle_sees_mounting_hole_drills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The widen oracle builds its model the way the pcb_place job does
    (``_copper_model``), so mounting-hole drills are in it — a rail is
    never widened toward a hole the job would refuse (gr513867)."""
    from precis.pcb import drc as pcb_drc

    models: list[dict] = []
    real = pcb_drc.run_geometric_drc

    def spy(model, **kw):
        models.append(model)
        return real(model, **kw)

    monkeypatch.setattr(pcb_drc, "run_geometric_drc", spy)
    b = _tiny_board()
    b.hole(28.0, 10.0, drill=2.2, ring=4.0)
    pcb.local.route(b, place=False)
    assert models, "the widen oracle never ran"
    for m in models:
        assert any(
            abs(d["x"] - 28.0) < 1e-6
            and abs(d["y"] - 10.0) < 1e-6
            and abs(d["dia_mm"] - 2.2) < 1e-6
            for d in m["drills"]
        )
