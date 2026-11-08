"""Slice 1c: the Decision-19 one-screen summary (≤40 lines, at least one
ranked edit with a measured negative Δ), the run manifest's shape, and
byte-deterministic run files (Decision 22) — spec §1c's own tests."""

from __future__ import annotations

import dataclasses
import re

import numpy as np
import pytest

from precis.pcb.topoplace import (
    ObjectiveShares,
    TopoPlaceResult,
    assign_pins,
    pair_map,
    runstore,
    topo_place,
)
from precis.pcb.topoplace.summary import ranked_edits, render_summary
from tests.fixtures.pcb.topo_synth import synth_design

W = ObjectiveShares()
_EFFORT = 4000
_SEED = 1


def _run(seed: int = _SEED):
    """One full 1c pipeline on the dense 30-part synthetic board —
    dense enough that a moderate effort leaves crossings for the ranked
    edits to price."""
    d, _inst, _nets = synth_design(
        seed=0, n_parts=30, n_nets=80, n_multinet=4, side=40.0
    )
    result = topo_place(d, objective=W, effort=_EFFORT, seed=seed)
    assignment = assign_pins(d, result.best_pose)
    pm = pair_map(d)
    return d, result, assignment, pm


def _files(d, result, assignment, pm, seed: int = _SEED) -> dict:
    summary = render_summary(
        d,
        result,
        assignment,
        pm,
        objective=W,
        effort=_EFFORT,
        seed=seed,
        slug="synth",
    )
    return runstore.run_files(
        d,
        result,
        assignment,
        pm,
        summary=summary,
        manifest_payload=runstore.manifest(
            d,
            result,
            objective=W,
            effort=_EFFORT,
            board_hash="0" * 16,
            parts=[
                {"refdes": p, "lcsc": None, "source": "script-defined"} for p in d.parts
            ],
            footprints={},
        ),
    )


def test_summary_one_screen_with_ranked_edits() -> None:
    """The summary stays on one screen (≤ 40 lines, Decision 19) and
    names at least one ranked edit with a measured negative crossing
    delta — each edit is priced by re-running, never predicted."""
    d, result, assignment, pm = _run()
    assert result.crossings > 0  # the fixture must leave edits something to cut
    text = render_summary(
        d,
        result,
        assignment,
        pm,
        objective=W,
        effort=_EFFORT,
        seed=_SEED,
        slug="synth",
    )
    lines = text.splitlines()
    assert len(lines) <= 40
    assert "topo-place — synth" in lines[0]
    assert "n/a (slice 2)" in text  # later-slice fields degrade, never lie
    edit_lines = [l for l in lines if re.search(r": -\d+ crossings$", l)]
    assert edit_lines, f"no negative-Δ ranked edit in:\n{text}"


def test_ranked_edits_are_measured_and_sorted() -> None:
    d, result, assignment, pm = _run()
    edits = ranked_edits(
        d, result, assignment, pm, objective=W, effort=_EFFORT, seed=_SEED
    )
    assert edits
    deltas = [e.delta for e in edits]
    assert deltas == sorted(deltas)
    assert deltas[0] < 0


