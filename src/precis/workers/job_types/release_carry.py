"""The automatic carry (gr741489): a corrective commit onto ``release/r<N>``.

The deploy gate holds a release whose engine land was cleared by a FIX or
REVERT until the corrective commit is on ``release/r<N>``
(:func:`precis.workers.job_types.release_poll.deploy_review_blockers`). A
corrective that landed on main after the cut used to wait there for a
hand cherry-pick or an answered carry ask — r341 stopped twice on it,
2026-11-20. This module does the step the ask names: a plain
``git cherry-pick -x`` of each missing corrective onto the release branch,
pushed as a fast-forward. It adds reviewed commits to the branch and
removes no review hold: the gate judges the branch copy by the main
land's review, as it does a hand pick.

Two halves:

* :func:`settle` runs inside ``release_poll``'s open-pass tick, where the
  carry hold used to file its ask at once. It mints ONE carry node under
  the pass root for every corrective the release still misses, and tells
  the gate per hold whether the ask is still due: not while a carry is in
  flight or about to be minted, yes with the reason once the automatic
  carry cannot be done.
* :func:`carry` is the node's job. Like the merge-back it is no job type
  of its own but ``release_confirm``'s ``carry`` mode: the plan tick pins
  it to the host the cut ran on, and it runs synchronously in the job's
  submit — a fetch, the picks, a push: seconds.

The pick is made in a throwaway detached worktree cut from the freshly
fetched release head; no existing worktree is touched. Every picked
commit must replay as an exact copy of its source
(``release_poll._replays_as``, the proof the gate itself uses for a
carried commit), and the tree's history must be exactly the release head
plus those picks, so what lands is what was reviewed. A conflict, a failed
proof, a merge commit or a git failure pushes nothing and ends the carry
with a recorded reason; the carry ask then files with that reason. The
push is a plain fast-forward (:func:`precis.workers.factory_git_mirror.push_cas`
— never a force, never a lease); a release head that moved under it is
retried on the new head, then left for the next tick.

One carry of a release runs at a time: :func:`carry` holds a per-release
advisory lock for its whole run, and an entry that does not get it does
nothing — it touches no tree and records no outcome. The worktree is
named for the run (the node and the process), so even a second entry
past the lock could not pick into the first's tree; trees a dead run
left behind are removed by name once the lock is held. A node that
already recorded a carry is not carried twice.

Nothing is carried once the release's deploy started
(:func:`precis.plans.release.deploy_started`), once the carry's pass
closed (a queued carry must not push after its pass ended — a drop, a
cancel; td741722), for a hold whose carry ask
was answered (an answer waives the carry), or while ``release.auto_carry``
is off — off restores the ask-at-once behaviour exactly. The last look at
all four and the push share the pass root's row lock, the lock
``mark_deploy_started`` takes: a deploy cannot stamp its start between
the look and the push. After a carry the job puts the pass on the new
head at once (``release_poll.follow_moved_head``), so the head's verdict
is polled without waiting for the next tick.

Credential handling follows the lander, not the older merge-back: local
git runs credential-free with hooks off (``run_slice._git``), and the two
network steps — the fetch and the push — run in a fresh ops gitdir
checked against the ``ls-remote`` oracle
(:mod:`precis.workers.factory_git_mirror`), because the checkout's config
is writable by builder sessions.

A failed carry is not retried on the same release head: the node's
recorded outcome names the head it was tried on, and :func:`settle` mints
a new carry only after the head moved. A corrective the branch already
holds (an empty pick) is skipped, and never tried again. A fault — a
fetch or a push that did not answer, a job that ended with no outcome —
is no verdict on the commit: :func:`settle` tries it again on the next
tick, :data:`_FAULT_TRIES` times on one head, then asks. A release head
that keeps moving under the push is bounded the same way, per release.

The carry is an optional step whose fallback is the ask, so it never
holds a pass open. The job ends ``succeeded`` whatever the outcome (a
failed job would climb the node's retry cap into an ask on the pass), and
:func:`sweep` closes a carry node that has nothing left to do — the
deploy started, or the gate stopped waiting for its commits while the
node sat undispatched.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from precis.plans import release as _release
from precis.store.advisory import try_xact_advisory_lock
from precis.todo import TERMINAL_TODO_STATUSES
from precis.workers import factory_git as _factory_git
from precis.workers import factory_git_mirror as _mirror

log = logging.getLogger(__name__)

NAME = "release_carry"

#: The DB-only switch (``precis.settings.REGISTRY``), default on.
SETTING_KEY = "release.auto_carry"

#: ``meta`` key on a carry node: ``{rN, shas, head, outcome?}`` — the
#: release, the corrective shas (12 chars) it was minted for, the release
#: head at mint, and what its job ended with (:func:`carry`).
CARRY_KEY = "release_carry"

#: How long an open carry node may hold the carry ask back. The tick
#: dispatches the node within a pass or two and the job takes seconds; a
#: node still open after this is not going to finish by itself (a refused
#: dispatch, a spent retry budget), and a silent hold is what gr702442
#: was filed for.
CARRY_WAIT_AFTER = timedelta(minutes=30)

#: Picks re-made on a release head that moved under the push before the
#: job leaves the carry to the next tick.
_PUSH_TRIES = 3

#: Carries that may end in a fault (a fetch or a push that did not
#: answer, a job that left no outcome) on one release head before the ask
#: files instead of another; also how many carries of one release may
#: lose the race for its head (``moved``).
_FAULT_TRIES = 3

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: Serializes the open-carry re-read and the mint ("rel_carr").
_CARRY_LOCK_KEY = 0x72656C5F63617272

#: The per-release run lock's namespace (int4 "carr"); the second key is
#: ``hashtext(rN)``. One carry of a release runs at a time.
_RUN_LOCK_NS = 0x63617272

#: A hung holder loses the run lock after this long — far above a run (a
#: fetch, a few picks, a push).
_RUN_LOCK_MAX = "1800s"

#: The carry worktrees' name stem under ``<root>/.claude/worktrees/``;
#: a run's tree is ``<stem>-<rN>-<node>-<pid>``.
_TREE_STEM = "release-carry"

#: Outcomes :func:`carry` returns without recording them on the node:
#: another run holds the lock, or the node already recorded its carry.
UNRECORDED_STATES = frozenset({"busy", "settled"})

#: Statuses that close a carry node.
_CLOSED: frozenset[str] = frozenset(TERMINAL_TODO_STATUSES | {"cancelled"})

#: Outcome states that end a carry for good: the branch holds the change
#: as far as git can tell, so a second pick could only come out empty.
_SETTLED_STATES = frozenset({"carried", "noop"})


def enabled(store: Any) -> bool:
    """``release.auto_carry`` read strictly: unset is the registry default
    (on); a failed read or an unrecognised value is off — the ask-at-once
    behaviour, never an unasked push."""
    from precis import settings

    try:
        raw = settings._db_read_strict(store, SETTING_KEY)
    except Exception:
        log.warning("%s: settings read failed; reading off", SETTING_KEY, exc_info=True)
        return False
    if raw is None:
        return settings.REGISTRY[SETTING_KEY].default is True
    return settings._coerce_bool(raw, SETTING_KEY) is True


def _same(a: str, b: str) -> bool:
    """Two sha spellings name one commit (``release_poll._same_sha``)."""
    a, b = a.lower(), b.lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


@dataclass(frozen=True)
class CarryNode:
    """One carry node under a pass root, as :func:`settle` reads it."""

    id: int
    shas: tuple[str, ...]
    head: str
    status: str
    created_at: datetime | None
    outcome: dict[str, Any]

    @property
    def closed(self) -> bool:
        return self.status in _CLOSED


def carry_nodes(store: Any, root: int, rn: str, *, conn: Any = None) -> list[CarryNode]:
    """The carry nodes of release ``rn`` under the pass ``root``, oldest
    first."""
    sql = """
        SELECT r.ref_id, r.meta, r.created_at,
               (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                 WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1)
          FROM refs r
         WHERE r.kind = 'todo' AND r.retired_at IS NULL AND r.parent_id = %s
           AND r.meta -> %s IS NOT NULL
         ORDER BY r.ref_id
    """
    if conn is not None:
        rows = conn.execute(sql, (root, CARRY_KEY)).fetchall()
    else:
        with store.pool.connection() as own:
            rows = own.execute(sql, (root, CARRY_KEY)).fetchall()
    out: list[CarryNode] = []
    for ref_id, meta, created_at, status in rows:
        spec = (meta or {}).get(CARRY_KEY)
        if not isinstance(spec, dict) or str(spec.get("rN") or "") != rn:
            continue
        shas = tuple(str(s).lower() for s in spec.get("shas") or () if s)
        outcome = spec.get("outcome")
        if created_at is not None and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        out.append(
            CarryNode(
                id=int(ref_id),
                shas=shas,
                head=str(spec.get("head") or "").lower(),
                status=str(status or "open"),
                created_at=created_at,
                outcome=outcome if isinstance(outcome, dict) else {},
            )
        )
    return out


def pass_root(store: Any, rn: str) -> int | None:
    """The open release pass whose plan recorded the cut ``rn`` (the
    newest, should a dropped pass share the number)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id,
                   (SELECT s.value FROM ref_tags rs JOIN tags s USING (tag_id)
                     WHERE rs.ref_id = r.ref_id AND s.namespace = 'STATUS' LIMIT 1)
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'release-pass'
               AND r.meta -> 'release' ->> 'rN' = %s
             ORDER BY r.ref_id DESC
            """,
            (rn,),
        ).fetchall()
    return next(
        (int(rid) for rid, status in rows if (status or "open") not in _CLOSED), None
    )


def open_carry(store: Any, rel: dict[str, Any]) -> int | None:
    """The id of the carry node still open for the release ``rel``, else
    ``None`` — what the deploy's blocker line names while the factory is
    at it."""
    rn = str(rel.get("rN") or "")
    root = pass_root(store, rn) if rn else None
    if root is None:
        return None
    live = [n for n in carry_nodes(store, root, rn) if not n.closed]
    return live[-1].id if live else None


# ── the tick's half ────────────────────────────────────────────────


@dataclass
class Settled:
    """What :func:`settle` decided for one tick. ``reasons`` maps each
    hold's missing shas to: ``None`` — the factory carries it, no ask;
    ``""`` — the automatic carry does not apply, ask as before; a
    sentence — the carry cannot be done, ask and say why."""

    notes: list[str] = field(default_factory=list)
    reasons: dict[tuple[str, ...], str | None] = field(default_factory=dict)


def _sha_state(nodes: list[CarryNode], sha: str, head: str, rn: str) -> tuple[str, str]:
    """``(verb, why)`` for one missing corrective with no carry in
    flight: ``mint`` a carry, ``wait`` (a carry pushed it and this tick's
    scan predates the push), or ``ask`` with the reason."""
    prior = next(
        (n for n in reversed(nodes) if any(_same(sha, s) for s in n.shas)), None
    )
    if prior is None:
        return "mint", ""
    out = prior.outcome
    state = str(out.get("state") or "")
    if state == "moved":
        # The head moves with every try, so this is counted per release.
        moved = sum(1 for n in nodes if n.outcome.get("state") == "moved")
        if moved < _FAULT_TRIES:
            return "mint", ""
        return "ask", (
            f"td{prior.id}: release/{rn} moved under the push in {moved} carries"
        )
    if state in _SETTLED_STATES:
        base = str(out.get("base") or "")
        if state == "carried" and base and _same(base, head):
            return "wait", ""
        pushed = str(out.get("pushed") or "")
        skipped = out.get("skipped")
        why = str((skipped if isinstance(skipped, dict) else {}).get(sha) or "")
        if not why and state == "carried":
            why = f"it pushed the pick as {pushed[:12]}"
        if not why:
            why = str(out.get("why") or "it found nothing to pick")
        return "ask", (
            f"td{prior.id}: {why}, yet the gate still does not find {sha} "
            f"on release/{rn}"
        )
    tried_on = str(out.get("base") or prior.head)
    if tried_on and not _same(tried_on, head):
        return "mint", ""  # the release head moved since: one more try
    why = str(out.get("why") or f"it closed {prior.status} with no recorded outcome")
    if state in ("failed", ""):
        # A fault, or a job that left no outcome (it found another run
        # holding the lock, or died): no verdict on the commit — a few
        # tries on this head.
        faults = sum(
            1
            for n in nodes
            if str(n.outcome.get("state") or "") in ("failed", "")
            and any(_same(sha, s) for s in n.shas)
            and _same(str(n.outcome.get("base") or n.head), head)
        )
        if faults < _FAULT_TRIES:
            return "mint", ""
        why = f"{why} ({faults} tries)"
    return "ask", f"td{prior.id}: {why}"


def _mint(
    store: Any, root: int, rn: str, shas: list[str], head: str
) -> tuple[int, bool]:
    """``(carry node id, minted)`` — one open carry per release: the
    re-read and the mint share an advisory lock in one transaction, so
    two ticks cannot both mint."""
    from precis.plans import templates as _templates

    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CARRY_LOCK_KEY,))
        live = [n for n in carry_nodes(store, root, rn, conn=conn) if not n.closed]
        if live:
            return live[-1].id, False
        return _templates.mint_release_carry(store, root, rn, shas, head), True


def settle(
    store: Any,
    rel: dict[str, Any],
    wanted: list[list[str]],
    *,
    head: str | None,
    now: datetime,
    dry_run: bool,
    blocked: str | None = None,
) -> Settled:
    """Decide the carry holds of the release ``rel`` for this tick.

    ``wanted`` is one list of missing corrective shas (12 chars) per hold
    whose carry ask is unanswered; ``head`` the release head the gate's
    scan resolved; ``blocked`` why the plan tick dispatches nothing
    (``plan_dispatch.read_only_reason``). Mints at most one carry node,
    for every corrective that may be carried now, and returns the
    per-hold verdicts (:class:`Settled`)."""
    from precis.workers.job_types import run_slice as _rs

    out = Settled()
    keys = [tuple(m) for m in wanted]

    def every(reason: str | None) -> Settled:
        out.reasons = dict.fromkeys(keys, reason)
        return out

    if not keys:
        return out
    rn = str(rel.get("rN") or "")
    if not enabled(store) or _rs.factory_off():
        return every("")
    root = pass_root(store, rn)
    if root is None or _release.deploy_started(store, root) is not None:
        return every("")
    if blocked:
        return every(f"the automatic carry is not running ({blocked})")
    if not head:
        return every(f"the automatic carry could not read release/{rn}'s head")
    nodes = carry_nodes(store, root, rn)
    live = [n for n in nodes if not n.closed]
    if live:
        node = live[-1]
        age = now - node.created_at if node.created_at is not None else timedelta(0)
        if age >= CARRY_WAIT_AFTER:
            mins = int(age.total_seconds() // 60)
            last = str(node.outcome.get("why") or "")
            return every(
                f"the automatic carry td{node.id} has not finished after {mins} min"
                + (f" (last: {last})" if last else "")
            )
        out.notes.append(
            f"engine review: td{node.id} is carrying {', '.join(node.shas)} "
            f"onto release/{rn} — no carry ask"
        )
        return every(None)
    to_mint: list[str] = []
    for key in keys:
        whys: list[str] = []
        for sha in key:
            verb, why = _sha_state(nodes, sha, head, rn)
            if verb == "ask":
                whys.append(why)
            elif verb == "mint" and sha not in to_mint:
                to_mint.append(sha)
        out.reasons[key] = (
            "the automatic carry failed — " + "; ".join(dict.fromkeys(whys))
            if whys
            else None
        )
    if not to_mint:
        return out
    shown = ", ".join(to_mint)
    if dry_run:
        out.notes.append(f"engine review: would carry {shown} onto release/{rn}")
        return out
    try:
        node_id, fresh = _mint(store, root, rn, to_mint, head)
    except Exception as exc:
        log.warning("%s: carry mint for %s raised", NAME, rn, exc_info=True)
        why = f"the automatic carry could not be minted ({type(exc).__name__})"
        for key in keys:
            if out.reasons[key] is None:
                out.reasons[key] = why
        return out
    out.notes.append(
        f"engine review: minted td{node_id} to carry {shown} onto release/{rn}"
        if fresh
        else f"engine review: td{node_id} is already carrying onto release/{rn}"
    )
    return out


def sweep(
    store: Any,
    rel: dict[str, Any],
    wanted: list[str],
    *,
    now: datetime,
    dry_run: bool,
) -> list[str]:
    """Close the open carry nodes of ``rel``'s release that have nothing
    left to do, and say so. A carry is an optional step with the ask as
    its fallback, so it must never be what keeps a pass open: a node the
    tick could not dispatch (a refused pin, the factory switched off on
    the host) would otherwise hold the pass root — and with it the next
    release — long after the release deployed.

    Closed ``won't-do``: every open carry node once the release's deploy
    started, and one past :data:`CARRY_WAIT_AFTER` none of whose commits
    the gate still waits for (``wanted`` — carried by hand, or the carry
    ask answered). A node the gate still needs stays; its ask is already
    filed by then. Runs whatever ``release.auto_carry`` says: the nodes
    may be from before it was switched off."""
    from precis.store.types import Tag
    from precis.workers.job_types import ci_verdict_poll as _cvp

    rn = str(rel.get("rN") or "")
    root = pass_root(store, rn) if rn else None
    if root is None:
        return []
    live = [n for n in carry_nodes(store, root, rn) if not n.closed]
    if not live:
        return []
    started = _release.deploy_started(store, root)
    notes: list[str] = []
    for node in live:
        if started is not None:
            why = f"{started}; nothing more is carried"
        else:
            age = now - node.created_at if node.created_at is not None else timedelta(0)
            if age < CARRY_WAIT_AFTER or any(
                _same(s, w) for s in node.shas for w in wanted
            ):
                continue
            why = "the gate no longer waits for its commits"
        if dry_run:
            notes.append(f"engine review: would close carry td{node.id} — {why}")
            continue
        _cvp._node_comment(store, node.id, f"release_poll: closed — {why}.")
        store.add_tag(
            node.id,
            Tag.closed("STATUS", "won't-do"),
            set_by="system",
            replace_prefix=True,
        )
        notes.append(f"engine review: closed carry td{node.id} — {why}")
    return notes


# ── the job's half ─────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Seam: credential-free local ``git <args>`` in ``cwd`` with hooks
    off (``run_slice._git``); never raises."""
    from precis.workers.job_types import run_slice as _rs

    try:
        return _rs._git(args, cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 127, stdout="", stderr=str(exc))


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or "").strip()


