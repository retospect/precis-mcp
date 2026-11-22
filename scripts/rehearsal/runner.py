"""Orchestration for ``scripts/factory-rehearsal``.

One run: build the world, start the scratch database, migrate it, start the
worker, connect the driver, run the selected scenarios in order, print one
line per scenario, tear everything down (unless ``--keep``).

Exit status: 0 when no scenario failed and the run was gating; 1 when one
failed (``FAIL``), when a scenario selected by name is not implemented, or
when the world could not be brought up; 2 for a usage error; 3 when every
scenario passed but the run was **non-gating** (``--lint off`` or
``--allow-dirty``): such a pass is not an acceptance pass, and the table says
so.
"""

from __future__ import annotations

import argparse
import shutil
import signal
import sys
import time
import traceback
from pathlib import Path
from types import FrameType

from . import db as dbmod
from . import invariants as inv
from . import procguard
from .ctx import Ctx
from .safety import Refused
from .scenarios import GATE, REGISTRY, Scenario
from .world import WorldError, build_world, run_git

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH = REPO_ROOT / "scratch" / "rehearsal"

#: Resources the world's host must advertise before a job can be claimed.
BASE_TOKENS = {"repo_checkout", "git"}
LANE_TOKENS = {
    "devin": "devin_bin",
    "codex": "codex_bin",
    "claude-sub": "claude_bin",
    "kimi": "kimi_bin",
}

#: The builder lane choices; the reviewer is always the codex lane (another
#: vendor than every builder lane).
BUILDER_LANES = ("claude-sub", "devin", "kimi")
DEFAULT_BUILDER = "claude-sub"
REVIEW_LANE = "codex"


def lanes_for(builder: str) -> dict[str, int]:
    """``{lane: concurrency cap}`` enabled for a run."""
    return {builder: 3, REVIEW_LANE: 1}


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scripts/factory-rehearsal",
        description=(
            "Rehearse the dark-factory build engine against a scratch remote and "
            "a scratch database (never production). One result line per scenario."
        ),
    )
    p.add_argument(
        "--scenario",
        "-s",
        action="append",
        default=None,
        metavar="NAME",
        help="run this scenario (repeatable); default: every gate scenario",
    )
    p.add_argument("--list", action="store_true", help="list the scenarios and exit")
    p.add_argument(
        "--keep",
        action="store_true",
        help="leave the world, the worker and the database up for inspection",
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="remove this checkout's leftover rehearsal worlds, processes and databases, then exit",
    )
    p.add_argument(
        "--seed-ref",
        default="HEAD",
        help="ref of this repository seeded as the scratch remote's main (default %(default)s: "
        "the code the driver itself runs)",
    )
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help="seed even though src/ has uncommitted changes (the worker then runs the "
        "committed code and the driver the working tree): NON-GATING",
    )
    p.add_argument(
        "--venv-from",
        type=Path,
        default=None,
        help="checkout whose .venv seeds the host checkout's (default: this checkout)",
    )
    p.add_argument(
        "--builder-lane",
        choices=BUILDER_LANES,
        default=DEFAULT_BUILDER,
        help="the lane whose shim builds (default %(default)s); the codex lane reviews",
    )
    p.add_argument(
        "--passes",
        default="heartbeat,job_ssh_node,job_inproc",
        help="worker --only passes, comma separated (default %(default)s)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=240.0,
        help="seconds per engine step (default %(default)s)",
    )
    p.add_argument(
        "--follow",
        type=float,
        default=0.0,
        help="smoke: keep following the job this many seconds after the shim launched",
    )
    p.add_argument(
        "--uv-offline", action="store_true", help="run the worker's uv offline"
    )
    p.add_argument(
        "--lint",
        choices=("real", "off"),
        default="real",
        help=(
            "scripts/ship --quick's pre-qland lint on a land: 'real' runs it on the "
            "host (default); 'off' sets PRECIS_QLAND_LINT=0: NON-GATING"
        ),
    )
    return p.parse_args(argv)


def say(text: str) -> None:
    print(text, flush=True)


_T0 = time.monotonic()


def note(text: str) -> None:
    print(f"  . [{time.monotonic() - _T0:5.1f}s] {text}", file=sys.stderr, flush=True)


def _terminate(signum: int, frame: FrameType | None) -> None:
    """SIGTERM and SIGHUP end the run like Ctrl-C does: the teardown in
    ``main``'s ``finally`` still runs."""
    raise KeyboardInterrupt(f"signal {signum}")


