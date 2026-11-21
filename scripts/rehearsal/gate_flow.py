"""Gate scenarios 2 and 3: the reviewer sends the build back; the base moves
under a finished build (rebase conflict at land)."""

from __future__ import annotations

from typing import Any

from . import steps
from .ctx import Ctx
from .gate import (
    TERMINAL,
    assert_landed_clean,
    brief_has,
    fail_fast,
    first_job_ended,
    main_file,
    run_jobs,
    session_for_job,
    wait_job_ended,
    wait_job_started,
    wait_landed,
)
from .worker import NODE

# ── 2. the reviewer sends it back ──────────────────────────────────


def review_fix(ctx: Ctx) -> str:
    d = ctx.driver
    plan = ctx.register(d.mint_build("review-fix"))
    b, r = plan.build, plan.review
    a_path, fix_path = f"docs/rehearsal/td{b}-a.md", f"docs/rehearsal/td{b}-fix.md"
    finding = f"{a_path} lacks its companion file"
    d.write_script(
        b,
        [
            steps.attempt(
                *steps.builder(
                    a_path,
                    "first round\n",
                    message=f"docs(rehearsal): first round (td{b})",
                )
            ),
            steps.attempt(
                *steps.builder(
                    fix_path, "the fix\n", message=f"docs(rehearsal): the fix (td{b})"
                )
            ),
        ],
    )
    d.write_script(
        r,
        [
            steps.attempt(steps.verdict("FIX", finding, anchors=f"{a_path}::top")),
            steps.attempt(steps.verdict("LAND")),
        ],
    )
    rounds = ctx.wait(
        "the fix round to be counted",
        lambda: (d.meta(b).get("preland_fix_rounds") or {}).get("count"),
    )
    assert rounds == 1, (
        f"preland_fix_rounds.count is {rounds!r} after the first FIX, expected 1"
    )
    j1 = first_job_ended(ctx, b, 0)
    assert j1 is not None, "build job 1 has not ended although its review answered"
    ref1 = j1["meta"]["push"]["ref"]
    sha1 = j1["meta"]["push"]["sha"]
    j2 = wait_job_started(ctx, b, 1)
    start = j2["meta"].get("start") or {}
    assert (
        start.get("ref") == ref1
        and start.get("sha") == sha1
        and start.get("wip") is False
    ), f"fix job start {start}, expected {sha1[:12]} on {ref1}"
    assert j2["id"] != j1["id"]
    wait_landed(ctx, plan)
    rec1, rec2 = session_for_job(ctx, b, j1["id"]), session_for_job(ctx, b, j2["id"])
    assert rec1["cwd"] != rec2["cwd"] and rec2["cwd"].endswith(
        f"slice-td{b}-j{j2['id']}"
    ), "the fix did not run in a folder of its own"
    assert rec2["head0"] == sha1, (
        f"the fix started at {rec2['head0'][:12]}, not at the reviewed head {sha1[:12]}"
    )
    brief_has(ctx, b, 2, finding, "This attempt continues earlier work")
    assert len(run_jobs(ctx, b)) == 2, (
        f"td{b} ran {len(run_jobs(ctx, b))} builds, expected 2"
    )
    assert len(run_jobs(ctx, r)) == 2, (
        f"the reviewer ran {len(run_jobs(ctx, r))} times, expected 2"
    )
    assert (main_file(ctx, a_path), main_file(ctx, fix_path)) == (
        "first round\n",
        "the fix\n",
    ), "main lacks the first round or the fix"
    land_id = assert_landed_clean(ctx, plan)
    return (
        f"FIX counted once, fix built from {sha1[:8]} in a new folder, "
        f"then LAND, landed by jo{land_id}"
    )


# ── 3. rebase conflict at land ─────────────────────────────────────


