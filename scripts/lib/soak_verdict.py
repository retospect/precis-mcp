"""Soak verdict read (me580555, review round 1): the `soak*` check-runs
on one sha, paginated, collapsed to the latest attempt per leg name.

`round cut`/`open` read the last gated main sha's soak verdict and refuse
on red — the soak's continue-on-error legs keep the run conclusion green,
so the check-runs are the only place their red is surfaced. The dedicated
`verdict-soak` job (check.yml) turns a red soak matrix into a failing
check-run — named WITHOUT the `soak` prefix (ci_watch counts any `soak*`
job as a soak leg and would gripe it and pad the green streak on soakless
runs), so readers take it by ``VERDICT_JOB``, not the prefix. It is itself
not a blocking leg (``last_gated_main_sha.LEG_PREFIXES`` never matches it).

Rows are ``[name, status, conclusion, html_url]`` parsed from the
``--jq @tsv`` output; the run id comes out of html_url's
``/actions/runs/<id>`` tail so a re-run leg supersedes its earlier
attempt (check-runs list every attempt).
"""

from __future__ import annotations

import re
from pathlib import Path

#: Conclusions that read as a soak failure; skipped/neutral do not.
_BAD = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
_OK = {"success", "skipped", "neutral"}
_RUN_ID = re.compile(r"/actions/runs/(\d+)")
_JQ = r'.check_runs[] | [.name, .status, (.conclusion // ""), .html_url] | @tsv'
#: The dedicated verdict-soak job's check-run name (check.yml). The ONE
#: constant every soak reader shares — it deliberately lacks the `soak`
#: prefix (see the module doc).
VERDICT_JOB = "verdict-soak"


def soak_legs(run_command, sha: str, cwd: Path) -> list[list[str]] | None:
    """Every check-run row on ``sha``, all pages — None when gh fails.

    ``run_command`` is ``git_network.run_command`` (injected for tests):
    ``(argv, cwd=, timeout=) -> CompletedProcess``. One page is 100 rows;
    the loop stops at the first short page. The ``-f`` params keep the
    URL argument ending in ``/check-runs``.
    """
    rows: list[list[str]] = []
    page = 1
    while True:
        cp = run_command(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs",
                # gh api defaults to POST once -f/-F is present; --method
                # GET moves the params to the query string.
                "--method",
                "GET",
                "-f",
                "per_page=100",
                "-f",
                f"page={page}",
                "--jq",
                _JQ,
            ],
            cwd=cwd,
            timeout=60,
        )
        if cp.returncode != 0:
            return None
        got = [ln.split("\t") for ln in cp.stdout.splitlines() if ln.count("\t") == 3]
        rows.extend(got)
        if len(got) < 100:
            return rows
        page += 1


def _run_id(row: list[str]) -> int:
    m = _RUN_ID.search(row[3])
    return int(m.group(1)) if m else 0


def latest(rows: list[list[str]]) -> list[list[str]]:
    """The newest row per soak leg name — the `soak (PY, K)` matrix legs
    plus ``VERDICT_JOB`` (by run id; ties keep the last)."""
    out: dict[str, list[str]] = {}
    for row in rows:
        if not (row[0].startswith("soak") or row[0] == VERDICT_JOB):
            continue
        prev = out.get(row[0])
        if prev is None or _run_id(row) >= _run_id(prev):
            out[row[0]] = row
    return list(out.values())


def classify(rows: list[list[str]]) -> str:
    """``red`` a latest leg completed non-OK; ``pending`` none red but some
    leg not completed; ``green`` every latest leg completed OK; ``none``
    no soak rows at all. A skipped ``VERDICT_JOB`` leg is dropped first:
    the job skipped means the soak never ran, so it is no verdict."""
    legs = [r for r in latest(rows) if not (r[0] == VERDICT_JOB and r[2] == "skipped")]
    if not legs:
        return "none"
    for r in legs:
        if r[1] == "completed" and r[2] in _BAD:
            return "red"
    if any(r[1] != "completed" or r[2] not in _OK for r in legs):
        return "pending"
    return "green"


def red_jobs(rows: list[list[str]]) -> tuple[list[str], str]:
    """``(failing leg names, run_id)`` for a red verdict."""
    bad = [r for r in latest(rows) if r[1] == "completed" and r[2] in _BAD]
    run_id = str(_run_id(bad[0])) if bad else ""
    return [r[0] for r in bad], run_id