def clean_all() -> int:
    """Remove this checkout's rehearsal processes, databases and run
    directories. Processes are found by their command lines (under a run
    directory), never from a recorded pid alone."""
    gone = 0
    runs = sorted(SCRATCH.glob("run-*")) if SCRATCH.is_dir() else []
    for run in runs:
        groups = procguard.end_run_processes(run)
        if groups:
            note(f"ended process group(s) {groups} of {run.name}")
    try:
        projects = dbmod.other_projects(dbmod.project_prefix(REPO_ROOT))
    except Refused as exc:
        note(str(exc))
        projects = []
    for project in projects:
        note(dbmod.remove_project(project))
        gone += 1
    for run in runs:
        shutil.rmtree(run, ignore_errors=True)
        gone += 1
    say(f"cleaned {gone} item(s)")
    return 0


def _save_artifacts(run_dir: Path) -> str:
    """Keep what explains a failed run (worker and driver logs, the shim's
    session records, the briefs it received, its scripts) under
    ``scratch/rehearsal/last-failure/`` before the world is removed."""
    dest = SCRATCH / "last-failure"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    for name in ("logs", "sessions", "briefs", "scripts"):
        src = run_dir / name
        if src.is_dir():
            shutil.copytree(src, dest / name)
    return f"failure artifacts kept in {dest}"


def resolve_seed_ref(ref: str, *, allow_dirty: bool) -> tuple[str, str, bool]:
    """``(ref to seed, its sha, dirty)``. The default seed is this checkout's
    ``HEAD``: the driver runs this checkout's working tree, so the worker must
    run the same committed code. Refuses a dirty ``src/`` unless
    ``allow_dirty`` (then ``dirty`` is true and the run is non-gating)."""
    probe = run_git(
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        REPO_ROOT,
        check=False,
    )
    if probe.returncode != 0:
        raise WorldError(f"seed ref {ref!r} does not resolve in {REPO_ROOT}")
    dirty = bool(
        run_git(
            ["status", "--porcelain", "--", "src"], REPO_ROOT, check=False
        ).stdout.strip()
    )
    if dirty and not allow_dirty:
        raise WorldError(
            "src/ has uncommitted changes: the worker would run the committed "
            "code and the driver the working tree; commit them or pass "
            "--allow-dirty (a non-gating run)"
        )
    return ref, probe.stdout.strip(), dirty


def run_scenario(ctx: Ctx, scenario: Scenario) -> tuple[str, str, list[str]]:
    """Run one scenario and, when it asks for it, the shared invariant
    checker. Returns ``(PASS|FAIL, one-line detail, extra lines)``."""
    assert scenario.run is not None
    started = time.monotonic()
    ctx.facts.clear()
    extra: list[str] = []
    if scenario.invariants:
        # Rule 9 reports only the holds this scenario leaves.
        ctx.slot_baseline = inv.slot_baseline(ctx.driver)
    try:
        detail = scenario.run(ctx)
    except (AssertionError, RuntimeError) as exc:
        text = str(exc) or type(exc).__name__
        lines = text.splitlines()
        extra += lines[1:14]
        extra += [f"| {ln}" for ln in ctx.driver.events[-12:]]
        tail = ctx.worker.log_tail(12) if ctx.worker else ""
        if tail:
            extra.append("worker log tail:")
            extra += [f"> {ln}" for ln in tail.splitlines()]
        return "FAIL", lines[0], extra
    notes: list[str] = []
    if scenario.invariants:
        ctx.settle_folders()  # a violation of rule 1 is reported by the checker
        violations, notes = inv.check(
            ctx.world, ctx.driver, ctx.expect, ctx.slot_baseline
        )
        if violations:
            return (
                "FAIL",
                f"{detail}; {len(violations)} invariant(s) violated",
                [f"invariant {v}" for v in violations] + [f"note: {n}" for n in notes],
            )
        detail += "; invariants hold"
    extra += [f"note: {n}" for n in notes]
    extra += [f"fact: {f}" for f in ctx.facts]
    return "PASS", f"{detail} ({time.monotonic() - started:.0f}s)", extra


#: Seconds the jobs of a failed scenario get to end once their plans are
#: retired and their processes stopped.
QUIESCE_SECONDS = 120.0


