"""The scenario registry.

A scenario is a function ``run(ctx) -> str`` that drives one situation
through the real engine and asserts the outcome in the scratch database, the
hub and the scratch remote; it returns a short detail for the result line and
raises :class:`AssertionError` (or :class:`~.driver.Timeout`) when the
engine did something else. ``run=None`` marks a scenario whose script is not
written yet: the runner reports it ``SKIP`` (or ``FAIL`` when it was
selected by name), never ``PASS``.

``smoke`` is phase 1's proof: a tick mints a ``run_slice`` job for one task
and the scripted session is launched for it and receives a brief. The seven
named scenarios are the rehearsal gate Reto ruled (phase 2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import gate, gate_crash, gate_flow
from .ctx import Ctx
from .driver import Driver

Runner = Callable[[Ctx], str]


@dataclass(frozen=True)
class Scenario:
    name: str
    summary: str
    run: Runner | None
    #: Part of a run that names no scenario.
    default: bool = True
    #: The shared invariant checker runs after it.
    invariants: bool = True


def smoke(ctx: Ctx) -> str:
    """Phase 1: a tick mints a ``run_slice`` job for one task; the shim is
    launched for it and receives a brief."""
    d = ctx.driver
    plan = d.mint_build("smoke")
    first = ctx.wait(
        f"a scripted session for build td{plan.build}",
        lambda: next(iter(d.sessions(plan.build)), None),
    )
    assert isinstance(first, dict)
    todo = plan.build
    jobs = d.jobs(todo, "run_slice")
    assert jobs, f"a session ran for td{todo} but no run_slice job is parented to it"
    brief = d.brief(todo, 1)
    assert brief.strip(), f"the shim received an empty brief for td{todo}"
    assert f"td{todo}" in brief, f"the brief does not name td{todo}"
    folder = Path(str(first.get("cwd", ""))).name
    detail = (
        f"td{todo}: run_slice jo{jobs[0]['id']} launched the {first.get('lane')} shim "
        f"(pid {first.get('pid')}) in {folder}; brief {len(brief)} bytes"
    )
    if ctx.follow > 0:
        end = ctx.driver.wait_until(
            lambda: (
                d.status(jobs[0]["id"]) in ("succeeded", "failed", "cancelled")
                and d.status(jobs[0]["id"])
            ),
            timeout=ctx.follow,
            what=f"jo{jobs[0]['id']} to finish",
            health=ctx.health,
        )
        detail += f"; job then {end}"
    return detail


def run_review_jobs(d: Driver, review: int) -> list[dict[str, Any]]:
    return d.jobs(review, "run_slice")


def happy_path(ctx: Ctx) -> str:
    d = ctx.driver
    w = ctx.world
    before = w.remote_tip("main")
    plan = ctx.register(d.mint_build("happy-path"))
    path = f"docs/rehearsal/td{plan.build}.md"

    def landed() -> bool:
        # An attempt-free admission / ship-busy deferral is the engine
        # waiting, not a failure: the tick re-mints the land.
        for job in gate.failed_jobs(ctx, plan.build):
            raise AssertionError(
                f"jo{job['id']} {job['type']} failed\n{d.describe(plan.build, plan.review)}"
            )
        return any(j["status"] == "succeeded" for j in d.jobs(plan.build, "land_slice"))

    try:
        ctx.wait(f"the land of td{plan.build}", landed)
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n{d.describe(plan.build, plan.review)}") from exc
    after = w.remote_tip("main")
    ctx.landed(plan)
    assert after != before, "the scratch remote's main did not move"
    shown = w.git_remote("show", f"main:{path}")
    assert shown.returncode == 0, f"main does not carry {path}: {shown.stderr.strip()}"
    left = ctx.settle_folders(plan.build)
    assert not left, f"attempt folders left after the settle wait: {left}"
    assert not d.hub_refs(plan.build), f"hub refs left: {d.hub_refs(plan.build)}"
    assert not d.meta(plan.build).get("build_ref"), "build_ref not cleared"
    parent = w.git_remote("rev-parse", f"{after}^").stdout.strip()
    assert parent == before, (
        f"the land is not one squash commit on {before[:8]}: parent {parent[:8]}"
    )
    reviews = run_review_jobs(d, plan.review)
    assert reviews and d.sessions(plan.review), (
        "no reviewer session ran for the plan's review"
    )
    lands = [
        j["id"] for j in d.jobs(plan.build, "land_slice") if j["status"] == "succeeded"
    ]
    verdict = d.meta(plan.review).get("review") or {}
    assert str(verdict.get("verdict", "")).upper() == "LAND", (
        f"review verdict record {verdict}"
    )
    assert int(verdict.get("job") or 0) < min(lands), (
        "the LAND verdict came after the land job"
    )
    return f"main {before[:8]} -> {after[:8]} carries {path}"


def _later(name: str, summary: str) -> Scenario:
    return Scenario(name=name, summary=summary, run=None)


REGISTRY: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(
            "smoke",
            "a tick mints a run_slice job and the shim receives its brief",
            smoke,
            default=False,
            invariants=False,
        ),
        Scenario(
            "happy-path",
            "build, review LAND, land: main has the change, folders and refs gone",
            happy_path,
        ),
        Scenario(
            "builder-wip",
            "builder cannot build: the worker pushes a wip snapshot, the next attempt continues from it",
            gate.builder_wip,
        ),
        Scenario(
            "review-fix",
            "reviewer sends it back: a new attempt continues from the pushed ref, then LAND",
            gate_flow.review_fix,
        ),
        Scenario(
            "rebase-conflict",
            "main moves between build and land: conflict-only attempt, only the resolution reviewed",
            gate_flow.rebase_conflict,
        ),
        Scenario(
            "hard-kill",
            "SIGKILL mid-commit (and the worker too): finalize saves to the hub despite the stale lock",
            gate_crash.hard_kill,
        ),
        Scenario(
            "refused-push",
            "credential-shaped string: (a) fixed in session, (b) left: folder kept, comment and alert",
            gate_crash.refused_push,
        ),
        Scenario(
            "host-lost",
            "worker and session die before any push: orphan finalized, or the disk is gone and the task rebuilds",
            gate_crash.host_lost,
        ),
    )
}

#: The seven scenarios Reto's acceptance gate names, in order.
GATE: tuple[str, ...] = (
    "happy-path",
    "builder-wip",
    "review-fix",
    "rebase-conflict",
    "hard-kill",
    "refused-push",
    "host-lost",
)
