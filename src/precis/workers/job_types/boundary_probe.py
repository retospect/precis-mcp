"""``boundary_probe`` job_type — mechanical fence for a slice branch
(td574934 A4, gr596928).

A boundary is a named invariant the factory relies on — the agent
envelope, the slice env allowlist, the ``safe_fetch`` SSRF guard, the
deny list — with a registered probe under ``scripts/probes/`` that
verifies it against a tree (contract: exit 0 pass, 1 breach with the
finding on stdout, anything else or a 600 s timeout = cannot-run). A
branch *hits* a boundary when its diff touches one of the boundary's
paths; before this job a breakout like td573740 round 2 was found only
by a reviewer reading the diff.

Detached submit/poll pair under ``ssh_node`` (the ``lane_health``
shape), mechanical — no LLM, no store writes:

* :func:`submit` reads the branch diff (``origin/main...HEAD`` plus the
  working tree, :func:`run_slice.changed_paths`), computes the boundary
  hits, builds the run plan and launches one detached child
  (``python -m …boundary_probe run --plan … --out …``).
* The child runs each planned probe inside the slice's test container —
  the ``precis-dev`` image with the branch worktree mounted read-only
  at ``/app`` and the probe file at ``/probe.py``: ``--network none``,
  no env (no DSN), no HOME mount. It writes every run's record to the
  results file.
* :func:`poll` records ``meta.probes`` —
  ``[{boundary, probe, probe_sha, head_sha, exit, class}]`` — and a
  summary chunk. Probe provenance: the ``origin/main`` blob runs first;
  when the diff changes the probe the branch's version runs too (both
  must pass); a probe new on the branch runs branch-only; a probe the
  branch deleted is ``cannot-run``; a boundary with no registered probe
  is ``unprobed`` — the job refuses it rather than passing (fail
  closed). Anything but all-``pass`` fails the job.

Not wired into ``land_slice`` or ``plan_dispatch`` — that is A3/A5.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types import release_cut as _rc
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "boundary_probe"

#: Per-probe wall budget inside the container run — the contract's
#: cannot-run bound.
_PROBE_TIMEOUT_S = 600

#: The job's own wall cap: four boundaries, main + branch versions each,
#: plus container start overhead — serial worst case sits under this.
_WALL_S = 5400.0

#: Boundary name → repo paths a branch may not touch unverified.
#: TODO A2: take this from ``precis.plans.boundary.BOUNDARIES`` when A2's
#: hit-list registry lands — until then it is the td574934 §1 table. A
#: boundary's own probe file belongs to it: a probe change is itself a
#: boundary hit, and main's probe still verifies the boundary.
BOUNDARIES: dict[str, tuple[str, ...]] = {
    "envelope": (
        "src/precis/utils/claude_agent.py",
        "src/precis/workers/envelope.py",
        "src/precis/workers/executors/agent_container.py",
        "scripts/probes/envelope.py",
    ),
    "slice_env": (
        "src/precis/workers/job_types/run_slice.py",
        "scripts/probes/slice_env.py",
    ),
    "safe_fetch": (
        "src/precis/utils/safe_fetch.py",
        "scripts/probes/safe_fetch.py",
    ),
    "deny_list": (
        "src/precis/plans/lanes.py",
        "src/precis/workers/job_types/run_slice.py",
        "scripts/probes/deny_list.py",
    ),
}

#: The registered probe for each boundary — a boundary with no entry is
#: refused (class ``unprobed``), never silently passed.
PROBES: dict[str, str] = {
    "envelope": "scripts/probes/envelope.py",
    "slice_env": "scripts/probes/slice_env.py",
    "safe_fetch": "scripts/probes/safe_fetch.py",
    "deny_list": "scripts/probes/deny_list.py",
}

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The slice worktree holding the branch under test (required).
        "worktree": {"type": "string"},
        # Informational — recorded on the handle; the diff is HEAD's.
        "branch": {"type": "string"},
        # ssh_node's host pin (the host that holds the worktree).
        "target_node": {"type": "string"},
    },
    "required": ["worktree"],
    "additionalProperties": False,
}

#: Host env the detached child needs: the container CLI's config (HOME,
#: DOCKER_HOST, COLIMA_HOME) and the probe's own knobs. No DSN, no
#: vault names — the child inherits nothing else.
_CHILD_ENV: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "DOCKER_HOST",
    "COLIMA_HOME",
    "PRECIS_CONTAINER_BIN",
    "PRECIS_PODMAN_BIN",
    "PRECIS_BOUNDARY_PROBE_IMAGE",
    "PRECIS_JOB_CPUSET",
    "PRECIS_JOB_CPU_SHARES",
    "PRECIS_SLICE_OPS_DIR",
    "PRECIS_NODE",
)


def wall_seconds() -> float:
    """Type cap for ssh_node's lease and kill deadline."""
    return _WALL_S