def quiesce(ctx: Ctx, *, timeout: float = QUIESCE_SECONDS) -> list[str]:
    """Bring the shared world to rest after a failed scenario, so what it left
    running does not run into the next one (refused-push and host-lost each
    pass alone and failed only behind an aborted scenario). Within the sealed
    world only: restart the worker when the scenario left it dead, retire
    every open plan of the run (each plan any scenario registered whose build
    is not done; the dispatcher mints nothing more for them), stop the
    scripted sessions and ships still running (never the worker's own group:
    :mod:`.procguard` signals only this run's groups), then wait until every
    job is over and the attempt folders settled. A step that fails is noted
    and the rest still run. Returns note lines; never raises."""
    notes: list[str] = []
    d = ctx.driver

    def restart() -> None:
        if ctx.worker is not None and not ctx.worker.alive():
            notes.append(
                f"quiesce: worker was dead, restarted in {ctx.restart_worker():.0f}s"
            )

    def retire() -> None:
        from precis.errors import NotFound

        for plan in ctx.plans:
            if d.status(plan.build) == "done":
                continue
            try:
                nodes = d.store.retire_todo_subtree(plan.root)
                notes.append(
                    f"quiesce: retired open plan td{plan.root} (+{nodes} nodes)"
                )
            except NotFound:
                pass

    def stop() -> None:
        keep: tuple[int, ...] = ()
        if ctx.worker is not None and ctx.worker.proc is not None:
            keep = (ctx.worker.proc.pid,)
        ended = procguard.end_run_processes(ctx.world.run_dir, exclude=keep)
        if ended:
            notes.append(f"quiesce: stopped process group(s) {ended}")

    def live() -> list[int]:
        return [
            j["id"]
            for j in d.jobs()
            if j["type"] != "plan_dispatch"
            and (
                j["status"] in ("queued", "running", "yielded", "paused")
                or not j["status"]
            )
        ]

    def wait() -> None:
        try:
            d.wait_until(
                lambda: not live(),
                timeout=timeout,
                what="the jobs of the failed scenario to end",
                tick_every=None,
                health=ctx.health,
            )
        except (AssertionError, RuntimeError) as exc:  # a timeout, a dead worker
            notes.append(f"quiesce: {str(exc).splitlines()[0]}; still live: {live()}")
        left = ctx.settle_folders(timeout=timeout)
        if left:
            notes.append(f"quiesce: attempt folder(s) still there: {left}")

    # Each step is tried whatever the one before it did.
    for label, step in (
        ("worker restart", restart),
        ("plan retirement", retire),
        ("process stop", stop),
        ("job wait", wait),
    ):
        try:
            step()
        except Exception as exc:  # the next scenario reports what this left
            notes.append(f"quiesce: {label} failed: {type(exc).__name__}: {exc}")
    for line in notes:
        note(line)
    return notes