def _err(proc: subprocess.CompletedProcess[str]) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1][:200] if lines else f"exit {proc.returncode}"


def _auth_env() -> dict[str, str]:
    """The env of the two network steps: the scrubbed git env plus the
    factory credential (the lander's wiring)."""
    from precis.workers.job_types import run_slice as _rs

    env = _factory_git.git_auth_env(base=_rs._git_env())
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _origin_url(root: Path, env: dict[str, str]) -> str | None:
    """Seam: the trusted origin URL of the checkout ``root``."""
    return _mirror.github_url(root, env=env)


def _objects(root: Path) -> Path | None:
    """The object store the checkout and its worktrees share."""
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root)
    path = _out(common)
    return Path(path) / "objects" if common.returncode == 0 and path else None


def _record(store: Any, todo_id: int, outcome: dict[str, Any]) -> dict[str, Any]:
    """Stamp ``outcome`` on the carry node (its own meta — never the
    shared ``meta.release``, which other writers replace whole)."""
    ref = store.get_ref(kind="todo", id=todo_id)
    spec = (ref.meta or {}).get(CARRY_KEY) if ref is not None else None
    stamped = {**outcome, "at": datetime.now(UTC).strftime(_TS_FMT)}
    if isinstance(spec, dict):
        store.stamp_ref_meta(todo_id, {CARRY_KEY: {**spec, "outcome": stamped}})
    return outcome


