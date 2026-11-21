"""The six gate scenarios beyond the happy path (gr741695, phase 2).

Each is a function ``run(ctx) -> str``: it writes per-task session scripts
(:mod:`.steps`), lets the real engine run, and asserts on database state,
hub refs, the scratch remote and the attempt folders. It raises
:class:`AssertionError` with the evidence when the engine did something else;
the runner then runs the shared invariant checker (:mod:`.invariants`).

Assertions read meta keys, refs, file content and job/task state, not prose.
"""

from __future__ import annotations

from typing import Any

from . import steps
from .ctx import Ctx
from .driver import Plan
from .worker import NODE

TERMINAL = ("succeeded", "failed", "cancelled")


# ── shared helpers ─────────────────────────────────────────────────


def failed_jobs(
    ctx: Ctx,
    *todos: int,
    allow: tuple[int, ...] = (),
    allow_types: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    out = []
    for todo in todos:
        for job in ctx.driver.jobs(todo):
            if (
                job["status"] == "failed"
                and job["id"] not in allow
                and job["type"] not in allow_types
            ):
                out.append(job)
    return out


def fail_fast(
    ctx: Ctx,
    plan: Plan,
    *,
    allow: tuple[int, ...] = (),
    allow_types: tuple[str, ...] = (),
) -> None:
    """Raise when a job of the plan failed that the scenario did not expect."""
    bad = failed_jobs(
        ctx, plan.build, plan.review, allow=allow, allow_types=allow_types
    )
    if bad:
        raise AssertionError(
            f"jo{bad[0]['id']} {bad[0]['type']} failed unexpectedly\n"
            + ctx.driver.describe(plan.build, plan.review)
        )


def run_jobs(ctx: Ctx, todo: int) -> list[dict[str, Any]]:
    return ctx.driver.jobs(todo, "run_slice")


def land_done(ctx: Ctx, plan: Plan) -> bool:
    return any(
        j["status"] == "succeeded" for j in ctx.driver.jobs(plan.build, "land_slice")
    )


def wait_landed(
    ctx: Ctx,
    plan: Plan,
    *,
    allow: tuple[int, ...] = (),
    allow_types: tuple[str, ...] = (),
    timeout: float | None = None,
) -> None:
    def done() -> bool:
        fail_fast(ctx, plan, allow=allow, allow_types=allow_types)
        return land_done(ctx, plan)

    try:
        ctx.wait(f"the land of td{plan.build}", done, timeout=timeout)
    except AssertionError as exc:
        text = str(exc)
        if "\n" not in text or "td" + str(plan.build) not in text:
            text += "\n" + ctx.driver.describe(plan.build, plan.review)
        raise AssertionError(text) from exc
    ctx.landed(plan)


def main_file(ctx: Ctx, path: str) -> str | None:
    proc = ctx.world.git_remote("show", f"main:{path}")
    return proc.stdout if proc.returncode == 0 else None


def hub_file(ctx: Ctx, ref: str, path: str) -> str | None:
    from .world import run_git

    proc = run_git(
        ["--git-dir", str(ctx.world.hub), "show", f"{ref}:{path}"], check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def hub_subject(ctx: Ctx, ref: str) -> str:
    from .world import run_git

    return run_git(
        ["--git-dir", str(ctx.world.hub), "log", "-1", "--format=%s", ref], check=False
    ).stdout.strip()


def assert_landed_clean(ctx: Ctx, plan: Plan) -> int:
    """After a land: hub refs and ``build_ref`` cleared, ``build_floor`` is
    the land job's id, no ``conflict`` left. Returns the land job id."""
    d = ctx.driver
    todo = plan.build
    assert not d.hub_refs(todo), (
        f"hub refs left for td{todo}: {sorted(d.hub_refs(todo))}"
    )
    meta = d.meta(todo)
    assert not meta.get("build_ref"), f"build_ref not cleared: {meta.get('build_ref')}"
    assert not meta.get("conflict"), f"conflict not cleared: {meta.get('conflict')}"
    assert not meta.get("conflict_tries"), (
        f"conflict_tries not cleared: {meta.get('conflict_tries')}"
    )
    lands = [j for j in d.jobs(todo, "land_slice") if j["status"] == "succeeded"]
    assert lands, f"no succeeded land of td{todo}"
    floor = meta.get("build_floor")
    assert floor is not None and int(floor) == lands[-1]["id"], (
        f"build_floor is {floor!r}, the land job is jo{lands[-1]['id']}"
    )
    left = ctx.settle_folders(todo)
    assert not left, f"attempt folders left for td{todo} after the settle wait: {left}"
    return int(lands[-1]["id"])


def first_job_ended(ctx: Ctx, todo: int, index: int = 0) -> dict[str, Any] | None:
    """The ``index``-th run_slice job of ``todo`` once it is terminal and its
    ``meta.push`` is stamped, else ``None``."""
    jobs = run_jobs(ctx, todo)
    if len(jobs) <= index:
        return None
    job = jobs[index]
    if job["status"] in TERMINAL and job["meta"].get("push"):
        return job
    return None


def wait_job_started(ctx: Ctx, todo: int, index: int = 0) -> dict[str, Any]:
    def started() -> dict[str, Any] | None:
        jobs = run_jobs(ctx, todo)
        if len(jobs) <= index:
            return None
        job = jobs[index]
        # ``meta.start`` is stamped once the attempt's clone is cut (``None``
        # for the base tip), so its presence means the start is known.
        return job if "start" in job["meta"] or job["status"] in TERMINAL else None

    got = ctx.wait(f"run_slice job #{index + 1} of td{todo} to start", started)
    assert isinstance(got, dict)
    return got


def wait_handles_persisted(ctx: Ctx, *todos: int) -> None:
    """Wait until every ``run_slice`` job of ``todos`` exists and has its
    ``meta.compute_handle`` (or is over). A worker killed between
    ``submit()`` and that persist leaves the job failed "submit outcome
    unknown ... not resubmitting" by design (gr191673), which is a different
    scenario from the one rehearsed here: kill the worker only when no job is
    in that window."""

    def persisted() -> bool:
        for todo in todos:
            jobs = run_jobs(ctx, todo)
            if not jobs:
                return False
            for job in jobs:
                if (
                    job["status"] not in TERMINAL
                    and "compute_handle" not in job["meta"]
                ):
                    return False
        return True

    ctx.wait("the run_slice jobs to hold their compute handles", persisted)


def wait_job_ended(ctx: Ctx, todo: int, index: int = 0) -> dict[str, Any]:
    """Wait for the job to end and its push to be stamped, without ticking
    (so the dispatcher cannot start the next attempt before the caller reads
    the state this job left)."""
    got = ctx.wait(
        f"run_slice job #{index + 1} of td{todo} to end",
        lambda: first_job_ended(ctx, todo, index),
        tick_every=None,
    )
    assert isinstance(got, dict)
    return got


def brief_has(ctx: Ctx, todo: int, attempt: int, *needles: str) -> None:
    text = ctx.driver.brief(todo, attempt)
    for needle in needles:
        assert needle in text, (
            f"the brief of td{todo} attempt {attempt} lacks {needle!r}"
        )


def session_for_job(ctx: Ctx, todo: int, job_id: int) -> dict[str, Any]:
    for rec in ctx.driver.sessions(todo):
        if str(rec.get("job")) == str(job_id):
            return rec
    raise AssertionError(f"no session record of td{todo} for jo{job_id}")


# ── 1. builder cannot build ────────────────────────────────────────


def _wip_round(ctx: Ctx, *, exit_code: int) -> str:
    d, w = ctx.driver, ctx.world
    plan = ctx.register(d.mint_build(f"wip-exit{exit_code}"))
    b = plan.build
    a_path, b_path, c_path = (f"docs/rehearsal/td{b}-{n}.md" for n in ("a", "b", "c"))
    first = [
        steps.write(a_path, "piece one\n"),
        steps.commit(f"docs(rehearsal): piece one (td{b})"),
        steps.write(a_path, "tracked change left uncommitted\n", append=True),
        steps.write(b_path, "untracked piece left behind\n"),
    ]
    if exit_code == 0:
        # A clean exit with a commit message in scratch/ would be committed
        # by the worker (poll step 4); this builder leaves no message.
        first += [
            steps.delete("scratch/commit-msg.txt"),
            steps.exit_(0, "Cannot finish: the parser is not written; edits left."),
        ]
    else:
        first.append(steps.exit_(exit_code, "The session failed."))
    d.write_script(
        b,
        [
            steps.attempt(*first),
            steps.attempt(
                *steps.builder(
                    c_path,
                    "piece three\n",
                    message=f"docs(rehearsal): piece three (td{b})",
                )
            ),
        ],
    )
    # Attempt 1: tick until its job exists, then let it end without a tick.
    j1 = wait_job_started(ctx, b, 0)
    j1 = wait_job_ended(ctx, b, 0)
    push = j1["meta"]["push"]
    ref1 = f"refs/factory/build/{b}/{j1['id']}"
    assert push["state"] == "pushed", f"job 1 push state {push['state']!r}: {push}"
    assert push["wip"] is True, f"job 1 push is not a wip snapshot: {push}"
    assert push["ref"] == ref1, f"job 1 pushed to {push['ref']}, expected {ref1}"
    sha1 = push["sha"]
    assert w.hub_refs(f"refs/factory/build/{b}/").get(ref1) == sha1, (
        "the hub does not hold job 1's ref at the recorded sha"
    )
    assert hub_subject(ctx, ref1).startswith("wip: snapshot"), hub_subject(ctx, ref1)
    tracked = hub_file(ctx, ref1, a_path) or ""
    assert "piece one" in tracked and "tracked change left uncommitted" in tracked, (
        f"the snapshot lacks the tracked change: {tracked!r}"
    )
    assert hub_file(ctx, ref1, b_path) == "untracked piece left behind\n", (
        "the snapshot lacks the untracked file"
    )
    parent_a = hub_file(ctx, f"{ref1}~1", a_path) or ""
    assert parent_a == "piece one\n", (
        f"the committed piece is not the snapshot's parent: {parent_a!r}"
    )
    meta = d.meta(b)
    build_ref = meta.get("build_ref") or {}
    if build_ref.get("ref") == ref1:
        assert build_ref.get("wip") is True and build_ref.get("host") == NODE, (
            f"build_ref {build_ref}"
        )
        ctx.note(f"td{b}: build_ref wip=True host={build_ref.get('host')} after job 1")
    else:
        ctx.note(
            f"td{b}: build_ref already moved on when read ({build_ref.get('ref')}); job meta.push checked instead"
        )
    folder1 = f"slice-td{b}-j{j1['id']}"
    left = ctx.settle_folders(b)
    assert folder1 not in left, f"the folder of job 1 is still there: {folder1}"
    assert j1["status"] in TERMINAL, f"job 1 is {j1['status']}"
    ctx.note(f"td{b}: exit {exit_code}, job 1 ended {j1['status']}")
    # Attempt 2: dispatched by the next ticks, from the hub.
    j2 = wait_job_started(ctx, b, 1)
    assert j2["id"] != j1["id"]
    p2 = j2["meta"].get("params") or {}
    assert p2.get("target_node") == NODE, f"job 2 params {p2}"
    assert j2["meta"].get("hub_pin"), f"job 2 carries no hub_pin: {sorted(j2['meta'])}"
    start = j2["meta"].get("start") or {}
    assert (
        start.get("sha") == sha1
        and start.get("ref") == ref1
        and start.get("wip") is True
    ), f"job 2 start {start}, expected {sha1[:12]} on {ref1}"
    wait_landed(ctx, plan, allow=(j1["id"],))
    rec2 = session_for_job(ctx, b, j2["id"])
    assert rec2["head0"] == sha1, (
        f"attempt 2 started at {rec2['head0'][:12]}, not the wip snapshot {sha1[:12]}"
    )
    assert (
        rec2["cwd"].endswith(f"slice-td{b}-j{j2['id']}")
        and rec2["cwd"] != session_for_job(ctx, b, j1["id"])["cwd"]
    )
    brief_has(
        ctx,
        b,
        2,
        "This attempt continues earlier work",
        "git reset --soft HEAD~1",
        sha1[:12],
    )
    got_a = main_file(ctx, a_path) or ""
    assert "piece one" in got_a and "tracked change left uncommitted" in got_a, (
        f"main's {a_path}: {got_a!r}"
    )
    assert main_file(ctx, b_path) == "untracked piece left behind\n", (
        f"main lacks {b_path}"
    )
    assert main_file(ctx, c_path) == "piece three\n", f"main lacks {c_path}"
    land_id = assert_landed_clean(ctx, plan)
    return f"exit {exit_code}: wip {sha1[:8]} on {ref1.rsplit('/', 1)[0]}/{j1['id']}, attempt 2 from it, landed by jo{land_id}"


def builder_wip(ctx: Ctx) -> str:
    return "; ".join(_wip_round(ctx, exit_code=c) for c in (0, 3))