def _image() -> str:
    """The slice test image probes run in — ``precis-dev`` (the one
    ``scripts/test`` gates with), overridable for a renamed local tag."""
    return os.environ.get("PRECIS_BOUNDARY_PROBE_IMAGE") or "precis-dev"


def boundary_hits(changed: list[str]) -> list[str]:
    """Sorted boundary names the changed paths touch."""
    return sorted(
        name
        for name, globs in BOUNDARIES.items()
        if any(fnmatch.fnmatchcase(p, g) for p in changed for g in globs)
    )


def _classify_exit(code: int | None) -> str:
    """The probe contract: 0 pass, 1 breach, anything else cannot-run."""
    if code == 0:
        return "pass"
    if code == 1:
        return "breach"
    return "cannot-run"


def _record(
    boundary: str,
    probe: str | None,
    probe_sha: str | None,
    head_sha: str,
    exit_code: int | None,
    cls: str,
    finding: str = "",
) -> dict[str, Any]:
    """One ``meta.probes`` row."""
    rec: dict[str, Any] = {
        "boundary": boundary,
        "probe": probe,
        "probe_sha": probe_sha,
        "head_sha": head_sha,
        "exit": exit_code,
        "class": cls,
    }
    if finding:
        rec["finding"] = finding
    return rec


def _blob_sha(tree: Path, spec: str) -> str | None:
    """Blob sha of ``<ref>:<path>`` (e.g. ``origin/main:scripts/…``),
    ``None`` when the ref does not carry it."""
    proc = _rs._git(["rev-parse", "-q", "--verify", spec], tree)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _extract_probe(tree: Path, spec: str, dest: Path) -> bool:
    """Write ``<ref>:<path>``'s content to ``dest``; ``False`` when git
    cannot produce it."""
    proc = _rs._git(["show", spec], tree)
    if proc.returncode != 0:
        return False
    try:
        dest.write_text(proc.stdout, encoding="utf-8")
    except OSError:
        return False
    return True