def _plan_picks(
    root: Path, shas: list[str], base: str, main_tip: str, rn: str
) -> tuple[list[tuple[str, str]], dict[str, str], str | None]:
    """``(picks, skipped, refusal)`` — the correctives to pick as
    ``(short, full sha)`` in main's order, oldest first; the ones the
    release head already contains; or why the set cannot be carried."""
    skipped: dict[str, str] = {}
    picks: list[tuple[str, str]] = []
    for short in shas:
        full = _out(_git(["rev-parse", "--verify", "-q", f"{short}^{{commit}}"], root))
        if not full:
            return [], skipped, f"corrective {short} is not a commit this checkout has"
        if _git(["merge-base", "--is-ancestor", full, main_tip], root).returncode != 0:
            return [], skipped, f"corrective {short} is not on origin/main"
        if _git(["merge-base", "--is-ancestor", full, base], root).returncode == 0:
            skipped[short] = f"release/{rn} already contains it"
            continue
        parents = _out(_git(["rev-list", "--parents", "-n", "1", full], root)).split()
        if len(parents) != 2:
            return (
                [],
                skipped,
                (f"corrective {short} is a merge or a root commit — not a plain pick"),
            )
        picks.append((short, full))
    if len(picks) > 1:
        fork = _out(_git(["merge-base", main_tip, base], root))
        listing = _git(
            ["rev-list", "--reverse", "--topo-order", f"{fork}..{main_tip}"], root
        )
        order = {sha: i for i, sha in enumerate(_out(listing).split())}
        if (
            not fork
            or listing.returncode != 0
            or any(f not in order for _s, f in picks)
        ):
            return [], skipped, "main's order of the correctives could not be read"
        picks.sort(key=lambda p: order[p[1]])
    return picks, skipped, None