def test_ranked_edits_probe_lighter_when_the_base_run_moved_nothing(
    monkeypatch,
) -> None:
    """gr524239: an all-fixed base run (0 evals) prices its unlock probes
    at effort/100, not effort/10 — a probe moves ONE part, and the full
    figure re-ran a settle per try (~26 s of the taz-leaf summary)."""
    import precis.pcb.topoplace.summary as tps

    d, _inst, _nets = synth_design(seed=0, n_parts=10, n_nets=20, n_fixed=10)
    result = topo_place(d, objective=W, effort=_EFFORT, seed=1)
    assert result.evals == 0
    seen: list[int] = []
    real = tps.topo_place

    def spy(d_, *, objective, effort, seed, **kw):
        seen.append(effort)
        return real(d_, objective=objective, effort=effort, seed=seed, **kw)

    monkeypatch.setattr(tps, "topo_place", spy)
    ranked_edits(
        d,
        result,
        assign_pins(d, result.best_pose),
        pair_map(d),
        objective=W,
        effort=_EFFORT,
        seed=1,
    )
    assert seen, "no unlock probe ran"
    assert all(e == max(1, _EFFORT // 100) for e in seen)


@pytest.mark.nightly(
    reason="64 s at run 42354225792 (me580555, gate-s2): tests >=30 s run on the nightly FULL shape (the gate's soak also deselects -m 'not nightly'), off the 5-minute gate"
)
def test_run_files_byte_deterministic(tmp_path) -> None:
    """Decision 22: same design + same seed → byte-identical run dir."""
    files_a = _files(*_run(seed=7))
    files_b = _files(*_run(seed=7))
    assert files_a.keys() == files_b.keys()
    for name in files_a:
        assert files_a[name] == files_b[name], name
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    written_a = runstore.write_run(files_a, dir_a)
    written_b = runstore.write_run(files_b, dir_b)
    assert written_a == written_b == sorted(files_a)
    for name in written_a:
        assert (dir_a / name).read_bytes() == (dir_b / name).read_bytes(), name
    # a different seed is a different run (the seed actually reaches the
    # solver, not just the manifest field)
    files_c = _files(*_run(seed=8))
    assert files_a["elites.json"] != files_c["elites.json"]


def test_manifest_carries_the_run_provenance() -> None:
    d, result, assignment, pm = _run()
    files = _files(d, result, assignment, pm)
    m = files["manifest.json"]
    assert m["schema"] == runstore.RUN_SCHEMA
    assert m["code_version"] == runstore.CODE_VERSION
    assert m["seed"] == _SEED and m["effort"] == _EFFORT
    assert m["result"]["crossings"] == result.crossings
    assert m["result"]["evals"] == result.evals
    assert {p["refdes"] for p in m["parts"]} == set(d.parts)
    # every file the spec's run dir names exists
    assert set(files) == {
        "manifest.json",
        "elites.json",
        "pairs.json",
        "pins.json",
        "summary.txt",
        "placement.svg",
    }
    assert files["placement.svg"].startswith("<svg")


def test_manifest_run_hash_is_content_identity() -> None:
    """``run_hash`` (td598638 round 2, pcb-intent-api §Recording): the
    run's identity, 16 hex over the manifest fields that determine the
    result — the inputs and the result row minus ``ref``. A manifest
    differing only in the schema tag, the version stamps or what it
    referenced hashes the same (no timestamp/path/hostname/label field
    ever enters it); a changed result value is a different run."""
    d, result, assignment, pm = _run()
    m = _files(d, result, assignment, pm)["manifest.json"]
    assert m["run_hash"] == runstore.manifest_run_hash(m)
    assert len(m["run_hash"]) == 16

    provenance_only = {
        **m,
        "schema": "precis.topo-run/999",
        "code_version": m["code_version"] + 100,
        "wheel_version": "0.0.0",
        "result": {**m["result"], "ref": {"run": "0" * 16, "job": None}},
    }
    assert runstore.manifest_run_hash(provenance_only) == m["run_hash"]

    changed = {
        **m,
        "result": {**m["result"], "crossings": m["result"]["crossings"] + 1},
    }
    assert runstore.manifest_run_hash(changed) != m["run_hash"]

    changed_ref = {
        **m,
        "result": {
            **m["result"],
            "ref_values": {**m["result"]["ref_values"], "ratsnest": -1.0},
        },
    }
    assert runstore.manifest_run_hash(changed_ref) != m["run_hash"]


def test_manifest_run_hash_reads_a_6_manifest() -> None:
    """The /6 reader: a manifest written before ``run_hash`` existed —
    schema ``/6``, ``result.ref`` a bare string — still loads and
    hashes by the same rule (``run_hash`` and ``ref`` never enter the
    hash), so a /7 run can reference a /6 run without a rewrite."""
    d, result, assignment, pm = _run()
    m = _files(d, result, assignment, pm)["manifest.json"]
    v6 = {
        **{k: v for k, v in m.items() if k != "run_hash"},
        "schema": "precis.topo-run/6",
        "result": {**m["result"], "ref": "run:out/earlier"},
    }
    assert runstore.manifest_run_hash(v6) == m["run_hash"]


def test_objective_terms_match_shares() -> None:
    """gr549293: the manifest's objective dict records EVERY
    ``ObjectiveShares`` term — the keys are the dataclass's own field
    list, so a new share can never be dropped again (``measures`` was
    the one the hand-written key list left off)."""
    d, _inst, _nets = synth_design(seed=0, n_parts=4, n_nets=4, n_multinet=0)
    result = TopoPlaceResult(
        best_pose=np.zeros((len(d.parts), 4)),
        best_cost=0.0,
        crossings=0,
        length=0.0,
        evals=0,
        restarts=0,
        seed=_SEED,
        elites=(),
        ladder_scores=(),
    )
    m = runstore.manifest(
        d,
        result,
        objective=W,
        effort=_EFFORT,
        board_hash="0" * 16,
        parts=[],
        footprints={},
    )
    terms = [f.name for f in dataclasses.fields(ObjectiveShares)]
    assert sorted(m["objective"]) == sorted(terms)
    assert m["objective"]["measures"] == float(W.measures)