def run_named(
    ctx: Ctx,
    names: list[str],
    *,
    explicit: bool,
    non_gating: list[str],
    results: list[tuple[str, str, str]],
) -> bool:
    """Run the named scenarios in order, appending to ``results``; after a FAIL
    that another scenario follows, :func:`quiesce` the world first. Returns
    whether any failed."""
    failed = False
    for index, name in enumerate(names):
        scenario = REGISTRY[name]
        if scenario.run is None:
            status = "FAIL" if explicit else "SKIP"
            failed = failed or explicit
            results.append((status, name, "not yet implemented"))
            say(f"{status} {name} - not yet implemented")
            continue
        status, detail, extra = run_scenario(ctx, scenario)
        failed = failed or status == "FAIL"
        results.append((status, name, detail))
        tag = " [non-gating]" if non_gating and status == "PASS" else ""
        say(f"{status} {name} - {detail.splitlines()[0] if detail else ''}{tag}")
        for line in extra:
            print(f"     {line}", file=sys.stderr)
        if status == "FAIL" and index < len(names) - 1:
            quiesce(ctx)
    return failed


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.clean:
        return clean_all()
    if args.list:
        for s in REGISTRY.values():
            say(
                f"{s.name:16} {'ready' if s.run else 'not yet implemented':20} {s.summary}"
            )
        return 0
    names = args.scenario or [n for n, sc in REGISTRY.items() if sc.default]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}; try --list", file=sys.stderr)
        return 2
    explicit = args.scenario is not None
    runnable = [n for n in names if REGISTRY[n].run is not None]
    results: list[tuple[str, str, str]] = []
    failed = False
    if not runnable:
        for n in names:
            status = "FAIL" if explicit else "SKIP"
            failed = failed or explicit
            results.append((status, n, "not yet implemented"))
        for status, n, detail in results:
            say(f"{status} {n} - {detail}")
        return 1 if failed else 0

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    run_id = time.strftime("%Y%m%d%H%M%S")
    run_dir = SCRATCH / f"run-{run_id}"
    # The scratch database is never anything but this run's own container.
    from .db import ScratchDb

    database: ScratchDb | None = None
    worker = None
    driver = None
    world = None
    non_gating: list[str] = []
    t0 = time.monotonic()
    try:
        ref, seed_sha, dirty = resolve_seed_ref(
            args.seed_ref, allow_dirty=args.allow_dirty
        )
        head_sha = run_git(["rev-parse", "HEAD"], REPO_ROOT).stdout.strip()
        if dirty:
            non_gating.append("src/ dirty")
        if args.lint == "off":
            non_gating.append("lint off")
        world, notes = build_world(run_dir, REPO_ROOT, ref, venv_from=args.venv_from)
        for n in notes:
            note(n)
        header = (
            f"rehearsal: seed {seed_sha[:12]} (checkout HEAD {head_sha[:12]}), "
            f"builder lane {args.builder_lane}, lint {args.lint}"
            + (f"; NON-GATING ({', '.join(non_gating)})" if non_gating else "")
        )
        say(header)
        database = ScratchDb.up(run_id, REPO_ROOT)
        note(f"scratch database {database.project} at {database.dsn}")
        from .worker import NODE, Worker, build_env

        env = build_env(
            world,
            database.dsn,
            uv_offline=args.uv_offline,
            extra={"PRECIS_QLAND_LINT": "0"} if args.lint == "off" else None,
        )
        note(database.migrate(world.python, env=env, cwd=world.run_dir))
        from .driver import Driver

        driver = Driver(world, database.dsn)
        lanes = lanes_for(args.builder_lane)
        driver.configure_lanes(lanes)
        worker = Worker(
            world, env, passes=tuple(p for p in args.passes.split(",") if p)
        )
        worker.start()
        note(
            f"worker pid {worker.proc.pid if worker.proc else '?'} ({', '.join(worker.passes)}); log {worker.log_path}"
        )
        want = BASE_TOKENS | {LANE_TOKENS[n] for n in lanes}

        def health() -> None:
            if not worker.alive():
                raise RuntimeError("the worker exited:\n" + worker.log_tail())

        driver.wait_until(
            lambda: want <= driver.advertised(),
            timeout=120,
            what=f"the host {NODE} to advertise {sorted(want)}",
            tick_every=None,
            health=health,
        )
        note(
            f"host {NODE} advertises {sorted(want)}; setup took {time.monotonic() - t0:.0f}s"
        )
        ctx = Ctx(
            world=world,
            driver=driver,
            health=health,
            step_timeout=args.timeout,
            follow=args.follow,
            worker=worker,
            builder_lane=args.builder_lane,
        )
        failed = (
            run_named(
                ctx, names, explicit=explicit, non_gating=non_gating, results=results
            )
            or failed
        )
    except (Refused, WorldError, RuntimeError) as exc:
        failed = True
        say(
            f"FAIL setup - {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
        )
        for line in str(exc).splitlines()[1:12]:
            print(f"     {line}", file=sys.stderr)
        if not isinstance(exc, (Refused, WorldError)):
            traceback.print_exc()
    except KeyboardInterrupt:
        failed = True
        say("FAIL setup - interrupted")
    finally:
        keep = args.keep and database is not None
        if driver is not None:
            driver.close()
        if not keep:
            if worker is not None:
                note(worker.stop())
            if world is not None:
                ended = procguard.end_run_processes(world.run_dir)
                if ended:
                    note(f"ended leftover process group(s) {ended}")
                if driver is not None:
                    (world.logs / "driver.log").write_text(
                        "\n".join(driver.events) + "\n", encoding="utf-8"
                    )
                if failed:
                    note(_save_artifacts(world.run_dir))
            if database is not None:
                note(database.down())
            if world is not None:
                shutil.rmtree(world.run_dir, ignore_errors=True)
        elif database is not None:
            say(f"kept: world {run_dir}")
            say(f"      database {database.dsn}  (compose project {database.project})")
            if worker is not None and worker.proc is not None:
                say(f"      worker pid {worker.proc.pid}, log {worker.log_path}")
            say("      remove it all with: scripts/factory-rehearsal --clean")
    if len(names) > 1 or failed:
        passed = sum(1 for s, _, _ in results if s == "PASS")
        skipped = sum(1 for s, _, _ in results if s == "SKIP")
        say(
            f"{passed} passed, {sum(1 for s, _, _ in results if s == 'FAIL')} failed, "
            f"{skipped} skipped of {len(results)}"
            + (f"; NON-GATING run ({', '.join(non_gating)})" if non_gating else "")
        )
    if failed:
        return 1
    return 3 if non_gating else 0


__all__ = ["GATE", "REGISTRY", "main", "parse_args"]