def _empty_pick(tree: Path) -> bool:
    """The stopped cherry-pick changed nothing: no unmerged path, nothing
    staged, nothing modified — the branch already holds the change."""
    if _git(["rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"], tree).returncode != 0:
        return False
    status = _git(["status", "--porcelain", "--untracked-files=no"], tree)
    return status.returncode == 0 and not _out(status)


def carry_wanted(store: Any, rel: dict[str, Any]) -> tuple[list[str], str | None]:
    """Seam: the corrective shas the gate still wants on ``rel``'s
    branch, read fresh (``release_poll.carry_wanted``)."""
    from precis.workers.job_types import release_poll as _rp

    return _rp.carry_wanted(store, rel)


@contextmanager
def _run_lock(store: Any, rn: str) -> Iterator[bool]:
    """Whether this entry holds release ``rn``'s run lock — a
    transaction-scoped advisory lock on its own connection, held until
    the ``with`` block ends. ``False`` when another run holds it, and
    also when it cannot be asked for (a store without a DSN, no lock
    connection): a carry pushes, so it never runs unguarded."""
    dsn = getattr(store, "dsn", None)
    if not dsn:
        yield False
        return
    try:
        cm = try_xact_advisory_lock(
            dsn,
            _RUN_LOCK_NS,
            text_key=rn,
            idle_timeout=_RUN_LOCK_MAX,
            application_name="precis release_carry",
        )
        held = cm.__enter__()
    except psycopg.Error as exc:
        log.warning("%s: run lock unavailable (%s) — not carrying", NAME, exc)
        yield False
        return
    try:
        yield held
    finally:
        cm.__exit__(None, None, None)


