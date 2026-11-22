"""The shared invariant checker: run after every scenario.

What must hold of the world whatever the scenario did, read from the scratch
database, the host checkout, the hub, the scratch remote and the worker log.
A scenario declares what it deliberately leaves behind in an
:class:`Expect`; everything else is a violation. The central rule (7) is the
one the cut-over exists for: every file a session wrote is on ``main``
(landed), reachable from a hub ref (unfinished) or in a kept folder that has
its comment and its alert; nothing a session wrote exists only in a folder
that was deleted.

:func:`check` returns ``(violations, notes)``: violations fail the scenario,
notes are reported (counts).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .driver import Driver
    from .world import World

#: Refs the hub may hold besides the attempt refs of tasks not yet landed.
HUB_BASE_PREFIXES = ("refs/heads/",)
BUILD_PREFIX = "refs/factory/build/"
KEEP_PREFIX = "refs/factory-keep/"

_LANDED = re.compile(r"^\s*landed:\s*([0-9a-f]{7,40})", re.IGNORECASE)
_SUBJECT_TODO = re.compile(r"\(td(\d+)\)")
_TRACEBACK = re.compile(r"^Traceback \(most recent call last\)", re.MULTILINE)

#: The multi-unit host rows a launch's footprint reserves (``admission``).
SLOT_RESOURCES = ("cpu_milli", "mem_mb")
#: Seconds a just-ended launch may take to release its holds before a hold
#: reads as leaked (the release runs after the child's exit, not with it).
HOLD_GRACE_SECONDS = 15.0


@dataclass
class Expect:
    """What one scenario declares."""

    #: Every build task the scenario ran (todo ids).
    tasks: set[int] = field(default_factory=set)
    #: The tasks that landed.
    landed: set[int] = field(default_factory=set)
    #: Build todo -> its pre-land reviewer todo.
    reviews: dict[int, int] = field(default_factory=dict)
    #: Attempt folder names that stay (kept, with comment and alert).
    kept: set[str] = field(default_factory=set)
    #: Alert sources that may be open at the end.
    alerts: set[str] = field(default_factory=set)
    #: Todos whose open ask is expected.
    asks: set[int] = field(default_factory=set)
    #: ``refs/factory-keep/*`` may exist (in the hub or the remote).
    keep_refs: bool = False
    #: Credential-shaped strings that must be in no object of hub or remote.
    secrets: list[str] = field(default_factory=list)
    #: The scenario stopped the worker on purpose and a log without a clean
    #: shutdown is expected (a killed worker leaves no traceback either).
    worker_killed: bool = False


def _git_lines(world: World, *args: str) -> list[str]:
    return [ln for ln in world.host_git(*args).stdout.splitlines() if ln.strip()]


def check_folders(world: World, expect: Expect) -> tuple[list[str], list[str]]:
    v: list[str] = []
    notes: list[str] = []
    names = [p.name for p in world.attempt_folders()]
    extra = sorted(set(names) - expect.kept)
    if extra:
        v.append(f"1: attempt folder(s) left that no scenario kept: {extra}")
    gone = sorted(expect.kept - set(names))
    if gone:
        v.append(f"1: folder(s) the scenario declared kept are gone: {gone}")
    return v, notes


def check_hub(world: World, expect: Expect) -> list[str]:
    v: list[str] = []
    refs = world.all_refs(world.hub) if (world.hub / "HEAD").is_file() else {}
    for ref in sorted(refs):
        if ref.startswith(BUILD_PREFIX):
            parts = ref.removeprefix(BUILD_PREFIX).split("/")
            if parts[0].isdigit() and int(parts[0]) in expect.landed:
                v.append(f"2: the hub still holds {ref} of a landed task")
        elif ref.startswith(KEEP_PREFIX):
            if not expect.keep_refs:
                v.append(f"2: unexpected {ref} in the hub")
        elif not ref.startswith(HUB_BASE_PREFIXES):
            v.append(f"2: unexpected hub ref {ref}")
    return v


def check_host(world: World) -> list[str]:
    v: list[str] = []
    status = _git_lines(world, "status", "--porcelain")
    if status:
        v.append(f"3: the host checkout is dirty: {status[:5]}")
    branch = world.host_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        v.append(f"3: the host checkout is on {branch!r}, not main")
    head = world.host_git("rev-parse", "HEAD").stdout.strip()
    if head != world.host_head0:
        anc = world.host_git("merge-base", "--is-ancestor", world.host_head0, head)
        if anc.returncode != 0:
            v.append(
                f"3: the host checkout's HEAD {head[:12]} is not a descendant of its "
                f"start {world.host_head0[:12]}"
            )
    trees = [
        ln
        for ln in _git_lines(world, "worktree", "list", "--porcelain")
        if ln.startswith("worktree ")
    ]
    if len(trees) != 1:
        v.append(f"3: git worktree list shows {len(trees)} entries: {trees}")
    slices = _git_lines(world, "branch", "--list", "slice/*")
    if slices:
        v.append(f"3: local slice branch(es) in the host checkout: {slices}")
    if (world.host / ".git" / "index.lock").exists():
        v.append("3: the host checkout has an index.lock")
    return v


def check_remote(world: World, expect: Expect) -> list[str]:
    v: list[str] = []
    refs = world.all_refs(world.remote)
    odd = sorted(set(refs) - {"refs/heads/main", "refs/heads/prod"})
    if odd:
        v.append(f"4: the scratch remote holds refs beyond main and prod: {odd}")
    if refs.get("refs/heads/prod") not in (None, world.seed_sha):
        v.append("4: prod on the scratch remote moved")
    allowed = set(world.competitors)
    log = world.git_remote(
        "log",
        "--first-parent",
        "--format=%H\t%P\t%s",
        f"{world.seed_sha}..refs/heads/main",
    )
    for line in log.stdout.splitlines():
        sha, _, rest = line.partition("\t")
        parents, _, subject = rest.partition("\t")
        if sha in allowed:
            continue
        m = _SUBJECT_TODO.findall(subject)
        if not m or not any(int(n) in expect.tasks for n in m):
            v.append(
                f"4: main carries a commit that is neither a land nor declared: {sha[:12]} {subject[:80]}"
            )
        elif len(parents.split()) != 1:
            v.append(
                f"4: the land {sha[:12]} is not one squash commit ({len(parents.split())} parents)"
            )
    return v


def check_landed(world: World, driver: Driver, expect: Expect) -> list[str]:
    """Rule 8: a land is a real one. Per landed task: the build todo is done,
    a ``landed: <sha>`` comment names a commit that is on the remote's main,
    its pre-land reviewer ran and left a LAND verdict from a job that ended
    before the land job that succeeded; and the host checkout's local main did
    not move (the engine's documented behaviour)."""
    v: list[str] = []
    if not expect.landed:
        return v
    for todo in sorted(expect.landed):
        status = driver.status(todo)
        if status != "done":
            v.append(f"8: landed td{todo} is {status!r}, not done")
        shas = [m.group(1) for c in driver.comments(todo) if (m := _LANDED.match(c))]
        if not shas:
            v.append(f"8: td{todo} has no 'landed: <sha>' comment")
        else:
            anc = world.git_remote(
                "merge-base", "--is-ancestor", shas[-1], "refs/heads/main"
            )
            if anc.returncode != 0:
                v.append(
                    f"8: td{todo}'s landed sha {shas[-1][:12]} is not on the remote's main"
                )
        review = expect.reviews.get(todo)
        if review is not None:
            if not driver.sessions(review):
                v.append(f"8: no reviewer session ran for td{review}")
            rec = driver.meta(review).get("review")
            lands = [
                j["id"]
                for j in driver.jobs(todo, "land_slice")
                if j["status"] == "succeeded"
            ]
            if (
                not isinstance(rec, dict)
                or str(rec.get("verdict", "")).upper() != "LAND"
            ):
                v.append(f"8: td{review} has no LAND verdict record: {rec}")
            elif lands and int(rec.get("job") or 0) > max(lands):
                v.append(
                    f"8: td{review}'s verdict job jo{rec.get('job')} came after the land jo{max(lands)}"
                )
    # The engine's documented behaviour (``land_slice._advance_local_main``): a
    # land's clone shares no main with the host checkout, so the host's local
    # main does NOT move with a land; the hub's copy of the base is what the
    # next build and land start from.
    host_main = world.host_git("rev-parse", "refs/heads/main").stdout.strip()
    if host_main != world.host_head0:
        v.append(
            f"8: the host checkout's local main moved to {host_main[:12]} with a land "
            "(the engine keeps it where it was)"
        )
    return v


def check_secrets(world: World, expect: Expect) -> list[str]:
    if not expect.secrets:
        return []
    v: list[str] = []
    for label, git_dir in (("hub", world.hub), ("remote", world.remote)):
        if not (git_dir / "HEAD").is_file() and not (git_dir / "objects").is_dir():
            continue
        hits = world.objects_containing(git_dir, expect.secrets)
        if hits:
            v.append(
                f"5: a credential-shaped string is in {len(hits)} object(s) of the {label}: {hits[:3]}"
            )
    return v


def check_database(
    world: World, driver: Driver, expect: Expect, *, since_job: int = 0
) -> list[str]:
    v: list[str] = []
    log = world.logs / "worker.log"
    if log.is_file():
        text = log.read_text(encoding="utf-8", errors="replace")
        if _TRACEBACK.search(text):
            at = _TRACEBACK.search(text)
            assert at is not None
            v.append(
                f"6: the worker log has a traceback: {text[at.start() : at.start() + 400]!r}"
            )
    for job in driver.jobs():
        if job["type"] == "plan_dispatch":
            continue
        if (
            job["status"] in ("queued", "running", "yielded", "paused")
            or not job["status"]
        ):
            v.append(
                f"6: jo{job['id']} {job['type']} is still {job['status'] or 'unstated'}"
            )
    from precis.alerts import list_open_alerts

    sources = {a["source"] for a in list_open_alerts(driver.store)}
    stray = sorted(s for s in sources if s not in expect.alerts)
    if stray:
        v.append(f"6: open alert(s) the scenario did not declare: {stray}")
    for s in sorted(expect.alerts - sources):
        v.append(f"6: the declared alert {s} is not open")
    for todo in driver.open_asks():
        if todo not in expect.asks:
            v.append(f"6: td{todo} carries an open ask the scenario did not declare")
    return v


def _tree_paths(git_dir: Path, rev: str) -> set[str]:
    from .world import run_git

    proc = run_git(
        ["--git-dir", str(git_dir), "ls-tree", "-r", "--name-only", rev], check=False
    )
    return set(proc.stdout.split("\n")) if proc.returncode == 0 else set()


def written_paths(driver: Driver, todo: int) -> set[str]:
    """Paths the sessions of ``todo`` wrote through the shim, minus those
    whose last act was a deletion (a scripted removal)."""
    last: dict[str, str] = {}
    for rec in driver.sessions(todo):
        for step in rec.get("steps", []):
            if step.get("do") in ("write", "delete") and step.get("path"):
                last[str(step["path"])] = str(step["do"])
    return {p for p, how in last.items() if how == "write"}


def check_central(world: World, driver: Driver, expect: Expect) -> list[str]:
    """Rule 7: nothing a session wrote exists only in a deleted folder."""
    v: list[str] = []
    main_paths = _tree_paths(world.remote, "refs/heads/main")
    for todo in sorted(expect.tasks):
        paths = written_paths(driver, todo)
        if not paths:
            continue
        hub_paths: set[str] = set()
        for ref in world.hub_refs(f"{BUILD_PREFIX}{todo}/"):
            hub_paths |= _tree_paths(world.hub, ref)
        kept_paths: set[str] = set()
        for name in expect.kept:
            if name.startswith(f"slice-td{todo}-"):
                folder = world.folders / name
                kept_paths |= {
                    str(p.relative_to(folder))
                    for p in folder.rglob("*")
                    if p.is_file() and ".git" not in p.parts
                }
        for path in sorted(paths):
            if path in main_paths or path in hub_paths or path in kept_paths:
                continue
            v.append(
                f"7: td{todo} wrote {path}; it is on main, in no hub ref and in no kept folder"
            )
    # A kept folder must have its comment and its alert.
    from precis.alerts import list_open_alerts

    sources = {a["source"] for a in list_open_alerts(driver.store)}
    for name in sorted(expect.kept):
        m = re.match(r"slice-td(\d+)-", name)
        if m is None:
            continue
        todo = int(m.group(1))
        if not any(name in c for c in driver.comments(todo)):
            v.append(f"7: kept folder {name} has no comment on td{todo}")
        if not any(s.startswith("run_slice:kept:") for s in sources):
            v.append(f"7: kept folder {name} has no run_slice:kept alert")
    return v


def describe_holds(holds: list[dict[str, Any]]) -> list[str]:
    """One line per holder and acquisition time: the holder, its cpu_milli
    and mem_mb units, its acquired_at."""
    grouped: dict[tuple[str, Any], dict[str, int]] = {}
    for h in holds:
        slot = grouped.setdefault((h["holder"], h["acquired_at"]), {})
        slot[h["resource"]] = slot.get(h["resource"], 0) + int(h["units"])
    return [
        f"holder={holder} cpu_milli={units.get('cpu_milli', 0)} "
        f"mem_mb={units.get('mem_mb', 0)} acquired_at={at}"
        for (holder, at), units in grouped.items()
    ]


@dataclass(frozen=True)
class SlotBaseline:
    """The slot state at a scenario's start: the holds already there (ids) and
    each row's shortfall (capacity - free), so rule 9 reports only what the
    scenario itself leaked."""

    holds: frozenset[int] = frozenset()
    short: dict[tuple[str, str], int] = field(default_factory=dict)


def slot_baseline(driver: Driver) -> SlotBaseline:
    return SlotBaseline(
        holds=frozenset(int(h["id"]) for h in driver.slot_holds(SLOT_RESOURCES)),
        short={
            (r["host"], r["resource"]): r["capacity"] - r["free"]
            for r in driver.slot_rows(SLOT_RESOURCES)
        },
    )


def check_slot_holds(
    driver: Driver,
    *,
    baseline: SlotBaseline | None = None,
    grace: float = HOLD_GRACE_SECONDS,
) -> list[str]:
    """Rule 9: at a scenario's end, with no live launch, no cpu_milli or
    mem_mb hold is left that the scenario's start did not have, and
    ``free == capacity`` for those rows beyond their start shortfall.
    Whatever is still held was not released by its launch, as when a
    SIGKILLed worker's spawn never ran its release (gr762788). Against the
    baseline one leak fails only the scenario that caused it; without one
    every hold counts. Polls up to ``grace`` seconds for a release in
    flight."""
    base = baseline or SlotBaseline()
    deadline = time.monotonic() + grace

    def leftover() -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], int]]]:
        holds = [
            h for h in driver.slot_holds(SLOT_RESOURCES) if h["id"] not in base.holds
        ]
        short = []
        for r in driver.slot_rows(SLOT_RESOURCES):
            was = base.short.get((r["host"], r["resource"]), 0)
            extra = (r["capacity"] - r["free"]) - was
            if extra > 0:
                short.append((r, extra))
        return holds, short

    holds, short = leftover()
    while (holds or short) and time.monotonic() < deadline:
        time.sleep(1.0)
        holds, short = leftover()
    v: list[str] = []
    if holds:
        v.append(
            f"9: {len(holds)} resource_slot_hold(s) left with no launch live "
            "(a launch did not release its footprint): "
            + "; ".join(describe_holds(holds))
        )
    for r, extra in short:
        v.append(
            f"9: {r['host']} {r['resource']} free {r['free']} != capacity "
            f"{r['capacity']} with no launch live ({extra} unit(s) not returned)"
        )
    return v


def check(
    world: World,
    driver: Driver,
    expect: Expect,
    baseline: SlotBaseline | None = None,
) -> tuple[list[str], list[str]]:
    """All nine invariants (rule 9 against ``baseline``, the slot state at the
    scenario's start). Returns ``(violations, notes)``."""
    violations: list[str] = []
    f_v, notes = check_folders(world, expect)
    violations += f_v
    violations += check_hub(world, expect)
    violations += check_host(world)
    violations += check_remote(world, expect)
    violations += check_secrets(world, expect)
    violations += check_database(world, driver, expect)
    violations += check_central(world, driver, expect)
    violations += check_landed(world, driver, expect)
    violations += check_slot_holds(driver, baseline=baseline)
    return violations, notes


def summarize(violations: list[str]) -> str:
    return "invariants violated:\n  " + "\n  ".join(violations) if violations else ""


def describe_state(world: World) -> dict[str, Any]:
    """A small picture of the world for failure messages."""
    return {
        "folders": [p.name for p in world.attempt_folders()],
        "hub_builds": sorted(world.hub_refs()),
        "remote_main": world.remote_tip("main")[:12],
    }
