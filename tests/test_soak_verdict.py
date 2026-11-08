"""``scripts/lib/soak_verdict.py`` — the check-runs soak reader
``round cut``/``open`` and ``release`` share (me580555). Row shape is
``[name, status, conclusion, html_url]``; the workflow-run id comes off
html_url's ``/actions/runs/<id>`` tail so a re-run supersedes.

The skipped-matrix half of the contract — the ``${{``-named placeholder
check-run and ``classify``'s ``skipped`` verdict (gr586293, td599737
review) — is pending in ``docs/backlog/soak-reader-skipped-verdict.md``:
``scripts/lib/**`` is deny-write on every slice lane. These tests pin
only the undisputed contract that fix must not regress.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests._script import load_script

REPO = Path(__file__).resolve().parents[1]
SV: ModuleType = load_script(
    "soak_verdict", REPO / "scripts" / "lib" / "soak_verdict.py"
)

URL = "https://ci.invalid/o/r/actions/runs"


def _row(name: str, status: str, conclusion: str, run: int = 1) -> list[str]:
    return [name, status, conclusion, f"{URL}/{run}"]


def test_latest_keeps_the_newest_attempt_per_leg_name() -> None:
    rows = [
        _row("soak (3.13, 1)", "completed", "failure", run=10),
        _row("soak (3.13, 2)", "completed", "success", run=10),
        _row("soak (3.13, 1)", "completed", "success", run=11),
    ]
    out = SV.latest(rows)
    assert {r[0] for r in out} == {"soak (3.13, 1)", "soak (3.13, 2)"}
    assert next(r for r in out if r[0] == "soak (3.13, 1)")[2] == "success"


def test_latest_tie_keeps_the_last_row() -> None:
    rows = [
        _row("soak (3.13, 1)", "completed", "failure", run=7),
        _row("soak (3.13, 1)", "completed", "success", run=7),
    ]
    assert [r[2] for r in SV.latest(rows)] == ["success"]


def test_latest_keeps_verdict_soak_and_drops_unrelated_names() -> None:
    rows = [
        _row("lint", "completed", "failure", run=3),
        _row("test-linux (3.13, 1)", "completed", "success", run=3),
        _row("gate-fast (3.13, 1)", "completed", "success", run=3),
        _row("verdict-soak", "completed", "success", run=3),
    ]
    assert [r[0] for r in SV.latest(rows)] == ["verdict-soak"]


@pytest.mark.parametrize(
    ("rows", "verdict"),
    [
        ([], "none"),
        ([_row("lint", "completed", "failure")], "none"),
        ([_row("soak (3.13, 1)", "completed", "failure")], "red"),
        ([_row("soak (3.13, 1)", "completed", "timed_out")], "red"),
        ([_row("soak (3.13, 1)", "completed", "cancelled")], "red"),
        ([_row("verdict-soak", "completed", "failure")], "red"),
        ([_row("soak (3.13, 1)", "in_progress", "")], "pending"),
        ([_row("soak (3.13, 1)", "completed", "")], "pending"),
        ([_row("soak (3.13, 1)", "completed", "success")], "green"),
        (
            [
                _row("soak (3.13, 1)", "completed", "success"),
                _row("soak (3.13, 2)", "completed", "skipped"),
            ],
            "green",
        ),
        # A skipped verdict-soak beside a red leg stays red — the skip
        # drops only the verdict job, never the legs' verdict.
        (
            [
                _row("verdict-soak", "completed", "skipped"),
                _row("soak (3.13, 1)", "completed", "failure"),
            ],
            "red",
        ),
    ],
    ids=[
        "empty",
        "no-soak-rows",
        "failure",
        "timed-out",
        "cancelled",
        "verdict-job-failure",
        "in-progress",
        "no-conclusion",
        "green",
        "success-plus-skipped-leg",
        "skipped-verdict-red-leg",
    ],
)
def test_classify(rows: list[list[str]], verdict: str) -> None:
    assert SV.classify(rows) == verdict


def test_red_jobs_names_the_bad_legs_and_their_run() -> None:
    rows = [
        _row("soak (3.13, 1)", "completed", "success", run=9),
        _row("soak (3.13, 2)", "completed", "failure", run=9),
        _row("soak (3.13, 3)", "completed", "cancelled", run=9),
        _row("soak (3.13, 4)", "completed", "skipped", run=9),
    ]
    jobs, run_id = SV.red_jobs(rows)
    assert jobs == ["soak (3.13, 2)", "soak (3.13, 3)"]
    assert run_id == "9"


def test_red_jobs_empty_when_nothing_bad() -> None:
    assert SV.red_jobs([_row("soak (3.13, 1)", "completed", "success")]) == (
        [],
        "",
    )


def test_soak_legs_paginates_until_a_short_page() -> None:
    calls: list[list[str]] = []

    def run_command(argv: list[str], **_kw: Any) -> Any:
        calls.append(list(argv))
        if "page=1" in argv:
            body = "".join(
                f"soak (3.13, {i})\tcompleted\tsuccess\t{URL}/9\n" for i in range(100)
            )
            return SimpleNamespace(returncode=0, stdout=body, stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=f"verdict-soak\tcompleted\tsuccess\t{URL}/9\n",
            stderr="",
        )

    rows = SV.soak_legs(run_command, "a" * 40, REPO)
    assert rows is not None
    assert len(rows) == 101
    assert sum("page=2" in c for c in calls) == 1


def test_soak_legs_none_when_gh_fails() -> None:
    cp = SimpleNamespace(returncode=1, stdout="", stderr="denied")
    assert SV.soak_legs(lambda *a, **k: cp, "a" * 40, REPO) is None