def _tree_home(root: Path) -> Path:
    return root / ".claude" / "worktrees"


def _drop_tree(root: Path, tree: Path) -> None:
    _git(["worktree", "remove", "--force", str(tree)], root)
    shutil.rmtree(tree, ignore_errors=True)


def _reap_trees(root: Path, rn: str) -> None:
    """Remove the carry worktrees of release ``rn`` a dead run left
    behind — by name, and only called with the run lock held, when no
    live run of this release can own one."""
    home = _tree_home(root)
    stale = sorted(home.glob(f"{_TREE_STEM}-{rn}-*")) if home.is_dir() else []
    for tree in stale:
        _drop_tree(root, tree)
    if stale:
        _git(["worktree", "prune"], root)


def _root_status(store: Any, ref_id: int) -> str:
    """The ref's STATUS tag — ``open`` when it carries none."""
    return next(
        (str(t.value) for t in store.tags_for(ref_id) if t.prefix == "STATUS"),
        "open",
    )


def _last_look(
    store: Any,
    todo_id: int,
    rel: dict[str, Any],
    picked: dict[str, str],
    base: str,
    pass_id: int,
) -> dict[str, Any] | None:
    """The outcome that stops the push, else ``None`` — read with the
    pass root's row lock held, right before the push: the switch, the
    pass still being open (a carry queued while the pass was open must
    not push after the pass ended — a drop, a cancel), the deploy start,
    and the gate once more (an answer waives the carry, and a commit
    that reached the branch another way needs none)."""
    rn = str(rel["rN"])
    if not enabled(store):
        return {"state": "refused", "why": f"{SETTING_KEY} is off", "base": base}
    status = _root_status(store, pass_id)
    if status in _CLOSED:
        return {
            "state": "refused",
            "why": f"the pass td{pass_id} is {status} — it takes no more commits",
            "base": base,
        }
    started = _release.deploy_started(store, todo_id)
    if started is not None:
        return {
            "state": "refused",
            "why": f"{started} — it takes no more commits",
            "base": base,
        }
    wanted, fatal = carry_wanted(store, rel)
    if fatal is not None:
        return {"state": "failed", "why": fatal, "base": base}
    gone = [s for s in picked if not any(_same(s, w) for w in wanted)]
    if gone:
        return {
            "state": "refused",
            "why": f"the gate no longer waits for {', '.join(gone)} on "
            f"release/{rn} (its ask was answered, or it reached the branch) "
            "— nothing pushed",
            "base": base,
        }
    return None