def rebase_conflict(ctx: Ctx) -> str:
    d, w = ctx.driver, ctx.world
    shared = "docs/rehearsal/shared-conflict.md"
    base_tip = w.advance_remote_main(shared, "base\n", "docs(rehearsal): shared base")
    plan = ctx.register(d.mint_build("rebase-conflict"))
    b, r = plan.build, plan.review
    own = f"docs/rehearsal/td{b}-own.md"
    resolved = "base\nbuilder line\ncompetitor line\n"
    d.write_script(
        b,
        [
            steps.attempt(
                steps.write(shared, "base\nbuilder line\n"),
                steps.write(own, "the builder's own file\n"),
                steps.commit(f"docs(rehearsal): builder change (td{b})"),
                steps.rebase(),
                steps.check(),
                steps.push(),
                steps.verify(),
            ),
            steps.attempt(
                steps.rebase(conflict={shared: resolved}),
                steps.check(),
                steps.push(),
                steps.verify(),
            ),
        ],
    )
    # A second task whose review is held until the resolution is pushed: its
    # land must then wait for the pending resolution's land.
    plan_c = ctx.register(d.mint_build("conflict-bystander"))
    gate_file = str(w.tmp / f"c-gate-{plan_c.build}")
    c_path = f"docs/rehearsal/td{plan_c.build}-c.md"
    d.write_script(plan_c.build, [steps.attempt(*steps.builder(c_path, "bystander\n"))])
    d.write_script(
        plan_c.review,
        [steps.attempt(steps.wait_for(gate_file, timeout=900), steps.verdict("LAND"))],
    )
    wait_job_started(ctx, b, 0)
    j1 = wait_job_ended(ctx, b, 0)
    ref1, sha1 = j1["meta"]["push"]["ref"], j1["meta"]["push"]["head"]
    # The base moves between the push and the land, in the same file.
    moved = w.advance_remote_main(
        shared, "base\ncompetitor line\n", "docs(rehearsal): competing change"
    )
    ctx.note(f"main moved {base_tip[:8]} -> {moved[:8]} after td{b}'s push")

    def conflict_stamped() -> dict[str, Any] | None:
        fail_fast(ctx, plan, allow_types=("land_slice",))
        got = d.meta(b).get("conflict")
        return got if isinstance(got, dict) else None

    conflict = ctx.wait("the land's conflict stamp", conflict_stamped)
    assert isinstance(conflict, dict)
    assert shared in (conflict.get("files") or []), (
        f"meta.conflict.files {conflict.get('files')}, expected {shared}"
    )
    assert conflict.get("head") == sha1 and conflict.get("ref") == ref1, (
        f"meta.conflict {conflict}"
    )
    assert conflict.get("base_tip") == moved, (
        f"meta.conflict.base_tip {conflict.get('base_tip')} != {moved}"
    )
    assert conflict.get("at"), f"meta.conflict lacks at: {conflict}"
    # The stamp is written before the job's own status flips.
    ctx.wait(
        "the conflicted land to end red",
        lambda: [j for j in d.jobs(b, "land_slice") if j["status"] == "failed"],
        tick_every=None,
    )
    reds = [j for j in d.jobs(b, "land_slice") if j["status"] == "failed"]
    assert len(reds) == 1, f"expected one red land, found {[j['id'] for j in reds]}"
    ctx.note(f"land jo{reds[0]['id']} red on the conflict in {conflict['files']}")

    def conflict_job() -> dict[str, Any] | None:
        for j in run_jobs(ctx, b):
            if (j["meta"].get("params") or {}).get("conflict"):
                return j
        return None

    cj = ctx.wait("the conflict-only attempt", conflict_job)
    assert isinstance(cj, dict)
    cj_id = int(cj["id"])
    cp = cj["meta"]["params"]
    assert cp["conflict"].get("try") == 1, f"params.conflict {cp['conflict']}"
    assert cp.get("target_node") == NODE, f"conflict job params {cp}"
    assert cp["conflict"].get("head") == sha1 and shared in (
        cp["conflict"].get("files") or []
    ), f"params.conflict {cp['conflict']}"

    def resolution_pushed() -> bool:
        fail_fast(ctx, plan, allow_types=("land_slice",))
        j = next((x for x in run_jobs(ctx, b) if x["id"] == cj_id), None)
        return bool(
            j
            and j["status"] in TERMINAL
            and (j["meta"].get("push") or {}).get("state") == "pushed"
        )

    ctx.wait("the resolution to be pushed", resolution_pushed)
    cj = next(x for x in run_jobs(ctx, b) if x["id"] == cj_id)
    res_ref = cj["meta"]["push"]["ref"]
    assert res_ref != ref1, "the resolution reused the first attempt's ref"
    rec = session_for_job(ctx, b, cj["id"])
    assert rec["head0"] == sha1, (
        f"the conflict attempt started at {rec['head0'][:12]}, "
        f"not at the branch tip {sha1[:12]}"
    )
    ctx.note(f"conflict try 1 jo{cj['id']} pushed {res_ref}")
    with open(gate_file, "w", encoding="utf-8") as fh:
        fh.write("go")

    def resolution_review() -> dict[str, Any] | None:
        for j in run_jobs(ctx, r):
            if (j["meta"].get("params") or {}).get("resolution"):
                return j
        return None

    rj = ctx.wait("the resolution's reviewer", resolution_review)
    assert isinstance(rj, dict)
    res = rj["meta"]["params"]["resolution"]
    # plan_dispatch._resolution_param: ``of`` is the commit that failed to land
    # (meta.conflict.head, already reviewed) and ``ref`` the hub ref holding
    # that commit, i.e. the first attempt's, not the resolution's.
    assert res.get("of") == sha1 and res.get("ref") == ref1, (
        f"params.resolution {res}, expected of={sha1[:12]} ref={ref1}"
    )
    wait_landed(ctx, plan, allow_types=("land_slice",))
    wait_landed(ctx, plan_c)
    assert main_file(ctx, shared) == resolved, (
        f"main's {shared}: {main_file(ctx, shared)!r}"
    )
    assert main_file(ctx, own) == "the builder's own file\n", (
        "main lacks the builder's own file"
    )
    assert main_file(ctx, c_path) == "bystander\n", "main lacks the bystander's change"
    land_b = max(j["id"] for j in d.jobs(b, "land_slice") if j["status"] == "succeeded")
    land_c = max(
        j["id"]
        for j in d.jobs(plan_c.build, "land_slice")
        if j["status"] == "succeeded"
    )
    assert land_c > land_b, (
        f"the bystander's land jo{land_c} was minted before the resolution's land "
        f"jo{land_b} finished: the pending resolution did not hold it"
    )
    assert_landed_clean(ctx, plan)
    assert_landed_clean(ctx, plan_c)
    return (
        f"conflict in {shared}, try 1 resolved from {sha1[:8]}, resolution reviewed "
        f"(params.resolution), landed by jo{land_b}; the bystander's land jo{land_c} came after it"
    )
