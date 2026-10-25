"""Slice 1c: the Decision-19 one-screen summary (≤40 lines, at least one
ranked edit with a measured negative Δ), the run manifest's shape, and
byte-deterministic run files (Decision 22) — spec §1c's own tests."""

from __future__ import annotations

import re

from precis.pcb.topoplace import (
    ObjectiveShares,
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