def carry(store: Any, todo_id: int, root: Path) -> dict[str, Any]:
    """Carry the node ``todo_id``'s correctives onto ``release/r<N>`` from
    the checkout ``root``; the outcome, also stamped on the node.

    ``state`` is ``carried`` (``pushed`` the new release head, ``picked``
    the ``{corrective: branch commit}`` map), ``noop`` (nothing left to
    pick), ``conflict`` (``files``), ``refused`` (a pick that is not a
    plain exact copy, a carry that may not run), ``moved`` (the release
    head moved under every try; nothing pushed, the next tick tries
    again) or ``failed`` (``why`` — a fault: a fetch or push that did
    not answer; :func:`settle` tries again a few times). ``base`` is the
    release head the picks were made on.

    Two states are returned and NOT stamped (:data:`UNRECORDED_STATES`):
    ``busy`` — another run holds the release's run lock, so this entry
    touched nothing — and ``settled`` — the node already recorded a
    ``carried`` or ``noop`` outcome, which stands."""
    rel = _release.release_of(store, todo_id)
    if rel is None:
        return {"state": "refused", "why": "no release is recorded on the plan"}
    rn = str(rel["rN"])
    with _run_lock(store, rn) as held:
        if not held:
            return {
                "state": "busy",
                "why": f"another carry of release/{rn} holds the run lock "
                "(or the lock could not be asked for)",
            }
        return _carry_held(store, todo_id, root, rel)


