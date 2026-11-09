"""``release_cut`` — cut ``release/r<N>`` and return (dark release pass, td543733).

Spec: ``docs/backlog/dark-factory.md`` § "4. Release pass, event-driven";
the ``cut`` node of the ``release-pass`` template. Mechanical, no LLM. A
detached submit/poll pair under ``ssh_node``:

* :func:`submit` runs ``scripts/release --cut-only`` in the repo checkout
  (:func:`precis.workers.job_types.run_slice.slice_root`: ``PRECIS_SLICE_ROOT``,
  else the main checkout around the worker's code) detached, stdout+stderr to
  a log and the exit code to a file beside it (under the temp dir), and
  returns ``{pid, pgid, root, log, exit_file, started_at}``. The sha it
  cuts comes from :func:`_cut_target` (gr592271): the pass's recorded
  ``meta.release_target.sha`` for a first cut, the recomputed newest
  fully-reviewed head for a release-fix re-cut, the tip for a pass
  minted without a target — ``--sha`` is passed only in the first two.
* :func:`poll` waits for the exit file; on exit 0 with a
  ``RELEASE_CUT: r<N> <sha>`` line it records the cut on the plan
  (:func:`precis.plans.release.record_cut`: ``meta.release`` on the plan
  root with the ``host`` and ``checkout`` it ran in, the real range on the
  plan's ``tests-green`` gate and loop, ``deploy r<N>`` on the approval
  ask), writes the shared ``release.round`` row through
  :func:`precis.plans.release.update_round` (``create=True`` — the
  script's own per-clone publish may have skipped on this host; the row
  carries ``pass: td<root>`` so ``release_poll`` can abandon it when
  the pass is dropped), and
  succeeds with r<N> and the sha in the
  summary and ``meta.release_cut``. The round write is best-effort:
  skipped when the cut's host is unknown, a failure a log warning plus a
  summary note — never a job failure, the cut happened. No
  ``RELEASE_CUT`` line (S already live,
  a release in flight elsewhere) or a non-zero exit fails the job with the
  log tail; the plan tick's retry cap then files the ask.

Before ``--cut-only`` launches, :func:`submit` clears the checkout's
stale local round when three preflight conditions hold: the clone's own
``round.json`` has exactly one ``deploying``/``deployed`` record (an int
``n``, a pinned sha, no pending cut, no runtime confirm), the shared
``release.round`` row proves that release rolled out elsewhere
(:func:`precis.plans.release.round_confirmed`: ``confirmed`` at ``n`` or
a later number — a hand-cut r<n+1> supersedes it too, a smaller number
never does), and no ``release_deploy`` job is live anywhere in the store
(:func:`precis.plans.release.live_deploy_job`). The supersede runs as a
detached ``supersede`` phase — ``scripts/round supersede r<n>`` with
``PRECIS_GRIPE_NOTE=0`` so the shared row is never rewritten; the verb's
own ancestor-of-prod gate is the proof, it touches no remote ref, and a
refusal fails the job with the log tail (no cut launched). On exit 0 the
handle swaps onto the usual ``--cut-only`` launch, the supersede note
carried into the cut's summary. An open ``cut``/``gating``/``fixing``/
``green`` local record is not covered — ``supersede`` refuses those, and
the false-``RELEASE_CUT`` hazard they carry needs ``scripts/release``'s
resume path, a later slice (td575911).

The cut never waits for CI: ``ci_verdict_poll`` records the verdict and the
``tests-green`` gate reads it. Refused while ``PRECIS_FACTORY=off``. The
helpers below (:func:`launch_script`, :func:`finish_run`) are shared with
``release_deploy``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.plans import pr_class
from precis.plans import release as _release
from precis.workers import factory_git as _factory_git
from precis.workers.job_types import release_poll as _rp
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "release_cut"

_DEFAULT_WALL_S = 30 * 60
_TAIL_CAP = 2000

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The plan's cut node; its plan root gets meta.release. Unset: the
        # cut still runs, nothing is stamped.
        "todo_id": {"type": "integer"},
        # ssh_node's host pin (the host holding the checkout + push creds).
        "target_node": {"type": "string"},
        "resources": {
            "type": "object",
            "properties": {"wall_seconds": {"type": "integer"}},
        },
    },
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Dark release pass: run scripts/release --cut-only in the repo checkout "
    "(cut release/r<N> from main, no CI wait), then stamp r<N>/sha on the "
    "plan root's meta.release and the real range on the plan's tests-green "
    "gate. Refused while PRECIS_FACTORY=off."
)


def wall_seconds() -> float:
    return float(_DEFAULT_WALL_S)


# ── Shared detached-script helpers (release_deploy uses them too) ──


def _launch(
    argv: list[str],
    *,
    cwd: Path,
    log_path: Path,
    exit_file: Path,
    extra_env: Mapping[str, str] | None = None,
) -> int:
    """Seam: start ``argv`` detached (run_slice's launcher, stdin from
    /dev/null, the worker's own env plus the factory git token as git's
    credential helper — the cut and deploy push ``release/r<N>``, ``gated``
    and ``prod`` from a service account's checkout; ``scripts/deploy``
    keeps the worker's own env for the rest). ``extra_env`` merges over
    that env (the stale-round supersede's ``PRECIS_GRIPE_NOTE=0``)."""
    env = _factory_git.git_auth_env()
    if extra_env:
        env.update(extra_env)
    return _rs._launch(
        argv,
        cwd=cwd,
        stdin=Path(os.devnull),
        log=log_path,
        exit_file=exit_file,
        env=env,
    )


def _run_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "precis-release-ops"
    path.mkdir(parents=True, exist_ok=True)
    return path


def factory_refusal(name: str) -> str | None:
    if _rs.factory_off():
        return (
            f"{name}: the factory is off (PRECIS_FACTORY=off); next: unset it on "
            "the worker host to run release ops"
        )
    return None


def todo_refusal(store: Any, name: str, todo_id: Any, *, required: bool) -> str | None:
    if todo_id is None and not required:
        return None
    if not isinstance(todo_id, int) or isinstance(todo_id, bool) or todo_id <= 0:
        return f"{name}: params.todo_id must be a positive integer (the plan node)"
    if store is not None and store.get_ref(kind="todo", id=todo_id) is None:
        return f"{name}: todo {todo_id} not found; next: pass a live plan node id"
    return None


def launch_script(
    ctx: Any,
    name: str,
    args: list[str],
    *,
    script: str = "release",
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Run ``<root>/scripts/<script> <args>`` detached; the handle, or
    ``None`` after failing the job. ``env`` merges over the launcher's
    factory-git env."""
    try:
        root = _rs.slice_root()
        path = root / "scripts" / script
        if not path.exists():
            raise RuntimeError(f"no scripts/{script} in {root}")
        base = _run_dir() / f"{name}-{ctx.ref_id}"
        log_path = base.with_suffix(".log")
        exit_file = base.with_suffix(".exit")
        exit_file.unlink(missing_ok=True)
        pid = _launch(
            [str(path), *args],
            cwd=root,
            log_path=log_path,
            exit_file=exit_file,
            extra_env=env or None,
        )
    except Exception as exc:
        log.warning("%s: launch failed", name, exc_info=True)
        ctx.record_failure(f"{name}: launch failed: {exc}")
        return None
    ctx.append_chunk(
        "job_event",
        f"{name}: launched scripts/{script} {' '.join(args)} pid {pid} in {root}",
    )
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "root": str(root),
        "log": str(log_path),
        "exit_file": str(exit_file),
        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        handle["boot_id"], handle["start_time"] = identity
    return handle


def finish_run(ctx: Any, handle: Any, name: str) -> tuple[int | None, str] | None:
    """``None`` while the script runs; else ``(exit code, log text)``
    (``exit code`` ``None`` when the process vanished without one)."""
    if not isinstance(handle, dict):
        ctx.record_failure(f"{name}: malformed handle {handle!r}")
        return None
    exit_code = _rs._read_exit(handle)
    if exit_code is None and _rs._process_alive(int(handle.get("pid") or 0)):
        return None
    ctx.set_meta(compute_handle_released=True, exit_code=exit_code)
    return exit_code, _rs._read_log(handle)


def fail_with_tail(ctx: Any, reason: str, log_text: str) -> None:
    tail = log_text[-_TAIL_CAP:].strip()
    ctx.record_failure(f"{reason}\n--- log tail ---\n{tail}" if tail else reason)


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: SIGTERM the script's process group."""
    del ctx
    if not isinstance(handle, dict):
        return
    pgid = int(handle.get("pgid") or handle.get("pid") or 0)
    if pgid <= 1:
        return
    try:
        _rs._signal_group(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def run_host(params: dict[str, Any]) -> str | None:
    """The node this job runs on: its ``params.target_node`` pin (ssh_node
    pins a job to the node that launched it), else this worker's
    ``PRECIS_NODE``; ``None`` for a node-less worker."""
    node = params.get("target_node") or os.environ.get("PRECIS_NODE")
    return str(node).strip() or None if node else None


def origin_repo(root: Path) -> str | None:
    """``owner/repo`` of the checkout's ``origin`` remote, else ``None``."""
    try:
        proc = _rs._git(["remote", "get-url", "origin"], root)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _release.repo_from_remote(proc.stdout)


# ── the stale local round record (read straight from the file, no net) ──


def _round_json_path(root: Path) -> Path | None:
    """Seam (tests patch it): the checkout's per-clone round record
    ``<git-common-dir>/precis-round/round.json`` (``scripts/round``'s state
    dir), else ``None`` when git cannot say."""
    try:
        proc = _rs._git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], root
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()) / "precis-round" / "round.json"


def _stale_local_round(root: Path) -> int | None:
    """``n`` of the one local ``round.json`` record ``scripts/round
    supersede r<n>`` would take — ``deploying``/``deployed``, an int
    ``n``, no pending cut, a ``deployment`` dict pinning a non-empty
    ``sha``, no ``runtime_confirmed_at`` — else ``None``. A missing or
    unparsable file, a closed round, an open (``cut``/``gating``/
    ``fixing``/``green`` — ``supersede`` refuses them) or terminal
    record, and several candidates all answer ``None``."""
    path = _round_json_path(root)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("open"):
        return None
    candidates = []
    for rel in data.get("releases") or []:
        if not isinstance(rel, dict):
            continue
        n = rel.get("n")
        if rel.get("state") not in ("deploying", "deployed") or type(n) is not int:
            continue
        if rel.get("cut_pending"):
            continue
        dep = rel.get("deployment")
        if (
            not isinstance(dep, dict)
            or not isinstance(dep.get("sha"), str)
            or not dep["sha"]
            or dep.get("runtime_confirmed_at")
        ):
            continue
        candidates.append(n)
    return candidates[0] if len(candidates) == 1 else None


def _stale_supersede_n(ctx: Any) -> int | None:
    """``n`` of the checkout's stale local record the cut supersedes
    first — the three preflight conditions of the module docstring — or
    ``None``. A read failure is a warning and "no": the cut launches as
    today and the script's own ``stale_why`` gate decides."""
    try:
        root = _rs.slice_root()
    except RuntimeError:
        return None  # launch_script below reports the same failure
    n = _stale_local_round(root)
    if n is None:
        return None
    try:
        confirmed = _release.round_confirmed(_release.read_round(ctx.store), n)
        quiet = not _release.live_deploy_job(ctx.store)
    except Exception:
        log.warning("%s: stale-round preflight read failed", NAME, exc_info=True)
        return None
    return n if confirmed and quiet else None


# ── release_cut ────────────────────────────────────────────────────


def _cut_target(store: Any, todo_id: Any) -> tuple[str | None, str | None]:
    """``(sha, refusal)`` — the sha ``scripts/release --cut-only --sha``
    cuts, ``None`` for a tip cut, or ``(None, refusal)`` while the
    engine-review gate holds (gr592271).

    Three cases: a re-cut (the plan's ``meta.release`` exists — the
    fix's land moved main since the pass minted, so the recorded
    target is stale) recomputes the newest fully-reviewed head through
    :func:`release_poll.cleared_head`; a first cut uses the pass's
    recorded ``meta.release_target`` — only the lands inside its
    ``upto`` window may hold it, a newer land's open review does not;
    a pass without a recorded target (a hand mint, or pre-gr592271)
    keeps the whole-gate tip rule."""
    rel = _release.release_of(store, todo_id) if isinstance(todo_id, int) else None
    if rel is not None:
        repo = pr_class.resolve_repo(store, None)
        if not repo:
            return None, (
                "no GitHub owner/repo (release.repo and the checkout's "
                "origin both empty); next: set release.repo"
            )
        try:
            sha, blockers, _notes = _rp.cleared_head(store, repo=repo)
        except pr_class.GitHubUnreadable as exc:
            return None, f"GitHub unreadable ({exc}); next: the next tick retries"
        if sha is None:
            return None, "; ".join(blockers)
        return sha, None
    target = (
        _release.release_target_of(store, todo_id) if isinstance(todo_id, int) else None
    )
    if target is not None:
        verdicts, _notes, fatal = _rp._engine_verdicts(store)
        if fatal is not None:
            return None, fatal
        blockers = [
            v.line
            for v in verdicts or ()
            if not v.cleared and _rp._sha_in(v.unit.sha12, target["upto"])
        ]
        if blockers:
            return None, "; ".join(blockers)
        return str(target["sha"]), None
    blockers, _notes = _rp.engine_review_blockers(store)
    if blockers:
        return None, "; ".join(blockers)
    return None, None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    todo_id = params.get("todo_id")
    reason = factory_refusal(NAME) or todo_refusal(store, NAME, todo_id, required=False)
    if reason is not None or store is None or not isinstance(todo_id, int):
        return reason
    # A re-cut runs where the plan's first cut ran: the .release-owner marker
    # is in that checkout, so --cut-only elsewhere cuts nothing (td543855).
    # The first cut has no meta.release yet and is not pinned.
    pin = _release.pin_refusal(
        _release.release_of(store, todo_id),
        host=str(params.get("target_node") or "").strip() or None,
    )
    if pin is not None:
        return f"{NAME}: {pin}"
    _sha, refusal = _cut_target(store, todo_id)
    return f"{NAME}: {refusal}" if refusal is not None else None


def submit(ctx: Any, spec: Any) -> Any:
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    reason = factory_refusal(NAME) or todo_refusal(
        ctx.store, NAME, params.get("todo_id"), required=False
    )
    if reason is not None:
        ctx.record_failure(reason)
        return None
    sha, refusal = _cut_target(ctx.store, params.get("todo_id"))
    if refusal is not None:
        ctx.record_failure(f"{NAME}: {refusal}")
        return None
    argv = ["--cut-only"] + (["--sha", sha] if sha else [])
    if ctx.store is not None:
        n = _stale_supersede_n(ctx)
        if n is not None:
            # A launched phase, not a synchronous call: the verb needs
            # the factory git env for its ls-remote/fetch, and a
            # restarted worker still reads the verdict from the log and
            # exit file. PRECIS_GRIPE_NOTE=0 skips its release.round
            # publish — the other clone's confirmed row stays as it was.
            handle = launch_script(
                ctx,
                f"{NAME}_supersede",
                ["supersede", f"r{n}"],
                script="round",
                env={"PRECIS_GRIPE_NOTE": "0"},
            )
            if isinstance(handle, dict):
                handle.update(phase="supersede", n=n, cut_argv=argv)
            return handle
    return launch_script(ctx, NAME, argv)


def _superseded(ctx: Any, handle: dict[str, Any]) -> bool:
    """The ``supersede`` phase's verdict: a refusal fails the job with
    the log tail and cuts nothing; exit 0 swaps the handle in place onto
    the ``--cut-only`` launch (``release_confirm._confirmed``'s pattern:
    a fresh handle persisted as ``compute_handle`` with a fresh
    deadline, the note carried into the cut's summary)."""
    done = finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    n = handle.get("n")
    if exit_code != 0:
        fail_with_tail(
            ctx,
            f"{NAME}: scripts/round supersede r{n} exit {exit_code}; no cut launched",
            log_text,
        )
        return True
    note = f"superseded stale local r{n}, prod moved past it"
    tail = log_text.strip()[-300:]
    fresh = launch_script(ctx, NAME, list(handle.get("cut_argv") or ["--cut-only"]))
    ctx.append_chunk("job_event", f"{NAME}: {note}" + (f" — {tail}" if tail else ""))
    ctx.set_meta(stale_round_superseded={"n": n})
    if fresh is None:
        return True  # the launch failure is recorded; no cut
    handle.clear()
    handle.update(fresh)
    handle["notes"] = [note]
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=time.time() + wall_seconds(),
    )
    return False


def poll(ctx: Any, handle: Any) -> bool:
    if isinstance(handle, dict) and handle.get("phase") == "supersede":
        return _superseded(ctx, handle)
    done = finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    cut = _release.parse_cut(log_text)
    if exit_code != 0 or cut is None:
        why = (
            f"exit {exit_code}"
            if exit_code != 0
            else "exit 0 but no RELEASE_CUT line (S already live, or a release "
            "is in flight elsewhere — nothing cut for this plan)"
        )
        fail_with_tail(ctx, f"{NAME}: scripts/release --cut-only {why}", log_text)
        return True
    rn, sha, rng = cut
    params = (ctx.meta or {}).get("params") or {}
    todo_id = params.get("todo_id")
    checkout = str(handle.get("root") or ".")
    repo = origin_repo(Path(checkout))
    host = run_host(params)
    notes: list[str] = list(handle.get("notes") or [])
    if isinstance(todo_id, int):
        now = datetime.now(UTC)
        try:
            notes += _release.record_cut(
                ctx.store,
                todo_id,
                rn,
                sha,
                rng,
                repo=repo,
                host=host,
                checkout=checkout,
                now=now,
            )
        except Exception as exc:
            log.warning("%s: record_cut on td%s failed", NAME, todo_id, exc_info=True)
            fail_with_tail(
                ctx,
                f"{NAME}: cut {rn} at {sha[:9]} but recording it on td{todo_id} "
                f"failed: {exc}",
                log_text,
            )
            return True
        if host is None:
            notes.append("release.round not written: the cut's host is unknown")
        else:
            try:
                notes += _release.update_round(
                    ctx.store,
                    {
                        "n": int(rn[1:]),
                        "branch": f"release/{rn}",
                        "base": sha,
                        "state": "cut",
                        "owner": {"host": host, "checkout": checkout},
                        "pass": f"td{_release.plan_root(ctx.store, todo_id)}",
                    },
                    create=True,
                    now=now,
                )
            except Exception as exc:
                # The cut happened; like record_prod_head this write is
                # best-effort — a warning plus a summary note, not a failure.
                log.warning("%s: release.round write failed", NAME, exc_info=True)
                notes.append(f"release.round not written: {type(exc).__name__}")
    ctx.set_meta(
        release_cut={
            "rN": rn,
            "sha": sha,
            "range": rng,
            "repo": repo,
            "host": host,
            "checkout": checkout,
        }
    )
    ctx.append_chunk(
        "job_summary",
        "\n".join(
            [
                f"release_cut: {rn} at {sha} (range {rng}, repo {repo or 'unknown'}, "
                f"host {host or 'unknown'}, checkout {checkout})"
            ]
            + [f"- {n}" for n in notes]
        ),
    )
    ctx.set_status("succeeded")
    return True


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "NAME",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "finish_run",
    "kill",
    "launch_script",
    "origin_repo",
    "poll",
    "run_host",
    "submit",
    "validate_submit",
    "wall_seconds",
]
