#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""scripts/lib/red_carried.py — is a red remote gate red only on tests its
base already fails?

``scripts/ship --remote`` calls this when its ``ci/<branch>`` check run ends
red (gr739541, ruled by Reto 2026-11-20). main collects red tests between
releases — the quick lane runs no pytest — and a harness diff's gate runs
on main + diff, so it went red on tests the diff never touched. The gate
lands anyway, with a ``Gate: red-carried <ids>`` trailer, if and only if
all of these hold:

1. The run is red on tests alone: every job that is not green is a
   ``gate-fast`` shard or a ``soak`` leg that failed (plus ``verdict-soak``,
   the soak legs' roll-up), and each uploaded its junit in the run's
   current attempt. The junit is written when pytest ends, so every file of
   that shard has a verdict.
2. The rest of the suite has a verdict on this tree too. A red soak leg
   means gate-fast was green and the soak ran everything. A red gate-fast
   skips the soak (check.yml), so that case passes only when the diff
   touches one of the selector's FULL triggers — gate-fast then ran the
   whole suite — or when ``PRECIS_RED_CARRIED_SOAKLESS=1`` accepts
   gate-fast's selection as the verdict (:data:`SOAKLESS_OK`).
3. No failing test crashed its worker: under ``--max-worker-restart=0``
   that stops the shard at once, and the tests after it never ran.
4. No failing id sits in a file the diff changes, and the diff does not
   change this judge.
5. Every failing id is also red in the reference run: the check run with a
   test verdict on the commit closest behind the gate's base, at most
   ``PRECIS_RED_CARRIED_MAX_BEHIND`` (50) commits back. A main push is
   never one (it runs no shards); a release cut, a nightly or a landed
   gate is.

Anything else is a refusal: a lint or type red, a cancelled or timed-out
shard, a failure junit cannot place in a file, an id the reference run does
not carry, a closest run that is green or names no ids, more carried ids
than ``PRECIS_RED_CARRIED_MAX`` (25), evidence gh cannot read.

A reference run still in flight is read as its shards upload — an id red
in a finished shard is red whatever the rest does — and waited for up to
``PRECIS_RED_CARRIED_WAIT_MIN`` (30) while ids are missing.

    red_carried.py --run <gate run id> --head <gate sha> --base <base sha>

Exit 0: carried; stdout is the trailer value (``<id> <id> …; base run
<id>``). Exit 1: not carried or undecidable; stderr says why. Exit 2: bad
usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

_SCRIPTS = Path(__file__).resolve().parent.parent
#: The junit readers the release gate's failed-first plan already uses.
_FF = runpy.run_path(str(_SCRIPTS / "ci-failed-first"))

#: gh fills ``{owner}/{repo}`` from the checkout's remote.
REPO = "{owner}/{repo}"
#: A test shard job of the gate shape: ``gate-fast (3.13, 7)`` or the soak
#: leg ``soak (3.13, 7)``; the groups are the kind and the shard.
_SHARD_JOB = re.compile(r"(gate-fast|soak) \([^,()]+, (\d+)\)")
#: The job that turns a red soak leg into a red run; it runs no test.
_SOAK_ROLLUP = "verdict-soak"
#: Jobs whose settled conclusion means pytest ran in the run.
_TEST_JOB = re.compile(r"(gate-fast|test-linux|soak) \(")
#: A shard's junit artifact: gate-fast's ``junit-fast-K`` or the full
#: matrix's ``junit-N-K``.
_JUNIT_ART = re.compile(r"junit-(fast-\d+|\d+-\d+)")
#: xdist's report for a test whose worker died (a hard crash, or
#: pytest-timeout's thread method killing the process). With
#: ``--max-worker-restart=0`` the session ends there: the junit parses and
#: names one failure, but the shard's remaining tests never ran.
_EARLY_STOP = re.compile(r"crashed while running|maximum crashed workers")
#: The judge and the junit readers it borrows. A diff that changes them
#: would be ruling on its own red.
_JUDGE = frozenset({"scripts/lib/red_carried.py", "scripts/ci-failed-first"})
#: Newest check.yml runs searched for the reference, 100 a page.
_RUN_PAGES = 3
#: More carried ids than this is a red main, not a gate to wave through.
MAX_IDS = 25
#: A reference run further behind the base than this vouches for nothing:
#: main may have fixed an id since, and the diff broken it again.
MAX_BEHIND = 50
WAIT_MIN = 30
POLL_S = 60
#: Whether a red gate-fast on a SELECTED gate may be carried. check.yml
#: runs the soak only after a green gate-fast, so on such a run the test
#: files outside gate-fast's selection have no verdict on the gate's tree
#: (a third of the suite on run 44729999474). Off until that is ruled
#: (gr739541); ``PRECIS_RED_CARRIED_SOAKLESS=1`` turns it on for one ship.
SOAKLESS_OK = False


class Refuse(Exception):
    """The red is not provably carried; the message is the reason."""


def _gh_json(args: list[str], *, timeout: int = 120) -> Any:
    try:
        cp = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refuse(f"gh {' '.join(args[:3])}: {exc}") from exc
    if cp.returncode != 0:
        raise Refuse(
            f"gh {' '.join(args[:3])} failed ({cp.returncode}): "
            f"{(cp.stderr or cp.stdout).strip()[:200]}"
        )
    try:
        return json.loads(cp.stdout)
    except ValueError as exc:
        raise Refuse(f"gh {' '.join(args[:3])}: unreadable JSON") from exc


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def run_view(run_id: int | str) -> dict[str, Any]:
    """Status, conclusion, jobs and the current attempt's start."""
    run = _gh_json(
        ["run", "view", str(run_id), "--json", "status,conclusion,jobs,startedAt"]
    )
    if not isinstance(run, dict):
        raise Refuse(f"run {run_id}: not a workflow-run object")
    return run


def recent_runs() -> list[dict[str, Any]]:
    """The newest check.yml runs, newest first."""
    runs: list[dict[str, Any]] = []
    for page in range(1, _RUN_PAGES + 1):
        got = _gh_json(
            [
                "api",
                f"repos/{REPO}/actions/workflows/check.yml/runs"
                f"?per_page=100&page={page}",
                "--jq",
                "[.workflow_runs[] | {id, head_sha, head_branch, event, "
                "status, conclusion, created_at}]",
            ]
        )
        if not isinstance(got, list):
            raise Refuse("check.yml run list: not a list")
        runs.extend(r for r in got if isinstance(r, dict))
        if len(got) < 100:
            break
    return runs


def artifacts(run_id: int | str) -> dict[str, dict[str, Any]]:
    """The run's live artifacts by name, as ``{id, name, created_at}``. A
    rerun uploads a name again; the newest of a name wins."""
    got = _gh_json(
        [
            "api",
            f"repos/{REPO}/actions/runs/{run_id}/artifacts?per_page=100",
            "--jq",
            "[.artifacts[] | {id, name, created_at, expired}]",
        ]
    )
    if not isinstance(got, list):
        raise Refuse(f"run {run_id}: artifact list is not a list")
    out: dict[str, dict[str, Any]] = {}
    for art in sorted(got, key=lambda a: str(a.get("created_at") or "")):
        if not art.get("expired") and art.get("name") and art.get("id"):
            out[str(art["name"])] = art
    return out


def junit_xml(art_id: int) -> str | None:
    return _FF["_xml_from_artifact"](REPO, art_id)


def changed_files(base: str, head: str) -> set[str]:
    # -z: git quotes a non-ASCII path otherwise, and it would match no id.
    cp = _git("diff", "--name-only", "-z", base, head)
    if cp.returncode != 0:
        raise Refuse(f"git diff {base[:8]} {head[:8]} failed: {cp.stderr.strip()}")
    return {p for p in cp.stdout.split("\0") if p}


def distance(sha: str, base: str) -> int | None:
    """Commits ``base`` has that ``sha`` lacks, or None when ``sha`` is not
    an ancestor of ``base`` (or is not in this clone, which says the same)."""
    if _git("merge-base", "--is-ancestor", sha, base).returncode != 0:
        return None
    cp = _git("rev-list", "--count", f"{sha}..{base}")
    out = cp.stdout.strip()
    return int(out) if cp.returncode == 0 and out.isdigit() else None


def full_gate(touched: set[str]) -> bool:
    """Whether the diff touches one of the selector's FULL triggers
    (``scripts/impacted-tests``): check.yml's gate-fast then ran the whole
    suite minus the nightly files, and there is no soak to miss. Unreadable
    selector = not full."""
    try:
        is_full = runpy.run_path(str(_SCRIPTS / "impacted-tests"))["_is_full_trigger"]
    except (OSError, SyntaxError, ImportError, KeyError):
        return False
    return any(is_full(path) is not None for path in touched)


def red_shards(run: dict[str, Any]) -> list[tuple[str, str]]:
    """The gate run's failed test shards as ``(kind, shard)``. Refuses a
    run whose red is anything but settled test shards and their roll-up."""
    shards: list[tuple[str, str]] = []
    rollup = False
    for job in run.get("jobs", []):
        name = str(job.get("name") or "")
        conclusion = job.get("conclusion")
        if job.get("status") != "completed":
            raise Refuse(f"job {name!r} never concluded ({job.get('status')})")
        if conclusion in ("success", "skipped", "neutral"):
            continue
        if name == _SOAK_ROLLUP and conclusion == "failure":
            rollup = True
            continue
        m = _SHARD_JOB.fullmatch(name)
        if m is None or conclusion != "failure":
            raise Refuse(f"job {name!r} is {conclusion} — not a failed test shard")
        shards.append((m.group(1), m.group(2)))
    if rollup and not any(kind == "soak" for kind, _ in shards):
        raise Refuse(f"{_SOAK_ROLLUP} is red but no soak leg failed")
    if not shards:
        raise Refuse("the run is red but no test shard failed")
    return shards


def failed_ids(xml_text: str, root: Path, *, strict: bool) -> list[str]:
    """Failed and errored testcases of one junit as node ids, deduped in
    file order. ``strict`` is the gate's read: it refuses a failure no file
    can be found for (a collection error, a renamed module) and one that
    ended the shard early (:data:`_EARLY_STOP`); the lenient read drops
    the first and keeps the second — red is red in a reference run."""
    try:
        doc = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        if strict:
            raise Refuse(f"junit unreadable: {exc}") from exc
        return []
    ids: list[str] = []
    for tc in doc.iter("testcase"):
        marks = [el for el in tc if el.tag in ("failure", "error")]
        if not marks:
            continue
        classname, name = tc.get("classname", ""), tc.get("name", "")
        nid = _FF["_nodeid"](root, classname, name)
        if nid is None:
            if strict:
                raise Refuse(f"cannot place failed test {classname}::{name} in a file")
            continue
        if strict and any(
            _EARLY_STOP.search(f"{el.get('message') or ''}\n{el.text or ''}")
            for el in marks
        ):
            raise Refuse(
                f"{nid} crashed its worker, which ends the shard at once — the "
                "tests after it did not run"
            )
        if nid not in ids:
            ids.append(nid)
    return ids


def gate_failed_ids(
    run_id: int | str, run: dict[str, Any], shards: list[tuple[str, str]], root: Path
) -> list[str]:
    """The node ids the gate run's red shards failed, from their junit
    (``junit-fast-K`` for gate-fast, ``junit-<N>-K`` for a soak leg). A red
    shard with no junit from the current attempt did not finish its pytest
    (an older one is a cancelled attempt's); one whose junit names no
    failure is red for a reason no id carries — all refuse."""
    started = str(run.get("startedAt") or "")
    if not started:
        raise Refuse(f"run {run_id}: no start time to date its junits against")
    arts = artifacts(run_id)
    ids: list[str] = []
    for kind, k in shards:
        want = re.compile(
            rf"junit-fast-{k}" if kind == "gate-fast" else rf"junit-\d+-{k}"
        )
        art = next((a for name, a in arts.items() if want.fullmatch(name)), None)
        if art is not None and str(art.get("created_at") or "") < started:
            raise Refuse(
                f"{kind} shard {k}'s junit is from an earlier attempt of the run"
            )
        xml = junit_xml(int(art["id"])) if art is not None else None
        if xml is None:
            raise Refuse(
                f"{kind} shard {k} left no readable junit — its pytest did not end"
            )
        got = failed_ids(xml, root, strict=True)
        if not got:
            raise Refuse(f"{kind} shard {k} is red but its junit names no failed test")
        ids.extend(i for i in got if i not in ids)
    return ids


def candidates(
    runs: list[dict[str, Any]], base: str, gate_run: str
) -> list[dict[str, Any]]:
    """Runs that can hold a test verdict for an ancestor of ``base``,
    closest behind it first, the newer of two on one commit first. A push
    to main runs no shards, so it is never one."""
    out: list[dict[str, Any]] = []
    for run in runs:
        if str(run.get("id")) == str(gate_run):
            continue
        if run.get("event") == "push" and run.get("head_branch") == "main":
            continue
        sha = str(run.get("head_sha") or "")
        d = distance(sha, base) if sha else None
        if d is None:
            continue
        out.append({**run, "distance": d})
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    out.sort(key=lambda r: int(r["distance"]))
    return out


def _tested(run: dict[str, Any]) -> bool:
    return any(
        _TEST_JOB.match(str(j.get("name") or ""))
        and j.get("conclusion") in ("success", "failure")
        for j in run.get("jobs", [])
    )


def _failed_first_red(run: dict[str, Any]) -> bool:
    """A release run whose ``failed-first`` job went red: the shard matrix
    is skipped by design, so the run has a verdict and no junit."""
    return any(
        j.get("name") == "failed-first" and j.get("conclusion") == "failure"
        for j in run.get("jobs", [])
    )


def _label(cand: dict[str, Any]) -> str:
    return (
        f"run {cand.get('id')} ({cand.get('head_branch')}, "
        f"{str(cand.get('head_sha') or '')[:8]}, {cand['distance']} commit(s) "
        "behind the base)"
    )


def carried_by(
    gate_ids: list[str],
    cands: list[dict[str, Any]],
    root: Path,
    *,
    deadline: float,
    max_behind: int = MAX_BEHIND,
    poll_s: float = POLL_S,
) -> dict[str, Any]:
    """The reference run, when it carries every gate id; refuses otherwise.
    The closest candidate with a settled test verdict decides. One still
    running is read as its junits arrive and waited for until ``deadline``."""
    for cand in cands:
        if int(cand["distance"]) > max_behind:
            raise Refuse(
                f"the closest run with a test verdict, {_label(cand)}, is over "
                f"{max_behind} commits back (PRECIS_RED_CARRIED_MAX_BEHIND) — "
                "too far to vouch for the base"
            )
        seen: dict[int, list[str]] = {}
        while True:
            run = run_view(cand["id"])
            done = run.get("status") == "completed"
            if done and run.get("conclusion") not in ("success", "failure"):
                break  # cancelled: no verdict here, the next run back decides
            if done and not _tested(run):
                if _failed_first_red(run):
                    # It IS the newest verdict, but it uploads no junit:
                    # an older run must not answer for ids it may have fixed.
                    raise Refuse(
                        f"{_label(cand)} is red on its failed-first job, which "
                        "leaves no junit to compare — re-run scripts/ship "
                        "--remote after the next release cut's run"
                    )
                break  # a docs lane, or it died before a shard ran
            green = done and run.get("conclusion") == "success"
            # A green run's junits are not read: one left by a failed
            # attempt that a rerun turned green would name a red it lacks.
            arts = {} if green else artifacts(cand["id"])
            for name, art in arts.items():
                art_id = int(art["id"])
                if art_id in seen or not _JUNIT_ART.fullmatch(name):
                    continue
                xml = junit_xml(art_id)
                if xml is not None:
                    seen[art_id] = failed_ids(xml, root, strict=False)
            live = {int(a["id"]) for a in arts.values()}
            red = {i for art_id, ids in seen.items() if art_id in live for i in ids}
            missing = [i for i in gate_ids if i not in red]
            if not missing:
                return cand
            if done:
                state = "green" if green else f"red on {len(red)} id(s) junit names"
                raise Refuse(
                    f"{len(missing)} of {len(gate_ids)} failing id(s) are not red "
                    f"in {_label(cand)}, which is {state}: "
                    f"{', '.join(missing[:5])}"
                    f"{' …' if len(missing) > 5 else ''}. They are this diff's, "
                    "or newer than that run — the next release cut's run will "
                    "carry the second kind"
                )
            if time.time() >= deadline:
                raise Refuse(
                    f"{_label(cand)} is still running and does not show "
                    f"{len(missing)} of the failing id(s) red yet — re-run "
                    "scripts/ship --remote when it has finished"
                )
            time.sleep(poll_s)
    raise Refuse(
        "no check run with a test verdict sits on an ancestor of the base "
        f"among the newest {100 * _RUN_PAGES} check.yml runs"
    )


def decide(
    gate_run: str,
    head: str,
    base: str,
    root: Path,
    *,
    wait_min: float = WAIT_MIN,
    max_ids: int = MAX_IDS,
    max_behind: int = MAX_BEHIND,
    soakless_ok: bool = SOAKLESS_OK,
) -> tuple[list[str], dict[str, Any]]:
    """``(carried ids, reference run)`` or :class:`Refuse`."""
    run = run_view(gate_run)
    shards = red_shards(run)
    touched = changed_files(base, head)
    judge = sorted(touched & _JUDGE)
    if judge:
        raise Refuse(
            f"the diff changes the judge of this rule ({', '.join(judge)}) — "
            "it lands on a green gate"
        )
    if (
        any(kind == "gate-fast" for kind, _ in shards)
        and not soakless_ok
        and not full_gate(touched)
    ):
        raise Refuse(
            "gate-fast is red, so the soak did not run: the test files "
            "outside gate-fast's selection have no verdict on this tree. "
            "PRECIS_RED_CARRIED_SOAKLESS=1 accepts gate-fast's selection as "
            "the verdict"
        )
    ids = gate_failed_ids(gate_run, run, shards, root)
    own = [i for i in ids if i.split("::", 1)[0] in touched]
    if own:
        raise Refuse(
            f"{len(own)} failing id(s) sit in a file this diff changes: "
            f"{', '.join(own[:5])}"
        )
    if len(ids) > max_ids:
        raise Refuse(
            f"{len(ids)} failing ids is over the cap of {max_ids} "
            "(PRECIS_RED_CARRIED_MAX) — fix main first"
        )
    ref = carried_by(
        ids,
        candidates(recent_runs(), base, gate_run),
        root,
        deadline=time.time() + 60 * wait_min,
        max_behind=max_behind,
    )
    return ids, ref


def _env_num(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.isdigit() else default


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="red_carried.py", description=__doc__)
    ap.add_argument("--run", required=True, help="the red gate's check.yml run id")
    ap.add_argument("--head", required=True, help="the sha the gate ran on")
    ap.add_argument("--base", required=True, help="the main sha that tree sits on")
    ap.add_argument("--root", default=".", help="the checkout (default: cwd)")
    a = ap.parse_args(argv)
    if not a.run.isdigit():
        ap.error(f"run id must be numeric: {a.run!r}")
    soakless = os.environ.get("PRECIS_RED_CARRIED_SOAKLESS")
    try:
        ids, ref = decide(
            a.run,
            a.head,
            a.base,
            Path(a.root),
            wait_min=_env_num("PRECIS_RED_CARRIED_WAIT_MIN", WAIT_MIN),
            max_ids=_env_num("PRECIS_RED_CARRIED_MAX", MAX_IDS),
            max_behind=_env_num("PRECIS_RED_CARRIED_MAX_BEHIND", MAX_BEHIND),
            soakless_ok=SOAKLESS_OK if soakless is None else soakless == "1",
        )
    except Refuse as exc:
        print(f"red-carried: no — {exc}", file=sys.stderr)
        return 1
    print(
        f"red-carried: yes — {len(ids)} failing id(s), all red in {_label(ref)}, "
        "none in a file this diff changes",
        file=sys.stderr,
    )
    print(f"{' '.join(ids)}; base run {ref.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