def _carry_held(
    store: Any, todo_id: int, root: Path, rel: dict[str, Any]
) -> dict[str, Any]:
    """:func:`carry` with the release's run lock held."""
    from precis.workers.job_types import release_poll as _rp

    rn = str(rel["rN"])
    ref = store.get_ref(kind="todo", id=todo_id)
    spec = (ref.meta or {}).get(CARRY_KEY) if ref is not None else None
    minted = [str(s) for s in spec.get("shas") or ()] if isinstance(spec, dict) else []
    if not isinstance(spec, dict) or str(spec.get("rN") or "") != rn or not minted:
        return {"state": "refused", "why": f"the node names no corrective for {rn}"}
    before = spec.get("outcome")
    if isinstance(before, dict) and before.get("state") in _SETTLED_STATES:
        # Read with the lock held: an earlier entry finished this carry.
        return {
            "state": "settled",
            "why": f"td{todo_id} already recorded its carry ({before.get('state')})",
        }

    def done(outcome: dict[str, Any]) -> dict[str, Any]:
        return _record(store, todo_id, outcome)

    if not enabled(store):
        return done({"state": "refused", "why": f"{SETTING_KEY} is off"})
    started = _release.deploy_started(store, todo_id)
    if started is not None:
        return done(
            {"state": "refused", "why": f"{started} — it takes no more commits"}
        )
    branch = _release.release_branch(rn)
    env = _auth_env()
    url = _origin_url(root, env)
    objects = _objects(root)
    if url is None or objects is None:
        return done(
            {"state": "failed", "why": "the checkout names no trusted origin url"}
        )
    ops = _mirror.mirror_root(root)
    pass_id = _release.plan_root(store, todo_id)
    _reap_trees(root, rn)
    tree = _tree_home(root) / f"{_TREE_STEM}-{rn}-{todo_id}-{os.getpid()}"
    base = ""
    try:
        for attempt in range(1, _PUSH_TRIES + 1):
            tips = [
                _mirror.fetch_ref_into(
                    root,
                    url,
                    name,
                    f"refs/remotes/origin/{name}",
                    ops_parent=ops,
                    env=env,
                    object_dirs=(objects,),
                )
                for name in ("main", branch)
            ]
            main_tip, head = tips
            if not main_tip or not head:
                gone = "main" if not main_tip else branch
                return done({"state": "failed", "why": f"origin/{gone} did not fetch"})
            base = head
            wanted, fatal = carry_wanted(store, rel)
            if fatal is not None:
                return done({"state": "failed", "why": fatal, "base": base})
            todo = [s for s in minted if any(_same(s, w) for w in wanted)]
            skipped = {
                s: "the gate no longer wants it (on the branch, or its ask was answered)"
                for s in minted
                if s not in todo
            }
            picks, contained, refusal = _plan_picks(root, todo, base, main_tip, rn)
            skipped.update(contained)
            if refusal is not None:
                return done({"state": "refused", "why": refusal, "base": base})
            _drop_tree(root, tree)
            picked: dict[str, str] = {}
            if picks:
                add = _git(["worktree", "add", "--detach", str(tree), base], root)
                if add.returncode != 0:
                    return done(
                        {
                            "state": "failed",
                            "why": f"git worktree add: {_err(add)}",
                            "base": base,
                        }
                    )
            for short, full in picks:
                pick = _git(["cherry-pick", "-x", full], tree)
                if pick.returncode != 0:
                    unmerged = _git(["diff", "--name-only", "--diff-filter=U"], tree)
                    files = [f for f in _out(unmerged).splitlines() if f.strip()]
                    if files:
                        _git(["cherry-pick", "--abort"], tree)
                        return done(
                            {
                                "state": "conflict",
                                "why": f"{short} does not apply to release/{rn} at "
                                f"{base[:12]}: conflict in {', '.join(files[:8])}",
                                "files": files[:50],
                                "base": base,
                            }
                        )
                    if _empty_pick(tree):
                        skip = _git(["cherry-pick", "--skip"], tree)
                        if skip.returncode != 0:
                            _git(["cherry-pick", "--abort"], tree)
                            return done(
                                {
                                    "state": "refused",
                                    "why": f"git cherry-pick --skip after the "
                                    f"empty pick of {short}: {_err(skip)}",
                                    "base": base,
                                }
                            )
                        skipped[short] = f"release/{rn} already holds its change"
                        continue
                    _git(["cherry-pick", "--abort"], tree)
                    return done(
                        {
                            "state": "refused",
                            "why": f"git cherry-pick {short}: {_err(pick)}",
                            "base": base,
                        }
                    )
                result = _out(_git(["rev-parse", "HEAD"], tree))
                if not result or not _rp._replays_as(
                    root, f"{full}^", f"{result}^", full, result
                ):
                    return done(
                        {
                            "state": "refused",
                            "why": f"the pick of {short} is not an exact copy of "
                            "it (the replay proof failed)",
                            "base": base,
                        }
                    )
                picked[short] = result
            if not picked:
                return done(
                    {
                        "state": "noop",
                        "why": f"release/{rn} at {base[:12]} needs none of "
                        f"{', '.join(minted)}",
                        "skipped": skipped,
                        "base": base,
                    }
                )
            # Each pick was proven against its own parent; this proves the
            # chain: the tree holds the release head plus exactly these
            # picks, in this order, and nothing else is about to be pushed.
            history = _git(["rev-list", "--reverse", f"{base}..HEAD"], tree)
            if history.returncode != 0 or _out(history).split() != list(
                picked.values()
            ):
                return done(
                    {
                        "state": "refused",
                        "why": f"the carry worktree does not hold release/{rn} at "
                        f"{base[:12]} plus exactly the picks — nothing pushed",
                        "base": base,
                    }
                )
            tip = list(picked.values())[-1]
            # The pass root's row lock, the one mark_deploy_started takes:
            # no deploy can stamp its start between this look and the push.
            # Nothing is written under it; it spans the look and the push
            # and nothing else.
            stop: dict[str, Any] | None = None
            pushed, detail = "unlocked", ""
            try:
                with store.atomic() as conn:
                    conn.execute(_release._LOCK_ROOT_SQL, (pass_id,))
                    stop = _last_look(store, todo_id, rel, picked, base, pass_id)
                    if stop is None:
                        pushed, detail = _mirror.push_cas(
                            url,
                            branch,
                            tip,
                            base,
                            ops_parent=ops,
                            env=env,
                            object_dirs=(objects,),
                        )
            except psycopg.Error as exc:
                # The lock was not to be had, or its transaction did not
                # close. A push that answered "ok" stands either way.
                log.warning("%s: pass root lock for %s", NAME, rn, exc_info=True)
                if pushed != "ok":
                    return done(
                        {
                            "state": "failed",
                            "why": "the pass root's row lock was not available "
                            f"({type(exc).__name__})",
                            "base": base,
                        }
                    )
            if stop is not None:
                return done(stop)
            if pushed == "ok":
                return done(
                    {
                        "state": "carried",
                        "pushed": tip,
                        "base": base,
                        "picked": picked,
                        "skipped": skipped,
                    }
                )
            if pushed != "raced":
                return done({"state": "failed", "why": detail, "base": base})
            log.info(
                "%s: %s moved under the push (try %d): %s",
                NAME,
                branch,
                attempt,
                detail,
            )
        return done(
            {
                "state": "moved",
                "why": f"{branch} moved under {_PUSH_TRIES} tries; nothing pushed",
                "base": base,
            }
        )
    finally:
        _drop_tree(root, tree)


