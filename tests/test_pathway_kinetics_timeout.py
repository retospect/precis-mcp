"""``runner.run_kinetics_subprocess`` — the wall-clock ceiling on the
aggregate's microkinetics solve.

Context: ``autocatpath_aggregate`` job 449981 was claimed four times between
2026-09-25 18:00Z and 2026-09-26 07:25Z, wrote its "combining N seed
partial(s)" chunk each time and nothing after, and grew its worker to ~117 GB.
Replayed against its real inputs on the same engine version, the whole job
measures 33s. The cause is not established
(``docs/backlog/autocatpath-aggregate-ran-11h-on-a-33s-job.md``); what IS
established is that a diagnostic the code itself calls "never load-bearing"
had no ceiling and its ``try/except`` could not interrupt a hang.

These tests stub the child via ``_child_cmd`` — the same seam
``tests/test_pathway_plugin.py`` uses for the seed subprocess — so they
exercise the real subprocess/timeout/envelope plumbing without paying for an
engine run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

from precis_pathway import runner
from precis_pathway.types import PathwayArtifact

# Hangs forever. The point of the whole change: only a separate process can
# be killed out of this, which is why a thread would not do.
_STUB_HANGS = """\
import time
while True:
    time.sleep(0.05)
"""

_STUB_OK = """\
import json, sys
out_path = sys.argv[2]
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({"kinetics": {"tof": 1.25e-4, "product": "NH3"}}, fh)
"""

_STUB_REPORTS_ERROR = """\
import json, sys
out_path = sys.argv[2]
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({"kinetics_error": "engine 0.13.0 lacks kinetics"}, fh)
"""

# Dies without writing an envelope at all — an OOM kill, a segfault in the
# engine, an import that blows up. There is no result file to read.
_STUB_DIES_SILENTLY = """\
import sys
sys.stderr.write("Killed: out of memory\\n")
sys.exit(137)
"""


def _artifact() -> PathwayArtifact:
    """Only the keys the call under test reads.

    ``run_kinetics_subprocess`` touches ``results_json``'s nodes/edges/score
    and nothing else, so filling in the other seven ``PathwayArtifact`` fields
    would be noise that hides which inputs actually matter.
    """
    return cast(
        "PathwayArtifact",
        {
            "config": {"name": "no_to_nh3_pd"},
            "results_json": {
                "nodes": {"s0": {}},
                "edges": [],
                "score": {"activity": {"span_eV": 1.1}},
            },
        },
    )


def _patch_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    script = tmp_path / "stub.py"
    script.write_text(text, encoding="utf-8")
    monkeypatch.setattr(
        runner, "_child_cmd", lambda req, out: [sys.executable, str(script), req, out]
    )


def test_a_hanging_solve_is_killed_and_lands_as_kinetics_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The defect this exists for: an unbounded solve holding the worker."""
    _patch_child(monkeypatch, tmp_path, _STUB_HANGS)
    art = _artifact()

    runner.run_kinetics_subprocess(art["config"], art, timeout=2)

    err = art["results_json"]["kinetics_error"]
    assert "timed out after 2s" in err
    assert runner._KINETICS_TIMEOUT_ENV in err, "the message must name its own knob"
    assert "kinetics" not in art["results_json"]


def test_the_aggregate_survives_a_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kinetics is a bonus riding on the aggregate, never load-bearing — a
    timeout must not raise, so the aggregate still persists."""
    _patch_child(monkeypatch, tmp_path, _STUB_HANGS)
    art = _artifact()

    runner.run_kinetics_subprocess(art["config"], art, timeout=2)

    assert art["results_json"]["nodes"] == {"s0": {}}, "artifact left intact"


def test_a_successful_solve_is_folded_onto_results_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_child(monkeypatch, tmp_path, _STUB_OK)
    art = _artifact()

    runner.run_kinetics_subprocess(art["config"], art, timeout=30)

    assert art["results_json"]["kinetics"] == {"tof": 1.25e-4, "product": "NH3"}
    assert "kinetics_error" not in art["results_json"]


def test_a_child_reported_error_is_passed_through_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An engine predating the kinetics module must read the same through the
    subprocess as it did in-process — the contract did not change."""
    _patch_child(monkeypatch, tmp_path, _STUB_REPORTS_ERROR)
    art = _artifact()

    runner.run_kinetics_subprocess(art["config"], art, timeout=30)

    assert art["results_json"]["kinetics_error"] == "engine 0.13.0 lacks kinetics"
    assert "kinetics" not in art["results_json"]


def test_a_child_that_dies_without_an_envelope_carries_its_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No result file at all. The exit code alone is not a diagnosis, so the
    stderr tail has to survive — otherwise an OOM-killed child is
    indistinguishable from a clean 'no kinetics'."""
    _patch_child(monkeypatch, tmp_path, _STUB_DIES_SILENTLY)
    art = _artifact()

    runner.run_kinetics_subprocess(art["config"], art, timeout=30)

    err = art["results_json"]["kinetics_error"]
    assert "137" in err
    assert "out of memory" in err


def test_timeout_zero_runs_in_process_with_no_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch restores the pre-change behaviour exactly — no child,
    so a monkeypatched in-process engine is still reachable."""
    calls: list[str] = []

    def _fake_run_kinetics(config: dict, artifact: dict) -> None:
        calls.append(config["name"])
        artifact["results_json"]["kinetics"] = {"tof": 9.0}

    monkeypatch.setattr(runner, "run_kinetics", _fake_run_kinetics)
    monkeypatch.setattr(
        runner, "_child_cmd", lambda *_a: pytest.fail("must not spawn a child")
    )
    art = _artifact()

    runner.run_kinetics_subprocess(art["config"], art, timeout=0)

    assert calls == ["no_to_nh3_pd"]
    assert art["results_json"]["kinetics"] == {"tof": 9.0}


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("", runner._DEFAULT_KINETICS_TIMEOUT_S),
        ("120", 120),
        ("0", 0),
        ("not-a-number", runner._DEFAULT_KINETICS_TIMEOUT_S),
        ("-5", 0),
    ],
)
def test_the_ceiling_reads_its_env_knob(
    monkeypatch: pytest.MonkeyPatch, env: str, expected: int
) -> None:
    monkeypatch.setenv(runner._KINETICS_TIMEOUT_ENV, env)
    assert runner._kinetics_timeout_s() == expected


def test_the_default_ceiling_is_far_above_the_measured_cost() -> None:
    """29s measured on job 449981's real inputs (engine 0.22.0). The default
    has to leave room for a slow-but-healthy solve, or this guard starts
    discarding good results instead of catching bad ones."""
    assert runner._DEFAULT_KINETICS_TIMEOUT_S >= 29 * 10


def test_the_aggregate_job_calls_the_bounded_form() -> None:
    """The whole point is that the JOB stops calling the unbounded one."""
    import inspect

    from precis_pathway import aggregate_job

    src = inspect.getsource(aggregate_job._dispatch)
    assert "run_kinetics_subprocess(" in src
    assert "runner.run_kinetics(" not in src
