"""Gate scenarios 4 to 6: a session killed mid-commit, a push the hub refuses,
a host that is lost before any push."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import steps
from .ctx import Ctx
from .fixtures import credential_shaped
from .gate import (
    TERMINAL,
    assert_landed_clean,
    hub_file,
    hub_subject,
    main_file,
    run_jobs,
    wait_handles_persisted,
    wait_job_ended,
    wait_job_started,
    wait_landed,
)
from .worker import NODE


def _job(ctx: Ctx, todo: int, index: int) -> dict[str, Any] | None:
    jobs = run_jobs(ctx, todo)
    return jobs[index] if len(jobs) > index else None


def _comments_mentioning(ctx: Ctx, todo: int, *needles: str) -> list[str]:
    return [c for c in ctx.driver.comments(todo) if all(n in c for n in needles)]


# ── 4. hard kill mid-commit ────────────────────────────────────────


def _hard_kill_round(ctx: Ctx, *, kill_worker: bool) -> str:
    d, w = ctx.driver, ctx.world
    tag = "worker" if kill_worker else "session"
    plan = ctx.register(d.mint_build(f"hard-kill-{tag}"))
    plan_c = ctx.register(d.mint_build(f"hard-kill-bystander-{tag}"))
    b = plan.build
    a_path, b_path, c_path = (f"docs/rehearsal/td{b}-{n}.md" for n in ("a", "b", "c"))
    bystander_path = f"docs/rehearsal/td{plan_c.build}-by.md"
    marker = w.tmp / f"hang-{b}.marker"
    marker.unlink(missing_ok=True)
    d.write_script(
        b,
        [
            steps.attempt(
                steps.write(a_path, "piece one\n"),
                steps.commit(f"docs(rehearsal): piece one (td{b})"),
                steps.write(
                    a_path, "tracked change in the killed commit\n", append=True
                ),
                steps.write(b_path, "untracked piece in the killed commit\n"),
                steps.hang_in_commit(
                    str(marker), f"docs(rehearsal): never made (td{b})"
                ),
            ),
            steps.attempt(
                *steps.builder(
                    c_path,
                    "piece three\n",
                    message=f"docs(rehearsal): piece three (td{b})",
                )
            ),
        ],
    )
    d.write_script(
        plan_c.build, [steps.attempt(*steps.builder(bystander_path, "bystander\n"))]
    )
    ctx.wait("the session to hold index.lock inside its commit", marker.exists)
    held = marker.read_text(encoding="utf-8")
    assert held.startswith("locked"), (
        f"the commit hook saw {held!r}, not a held index.lock"
    )
    rec = d.sessions(b)[0]
    tree = Path(rec["cwd"])
    assert (tree / ".git" / "index.lock").exists(), (
        "no index.lock in the session's clone"
    )
    host_head = w.host_git("rev-parse", "HEAD").stdout.strip()
    if kill_worker:
        # Before the session dies, so the worker is killed with the death unseen.
        wait_handles_persisted(ctx, b, plan_c.build)
    pgid = d.kill_session(b)
    killed_at = time.monotonic()
    ctx.note(f"td{b}: SIGKILL to process group {pgid} while index.lock was held")
    if kill_worker:
        ctx.kill_worker()
        time.sleep(2.0)
        ctx.restart_worker()
    j1 = wait_job_ended(ctx, b, 0)
    noticed = time.monotonic() - killed_at
    push = j1["meta"]["push"]
    assert push["state"] == "pushed" and push["wip"] is True, (
        f"the death was not saved to the hub: {push}"
    )
    ref1 = push["ref"]
    assert w.hub_refs(f"refs/factory/build/{b}/").get(ref1) == push["sha"]
    assert hub_subject(ctx, ref1).startswith("wip: snapshot"), hub_subject(ctx, ref1)
    got_a = hub_file(ctx, ref1, a_path) or ""
    assert "piece one" in got_a and "tracked change in the killed commit" in got_a, (
        f"the snapshot lacks the tracked change: {got_a!r}"
    )
    assert hub_file(ctx, ref1, b_path) == "untracked piece in the killed commit\n", (
        "the snapshot lacks the untracked file"
    )
    assert hub_file(ctx, f"{ref1}~1", a_path) == "piece one\n", (
        "the committed piece is not the parent"
    )
    assert tree.name not in ctx.settle_folders(b), (
        f"the dead session's folder is still there after the settle wait: {tree.name}"
    )
    assert w.host_git("rev-parse", "HEAD").stdout.strip() == host_head, (
        "the host checkout's HEAD moved"
    )
    assert not w.host_git("status", "--porcelain").stdout.strip(), (
        "the host checkout is dirty"
    )
    assert not (w.host / ".git" / "index.lock").exists(), (
        "the host checkout has an index.lock"
    )
    index_asks = (
        [t for t in d.open_asks() if t in (b, plan.root)]
        + _comments_mentioning(ctx, b, "index.lock")
        + _comments_mentioning(ctx, b, "index-lock")
    )
    assert not index_asks, f"an index-lock ask or comment appeared: {index_asks}"
    ctx.note(
        f"td{b} ({tag} variant): death noticed and saved in {noticed:.0f}s: {push['note'][:80]}"
    )
    j2 = wait_job_started(ctx, b, 1)
    start = j2["meta"].get("start") or {}
    assert start.get("sha") == push["sha"] and start.get("ref") == ref1, (
        f"attempt 2 start {start}, expected {push['sha'][:12]} on {ref1}"
    )
    wait_landed(ctx, plan, allow=(j1["id"],))
    wait_landed(ctx, plan_c)
    assert "tracked change in the killed commit" in (main_file(ctx, a_path) or ""), (
        "main lacks the killed commit's tracked change"
    )
    assert main_file(ctx, b_path) == "untracked piece in the killed commit\n", (
        "main lacks the untracked piece"
    )
    assert main_file(ctx, c_path) == "piece three\n", "main lacks the third piece"
    assert main_file(ctx, bystander_path) == "bystander\n", (
        "main lacks the bystander's change"
    )
    assert_landed_clean(ctx, plan)
    assert_landed_clean(ctx, plan_c)
    return f"{tag} kill: saved to hub in {noticed:.0f}s despite the stale lock, attempt 2 continued, both landed"


def hard_kill(ctx: Ctx) -> str:
    return "; ".join(_hard_kill_round(ctx, kill_worker=k) for k in (False, True))


# ── 5. refused push ────────────────────────────────────────────────


def _refused_fixed(ctx: Ctx) -> str:
    d = ctx.driver
    cred = credential_shaped()
    ctx.expect.secrets.append(cred)
    plan = ctx.register(d.mint_build("refused-fixed"))
    b = plan.build
    leak = f"docs/rehearsal/td{b}-leak.md"
    d.write_script(
        b,
        [
            steps.attempt(
                steps.write(leak, f"token {cred}\n"),
                steps.commit(f"docs(rehearsal): leaky (td{b})"),
                steps.rebase(),
                steps.check(),
                steps.push(
                    on_refused=[steps.write(leak, "token removed\n"), steps.amend()]
                ),
                steps.verify(),
            )
        ],
    )
    wait_landed(ctx, plan)
    rec = d.sessions(b)[0]
    pushes = [s for s in rec["steps"] if s["do"] == "push"]
    assert [p["refused"] for p in pushes] == [True, False], (
        f"push steps {[(p['rc'], p['refused']) for p in pushes]}"
    )
    assert (
        "precis-hub: refused" in pushes[0]["output"]
        and "secret-scan" in pushes[0]["output"]
    ), pushes[0]["output"]
    assert cred not in pushes[0]["output"], "the refusal echoed the credential"
    j1 = run_jobs(ctx, b)[0]
    assert j1["meta"]["push"]["state"] == "pushed", j1["meta"]["push"]
    assert main_file(ctx, leak) == "token removed\n", (
        f"main's {leak}: {main_file(ctx, leak)!r}"
    )
    assert_landed_clean(ctx, plan)
    return "refused once, fixed in the same session by amend, landed clean"


def _refused_kept(ctx: Ctx) -> str:
    from precis.alerts import list_open_alerts

    d, w = ctx.driver, ctx.world
    cred = credential_shaped()
    ctx.expect.secrets.append(cred)
    plan = ctx.register(d.mint_build("refused-kept"))
    b = plan.build
    leak = f"docs/rehearsal/td{b}-leak2.md"
    clean = f"docs/rehearsal/td{b}-clean.md"
    d.write_script(
        b,
        [
            steps.attempt(
                steps.write(leak, f"token {cred}\n"),
                steps.commit(f"docs(rehearsal): leaky, unpushed (td{b})"),
                steps.check(),
                steps.exit_(0, "Finished, but I did not push."),
            ),
            steps.attempt(
                *steps.builder(
                    clean,
                    "clean rebuild\n",
                    message=f"docs(rehearsal): clean rebuild (td{b})",
                )
            ),
        ],
    )
    wait_job_started(ctx, b, 0)
    j1 = wait_job_ended(ctx, b, 0)
    push = j1["meta"]["push"]
    assert push["state"] == "withheld", f"push state {push['state']!r}: {push}"
    folder = f"slice-td{b}-j{j1['id']}"
    ref1 = f"refs/factory/build/{b}/{j1['id']}"
    assert ref1 not in w.hub_refs(f"refs/factory/build/{b}/"), (
        "the hub holds a ref of the refused work"
    )
    assert folder in d.folders(b), f"the folder {folder} was not kept"
    ctx.expect.kept.add(folder)
    source = f"run_slice:kept:{NODE}"
    ctx.expect.alerts.add(source)
    ctx.wait(
        "the kept-folder comment",
        lambda: _comments_mentioning(ctx, b, folder, "kept"),
        tick_every=None,
    )
    alerts = [a for a in list_open_alerts(d.store) if a["source"] == source]
    assert len(alerts) == 1, (
        f"{len(alerts)} open {source} alert(s): {[a['title'] for a in alerts]}"
    )
    first_seen = alerts[0]["seen_count"]
    note = push.get("note", "")
    assert "secret" in note.lower(), (
        f"push note does not name the secret scan: {note!r}"
    )
    # What the task does next, and the settle passes on the way.
    outcome = ctx.wait(
        "the next step on the task (a new attempt or an ask)",
        lambda: (
            "re-dispatch"
            if len(run_jobs(ctx, b)) > 1
            else ("ask" if [t for t in d.open_asks() if t in (b, plan.root)] else None)
        ),
    )
    ctx.note(
        f"td{b} after a withheld push: {outcome}; meta.parked={d.meta(b).get('parked')}"
    )
    assert outcome == "re-dispatch", (
        f"the task did not simply start another attempt: {outcome}\n{d.describe(b, plan.review)}"
    )
    wait_landed(ctx, plan, allow=(j1["id"],))
    assert folder in d.folders(b), "the kept folder was dropped"
    kept_comments = _comments_mentioning(ctx, b, folder, "kept")
    assert len(kept_comments) == 1, (
        f"the kept comment was repeated: {len(kept_comments)}"
    )
    alerts2 = [a for a in list_open_alerts(d.store) if a["source"] == source]
    assert len(alerts2) == 1, f"{len(alerts2)} open {source} alerts after the sweeps"
    sweeps = (
        len(d.jobs(b, "run_slice"))
        + len(d.jobs(b, "land_slice"))
        + len(run_jobs(ctx, plan.review))
    )
    ctx.note(
        f"kept folder {folder}: after {sweeps} later submits the alert is still one (seen_count {first_seen} -> {alerts2[0]['seen_count']}), "
        f"the comment is still one"
    )
    assert main_file(ctx, clean) == "clean rebuild\n"
    assert main_file(ctx, leak) is None, "the refused work reached main"
    assert_landed_clean_except_folder(ctx, plan, folder)
    return f"withheld push kept {folder} (one comment, one alert), the task re-dispatched and landed"


def assert_landed_clean_except_folder(ctx: Ctx, plan: Any, folder: str) -> None:
    d = ctx.driver
    todo = plan.build
    assert not d.hub_refs(todo), f"hub refs left for td{todo}"
    assert not d.meta(todo).get("build_ref"), "build_ref not cleared"
    others = ctx.settle_folders(todo, keep=(folder,))
    assert not others, f"attempt folders left besides the kept one: {others}"
    lands = [j for j in d.jobs(todo, "land_slice") if j["status"] == "succeeded"]
    assert lands and d.meta(todo).get("build_floor") is not None


def refused_push(ctx: Ctx) -> str:
    return "; ".join((_refused_fixed(ctx), _refused_kept(ctx)))


# ── 6. host lost before any push ───────────────────────────────────


def _host_lost_round(ctx: Ctx, *, delete_folder: bool) -> str:
    d, w = ctx.driver, ctx.world
    tag = "disk gone" if delete_folder else "folder intact"
    plan = ctx.register(
        d.mint_build(f"host-lost-{'disk' if delete_folder else 'intact'}")
    )
    b = plan.build
    a_path, b_path, c_path = (f"docs/rehearsal/td{b}-{n}.md" for n in ("a", "b", "c"))
    marker = w.tmp / f"lost-{b}.marker"
    never = w.tmp / f"never-{b}"
    marker.unlink(missing_ok=True)
    first = [
        steps.write(a_path, "piece one\n"),
        steps.commit(f"docs(rehearsal): piece one (td{b})"),
    ]
    if not delete_folder:
        first.append(steps.write(b_path, "untracked piece\n"))
    first += [steps.touch(str(marker)), steps.wait_for(str(never), timeout=3600)]
    second = (
        steps.builder(
            a_path, "rebuilt piece one\n", message=f"docs(rehearsal): rebuilt (td{b})"
        )
        if delete_folder
        else steps.builder(
            c_path, "piece three\n", message=f"docs(rehearsal): piece three (td{b})"
        )
    )
    d.write_script(b, [steps.attempt(*first), steps.attempt(*second)])
    ctx.wait("the session to reach the point of no push", marker.exists)
    rec = d.sessions(b)[0]
    tree = Path(rec["cwd"])
    assert run_jobs(ctx, b)
    assert tree.is_dir() and not w.hub_refs(f"refs/factory/build/{b}/"), (
        "the hub already holds work"
    )
    d.kill_session(b)
    ctx.kill_worker()
    if delete_folder:
        import shutil

        shutil.rmtree(tree, ignore_errors=True)
        assert not tree.exists(), "could not delete the folder"
        ctx.note(f"td{b}: session killed, worker killed, folder {tree.name} deleted")
    else:
        assert tree.is_dir(), "the folder vanished"
        ctx.note(f"td{b}: session killed, worker killed, folder {tree.name} intact")
    time.sleep(2.0)
    began = time.monotonic()
    ctx.restart_worker()

    def ended() -> dict[str, Any] | None:
        j = _job(ctx, b, 0)
        return (
            j
            if j
            and j["status"] in TERMINAL
            and (j["meta"].get("push") or delete_folder)
            else None
        )

    done = ctx.wait(
        "the dead job to end after the restart", ended, tick_every=None, timeout=300
    )
    assert isinstance(done, dict)
    took = time.monotonic() - began
    events = [t[:140] for k, t in d.chunk_texts(done["id"])][-3:]
    ctx.note(
        f"td{b} ({tag}): job jo{done['id']} ended {done['status']} {took:.0f}s after the restart; last events: {events}"
    )
    if delete_folder:
        push = done["meta"].get("push")
        assert not push or push.get("state") != "pushed", (
            f"a push record for work whose disk is gone: {push}"
        )
        br = d.meta(b).get("build_ref")
        assert not br or br.get("ref") in w.hub_refs(f"refs/factory/build/{b}/"), (
            f"build_ref {br} names a ref the hub lacks"
        )
        assert not w.hub_refs(f"refs/factory/build/{b}/"), (
            "the hub holds a ref for work that never reached it"
        )
    else:
        push = done["meta"]["push"]
        assert push["state"] == "pushed" and push["wip"] is True, (
            f"orphan folder not saved: {push}"
        )
        ref1 = push["ref"]
        assert hub_file(ctx, ref1, b_path) == "untracked piece\n", (
            "the snapshot lacks the untracked file"
        )
        assert hub_file(ctx, f"{ref1}~1", a_path) == "piece one\n", (
            "the committed piece is not the snapshot's parent"
        )
        assert hub_subject(ctx, ref1).startswith("wip: snapshot"), hub_subject(
            ctx, ref1
        )
        ctx.note(f"td{b}: snapshot subject {hub_subject(ctx, ref1)!r}")
        assert tree.name not in ctx.settle_folders(b), (
            "the orphan folder was not settled"
        )
    j2 = wait_job_started(ctx, b, 1)
    start = j2["meta"].get("start")
    if delete_folder:
        assert not start, (
            f"attempt 2 started from {start} although nothing reached the hub"
        )
    else:
        assert start and start.get("ref") == done["meta"]["push"]["ref"], (
            f"attempt 2 start {start}"
        )
    wait_landed(ctx, plan, allow=(done["id"],))
    if delete_folder:
        assert main_file(ctx, a_path) == "rebuilt piece one\n", (
            f"main's {a_path}: {main_file(ctx, a_path)!r}"
        )
    else:
        assert (
            main_file(ctx, a_path) == "piece one\n"
            and main_file(ctx, b_path) == "untracked piece\n"
            and main_file(ctx, c_path) == "piece three\n"
        ), "main lacks a piece"
    assert_landed_clean(ctx, plan)
    return f"{tag}: dead job ended {took:.0f}s after the restart, attempt 2 {'from base' if delete_folder else 'from the saved snapshot'}, landed"


def host_lost(ctx: Ctx) -> str:
    return "; ".join(_host_lost_round(ctx, delete_folder=x) for x in (False, True))
