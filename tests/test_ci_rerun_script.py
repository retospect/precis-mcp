"""``scripts/ci-rerun``'s own argv and refusal paths (gr588028 review,
F3): the script is the seam the ``ci_rerun`` job fakes — these run the
REAL functions with ``subprocess.run`` monkeypatched, so a wrong gh
command line or a missing id check fails here. No real gh is ever
executed."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._script import load_script

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci-rerun"
ci_rerun = load_script("ci_rerun_script", _SCRIPT)


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``subprocess.run`` faked: ``calls`` records every argv + kwargs;
    ``results`` is the queue of ``(rc, stdout, stderr)`` to hand back."""
    state: dict[str, Any] = {"calls": [], "results": []}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[Any]:
        state["calls"].append((list(argv), kw))
        rc, out, err = state["results"].pop(0) if state["results"] else (0, "", "")
        return subprocess.CompletedProcess(argv, rc, out, err)

    monkeypatch.setattr(subprocess, "run", run)
    return state


def test_rerun_argv_is_gh_run_rerun_failed(gh: dict[str, Any], capsys: Any) -> None:
    """List argv, no shell: the pre-rerun baseline read (a ``gh run
    view``, a read), then exactly ``gh run rerun <id> --failed`` — the
    only write. The baseline prints as ``ci-rerun: attempt=<n>``."""
    gh["results"].extend(
        [
            (0, '{"attempt": 1, "updatedAt": "2026-11-08T00:00:00Z"}', ""),
            (0, "", ""),
        ]
    )
    assert ci_rerun.rerun("31") == 0
    assert [c[0] for c in gh["calls"]] == [
        ["gh", "run", "view", "31", "--json", "attempt,updatedAt"],
        ["gh", "run", "rerun", "31", "--failed"],
    ]
    assert all(isinstance(a, list) and not kw.get("shell") for a, kw in gh["calls"])
    assert "ci-rerun: attempt=1" in capsys.readouterr().out


def test_rerun_still_fires_when_the_attempt_read_fails(
    gh: dict[str, Any], capsys: Any
) -> None:
    """A failed baseline read costs the baseline only — the rerun
    still fires and no attempt line is printed."""
    gh["results"].extend([(1, "", "boom"), (0, "", "")])
    assert ci_rerun.rerun("31") == 0
    assert [c[0] for c in gh["calls"]] == [
        ["gh", "run", "view", "31", "--json", "attempt,updatedAt"],
        ["gh", "run", "rerun", "31", "--failed"],
    ]
    assert "attempt=" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad", ["31; rm -rf /", "31 --foo", "-1", "", "0x1f", "abc", " 31"]
)
def test_rerun_rejects_a_non_numeric_id_before_any_gh_call(
    gh: dict[str, Any], bad: str, capsys: Any
) -> None:
    """A non-numeric or empty id is refused by ``rerun()`` itself —
    BEFORE any subprocess call, exit 2 (bad usage)."""
    assert ci_rerun.rerun(bad) == 2
    assert gh["calls"] == []
    assert "ci-rerun:" in capsys.readouterr().err


def test_main_rejects_a_bad_run_id_at_the_cli(gh: dict[str, Any]) -> None:
    """``main``'s argparse path refuses the same way — SystemExit 2."""
    with pytest.raises(SystemExit) as exc:
        ci_rerun.main(["31 --foo"])
    assert exc.value.code == 2
    assert gh["calls"] == []


def test_probe_prints_status_and_the_permissions_header_only(
    gh: dict[str, Any], capsys: Any
) -> None:
    """``--probe`` POSTs run 0's rerun-failed endpoint (404 — nothing
    fires) and prints the status + the one permissions header."""
    gh["results"].append(
        (
            0,
            b"HTTP/2 404\nx-accepted-github-permissions: actions=write\n"
            b"content-type: application/json\n\n{}\n",
            b"",
        )
    )
    assert ci_rerun.probe() == 0
    argv, _kw = gh["calls"][0]
    assert argv == [
        "gh",
        "api",
        "-i",
        "--method",
        "POST",
        "repos/{owner}/{repo}/actions/runs/0/rerun-failed",
    ]
    assert capsys.readouterr().out == (
        "probe: status=404 accepted-permissions=actions=write run=0\n"
    )


def test_probe_filters_the_header_through_the_allowed_charset(
    gh: dict[str, Any], capsys: Any
) -> None:
    """A header value outside the printable set reports ``unprintable``
    — the probe never echoes bytes it was not built to read."""
    gh["results"].append(
        (0, b"HTTP/2 404\nx-accepted-github-permissions: a\tb\x01c\n\n", b"")
    )
    assert ci_rerun.probe() == 0
    out = capsys.readouterr().out
    assert "accepted-permissions=unprintable" in out
    assert "\x01" not in out


def test_probe_reports_absent_when_the_header_is_missing(
    gh: dict[str, Any], capsys: Any
) -> None:
    gh["results"].append((0, b"HTTP/2 404\ncontent-type: application/json\n\n", b""))
    assert ci_rerun.probe() == 0
    assert "accepted-permissions=absent" in capsys.readouterr().out


def test_a_403_fails_with_the_ci_rerun_prefixed_line(
    gh: dict[str, Any], capsys: Any
) -> None:
    """A GitHub auth refusal exits 1 with the ``ci-rerun:`` prefixed
    stderr line the job's auth check scans."""
    gh["results"].extend(
        [
            (0, '{"attempt": 1}', ""),
            (1, "", "gh: Resource not accessible by integration (HTTP 403)"),
        ]
    )
    assert ci_rerun.rerun("31") == 1
    err = capsys.readouterr().err
    assert (
        "ci-rerun: gh run rerun 31 --failed failed (1): "
        "gh: Resource not accessible by integration (HTTP 403)" in err
    )