def _branch_probe(tree: Path, rel: str) -> str | None:
    """Blob sha of the probe file the worktree carries, ``None`` when
    the branch has no such file."""
    if not (tree / rel).is_file():
        return None
    proc = _rs._git(["hash-object", "--", rel], tree)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _rev_parse(tree: Path, ref: str) -> str | None:
    proc = _rs._git(["rev-parse", "-q", "--verify", ref], tree)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def probe_plan(
    tree: Path, changed: list[str], head_sha: str, work: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(runs, records)`` for the boundaries the diff hits.

    A run is one containerized probe invocation: ``source`` ``main``
    (origin/main's blob, extracted under ``work``) or ``branch`` (the
    worktree file). ``records`` are the runs that cannot happen —
    ``unprobed`` (no registered probe, or no probe file anywhere) and
    ``cannot-run`` (a probe the branch deleted, or an unreadable main
    blob)."""
    runs: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for boundary in boundary_hits(changed):
        rel = PROBES.get(boundary)
        if rel is None:
            records.append(
                _record(
                    boundary,
                    None,
                    None,
                    head_sha,
                    None,
                    "unprobed",
                    "no registered probe for this boundary",
                )
            )
            continue
        main_sha = _blob_sha(tree, f"origin/main:{rel}")
        branch_sha = _branch_probe(tree, rel)
        probe_changed = rel in changed
        if main_sha is not None:
            dest = work / f"main-{boundary}.py"
            if _extract_probe(tree, f"origin/main:{rel}", dest):
                runs.append(
                    {
                        "boundary": boundary,
                        "probe": rel,
                        "probe_sha": main_sha,
                        "head_sha": head_sha,
                        "source": "main",
                        "file": str(dest),
                    }
                )
            else:
                records.append(
                    _record(
                        boundary,
                        rel,
                        main_sha,
                        head_sha,
                        None,
                        "cannot-run",
                        "git show could not extract origin/main's probe",
                    )
                )
        if branch_sha is not None and (probe_changed or main_sha is None):
            runs.append(
                {
                    "boundary": boundary,
                    "probe": rel,
                    "probe_sha": branch_sha,
                    "head_sha": head_sha,
                    "source": "branch",
                    "file": str(tree / rel),
                }
            )
        elif main_sha is not None and branch_sha is None and probe_changed:
            # The branch deleted the probe — after this lands the
            # boundary has no mechanical check at all. Refuse it.
            records.append(
                _record(
                    boundary,
                    rel,
                    None,
                    head_sha,
                    None,
                    "cannot-run",
                    "probe deleted on the branch",
                )
            )
        if main_sha is None and branch_sha is None:
            records.append(
                _record(
                    boundary,
                    rel,
                    None,
                    head_sha,
                    None,
                    "unprobed",
                    "probe absent on origin/main and on the branch",
                )
            )
    return runs, records


def _container_bin() -> str:
    """The container CLI on this host (docker/podman), the agent
    container path's own resolution."""
    from precis.workers.executors import agent_container as _ac

    return _ac._container_bin()


def _container_argv(probe_file: str, tree: Path, image: str) -> list[str]:
    """``docker run`` for one probe: the branch tree read-only at
    ``/app``, the probe file at ``/probe.py`` — ``--network none``, no
    env (no DSN), no HOME mount, the image's own user and PATH."""
    from precis.utils.container_limits import container_limit_flags

    return [
        _container_bin(),
        "run",
        "--rm",
        *container_limit_flags(),
        "--network",
        "none",
        "-v",
        f"{tree}:/app:ro",
        "-v",
        f"{probe_file}:/probe.py:ro",
        "-w",
        "/app",
        image,
        "python",
        "/probe.py",
    ]


def _run_probe(run: dict[str, Any], tree: Path, image: str) -> tuple[int | None, str]:
    """One containerized probe → ``(exit code or None, stdout+stderr)``."""
    argv = _container_argv(str(run["file"]), tree, image)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"probe exceeded {_PROBE_TIMEOUT_S}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"container launch failed: {exc}"
    out = (proc.stdout or "") + (
        f"\n--- stderr ---\n{proc.stderr}" if proc.stderr else ""
    )
    return proc.returncode, out


def _child_env() -> dict[str, str]:
    """The detached child's env: the allowlisted host surface only —
    the container CLI's config and the probe knobs, never a DSN or a
    vault name."""
    return {k: os.environ[k] for k in _CHILD_ENV if os.environ.get(k)}


def _refusal(params: dict[str, Any]) -> str | None:
    if _rs.factory_off():
        return (
            f"{NAME}: the factory is off (PRECIS_FACTORY=off); next: unset "
            "it on the worker host to run boundary probes"
        )
    worktree = params.get("worktree")
    if not isinstance(worktree, str) or not worktree.strip():
        return f"{NAME}: params.worktree must name the slice worktree path"
    return None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del store, gripe_id
    return _refusal(dict(params or {}))


def submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half: compute the hits, build the run plan and
    launch the probe child."""
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    refusal = _refusal(params)
    if refusal:
        ctx.record_failure(refusal)
        return None
    tree = Path(str(params["worktree"])).resolve()
    changed = _rs.changed_paths(tree)
    if changed is None:
        ctx.record_failure(
            f"{NAME}: could not read the branch diff in {tree} (git failed); "
            "failing closed"
        )
        return None
    head_sha = _rev_parse(tree, "HEAD") or ""
    hits = boundary_hits(changed)
    if not hits:
        return {"no_boundary": True, "head_sha": head_sha}
    run_dir = _rs.ops_root() / NAME / f"job-{ctx.ref_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    runs, records = probe_plan(tree, changed, head_sha, run_dir)
    plan_file = run_dir / "plan.json"
    out_file = run_dir / "results.json"
    out_file.unlink(missing_ok=True)
    plan_file.write_text(
        json.dumps(
            {
                "tree": str(tree),
                "image": _image(),
                "runs": runs,
                "records": records,
            }
        ),
        encoding="utf-8",
    )
    log_path = run_dir / "child.log"
    exit_file = run_dir / "child.exit"
    exit_file.unlink(missing_ok=True)
    try:
        pid = _rs._launch(
            [
                sys.executable,
                "-m",
                __name__,
                "run",
                "--plan",
                str(plan_file),
                "--out",
                str(out_file),
            ],
            cwd=run_dir,
            stdin=None,
            log=log_path,
            exit_file=exit_file,
            env=_child_env(),
        )
    except Exception as exc:
        log.warning("%s: probe child launch failed", NAME, exc_info=True)
        ctx.record_failure(f"{NAME}: probe child launch failed: {exc}")
        return None
    ctx.append_chunk(
        "job_event",
        f"{NAME}: launched probe child pid {pid} for "
        f"{len(runs)} run(s) over {', '.join(hits)}",
    )
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "log": str(log_path),
        "exit_file": str(exit_file),
        "out": str(out_file),
        "head_sha": head_sha,
        "boundary_hits": hits,
        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        handle["boot_id"], handle["start_time"] = identity
    return handle


def poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half: fold the child's results into ``meta.probes``
    and fail on anything but all-pass."""
    if isinstance(handle, dict) and handle.get("no_boundary"):
        ctx.set_meta(probes=[], boundary_hits=[], head_sha=handle.get("head_sha") or "")
        ctx.append_chunk("job_summary", f"{NAME}: the branch touches no boundary")
        ctx.set_status("succeeded")
        return True
    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return False
    exit_code, log_text = done
    out = Path(str(handle.get("out", ""))) if isinstance(handle, dict) else None
    records: list[dict[str, Any]] | None = None
    if out is not None and out.is_file():
        try:
            records = json.loads(out.read_text(encoding="utf-8"))["records"]
        except (OSError, ValueError, KeyError, TypeError):
            records = None
    if records is None:
        _rc.fail_with_tail(
            ctx,
            f"{NAME}: probe child exit {exit_code} left no results",
            log_text,
        )
        return True
    ctx.set_meta(probes=records)
    bad = [r for r in records if r.get("class") != "pass"]
    lines = [
        f"{r.get('boundary')} ({r.get('probe') or '-'}): {r.get('class')}"
        + (f" — {r.get('finding', '')[:200]}" if r.get("finding") else "")
        for r in bad
    ]
    ctx.append_chunk(
        "job_summary",
        f"{NAME}: {len(records) - len(bad)}/{len(records)} probe run(s) "
        "passed" + (("; " + " | ".join(lines[:8])) if lines else ""),
    )
    if bad:
        ctx.record_failure(
            f"{NAME}: boundary probe(s) not clean\n" + "\n".join(lines[:8])
        )
        return True
    ctx.set_status("succeeded")
    return True


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: SIGTERM the child's process group."""
    _rc.kill(ctx, handle)


def _main(argv: list[str]) -> int:
    """The detached probe child: run every planned probe in the test
    container and write the records the job's poll reads."""
    if argv[:1] != ["run"] or "--plan" not in argv or "--out" not in argv:
        print(
            f"usage: python -m {__name__} run --plan <plan.json> --out <results.json>",
            file=sys.stderr,
        )
        return 2
    plan_file = Path(argv[argv.index("--plan") + 1])
    out_file = Path(argv[argv.index("--out") + 1])
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    tree = Path(plan["tree"])
    image = plan["image"]
    records: list[dict[str, Any]] = []
    for run in plan.get("runs", []):
        exit_code, output = _run_probe(run, tree, image)
        cls = _classify_exit(exit_code)
        rec = _record(
            run["boundary"],
            run["probe"],
            run["probe_sha"],
            run.get("head_sha") or "",
            exit_code,
            cls,
            "" if cls == "pass" else output[-4000:],
        )
        rec["source"] = run.get("source", "?")
        records.append(rec)
        print(
            f"probe: boundary={run['boundary']} source={run.get('source')} "
            f"exit={exit_code} class={cls}",
            flush=True,
        )
    records.extend(plan.get("records", []))
    out_file.write_text(json.dumps({"records": records}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"ssh_node"}),
    requires=frozenset(),
    description=(
        "Run each registered boundary probe a slice branch hits: "
        "origin/main's scripts/probes/<boundary>.py in the precis-dev "
        "container against the branch tree (--network none, no DSN, no "
        "HOME mount), plus the branch's version when the diff changes or "
        "adds the probe; meta.probes records {boundary, probe, probe_sha, "
        "head_sha, exit, class} and anything but all-pass fails — a "
        "boundary with no registered probe is refused as unprobed."
    ),
    submit=submit,
    poll=poll,
    kill=kill,
    wall_seconds=wall_seconds,
    validate_submit=validate_submit,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "BOUNDARIES",
    "NAME",
    "PROBES",
    "SPEC",
    "boundary_hits",
    "kill",
    "load",
    "poll",
    "probe_plan",
    "submit",
    "validate_submit",
    "wall_seconds",
]