def _follow_head(store: Any, todo_id: int) -> str | None:
    """After a carry: put the pass on the pushed head now
    (``release_poll.follow_moved_head`` — restamp, reopen the loop, mint
    the poll) instead of at the next ten-minute tick. Its note, ``None``
    when it had nothing to do. Best-effort: a fault is logged and the
    next ``release_poll`` tick does the same."""
    from precis.workers.job_types import release_poll as _rp

    try:
        return _rp.follow_moved_head(store, _release.plan_root(store, todo_id))
    except Exception:
        log.warning("%s: head follow after the carry raised", NAME, exc_info=True)
        return "release head follow raised — the next release_poll tick retries"


def run(ctx: Any, params: dict[str, Any]) -> None:
    """``release_confirm``'s ``carry`` mode: record :func:`carry`'s
    outcome and end the job. Every outcome ends it ``succeeded``, a fault
    included: the outcome on the node is what ``release_poll`` reads on
    its next tick — it tries a faulted carry again, a few times, then
    asks. A failed job would instead climb the node's retry cap into an
    ask that holds the whole pass open, for a step whose fallback, the
    carry ask, already exists. Only a host with no checkout fails.

    After a ``carried`` outcome the pass is put on the pushed head at
    once (:func:`_follow_head`); its note joins the summary and never
    changes the outcome."""
    from precis.workers.job_types import run_slice as _rs

    todo_id = int(params["todo_id"])
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        ctx.record_failure(f"{NAME}: {exc}")
        return
    out = carry(ctx.store, todo_id, root)
    ctx.set_meta(release_carry=out)
    picked = out.get("picked") or {}
    line = {
        "carried": "picked "
        + ", ".join(f"{src} as {str(new)[:12]}" for src, new in picked.items())
        + f"; the release head is {str(out.get('pushed') or '')[:12]}",
        "noop": f"nothing pushed — {out.get('why')}",
        "conflict": f"nothing pushed — {out.get('why')}; the carry ask files",
        "refused": f"nothing pushed — {out.get('why')}",
        "moved": f"{out.get('why')}; the next release_poll tick tries again",
        "failed": f"nothing pushed — {out.get('why')}; the next release_poll "
        "tick tries again",
        "busy": f"nothing done — {out.get('why')}",
        "settled": f"nothing done — {out.get('why')}",
    }[out["state"]]
    if out["state"] == "carried":
        note = _follow_head(ctx.store, todo_id)
        if note:
            line = f"{line}\n{note}"
    ctx.append_chunk("job_summary", f"{NAME}: {line}")
    ctx.set_status("succeeded")


__all__ = [
    "CARRY_KEY",
    "CARRY_WAIT_AFTER",
    "NAME",
    "SETTING_KEY",
    "UNRECORDED_STATES",
    "CarryNode",
    "Settled",
    "carry",
    "carry_nodes",
    "carry_wanted",
    "enabled",
    "open_carry",
    "pass_root",
    "run",
    "settle",
    "sweep",
]
