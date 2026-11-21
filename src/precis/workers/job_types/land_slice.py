"""``land_slice`` — the lander: land one finished slice branch (dark factory).

Spec: `me692430` § "Branches: who merges what, how"
(the builder commits on its branch only; the lander lands it with
``scripts/ship`` and drops its folder). Mechanical: no
LLM. :mod:`precis.workers.job_types.run_slice` mints one of these (idem key
``land_slice:td<id>``, a child of the slice todo, pinned to the host whose
hub holds the build) once a build session exited clean, its work was pushed
to the hub and its branch passed the deny-path check. The land re-checks the
resolved deny set before launching ship — the last gate (gr645524): a denied
diff is refused with the ``human_only_hit`` stamp, so the tick's ruling ask
opens on the frozen branch rather than a rebuild that can never land. One
named exception (td659522): the ``autocatpath_min_version`` line of
``deploy/group_vars/all.yml`` in a pin-bump node's own commit, proved by
:func:`pin_bump.land_refusal` (:func:`_pin_exempt`).

**The land's own folder** (gr741695, :mod:`._slice_ws`). The builder's
folder is gone by the time the lander runs, and the lander never enters a
folder another job made: :func:`submit` cuts a fresh clone,
``_slice_ws.attempt_dir(root, todo, <this job>, ROLE_LAND)`` (``slice-td<id>-
land-j<job>``), from the host's hub at ``params.head``, the commit to land,
which the hub holds under ``params.ref``
(:func:`_cut_land_folder`, :func:`_land_start`). The hub's copy of the land
target is synced from GitHub first (``sync_hub``); ``params.worktree`` is
ignored. ``head`` must be the ref's tip or an ancestor of it — the ref may
carry a ``wip:`` snapshot on top, and the land takes ``head``; a ref that no
longer holds it, a ref the host's hub lacks or an unreadable hub fails the
land attempt-free under ``infra-land`` with the workspace layer's message
(:func:`_fail_workspace`), nothing landed. A land minted before the switch
has no ``ref``: its work is looked for in the hub's own
``refs/factory/build/<id>/0`` (deleted and imported again when it does not
hold ``head``), then in the host checkout's local ``slice/<id>`` (fetched
into the hub from the checkout's path when it holds ``head``), then on
GitHub under ``refs/factory/slice/<id>`` (``import_legacy``); ``head`` falls
back to ``meta.pending_land.head``, else that ref's tip. What the cut
resolved (``{head, ref}``) goes on the job meta (:data:`LAND_START_KEY`) for
the conflict stamp. The clone's ``origin`` is
then set to the repository's GitHub url (what the mirror's url rewrite and
``scripts/ship`` expect; the cut pointed it at the hub) while the base's
tracking ref it fetched stays. A folder that already exists under the job's
own name is the job's when a ship was ever launched in it (the job's
``meta.compute_handle``, a proven launch, or the ship's log or exit file in
the folder; :func:`_launch_on_record`): a job resumed after an outage, or
adopting a ship it launched, re-enters it and cuts nothing. A folder with no
launch on record and no ship behind it is an unfinished cut (the worker died
between the cut's rename and its last step): it holds nothing of its own, so
it is dropped and cut again. Everything from there to the end
of the ship runs in the clone exactly as it did in the builder's worktree —
locks, ``index.lock`` handling, the release sync, the stack sweep, the
mirror populate, the drift preflight, the launch and the verify phases; only
the ``slice-main-td<id>`` probe tree stays a worktree of the host checkout.
The clone's git dir is ``<clone>/.git`` and is its own: nothing in it is
shared with another land or with the host checkout, so the land's ``precis-
land.lock`` for a clone lives in the host checkout's git common dir, one
file per todo (:func:`_land_lock_where`) — what it serialises was only ever
two ships of one slice (the old file sat in each slice's own worktree
gitdir), which share the todo's mirror. Its stamp names the folder
(``tree=``), and a ship of the same todo running in another job's folder is
a retryable busy, not an adoption. The lock file is removed when the land
that held it ends and nothing holds it (:func:`_unlink_land_lock`); a lock
counts only when the file locked is still the one at the path
(:func:`_try_land_lock`), which is what makes the removal safe. A PR land
(``params.pr``) keeps its worktree and the old per-gitdir lock. After a land
the task's ``meta.build_floor`` is this land job's id (stamped when the land
record starts and again by the cleanup): an attempt ref below it is never
continued from, whatever the cleanup could delete. The main probe syncs the
hub's main from GitHub before refreshing the host checkout's ``origin/main``
from it, and skips the refresh when the sync cannot say.

Detached submit/poll pair under ``ssh_node``, like ``run_slice``:

* :func:`submit` reads the squash subject (:func:`squash_subject`): the
  brief's stated ``subject:`` line (:func:`_node_subject` — the todo body,
  then the newest ``brief:``/``handoff:`` comment) when present, else the
  last non-test/wip commit; its trailing handle list is made to carry
  ``(td<id>)`` so ``scripts/lib/land_notify.py`` moves the todo, the
  whole capped at :data:`SUBJECT_CAP` characters cut at a word boundary
  (gr574383). Then it launches
  ``scripts/ship --quick "<subject>"`` detached in the land's clone
  (``PRECIS_SHIP_BIN`` overrides the
  path; ``PRECIS_SHIP_VERBOSE=1`` so the log holds the whole transcript;
  ``PRECIS_GATE_N`` caps the checked gate's xdist workers at
  :func:`test_nproc` — ``PRECIS_LAND_TEST_NPROC``, default 3 — because a
  static-wide selection on the agent host OOM-killed a worker twice, gr567471;
  ``PRECIS_TEST_NPROC`` carries the same count to any ``scripts/test``
  run inside the ship). Every child the lander spawns starts from
  :func:`land_env` — an allowlist env, never the worker's whole env, so
  no DB DSN or vault variable reaches a gate — and the launches that
  fetch, push or call ``gh`` additionally get the factory credential
  wiring (gr659929): the lander's own drift preflight and branch push
  (trusted code) get :func:`precis.workers.factory_git.git_auth_env`.
  The lander's own git calls go through :func:`_git`, which runs the
  same allowlist scrubbed of credential names with hooks off — never
  the worker's ambient env — and its authenticated transports (the
  mirror populate and sync, the branch push, the cleanup's
  origin-branch delete) run in the mirror's fresh ops gitdir so no
  branch-writable config or hook meets the token.
  Ship is different: its host-side steps run the *branch's* scripts, and
  no process-table rule can separate their git calls from ship's own
  (review td660513 on the broker that tried — depth-authorized asks
  still answered a gate child's ``git credential fill``, and the broker
  itself was branch-importable through a root ``precis/__init__.py``).
  So ship gets **no credential at all**: the lander keeps a bare mirror
  of the land refs in the main checkout's ``scratch/``
  (:mod:`precis.workers.factory_git_mirror`) — one directory per launch,
  so a sibling land's populate can never refresh away this launch's
  unsynced squash — refreshes it from GitHub under the token at launch,
  and gives ship env-config url-rewrite entries pointing every git
  transport contact with the real origin URL at the mirror — ship
  fetches, squashes and CAS-pushes exactly as before, against the
  mirror. Every credential-bearing call runs through the trusted
  boundary — a fresh ops gitdir, hooks off, no system/global/local
  config, no ``refs/replace`` substitution — because the mirror, the
  clone's gitdir and the host checkout's common dir are all
  branch-writable; and
  every GitHub answer is compared
  against an ``ls-remote`` oracle no on-disk config can redirect. The
  launch tips that CAS those pushes persist beside the mirror — never
  in the branch-writable tree — and mirror mode itself persists on the
  todo's meta (:data:`FACTORY_MIRROR_KEY`), out of every same-uid
  file's reach, so an adopted handle's sync still fails closed when
  the launch files are gone (review td660513, second pass: the pair
  was deletable by the very gate code the boundary exists for).
  Ship's green exit is not the land: the
  lander then pushes only the refs ship moved to the real origin, each
  leased on the tip GitHub held at launch, each required to equal the
  sha ship reported and to descend from its tip; a lost race or a
  mismatch defers the land (short backoff, then ship re-runs against a
  fresh mirror) and a sync failure never records a land origin does not
  hold — while the post-land effects ship already wrote or would write
  (``origin/*`` refs, a fast-forwarded local main, land_notify's
  todo/gripe marks) are repaired, refused or held until the push
  verifies: the notify marks ride ``PRECIS_LAND_NOTIFY=0``/
  ``PRECIS_GRIPE_NOTE=0`` and the lander posts them itself, the
  ``origin/*`` refs are reset to GitHub's real tips on a deferred sync,
  and the local-main advance is refused outright by a PATH-leading
  ``git`` shim in ship's env — the lander replays it with
  :func:`precis.workers.factory_git_mirror.advance_local_main` after
  the sync, because a best-effort rewind cannot cover a crashed worker
  or a blind GitHub (review td660513, second pass). The token reaches
  only the lander's own git calls — never argv, never a ship env name,
  never an fd — and the gates (the verify ``scripts/test``, the
  selector, the collect probe) run branch code on the host under the
  bare allowlist, credential-free.
  A branch carrying nothing at all — no commit
  ahead and no net diff, the fix already on main by another route —
  gets no ship: submit returns the ``no_land`` handle and the poll
  writes the same nothing-new record, done not red (gr648125).
* :func:`poll` waits for ship's exit. Green: the landed sha (ship's
  ``shipped — landed on main:`` line, else ``git rev-parse origin/main``)
  goes on the job (``meta.landed_sha``); a ``landed: <sha>`` comment and
  ``STATUS:done`` go on the todo only when land_notify did not already
  write them (its comment is the todo's newest) — a pin-bump node keeps
  its STATUS until its gate image carries the pin (td659522 F7: ship ran
  with land_notify held, the lander writes the subject's other handles,
  and ``pin_bump``'s image wait closes the node); the sha is written into
  the plan's pending ``sha-in-prod`` gate (:func:`pending_gate`). When ship
  took its nothing-new guard (:func:`nothing_new`: the branch's tree equals
  origin/main's, nothing squashed) no sha is the slice's: the todo closes
  with :data:`NO_DIFF_NOTE` and no PR is closed (gr561142: main's head is
  another land's commit); the gate stays pending — unless the todo's own
  ``landed:`` comment still names a sha, a reland after the first land's
  gate write died mid-job, which fills it from that comment (gr576154) —
  never one older than the ``handoff: review: REVERT`` a revert verdict
  leaves on the todo: that sha is the reverted commit (td626034).
  The land that fills a
  ``gripe-fix`` plan's gate also moves the plan's gripe to the verify rung
  (``resolved: <sha>`` + ``STATUS:verify``, :func:`_notify_gripe`;
  gr561932), so ``gripe_validate`` closes it once the sha is on prod. Then
  cleanup (:func:`_cleanup`): the land's clone is dropped
  (:func:`_slice_ws.drop`: its ``precis-test-slice-*`` compose project
  down with ``-v`` — it used to stay warm forever, gr570133, and the
  SessionStart reapers never run on this host — then the directory, the
  branch with it), the todo's attempt refs are deleted from the hub
  (``delete_builds``) and ``meta.build_ref`` is cleared on the task (a ref
  left behind would start a later attempt from commits already landed),
  and, when the ``ls_remote`` oracle shows a legacy ``slice/<id>`` ref on
  origin (what the engine before gr741695 pushed), an ops-repo
  ``push --delete`` removes it (:func:`_delete_remote_branch`). A PR
  land's worktree is still removed with ``worktree remove`` and
  ``branch -D``. A cleanup
  step that fails is reported in the summary, not a job failure: the land
  is real. Every launch — the first and each deferral relaunch — first
  sweeps the host's other finished slice stacks
  (:func:`_launch_ship` → :func:`_sweep_stacks` →
  :mod:`precis.workers.slice_stacks`): gone trees down with ``-v``,
  quiet stacks idle past ``PRECIS_SLICE_STACK_IDLE_S`` (default 3600 s;
  the warm gate-cache volume is kept) and the oldest quiet stacks over
  ``PRECIS_SLICE_STACK_CAP`` (default 8) — except any whose newest
  activity is younger than ``PRECIS_SLICE_STACK_CAP_FLOOR_S`` (default
  900 s: ship writes no run marker and gates through separate ``compose
  exec`` calls, so a quiet young stack may be a land mid-ship), and each
  cap eviction re-inspects liveness right before the ``down``. A stack
  with a running non-gate container, an attached exec or a live
  ``precis-test-runs/<pid>`` marker is never touched, and
  ``PRECIS_SLICE_STACK_SWEEP=off`` disables the sweep. Without it
  finished stacks piled into the 8 GiB docker VM until
  gates died on OOM-137 and address-pool exhaustion. Red (lint ✖, a
  forward-merge conflict, anything non-zero): the
  job fails with the ✖ line, a ``handoff:`` comment tells the next build
  session why, and the todo reopens with ``attempt:<n>`` (run_slice's
  :func:`~precis.workers.job_types.run_slice.release_todo`) — except a
  red outside the diff (a gate kill past its deferral cap, marked or a
  bare exit 137), which fails the job
  attempt-free (``meta.attempt_free = "infra-land"`` + ``red_class``, the
  lease back clean, no ``attempt:<n>``) with ``meta.land_again_cap``
  set: ``plan_dispatch`` goes to the re-land ask — never an automatic
  re-land (the same 137 can be the branch's own memory). The one
  exception is a plain ``index.lock`` wait (gr684320): the lander never
  force-reaps, so a lock still held past the brief wait fails
  attempt-free ``"index-lock"`` *verbatim* — the dispatcher re-mints the
  land without a builder and outside the re-land cap, backs off like any
  repeated free failure and asks once the same reason has run to its
  cap. Nothing is saved on a red: the lander adds no
  work of its own and the hub holds the commit that was to land, so the
  land's clone is dropped once no ship is behind it
  (:func:`_drop_land_folder`; a folder with a live ship is left for the
  host sweep). An ordinary red whose ship line is a rebase or merge conflict
  (:data:`CONFLICT_MARKS`) first stamps the task's ``meta.conflict``
  (:data:`CONFLICT_KEY`: ``{head, ref, base_tip, files, at, job}``, the
  conflicted paths as git reports them in the clone), which the dispatcher
  reads to mint a conflict-only attempt. A ✖ about *main* (:func:`is_main_refusal`:
  the drift guard's two refusals) is a deferral, not a red: poll counts it
  (``handle.main_refusals``), parks the handle for
  :func:`refusal_backoff_s` (``handle.not_before``, default 30 min), then
  relaunches ship; the todo is untouched. Each step renews ``meta.deadline``
  (park: backoff + wall; relaunch: wall), so a parked land is not killed at
  the 1 h wall while a running ship still is; the running phase's deadline
  rides the handle (``handle.deadline`` — ``started_at`` + wall + the gate
  waits already observed), so a worker restart's re-adoption resumes the
  job's own count instead of restarting the wall at the re-adopt poll
  (gr647913); past
  :data:`MAX_MAIN_REFUSALS` the job fails with a "land BLOCKED" handoff.
  Before every launch the lander runs the drift helper itself under its own
  identity (:func:`_drift_preflight`); when that cannot say how old main's
  verdict is (a blind token, GitHub unanswerable) the land parks the same
  way instead of riding ship's "unknown never refuses" (fail-closed,
  gr550941). Every main-side deferral event ends with the helper's
  ``--diag`` line. Ship's host-side refusals (:func:`is_host_refusal`: the
  docker VM disk preflight, exit 3, no ✖) defer the same way under the same
  counter and cap; the blocked handoff then names the node, not main
  (gr552214). A red log whose gate was killed by the host
  (:func:`gate_kill_line` over the whole log, non-zero exit: ship's ✖
  ``exited 137 (SIGKILL)`` line — the docker VM's OOM killer or the
  container's own mem_limit, gr570133 — or xdist's line-start
  ``worker 'gwN' crashed while running``; the anchored shapes keep a
  failure that QUOTES the literals an ordinary red) — and a bare exit
  137, ship SIGKILLed with no mark in its log — defers the same way
  but under its own ``handle.gate_kills`` counter and the shorter
  ``PRECIS_LAND_GATE_KILL_BACKOFF_S`` (default
  120 s): the VM, not the branch, ran out of memory, so no attempt is
  burned and the relaunch sweeps the idle stacks first. An exit-137 —
  marked or bare — past
  :data:`MAX_GATE_KILL_RETRIES` fails attempt-free with a handoff naming
  the docker VM's memory or the branch's own memory use (a branch memory
  hog can hit the same mem_limit, so the handoff does not clear the
  branch): ``meta.land_again_cap`` parks the run at the
  ``plan_dispatch`` ask — no auto re-land, no builder unless the human
  rewrites the brief. An xdist crash retries once, then falls to
  the ordinary red (a deterministic crash is a branch problem).
  Whatever the red's class, the failing lines — the gate's error lines,
  else the excerpt before the ✖ — ride the job summary and the
  ``handoff:`` comment, because the land log dies with the worktree
  (gr573347 a). And a ``checked selection is RED`` verdict whose failing
  test ids are ALL outside the diff does not charge the branch on sight
  (gr573347 b): a ``verify`` phase on the handle re-runs the ids detached
  — standalone in the tree first (a green is a flake or an order
  dependence: attempt-free, the re-land tick takes it), then, still red,
  on a throwaway ``origin/main`` worktree ``slice-main-td<id>`` (always
  removed afterwards, with its stack). Ids red on main too are not the
  branch's: when the checked selection is a subset, the land gates on it
  MINUS them (the ``deselect`` phase — ``scripts/test --selected
  --deselect``, engine or not) and ship relaunches ``--quick`` on its
  green — this run's lint already passed — with a ``handoff``-style note
  on the todo. When the selection IS the suite (FULL) no minus-them gate
  runs at all (td602151 removed the 3 h full-gate budget: no land runs
  the suite); the ids are recorded as excluded on the todo and ship
  relaunches ``--quick`` on the 1 h wall the same way. An unreadable
  selector (timeout or error) counts as FULL (gr586292): ``scripts/test
  --selected`` would re-read it without a timeout and run the whole
  suite on a FULL answer; the deploy gate runs the suite. Only a
  main-green red burns an attempt; any launch, probe or
  exclusion-record failure falls back to the ordinary red.
* :func:`kill` SIGTERMs the live phase's process group (a running
  verify's during verify, ship's otherwise) and drops the main probe's
  worktree and stack; it pushes no branch (gr741695: the hub holds the
  commit to land) and leaves the clone for the host sweep. A
  WALL-DEADLINE kill additionally carries
  :data:`DEADLINE_KILL_META` — applied by ``ssh_node``'s terminalize write
  as :attr:`~precis.workers.job_types.JobTypeSpec.deadline_kill_meta`:
  the land reads as a distinct timed-out verdict (attempt-free,
  ``red_class="timed-out"``, ``land_again_cap``), so the plan tick parks
  the slice at the "land again (no builder)" ask instead of counting a
  gate failure or auto re-landing a ship that may already have merged
  (gr647914).

**Release fixes land on the release branch** (release-train T1,
`me692603`). A slice todo whose
``meta.land_target`` (or a ``land target: release/r<N>`` brief line,
:func:`land_target` → :func:`precis.plans.release.land_target_of`)
names a release branch lands with ``scripts/ship --quick --onto
release/r<N>``: ship applies the branch's change onto
``origin/release/r<N>`` and pushes that branch only — main never moves
(it gets the fix when the release merges back after its deploy, T3).
The target is read from the todo on every launch, not from the job
params, so each land mint path agrees. Such a land skips the spot
selection and main's drift preflight (ship's release lane does the
same), reads its squash subject and the already-landed proof against
``origin/release/r<N>``, and takes its sha from ship's ``shipped —
landed on release/r<N>:`` line. Before every such launch the land
reads the plan's release through :func:`_release_closed`: a deploy
already started or pinned (``precis.plans.release.deploy_started`` —
the deploy owner's own records; ship's round lock is only this host's
checkout), or a plan whose release is another branch, refuses
attempt-free with ``land_again_cap`` — nothing pushed, the coordinator
asked. Every such launch — first and relaunch — also merges
``origin/release/r<N>`` into the branch first (:func:`_sync_release`,
gr738762): ship lints the branch as cut and applies its diff onto the
release head only afterwards, and sibling fixes move that head in
parallel. A conflict fails the land as an ordinary red
(:data:`RELEASE_SYNC_CONFLICT_MARK`) whose handoff says to merge the
release, never ``origin/main``. A slice with no target lands on main
exactly as before.

**Pull requests** (td543749). With ``params.pr = <n>`` (a ``pr-intake``
plan's land node) the branch is ``slice/pr-<n>`` and the worktree is
``params.worktree`` when ``dep_check`` pinned one (it must exist: it holds
the relock commit), else ``slice-pr-<n>``, cut here from ``git fetch origin
+pull/<n>/head`` and refused when the head is not the sha the intake
classified (``meta.pr.head_sha`` on the plan root). A PR land is not
touched by the clone cut (gr741695): it keeps its linked worktree of the
host checkout, the per-gitdir land lock and the worktree cleanup. The subject is the PR
title made lint-clean (:func:`precis.plans.pr_class.land_subject`) plus
``(td<id>)``. Every land ships ``--quick`` (:func:`ship_flag`; Reto
2026-11-05, gr570221 — lint only; the release gate on ``/go`` runs the
suite before deploy) unless a checked gate was asked for —
``params.checked`` (a pr-intake plan's land node) or an engine-path
diff (td574937 E1) — AND the selector's answer is a small spot
selection (:func:`spot_selection`). Reto 2026-11-08 (gr599427): lands
run basic checks only — lint plus a spot check; GitHub CI at the
release gate runs the suite, so no land runs the wide or the full
local gate (E1's "an engine land never drops to lint" is superseded —
the engine review reads the landed diff, not a checked gate). A
checked land the selector refuses with
FULL (a migration or harness diff: :data:`FULL_SELECTOR_MARK`) relaunches
once with :func:`full_fallback_flag` — ``--quick``, the same lane the
default already uses (me570388) — without consuming an attempt.
After a green land — of a PR node, or of any slice whose todo carries
``meta.pr_close = <n>`` (the "land after fixes" slice) — the PR is closed
with a comment naming the squash sha (:func:`_gh`, the host's ``gh auth``:
the attribution gap of gr543694 applies). A failed close is reported in
the summary, not a job failure.

**Pre-launch hygiene** (gr650484). Before ship runs, :func:`_launch_ship`
serialises lands of one slice: the lander's ``precis-land.lock`` flock
(in a land's clone, the host checkout's per-todo file,
:func:`_land_lock_where`) is probed first because its stamp carries the
slice's identity —
held by a live pid behind this slice's stamp, the running ship is
adopted (this job polls its exit file and lands the record); held by a
foreign stamp, a dead stamp's orphaned descendants, or ship's own
``precis-ship-worktree.lock.d`` (a hand-run ship names no slice), the
land ends attempt-free — never ``succeeded`` without a land record.
The stamp names the ship's pid only once ship exists (td652194): the
pre-launch window stamps none, so a second land there defers as an
orphan rather than adopting — and later killing — the worker itself.
It carries the launch flag, so an adopted handle relaunches on a
FULL-selector refusal exactly as the owning job would. The fd is
passed into the launched ship so the lock outlives a worker that dies
mid-land (the window a retried ``land_slice`` used to launch
a duplicate in — jo650396, td649847). Then the cheap outs: when the
slice's work is already on ``origin/main`` — a tree-identical branch,
or the squash subject already on main with the slice's changed paths
verified identical (:func:`_already_landed`) — the land finishes green
without a ship (:func:`_landed_finish`, the same tail poll's green
runs); and every ``index.lock`` the ship would hit — the folder's and,
for a linked worktree (a PR land's; a clone's git dir is its own), the
primary checkout's its main-sync fast-forwards — is reaped when provably stale (:data:`INDEX_LOCK_STALE_S`
plus no live git in the checkout and no open handle — the reaper's
keep-on-doubt rule) or waited on briefly then failed retryable
(``attempt_free``, never the red path; :func:`_clear_index_locks`).

Guard: ``PRECIS_FACTORY=off`` refuses every submit. Git, launch, docker
and process probes go through module-level seams (:func:`_git`,
:func:`_launch`, :func:`_process_alive`, :func:`_pending_gates`,
:func:`_evict_stack`, :func:`_sweep_stacks`) so tests never run ship,
git, docker or SQL.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.plans import governs as _governs
from precis.plans import lanes as _lanes
from precis.plans import release as _release
from precis.plans.logbook import log_plan_event
from precis.plans.templates import (
    BUILD_FLOOR_KEY,
    BUILD_REF_KEY,
    PENDING_LAND_KEY,
    PENDING_SHA,
    PRELAND_DEFER_FAILED_KEY,
    PRELAND_FIX_ROUNDS_KEY,
    PRELAND_HELD_KEY,
    SLICE_REF_NS,
)
from precis.store.types import ChunkInsert, Tag
from precis.utils.secret_scan import mask_secrets
from precis.workers import admission
from precis.workers import factory_git as _factory_git
from precis.workers import factory_git_mirror as _factory_git_mirror
from precis.workers import slice_stacks as _slice_stacks
from precis.workers.job_types import JobTypeSpec, Reconcile, _slice_ws
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "land_slice"

#: ``red_class`` of a land whose launch the worker host refused for
#: capacity (:class:`admission.Refused`): attempt-free, re-minted by the
#: tick; ``plan_dispatch`` keys its capacity wait on it.
ADMISSION_RED_CLASS = "admission"

#: ``scripts/ship``'s subject hard limit, in CHARACTERS
#: (``ship_subject_lint`` counts UTF-8 code points and refuses past 120;
#: 72 is only its "aim for <=72" note). This used to be a 72-BYTE cap, so
#: a subject that fit the real limit was shortened for the appended
#: handles — and cut mid-word (gr574383).
SUBJECT_CAP = 120
_DEFAULT_WALL_S = 3600
_TAIL_CAP = 2000

#: ``scripts/lib/land_notify.py``'s trailing handle list, including its
#: partial ``refs grN`` form (gr659980: a list holding one was not seen,
#: so a second `` (tdN)`` list was appended and land-notify read only that).
_HANDLE = r"(?:gr|td)\d+"
_ITEM = rf"(?:{_HANDLE}|refs\s+gr\d+)"
_HANDLES_RE = re.compile(rf"\(({_ITEM}(?:[,\s]+{_ITEM})*)\)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_STATUS_RE = re.compile(r"SHIP_STATUS:\s*(\S+)")
#: Ship's ``shipped — landed on main:`` report line, or the release
#: branch a release fix lands on (``scripts/ship --onto``, release-train T1).
_LANDED_RE = re.compile(r"shipped — landed on (?:main|release/r\d+):")
#: Ship's nothing-new guard: the branch's tree equals origin/main's, so no
#: squash commit was made (the ``landed on main:`` line that follows names
#: main's existing head, not the slice's work — gr561142).
_NOTHING_NEW_MARK = "nothing new to ship — tree identical to origin/main"
#: ``--onto``'s twin: the release head already carries the change.
_NOTHING_NEW_ONTO_RE = re.compile(
    r"nothing new to ship — origin/release/r\d+ already carries this change"
)
#: The ``landed:`` comment of a slice that landed no commit.
NO_DIFF_NOTE = (
    "landed: nothing — branch had no net diff against origin/main (already on main)"
)


def ship_flag(params: dict[str, Any], engine: bool = False, spot: bool = False) -> str:
    """``--checked`` only when a checked gate was asked for AND the
    selector's answer is a small spot selection — else ``--quick``.

    A land asks for the checked gate two ways: ``params.checked`` (a
    pr-intake plan's land node — r200 went red on a factory land for an
    ``args`` key only the dispatch contract test catches) or an
    engine-path diff (td574937 E1). It earns ``--checked`` only when
    ``spot`` — :func:`spot_selection`, read by the caller — is True.

    Reto 2026-11-08 (gr599427): every factory land — engine diffs
    included — runs basic checks only; GitHub CI at the release gate
    runs the suite and a red release goes through the existing fix
    loop. E1's "an engine land never drops to lint" is superseded: the
    engine review reads the landed diff, not a checked gate — and a
    wide selection was a 1 h wall death, not a safety net (11k ids on
    a stale coverage map, gr561463).
    """
    if engine or params.get("checked") is True:
        return "--checked" if spot else "--quick"
    return "--quick"


#: ``--checked`` earns its gate only when the impacted-tests selection's
#: answer IS a spot check: at most this many COLLECTED pytest items
#: (gr599427, comment 2's numbers). td602151: the td601534 code counted
#: selector target LINES — a file target carries every item in the file
#: plus its parametrizations, so ≤200 lines could run thousands of tests.
SPOT_SELECTION_MAX_IDS = 200
#: The whole spot decision — the selector answer AND the ``pytest
#: --collect-only`` count that bounds it — answers inside this many
#: seconds or counts as wide: deciding alone must not out-cost the gate.
SPOT_SELECTION_TIMEOUT_S = 180


#: The line ``pytest --collect-only -q`` ends with — ``N/M tests
#: collected`` when a ``-m`` filter deselected some; N is what the gate
#: would run.
_COLLECTED_RE = re.compile(r"(\d+)(?:/\d+)? tests? collected")


def _collected_count(tree: Path, targets: list[str], timeout_s: float) -> int | None:
    """Items the checked gate would run for ``targets`` — the
    ``uv run pytest --collect-only -q -m 'not slow'`` count in ``tree``
    (the selector's ``uv run`` already synced the project env; ``-m
    'not slow'`` is the checked lane's own filter). ``None`` — collect
    errors (a target importing an extra the host venv lacks), fails or
    outruns ``timeout_s`` — counts as unbounded: the caller lands
    ``--quick``."""
    try:
        cp = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                "--collect-only",
                "-q",
                "-m",
                "not slow",
                *targets,
            ],
            cwd=str(tree),
            # Collect imports the branch's conftest — host-side branch
            # code, so the scrubbed env with no credential (gr659929).
            env={**land_env(), "UV_PYTHON": ship_python()},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    counts = _COLLECTED_RE.findall(cp.stdout)
    return int(counts[-1]) if counts else None


def spot_selection(tree: Path) -> bool:
    """True when the checked gate for this diff is a small spot check,
    decided inside :data:`SPOT_SELECTION_TIMEOUT_S` for both probes:
    the selector answers non-FULL with at least one target AND a real
    ``pytest --collect-only`` over those targets reports at most
    :data:`SPOT_SELECTION_MAX_IDS` items. Everything else — ``FULL``, an
    empty answer, ``testmon=1`` (``ship --checked`` then runs the map's
    whole impacted set on top of the static targets; bounding it means
    collecting the entire suite, which the host venv cannot do — missing
    extras error — and which alone would out-cost the spot gate), a
    failed/slow probe, or a wider count — lands ``--quick`` and GitHub
    CI is the gate (gr599427)."""
    budget = time.monotonic() + SPOT_SELECTION_TIMEOUT_S
    out = _selector_stdout(tree, SPOT_SELECTION_TIMEOUT_S)
    if out is None:
        return False
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if lines == ["FULL"]:
        return False
    # The worktree family prints line 1 ``testmon=0|1`` then one pytest
    # target per line; testmon=1 adds the map's impacted set on top.
    if lines and lines[0].startswith("testmon="):
        if lines[0] == "testmon=1":
            return False
        lines = lines[1:]
    remaining = budget - time.monotonic()
    if not lines or remaining <= 0:
        return False
    n = _collected_count(tree, lines, remaining)
    return n is not None and n <= SPOT_SELECTION_MAX_IDS


def _planned_flag(meta: dict[str, Any] | None, params: dict[str, Any]) -> str:
    """The flag submit picked for this job — the default when a handle
    carries none (the engine/spot stamps ride the job meta)."""
    m = meta or {}
    return ship_flag(
        params,
        engine=bool(m.get("engine_paths")),
        spot=bool(m.get("spot_selection")),
    )


#: The ✖ line ``scripts/ship --checked`` dies with when the impacted-tests
#: selector answers FULL for the diff (a migration or a harness file): the
#: checked lane cannot run a full selection by design, so the refusal names
#: the lane, not the branch (jo570215; a builder handed it as a fix request
#: "repaired" the migration instead, gr570221).
FULL_SELECTOR_MARK = "the selector answers FULL"


def full_fallback_flag() -> str | None:
    """The flag a FULL-selector refusal relaunches with: ``--quick`` (lint
    only; the release gate on ``/go`` runs the full suite before deploy —
    Reto 2026-11-05, me570388; engine lands included since gr599427 —
    no land relaunches ``--full``) unless ``PRECIS_LAND_FULL_FALLBACK``
    is ``off``, which keeps the refusal a red."""
    value = os.environ.get("PRECIS_LAND_FULL_FALLBACK", "quick").strip().lower()
    return None if value in ("off", "0", "no", "false") else "--quick"


#: Substrings of ship's own refusal lines (``die`` in ``scripts/ship``'s
#: qland drift guard) that are about MAIN's state, not the branch's: the
#: land is deferred and ship relaunched, not the slice reopened.
MAIN_REFUSAL_MARKS: tuple[str, ...] = (
    "main has no all-green shard matrix",
    "main's last all-green shard matrix is",
)
#: Substrings of ship's host-side refusals (``docker_disk_preflight`` in
#: ``scripts/lib/docker-disk.sh``, exit 3, printed to stderr without a ✖):
#: the lander's own node cannot run a gate right now. Same deferral as a
#: main refusal (gr552214): nothing on the branch caused it, and a builder
#: cannot change it.
#: ``ship --onto``'s round lock is non-blocking: a release-fix land that
#: reaches its push while a sibling's (or the release's own cut or
#: deploy step) holds the lock dies on it with nothing pushed. The
#: holder is seconds from done, so this wait is its own short one
#: (:data:`RELEASE_BUSY_BACKOFF_S`, counted in ``handle.release_busy``
#: up to :data:`MAX_RELEASE_BUSY_WAITS`) — on the half-hour main-refusal
#: backoff the fixers of one red release, building side by side, landed
#: half an hour apart.
RELEASE_BUSY_MARK = "release lifecycle busy"

#: The red reason's stable first words when merging the moved release head
#: into a release fix's branch conflicts (gr738762):
#: ``plan_dispatch._MECHANICAL_REDS`` keys its "merge, never rebase on
#: main" brief on it.
RELEASE_SYNC_CONFLICT_MARK = "release sync conflict"
RELEASE_BUSY_BACKOFF_S = 60
MAX_RELEASE_BUSY_WAITS = 30
HOST_REFUSAL_MARKS: tuple[str, ...] = (
    # release-train T3 review F4: a wait on a sibling, not a red: defer.
    RELEASE_BUSY_MARK,
    "docker VM disk has",
    "disk probe failed:",
    # The docker CLI itself is broken for the lander's user (no compose
    # plugin: every `docker compose …` dies parsing its own flags; jo553514).
    # Ship's ✖ then misreads the dead gate as a lockfile problem, so the host
    # line wins over the ✖ in :func:`poll`.
    "unknown shorthand flag: 'f' in -f",
    "is not a docker command",
    "Cannot connect to the Docker daemon",
    # No gate image on the node: compose cannot pull `precis-dev` and the
    # fallback build has no local :premodels seed (jo553766). The node needs
    # `scripts/build-image precis-dev`, not a branch fix.
    "pull access denied for precis-dev",
    "load metadata for precis-mcp:premodels",
)
#: Deferrals tolerated per land; the next main refusal fails the job with a
#: "land BLOCKED" handoff (nothing wrong on the branch).
MAX_MAIN_REFUSALS = 8
#: Seconds a deferred land waits before ship runs again
#: (``PRECIS_LAND_REFUSAL_BACKOFF_S`` overrides): main's state changes on the
#: scale of a /go, not of a poll, so an immediate retry only burns the cap.
MAIN_REFUSAL_BACKOFF_S = 1800
#: The shapes naming a gate process the host killed, not a verdict on the
#: diff — ANCHORED to line start, because a genuinely red run whose
#: pytest failure output quotes the literals (this feature's own
#: KILL_LOG/XDIST_LOG constants printed in a failure report) must read
#: as an ordinary red. Ship's own OOM note is a line STARTING with its
#: ✖ mark (``gate_oom_note``, scripts/lib/gate-slot.sh; exit 137 — the
#: docker VM's OOM killer or the container's own mem_limit, gr570133)
#: and pytest-xdist's dead-worker report starts the line
#: ``worker 'gwN' crashed while running '<test>'``. A red log carrying
#: one defers like a host refusal, under ``handle.gate_kills``.
GATE_KILL_137_RE = re.compile(r"^✖ .*exited 137 \(SIGKILL\)")
GATE_KILL_XDIST_RE = re.compile(r"^worker 'gw\d+' crashed while running")
#: Exit-137 deferrals tolerated per land; the next one fails the job with a
#: host handoff naming the docker VM. An xdist "crashed while running" gets
#: ONE retry — a deterministic crash is a branch problem.
MAX_GATE_KILL_RETRIES = 2
#: Seconds a gate-killed land waits before ship runs again
#: (``PRECIS_LAND_GATE_KILL_BACKOFF_S`` overrides): the sweep on relaunch
#: may have freed the VM, so the retry is quick.
GATE_KILL_BACKOFF_S = 120
#: The cause a gate-kill deferral or blocked handoff names on the job.
GATE_KILL_CROSS = "gate killed (exit 137, VM memory) - infrastructure, not the branch"
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: The flock file in the worktree's gitdir (a PR land's worktree, a land
#: minted before gr741695). One land holds it for the ship's whole life —
#: the fd is passed to the launched ship process — so a second land on the
#: same worktree finds it held: its own stamp means adopt the running ship;
#: a foreign or orphaned holder means a retryable busy (gr650484). A land
#: that cut its own clone (gr741695) holds a per-todo file of this shape in
#: the host checkout's git common dir instead (:func:`_land_lock_where`):
#: every land job of one todo has a folder of its own, so a per-folder file
#: could no longer tell a second job that a ship of the same slice, left
#: running by a job that ended, still owns the todo's mirror.
LAND_LOCK_FILE = "precis-land.lock"
LAND_LOCK_TODO_FILE = "precis-land-td{todo}.lock"

#: scripts/ship's own per-worktree lock dir in the gitdir
#: (scripts/lib/ship-lock.sh). A live holder means a ship is already in
#: flight in this worktree — from a land or a hand run.
SHIP_LOCK_DIR = "precis-ship-worktree.lock.d"

#: An ``index.lock`` this old, with no live git in the checkout and no
#: process holding it open, is reaped before the ship — the same
#: keep-on-doubt rule as scripts/reap-index-locks' STALE_SECONDS.
INDEX_LOCK_STALE_S = 600

#: How long a live ``index.lock`` is waited on before the job fails
#: retryable (``attempt_free`` — the tick re-mints the land; the red
#: path would charge the branch for infra that is not its fault).
INDEX_LOCK_WAIT_S = 30.0

#: How far down origin/main the already-landed subject scan reads. A
#: missed match costs one ship run, not a wrong record.
ALREADY_LANDED_WINDOW = 400

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The slice todo.
        "todo_id": {"type": "integer"},
        # A pull request's pinned worktree (dep_check's relock). A slice
        # land cuts its own clone and ignores this (gr741695).
        "worktree": {"type": "string"},
        # The slice's branch, slice/<id>.
        "branch": {"type": "string"},
        # The commit to land: the builder's last real commit (gr741695;
        # also the build tip the overlap hold uses).
        "head": {"type": "string"},
        # The hub ref that holds ``head`` (``refs/factory/build/<todo>/<job>``);
        # absent on a land minted before gr741695 (``_slice_ws.import_legacy``).
        "ref": {"type": "string"},
        # The lane holding the lease (released on a red land).
        "lane": {"type": "string"},
        # ssh_node's host pin: the host whose hub holds the work.
        "target_node": {"type": "string"},
        # A pull request's land (worktree/branch then optional, see above).
        "pr": {"type": "integer"},
        # True asks for --checked — honoured only when the selector's
        # answer is a spot selection (:func:`ship_flag`); else --quick.
        "checked": {"type": "boolean"},
        # The repo the slice was built in (td659522); only the default
        # repo lands here, see :func:`_repo_refusal`.
        "repo": {"type": "string"},
    },
    "required": ["todo_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Land one finished slice: scripts/ship --quick with the squash subject "
    "(the brief's `subject:` line, else the last non-test commit's) in a "
    "fresh clone it cuts from the host's hub at params.head (held by "
    "params.ref), the landed sha on the todo and into the plan's "
    "pending sha-in-prod gate, then drop the clone, the todo's attempt refs "
    "in the hub and any legacy slice/<id> branch on origin. A red ship "
    "drops the clone too and reopens the todo with attempt:<n>; "
    "a refusal about main's state (drift guard) retries ship every 30 min, up to 8 times. "
    "Minted by run_slice; with params.pr it lands a pull request's head "
    "(slice/pr-<n>, --checked when the selection is a spot check) and "
    "closes the PR. Refused while "
    "PRECIS_FACTORY=off."
)


# ── Config ─────────────────────────────────────────────────────────


def wall_seconds() -> float:
    """Type cap for ssh_node's lease and kill deadline
    (``PRECIS_LAND_SLICE_WALL_S``, default 1 h)."""
    try:
        n = int(os.environ.get("PRECIS_LAND_SLICE_WALL_S", ""))
    except ValueError:
        return float(_DEFAULT_WALL_S)
    return float(n if n > 0 else _DEFAULT_WALL_S)


def refusal_backoff_s() -> int:
    """Deferral wait (``PRECIS_LAND_REFUSAL_BACKOFF_S``, default
    :data:`MAIN_REFUSAL_BACKOFF_S`; never negative)."""
    try:
        n = int(os.environ.get("PRECIS_LAND_REFUSAL_BACKOFF_S", MAIN_REFUSAL_BACKOFF_S))
    except ValueError:
        return MAIN_REFUSAL_BACKOFF_S
    return max(n, 0)


def gate_kill_backoff_s() -> int:
    """Deferral wait after a gate kill (``PRECIS_LAND_GATE_KILL_BACKOFF_S``,
    default :data:`GATE_KILL_BACKOFF_S`; never negative)."""
    try:
        n = int(os.environ.get("PRECIS_LAND_GATE_KILL_BACKOFF_S", GATE_KILL_BACKOFF_S))
    except ValueError:
        return GATE_KILL_BACKOFF_S
    return max(n, 0)


def index_lock_stale_s() -> float:
    """Age over which a git-less, unheld ``index.lock`` is reaped
    (``PRECIS_LAND_INDEX_LOCK_STALE_S``, default
    :data:`INDEX_LOCK_STALE_S`)."""
    try:
        return float(
            os.environ.get("PRECIS_LAND_INDEX_LOCK_STALE_S") or INDEX_LOCK_STALE_S
        )
    except ValueError:
        return INDEX_LOCK_STALE_S


def index_lock_wait_s() -> float:
    """The brief wait on a live ``index.lock`` before the retryable fail
    (``PRECIS_LAND_INDEX_LOCK_WAIT_S``, default :data:`INDEX_LOCK_WAIT_S`)."""
    try:
        return float(
            os.environ.get("PRECIS_LAND_INDEX_LOCK_WAIT_S") or INDEX_LOCK_WAIT_S
        )
    except ValueError:
        return INDEX_LOCK_WAIT_S


def ship_bin(tree: Path) -> str:
    """``PRECIS_SHIP_BIN`` (the test seam), else the worktree's own
    ``scripts/ship``."""
    return os.environ.get("PRECIS_SHIP_BIN") or str(tree / "scripts" / "ship")


#: xdist workers for the lander's ``--checked`` pytest when
#: ``PRECIS_LAND_TEST_NPROC`` is unset: half of scripts/test's default six.
#: A 5k–8k static-wide selection OOM-killed a worker at -n6 on the agent host
#: twice (gr567471, Reto 2026-11-05 "ship it").
DEFAULT_TEST_NPROC = 3


def test_nproc() -> str:
    """The xdist worker count the lander hands the ship, as ``PRECIS_GATE_N``
    (what ``scripts/ship --checked`` expands into its ``pytest -n``) and
    ``PRECIS_TEST_NPROC`` (what ``scripts/test`` reads): ``PRECIS_LAND_TEST_NPROC``
    when it is a non-negative integer, else :data:`DEFAULT_TEST_NPROC`. A
    string, because it rides an env dict."""
    raw = os.environ.get("PRECIS_LAND_TEST_NPROC", "")
    try:
        n = int(raw)
    except ValueError:
        return str(DEFAULT_TEST_NPROC)
    return str(n if n >= 0 else DEFAULT_TEST_NPROC)


#: How many of the shared gate semaphore's slot dirs a land launch may
#: claim (``PRECIS_GATE_SLOTS`` on the ship/test env, td599058):
#: ``scripts/lib/gate-slot.sh`` derives the fleet cap from the docker VM —
#: 6 on the factory host's — and a wave of lands and their verify probes
#: must not take it all. Every land launch sees only this many slot dirs,
#: so factory gates queue among themselves on at most
#: :data:`DEFAULT_LAND_GATE_SLOTS` slots and the rest stay free for
#: interactive gates. ``PRECIS_LAND_GATE_SLOTS`` narrows to 1 (a small
#: VM); anything outside 1–2 falls back to the default.
DEFAULT_LAND_GATE_SLOTS = 2


def land_gate_slots() -> str:
    """The ``PRECIS_GATE_SLOTS`` the lander hands ship/test launches, a
    string for the env dict: ``PRECIS_LAND_GATE_SLOTS`` when it is 1 or 2,
    else :data:`DEFAULT_LAND_GATE_SLOTS`."""
    try:
        n = int(os.environ.get("PRECIS_LAND_GATE_SLOTS", ""))
    except ValueError:
        n = 0
    return str(n if 1 <= n <= 2 else DEFAULT_LAND_GATE_SLOTS)


#: Parent-env names every land-spawned child may inherit; everything else
#: is dropped. gr659929: ``_launch_ship`` handed ship the whole worker env
#: — the DB DSN, the vault variables, every token — and ship's host-side
#: steps run code out of the slice tree (the checked-lane selector, the
#: dotless drift helper), so any commit could read the no-expiry factory
#: token. The factory git token is not on the list: the lander's own
#: fetch/push/gh launches add it through
#: :func:`precis.workers.factory_git.git_auth_env` on top of
#: :func:`land_env`, and ship gets **no credential at all** — its origin
#: is the lander's local mirror
#: (:func:`~precis.workers.factory_git_mirror.mirror_ship_env`), and the
#: token reaches the real origin only in the lander's own mirror populate
#: and sync calls — never as an env name on a ship launch, never on an
#: inherited fd. The gate children — the verify ``scripts/test``, the
#: selector and the collect probe, all of which run branch code on the
#: host — get the bare list.
LAND_ENV_NAMES: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "TMPDIR",
    "TZ",
    # A socket path, not the keys; colima/ssh-backed docker may want it.
    "SSH_AUTH_SOCK",
    # The docker endpoint on the slice host (colima/OrbStack unix socket,
    # or a tcp endpoint the unit pins) and gh's config-home override.
    "DOCKER_HOST",
    "GH_CONFIG_DIR",
    # The squash commit's identity when the unit env carries it (else the
    # checkout's git config answers).
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_PAGER",
    "PAGER",
    # ship's bounded git-network retry knob (scripts/lib/git-network).
    "GIT_NETWORK_SHELL_TIMEOUT",
)

#: Prefixes inherited as a class, subject to :data:`_LAND_ENV_SECRET_RE`:
#: the ship/test/gate/land knob families (every ``PRECIS_SHIP_*``,
#: ``PRECIS_GATE_*``, ``PRECIS_TEST_*``, ``PRECIS_QLAND_*``,
#: ``PRECIS_LAND_*`` override an operator set on the unit keeps working),
#: the locale and XDG dirs, uv's cache/offline knobs, and the
#: docker/compose/colima tunables. ``GH_*`` is deliberately not a prefix —
#: ``GH_TOKEN`` enters only through the credential wiring.
_LAND_ENV_PREFIXES: tuple[str, ...] = (
    "PRECIS_",
    "LC_",
    "XDG_",
    "UV_",
    "DOCKER_",
    "COMPOSE_",
    "COLIMA_",
)

#: A name matching this is a credential, never a knob — dropped from the
#: prefix inheritance: ``PRECIS_FACTORY_GIT_TOKEN``, ``PRECIS_MCP_TOKEN``,
#: ``PRECIS_DATABASE_URL``/``PRECIS_TEST_PG_URL`` (a ``_URL`` can carry
#: userinfo), ``UV_INDEX_URL`` and friends.
_LAND_ENV_SECRET_RE = re.compile(
    r"TOKEN|SECRET|PASSW|CREDENTIAL|_KEY|_DSN|DATABASE|_URL|OAUTH|PRIVATE"
    r"|BEARER|COOKIE|_PEM|_CERT"
)


_CEILING_CACHE: dict[str, tuple[str, ...]] = {}


def _worktrees_ceiling(root_env: str) -> tuple[str, ...]:
    """The host checkout's worktrees directory, as ``GIT_CEILING_DIRECTORIES``
    entries (the path as named and, when different, resolved — git compares
    the literal text). The land clone and every other folder under it must
    be a repository of its own: a folder that lost its ``.git`` would
    otherwise resolve to the host checkout above it, and a ``reset``,
    ``checkout``, ``clean`` or lock removal would act on that
    (gr742505). Cached per ``PRECIS_SLICE_ROOT`` value (``root_env``): with
    none set, resolving the root runs git. A failure is not cached — it
    answers ``()`` for that call only."""
    cached = _CEILING_CACHE.get(root_env)
    if cached is not None:
        return cached
    try:
        folder = _rs.worktrees_dir(_rs.slice_root())
    except Exception:
        return ()
    entries = [str(folder)]
    try:
        real = str(folder.resolve())
    except OSError:
        real = str(folder)
    if real not in entries:
        entries.append(real)
    _CEILING_CACHE[root_env] = tuple(entries)
    return _CEILING_CACHE[root_env]


def _with_ceiling(env: dict[str, str], *extra: Path) -> dict[str, str]:
    """``env`` with ``GIT_CEILING_DIRECTORIES`` naming the host's worktrees
    directory (:func:`_worktrees_ceiling`) and each ``extra`` directory, after
    whatever the worker env already named. The ceiling stops git walking UP
    into those directories; a process whose cwd is the host checkout itself,
    or a folder that has its own ``.git`` (a clone, or a linked worktree's
    ``.git`` file), never reaches it."""
    entries = [e for e in env.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep) if e]
    entries += _worktrees_ceiling(os.environ.get("PRECIS_SLICE_ROOT", ""))
    for folder in extra:
        entries += [str(folder), os.path.realpath(folder)]
    deduped = list(dict.fromkeys(entries))
    if deduped:
        env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(deduped)
    return env


def land_env() -> dict[str, str]:
    """The scrubbed environment every land-spawned child starts from:
    :data:`LAND_ENV_NAMES` plus the :data:`_LAND_ENV_PREFIXES` names that
    are not credential-shaped. Callers add the factory credential on top:
    :func:`precis.workers.factory_git.git_auth_env` for the lander's own
    fetch/push/gh launches (trusted code). Ship gets no credential: its
    origin is the lander's local mirror
    (:func:`~precis.workers.factory_git_mirror.mirror_ship_env`), which
    the lander itself populates and syncs under the credential wiring.
    ``GIT_NO_REPLACE_OBJECTS=1`` is SET, not inherited: the mirror, the
    shared dir and the worktree gitdir are all same-uid writable, and a
    ``refs/replace/*`` planted for an oracle-verified sha must never
    substitute the object a child's trusted read resolves — ship's
    fetch/merge, the drift helper's own ``rev-list``, a gate child's
    git calls alike (review td660513, pre-land pass). ``PATH`` gains the
    worker user's ``~/.local/bin`` appended when ``uv`` is installed
    there but missing from the inherited PATH — a non-login coder
    service carries no shell-hook PATH (gr702644), and ship plus its
    nested ``/usr/bin/env uv`` helpers must still resolve it; the append
    preserves the operator's PATH precedence
    (:func:`~precis.workers.job_types.run_slice.path_with_user_uv`, the
    same rule ``slice_env`` gives the session itself). ``GIT_CEILING_DIRECTORIES``
    names the host checkout's worktrees directory (:func:`_with_ceiling`), so
    a land folder that lost its ``.git`` is an error for ship and everything
    under it, never the host checkout above (gr742505)."""
    env = {k: os.environ[k] for k in LAND_ENV_NAMES if os.environ.get(k)}
    for name, value in os.environ.items():
        if (
            name not in env
            and name.startswith(_LAND_ENV_PREFIXES)
            and not _LAND_ENV_SECRET_RE.search(name)
        ):
            env[name] = value
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    patched = _rs.path_with_user_uv(env.get("PATH"))
    if patched != (env.get("PATH") or os.defpath):
        env["PATH"] = patched
    return _with_ceiling(env)


# ── Seams (tests monkeypatch these) ────────────────────────────────


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd``; never raises on a non-zero exit.

    Never the worker's ambient env (review td660513, gr659929):
    ``run_slice._git`` inherits ``os.environ`` wholesale — the factory
    token and its ``GH_TOKEN`` shadow included — and the repos these
    calls touch are branch-writable, so a planted ``hooks/*``,
    ``core.fsmonitor``, filter or transport redirect would meet the
    token in a spawned child's env. The scrubbed local env drops the
    credential names and blinds system/global config, and hooks are
    off (:data:`~precis.workers.factory_git_mirror.TRUSTED_ARGS`). The
    transports that need the credential — the origin branch delete in
    :func:`_cleanup`, :func:`_push_branch` — ride the mirror's fresh
    ops gitdir instead.

    A ``cwd`` directly under a ``.claude/worktrees`` directory (a land's
    clone, a PR land's or a probe's worktree — in any repository, the
    second-repository lander included) also gets that directory as a git
    ceiling: if the folder lost its ``.git`` the call fails instead of
    running in the checkout above it (gr742505). The folder's own ``.git``
    file or directory is found first, so a worktree works as before."""
    folder = Path(cwd).parent
    extra = (
        (folder,)
        if folder.name == "worktrees" and folder.parent.name == ".claude"
        else ()
    )
    return subprocess.run(
        ["git", *_factory_git_mirror.TRUSTED_ARGS, *args],
        cwd=str(cwd),
        env=_factory_git_mirror.local_env(_with_ceiling(land_env(), *extra)),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=300,
    )


def _launch(
    argv: list[str],
    *,
    cwd: Path,
    log: Path,
    exit_file: Path,
    env: dict[str, str],
    pass_fds: tuple[int, ...] = (),
    store: Any = None,
    work_class: str = "land",
    pid_file: Path | None = None,
) -> int:
    """Start ``argv`` detached (run_slice's launcher, stdin from
    ``/dev/null``). ``pass_fds`` are fds the child inherits — the land
    flock rides one so it is held for the ship's whole life (gr650484).
    With a real ``store`` the launch is admitted first
    (:func:`admission.spawn`); a full host raises
    :class:`admission.Refused`. Returns the pid (= the process group)."""
    return _rs._launch(
        argv,
        cwd=cwd,
        stdin=Path(os.devnull),
        log=log,
        exit_file=exit_file,
        env=env,
        pass_fds=pass_fds,
        store=store,
        work_class=work_class,
        pid_file=pid_file,
    )


def _process_alive(pid: int) -> bool:
    return _rs._process_alive(pid)


def _evict_stack(project: str, *, volumes: bool) -> bool:
    """``docker compose -p <project> down`` on one slice stack
    (:mod:`precis.workers.slice_stacks`); never raises — docker trouble is
    a cleanup note, not a job failure."""
    try:
        return _slice_stacks.evict_project(project, volumes=volumes)
    except Exception:  # pragma: no cover — defensive; the land is real
        log.warning("land_slice: compose down of %s failed", project, exc_info=True)
        return False


def _sweep_stacks(*, keep: Path | None = None) -> list[str]:
    """The pre-launch sweep of gone/idle ``precis-test-slice-*`` stacks on
    this host (:func:`slice_stacks.sweep`); never raises."""
    try:
        return _slice_stacks.sweep(keep=keep)
    except Exception:  # pragma: no cover — defensive; a launch must go on
        log.warning("land_slice: slice-stack sweep failed", exc_info=True)
        return []


def _now() -> datetime:
    """The clock (UTC); a seam so tests move time."""
    return datetime.now(UTC)


def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>`` (the PR close); never raises on a non-zero exit."""
    from precis.workers.job_types import pr_action

    return pr_action._gh(args)


def _pending_gates(store: Any, plan_tag: str, todo_id: int) -> list[tuple[int, bool]]:
    """``(gate id, blocked-by todo_id)`` of every live ``sha-in-prod`` gate
    tagged ``plan_tag`` whose ``args.sha`` is still :data:`PENDING_SHA`."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id,
                   EXISTS (SELECT 1 FROM links l
                            WHERE l.src_ref_id = r.ref_id AND l.dst_ref_id = %s
                              AND l.relation = 'blocked-by')
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
               AND r.meta->'gate'->>'kind' = 'sha-in-prod'
               AND r.meta->'gate'->'args'->>'sha' = %s
             ORDER BY r.ref_id
            """,
            (todo_id, plan_tag, PENDING_SHA),
        ).fetchall()
    return [(int(r[0]), bool(r[1])) for r in rows]


# ── Subject ────────────────────────────────────────────────────────


def with_handle(subject: str, todo_id: int) -> str:
    """``subject`` with ``td<todo_id>`` in its trailing handle list (added
    to an existing ``(grN)`` list, else appended as `` (td<id>)``), the
    text before the list trimmed so the whole fits :data:`SUBJECT_CAP`
    characters."""
    subject = " ".join(subject.split())
    handle = f"td{todo_id}"
    m = _HANDLES_RE.search(subject)
    if m is None:
        head, handles = subject, handle
    else:
        head, handles = subject[: m.start()], m.group(1)
        if handle not in re.findall(_HANDLE, handles):
            handles = f"{handles}, {handle}"
    tail = f" ({handles})"
    head = head.rstrip()
    if len(head + tail) > SUBJECT_CAP:
        head = _rs.cap_subject_chars(head, SUBJECT_CAP - len(tail))
    return head + tail


#: Commit subjects that never label the squash (gr574383): a
#: red-test-first ``test`` commit and ship's own ``wip(<branch>):``
#: snapshot. When every commit is one, the last stands in — a test
#: subject still lands.
_NO_LAND_RE = re.compile(r"^(?:test|wip)(?:\([^)]*\))?!?:")


def squash_subject(
    subjects: list[str], todo_id: int, node: str | None = None
) -> str | None:
    """The squash subject: ``node`` — the brief's stated ``subject:`` line
    (:func:`_node_subject`) — when given; else from the branch's commit
    subjects (oldest first), the LAST non-test/wip commit — never the
    first commit blindly and never an early commit just for naming
    ``td<todo_id>`` (gr574383: a red-test-first commit used to win, and
    the slice landed under its test subject; the td599723 review: an
    early ``feat`` still beat the fix-ups that came after it). Through
    :func:`with_handle`. ``None`` for no commits."""
    subjects = [s.strip() for s in subjects if s.strip()]
    if not subjects:
        return None
    if node and node.strip():
        return with_handle(node, todo_id)
    pool = [s for s in subjects if not _NO_LAND_RE.match(s)]
    if not pool:
        # Every commit is a test/wip: the last stands in — a test subject
        # still lands, and the newest is still not the first blindly.
        return with_handle(subjects[-1], todo_id)
    return with_handle(pool[-1], todo_id)


#: A ``subject:`` line's value is taken only when it already looks
#: conventional — the same type set ``scripts/ship``'s subject lint
#: enforces — so a prose "subject:" in a brief cannot poison the land.
_SUBJECT_LINE_RE = re.compile(
    r"^subject:\s*("
    r"(?:feat|fix|docs|test|chore|build|refactor|perf|ci|release|revert)"
    r"(?:\([^)]*\))?!?: \S.*)$"
)


def _node_subject(store: Any, todo_id: int) -> str | None:
    """The brief's stated squash subject: the first conventional
    ``subject:`` line on the todo — its body first, then the newest
    ``brief:``/``handoff:`` comment (the builder's final hand-off may
    carry one). ``None`` when the brief states none (gr574383)."""
    if store is None:
        return None
    body, latest, _latest_ord = _rs._brief_parts(store, todo_id)
    for text in (body, latest or ""):
        for line in text.splitlines():
            m = _SUBJECT_LINE_RE.match(line.strip())
            if m:
                return m.group(1).strip()
    return None


def _branch_subjects(tree: Path, onto: str | None = None) -> list[str] | None:
    """The branch's own commit subjects, oldest first — ahead of
    ``origin/main``, or of ``origin/<onto>`` for a release fix (its tree
    is cut from the release, whose own fixes are not the slice's).
    Merge commits are
    skipped (gr627889): the land squashes the branch, so a builder's
    ``Merge origin/main into slice/...`` resolving a collision is not a
    candidate squash subject (it failed ship's ``type(scope):`` lint three
    times a chain); ``scripts/ship`` resolves its own message the same way
    (``--no-merges``)."""
    proc = _git(
        [
            "log",
            "--reverse",
            "--no-merges",
            "--format=%s",
            f"origin/{onto or 'main'}..HEAD",
        ],
        tree,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.splitlines()


# ── Ship output ────────────────────────────────────────────────────


def _lines(log_text: str) -> list[str]:
    return [_ANSI_RE.sub("", ln).rstrip() for ln in log_text.splitlines()]


def ship_status(log_text: str) -> str | None:
    """The last ``SHIP_STATUS: <token>`` token in ship's output."""
    for line in reversed(_lines(log_text)):
        m = _STATUS_RE.search(line)
        if m:
            return m.group(1)
    return None


def cross_line(log_text: str) -> str | None:
    """The last ``✖`` line in ship's output (its failure verdict)."""
    for line in reversed(_lines(log_text)):
        if "✖" in line:
            return line.strip()
    return None


_GATE_ERROR_RE = re.compile(
    r"^(?:\S+\.py:\d+(?::\d+)?: (?:error|[A-Z]{1,4}\d{3,4})\b|FAILED |E {3})"
)
#: Ruff's FULL format (the pinned ruff's default): a ``CODE [*] message``
#: header line, then `` --> path:L:C`` on a following line. Condensed to
#: the concise shape ``path:L:C CODE message`` so both ruff dialects read
#: alike on the handoff (gr573347: a full-format ruff red reached the todo
#: as the bare ✖ verdict).
_RUFF_HEAD_RE = re.compile(r"^([A-Z]{1,4}\d{3,4})(?:\s+\[\*\])?\s+(.+)$")
_RUFF_LOC_RE = re.compile(r"^-->\s*(\S+?):(\d+):(\d+)\s*$")
_GATE_ERROR_LINES = 20
_GATE_ERROR_CHARS = 2500


def gate_error_lines(log_text: str) -> str:
    """The lint gate's own error lines in ship's output — mypy
    (``path.py:N: error: …``), ruff concise (``path.py:N:M: F401 …``) and
    full (``F401 [*] msg`` + `` --> path:L:C``, condensed to the concise
    shape) and pytest (``FAILED …`` / ``E   …``) — joined, at most
    :data:`_GATE_ERROR_LINES` and :data:`_GATE_ERROR_CHARS`; ``''`` when
    there are none. The next session's brief starts from the ``handoff:``
    comment, so the red's reason has to ride it (td565331: six mypy errors
    reached the todo as the one ✖ verdict line and nothing else)."""
    lines = _lines(log_text)
    out: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if _GATE_ERROR_RE.match(s):
            out.append(s)
        else:
            head = _RUFF_HEAD_RE.match(s)
            if head:
                for ahead in lines[i + 1 : i + 4]:
                    loc = _RUFF_LOC_RE.match(ahead.strip())
                    if loc:
                        out.append(
                            f"{loc.group(1)}:{loc.group(2)}:{loc.group(3)}: "
                            f"{head.group(1)} {head.group(2)}"
                        )
                        break
        if len(out) >= _GATE_ERROR_LINES:
            break
    text = "\n".join(out)
    if len(text) > _GATE_ERROR_CHARS:
        text = text[:_GATE_ERROR_CHARS].rstrip() + " …"
    return text


_GATE_EXCERPT_LINES = 40
_GATE_EXCERPT_CHARS = 3000


def gate_red_excerpt(log_text: str) -> str:
    """The last :data:`_GATE_EXCERPT_LINES` non-blank lines before the last
    ``✖`` line — the step banner plus the tool output leading to the
    verdict — capped at :data:`_GATE_EXCERPT_CHARS` (the tail kept: what
    died is nearest the ✖). ``''`` with no ``✖``. Fallback for
    :func:`gate_error_lines`: when no line parses, this is all the next
    session gets — the land log dies with the worktree (gr573347)."""
    lines = _lines(log_text)
    cross = max((i for i, ln in enumerate(lines) if "✖" in ln), default=None)
    if cross is None:
        return ""
    kept = [ln for ln in lines[:cross] if ln.strip()]
    text = "\n".join(kept[-_GATE_EXCERPT_LINES:])
    if len(text) > _GATE_EXCERPT_CHARS:
        text = "…\n" + text[-_GATE_EXCERPT_CHARS:]
    return text


#: Ship's verdict when the checked lane's pytest is red (``die`` in
#: ``scripts/ship``): only this cross triggers the outside-diff verify —
#: a lint or contract red is the branch's outright.
CHECKED_RED_MARK = "checked selection is RED"
#: The ``short test summary info`` banner; FAILED/ERROR lines count only
#: inside its block (which ends at the next ``===`` banner) — an
#: unindented captured-stdout line elsewhere cannot inject a fake id.
_SUMMARY_START_RE = re.compile(r"^=+\s*short test summary info\s*=+$")
_FAILED_ID_RE = re.compile(r"^(?:FAILED|ERROR)\s+(.+)$")


def _summary_id(rest: str) -> str | None:
    """The id half of a summary line: the text before the first `` - ``
    OUTSIDE square brackets — a parametrized id may itself contain
    `` - `` inside them (``t[a - b]``). ``None`` for unbalanced brackets
    or an empty id."""
    depth = 0
    for i, ch in enumerate(rest):
        if depth == 0 and rest.startswith(" - ", i):
            head = rest[:i].strip()
            return head or None
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                return None
    if depth != 0:
        return None
    head = rest.strip()
    return head or None


def failed_test_ids(log_text: str) -> list[str]:
    """Pytest's summary ids — ``FAILED <id> - <reason>`` and collection
    ``ERROR <id> - <reason>`` — de-duplicated in order, and only inside
    the ``=== short test summary info ===`` block. One malformed line
    spoils the set: unbalanced brackets or an id without ``::`` returns
    ``[]``, so the outside-diff check yields no outside set and the red
    stays ordinary."""
    out: list[str] = []
    in_summary = False
    for line in _lines(log_text):
        if _SUMMARY_START_RE.match(line):
            in_summary = True
            continue
        if not in_summary:
            continue
        if line.startswith("="):
            break
        m = _FAILED_ID_RE.match(line)
        if m:
            ident = _summary_id(m.group(1))
            if ident is None or "::" not in ident:
                return []
            if ident not in out:
                out.append(ident)
    return out


def outside_diff_ids(tree: Path, ids: list[str]) -> list[str]:
    """The ids whose test file ``git diff --name-only origin/main...HEAD``
    in ``tree`` does not name. An unanswerable diff returns ``[]`` — an
    unverifiable red stays an ordinary red, never attempt-free."""
    proc = _git(["diff", "--name-only", "origin/main...HEAD"], tree)
    if proc.returncode != 0:
        return []
    changed = {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}
    return [i for i in ids if i.split("::", 1)[0] not in changed]


def _selector_stdout(tree: Path, timeout_s: float) -> str | None:
    """``scripts/impacted-tests --stdin``'s stdout on the branch diff —
    ``None`` when the diff or the selector cannot be read (missing
    script, non-zero exit, timeout)."""
    diff = _git(["diff", "--name-only", "origin/main...HEAD"], tree)
    sel = tree / "scripts" / "impacted-tests"
    if diff.returncode != 0 or not sel.is_file():
        return None
    try:
        cp = subprocess.run(
            [str(sel), "--stdin"],
            input=diff.stdout,
            cwd=str(tree),
            # The slice tree's selector — host-side, so the scrubbed env
            # with no credential (gr659929).
            env={**land_env(), "UV_PYTHON": ship_python()},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout


def _checked_selection_full(tree: Path) -> bool | None:
    """``True`` when this diff's checked selection IS the suite — the
    ``scripts/impacted-tests --stdin`` FULL answer ``scripts/ship
    --checked`` refuses and ``scripts/test --selected`` expands to the
    whole suite. Then no deselect gate runs (td602151: the minus-ids
    run would BE the suite, and no land runs the suite); the main-red
    ids are recorded as excluded and the land relaunches ``--quick``.
    ``None`` — unreadable selector (timeout or error) — counts as FULL
    at the caller (gr586292): ``scripts/test --selected`` re-reads the
    selector without a timeout and runs the whole suite on a FULL
    answer, and no land runs the suite."""
    out = _selector_stdout(tree, 120)
    if out is None:
        return None
    return out.strip() == "FULL"


def is_main_refusal(cross: str | None) -> bool:
    """True when ``cross`` (a :func:`cross_line` result) is ship refusing on
    main's state (:data:`MAIN_REFUSAL_MARKS`), not on the branch."""
    return bool(cross) and any(mark in str(cross) for mark in MAIN_REFUSAL_MARKS)


def is_host_refusal(cross: str | None) -> bool:
    """True when ``cross`` is ship refusing on the lander's own host
    (:data:`HOST_REFUSAL_MARKS`: the docker VM disk preflight)."""
    return bool(cross) and any(mark in str(cross) for mark in HOST_REFUSAL_MARKS)


def host_refusal_line(log_text: str) -> str | None:
    """The last host-side refusal line in ship's output, or ``None``. These
    carry no ✖ (jo552075: ``scripts/ship: docker VM disk has 4 GB free; …``),
    so :func:`cross_line` misses them."""
    for line in reversed(_lines(log_text)):
        if is_host_refusal(line):
            return line.strip()
    return None


def gate_kill_line(log_text: str, exit_code: int | None) -> str | None:
    """The last gate-kill line in ship's output, or ``None``. The WHOLE
    log is scanned — ship's later generic "gate is RED" ✖ at the tail
    must not hide an earlier kill line — but the marks stay anchored
    (:data:`GATE_KILL_137_RE`, :data:`GATE_KILL_XDIST_RE`): failure output
    quoting the literals is a mid-line match and an ordinary red. A clean
    exit cannot be a kill (``scripts/test`` retries a lone 137 itself, so
    a mark can sit in a successful log); ``None`` — a dead ship that left
    no exit file — stays eligible: it is itself a kill suspect."""
    if exit_code == 0:
        return None
    for line in reversed(_lines(log_text)):
        if GATE_KILL_137_RE.match(line) or GATE_KILL_XDIST_RE.match(line):
            return line.strip()
    return None


def refusal_side(cross: str | None) -> str | None:
    """``"main"`` / ``"host"`` for a refusal that is not about the branch,
    else ``None``."""
    if is_main_refusal(cross):
        return "main"
    if is_host_refusal(cross):
        return "host"
    return None


def nothing_new(log_text: str) -> bool:
    """True when ship took its nothing-new guard (:data:`_NOTHING_NEW_MARK`,
    or ``--onto``'s :data:`_NOTHING_NEW_ONTO_RE`): a green exit that
    squashed nothing, so no sha is the slice's."""
    return any(
        _NOTHING_NEW_MARK in line or _NOTHING_NEW_ONTO_RE.search(line)
        for line in _lines(log_text)
    )


#: Lines ship's ``gate_slot_acquire`` prints while it queues (the lib's
#: wait banner, the position re-announce and the per-slot holder dump).
#: While the newest log line is one of these the land is parked behind
#: sibling gates — the slot wait is real queueing, so it does not eat
#: the job's wall (gr598110 item 4: the wall clock starts when the gate
#: starts).
_GATE_SLOT_CHATTER_RE = re.compile(
    r"^(?:waiting for a gate slot|gate slot: (?:next up|waiting at position)"
    r"|\s+gate slot \d+:|\(steals a slot immediately)"
)
#: Slot chatter that ends the wait — a steal means a slot was won, a
#: give-up or an access refusal means ship exits next.
_GATE_SLOT_DONE_RE = re.compile(
    r"^(?:stole gate slot|gate slot: (?:gave up|cannot|needs write access"
    r"|PRECIS_GATE_WAIT_MAX_S))"
)
#: ``scripts/test``'s queue ticker — its ``hb_start "gate-slot wait"``
#: prints ``scripts/test: gate-slot wait … Ns`` on stderr every 10 s
#: while the run queues inside ``gate_slot_acquire``. In a verify probe's
#: log the ticker displaces the holder dump as the newest line, so the
#: scan below skips it: it carries no queue state of its own and a LIVE
#: wait would otherwise read as working and stop re-arming the deadline
#: (td600305). Only this phase's ticker is transparent — the next
#: phase's (``test db up``, ``pytest``) proves the wait ended.
_GATE_SLOT_HEARTBEAT_RE = re.compile(r"^scripts/test: gate-slot wait\b")


def gate_slot_waiting(log_text: str) -> str | None:
    """The newest gate-slot wait line when the live ship is queued inside
    ``gate_slot_acquire``, else ``None``. Only slot chatter prints while
    it waits, so the wait lasts exactly as long as the newest non-blank
    line is chatter — any other line (the gate's own output, a steal, a
    give-up) means it left the queue. The holder dump is INDENTED
    (``  gate slot 0: …``), so the match runs on the unstripped line —
    stripping first made every real queue log read as working and the
    wait ate the deadline (td599058). ``scripts/test``'s
    ``gate-slot wait`` ticker lines (:data:`_GATE_SLOT_HEARTBEAT_RE`)
    ride the same log every 10 s and would otherwise displace the
    chatter as the newest line (td600305); skipped for the newest-line
    pick, a ticker still answers ``waiting`` on its own when it is all
    the log holds."""
    last = ""
    heartbeat = ""
    for line in _lines(log_text):
        if not line.strip():
            continue
        if _GATE_SLOT_HEARTBEAT_RE.match(line):
            heartbeat = line
        else:
            last = line
    if not last:
        return heartbeat.strip() or None
    if _GATE_SLOT_DONE_RE.match(last):
        return None
    return last.strip() if _GATE_SLOT_CHATTER_RE.match(last) else None


def _phase_started(phase: dict[str, Any]) -> float:
    """Epoch seconds of the phase's ``started_at`` — the deadline's
    anchor; this poll when the stamp is unreadable."""
    try:
        return (
            datetime.strptime(str(phase.get("started_at") or ""), _TS_FMT)
            .replace(tzinfo=UTC)
            .timestamp()
        )
    except ValueError:
        return _now().timestamp()


def _phase_seen(phase: dict[str, Any], now_ts: float) -> float:
    """Stamp ``polled_at`` for this live poll and return the instant the
    phase was last observed — the previous live poll, else its launch.
    The gap since is the current gate-slot wait's floor: nothing in it
    was seen working, so it counts as queue time, not wall."""
    seen = phase.get("polled_at")
    phase["polled_at"] = now_ts
    if isinstance(seen, (int, float)) and not isinstance(seen, bool):
        return float(seen)
    return _phase_started(phase)


def _wait_deadline(phase: dict[str, Any], seen: float, now_ts: float) -> float:
    """The phase's wall-clock deadline during a gate-slot wait, folded
    onto the handle: the stored ``deadline`` — ``started_at`` + wall +
    the waits already observed — grown by the gap since ``seen``.
    Anchoring the count in the handle keeps a re-adopted job on its own
    deadline instead of restarting the wall at the re-adopt poll
    (gr647913). A pre-fix handle carries no ``deadline``: anchored at
    its launch it folds to the old ``now + wall``."""
    deadline = phase.get("deadline")
    base = (
        float(deadline)
        if isinstance(deadline, (int, float)) and not isinstance(deadline, bool)
        else _phase_started(phase) + wall_seconds()
    )
    phase["deadline"] = base + max(now_ts - seen, 0.0)
    return float(phase["deadline"])


def infra_red(log_text: str, exit_code: int | None) -> str | None:
    """The red's class — ``"gate infrastructure"`` — when ship died on the
    gate's own machinery: a gate-kill line (:func:`gate_kill_line`'s
    anchored marks) or a bare exit 137 (128+SIGKILL with or without the
    mark, gr567471's OOM signature). ``None`` on a clean exit or an
    ordinary branch red. xdist's ``[gwN] node down:`` report is NOT a
    mark: it is the ordinary per-worker death line and rides the
    worker-crash rule — one retry, then a branch red. :func:`poll`
    routes every exit 137 — marked or bare — through the ``gate_kills``
    deferral itself, so it consults this only for the rest."""
    if exit_code == 0:
        return None
    if gate_kill_line(log_text, exit_code) is not None:
        return "gate infrastructure"
    if exit_code == 137:
        return "gate infrastructure"
    return None


def landed_short(log_text: str) -> str | None:
    """The sha on the line after ship's ``shipped — landed on main:`` (or
    ``… on release/r<N>:``, :data:`_LANDED_RE`)."""
    lines = _lines(log_text)
    for i in range(len(lines) - 1, -1, -1):
        if _LANDED_RE.search(lines[i]) and i + 1 < len(lines):
            token = (lines[i + 1].split() or [""])[0]
            if re.fullmatch(r"[0-9a-f]{7,40}", token):
                return token
    return None


def _landed_sha(log_text: str, tree: Path, onto: str | None = None) -> str | None:
    """The full landed sha: ship's report line resolved in ``tree``, else
    the land target's tip (``origin/main``, or ``origin/<onto>``)."""
    short = landed_short(log_text)
    tip = f"origin/{onto or 'main'}"
    for rev in ([f"{short}^{{commit}}"] if short else []) + [tip]:
        proc = _git(["rev-parse", "--verify", "--quiet", rev], tree)
        sha = proc.stdout.strip()
        if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    return short


# ── Store writes ───────────────────────────────────────────────────


def _newest_comment(store: Any, todo_id: int) -> str | None:
    comments = [
        c for c in _rs._todo_chunks(store, todo_id) if c.chunk_kind == _rs._COMMENT_KIND
    ]
    if not comments:
        return None
    return str(max(comments, key=lambda c: int(c.ord)).text or "")


def _close_todo(store: Any, todo_id: int) -> None:
    """``STATUS:done`` and every ``claimed-by:`` lease dropped (what the todo
    handler does on a terminal STATUS)."""
    for value in _rs._open_tag_values(store, todo_id):
        if value.startswith("claimed-by:"):
            store.remove_tag(todo_id, Tag.open(value))
    store.add_tag(
        todo_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )


def _record_land(store: Any, todo_id: int, sha: str | None, subject: str) -> list[str]:
    """The ``landed:`` comment + ``STATUS:done``, each only when land_notify
    did not write it. ``sha=None`` is the nothing-new land: the comment is
    :data:`NO_DIFF_NOTE`, so no reader of ``landed: <sha>`` finds a sha
    (gr561142). Returns what this wrote.

    A pin-bump node (``meta.pin_bump``, td659522 F7) gets the ``landed:``
    comment but stays open until its record says the gate image carries
    the new engine (``image.ready``): glue blocked by the node must not
    unblock on the land alone, and ``pin_bump``'s image wait closes it.

    The dedupe is keyed on the sha, not the newest comment: the
    skill-edit mint appends its own note after this record on some
    finishes, so a retried land tail would read a non-``landed:`` newest
    and stamp a duplicate. A sha-keyed check stays idempotent no matter
    what notes landed in between (and still writes a real land after a
    ``nothing new`` note, whose ``landed:`` line names no sha)."""
    wrote: list[str] = []
    if sha is not None:
        already = _landed_comment_sha(store, todo_id) == sha[:9].lower()
    else:
        already = (
            (_newest_comment(store, todo_id) or "")
            .lstrip()
            .lower()
            .startswith("landed:")
        )
    if not already:
        note = NO_DIFF_NOTE if sha is None else f"landed: {sha[:9]} — {subject}"
        _rs._append_comment(store, todo_id, note)
        wrote.append("landed comment")
    ref = store.get_ref(kind="todo", id=todo_id)
    if ref is not None and (
        (ref.meta or {}).get(PENDING_LAND_KEY)
        or (ref.meta or {}).get(PRELAND_DEFER_FAILED_KEY)
        or (ref.meta or {}).get(PRELAND_HELD_KEY)
    ):
        # The pre-land hold (gr570506) is discharged: the landed: comment
        # is the record now, and the stamp must not read a post-land
        # review as pre-land. A stale preland_defer_failed (gr663738),
        # the FIX-round count (gr684815) and the preland_held stash
        # (gr681208) discharge with it.
        store.stamp_ref_meta(
            todo_id,
            {
                PENDING_LAND_KEY: None,
                PRELAND_DEFER_FAILED_KEY: None,
                PRELAND_FIX_ROUNDS_KEY: None,
                PRELAND_HELD_KEY: None,
            },
        )
        wrote.append("pending_land cleared")
    if _pin_image_wait(ref):
        wrote.append("STATUS held: gate image wait (F7)")
    elif _rs._todo_status(store, todo_id) != "done":
        _close_todo(store, todo_id)
        wrote.append("STATUS:done")
    return wrote


def _pin_record(ref: Any) -> dict[str, Any] | None:
    """The ``meta.pin_bump`` record of a todo ref, or ``None``."""
    record = (getattr(ref, "meta", None) or {}).get("pin_bump") if ref else None
    return record if isinstance(record, dict) else None


def _pin_image_wait(ref: Any) -> bool:
    """A pin-bump node whose gate image does not carry the pin yet (its
    record's ``image.ready`` is not true): the land leaves it open."""
    record = _pin_record(ref)
    if record is None:
        return False
    image = record.get("image")
    return not (isinstance(image, dict) and image.get("ready") is True)


def _plan_tag(store: Any, todo_id: int) -> str | None:
    for value in _rs._open_tag_values(store, todo_id):
        if value.startswith("plan:") and value[5:].isdigit():
            return value
    return None


def pending_gate(store: Any, todo_id: int) -> int | None:
    """The plan's ``sha-in-prod`` gate this land fills: among the todo's
    plan's gates still at :data:`PENDING_SHA`, the one ``blocked-by`` the
    todo. ``None`` when the todo is in no plan or no pending gate is
    ``blocked-by`` it (so a revert task, which no gate waits on, never
    fills a gate that was reset after a REVERT verdict)."""
    plan = _plan_tag(store, todo_id)
    if plan is None:
        return None
    gates = _pending_gates(store, plan, todo_id)
    mine = [g for g, blocked_by_me in gates if blocked_by_me]
    return mine[0] if mine else None


def _write_gate(store: Any, gate_id: int, sha: str, todo_id: int) -> None:
    ref = store.get_ref(kind="todo", id=gate_id)
    gate = dict((ref.meta or {}).get("gate") or {}) if ref is not None else {}
    args = dict(gate.get("args") or {})
    args["sha"] = sha
    store.stamp_ref_meta(gate_id, {"gate": {**gate, "args": args}})
    _rs._append_comment(store, gate_id, f"sha: {sha} — landed by td{todo_id}")


#: ``landed: <sha>`` — the comment line whose sha a no-diff reland still
#: owes the pending gate (gr576154). ``landed: nothing``
#: (:data:`NO_DIFF_NOTE`) parses to no sha.
_LANDED_COMMENT_RE = re.compile(r"^landed:\s*([0-9a-f]{7,40})\b", re.IGNORECASE)

#: The reset/rearm boundary a review REVERT leaves on the requeued todo
#: (``plan_dispatch._review_revert``): its ``handoff: review: REVERT — …``
#: comment. A ``landed:`` comment OLDER than it names a sha the revert
#: took off main — recovering it would refill the gate with the reverted
#: commit (td626034). The ``(review td…; reverted by td…`` trailer is the
#: machine-written half, so a builder's own handoff quoting the verdict
#: never reads as the boundary.
_REVERT_HANDOFF_RE = re.compile(
    r"^handoff: review: REVERT\b[\s\S]*\(review td\d+; reverted by td\d+"
)


def _landed_comment_sha(store: Any, todo_id: int) -> str | None:
    """The sha the todo's newest ``landed: <sha>`` comment names. A reland
    after a land that pushed but died before its gate write finds no diff
    and no new sha — the earlier land's comment is the sha the plan's
    gate waits on (gr576154). ``None`` when no landed comment parses (a
    ``landed: nothing`` no-diff note, the todo never landed, or the only
    ``landed:`` comments predate the REVERT handoff — those name the
    reverted commit, td626034)."""
    comments = [
        c for c in _rs._todo_chunks(store, todo_id) if c.chunk_kind == _rs._COMMENT_KIND
    ]
    for c in sorted(comments, key=lambda c: int(c.ord), reverse=True):
        text = str(c.text).strip()
        if _REVERT_HANDOFF_RE.match(text):
            break
        for line in text.splitlines():
            m = _LANDED_COMMENT_RE.match(line.strip())
            if m is not None and m.group(1).lower() != PENDING_SHA:
                return m.group(1).lower()
    return None


# ── The gripe behind the plan ──────────────────────────────────────

#: The gripe comment chunk kind (``handlers.gripe._COMMENT_KIND``; restated:
#: workers never import handlers).
_GRIPE_COMMENT_KIND = "gripe_comment"

#: Gripe STATUS values a land leaves alone: terminal, or already on the
#: verify rung (``handlers.gripe.AUTO_VERIFY_STATUSES`` + the user rung).
_GRIPE_SETTLED = frozenset({"done", "wontfix", "verify", "auto_verify", "user_verify"})


def _plan_gripe(store: Any, plan_tag: str) -> int | None:
    """The gripe the plan root is ``related-to`` (a ``gripe-fix`` plan), or
    ``None`` for any other plan."""
    root_id = int(plan_tag[5:])
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT l.dst_ref_id
              FROM links l JOIN refs g ON g.ref_id = l.dst_ref_id
             WHERE l.src_ref_id = %s AND l.relation = 'related-to'
               AND g.kind = 'gripe' AND g.retired_at IS NULL
             ORDER BY l.dst_ref_id
             LIMIT 1
            """,
            (root_id,),
        ).fetchone()
    return int(row[0]) if row else None


def _resolve_gripe(
    store: Any,
    gripe_id: int,
    sha: str,
    detail: str,
    *,
    status: str,
    settled_comment: bool = False,
) -> list[str]:
    """The one ``resolved:`` writer (td659522 step 4): ``resolved: <sha9>
    (main, release pending) — <detail>`` as the gripe's newest comment,
    unless the newest already is one naming the sha, then ``STATUS:<status>``.
    A settled gripe (:data:`_GRIPE_SETTLED`) keeps its status and, unless
    ``settled_comment`` (land_notify's shape), gets no comment either.
    Returns what it did: ``resolved comment``, ``STATUS:<status>`` or
    ``left STATUS:<current>``."""
    current = _rs._todo_status(store, gripe_id)
    settled = current in _GRIPE_SETTLED
    did: list[str] = []
    if settled_comment or not settled:
        chunks = store.chunks.list_chunks_for_ref(gripe_id)
        comments = [c for c in chunks if c.chunk_kind == _GRIPE_COMMENT_KIND]
        newest = (
            str(max(comments, key=lambda c: int(c.ord)).text or "") if comments else ""
        )
        if not (newest.lstrip().lower().startswith("resolved:") and sha[:9] in newest):
            store.chunks.insert_chunks(
                gripe_id,
                [
                    ChunkInsert(
                        ord=max((int(c.ord) for c in chunks), default=0) + 1,
                        text=f"resolved: {sha[:9]} (main, release pending) — {detail}",
                        meta={"chunk_kind": _GRIPE_COMMENT_KIND},
                    )
                ],
            )
            did.append("resolved comment")
    if settled:
        did.append(f"left STATUS:{current}")
    else:
        store.add_tag(
            gripe_id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
        )
        did.append(f"STATUS:{status}")
    return did


def _notify_gripe(
    store: Any, gripe_id: int, sha: str, subject: str, todo_id: int
) -> str:
    """What a session does after its own land (gr561932): ``resolved: <sha>``
    as the gripe's newest comment and ``STATUS:verify``; ``gripe_validate``
    closes it once the sha is on prod. Idempotent: a settled gripe is left
    alone, a comment already naming the sha is not repeated. Returns the
    summary line."""
    did = _resolve_gripe(
        store,
        gripe_id,
        sha,
        f"{subject} (landed by the factory, td{todo_id})",
        status="verify",
    )
    if "STATUS:verify" not in did:
        return f"gripe gr{gripe_id}: {did[-1]}"
    return f"gripe gr{gripe_id}: {', '.join(did)} written"


#: land_notify's ``_ITEM_RE`` — one ``(refs )?(gr|td)N`` item; group 1
#: marks the ``refs grN`` partial form.
_ITEM_PARSE_RE = re.compile(r"(refs\s+)?(gr|td)(\d+)")
_MAX_HANDLES = 8

#: scripts/gripe-note's ``fixed_gripe_ids`` source: the subject's
#: trailing gripe-only ``(grN[, grM])`` — a mixed ``(gr1, td2)`` tail is
#: land_notify's, not gripe-note's — and the body's ``Fixes: grN[, grM]``
#: trailer lines.
_GRIPE_NOTE_SUBJECT_RE = re.compile(r"\((gr\d+(?:[,\s]+gr\d+)*)\)\s*$")
_GRIPE_NOTE_TRAILER_RE = re.compile(
    r"^\s*fixes:\s*(gr\d+(?:[,\s]+gr\d+)*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_GRIPE_NOTE_ID_RE = re.compile(r"gr(\d+)")


def _gripe_note_ids(message: str) -> list[int]:
    """``scripts/gripe-note``'s ``fixed_gripe_ids``: the subject line's
    trailing ``(grN…)`` plus every ``Fixes:`` trailer's ids, distinct, in
    order."""
    lines = message.splitlines()
    found: list[str] = []
    if lines and (m := _GRIPE_NOTE_SUBJECT_RE.search(lines[0])):
        found.append(m.group(1))
    found += [m.group(1) for m in _GRIPE_NOTE_TRAILER_RE.finditer("\n".join(lines[1:]))]
    seen: dict[int, None] = {}
    for group in found:
        for m in _GRIPE_NOTE_ID_RE.finditer(group):
            seen.setdefault(int(m.group(1)), None)
    return list(seen)


def _subject_handles(subject: str) -> list[tuple[str, int, bool]]:
    """The subject's trailing handles as ``(kind, id, partial)`` — what
    ship's land_notify would have walked had ``PRECIS_LAND_NOTIFY=0``
    not held it (review td660513 finding 4: the lander writes them only
    after the origin sync verified the sha)."""
    m = _HANDLES_RE.search(subject.strip())
    if m is None:
        return []
    out: list[tuple[str, int, bool]] = []
    seen: set[tuple[str, int]] = set()
    for item in _ITEM_PARSE_RE.finditer(m.group(1)):
        kind = "gripe" if item.group(2) == "gr" else "todo"
        key = (kind, int(item.group(3)))
        if key in seen:
            continue
        seen.add(key)
        out.append((kind, key[1], item.group(1) is not None))
        if len(out) >= _MAX_HANDLES:
            break
    return out


def _landed_elsewhere(store: Any, todo_id: int) -> str | None:
    """The second repo ``todo_id`` landed in (``meta.landed_repo``,
    stamped by ``repo_land``), or ``None`` for a precis todo."""
    ref = store.get_ref(kind="todo", id=todo_id)
    repo = (getattr(ref, "meta", None) or {}).get(_lanes.LANDED_REPO_KEY)
    if isinstance(repo, str) and repo and repo != _lanes.DEFAULT_REPO:
        return repo
    return None


def _deferred_notify(
    store: Any, subject: str, sha: str, todo_id: int, tree: Path
) -> list[str]:
    """What ship's post-land steps held back, written by the lander after
    the origin sync verified ``sha`` (review td660513 finding 4):

    * ``scripts/gripe-note`` first (``PRECIS_GRIPE_NOTE=0``): the
      ``landed <sha9> on main`` comment on every gripe the squash
      message names — subject-tail ``(grN…)`` and ``Fixes:`` trailers —
      skipped when the gripe already carries that comment;
    * then ``scripts/lib/land_notify.py`` (``PRECIS_LAND_NOTIFY=0``):
      other todos get the ``landed:`` comment + done (a todo that
      landed in a second repo is left alone, :func:`_landed_elsewhere`),
      a ``refs grN`` the partial comment alone, a plain gripe the
      resolved comment + ``STATUS:auto_verify``.

    gripe-note's comment lands before land_notify's so a ``resolved:``
    stays the newest — ship's own order. This land's own todo is written
    by :func:`_record_land` already."""
    notes: list[str] = []
    # The squash message ship committed: the subject plus any Fixes:
    # trailers it carried — read unauthenticated from the shared object
    # store, falling back to the subject alone.
    show = _git(["show", "-s", "--format=%B", sha], tree)
    message = (show.stdout or "") if show.returncode == 0 else ""
    text = f"landed {sha[:9]} on main"
    for gid in _gripe_note_ids(message or subject):
        comments = [
            c
            for c in store.chunks.list_chunks_for_ref(gid)
            if c.chunk_kind == _GRIPE_COMMENT_KIND
        ]
        if any(str(c.text or "").strip() == text for c in comments):
            notes.append(f"gripe gr{gid}: landed note already posted")
            continue
        ord_ = (
            max(
                (int(c.ord) for c in store.chunks.list_chunks_for_ref(gid)),
                default=0,
            )
            + 1
        )
        store.chunks.insert_chunks(
            gid,
            [
                ChunkInsert(
                    ord=ord_, text=text, meta={"chunk_kind": _GRIPE_COMMENT_KIND}
                )
            ],
        )
        notes.append(f"gripe gr{gid}: landed note")
    for kind, n, partial in _subject_handles(subject):
        if kind == "todo":
            if n == todo_id:
                continue
            elsewhere = _landed_elsewhere(store, n)
            if elsewhere is not None:
                # td659522: its ``landed:`` sha is that repo's (the
                # reviewer's target read); a precis sha there would lie.
                notes.append(f"todo td{n}: left alone (it landed in {elsewhere})")
                continue
            wrote = _record_land(store, n, sha, subject)
            notes.append(
                f"todo td{n}: "
                + (", ".join(wrote) + " written" if wrote else "already recorded")
            )
        elif partial:
            # ``landed (partial):`` — touches the gripe, no status.
            text = f"landed (partial): {sha[:9]} — {subject.strip()}"
            comments = [
                c
                for c in store.chunks.list_chunks_for_ref(n)
                if c.chunk_kind == _GRIPE_COMMENT_KIND
            ]
            newest = (
                str(max(comments, key=lambda c: int(c.ord)).text or "")
                if comments
                else ""
            )
            if newest.lstrip().startswith("landed (partial):") and sha[:9] in newest:
                notes.append(f"gripe gr{n}: already noted")
                continue
            ord_ = (
                max(
                    (int(c.ord) for c in store.chunks.list_chunks_for_ref(n)), default=0
                )
                + 1
            )
            store.chunks.insert_chunks(
                n,
                [
                    ChunkInsert(
                        ord=ord_, text=text, meta={"chunk_kind": _GRIPE_COMMENT_KIND}
                    )
                ],
            )
            notes.append(f"gripe gr{n}: partial landed comment")
        else:
            # land_notify's resolved path — ``resolved:`` comment +
            # ``STATUS:auto_verify`` (it auto-verifies; _notify_gripe's
            # ``STATUS:verify`` belongs to the plan's own gripe); it
            # comments on a settled gripe too.
            did = _resolve_gripe(
                store,
                n,
                sha,
                f"landed: {subject.strip()}",
                status="auto_verify",
                settled_comment=True,
            )
            notes.append(f"gripe gr{n}: {', '.join(did)}")
    return notes


# ── Cleanup ────────────────────────────────────────────────────────


def _land_clone(tree: Path) -> bool:
    """Whether ``tree`` is a clone this lander cut
    (:func:`_slice_ws.attempt_dir` with :data:`_slice_ws.ROLE_LAND`), as
    against a linked worktree of the host checkout (a PR land's, or a slice
    land minted before gr741695 and still running)."""
    parsed = _slice_ws.parse_attempt_dir(tree)
    return parsed is not None and parsed[2] == _slice_ws.ROLE_LAND


def _land_floor(tree: Path) -> int | None:
    """The land job's id, which names its clone: the task's
    ``meta.build_floor`` once this land has landed (an attempt ref of a job
    with a smaller id is never continued from). ``None`` for a folder that
    is not a land clone."""
    parsed = _slice_ws.parse_attempt_dir(tree)
    if parsed is None or parsed[2] != _slice_ws.ROLE_LAND:
        return None
    return parsed[1]


def _cleanup(tree: Path, branch: str, todo_id: int = 0, store: Any = None) -> list[str]:
    """After a land: remove the folder, the attempt refs and the origin
    branch (when pushed). One note per step; failures are reported, not
    raised.

    A land's own clone (:func:`_land_clone`) goes through
    :func:`_slice_ws.drop` (its Compose project, then the directory; the
    clone's branch goes with it), then :func:`_slice_ws.delete_builds`
    deletes the todo's attempt refs in the hub and the task's
    ``meta.build_ref`` is cleared: the work is on main now, and a ref left
    behind would start a later attempt of the same todo from commits that
    are already landed. ``meta.conflict`` (:data:`CONFLICT_KEY`) goes with
    it: the conflict an earlier land stamped is resolved. The same write
    stamps ``meta.build_floor`` with this land job's id, always — also
    stamped when the land record is written (:func:`_landed_finish`), so a
    crash between the record and this cleanup is covered. That write comes
    before the refs are deleted, and they are kept when it fails. A linked worktree
    (a PR land, a land minted before
    gr741695) is removed as before, with its stack and its branch. Either
    way a legacy ``refs/factory/slice/<id>`` / ``refs/heads/slice/<id>`` on
    GitHub is deleted as before."""
    notes: list[str] = []
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        return [f"cleanup skipped: {exc}"]

    if _land_clone(tree):
        drop_notes: list[str] = []
        try:
            gone = _slice_ws.drop(tree, notes=drop_notes)
        except Exception as exc:  # the land is real; a note, not a failure
            gone = False
            drop_notes.append(f"{type(exc).__name__}: {exc}")
        notes.append(
            "folder drop: ok"
            if gone
            else f"folder drop: FAILED ({'; '.join(drop_notes)[:200]})"
        )
        if todo_id > 0:
            # The stamp first, and the delete only when it took (repo_land's
            # rule): refs deleted under a ``build_ref`` that still names
            # them, with no floor in, refuse a reopened task at launch. With
            # the floor in, a ref left behind (a failed delete, a ref in
            # another host's hub) is never read.
            stamped = store is None
            if store is not None:
                stamps: dict[str, Any] = {BUILD_REF_KEY: None, CONFLICT_KEY: None}
                floor = _land_floor(tree)
                if floor is not None:
                    stamps[BUILD_FLOOR_KEY] = floor
                try:
                    store.stamp_ref_meta(todo_id, stamps)
                    stamped = True
                    notes.append(
                        f"meta.{BUILD_REF_KEY}: cleared"
                        + (f", {BUILD_FLOOR_KEY} {floor}" if floor is not None else "")
                    )
                except Exception as exc:
                    notes.append(f"meta.{BUILD_REF_KEY}: clear FAILED ({exc})")
            if stamped:
                try:
                    notes.append(
                        _slice_ws.delete_builds(_slice_ws.hub_path(root), todo_id)
                    )
                except Exception as exc:
                    notes.append(f"attempt refs: delete FAILED ({exc})")
            else:
                notes.append("attempt refs kept: the task's meta could not be stamped")
        _unlink_land_lock(tree, todo_id)
        notes.extend(_delete_remote_branch(root, branch))
        return notes

    def step(label: str, args: list[str]) -> bool:
        proc = _git(args, root)
        if proc.returncode == 0:
            notes.append(f"{label}: ok")
            return True
        notes.append(f"{label}: FAILED ({(proc.stderr or proc.stdout).strip()[:200]})")
        return False

    # The warm compose stack dies with the tree (gr570133): a
    # precis-test-slice-* project left up keeps a postgres and a sleep-
    # infinity gate on the 8 GiB docker VM, and the SessionStart reapers
    # never run on this host. -v: the tree is gone, nothing is kept. A
    # tree that names no slice project (a dep_check-pinned path) is a
    # designed skip — evict_project would refuse it anyway, and a FAILED
    # note misreads that as docker trouble.
    project = _slice_stacks.project_for_tree(tree)
    if not project.startswith(_slice_stacks.SLICE_PREFIX):
        notes.append(f"compose down: skipped (not a slice project: {project})")
    elif _evict_stack(project, volumes=True):
        notes.append("compose down: ok")
    else:
        notes.append(
            "compose down: FAILED "
            f"(docker compose -p {project} down --remove-orphans -v)"
        )
    step("worktree remove", ["worktree", "remove", str(tree)])
    step("branch -D", ["branch", "-D", branch])
    notes.extend(_delete_remote_branch(root, branch))
    return notes


def _delete_remote_branch(root: Path, branch: str) -> list[str]:
    """``git push --delete`` of the landed slice branch — an
    authenticated transport, so it never runs in ``root``'s own
    gitdir: the shared config and ``hooks/`` are branch-writable, and
    a ``push origin`` there would hand a planted ``pre-push`` hook or
    a ``url.insteadOf`` redirect the factory token (review td660513).
    The "is it on origin" check is the
    :func:`~precis.workers.factory_git_mirror.ls_remote` oracle — a
    non-repo cwd no on-disk config can redirect — and the delete
    pushes from a fresh ops gitdir under the credential wiring,
    exactly like :func:`_push_branch`. Both homes are checked and
    deleted in one push: the hidden ``refs/factory/<branch>`` the factory
    pushes now, and a ``refs/heads/<branch>`` an older build left as a
    visible branch. Notes; never raises."""
    try:
        url = _github_url(root)
        if url is None:
            return ["origin branch: skipped (no trusted origin url)"]
        env = _factory_git.git_auth_env(base=land_env())
        gone = [
            f"{ns}{branch}"
            for ns in (SLICE_REF_NS, "refs/heads/")
            if (
                _factory_git_mirror.ls_remote(url, (branch,), env=env, namespace=ns)
                or {}
            ).get(branch)
        ]
        if not gone:
            return ["origin branch: not pushed"]
        ops = _factory_git_mirror._ops_repo(
            _factory_git_mirror.mirror_root(root),
            env=_factory_git_mirror.local_env(env),
        )
        if ops is None:
            return ["push --delete: FAILED (no ops repo)"]
        try:
            proc = _factory_git_mirror._run(
                [
                    "git",
                    *_factory_git_mirror.TRUSTED_ARGS,
                    "-C",
                    str(ops),
                    "push",
                    url,
                    "--delete",
                    *gone,
                ],
                cwd=ops,
                env=_factory_git_mirror.trusted_env(env),
                timeout=60.0,
            )
        finally:
            _factory_git_mirror._drop_ops(ops)
    except Exception as exc:
        return [f"origin branch: FAILED ({type(exc).__name__}: {exc})"]
    if proc.returncode == 0:
        return ["push --delete: ok"]
    err = (proc.stderr or proc.stdout or "").strip()[:200]
    return [f"push --delete: FAILED ({err})"]


# ── Submit gate ────────────────────────────────────────────────────


def _todo_rejection(store: Any, params: dict[str, Any]) -> str | None:
    """``_rs._todo_rejection`` plus the pre-land exception (gr570506): a
    build closed ``done`` with ``meta.pending_land`` stamped is the
    machine's own hold — the tick minted this job on the reviewer's LAND
    verdict, so that ``done`` is what the land expects, not a refusal.
    The exception ends at that hold (gr663909): a ``won't-do`` or
    ``auto-timeout`` on the stamped build is a ruling made while the
    land queued, and the queued land refuses like any terminal
    todo."""
    reason = _rs._todo_rejection(store, params.get("todo_id"))
    if reason is None or "is closed" not in reason:
        return reason
    todo_id = params.get("todo_id")
    if (
        store is not None
        and isinstance(todo_id, int)
        and _rs._terminal_ruling(store, todo_id) is None
    ):
        return None
    return reason


def _repo_refusal(store: Any, params: dict[str, Any]) -> str | None:
    """Why this job may not land (td659522), or ``None``. The default repo
    lands here through the tree's ``scripts/ship``. Another repo is admitted
    (and ``submit``/``poll``/``kill`` hand it to ``repo_land``) only when
    the job's own ``params.repo`` names it, its profile's ``lander`` is
    ``land_slice``, ``factory.open_repos`` lists it, and the todo's
    ``repo:`` tag agrees with the param. A job with no ``params.repo`` (a
    hand mint, a pre-profile job) whose todo is tagged for another repo
    stays refused: it must never run ``scripts/ship``."""
    todo_id = params.get("todo_id")
    names = [params["repo"]] if params.get("repo") is not None else []
    if store is not None and isinstance(todo_id, int):
        names += _rs.slice_repos(store, todo_id)
    other = [str(n) for n in names if n != _lanes.DEFAULT_REPO]
    if not other:
        return None
    lead = f"land_slice: repo {other[0]!r} cannot be landed here: "
    repo = params.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return lead + "the job has no params.repo; next: land the slice by hand"
    repo = repo.strip()
    profile = _lanes.repo_profile(repo)
    if profile is None or profile.lander != "land_slice":
        return lead + "its profile has no lander yet; next: land the slice by hand"
    if repo not in _lanes.open_repos(store):
        return lead + "factory.open_repos does not list it; next: land it by hand"
    mismatch = _rs._repo_param_rejection(store, todo_id, repo)
    return mismatch.replace("run_slice:", "land_slice:", 1) if mismatch else None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Refuses while ``PRECIS_FACTORY=off``, for any repo but the default,
    and on a missing/closed todo."""
    del gripe_id
    if _rs.factory_off():
        return (
            "land_slice: the factory is off (PRECIS_FACTORY=off); next: unset it "
            "on the worker host to land slices"
        )
    wrong_repo = _repo_refusal(store, params)
    if wrong_repo is not None:
        return wrong_repo
    reason = _todo_rejection(store, params)
    return reason.replace("run_slice:", "land_slice:", 1) if reason else None


# ── Pull requests ──────────────────────────────────────────────────


def _pr_param(params: dict[str, Any]) -> int | None:
    pr = params.get("pr")
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        return None
    return pr


def _plan_pr(store: Any, todo_id: int) -> dict[str, Any] | None:
    """The plan root's ``meta.pr`` (``None`` outside a pr-intake plan)."""
    from precis.plans import pr_class

    try:
        return pr_class.pr_of(store, todo_id)
    except Exception:  # pragma: no cover — a land never fails on this read
        log.warning("land_slice: meta.pr read for td%d failed", todo_id, exc_info=True)
        return None


def _pr_worktree(
    params: dict[str, Any], number: int, head: str
) -> tuple[Path | None, str]:
    """``(worktree, '')`` for a PR land, or ``(None, why)``: the pinned
    ``params.worktree`` (must exist), else ``slice-pr-<n>`` cut here from
    the PR head, which must be ``head`` (the classified sha) when known."""
    from precis.workers.job_types import dep_check as _dc

    given = str(params.get("worktree") or "")
    if given:
        tree = Path(given)
        if tree.is_dir():
            return tree, ""
        return None, (
            f"pinned worktree {tree} is missing on this host (it holds "
            "dep_check's relock); reopen the plan's check node to re-run dep_check"
        )
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        return None, str(exc)
    tree, branch = _dc.pr_tree(root, number), _dc.pr_branch(number)
    if not tree.is_dir():
        fetch = _dc.fetch_pr_head(lambda args: _git(args, root), number, head, branch)
        if fetch.returncode != 0:
            return None, (
                f"git fetch of PR #{number}'s head into {branch} failed: "
                f"{fetch.stderr.strip()[:200]}"
            )
        add = _git(["worktree", "add", str(tree), branch], root)
        if add.returncode != 0:
            return None, f"git worktree add {tree} failed: {add.stderr.strip()[:200]}"
    if head:
        rev = _git(["rev-parse", "HEAD"], tree).stdout.strip().lower()
        if not rev or not rev.startswith(head.strip().lower()[:40]):
            return None, (
                f"PR #{number}'s head is {rev[:9] or '?'}, not {head[:9]} (the sha "
                "the intake classified and the decision saw); a moved head needs "
                "a new intake"
            )
    return tree, ""


def land_target(store: Any, params: dict[str, Any], todo_id: int) -> str | None:
    """The release branch this land targets (release-train T1), else
    ``None`` for main: the slice todo's ``meta.land_target`` or a ``land
    target: release/r<N>`` line in its brief
    (:func:`precis.plans.release.land_target_of`). Read from the todo,
    not the job params, so every land mint path — run_slice's, the
    reviewer's LAND verdict, the re-land tick — lands the same place. A
    pull-request land is never a release fix."""
    if store is None or _pr_param(params) is not None:
        return None
    ref = store.get_ref(kind="todo", id=todo_id)
    if ref is None:
        return None
    body, latest, _latest_ord = _rs._brief_parts(store, todo_id)
    return _release.land_target_of(ref.meta, body, latest)


def _release_closed(store: Any, todo_id: int, onto: str) -> str | None:
    """Why ``onto`` takes no more fixes, else ``None`` (release-train T1
    review): the plan's release has started or pinned its deploy
    (:func:`precis.plans.release.deploy_started` — the deploy owner's own
    records, read through the plan, since ship's round lock is this
    host's checkout), or the plan's release is not ``onto``. A todo in no
    release plan is not checked; an unreadable record fails closed."""
    try:
        rel = _release.release_of(store, todo_id)
        if rel is None:
            return None
        want = _release.release_branch(str(rel["rN"]))
        if want != onto:
            return f"the plan's release is {want}, not {onto}"
        return _release.deploy_started(store, todo_id)
    except Exception as exc:
        log.warning("land_slice: release state for td%d unreadable", todo_id)
        return f"the release's deploy state is unreadable ({type(exc).__name__})"


def _fetch_onto(tree: Path, onto: str) -> bool:
    """Refresh ``origin/<onto>`` in ``tree`` (best-effort: the subject and
    already-landed reads fall back to their failure paths on a miss);
    whether the fetch succeeded, for the release sync's skip."""
    return (
        _git(
            [
                "fetch",
                "--quiet",
                "origin",
                f"+refs/heads/{onto}:refs/remotes/origin/{onto}",
            ],
            tree,
        ).returncode
        == 0
    )


def _pushed_tip_ahead(
    root: Path | None, tree: Path, branch: str, base_ref: str
) -> tuple[bool, str | None]:
    """Whether the pushed ``<branch>`` still carries work ``base_ref``
    lacks — what a ``no_land`` record would abandon while ``_cleanup``'s
    ``push --delete`` removes the branch holding it (review td682306 on
    gr681208: a held land naming a recut or reset worktree reads empty
    while the reviewed commits sit on the pushed branch — recording
    done there deletes them unlanded). ``(True, tip)`` — origin's tip
    is ahead; ``(True, None)`` — verified: nothing is pushed, or the
    tip is not ahead; ``(False, None)`` — the pushed state could not be
    verified.

    The tip read is the ``ls_remote`` oracle — what GitHub actually
    serves, the same read ``_delete_remote_branch`` trusts — never the
    ``refs/remotes/origin/*`` ref a builder session can rewrite in the
    shared common dir. The fetch afterwards only materializes the tip's
    objects for the ancestry read, and its answer is trusted only when
    the tracking ref lands exactly on the oracle's sha — a config
    redirect cannot pass that check."""
    if root is None:
        return False, None
    url = _github_url(root)
    if url is None:
        return False, None
    env = _factory_git.git_auth_env(base=land_env())
    tip: str | None = None
    src: str | None = None
    for ns in (SLICE_REF_NS, "refs/heads/"):
        remote = _factory_git_mirror.ls_remote(url, (branch,), env=env, namespace=ns)
        if remote is None:
            return False, None
        tip = remote.get(branch)
        if tip is not None:
            src = f"{ns}{branch}"
            break
    if tip is None or src is None:
        return True, None
    remote_ref = f"refs/remotes/origin/{branch}"
    try:
        fetch = _git(
            ["fetch", "-q", "origin", f"+{src}:{remote_ref}"],
            tree,
        )
        got = (_git(["rev-parse", remote_ref], tree).stdout or "").strip()
        if fetch.returncode != 0 or got != tip:
            return False, None
        anc = _git(["merge-base", "--is-ancestor", tip, base_ref], tree).returncode
        if anc == 0:
            return True, None
        if anc != 1:
            # git could not run the ancestry read — not a verified
            # "nothing pushed ahead" answer.
            return False, None
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    return True, tip


def _pr_to_close(store: Any, params: dict[str, Any], todo_id: int) -> int | None:
    """The PR a green land closes: ``params.pr``, else the todo's
    ``meta.pr_close`` (the 'land after fixes' slice)."""
    number = _pr_param(params)
    if number is not None:
        return number
    ref = store.get_ref(kind="todo", id=todo_id)
    raw = (ref.meta or {}).get("pr_close") if ref is not None else None
    return (
        raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else None
    )


def _close_pr(store: Any, number: int, todo_id: int, sha: str) -> str:
    """Close PR ``number`` with a comment naming the squash sha; one note."""
    from precis.plans import pr_class

    pr = _plan_pr(store, todo_id) or {}
    repo = pr_class.resolve_repo(store, pr.get("repo"))
    if not repo:
        return f"PR #{number}: not closed (no owner/repo; set release.repo)"
    text = (
        f"Landed on main as {sha[:12]} (squash, td{todo_id}) by the precis "
        "lander. Closing: the change is on main."
    )
    proc = _gh(["pr", "close", str(number), "--repo", repo, "--comment", text])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:200]
        return f"PR #{number}: close FAILED ({err})"
    return f"PR #{number}: closed with the landed sha"


# ── Detached protocol ──────────────────────────────────────────────


class _MirrorRefreshError(RuntimeError):
    """The factory git mirror could not be refreshed from origin — infra,
    so the land ends attempt-free (gr730461)."""


#: The task meta key a land red on a conflict leaves (gr741695):
#: ``{head, ref, base_tip, files, at, job}`` — the commit that was to land and
#: the hub ref that holds it, the base's tip it conflicted with, the
#: conflicted paths as git reports them, when, and the land job's id
#: (``job``, an int: the dispatcher compares job ids, not this host's clock
#: in ``at`` against the database's, which a few seconds of skew misread).
#: The dispatcher reads it
#: to mint the conflict-only attempt; a land clears it (:func:`_cleanup`).
CONFLICT_KEY = "conflict"

#: Job meta ``land_start`` (:func:`_cut_land_folder`): ``{head, ref}`` the cut
#: resolved — the commit the clone stands on and the hub ref that holds it.
LAND_START_KEY = "land_start"

#: Ship's two ``die`` lines for a branch that does not integrate with the
#: base (``scripts/ship``: ``_squash_and_rebase`` and the in-lock forward
#: merge), the ✖ line the poll reads.
CONFLICT_MARKS: tuple[str, ...] = (
    "rebase conflict onto origin/main",
    "merge conflict integrating origin/main",
)


#: :func:`_drop_land_folder` modes: look for a ship behind the folder first
#: (the default: a failure after a ship ran), the caller knows none is
#: (a failure before any launch, which holds the land lock itself and would
#: read as one), or the caller knows one is (it is left for the host sweep).
FOLDER_PROBE = "probe"
FOLDER_DROP = "drop"
FOLDER_KEEP = "keep"


def is_conflict_refusal(cross: str | None) -> bool:
    """True when ``cross`` (a :func:`cross_line` result) is ship's rebase or
    merge conflict against the base (:data:`CONFLICT_MARKS`)."""
    return bool(cross) and any(mark in str(cross) for mark in CONFLICT_MARKS)


def _stamp_conflict(
    ctx: Any, todo_id: int, tree: Path, params: dict[str, Any], *, onto: str | None
) -> None:
    """Stamp ``meta.conflict`` (:data:`CONFLICT_KEY`) on the task from the
    clone ship left mid-rebase or mid-merge: the conflicted paths
    (``git diff --diff-filter=U``, as git prints them), the base tip ship
    integrated (``origin/<onto or main>`` in the clone) and the commit that
    was to land and the hub ref that holds it: what the cut resolved and
    recorded on the job (:data:`LAND_START_KEY` — a legacy land has no
    ``params.ref``, and ship's squash moves the clone's branch off
    ``head``), else ``params.head``, the task's pending land, the clone's
    branch. Best-effort and before the folder is dropped; a read
    that fails stamps what is known, and nothing here fails the land."""
    try:
        names = _git(["diff", "--name-only", "--diff-filter=U"], tree)
        files = (
            [p for p in (names.stdout or "").splitlines() if p.strip()]
            if names.returncode == 0
            else []
        )
        base_tip = (
            _git(
                [
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/origin/{onto or 'main'}",
                ],
                tree,
            ).stdout
            or ""
        ).strip()
        cut_at = (getattr(ctx, "meta", None) or {}).get(LAND_START_KEY)
        cut_at = cut_at if isinstance(cut_at, dict) else {}
        head = (
            str(cut_at.get("head") or "").strip()
            or str(params.get("head") or "").strip()
            or _pending_land_head(ctx.store, todo_id)
        )
        if not head:
            branch = str(params.get("branch") or _rs.slice_branch(todo_id))
            head = (
                _git(
                    ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], tree
                ).stdout
                or ""
            ).strip()
        stamp: dict[str, Any] = {
            "head": head,
            "ref": str(cut_at.get("ref") or params.get("ref") or ""),
            "base_tip": base_tip,
            "files": files,
            "at": _now().strftime(_TS_FMT),
        }
        # No id, no ``job``: the dispatcher then compares clocks, as it does
        # for a stamp from before the field.
        job = int(getattr(ctx, "ref_id", 0) or 0)
        if job > 0:
            stamp["job"] = job
        ctx.store.stamp_ref_meta(todo_id, {CONFLICT_KEY: stamp})
        ctx.append_chunk(
            "job_event",
            f"land_slice: conflict stamped on td{todo_id}: "
            f"{len(files)} conflicted path(s) against {base_tip[:12] or 'the base'}",
        )
    except Exception:
        log.warning("land_slice: conflict stamp on td%d failed", todo_id, exc_info=True)


def _fail(
    ctx: Any,
    todo_id: int,
    lane: Any,
    reason: str,
    *,
    handoff: str,
    attempt_free: str | None = None,
    verbatim: bool = False,
    folder: str = FOLDER_PROBE,
) -> None:
    """The red path: a ``handoff:`` for the next build session, the lease
    released (``attempt:<n>``), the job failed with ``reason``. The comment
    carries run_slice's ``_LANDER_HANDOFF_PREFIX`` marker so the retried
    builder's brief keeps it over an older ``brief:`` — it is the
    instruction the reopened todo exists to act on.

    ``attempt_free`` (gr570187) is the red's class when it was outside the
    diff (gate infrastructure killed the land): the job is marked
    ``meta.attempt_free = "infra-land"`` + ``meta.red_class`` so
    ``plan_dispatch`` re-mints a land — never a builder — and the lease
    goes back clean (``unclaim_todo``, no ``attempt:<n>``). A caller that
    also stamped ``meta.land_again_cap`` (a capped exit-137) parks the run
    at the ask instead: the tick does not re-land it on its own.
    ``verbatim=True`` (gr684320: a plain index.lock wait) stamps
    ``meta.attempt_free`` with the given reason itself — the dispatcher
    keeps a plain lock wait outside the re-land cap — while the lease
    still goes back clean.

    The job's own clone is dropped last (:func:`_drop_land_folder`; the
    ``folder`` modes are its) unless a ship is still behind it; nothing is
    saved first, because the lander adds no work of its own and the hub
    holds the commit that was to land (gr741695: the old park-save pushed
    the branch tip to GitHub here)."""
    attempt = None
    try:
        _rs._append_comment(
            ctx.store, todo_id, f"{_rs._LANDER_HANDOFF_PREFIX} {handoff}"
        )
    except Exception:  # pragma: no cover — the job outcome still lands
        log.warning("land_slice: handoff comment on td%d failed", todo_id)
    if attempt_free is not None:
        if verbatim:
            ctx.set_meta(**{_rs.ATTEMPT_FREE_KEY: attempt_free})
            suffix = f"; todo td{todo_id} back to open (no attempt: {attempt_free})"
        else:
            ctx.set_meta(**{_rs.ATTEMPT_FREE_KEY: "infra-land"}, red_class=attempt_free)
            suffix = f"; todo td{todo_id} back to open (no attempt: {attempt_free})"
        try:
            _rs.unclaim_todo(ctx.store, todo_id, str(lane) if lane else None)
        except Exception:  # pragma: no cover — defensive; the job still fails
            log.warning(
                "land_slice: lease unclaim on td%d failed", todo_id, exc_info=True
            )
        if (standing := _rs._terminal_status(ctx.store, todo_id)) is not None:
            suffix = f"; todo td{todo_id} stays STATUS:{standing} (result discarded)"
    else:
        try:
            attempt = _rs.release_todo(ctx.store, todo_id, str(lane) if lane else None)
        except Exception:  # pragma: no cover — defensive; the job still fails
            log.warning(
                "land_slice: lease release on td%d failed", todo_id, exc_info=True
            )
        if attempt is not None:
            suffix = f"; todo td{todo_id} back to open, attempt:{attempt}"
        elif (standing := _rs._terminal_status(ctx.store, todo_id)) is not None:
            suffix = f"; todo td{todo_id} stays STATUS:{standing} (result discarded)"
        else:
            suffix = ""
        try:
            # The rebuild must clear the pre-land review again (gr570506).
            _rs.rearm_preland_review(ctx.store, todo_id)
        except Exception:  # pragma: no cover — defensive; the job still fails
            log.warning("land_slice: pre-land re-arm on td%d failed", todo_id)
    # The lander adds no work of its own, so nothing is saved: the hub
    # holds the commit that was to land (gr741695).
    _drop_land_folder(ctx, todo_id, folder=folder)
    ctx.record_failure(f"land_slice: {reason}{suffix}")


def _drop_land_folder(ctx: Any, todo_id: int, *, folder: str = FOLDER_PROBE) -> None:
    """Drop this land job's clone once its ship is gone — on every failed
    land, whatever the class. A folder with a ship still behind it (the
    land lock held, or ship's own worktree lock live: :func:`_ship_busy`)
    is left for the host sweep, which settles a terminal land's folder as
    holding nothing (``run_slice``'s ``settle``); ``folder`` says whether
    to look (:data:`FOLDER_PROBE`) or that the caller already knows
    (:data:`FOLDER_DROP`, :data:`FOLDER_KEEP`). No folder, or one that is
    not a land clone (a PR land's worktree, a land minted before
    gr741695), is not touched. Never raises: the land's verdict stands."""
    try:
        if folder == FOLDER_KEEP:
            return
        root = _rs.slice_root()
        job_id = int(getattr(ctx, "ref_id", 0) or 0)
        tree = _slice_ws.attempt_dir(root, todo_id, job_id, _slice_ws.ROLE_LAND)
        if job_id <= 0 or not tree.exists():
            return
        busy = (
            _ship_busy(tree, todo_id, _rs.slice_branch(todo_id))
            if folder == FOLDER_PROBE
            else None
        )
        if busy is not None:
            ctx.append_chunk(
                "job_event",
                f"land_slice: folder {tree.name} kept for the host sweep: {busy['why']}",
            )
            return
        notes: list[str] = []
        gone = _slice_ws.drop(tree, notes=notes)
        if gone:
            _unlink_land_lock(tree, todo_id)
        ctx.append_chunk(
            "job_event",
            f"land_slice: folder {tree.name} "
            + ("dropped" if gone else "NOT dropped (left for the host sweep)")
            + (f" ({'; '.join(notes)[:200]})" if notes else ""),
        )
    except Exception:
        log.warning(
            "land_slice: dropping td%d's land folder failed", todo_id, exc_info=True
        )


def _pin_exempt(
    ctx: Any,
    todo_id: int,
    tree: Path,
    base_ref: str,
    paths: list[str],
    denied: list[str],
) -> tuple[list[str], str | None, bool]:
    """td659522 step 4, the one named exception to the deny gate (Reto,
    2026-11-15): a pin-bump node's own commit may carry the single
    ``autocatpath_min_version`` line of ``deploy/group_vars/all.yml``.
    Returns ``(denied, why, unreadable)``: ``denied`` without that one
    path when :func:`pin_bump.land_refusal` proves the branch is exactly
    that bump (the job event names it), else ``denied`` unchanged with
    the refusal. A todo carrying no pin record is not asked (``why`` is
    ``None``), and every other denied path stays denied.

    ``unreadable`` marks a check that could not run
    (:class:`pin_bump.LandUnreadable`, or it raised) when that path is
    the only one denied: no verdict on the branch, so the caller fails
    the attempt without the ``human_only_hit`` cap and the bump is built
    and checked again (the unreadable-diff rule, td645833)."""
    from precis.workers.job_types import pin_bump

    if pin_bump.GROUP_VARS_PATH not in denied:
        return denied, None, False
    only = denied == [pin_bump.GROUP_VARS_PATH]
    commit = ""
    try:
        ref = ctx.store.get_ref(kind="todo", id=todo_id)
        record = (getattr(ref, "meta", None) or {}).get(pin_bump.RECORD_KEY)
        if not isinstance(record, dict):
            return denied, None, False
        commit = str(record.get("commit"))[:9]
        why = pin_bump.land_refusal(ctx.store, todo_id, tree, base_ref, paths)
    except pin_bump.LandUnreadable as exc:
        return denied, str(exc), only
    except Exception:
        log.warning("land_slice: pin exemption for td%d failed", todo_id, exc_info=True)
        return denied, "the check itself failed", only
    if why is not None:
        return denied, why, False
    rest = [p for p in denied if p != pin_bump.GROUP_VARS_PATH]
    if not rest:
        ctx.set_meta(pin_exempt=pin_bump.GROUP_VARS_PATH)
        ctx.append_chunk(
            "job_event",
            f"land_slice: `{pin_bump.GROUP_VARS_PATH}` admitted past the deny "
            f"gate: the branch is td{todo_id}'s pin bump (record commit {commit}) "
            f"and that file changes only its {pin_bump.GROUP_VARS_KEY} line",
        )
    return rest, None, False


def _launch_on_record(ctx: Any, tree: Path) -> bool:
    """Whether a ship was ever launched for this job in ``tree``: the job's
    ``meta.compute_handle`` (a launch, a parked deferral), a proven launch
    of the current submit attempt (:func:`run_slice._recover_launch`), or
    the ship's own log or exit file in the folder (the paths a launch
    without an attempt directory uses). Doubt counts as a launch: the
    folder is then kept, as before."""
    try:
        if (getattr(ctx, "meta", None) or {}).get("compute_handle"):
            return True
        if _rs._recover_launch(ctx) is not None:
            return True
    except Exception:
        return True
    scratch = tree / "scratch"
    return (scratch / "land_slice.log").exists() or (
        scratch / "land_slice.exit"
    ).exists()


def _fail_workspace(ctx: Any, todo_id: int, lane: Any, why: str) -> None:
    """A land that could not get its folder (:class:`_slice_ws.WorkspaceError`,
    an unreadable checkout root) fails attempt-free under ``infra-land`` with
    the reason on the handoff: no attempt is burned and nothing was landed;
    the tick re-mints the land, or asks once its cap is spent."""
    _fail(
        ctx,
        todo_id,
        lane,
        why,
        handoff=(
            f"land DEFERRED (not red): the lander could not cut its folder: {why} "
            "Nothing was landed; the tick re-mints the land attempt-free."
        ),
        attempt_free="infra-land",
        folder=FOLDER_DROP,
    )


def _pending_land_head(store: Any, todo_id: int) -> str:
    """The task's ``meta.pending_land.head``, ``""`` when it holds none."""
    try:
        ref = store.get_ref(kind="todo", id=todo_id)
    except Exception:
        return ""
    pending = ((getattr(ref, "meta", None) or {}).get(PENDING_LAND_KEY)) or {}
    head = pending.get("head") if isinstance(pending, dict) else None
    return head.strip() if isinstance(head, str) else ""


def _ref_holds(hub: Path, tip: str, head: str) -> bool | None:
    """Whether the commit ``head`` is ``tip`` or an ancestor of it in the
    hub: ``True``/``False``, ``None`` when git cannot say."""
    if head == tip:
        return True
    anc = _git(["merge-base", "--is-ancestor", head, tip], hub).returncode
    return True if anc == 0 else (False if anc == 1 else None)


def _import_local_slice_branch(
    root: Path, hub: Path, todo_id: int, head: str
) -> str | None:
    """A land minted by the engine before gr741695 may still have its
    branch in the host checkout (the builder's worktree shared its git
    dir): when ``refs/heads/slice/<todo>`` exists there and holds ``head``
    (tip or ancestor; any tip when no head is known), bring it into the hub
    as ``refs/factory/build/<todo>/0`` with a fetch from the checkout's
    path — local, no hook, no credential — and return its tip. ``None``
    when there is no such branch, it does not hold ``head`` or the fetch
    failed."""
    branch = _rs.slice_branch(todo_id)
    shown = _git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], root
    )
    tip = (shown.stdout or "").strip()
    if shown.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", tip):
        return None
    if head:
        found = _git(["rev-parse", "--verify", "--quiet", f"{head}^{{commit}}"], root)
        full = (found.stdout or "").strip()
        if found.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", full):
            return None
        if full != tip and (
            _git(["merge-base", "--is-ancestor", full, tip], root).returncode != 0
        ):
            return None
    ref = _slice_ws.build_ref(todo_id, 0)
    got = _git(
        [
            "fetch",
            "--quiet",
            "--no-tags",
            "--",
            str(root),
            f"+refs/heads/{branch}:{ref}",
        ],
        hub,
    )
    if got.returncode != 0 or _slice_ws.hub_tip(hub, ref) != tip:
        return None
    return tip


def _land_start(
    ctx: Any, params: dict[str, Any], todo_id: int, hub: Path, url: str, root: Path
) -> tuple[_slice_ws.Start, str]:
    """``(start, head)``: where the cut starts (:class:`_slice_ws.Start`, the
    hub ref and the tip it holds) and the commit to land, both checked
    against the hub.

    ``params.ref`` names the attempt's ref. A land minted before gr741695
    has none, and its work is found in this order: the hub's own
    ``refs/factory/build/<todo>/0`` from an earlier import when it holds
    ``head`` (one that does not is deleted and the import runs again — it
    would fail every later land otherwise); the host checkout's local
    ``slice/<todo>`` (:func:`_import_local_slice_branch`); then what GitHub
    holds under ``refs/factory/slice/<todo>`` (:func:`_slice_ws.import_legacy`).
    The commit to land is ``params.head``, else the task's
    ``meta.pending_land.head``, else the ref's tip — unless that tip is a
    ``wip:`` snapshot, which is refused (a snapshot can carry stashes and
    other branches as extra parents). It must be the ref's
    tip or an ancestor of it: the ref may hold a ``wip:`` snapshot the
    worker made on top of the builder's last real commit, and the land
    takes the commit below it. The cut starts at the ref's tip, whatever
    ``head`` is (:func:`_slice_ws.cut` refuses a start commit its ref does
    not hold); the caller then puts the clone's branch on ``head``. A ref
    that no longer holds the commit, or a commit the hub does not have, is
    a :class:`_slice_ws.WorkspaceError` — nothing is cut and nothing
    lands."""
    ref = str(params.get("ref") or "").strip()
    head = str(params.get("head") or "").strip() or _pending_land_head(
        ctx.store, todo_id
    )
    if not ref:
        legacy = _slice_ws.build_ref(todo_id, 0)
        have = _slice_ws.hub_tip(hub, legacy)
        if have is not None and head:
            shown = _git(
                ["rev-parse", "--verify", "--quiet", f"{head}^{{commit}}"], hub
            )
            full = (shown.stdout or "").strip()
            if shown.returncode != 0 or _ref_holds(hub, have, full) is not True:
                # an import of an earlier, different state of the branch
                _git(["update-ref", "-d", legacy], hub)
                have = None
        if have is None:
            have = _import_local_slice_branch(root, hub, todo_id, head)
        if have is None:
            have = _slice_ws.import_legacy(hub, url, todo_id)
        if have is None:
            raise _slice_ws.WorkspaceError(
                f"the land of td{todo_id} names no hub ref, and neither the host "
                f"checkout's {_rs.slice_branch(todo_id)} (holding the commit to "
                f"land) nor GitHub's {_slice_ws.legacy_ref(todo_id)} has the work; "
                "next: rebuild the slice"
            )
        ref = legacy
    tip = _slice_ws.hub_tip(hub, ref)
    if tip is None:
        raise _slice_ws.WorkspaceError(
            f"this host's hub holds no {ref}; next: land on the host that built "
            f"td{todo_id}, or rebuild the slice"
        )
    start = _slice_ws.Start(sha=tip, ref=ref)
    if not head:
        # Nothing named the commit to land, so the ref's tip would be it —
        # but a ``wip:`` snapshot is never landed: it can carry stashes and
        # other branches as extra parents, content no reviewer saw.
        if _slice_ws._is_wip(hub, tip):
            raise _slice_ws.WorkspaceError(
                f"the newest work of td{todo_id} ({ref}) is an unfinished snapshot; "
                "next: let a build finish it"
            )
        return start, tip
    shown = _git(["rev-parse", "--verify", "--quiet", f"{head}^{{commit}}"], hub)
    sha = (shown.stdout or "").strip()
    if shown.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise _slice_ws.WorkspaceError(
            f"{ref} no longer holds {head[:12]} (the hub does not have it); "
            f"next: rebuild the slice"
        )
    held = _ref_holds(hub, tip, sha)
    if held is False:
        raise _slice_ws.WorkspaceError(
            f"{ref} no longer holds {head[:12]} (it is at {tip[:12]}); "
            "next: rebuild the slice"
        )
    if held is None:
        raise _slice_ws.WorkspaceError(
            f"git could not say whether {ref} holds {head[:12]}; next: retry the land"
        )
    return start, sha


def _cut_land_folder(
    ctx: Any,
    params: dict[str, Any],
    todo_id: int,
    tree: Path,
    onto: str | None,
    branch: str,
) -> None:
    """Cut ``tree``, this land job's own clone (gr741695): the hub synced
    to GitHub's tip of the land target, the commit to land
    (:func:`_land_start`) checked out on ``branch``, the ``.venv`` seeded
    from the host checkout. Then the clone's ``origin`` is made GitHub's
    url — the cut points it at the hub, but ``scripts/ship``, the mirror's
    url rewrite (:func:`~precis.workers.factory_git_mirror.mirror_ship_env`)
    and the lander's own fetches all expect the real origin there; the
    base's tracking ref (``origin/main``, or the release branch) the cut
    fetched stays, so every read the lander makes before ship's own fetch
    finds it. Raises :class:`_slice_ws.WorkspaceError`; the caller
    (:func:`_fail_workspace`) drops a folder a later step left behind."""
    root = _rs.slice_root()
    url = _github_url(root)
    if url is None:
        raise _slice_ws.WorkspaceError(
            "the factory checkout names no trusted origin url; next: set "
            "remote.origin.url of the host checkout to its github.com https url"
        )
    base = onto or "main"
    hub = _slice_ws.ensure_hub(root)
    _slice_ws.sync_hub(root, url=url, base=base)
    start, head = _land_start(ctx, params, todo_id, hub, url, root)
    notes: list[str] = []
    _slice_ws.cut(
        hub,
        tree,
        base=base,
        branch=branch,
        start=start,
        seed_from=root,
        notes=notes,
    )
    if _git(["rev-parse", "HEAD"], tree).stdout.strip() != head:
        # The ref holds a snapshot above the commit to land and the cut
        # checked the ref's tip out: the land takes the commit below it.
        back = _git(["checkout", "-q", "-B", branch, head], tree)
        if back.returncode != 0:
            raise _slice_ws.WorkspaceError(
                f"git could not check out {head[:12]} in {tree.name}: "
                f"{(back.stderr or back.stdout).strip()[:160]}; next: retry the land"
            )
    seturl = _git(["remote", "set-url", "origin", url], tree)
    if seturl.returncode != 0:
        raise _slice_ws.WorkspaceError(
            f"git could not set origin in {tree.name}: "
            f"{(seturl.stderr or seturl.stdout).strip()[:160]}; next: retry the land"
        )
    tracking = f"refs/remotes/origin/{base}"
    if _git(["rev-parse", "--verify", "--quiet", tracking], tree).returncode:
        raise _slice_ws.WorkspaceError(
            f"origin/{base} is missing in {tree.name} (the hub holds no {base}); "
            "next: retry the land once the hub has synced"
        )
    # What the cut resolved, for the conflict stamp: a legacy land has no
    # ``params.ref``, and ship's squash moves the clone's branch off ``head``.
    ctx.set_meta(**{LAND_START_KEY: {"head": head, "ref": start.ref}})
    ctx.append_chunk(
        "job_event",
        f"land_slice: cut {tree.name} at {head[:12]} from {start.ref}"
        + (f" ({'; '.join(notes)})" if notes else ""),
    )


def submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half: cut this land's own clone from the hub at the
    commit to land (:func:`_cut_land_folder`; a PR land keeps its worktree),
    then launch ship in it. Returns the handle, or ``None`` after driving
    the job terminal."""
    params = dict((ctx.meta or {}).get("params") or {})
    todo_id = params.get("todo_id")
    if _rs.factory_off():
        ctx.record_failure("land_slice: the factory is off (PRECIS_FACTORY=off)")
        return None
    wrong_repo = _repo_refusal(ctx.store, params)
    if wrong_repo is not None:
        ctx.record_failure(wrong_repo)
        return None
    if _rs.job_repo(params) != _lanes.DEFAULT_REPO:
        from precis.workers import repo_land

        return repo_land.submit(ctx, spec)
    del spec
    reason = _todo_rejection(ctx.store, params)
    if reason is not None:
        ctx.record_failure(reason.replace("run_slice:", "land_slice:", 1))
        return None
    assert isinstance(todo_id, int)
    lane = params.get("lane")
    number = _pr_param(params)
    pr = _plan_pr(ctx.store, todo_id) if number is not None else None
    branch_hint = str(
        params.get("branch")
        or (
            f"slice/pr-{number}"
            if number is not None
            else _rs.slice_branch(int(todo_id))
        )
    )
    onto = land_target(ctx.store, params, todo_id)
    if number is not None:
        found, why = _pr_worktree(params, number, str((pr or {}).get("head_sha") or ""))
        if found is None:
            _fail(ctx, todo_id, lane, why, handoff=f"land FAILED: {why}.")
            return None
        tree = found
    else:
        # gr741695: the land cuts its own clone — ``params.worktree`` (the
        # builder's folder, gone by now) is ignored. The name carries this
        # job's id, so a job resumed or re-submitted after an outage finds
        # its own folder again; a folder that is there is this job's.
        try:
            root = _rs.slice_root()
            job_id = int(getattr(ctx, "ref_id", 0) or 0)
            if job_id <= 0:
                raise _slice_ws.WorkspaceError(
                    "the land job has no id to name its folder; next: re-mint the land"
                )
            tree = _slice_ws.attempt_dir(root, todo_id, job_id, _slice_ws.ROLE_LAND)
        except (RuntimeError, _slice_ws.WorkspaceError) as exc:
            _fail_workspace(ctx, todo_id, lane, str(exc))
            return None
    # gr650484: a ship already running in this worktree owns the land —
    # resolve before the branch-subject/changed-path reads below, whose
    # git could bump into the running ship's index.lock. Ours (the land
    # lock's stamp names this slice/branch behind a live pid — and, for a
    # land's own clone, this job's folder) is adopted so its land record
    # still lands here; anything else — foreign stamp, orphaned flock,
    # hand-run ship — defers retryable. Before the cut: a land that must
    # defer cuts nothing. `_launch_ship` re-checks while actually acquiring
    # (the race window).
    busy = _ship_busy(tree, int(todo_id), branch_hint)
    if busy is not None:
        if busy["kind"] == "mine":
            return _adopt_ship(ctx, tree, int(todo_id), busy)
        _land_busy(ctx, int(todo_id), lane, busy["why"])
        return None
    if number is None and tree.exists() and not _launch_on_record(ctx, tree):
        # The folder is there but nothing was ever launched in it and no
        # ship is behind it (checked above): a worker that died between the
        # cut's rename and its last step (the branch on ``head``, ``origin``
        # set) left it. A land folder holds nothing of its own — cut again.
        dropped: list[str] = []
        if not _slice_ws.drop(tree, notes=dropped):
            _fail_workspace(
                ctx,
                todo_id,
                lane,
                f"the unfinished folder {tree.name} could not be removed "
                f"({'; '.join(dropped)[:160]}); next: retry the land",
            )
            return None
        ctx.append_chunk(
            "job_event",
            f"land_slice: {tree.name} was left unfinished by an earlier run "
            "(no launch on record) — dropped, cut again",
        )
    if number is None and not tree.exists():
        try:
            _cut_land_folder(ctx, params, todo_id, tree, onto, branch_hint)
        except _slice_ws.WorkspaceError as exc:
            _fail_workspace(ctx, todo_id, lane, str(exc))
            return None
    if onto is not None:
        _fetch_onto(tree, onto)
        ctx.set_meta(land_target=onto)
    subjects = _branch_subjects(tree, onto)
    subject = squash_subject(
        subjects or [], todo_id, node=_node_subject(ctx.store, todo_id)
    )
    if subject is not None and pr is not None and pr.get("title"):
        from precis.plans import pr_class

        subject = with_handle(
            pr_class.land_subject(str(pr["title"]), str(pr.get("class") or "")),
            todo_id,
        )
    # Stamp the engine-diff verdict on the job — always, so a land with
    # no stamp is a pre-E1 one the release gate reads as non-engine. A
    # diff git cannot read stamps ``<unreadable>`` (fail closed) and is
    # refused below, before the deny check it cannot feed.
    # A release fix's changes are read against its release branch: the
    # release's earlier fixes are not this slice's (T1 review). The base
    # is GitHub's verified tip when the oracle answers — the tracking
    # ref is branch-writable shared state (review td660513).
    base_ref = _land_base(tree, onto)
    paths = _rs.changed_paths(tree, base_ref)
    hits = ["<unreadable>"] if paths is None else _lanes.engine_hits(paths)
    ctx.set_meta(engine_paths=hits[:50])
    if paths is None:
        # td645833: a diff git cannot enumerate cannot be deny-checked —
        # refuse rather than let an unverified branch past the last gate
        # (run_slice's ``_deny_reason`` fails closed the same way at the
        # first gate). An ordinary failed attempt, no human_only_hit
        # cap: a fresh worktree may read.
        _fail(
            ctx,
            todo_id,
            lane,
            f"changed-path enumeration failed in {tree} (git failed); failing closed",
            handoff=(
                "land FAILED: git could not enumerate the branch's "
                f"changed paths in {tree}, so the deny gate cannot run "
                "— the land refuses to ship blind. Check the worktree's "
                "git state; a coordinator ruling lands the branch by hand."
            ),
        )
        return None
    # gr645524: the land is the last deny gate — run_slice's poll
    # check is the first (td574960: the brief's deny text, the
    # post-check and this land check name the same resolved set in
    # every mode). A denied diff never reaches ship. The
    # human_only_hit stamp caps the node at once — no retry and no
    # rescue can write a path no lane may — so the tick's ruling ask
    # opens on the frozen branch instead of burning the ladder on
    # rebuilds that can never land. The stamp's name undersells: at
    # the land every resolved-deny hit is a "no lane may write" hit.
    # gr657066: governs:factory skills join the set at the land base —
    # the fence is the base's declaration, so a branch cannot un-tag
    # it, and the scan failing closes like the diff read above.
    # td659522: one content-checked exception, a pin-bump node's own
    # commit (:func:`_pin_exempt`).
    governed = _governs.governed_skill_paths_at_ref(tree, base_ref, git=_git)
    if governed is None:
        _fail(
            ctx,
            todo_id,
            lane,
            f"governed-skill scan failed at {base_ref} "
            f"in {tree} (git failed); failing closed",
            handoff=(
                "land FAILED: git could not enumerate the governs:factory "
                "skills at the land base, so the deny gate cannot run "
                "— the land refuses to ship blind. Check the worktree's "
                "git state; a coordinator ruling lands the branch by hand."
            ),
        )
        return None
    denied = _rs.deny_violations(paths, _lanes.deny_paths_for(ctx.store) + governed)
    allow_only = _rs._skill_allow_only(ctx.store, todo_id)
    if allow_only is not None:
        outside = sorted(
            p
            for p in paths
            if not any(_rs.fnmatch.fnmatchcase(p, g) for g in allow_only)
        )
        if outside:
            _fail(
                ctx,
                todo_id,
                lane,
                "skill-editor scope violated",
                handoff="land FAILED: paths outside skill-editor scope: "
                + ", ".join(outside),
            )
            return None
    pin_why, pin_unreadable = None, False
    if denied:
        denied, pin_why, pin_unreadable = _pin_exempt(
            ctx, todo_id, tree, base_ref, paths, denied
        )
    if denied and pin_unreadable:
        _fail(
            ctx,
            todo_id,
            lane,
            f"the pin bump's `{denied[0]}` change could not be checked "
            f"({pin_why}); failing closed",
            handoff=(
                f"land FAILED: the lander could not check the pin bump's "
                f"`{denied[0]}` change ({pin_why}), so the deny gate held "
                "and nothing landed; the pin bump is built again."
            ),
        )
        return None
    if denied:
        ctx.set_meta(**{_rs.HUMAN_ONLY_KEY: denied[:50]})
        pin_note = f" (pin-bump exemption refused: {pin_why})" if pin_why else ""
        _fail(
            ctx,
            todo_id,
            lane,
            "branch touches deny path(s): " + ", ".join(denied[:50]) + pin_note,
            handoff=(
                "land FAILED: the branch touches deny path(s): "
                + ", ".join(f"`{p}`" for p in denied[:50])
                + pin_note
                + " — no lane may write them; a coordinator ruling "
                "lands the branch by hand or rewrites the work."
            ),
        )
        return None
    if subject is None:
        if subjects == [] and not paths:
            # review td682306 (gr681208): an empty checkout is the done
            # record below only when the PUSHED branch is empty too — a
            # held land can name a recut or reset worktree while
            # ``origin/<branch>`` still carries the commits review
            # judged; recording done here writes a false "landed:
            # nothing", releases the dependants, and lets ``_cleanup``'s
            # ``push --delete`` remove the branch holding the unlanded
            # work. Refuse like the missing-worktree arm: the reopened
            # build's restore puts the pushed tip back (or reports that
            # nothing was pushed).
            try:
                pushed_root: Path | None = _rs.slice_root()
            except Exception:
                pushed_root = None
            verified, pushed = _pushed_tip_ahead(
                pushed_root, tree, branch_hint, base_ref
            )
            if pushed is not None:
                _fail(
                    ctx,
                    todo_id,
                    lane,
                    f"{branch_hint} reads empty in {tree} but "
                    f"origin/{branch_hint} is at {pushed[:12]} ahead of "
                    f"{base_ref}",
                    handoff=(
                        f"land FAILED: the worktree's {branch_hint} has no "
                        f"commit ahead of {base_ref}, but "
                        f"origin/{branch_hint} is at {pushed[:12]} — the "
                        "pushed tip the review judged. Restore it into the "
                        "worktree (`git fetch` + `checkout -B`) and land "
                        "that, or rebuild the slice."
                    ),
                )
                return None
            if not verified:
                _fail(
                    ctx,
                    todo_id,
                    lane,
                    f"origin/{branch_hint}'s tip could not be verified "
                    f"(no trusted read) — refusing the no-land record",
                    handoff=(
                        "land FAILED: the worktree reads empty and "
                        f"origin/{branch_hint}'s state could not be "
                        "verified — the land refuses to record a no-land "
                        "that could abandon pushed work. Retry when "
                        "origin is readable; a coordinator ruling lands "
                        "or drops the branch by hand."
                    ),
                )
                return None
            # gr648125: the land was minted over a branch carrying
            # nothing — no commit ahead of origin/main and no net diff
            # (the fix is already on main by another route). Nothing to
            # land is done, not red: the poll's ``no_land`` path writes
            # the same record ship's nothing-new guard does. No ship
            # runs, so this land is unmirrored: clear a crashed
            # predecessor's stamp AND its launch files — the mirror and
            # the tips record beside it — or the poll's sync still reads
            # the stale record, judges a mirror in play, and the
            # nothing-new job fails or defers on a dead CAS basis
            # instead of completing (review td660513, third pass).
            try:
                ctx.store.stamp_ref_meta(todo_id, {FACTORY_MIRROR_KEY: None})
            except Exception:
                pass
            try:
                _factory_git_mirror.drop_launch(_rs.slice_root(), f"td{todo_id}")
            except Exception:
                pass
            return {"no_land": True, "worktree": str(tree)}
        what = "git log failed" if subjects is None else "no commit ahead"
        _fail(
            ctx,
            todo_id,
            lane,
            f"nothing to land on {params.get('branch')} ({what} of origin/main)",
            handoff=(
                f"land FAILED: {params.get('branch')} has no commit ahead of "
                "origin/main; commit the work on the branch."
            ),
        )
        return None
    # gr599427: --checked earns its gate only when the selector's answer
    # is a spot selection; the stamp rides the job so a flagless handle's
    # default (:func:`_planned_flag`) agrees. A release fix (``onto``)
    # lands lint-only: ship's release lane refuses --checked, and the
    # release push's own check.yml run is its gate.
    wants_spot = onto is None and (bool(hits) or params.get("checked") is True)
    spot = spot_selection(tree) if wants_spot else False
    ctx.set_meta(spot_selection=spot)
    flag = "--quick" if onto else ship_flag(params, engine=bool(hits), spot=spot)
    handle = _launch_ship(ctx, tree, flag, subject)
    if handle is None:
        return None
    # The ship may run from here on, so nothing below may lose its handle:
    # a raise would end the job while the ship goes on, and the sweep would
    # then stop that ship, possibly in the middle of its push.
    try:
        ctx.set_meta(land_subject=subject)
        # A handle parked before launch has no pid; its deferral event says why.
        if not handle.get("not_before"):
            target = f" --onto {onto}" if onto else ""
            ctx.append_chunk(
                "job_event",
                f"land_slice: scripts/ship {flag}{target} {subject!r} pid "
                f"{handle['pid']} in {tree} (gate_n={test_nproc()}, "
                f"slots={land_gate_slots()})",
            )
    except Exception:
        log.warning(
            "land_slice: bookkeeping after the launch of td%d failed",
            todo_id,
            exc_info=True,
        )
    return handle


def _worktree_gitdir(tree: Path) -> Path | None:
    """The folder's own git dir: a land's clone has ``<clone>/.git``, its
    own and shared with no other job (gr741695); a linked worktree (a PR
    land's) has ``<repo>/.git/worktrees/<name>``. ``None`` when git cannot
    answer — the locks below are skipped then and ship's own worktree lock
    is all that serialises."""
    proc = _git(["rev-parse", "--absolute-git-dir"], tree)
    raw = (proc.stdout or "").strip()
    return Path(raw) if proc.returncode == 0 and raw else None


def _git_common_dir(gitdir: Path, tree: Path) -> Path | None:
    """The shared ``.git`` behind a linked worktree's gitdir — its
    ``index.lock`` belongs to the primary checkout. ``None`` for a
    primary checkout, a land's clone (its git dir is its own common dir)
    or when git cannot answer."""
    proc = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], tree)
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0 or not raw:
        return None
    common = Path(raw)
    return common if common != gitdir else None


def _ship_lock_holder(gitdir: Path) -> int | None:
    """The pid holding ship's own per-worktree lock
    (``<gitdir>/precis-ship-worktree.lock.d``, scripts/lib/ship-lock.sh)
    when that pid is alive — a ship in flight in this worktree, from a
    land or a hand run. ``None`` when no live holder answers."""
    try:
        holder = (gitdir / SHIP_LOCK_DIR / "holder").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"\bpid=(\d+)\b", holder)
    if m is None:
        return None
    pid = int(m.group(1))
    return pid if _process_alive(pid) else None


def _lock_holder_text(path: Path) -> str:
    try:
        return (path.read_text(encoding="utf-8") or "").strip()
    except OSError:
        return ""


def _stamp_lock_holder(
    fd: int | None,
    pid: int | None,
    todo_id: int = 0,
    branch: str = "",
    subject: str = "",
    flag: str = "",
    attempt: str = "",
    tree: str = "",
) -> None:
    """Record the holder in the lock file — ``pid=N td=M branch=B
    flag=F tree=T subject=S`` — so the next land can tell its own
    already-running ship (adopt it, keeping its flag) from a foreign or
    orphaned holder (retryable). ``pid`` is the ship's, never the
    worker's: ``None`` before ship exists stamps no pid, which a second
    land reads as an orphan and defers (td652194). ``tree`` is the folder
    the ship runs in: the lock of a land's own clone is per todo, so a
    later land job of the same todo reads a stamp naming another job's
    folder as foreign (:func:`_classify_land_lock`) — no job enters a
    folder another job made. Best-effort: a failed write never wedges the
    lock — the flock itself is the gate."""
    if fd is None:
        return
    parts = [f"pid={pid}"] if pid is not None else []
    if todo_id:
        parts.append(f"td={todo_id}")
    if branch:
        parts.append(f"branch={branch}")
    if flag:
        parts.append(f"flag={flag}")
    if attempt:
        parts.append(f"attempt={attempt}")
    if tree:
        parts.append(f"tree={tree}")
    if subject:
        parts.append(f"subject={subject.splitlines()[0]}")
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (" ".join(parts) + "\n").encode())
    except OSError:
        pass


def _lock_holder_stamp(text: str) -> dict[str, str]:
    """Parse a ``_stamp_lock_holder`` line into a dict; absent fields
    are simply missing keys (an old ``pid=N``-only stamp reads as
    ``{"pid": ...}``). The subject runs to the line's end, so the other
    keys are read only ahead of it — a subject quoting ``flag=`` never
    poses as the stamp's."""
    stamp: dict[str, str] = {}
    head = re.split(r"\bsubject=", text, maxsplit=1)[0]
    for key, pat, where in (
        ("pid", r"\bpid=(\d+)\b", head),
        ("td", r"\btd=(\d+)\b", head),
        ("branch", r"\bbranch=(\S+)", head),
        ("flag", r"\bflag=(--\S+)", head),
        ("attempt", r"\battempt=([0-9a-f]{32})\b", head),
        ("tree", r"\btree=(\S+)", head),
        ("subject", r"\bsubject=([^\n]*)", text),
    ):
        m = re.search(pat, where)
        if m is not None:
            stamp[key] = m.group(1).strip()
    return stamp


def _classify_land_lock(
    gitdir: Path,
    todo_id: int,
    branch: str,
    holder_text: str,
    *,
    name: str = LAND_LOCK_FILE,
    tree: Path | None = None,
) -> dict[str, Any]:
    """Read a held land lock's stamp into a verdict (``gitdir / name`` is
    the lock file, :func:`_land_lock_where`).

    ``"mine"`` — the stamp names this same slice or branch and the
    stamped pid is alive: the caller adopts the running ship
    (:func:`_adopt_ship`). When ``tree`` is given and the stamp names a
    folder, it must be that folder: a ship of the same todo running in
    another land job's clone is ``"other"`` (gr741695: the adopter's
    folder holds none of that ship's work, and no job enters another's
    folder). ``"orphan"`` — the stamped pid is dead or unreadable while
    some descendant still pins the fd (:func:`_lock_holders` names them).
    ``"other"`` — a live pid behind a different slice's stamp. Anything
    but ``"mine"`` is a retryable busy for the caller."""
    stamp = _lock_holder_stamp(holder_text)
    pid = int(stamp["pid"]) if stamp.get("pid") else None
    if pid is not None and _process_alive(pid):
        if stamp.get("td") == str(todo_id) or (
            branch and stamp.get("branch") == branch
        ):
            foreign = stamp.get("tree")
            if tree is not None and foreign and foreign != str(tree):
                return {
                    "kind": "other",
                    "pid": pid,
                    "stamp": stamp,
                    "why": f"a ship of this slice is running in another land's "
                    f"folder (pid {pid}, {Path(foreign).name})",
                }
            return {
                "kind": "mine",
                "pid": pid,
                "stamp": stamp,
                "why": f"this slice's ship is already running (pid {pid})",
            }
        who = f"td={stamp.get('td') or '?'} branch={stamp.get('branch') or '?'}"
        return {
            "kind": "other",
            "pid": pid,
            "stamp": stamp,
            "why": f"the land lock is held by a live pid {pid} ({who})",
        }
    holders = _lock_holders(gitdir / name) or []
    holding = ", ".join(p for p, _c in holders) if holders else "no live holder listed"
    state = (
        f"stamped pid {pid} is dead"
        if pid is not None
        else "no ship pid stamped (a land still preparing its launch, or an orphan)"
    )
    return {
        "kind": "orphan",
        "pid": pid,
        "stamp": stamp,
        "why": f"the land lock is held by orphan(s) — {state}; holder pids: {holding}",
    }


#: The two variables that keep a clone's gate slots and release round state
#: in the host checkout's git common dir (``scripts/lib/gate-slot.sh``,
#: ``scripts/lib/round-lock.sh``): a clone's own git dir is its job's, so
#: without them its gates would queue on slots no sibling sees and a
#: release-fix land (``ship --onto``) would read an empty round journal
#: while the host's release deploys.
HOST_STATE_ENV_NAMES: tuple[str, ...] = (
    "PRECIS_GATE_SLOT_ROOT",
    "PRECIS_ROUND_STATE_ROOT",
)


def _host_state_env(tree: Path) -> dict[str, str]:
    """:data:`HOST_STATE_ENV_NAMES`, each the host checkout's git common
    dir (an absolute path; the hub's parent), for a land's own clone; ``{}``
    for any other folder, whose git dir already is that dir, and when the
    dir is not there (both scripts refuse a set-but-missing directory)."""
    if not _land_clone(tree):
        return {}
    try:
        common = _slice_ws.hub_path(_rs.slice_root()).parent.resolve()
    except Exception:
        return {}
    if not common.is_dir():
        return {}
    return {name: str(common) for name in HOST_STATE_ENV_NAMES}


def _land_lock_where(
    tree: Path, todo_id: int, gitdir: Path | None
) -> tuple[Path, str] | None:
    """``(directory, file name)`` of the land lock for ``tree``, or
    ``None`` when there is none to take.

    A land's own clone (:func:`_land_clone`) takes a per-todo file in the
    host checkout's git common dir — the hub's parent
    (:func:`_slice_ws.hub_path`): the clone's own git dir is its job's and
    a second land job of the todo never sees it. What the lock serialised
    was never two slices (the old file sat in each slice's own worktree
    gitdir) but two ships of one slice, which share the todo's mirror and
    launch tips (``mirror_dir(root, "td<id>")``). Any other tree (a PR
    land's worktree, a land minted before gr741695) keeps the file in its
    own gitdir."""
    if _land_clone(tree):
        try:
            hub = _slice_ws.hub_path(_rs.slice_root())
        except Exception:
            return None
        return hub.parent, LAND_LOCK_TODO_FILE.format(todo=todo_id)
    return (gitdir, LAND_LOCK_FILE) if gitdir is not None else None


def _try_land_lock(
    gitdir: Path, name: str = LAND_LOCK_FILE
) -> tuple[bool, int | None, str]:
    """One ``flock(LOCK_EX|LOCK_NB)`` attempt on
    ``<gitdir>/<name>`` (:func:`_land_lock_where`). ``(False, fd, "")`` —
    ours: the fd IS the lock, and :func:`_launch` passes it to the ship process so a
    second land finds it held for the run's whole life, not just this
    call's (a worker dying mid-land is exactly the window a retried
    land_slice would otherwise relaunch in — jo650396's duplicate
    ships). ``(True, None, <holder line>)`` — held: flocks release at
    process death, so a held lock means a live process owns it.
    ``(False, None, <why>)`` — the mechanism failed; the caller proceeds
    because ship's own ``precis-ship-worktree.lock.d`` still serialises
    a real overlap. The holder stamp is the caller's write —
    :func:`_launch_ship` stamps it once it owns the fd."""
    path = gitdir / name
    # A lock file may be unlinked by the land that held it last
    # (:func:`_unlink_land_lock`, while holding it), so a lock is ours only
    # when the file we locked is still the one at the path: otherwise
    # another process may have made a new file and locked that, and two
    # ships of one todo would each think they own it.
    for _attempt in range(3):
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            return False, None, f"{exc}"
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            os.close(fd)
            return False, None, "fcntl unavailable"
        except BlockingIOError:
            os.close(fd)
            return True, None, _lock_holder_text(path)
        except OSError as exc:
            os.close(fd)
            return False, None, f"{exc}"
        try:
            same = os.fstat(fd).st_ino == os.stat(path).st_ino
        except OSError:
            same = False
        if same:
            return False, fd, ""
        os.close(fd)
    return False, None, "the lock file was replaced under the lock three times"


def _unlink_land_lock(tree: Path, todo_id: int) -> None:
    """Remove a land clone's per-todo lock file from the host checkout's git
    common dir once the land that held it is over: one file per todo would
    pile up there for good. Only while holding the lock ourselves (so no
    ship or orphan of the todo is behind it) and only for a land's own
    clone; :func:`_try_land_lock` re-checks the file's identity after it
    locks, so a process that opened the file before this unlink can never
    take a lock on the removed one. A lock still held — by a ship, or by
    this very worker inside :func:`_launch_ship` — leaves the file. Never
    raises."""
    try:
        where = _land_lock_where(tree, todo_id, None)
        if where is None or not _land_clone(tree):
            return
        held, fd, _detail = _try_land_lock(*where)
        if held or fd is None:
            return
        try:
            (where[0] / where[1]).unlink(missing_ok=True)
        finally:
            os.close(fd)
    except Exception:
        log.warning("land_slice: land lock of td%d not removed", todo_id, exc_info=True)


def _land_busy(ctx: Any, todo_id: int, lane: Any, why: str) -> None:
    """The busy exit when a ship we cannot adopt owns this worktree —
    another slice's land lock, a hand-run ship, or an orphaned flock a
    dead land left pinned (gr650484: two ``scripts/ship --quick`` on
    slice-td649847 raced one worktree). It ends attempt-free, never
    ``succeeded``: a green exit could leave the land record, gate and
    cleanup unwritten if the original land job is gone; the tick
    re-mints under ``infra-land`` instead."""
    ctx.append_chunk(
        "job_summary",
        f"land_slice td{todo_id}: ship busy — {why}; deferred, no second ship launched",
    )
    _fail(
        ctx,
        todo_id,
        lane,
        f"ship busy: {why}",
        handoff=(
            "land DEFERRED (not red): a ship already owns this worktree "
            f"({why}). The tick re-mints the land attempt-free; the "
            "running ship's own land record — or a later pass's "
            "already-landed check — finishes the slice."
        ),
        attempt_free="ship-busy",
        folder=FOLDER_KEEP,
    )


def _adopt_ship(
    ctx: Any,
    tree: Path,
    todo_id: int,
    busy: dict[str, Any],
    subject: str | None = None,
    flag: str | None = None,
) -> dict[str, Any]:
    """Adopt the ship this slice's earlier land already launched: the
    land lock's stamp names this same slice/branch and a live pid, so
    this job polls its exit file the way a relaunched handle's poll
    does and drives :func:`_landed_finish` when it exits — the land
    record, gate and cleanup are written even if the original land job
    is gone (gr650484). The subject rides the stamp; a stamp without
    one (a pre-stamp land) falls back to the branch's squash subject —
    a ``git log`` read, never an index write the running ship could
    collide with. The flag rides it too (td652194: poll's FULL-selector
    relaunch compares it); a stamp without one takes the caller's, else
    submit's engine/spot meta is stamped here and the flag planned from
    it, so :func:`_planned_flag` agrees."""
    pid = int(busy["pid"])
    flag = busy["stamp"].get("flag") or flag
    if not flag:
        params = dict((ctx.meta or {}).get("params") or {})
        paths = _rs.changed_paths(tree)
        hits = ["<unreadable>"] if paths is None else _lanes.engine_hits(paths)
        spot = spot_selection(tree) if hits or params.get("checked") is True else False
        ctx.set_meta(engine_paths=hits[:50], spot_selection=spot)
        flag = ship_flag(params, engine=bool(hits), spot=spot)
    subject = (
        subject
        or busy["stamp"].get("subject")
        or squash_subject(
            _branch_subjects(tree) or [],
            todo_id,
            node=_node_subject(ctx.store, todo_id),
        )
        or ""
    )
    scratch = tree / "scratch"
    attempt = busy["stamp"].get("attempt")
    launched_todo = busy["stamp"].get("td")
    attempt_dir = (
        _rs.ops_root() / f"td{launched_todo}" / attempt
        if attempt and launched_todo
        else None
    )
    exit_file = attempt_dir / "exit" if attempt_dir else scratch / "land_slice.exit"
    log_file = attempt_dir / "log" if attempt_dir else scratch / "land_slice.log"
    launched = _now()
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "worktree": str(tree),
        "log": str(log_file),
        "exit_file": str(exit_file),
        "subject": subject,
        "flag": flag,
        "adopted": True,
        "started_at": launched.strftime(_TS_FMT),
        "deadline": launched.timestamp() + wall_seconds(),
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        handle["boot_id"], handle["start_time"] = identity
    ps_identity = (
        _rs._darwin_process_identity(pid) if sys.platform == "darwin" else None
    )
    if ps_identity is not None:
        handle["ps_identity"] = ps_identity
    # As after a launch: the ship runs, so its handle is returned whatever
    # the bookkeeping does.
    try:
        ctx.set_meta(land_subject=subject)
        ctx.append_chunk(
            "job_event",
            f"land_slice: adopted the already-running ship pid {pid} in "
            f"{tree} — its exit file lands this job's record",
        )
    except Exception:
        log.warning(
            "land_slice: bookkeeping after adopting the ship pid %d failed",
            pid,
            exc_info=True,
        )
    return handle


def reconcile(ctx: Any) -> Reconcile | None:
    """Resume ship poll, including publication of a completed mirror squash.

    A free flock cannot establish whether ship already ran. Only this
    submit attempt's launch or exit record can resume poll.
    """
    return _rs._recover_launch(ctx)


def _index_lock_pairs(gitdir: Path, tree: Path) -> list[tuple[Path, Path]]:
    """``(index.lock, checkout it guards)`` this land must clear: the
    slice's own first, then the primary checkout's when ``tree`` is a
    linked worktree — ship's main-sync fast-forwards the primary's
    local main, and a lock there wedges the land just as surely
    (jo650356's red was the shared index, not the slice's). The guard
    tree is what :func:`_git_in_tree` scans for a live writer."""
    pairs = [(gitdir / "index.lock", tree)]
    common = _git_common_dir(gitdir, tree)
    if common is not None:
        pairs.append((common / "index.lock", common.parent))
    return pairs


def _index_lock_judge(
    lock: Path, checkout: Path
) -> tuple[str, os.stat_result | None, str, list[tuple[str, str]]]:
    """``(state, judged, why, ignored)`` — :func:`_index_lock_state` plus
    why a lock was kept: ``young | git-in-tree | held-by-git |
    lsof-missing | lsof-error | ps-error | git-cwd-unreadable`` (``""``
    when stale or absent). ``ignored`` lists the non-git ``(pid, command)``
    holders seen: recorded, never obeyed (D3 — the Colima VM's file share
    holds the handle of a lock no git owns)."""
    try:
        st = lock.stat()
    except OSError:
        return "absent", None, "", []
    if time.time() - st.st_mtime < index_lock_stale_s():
        return "live", st, "young", []
    tree_git = _git_in_tree(checkout)
    if tree_git is not False:
        return "live", st, "git-in-tree" if tree_git else _tree_doubt(), []
    holders = _lock_holders(lock)
    if holders is None:
        return "live", st, "lsof-missing" if _lsof_bin() is None else "lsof-error", []
    git = [h for h in holders if _is_git_comm(h[1])]
    other = [h for h in holders if not _is_git_comm(h[1])]
    if git:
        return "live", st, "held-by-git", other
    return "stale", st, "", other


def _tree_doubt() -> str:
    """Why :func:`_git_in_tree` answered ``None``: the ps scan failed, or
    a live git's cwd could not be read."""
    try:
        cp = subprocess.run(
            ["ps", "-U", str(os.getuid()), "-o", "pid=,comm="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "ps-error"
    return (
        "ps-error"
        if cp.returncode != 0 or not cp.stdout.strip()
        else ("git-cwd-unreadable")
    )


def _index_lock_state(lock: Path, checkout: Path) -> tuple[str, os.stat_result | None]:
    """``(state, judged)``: ``state`` is ``"absent" | "stale" | "live"``
    and ``judged`` the stat the judgement ran on (``None`` when absent).
    The removal path carries ``judged`` to the unlink so only the inode
    that was judged can ever be removed — never a replacement that
    raced in after it. ``"stale"`` only on positive evidence — the same
    keep-on-doubt rule scripts/reap-index-locks runs: mtime older than
    :func:`index_lock_stale_s`, no *git* process holds the file open
    (:func:`_held_open` answered ``False``), and no live git has its cwd
    inside ``checkout`` (:func:`_git_in_tree` answered ``False``). A
    young lock, one lsof or ps cannot judge, or one with a live git in
    the tree is ``"live"`` — waited on, never removed. The reason a lock
    is kept is :func:`_index_lock_judge`'s third field."""
    try:
        st = lock.stat()
    except OSError:
        return "absent", None
    if time.time() - st.st_mtime < index_lock_stale_s():
        return "live", st
    if _git_in_tree(checkout) is not False:
        return "live", st
    if _held_open(lock) is not False:
        return "live", st
    return "stale", st


def _lsof_bin() -> str | None:
    """lsof's path: ``PATH`` first, else the macOS/Linux system dirs — a
    worker's PATH often lacks ``/usr/sbin`` (gr684320)."""
    found = shutil.which("lsof")
    if found:
        return found
    for cand in _LSOF_FALLBACKS:
        if os.path.exists(cand):
            return cand
    return None


_LSOF_FALLBACKS = ("/usr/sbin/lsof", "/usr/bin/lsof")


def _is_git_comm(comm: str) -> bool:
    base = os.path.basename(comm.strip())
    return base == "git" or base.startswith("git-")


def _lock_holders(lock: Path) -> list[tuple[str, str]] | None:
    """The ``(pid, command)`` pairs holding ``lock`` open (lsof), or
    ``None`` when lsof cannot answer — missing, dead, timed out, or
    stderr. lsof exits 1 on an ordinary "nothing holds it": that is
    ``[]``, not a failure."""
    lsof = _lsof_bin()
    if lsof is None:
        return None
    try:
        cp = subprocess.run(
            ["lsof", "-F", "pc", "--", str(lock)],
            executable=lsof,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.stderr.strip():
        return None
    out: list[tuple[str, str]] = []
    pid = ""
    for ln in cp.stdout.splitlines():
        if ln.startswith("p"):
            pid = ln[1:]
            out.append((pid, ""))
        elif ln.startswith("c") and out:
            out[-1] = (out[-1][0], ln[1:])
    return out


def _held_open(lock: Path) -> bool | None:
    """``True``/``False``/``None`` for "a live *git* has ``lock`` open" —
    :func:`_lock_holders` filtered to git processes (D3: a non-git holder
    never keeps a lock). ``None`` keeps the lock on doubt."""
    holders = _lock_holders(lock)
    if holders is None:
        return None
    return any(_is_git_comm(c) for _p, c in holders)


def _proc_cwd(pid: str) -> str | None:
    """A process's cwd: ``/proc`` where it exists, else lsof."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    lsof = _lsof_bin()
    if lsof is None:
        return None
    try:
        cp = subprocess.run(
            ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
            executable=lsof,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for ln in cp.stdout.splitlines():
        if ln.startswith("n"):
            return ln[1:]
    return None


def _is_zombie(pid: str) -> bool:
    """A zombie's cwd reads back empty — it is dead for staleness
    purposes. Linux ``/proc`` first, ``ps`` fallback."""
    try:
        with Path(f"/proc/{pid}/stat").open(encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[-1].split()[0] == "Z"
    except OSError:
        pass
    try:
        cp = subprocess.run(
            ["ps", "-o", "stat=", "-p", pid],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.stdout.strip().startswith("Z")


def _proc_dead(pid: str) -> bool:
    """Positive evidence only: the pid is gone or a zombie."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False
    return _is_zombie(pid)


def _git_in_tree(checkout: Path) -> bool | None:
    """``True``/``False``/``None`` for "a live git/git-* of this user has
    its cwd inside ``checkout``" — the writer a lock is meant to stop.
    ``None`` (the process table or a live git's cwd is unreadable) keeps
    the lock: a lock whose holder cannot be disproven is live."""
    try:
        cp = subprocess.run(
            ["ps", "-U", str(os.getuid()), "-o", "pid=,comm="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    root = os.path.realpath(checkout)
    saw_proc = False
    for line in cp.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        saw_proc = True
        pid, comm = parts[0].strip(), parts[1].strip()
        base = os.path.basename(comm)
        if base != "git" and not base.startswith("git-"):
            continue
        cwd = _proc_cwd(pid)
        if cwd is None:
            if _proc_dead(pid):
                continue
            return None
        real = os.path.realpath(cwd)
        if real == root or real.startswith(root + os.sep):
            return True
    # A ps that parsed nothing at all did not answer — a user's process
    # table is never empty — so the empty/garbage scan cannot disprove
    # a live writer either.
    return False if saw_proc else None


def _reap_index_lock(
    ctx: Any, lock: Path, judged: os.stat_result, *, who: str = NAME
) -> bool:
    """Remove the ``index.lock`` the keep-on-doubt judge called stale —
    identified by ``judged``, the stat :func:`_index_lock_state` saw.
    The single re-stat right before the unlink must match its
    ``st_dev``/``st_ino``/``st_mtime_ns``: a lock replaced between
    judgement and unlink is a live writer's, never ours to remove
    (gr650484). ``False`` when the identity no longer matches or the
    unlink lost a race (another reaper got there first — fine either
    way). ``who`` prefixes the job_event line (run_slice reuses this
    reap)."""
    try:
        now = lock.stat()
    except OSError:
        return False
    if (
        now.st_dev != judged.st_dev
        or now.st_ino != judged.st_ino
        or now.st_mtime_ns != judged.st_mtime_ns
    ):
        return False
    try:
        lock.unlink()
    except OSError:
        return False
    ctx.append_chunk(
        "job_event",
        f"{who}: reaped stale index.lock {lock} "
        f"(age {int(time.time() - judged.st_mtime)}s; no live git in the "
        "tree held it)",
    )
    return True


def _clear_index_locks(
    ctx: Any, gitdir: Path, tree: Path, todo_id: int, lane: Any
) -> bool:
    """gr650484 — ``False`` means ``_fail`` already ran, launch nothing.

    A stale ``index.lock`` (a killed git's remnant — jo650396's squash
    red, jo650356's main-sync red) wedges ship's first write and every
    retry after it; a live one is a git in flight ship must wait out,
    not collide with. Each pair of :func:`_index_lock_pairs` gets the
    same rule: provably dead → removed with a job_event line and
    :func:`run_slice._record_reap` (job meta ``index_lock_reaped`` + a
    todo comment — the dispatcher's re-formed-lock accounting counts
    land-side reaps too, gr684320); live or unjudgeable → the brief
    :func:`index_lock_wait_s` wait, then an explicit ``attempt_free =
    "index-lock"`` fail (verbatim, not ``infra-land`` — a plain wait
    clears itself, it must never hit the re-land cap or park the node)
    — never the red path, which would charge the branch for infra that
    is not its fault."""
    deadline = time.monotonic() + index_lock_wait_s()
    for lock, checkout in _index_lock_pairs(gitdir, tree):
        while True:
            state, judged = _index_lock_state(lock, checkout)
            if state == "absent":
                break
            if state == "stale" and judged is not None:
                _s, _j, _w, ignored = _index_lock_judge(lock, checkout)
                if _reap_index_lock(ctx, lock, judged):
                    _rs._record_reap(
                        ctx,
                        lock,
                        judged,
                        why="stale",
                        forced=False,
                        holders=[f"{pid} {cmd}" for pid, cmd in ignored],
                        todo_id=todo_id,
                    )
                    break
            if time.monotonic() >= deadline:
                _fail(
                    ctx,
                    todo_id,
                    lane,
                    f"index.lock {lock} was still held after "
                    f"{index_lock_wait_s():.0f}s — a live git owns the "
                    "checkout",
                    handoff=(
                        "land DEFERRED (not red): index.lock "
                        f"{lock} was held by a live git past the brief "
                        "wait — nothing on the branch to fix. The tick "
                        "re-mints the land attempt-free; if it keeps "
                        "recurring, find the git process running in "
                        "that checkout."
                    ),
                    attempt_free="index-lock",
                    folder=FOLDER_KEEP,
                    verbatim=True,
                )
                return False
            time.sleep(1.0)
    return True


def _land_base(tree: Path, onto: str | None) -> str:
    """The trusted base for the land's own reads: GitHub's tip of the land
    target, read through the mirror's :func:`ls_remote` oracle — a
    non-repo cwd, system/global config off — and fetched into the tree's
    object store. Never the remote-tracking ref alone: the shared git
    dir's ``refs/remotes/origin/*`` and the tree's config are writable by
    branch code, so an ``origin/main`` read could name a fabricated tip
    the deny gate then diffed (review td660513). Falls back to the
    tracking ref when GitHub cannot answer — a fast path's trust, not a
    gate: on doubt ship still runs and the sync's leases decide."""
    ref = onto or "main"
    try:
        root = _rs.slice_root()
        url = _github_url(root)
        if url:
            tip = (
                _factory_git_mirror.ls_remote(
                    url, (ref,), env=_factory_git.git_auth_env(base=land_env())
                )
                or {}
            ).get(ref)
            if tip:
                # Fetch the objects (unauthenticated — the sha came from
                # the oracle; a redirected fetch can only refuse), then
                # diff against the oracle's sha, not the tracking ref the
                # fetch may also have moved.
                _git(
                    [
                        "fetch",
                        "--quiet",
                        url,
                        f"+refs/heads/{ref}:refs/remotes/origin/{ref}",
                    ],
                    tree,
                )
                return tip
    except Exception:
        log.warning(
            "land_slice: verified land base failed — using the tracking ref",
            exc_info=True,
        )
    return f"origin/{ref}"


def _already_landed(
    tree: Path, subject: str, onto: str | None = None
) -> tuple[bool, str | None]:
    """``(True, sha|None)`` when the slice's work already sits on
    ``origin/main`` — or on ``origin/<onto>``, the release branch a
    release fix lands on (release-train T1; read the same way against
    that ref): the branch's tree identical to main's tip (ship's
    own nothing-new verdict — the sha is another land's, none returned;
    gr561142), or a commit on origin/main already carrying this land's
    squash subject AND its content — a ship that landed but died before
    its record wrote (gr650484: td649946's land ran a second ship on an
    already-landed slice). The subject match alone is never enough:
    one todo can land twice (td572037, td572552, td626856 did), so the
    proof is content — the paths the slice changed since
    ``merge-base origin/main HEAD`` must already be identical between
    ``origin/main`` and ``HEAD``. ``(False, None)`` otherwise — and
    whenever git cannot answer: this is a fast path, never a gate; on
    doubt ship still runs and its own nothing-new guard is the
    arbiter."""
    ref = _land_base(tree, onto)
    if ref.startswith("origin/"):
        # The oracle could not verify a tip: refresh the tracking ref
        # the legacy way and read against that.
        spec = f"+refs/heads/{onto}:refs/remotes/{ref}" if onto else "main"
        if _git(["fetch", "--quiet", "origin", spec], tree).returncode != 0:
            return False, None
    if _git(["diff", "--quiet", ref, "HEAD"], tree).returncode == 0:
        return True, None
    base = _git(["merge-base", ref, "HEAD"], tree)
    merge_base = (base.stdout or "").strip()
    if base.returncode != 0 or not merge_base:
        return False, None
    names = _git(["diff", "--name-only", merge_base, "HEAD"], tree)
    if names.returncode != 0:
        return False, None
    paths = [ln.strip() for ln in names.stdout.splitlines() if ln.strip()]
    proc = _git(
        [
            "log",
            f"--max-count={ALREADY_LANDED_WINDOW}",
            "--format=%H%x00%s",
            ref,
        ],
        tree,
    )
    if proc.returncode != 0:
        return False, None
    for line in proc.stdout.splitlines():
        sha, _, subj = line.partition("\x00")
        if subj.strip() != subject:
            continue
        # A same-subject commit on main is this land's only when the
        # slice's own changed paths are already identical there.
        if _git(["diff", "--quiet", ref, "HEAD", "--", *paths], tree).returncode == 0:
            return True, sha.strip()
        return False, None
    return False, None


def _ship_busy(tree: Path, todo_id: int, branch: str) -> dict[str, Any] | None:
    """A verdict when a ship already owns this worktree, else ``None``.

    The land flock is probed first because its stamp carries identity:
    ``"mine"`` (same slice/branch, live stamped pid) the caller adopts
    via :func:`_adopt_ship`; ``"other"``/``"orphan"`` it exits
    retryable via :func:`_land_busy`. A live holder in ship's own
    ``precis-ship-worktree.lock.d`` — a ship launched without our
    flock, e.g. by hand — names no slice, so it reads ``"ship"`` and is
    retryable too. Submit's early peek: a free flock is released at
    once; :func:`_launch_ship` makes the same checks while actually
    acquiring. Kept to lock files and ``rev-parse`` so the peek never
    collides with the running ship's ``index.lock``. ``tree`` need not
    exist yet (a land probes before it cuts its clone): the todo's lock
    in the host checkout's git common dir (:func:`_land_lock_where`) is
    probed either way, ship's own worktree lock only in a folder that is
    there."""
    gitdir = _worktree_gitdir(tree) if tree.is_dir() else None
    where = _land_lock_where(tree, todo_id, gitdir)
    if where is None:
        return None
    held, fd, detail = _try_land_lock(*where)
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    if held:
        return _classify_land_lock(
            where[0],
            todo_id,
            branch,
            detail,
            name=where[1],
            tree=tree if _land_clone(tree) else None,
        )
    holder = _ship_lock_holder(gitdir) if gitdir is not None else None
    if holder is not None:
        return {
            "kind": "ship",
            "pid": holder,
            "stamp": {},
            "why": (
                "a ship is already running in this worktree (its "
                f"lockdir names live pid {holder})"
            ),
        }
    return None


def _finish_if_landed(
    ctx: Any,
    tree: Path,
    params: dict[str, Any],
    subject: str,
    onto: str | None,
) -> bool:
    """Finish the job green without a ship when the slice's work is
    already on ``origin/<onto or main>`` (:func:`_already_landed`);
    ``True`` when it did."""
    landed, landed_sha = _already_landed(tree, subject, onto)
    if not landed:
        return False
    where = onto or "main"
    ctx.append_chunk(
        "job_event",
        f"land_slice: the slice's work is already on origin/{where}"
        + (
            f" (sha {landed_sha[:9]})"
            if landed_sha
            else f" (branch tree identical to {where}'s)"
        )
        + " — no scripts/ship run",
    )
    _landed_finish(
        ctx,
        {"worktree": str(tree), "subject": subject},
        params,
        sha=landed_sha,
        exit_code=None,
        status="already-landed",
    )
    return True


#: :func:`_sync_release` outcomes: the land failed on a conflict, the head
#: was merged in, or the branch was left as it was.
SYNC_FAILED = "failed"
SYNC_MERGED = "merged"
SYNC_LEFT = "left"

#: The merge commit's identity (it never lands — ship writes its own
#: commit). The lander's git env blinds global and system config, so a
#: host whose identity lives only there would otherwise fail the merge.
_SYNC_IDENTITY = (
    "-c",
    "user.name=precis land_slice",
    "-c",
    "user.email=land-slice@precis.invalid",
)


def _sync_release(ctx: Any, tree: Path, onto: str, todo_id: int, lane: Any) -> str:
    """Merge ``origin/<onto>`` into a release fix's branch before ship
    lints it (gr738762). ``scripts/ship --onto`` lints the branch as cut
    and only then applies its diff onto the release head with
    ``git merge-tree``; sibling fixes land on the same release branch in
    parallel, so the lint judged a stale tree — a red a sibling had
    already fixed failed this land, and a real conflict surfaced only
    after the whole gate ran. With the head merged in first, the lint
    sees the fix on top of the current release and a conflict is the
    builder's to resolve, briefed with the right base. Ship takes the
    landed commit's diff from the newer merge-base, so the land is still
    only the fix.

    Skips — launching as before — when the fetch fails (ship reports the
    network itself), the tree is dirty, git cannot answer, or the merge
    fails without a conflict (an identity or an untracked-file refusal;
    aborted; a merge that timed out is aborted too, so ship never gets a
    half-merged tree). :data:`SYNC_FAILED` after failing the land on a
    conflict (tree restored by ``merge --abort``), :data:`SYNC_MERGED`
    when the head was merged in, else :data:`SYNC_LEFT`. What happened
    lands on the job's ``meta.release_sync`` and a job event."""
    note = ""
    outcome = SYNC_LEFT
    merging = False
    try:
        if not _fetch_onto(tree, onto):
            note = "skipped: fetch failed"
        else:
            ref = f"origin/{onto}"
            anc = _git(["merge-base", "--is-ancestor", ref, "HEAD"], tree).returncode
            dirty = _git(["status", "--porcelain", "--untracked-files=no"], tree)
            if anc == 0:
                note = "already current"
            elif anc != 1:
                note = "skipped: ancestry unreadable"
            elif dirty.returncode != 0 or dirty.stdout.strip():
                note = "skipped: dirty tree"
            else:
                tip = (
                    _git(["rev-parse", "--verify", "--quiet", ref], tree).stdout or ""
                ).strip()
                merging = True
                merge = _git([*_SYNC_IDENTITY, "merge", "--no-edit", ref], tree)
                merging = False
                if merge.returncode == 0:
                    note = f"synced onto {tip[:12]}"
                    outcome = SYNC_MERGED
                else:
                    names = _git(["diff", "--name-only", "--diff-filter=U"], tree)
                    paths = [p.strip() for p in names.stdout.splitlines() if p.strip()]
                    _git(["merge", "--abort"], tree)
                    if paths:
                        _release_sync_conflict(ctx, todo_id, lane, onto, paths)
                        return SYNC_FAILED
                    detail = (merge.stderr or merge.stdout or "").strip()
                    note = f"skipped: merge failed ({detail[:120]})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        note = f"skipped: git did not answer ({type(exc).__name__})"
        if merging:
            try:
                _git(["merge", "--abort"], tree)
            except (OSError, subprocess.TimeoutExpired):
                note += "; merge --abort did not answer either"
    ctx.set_meta(release_sync=note)
    ctx.append_chunk("job_event", f"land_slice: release sync onto {onto}: {note}")
    return outcome


def _release_sync_conflict(
    ctx: Any, todo_id: int, lane: Any, onto: str, paths: list[str]
) -> None:
    """Fail the land for a conflict merging ``origin/<onto>`` into the
    branch: an ordinary red (an attempt), the handoff naming the paths
    and the right base — a merge of the release, never ``origin/main``
    and never a rebase of pushed commits."""
    listed = ", ".join(paths)
    ctx.set_meta(release_sync=f"conflict in {listed}")
    _fail(
        ctx,
        todo_id,
        lane,
        f"{RELEASE_SYNC_CONFLICT_MARK}: merging origin/{onto} into the branch "
        f"conflicts in {listed}",
        handoff=(
            f"land FAILED: {onto} moved since this branch was cut; merging "
            f"origin/{onto} into the branch conflicts in {listed}. Merge "
            f"origin/{onto} into the branch (never origin/main, never a "
            "rebase of pushed commits), resolve those paths keeping the "
            "release's side for anything this fix does not need to change, "
            "commit, and hand off again. Nothing was pushed."
        ),
        folder=FOLDER_DROP,
    )


def _launch_ship(
    ctx: Any,
    tree: Path,
    flag: str,
    subject: str,
    *,
    carry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Launch ``scripts/ship <flag> <subject>`` detached in ``tree`` and
    return the fresh handle (``flag`` kept in it so :func:`poll` can relaunch;
    ``main_refusals`` / ``last_refusal`` copied from ``carry``). A live ship
    this same slice already launched returns an adopted handle instead —
    :func:`poll` then drives its exit file like a relaunch's. ``None`` after
    driving the job terminal: the launch itself failed, a foreign or
    orphaned holder owns the worktree (retryable), the branch's work is
    already on origin/main, or an ``index.lock`` never freed."""
    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    lane = params.get("lane")
    number = _pr_param(params)
    branch = str(
        params.get("branch")
        or (f"slice/pr-{number}" if number is not None else _rs.slice_branch(todo_id))
    )
    scratch = tree / "scratch"
    log_path = scratch / "land_slice.log"
    exit_path = scratch / "land_slice.exit"
    # release-train T1: a release fix lands onto its release branch —
    # read from the todo on every launch, so a relaunch lands the same
    # place the first launch did.
    onto = land_target(ctx.store, params, todo_id)
    if onto is not None:
        closed = _release_closed(ctx.store, todo_id, onto)
        if closed is not None:
            # ship's own round lock is this host's checkout, not the deploy
            # owner's: the plan's record is the deploy's view (T1 review).
            ctx.set_meta(land_again_cap=True)
            _fail(
                ctx,
                todo_id,
                lane,
                f"{onto} takes no more fixes: {closed}",
                handoff=(
                    f"land REFUSED: {closed}, so a fix landing on {onto} now "
                    "would not ship with it. Nothing was pushed; the work stays "
                    "in the hub. A coordinator call: land it on main, or onto "
                    "the next release once it is cut."
                ),
                attempt_free="release-deployed",
                # before the land lock is taken: look for a ship as usual
                folder=FOLDER_PROBE,
            )
            return None
    # gr650484 — serialize ships before anything else touches the tree.
    # Our flock comes first because its stamp carries the slice's
    # identity: held by a live pid behind our own stamp means this
    # slice's ship is already running — adopt it so its land record is
    # still written; held by anything else (a foreign stamp, a dead
    # stamped pid whose orphan descendants pin the fd, a live ship's
    # lockdir) fails retryable, never succeeded-without-record. The fd
    # we hold rides into the launched ship so the lock outlives this
    # worker if it dies mid-land. Then the cheap outs: already-on-main
    # finishes green, a stale index.lock is reaped, a live one is
    # waited on and then fails retryable.
    gitdir = _worktree_gitdir(tree)
    lock_fd: int | None = None
    ship_fd: int | None = None
    try:
        if gitdir is None:
            ctx.append_chunk(
                "job_event",
                "land_slice: worktree gitdir unresolved — the land lock, "
                "already-landed and index.lock checks are skipped; "
                "ship's own worktree lock still serialises",
            )
        else:
            lock_dir, lock_name = _land_lock_where(tree, todo_id, gitdir) or (
                gitdir,
                LAND_LOCK_FILE,
            )
            held, lock_fd, detail = _try_land_lock(lock_dir, lock_name)
            if held:
                busy = _classify_land_lock(
                    lock_dir,
                    todo_id,
                    branch,
                    detail,
                    name=lock_name,
                    tree=tree if _land_clone(tree) else None,
                )
                if busy["kind"] == "mine":
                    return _adopt_ship(
                        ctx, tree, todo_id, busy, subject=subject, flag=flag
                    )
                _land_busy(ctx, todo_id, lane, busy["why"])
                return None
            if lock_fd is None:
                ctx.append_chunk(
                    "job_event",
                    f"land_slice: land lock unavailable ({detail}) — "
                    "proceeding; ship's own worktree lock still serialises",
                )
            else:
                # td652194: no pid until ship's exists — the worker's own
                # would read as "mine" to a second land in this window,
                # and its adopted handle's kill would signal the worker.
                _stamp_lock_holder(
                    lock_fd, None, todo_id, branch, subject, flag, tree=str(tree)
                )
            holder = _ship_lock_holder(gitdir)
            if holder is not None:
                _land_busy(
                    ctx,
                    todo_id,
                    lane,
                    "a ship is already running in this worktree (its "
                    f"lockdir names live pid {holder})",
                )
                return None
            if _finish_if_landed(ctx, tree, params, subject, onto):
                return None
            if not _clear_index_locks(ctx, gitdir, tree, todo_id, lane):
                return None
        # gr738762: a release fix is merged up to the release head here,
        # under the land lock and past the index.lock reap, so the first
        # launch and every relaunch (all go through this function) lint
        # the fix on top of the current release.
        if onto is not None:
            synced = _sync_release(ctx, tree, onto, todo_id, lane)
            if synced == SYNC_FAILED:
                return None
            # A sibling may have landed this very change: the merge makes
            # the tree identical to the release head, which ship's
            # onto-preflight refuses as "nothing to release" (a red), so
            # the already-landed proof runs again on the merged tree.
            if synced == SYNC_MERGED and _finish_if_landed(
                ctx, tree, params, subject, onto
            ):
                return None
        evicted = _sweep_stacks(keep=tree)
        if evicted:
            ctx.append_chunk(
                "job_event",
                "land_slice: evicted idle slice stacks: " + ", ".join(evicted),
            )
        # gr659929 (review td660513): ship's gates run the branch's own
        # scripts host-side, so ship's tree holds NO credential — not the
        # token in the env, not GH_TOKEN, not a token fd a gate child
        # could pread, and no helper it can make answer. The lander keeps
        # a bare mirror of the land refs in the main checkout's scratch
        # (:mod:`precis.workers.factory_git_mirror`), THIS launch's own
        # directory — a sibling land's populate can never refresh away
        # an unsynced squash. Populate runs BEFORE the drift probe: the
        # probe's helper is sourced from the verified main tip the
        # populate fetched and oracle-checked — never branch code under
        # the token (review finding 1). Every populate call goes through
        # the trusted boundary (fresh ops gitdir, hooks off, no
        # system/global config) because the mirror and the checkout are
        # adversary-writable (review finding 2).
        ship_env = {**land_env(), **_host_state_env(tree)}
        if _carries_pin(ctx.store, todo_id):
            # td659522 F7: ship's land_notify would close a pin-bump node
            # on the land; it stays open for the gate image wait, so the
            # notify marks are held as for a mirrored land and the lander
            # writes them itself (``notify_deferred``, ``_record_land``).
            ship_env["PRECIS_LAND_NOTIFY"] = "0"
            ship_env["PRECIS_GRIPE_NOTE"] = "0"
        token = _factory_git.factory_git_token()
        mirror: Path | None = None
        url: str | None = None
        mirror_tips: dict[str, str] | None = None
        if token:
            try:
                root = _rs.slice_root()
                mirror = _factory_git_mirror.mirror_dir(root, f"td{todo_id}")
                url = _github_url(root)
                if url is None:
                    raise RuntimeError(
                        "the factory checkout names no trusted origin url"
                    )
                populate_why: list[str] = []
                try:
                    mirror_tips = _factory_git_mirror.populate(
                        mirror,
                        _factory_git_mirror.cache_dir(root),
                        url,
                        onto,
                        env=_factory_git.git_auth_env(base=land_env()),
                        why=populate_why,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise _MirrorRefreshError(
                        "mirror refresh: " + _factory_git_mirror.refresh_error(exc)
                    ) from None
                if mirror_tips is None:
                    raise _MirrorRefreshError(
                        "the factory git mirror could not be refreshed from origin"
                        + (f" ({populate_why[-1]})" if populate_why else "")
                    )
            except Exception as exc:
                log.warning(
                    "land_slice: mirror populate for td%d failed",
                    todo_id,
                    exc_info=True,
                )
                _fail(
                    ctx,
                    todo_id,
                    params.get("lane"),
                    f"ship launch failed: {exc}",
                    handoff=f"land FAILED: scripts/ship did not start ({exc}).",
                    # A mirror refresh failure is infra (network), not the
                    # work: re-mint the land attempt-free (gr730461).
                    attempt_free=(
                        "mirror-refresh"
                        if isinstance(exc, _MirrorRefreshError)
                        else None
                    ),
                    folder=FOLDER_DROP,
                )
                return None
        else:
            # No credential resolves this launch — ship pushes origin on
            # the host's own credentials, and poll must not read it as
            # mirrored: drop every record an earlier token-bearing
            # attempt may have left — the launch files AND the durable
            # stamp on the todo (review td660513, second pass).
            try:
                _factory_git_mirror.drop_launch(_rs.slice_root(), f"td{todo_id}")
            except Exception:
                pass
            try:
                ctx.store.stamp_ref_meta(todo_id, {FACTORY_MIRROR_KEY: None})
            except Exception:
                pass
        # A release fix skips main's drift probe: ship's release lane
        # skips its drift guard too — the release push runs its own CI.
        pre: dict[str, Any] = (
            {"ran": False, "rc": None, "age": "", "diag": ""}
            if onto
            else _drift_preflight(
                tree, mirror=mirror, main_tip=(mirror_tips or {}).get("main")
            )
        )
        if _gate_unknown(pre):
            # Fail closed: the lander's identity cannot see main's gate, so do not
            # let ship's "unknown never refuses" carry an unattended land. Park
            # under the same backoff and cap as a main-side refusal.
            parked: dict[str, Any] = {
                "worktree": str(tree),
                "log": str(log_path),
                "exit_file": str(exit_path),
                "subject": subject,
                "flag": flag,
            }
            for key in ("main_refusals", "last_refusal", "gate_kills", "release_busy"):
                if carry is not None and key in carry:
                    parked[key] = carry[key]
            refusals = int(parked.get("main_refusals") or 0) + 1
            if refusals > MAX_MAIN_REFUSALS:
                branch = str(params.get("branch") or _rs.slice_branch(todo_id))
                reason, handoff = _blocked_handoff(branch, refusals, UNKNOWN_GATE_CROSS)
                ctx.append_chunk(
                    "job_summary",
                    f"land_slice td{todo_id}: BLOCKED — {UNKNOWN_GATE_CROSS}\n"
                    f"drift probe (lander identity): {pre['diag']}",
                )
                _fail(
                    ctx,
                    todo_id,
                    params.get("lane"),
                    reason,
                    handoff=handoff,
                    folder=FOLDER_DROP,
                )
                return None
            _defer_to_main(ctx, parked, UNKNOWN_GATE_CROSS, refusals, probe=pre["diag"])
            return parked
        if pre.get("ran"):
            # What the lander's identity saw before ship ran: a verdict's age or
            # the helper's own "none" — on the job even when the land goes on
            # (jo550940 launched nine times and never recorded which).
            ctx.append_chunk(
                "job_event",
                f"land_slice: drift preflight (lander identity): age="
                f"{pre.get('age') or '-'}; {pre.get('diag')}",
            )
        # Dup the lock fd to a high number for the child: a low fd is
        # what scripts and shells shuffle (scripts/lib/round-lock.sh
        # uses fd 9), and a descendant closing or reusing it would drop
        # the flock early. F_DUPFD floor 100, the plain fd as fallback.
        if lock_fd is not None:
            try:
                import fcntl

                ship_fd = fcntl.fcntl(lock_fd, fcntl.F_DUPFD, 100)
            except (ImportError, OSError):
                ship_fd = lock_fd
        # The credential broker the mirror replaced failed the review:
        # its depth-authorized socket asks could not tell ship's own git
        # calls from a gate child's ``git credential fill`` (same depth,
        # same env, forgeable argv), and the broker itself launched with
        # ``python -m`` from the slice tree, where a branch-added root
        # ``precis/__init__.py`` shadowed the trusted package and read the
        # token fd.
        try:
            scratch.mkdir(parents=True, exist_ok=True)
            if mirror_tips is not None and mirror is not None and url is not None:
                # The launch tips persist NEXT TO the mirror, under the
                # main checkout's scratch — never this tree's own
                # scratch/, which is branch-writable: a second land that
                # ADOPTS this running ship (gr650484) rebuilds its handle
                # from the lock stamp without them, and a missing
                # tree-side record once let poll read "no mirror" and
                # record the mirror-only squash as landed (review
                # td660513). A record that cannot be written fails the
                # launch instead.
                if not _factory_git_mirror.write_launch_tips(
                    root, f"td{todo_id}", mirror_tips
                ):
                    raise RuntimeError(
                        "the mirror launch record could not be persisted"
                    )
                # And its durable half — on the todo's meta, out of every
                # same-uid-writable file's reach: the launch record and
                # the mirror itself can both be deleted by the gate code
                # ship runs, and an adopted handle must still read this
                # launch as mirrored (review td660513, second pass). A
                # stamp that cannot be written fails the launch like the
                # launch record's.
                try:
                    ctx.store.stamp_ref_meta(todo_id, {FACTORY_MIRROR_KEY: True})
                except Exception as exc:
                    raise RuntimeError(
                        "the mirror-mode record could not be persisted"
                    ) from exc
                ship_env = _factory_git_mirror.mirror_ship_env(mirror, ship_env, url)
                # Ship's post-land effects are premature before the origin
                # sync verifies the pushed sha (review finding 4): its
                # land_notify todo/gripe marks and gripe-note are held —
                # the lander posts them itself once the push lands —
                # the shared-dir refs it advanced are repaired on a
                # failed or deferred sync, and the local-main advance is
                # refused outright (second pass: a best-effort rewind
                # cannot cover a crashed worker or a blind GitHub, so the
                # effect may not happen until publication verifies): a
                # PATH-leading git shim turns ship's ``_sync_local_main``
                # shapes down, and the lander replays the fast-forward
                # itself after the sync.
                ship_env["PRECIS_LAND_NOTIFY"] = "0"
                ship_env["PRECIS_GRIPE_NOTE"] = "0"
                shim = _factory_git_mirror.ship_git_shim(mirror)
                if shim is None:
                    raise RuntimeError("the ship git shim could not be written")
                ship_env["PATH"] = (
                    str(shim) + os.pathsep + (ship_env.get("PATH") or os.defpath)
                )
            # A parked relaunch (``carry``) has no submit intent: it mints a run
            # dir of its own, so a ship whose handle was never saved can be
            # found by the host sweep like any other launch's.
            attempt_dir = None
            if carry is not None or _rs._submit_attempt_dir(ctx, todo_id) is not None:
                attempt_dir = _rs._launch_attempt_dir(ctx, todo_id)
                exit_path = attempt_dir / "exit"
            else:
                exit_path.unlink(missing_ok=True)
            log_path = _rs._attempt_log(attempt_dir, log_path)
            # The deadline rides the handle (gr647913): ``started_at`` + the wall
            # — the clock stamps share the ``_now()`` seam so a worker restart's
            # re-adoption resumes the job's own count instead of restarting the
            # wall at the re-adopt poll.
            launched = _now()
            handle: dict[str, Any] = {
                "worktree": str(tree),
                "log": str(log_path),
                "exit_file": str(exit_path),
                "subject": subject,
                "flag": flag,
                "started_at": launched.strftime(_TS_FMT),
                "deadline": launched.timestamp() + wall_seconds(),
            }
            if mirror_tips is not None:
                # The sync's CAS basis (GitHub's tips at launch). Adopted
                # handles rebuild without it; poll falls back to the launch
                # record beside the mirror
                # (:func:`factory_git_mirror.read_launch_tips`).
                handle["factory_mirror_tips"] = mirror_tips
            for key in (
                "main_refusals",
                "last_refusal",
                "gate_kills",
                "release_busy",
                "fell_back_full",
            ):
                if carry is not None and key in carry:
                    handle[key] = carry[key]
            if attempt_dir is not None:
                _rs._write_launch_record(attempt_dir / "baseline", handle)
            pid = _launch(
                [ship_bin(tree), flag, *(("--onto", onto) if onto else ()), subject],
                cwd=tree,
                log=log_path,
                exit_file=exit_path,
                # The slice host's service account has no credential of
                # its own (td543734) — with a factory token resolvable the
                # factory token never reaches this env: ship's git traffic
                # to the real origin URL is rewritten to the lander's
                # mirror, populated and synced under the credential by
                # the lander alone. With no token resolvable the rewrite
                # is absent and the host's own credentials apply, as
                # before. UV_PYTHON pins ship's `uv run` steps to the
                # worker's Python line:
                # with no .venv in a slice tree uv takes the newest
                # interpreter satisfying
                # requires-python — Homebrew 3.15 on the agent host, for which
                # rdkit has no wheel, so the frozen sync fails and
                # gen-devin-hooks' check reads as "stale" (jo551780
                # event 6).
                env={
                    **ship_env,
                    "PRECIS_SHIP_VERBOSE": "1",
                    "UV_PYTHON": ship_python(),
                    # Cap the checked run's xdist workers (gr567471): ship's
                    # checked gate runs `pytest -n ${PRECIS_GATE_N:-6}` itself,
                    # not scripts/test, so both names carry the count.
                    "PRECIS_GATE_N": test_nproc(),
                    "PRECIS_TEST_NPROC": test_nproc(),
                    # Bound the land's share of the shared gate semaphore
                    # (td599058): without it gate-slot.sh derives up to 6
                    # slots and a land wave can take the whole VM.
                    "PRECIS_GATE_SLOTS": land_gate_slots(),
                },
                # The land flock rides into the ship process, so the lock
                # is held for the run's whole life — a second land_slice
                # finds it held even after this worker dies (gr650484).
                # Nothing else rides pass_fds: the credential never does.
                pass_fds=tuple(fd for fd in (ship_fd,) if fd is not None),
                store=ctx.store,
                work_class="land",
                pid_file=attempt_dir / "pid" if attempt_dir is not None else None,
            )
        except admission.Refused as exc:
            # The host cannot fit the gate right now — the wait shape of
            # _land_busy, not the red path: attempt-free, so the tick
            # re-mints the land when capacity returns (review td702053).
            ctx.append_chunk(
                "job_summary",
                f"land_slice td{todo_id}: ship launch deferred — host at "
                f"capacity ({exc}); no ship launched",
            )
            _fail(
                ctx,
                todo_id,
                params.get("lane"),
                f"ship launch deferred: {exc}",
                handoff=(
                    "land DEFERRED (not red): the worker host could not "
                    "admit the gate's footprint just now. The tick "
                    "re-mints the land attempt-free once capacity frees."
                ),
                attempt_free=ADMISSION_RED_CLASS,
                folder=FOLDER_DROP,
            )
            return None
        except Exception as exc:
            # A launch that raised may still have started a ship: the
            # folder is kept for the host sweep, which drops it once none is
            # behind it.
            log.warning("land_slice: launch for td%d failed", todo_id, exc_info=True)
            _fail(
                ctx,
                todo_id,
                params.get("lane"),
                f"ship launch failed: {exc}",
                handoff=f"land FAILED: scripts/ship did not start ({exc}).",
                folder=FOLDER_KEEP,
            )
            return None
        # The ship runs: nothing below may lose its handle.
        try:
            _stamp_lock_holder(
                lock_fd,
                pid,
                todo_id,
                branch,
                subject,
                flag,
                attempt_dir.name if attempt_dir is not None else "",
                str(tree),
            )
        except Exception:
            log.warning(
                "land_slice: lock stamp for td%d failed", todo_id, exc_info=True
            )
        _rs._record_launched_process(handle, pid, attempt_dir)
        return handle
    finally:
        # Our end of the land flock: the launched ship's inherited copy
        # of the dup'd fd keeps it held for the run's life; every
        # pre-launch return (busy, adopted, already-landed, deferred,
        # failed) releases it so the next land is not wedged by a dead
        # worker's lock. ``ship_fd`` must close here too — it shares the
        # flock's open file description, so a launch-time exception
        # leaving it open would pin the lock. No credential fd ever
        # reaches this close set: the token crossed to the mirror's git
        # calls as env wiring inside this worker only, and ship never
        # inherited any of it.
        for fd in {fd for fd in (lock_fd, ship_fd) if fd is not None}:
            try:
                os.close(fd)
            except OSError:
                pass


#: A pre-review launch's tips copy in the TREE's ``scratch/`` — kept as
#: a read-only fallback so a launch still running from before the record
#: moved beside the mirror can still sync. Branch-writable, so it is a
#: tips SOURCE only, never evidence a launch ran mirrored.
MIRROR_TIPS_FILE = "factory-mirror-tips.json"

#: Mirror mode's durable record — ``meta.factory_mirror`` on the todo,
#: stamped at a mirrored launch and cleared when the sync verifies its
#: squash on origin or a token-less launch supersedes it. The launch
#: tips file and the mirror's own directory under the main checkout's
#: ``scratch/`` are same-uid-writable by the gate code ship runs (see
#: :mod:`precis.workers.factory_git_mirror`), so deleting both must
#: never read as "no mirror": the todo's meta is the DB-resident
#: evidence an adopted handle's sync consults (review td660513, second
#: pass).
FACTORY_MIRROR_KEY = "factory_mirror"


def _read_mirror_tips(tree: Path) -> dict[str, str]:
    """The legacy launch-tips copy (:data:`MIRROR_TIPS_FILE`): ``{}``
    when absent or malformed. Consulted only once mirror mode is already
    established by trusted evidence — a planted copy alone never marks
    a launch mirrored."""
    try:
        raw = json.loads(
            (tree / "scratch" / MIRROR_TIPS_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(k, str)
        and isinstance(v, str)
        and re.fullmatch(r"[0-9a-f]{40}", v)
    }


def _mirror_in_play(
    root: Path | None, todo_id: int, handle: dict[str, Any], store: Any
) -> bool:
    """Did this launch run mirrored — ship pushed the lander's mirror,
    not GitHub? The durable evidence is job-visible state gate code
    cannot reach: the handle's tips, or :data:`FACTORY_MIRROR_KEY` on
    the todo's meta — stamped at a mirrored launch, it survives the
    adoption that rebuilds a handle without them. The launch record and
    the mirror's own directory stay as evidence for a launch that
    predates the stamp, but they are same-uid-writable: their absence
    can no longer acquit a launch the durable record convicts (a
    deleted pair once read as "no mirror" and the adopted land recorded
    a mirror-only squash as landed — review td660513, second pass).
    Never the tree's scratch."""
    if isinstance(handle.get("factory_mirror_tips"), dict):
        return True
    if todo_id:
        ref = store.get_ref(kind="todo", id=todo_id)
        if ref is not None and (ref.meta or {}).get(FACTORY_MIRROR_KEY):
            return True
    if root is None or not todo_id:
        return False
    launch_id = f"td{todo_id}"
    return (
        _factory_git_mirror.read_launch_tips(root, launch_id) is not None
        or _factory_git_mirror.mirror_dir(root, launch_id).exists()
    )


def _github_url(root: Path) -> str | None:
    """The real origin URL, read from the MAIN checkout (never a slice
    tree) — the mirror populate's fetch source and the sync's push
    destination."""
    return _factory_git_mirror.github_url(root, env=land_env())


#: Seconds a land defers after the mirror→origin sync loses the launch
#: tip's race (GitHub moved under the land): ship itself would have
#: retried in-lock immediately, so the backoff is minutes-short, not
#: main's 30 min verdict cadence (``PRECIS_LAND_MIRROR_SYNC_BACKOFF_S``
#: overrides).
MIRROR_SYNC_BACKOFF_S = 60


def mirror_sync_backoff_s() -> int:
    """The raced-sync deferral wait (never negative)."""
    try:
        n = int(
            os.environ.get("PRECIS_LAND_MIRROR_SYNC_BACKOFF_S", MIRROR_SYNC_BACKOFF_S)
        )
    except ValueError:
        return MIRROR_SYNC_BACKOFF_S
    return max(n, 0)


def _sync_to_origin(
    ctx: Any,
    handle: dict[str, Any],
    params: dict[str, Any],
    branch: str,
    *,
    expected_sha: str | None = None,
    onto: str | None = None,
) -> bool | None:
    """Ship pushed the mirror, not GitHub: place the refs it moved on the
    real origin before any land record is written. Every changed ref is
    pushed with a ``--force-with-lease`` on the tip GitHub held at launch
    (the ``factory_mirror_tips`` on the handle, else the launch record
    beside the mirror an adopted handle lacks), must equal
    ``expected_sha`` — the sha ship reported — and must descend from its
    launch tip, so a land that lost a race defers — ship re-runs against
    a fresh mirror — a "no land refs moved" mirror while ship claimed a
    sha is refused rather than recorded (review finding 3), and a land
    whose sync cannot reach GitHub defers too: the records may never
    name a sha origin does not hold. ``None`` = synced (or no mirror in
    play — no token at launch); ``False`` = deferred (the handle is
    parked, the meta set); ``True`` = the job is terminal (blocked past
    the cap, or failed)."""
    todo_id = int(params.get("todo_id") or 0)
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    try:
        root: Path | None = _rs.slice_root()
    except Exception:
        root = None
    launch_id = f"td{todo_id}"
    raw_tips = handle.get("factory_mirror_tips")
    record = (
        _factory_git_mirror.read_launch_tips(root, launch_id)
        if root is not None
        else None
    )
    tips = raw_tips if isinstance(raw_tips, dict) and raw_tips else (record or {})
    if not tips:
        # No CAS basis. Either the launch was never mirrored — no token
        # resolved at launch, so ship pushed origin itself on the host's
        # credentials, as before — or its record is gone (a reap, a
        # deleted file). Mirror mode is judged on state the gate code
        # ship runs cannot touch (review td660513): the handle's tips,
        # FACTORY_MIRROR_KEY on the todo's meta — a green ship that ran
        # mirrored without a readable basis is refused, never recorded —
        # its squash lives only in the lander's mirror.
        if not _mirror_in_play(root, todo_id, handle, ctx.store):
            return None
        # A pre-review launch can still carry its basis in the tree's
        # scratch; a readable legacy copy recovers the sync, and an
        # absent or forged one is refused by the lease or the descent
        # check anyway.
        tips = _read_mirror_tips(tree)
        if not tips:
            ctx.record_failure(
                f"land_slice: ship of td{todo_id} exited green but the "
                "mirror launch record is unreadable; refusing to record "
                "a land that may not be on origin"
            )
            return True
    outcome, detail = "error", "mirror state unreadable"
    mirror: Path | None = None
    url: str | None = None
    try:
        if root is None:
            raise RuntimeError("the factory checkout root is unreadable")
        mirror = _factory_git_mirror.mirror_dir(root, launch_id)
        url = _github_url(root)
        if url is None:
            raise RuntimeError("the factory checkout names no trusted origin url")
        # What ship claimed: the land target's pushed sha. A ref the
        # mirror saw moved without a claim — or a claimed land whose ref
        # never moved — is a refusal, not a record.
        expect = {onto or "main": expected_sha} if expected_sha else {}
        outcome, detail = _factory_git_mirror.sync(
            mirror,
            _factory_git_mirror.cache_dir(root),
            url,
            tips,
            expect=expect,
            env=_factory_git.git_auth_env(base=land_env()),
        )
    except Exception as exc:
        outcome, detail = "error", f"{type(exc).__name__}: {exc}"
    if outcome == "ok":
        ctx.append_chunk(
            "job_event", f"land_slice: factory mirror synced to origin ({detail})"
        )
        # The deferred local-main advance (review finding 4, second
        # pass): ship's own ``_sync_local_main`` was refused by the
        # launch's git shim; the lander replays it now that the push
        # verified, under the credential-free local env. Best-effort,
        # like ship's — never a land verdict.
        for note in _advance_local_main(tree, onto, expected_sha, tips):
            ctx.append_chunk("job_event", f"land_slice: {note}")
        if root is not None:
            # Verified on origin — the per-launch mirror and its launch
            # record are done (a relaunch repopulates both if one comes).
            _factory_git_mirror.drop_launch(root, launch_id)
        try:
            # And its durable half: the mirror-mode stamp on the todo.
            ctx.store.stamp_ref_meta(todo_id, {FACTORY_MIRROR_KEY: None})
        except Exception:
            pass
        return None
    # Ship's post-land state already fanned out: the common git dir's
    # ``refs/remotes/origin/*`` show the mirror's refs and the primary's
    # local main may sit on the unpushed squash (review finding 4).
    # Repair both to what GitHub actually holds before the park.
    for note in _repair_origin_state(
        ctx,
        tree,
        url,
        tips,
        phantom_main=expected_sha if onto is None else None,
    ):
        ctx.append_chunk("job_event", f"land_slice: {note}")
    cross = (
        f"✖ factory mirror sync to origin refused: {detail}"
        if outcome == "raced"
        else f"✖ factory mirror sync to origin failed: {detail}"
    )
    refusals = int(handle.get("main_refusals") or 0) + 1
    if refusals <= MAX_MAIN_REFUSALS:
        ctx.append_chunk(
            "job_event",
            "land_slice: ship pushed the mirror but the origin sync did not "
            f"stick ({detail}) — deferring; ship re-runs on a fresh mirror",
        )
        return _defer_to_main(
            ctx, handle, cross, refusals, backoff_s=mirror_sync_backoff_s()
        )
    reason, handoff = _blocked_handoff(branch, refusals, cross, "main")
    ctx.append_chunk("job_summary", f"land_slice td{todo_id}: RED — {cross}")
    _fail(ctx, todo_id, params.get("lane"), reason, handoff=handoff)
    return True


def _repair_origin_state(
    ctx: Any,
    tree: Path,
    url: str | None,
    tips: dict[str, str],
    *,
    phantom_main: str | None,
) -> list[str]:
    """Rollback notes for ship's post-land shared state after a sync that
    did NOT place it on origin (review td660513 finding 4): a linked
    worktree shares the main checkout's common git dir, so ship's pushes
    moved ``refs/remotes/origin/*`` for every worktree — and a ship that
    slipped its git shim (an adopted launch from before the shim, or a
    branch that called real git itself) may still have fast-forwarded
    the primary's ``main`` to a squash only the mirror holds.
    :func:`factory_git_mirror.repair_origin` resets the tracking refs to
    GitHub's real tips and rewinds the phantom main under the same
    trusted boundary as the sync. A land's clone (gr741695) has a git dir
    of its own (``<clone>/.git``): the repair runs there, which is what
    the relaunch in the same clone reads — nothing outside it was ever
    moved. Best-effort; never raises."""
    if url is None:
        return [
            "origin repair: no trusted origin url — shared refs left for the relaunch"
        ]
    try:
        gitdir = _worktree_gitdir(tree)
        common = _git_common_dir(gitdir, tree) if gitdir else None
    except Exception:
        gitdir = common = None
    if gitdir is None:
        return ["origin repair: could not resolve the shared git dir"]
    # A primary checkout's own gitdir IS the shared dir — the repair's
    # `git -C` calls resolve their common dir either way.
    common = common or gitdir
    try:
        return _factory_git_mirror.repair_origin(
            common,
            url,
            tuple(tips),
            phantom_main=phantom_main,
            env=_factory_git.git_auth_env(base=land_env()),
        )
    except Exception as exc:
        return [f"origin repair raised ({type(exc).__name__}: {exc})"]


def _advance_local_main(
    tree: Path,
    onto: str | None,
    expected_sha: str | None,
    tips: dict[str, str],
) -> list[str]:
    """Replay ship's ``_sync_local_main`` now that the mirror→origin
    sync verified (review td660513 finding 4, second pass): the launch's
    git shim refused ship's own advance — the shared main would have
    sat on a commit only the mirror holds — so the lander does it here,
    on the verified sha (the launch tip when ship squashed nothing), in
    the shared git dir and under a credential-free env. Main lands
    only: a release fix's ship never touched main. A land's clone
    (gr741695) shares no main with the host checkout and is dropped right
    after the land, so the advance is skipped there: moving the clone's own
    ``main`` would be a note and nothing else. (The host checkout's local
    ``main`` and ``origin/*`` no longer move with a land; the hub's copy
    of the base is what the next build and land start from.)
    Best-effort notes; never raises."""
    if onto is not None:
        return []
    if _land_clone(tree):
        return ["local main advance: skipped (the land's clone shares no main)"]
    target = expected_sha or tips.get("main")
    if not target or not re.fullmatch(r"[0-9a-f]{40}", target):
        return ["local main advance: no verified sha — left alone"]
    try:
        gitdir = _worktree_gitdir(tree)
        common = _git_common_dir(gitdir, tree) if gitdir else None
    except Exception:
        gitdir = common = None
    if gitdir is None:
        return ["local main advance: could not resolve the shared git dir"]
    # A primary checkout's own gitdir IS the shared dir.
    common = common or gitdir
    try:
        return [_factory_git_mirror.advance_local_main(common, target, env=land_env())]
    except Exception as exc:
        return [f"local main advance raised ({type(exc).__name__}: {exc})"]


#: Seconds the drift preflight may take before it counts as unanswered.
DRIFT_PROBE_TIMEOUT_S = 45

#: The cross line the lander writes when its own identity cannot read main's
#: gate; :data:`MAIN_REFUSAL_MARKS` matches it, so it defers like ship's own.
UNKNOWN_GATE_CROSS = (
    "✖ main has no all-green shard matrix the lander's identity can read "
    "(drift guard unknown: blind token or GitHub unanswerable; fail-closed, "
    "gr550941)"
)


def ship_python() -> str:
    """The Python line ship's ``uv run`` steps pin to (``UV_PYTHON``): the
    worker's own ``major.minor`` (a version, not the venv's interpreter path,
    so uv picks a matching base interpreter rather than a venv)."""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _drift_preflight(
    tree: Path, *, mirror: Path | None = None, main_tip: str | None = None
) -> dict[str, Any]:
    """What the lander's own GitHub identity sees of main's gate, before ship
    runs: ``scripts/last-gated-main-sha --age-hours --diag`` in ``tree`` under
    the factory token. ``{"ran": bool, "rc": int | None, "age": str, "diag":
    str}`` — ``age`` is the helper's stdout (hours, or empty for unknowable),
    ``diag`` its one stderr line with the exit code appended.

    gr549328/gr550941: ship refused the land on the agent host while the same main
    read green elsewhere. Ship treats an unknowable lookup as "do not refuse"
    (a GitHub outage must not stop the fleet); an *unattended* land must not
    ride that — :func:`_gate_unknown` parks it instead. ``ran`` is False when
    no helper could be read (then ship's own guard is all there is).

    Review td660513 (finding 1): under the token the helper is ``git show``n
    from the launch's *verified* mirror main tip
    (:func:`_mirror_preflight_helper`) — never from the branch's tree, whose
    helper (or a ``scratch/lib`` shadow of a stdlib import) would run branch
    code over the credential — and the interpreter starts isolated
    (``python -I``: no cwd on ``sys.path``, no user site). A live token with
    no verified helper source is a refusal, not a fall back to whatever the
    branch placed."""
    out: dict[str, Any] = {"ran": False, "rc": None, "age": "", "diag": ""}
    if mirror is not None:
        helper = _mirror_preflight_helper(tree, mirror, main_tip)
        if helper is None:
            out.update(
                ran=True,
                diag=(
                    "the verified mirror's main tip carried no readable "
                    "drift helper; the branch's own copy cannot run under "
                    "the token (refusing, not blind)"
                ),
            )
            return out
    elif _factory_git.factory_git_token():
        # The token resolves but no verified mirror source was supplied —
        # a branch-shaped helper under this env is the finding-1 hole.
        out.update(
            ran=True,
            diag=(
                "the token resolves but no verified mirror source was "
                "supplied; refusing to run a helper the branch can shape"
            ),
        )
        return out
    else:
        helper = _preflight_helper(tree)
        if helper is None:
            return out
    try:
        cp = subprocess.run(
            [
                # The worker's own interpreter, not the helper's `uv run`
                # shebang: on the agent host the deploy user's uv cache is
                # root-owned and uv exits 2 before the helper starts, which
                # ship's guard reads as "none green" (gr551238). The helper
                # is stdlib-only. ``-I`` isolates it: no script dir or cwd
                # on sys.path, no user site — nothing in the slice tree
                # decides its imports.
                sys.executable,
                "-I",
                str(helper),
                "--age-hours",
                "--start",
                "origin/main",
                "--hours",
                "48",
                "--diag",
            ],
            cwd=tree,
            # The helper's `gh api` read is the lander's own GitHub call,
            # so the credential wiring — on the allowlist env, not the
            # worker's whole env (gr659929). The allowlist's
            # GIT_NO_REPLACE_OBJECTS also blinds planted refs/replace
            # for the helper's own `git rev-list` read of the tree.
            env=_factory_git.git_auth_env(base=land_env()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=DRIFT_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        out.update(ran=True, diag=f"helper did not answer ({type(exc).__name__})")
        return out
    lines = [
        ln for ln in (cp.stderr or "").splitlines() if "last-gated-main-sha:" in ln
    ]
    if lines:
        diag = lines[-1].strip()[:400]
    else:
        # The helper never reached its diag: keep what the process said, so
        # an interpreter or shebang failure is not mistaken for a verdict.
        tail = " | ".join((cp.stderr or "").strip().splitlines()[-3:])[-300:]
        diag = f"(no diag line; stderr: {tail or '-'})"
    out.update(
        ran=True,
        rc=cp.returncode,
        age=(cp.stdout or "").strip(),
        diag=f"{diag} (exit {cp.returncode})",
    )
    return out


#: Where the preflight materialises main's copy of the drift helper. Under
#: ``scratch/`` so the helper's ``ROOT`` (two levels up) is still the tree.
#: Since gr550044 the logic lives in ``scripts/lib/last_gated_main_sha.py``
#: (the dotless name is a shim that execs it), so the lib path is tried
#: first — a materialised shim alone could not find its module. The lib
#: copy lands at ``scratch/lib/`` so its ``parents[2]`` is still the tree.
PREFLIGHT_HELPER_REL = Path("scratch") / "last-gated-main-sha"
PREFLIGHT_LIB_REL = Path("scratch") / "lib" / "last_gated_main_sha.py"


def _mirror_preflight_helper(
    tree: Path, mirror: Path, main_tip: str | None
) -> Path | None:
    """The drift helper at the launch's *verified* main tip, ``git show``n
    from the per-launch mirror — the populate oracle compared this sha to
    what GitHub served, so the content is main's and never anything the
    branch shaped (review td660513 finding 1: the tree's own helper, or a
    ``scratch/lib`` import shadow, ran branch code over the token before
    this). Materialised into the tree's ``scratch/`` as before so the
    helper's ``parents[2]`` ``ROOT`` still resolves the tree. ``None``
    when the tip names neither the lib module nor the dotless shim.

    The read rides :data:`~precis.workers.factory_git_mirror.TRUSTED_ARGS`
    — ``--no-replace-objects`` in particular (review td660513, pre-land
    pass): the mirror is same-uid writable, so a ``refs/replace`` planted
    for the verified tip would otherwise let this ``git show`` hand the
    probe attacker-authored code to run under the token anyway."""
    if not main_tip or not re.fullmatch(r"[0-9a-f]{40}", main_tip):
        return None
    env = _factory_git_mirror.trusted_env(land_env())
    for src, rel in (
        ("scripts/lib/last_gated_main_sha.py", PREFLIGHT_LIB_REL),
        ("scripts/last-gated-main-sha", PREFLIGHT_HELPER_REL),
    ):
        cp = _factory_git_mirror._run(
            [
                "git",
                *_factory_git_mirror.TRUSTED_ARGS,
                "-C",
                str(mirror),
                "show",
                f"{main_tip}:{src}",
            ],
            cwd=mirror,
            env=env,
            timeout=30.0,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            target = tree / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(cp.stdout, encoding="utf-8")
                target.chmod(0o755)
                return target
            except OSError:
                log.warning("land_slice: could not write %s", target, exc_info=True)
    return None


def _preflight_helper(tree: Path) -> Path | None:
    """The drift helper the preflight runs: ``origin/main``'s copy, written to
    ``scratch/`` in ``tree`` after a fetch, else the tree's own helper, else
    ``None``.

    The TOKEN-FREE path only — :func:`_drift_preflight` calls this only
    when no factory token resolves, so nothing the branch shaped meets a
    credential; with a mirror in play the verified
    :func:`_mirror_preflight_helper` supplies main's copy instead.

    A slice branch forks from an older main and ship only merges main in
    *after* the guard; the branch's helper can predate the rule and the flag
    the preflight relies on (jo550940 event 6: "(no diag line) (exit 2)" from
    a copy without ``--diag``). Main's copy is the one ship itself will run:
    the lib module when main has it, else the self-contained dotless script
    an older main still carries."""
    _git(["fetch", "--quiet", "origin", "main"], tree)
    for src, rel in (
        ("scripts/lib/last_gated_main_sha.py", PREFLIGHT_LIB_REL),
        ("scripts/last-gated-main-sha", PREFLIGHT_HELPER_REL),
    ):
        cp = _git(["show", f"origin/main:{src}"], tree)
        if cp.returncode == 0 and cp.stdout.strip():
            target = tree / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(cp.stdout, encoding="utf-8")
                target.chmod(0o755)
                return target
            except OSError:
                log.warning("land_slice: could not write %s", target, exc_info=True)
    for own in (
        tree / "scripts" / "lib" / "last_gated_main_sha.py",
        tree / "scripts" / "last-gated-main-sha",
    ):
        if own.is_file():
            return own
    return None


def _gate_unknown(pre: dict[str, Any]) -> bool:
    """True when the preflight ran and could not say how old main's verdict
    is: exit 0 with empty output (blind identity, GitHub unanswerable), no
    answer at all, or a process that died before the helper's diag line
    (whatever its exit code — a crashed interpreter is not a verdict). Exit 2
    *from the helper* (looked, none green) is ship's refusal to make."""
    if not pre.get("ran"):
        return False
    if str(pre.get("diag") or "").startswith("(no diag line"):
        return True
    return pre.get("rc") != 2 and not pre.get("age")


def _blocked_handoff(
    branch: str, refusals: int, cross: str, side: str = "main"
) -> tuple[str, str]:
    """``(reason, handoff)`` for a land parked past its deferral cap.
    ``side`` is :func:`refusal_side`, or ``"gate"`` for a land that kept
    being gate-killed: the handoff names what has to change."""
    if side == "host":
        return (
            f"land blocked by the lander's host for {refusals} refusals: {cross}",
            f"land BLOCKED on {branch}: the lander's host refused the land "
            f"{refusals} times ({cross}). Nothing is wrong on the branch; the "
            "node needs the ops action the refusal names (an operator, not a "
            "builder). Do not rebuild.",
        )
    if side == "gate":
        # Exit 137 is ALSO the container's own mem_limit (24 GiB,
        # docker/dev/compose.yaml) — a branch's memory hog can hit it, so
        # the handoff does not clear the branch ("Do not rebuild" would
        # be wrong for that half).
        return (
            f"land blocked by gate kills for {refusals} retries: {cross}",
            f"land BLOCKED on {branch}: the gate was killed {refusals} "
            f"times ({cross}). The cause is the docker VM's memory or the "
            "branch's own memory use — a memory hog on the branch is the "
            "builder's to fix; otherwise an operator frees the docker VM.",
        )
    return (
        f"land blocked by main for {refusals} refusals: {cross}",
        f"land BLOCKED on {branch}: main refused the land {refusals} times "
        f"({cross}). Nothing is wrong on the branch; it needs a /go (full "
        "gate) over main or a fixed red on main. Do not rebuild.",
    )


def _defer_to_main(
    ctx: Any,
    handle: dict[str, Any],
    cross: str,
    refusals: int,
    *,
    probe: str | None = None,
    side: str = "main",
    backoff_s: int | None = None,
    counter: str = "main_refusals",
) -> bool:
    """A main-side (or host-side, ``side="host"``) refusal is a deferral, not
    a red: count it in ``counter`` (``gate_kills`` for a gate kill — never
    ``main_refusals``, which is only about main's verdict), note it on the
    job and park the handle until ``not_before`` (now +
    :func:`refusal_backoff_s`, or ``backoff_s`` when given — the shorter
    gate-kill wait); :func:`_relaunch_when_due` runs ship again then. The
    dead ship's pid, identity and exit file are dropped so no poll re-reads
    its exit. The handle is mutated in place and persisted to
    ``meta.compute_handle`` (poll cannot hand one back). Always ``False``:
    the job stays running.

    A main-side event carries the lander identity's drift diag (``probe``,
    else a fresh :func:`_drift_preflight`) so a blind token and a red window
    read differently (gr550941); a host-side one has nothing to probe."""
    delay = refusal_backoff_s() if backoff_s is None else backoff_s
    due = _now() + timedelta(seconds=delay)
    for key in (
        "pid",
        "pgid",
        "boot_id",
        "start_time",
        "ps_identity",
        "gate_wait",
        "polled_at",
    ):
        handle.pop(key, None)
    Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
    handle.update(last_refusal=cross, not_before=due.strftime(_TS_FMT))
    handle[counter] = refusals
    if probe is None and side == "main":
        probe = _defer_probe(ctx, handle)
    ctx.append_chunk(
        "job_event",
        f"land_slice: deferred ({side}-side refusal #{refusals}): {cross} — "
        f"scripts/ship again after {due:%H:%M}Z"
        + (f"\ndrift probe (lander identity): {probe}" if probe else ""),
    )
    # Parked time must not eat the ssh_node wall deadline: it covers the
    # wait plus one fresh ship run (the 1 h wall), and rides the handle
    # the same way (gr647913).
    deadline = due.timestamp() + wall_seconds()
    handle["deadline"] = deadline
    ctx.set_meta(
        compute_handle=handle, compute_handle_released=False, deadline=deadline
    )
    return False


def _defer_probe(ctx: Any, handle: dict[str, Any]) -> str:
    """The drift diag a deferred main-side event carries: a fresh
    :func:`_drift_preflight` against this launch's mirror when the handle
    knows one — the verified-helper path, never a branch helper under
    the token."""
    tree = Path(str(handle.get("worktree") or ""))
    raw = handle.get("factory_mirror_tips")
    tips = raw if isinstance(raw, dict) and raw else {}
    mirror = None
    try:
        params = (ctx.meta or {}).get("params") or {}
        todo_id = int(params.get("todo_id") or 0)
        if todo_id:
            root = _rs.slice_root()
            launch_id = f"td{todo_id}"
            if not tips:
                # The adopted handle's fallback: the launch record
                # beside the mirror, not the branch-writable tree.
                tips = _factory_git_mirror.read_launch_tips(root, launch_id) or {}
            if tips:
                mirror = _factory_git_mirror.mirror_dir(root, launch_id)
    except Exception:
        mirror = None
    return _drift_preflight(tree, mirror=mirror, main_tip=tips.get("main"))["diag"]


def _relaunch_when_due(ctx: Any, handle: dict[str, Any]) -> bool:
    """Poll half of a parked deferral: ``False`` before ``not_before``; at or
    after it, drop the marker and launch ship again (same worktree, flag and
    subject, counters carried), persisting the new handle in place. ``True``
    only when the launch failed (the job is then already failed)."""
    try:
        due = datetime.strptime(str(handle["not_before"]), _TS_FMT).replace(tzinfo=UTC)
    except (KeyError, ValueError):
        due = _now()  # unreadable marker: do not park forever
    if _now() < due:
        return False
    params = (ctx.meta or {}).get("params") or {}
    handle.pop("not_before", None)
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    flag = str(handle.get("flag") or _planned_flag(ctx.meta, params))
    fresh = _launch_ship(
        ctx, tree, flag, str(handle.get("subject") or ""), carry=handle
    )
    if fresh is None:
        return True
    handle.clear()
    handle.update(fresh)
    if fresh.get("not_before"):
        return False  # parked again by the preflight; its deferral set the meta
    ctx.set_meta(
        compute_handle=fresh,
        compute_handle_released=False,
        deadline=fresh["deadline"],
    )
    return False


def _launch_verify(
    slice_tree: Path,
    ids: list[str],
    *,
    phase: str,
    run_tree: Path,
    argv: list[str] | None = None,
    store: Any = None,
) -> dict[str, Any] | None:
    """Detached ``scripts/test <ids>`` in ``run_tree``, the log and exit
    file under the SLICE tree's ``scratch/`` so they outlive a probe
    worktree. ``argv`` replaces the bare id list (the ``deselect``
    phase's ``--selected --deselect <id>…``) while ``ids`` keeps the
    failing ids the phase is about. ``None`` when the launch itself
    failed — the caller falls back to the ordinary red.
    :class:`admission.Refused` propagates: the host could not fit the
    probe's footprint, which defers the land attempt-free at the caller,
    never charging the branch for the host's capacity (review td702053)."""
    scratch = slice_tree / "scratch"
    log_path = scratch / f"land_verify_{phase}.log"
    exit_path = scratch / f"land_verify_{phase}.exit"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        exit_path.unlink(missing_ok=True)
        pid = _launch(
            [str(run_tree / "scripts" / "test"), *(argv if argv is not None else ids)],
            cwd=run_tree,
            log=log_path,
            exit_file=exit_path,
            # A gate run of the branch's tests on the host: the scrubbed
            # env, and deliberately no credential — scripts/test makes no
            # fetch, push or `gh` call (gr659929).
            env={
                **land_env(),
                # a land clone's gates queue on the host's slots, not its own
                **_host_state_env(run_tree),
                "UV_PYTHON": ship_python(),
                "PRECIS_TEST_NPROC": test_nproc(),
                "PRECIS_GATE_SLOTS": land_gate_slots(),
            },
            store=store,
            work_class="verify",
        )
    except admission.Refused:
        # The host is full, not the branch red: the caller defers
        # attempt-free (review td702053).
        raise
    except Exception as exc:
        log.warning(
            "land_slice: verify launch (%s) failed: %s", phase, exc, exc_info=True
        )
        return None
    launched = _now()
    verify: dict[str, Any] = {
        "phase": phase,
        "ids": list(ids),
        "pid": pid,
        "pgid": pid,
        "log": str(log_path),
        "exit_file": str(exit_path),
        "run_tree": str(run_tree),
        "started_at": launched.strftime(_TS_FMT),
        # The phase's own deadline rides the handle the same way
        # (gr647913) — its wall counts from its own launch.
        "deadline": launched.timestamp() + wall_seconds(),
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        verify["boot_id"], verify["start_time"] = identity
    ps_identity = (
        _rs._darwin_process_identity(pid) if sys.platform == "darwin" else None
    )
    if ps_identity is not None:
        verify["ps_identity"] = ps_identity
    return verify


def _defer_admission_refused(
    ctx: Any, handle: dict[str, Any], what: str, exc: admission.Refused
) -> bool:
    """A verify launch refused for capacity — the wait shape of the ship
    launch's refusal (:func:`_launch_ship`), not the red path: the job
    fails attempt-free (``red_class = "admission"``) so the tick re-mints
    the land when the host frees, and the branch is never charged for the
    host's moment (review td702053)."""
    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    handle.pop("verify", None)
    ctx.append_chunk(
        "job_summary",
        f"land_slice td{todo_id}: {what} launch deferred — host at capacity ({exc})",
    )
    _fail(
        ctx,
        todo_id,
        params.get("lane"),
        f"{what} launch deferred: {exc}",
        handoff=(
            "land DEFERRED (not red): the worker host could not admit the "
            f"{what} probe's footprint just now. The tick re-mints the land "
            "attempt-free once capacity frees."
        ),
        attempt_free=ADMISSION_RED_CLASS,
    )
    return True


def _promote_verify(handle: dict[str, Any], verify: dict[str, Any]) -> None:
    """Make the running probe the handle's live process — ``pid``,
    ``pgid``, ``boot_id``, ``start_time`` and ``ps_identity`` copied to the
    TOP level, the same slot the ship relaunch fills. The executor's
    cancel, wall-clock and orphan-reap gates all validate the top-level
    identity before calling this job's ``kill``; while it still names the
    dead ship every gate fails closed and the kill hook never runs,
    leaking the probe and the ``slice-main-td<id>`` worktree."""
    handle["verify"] = verify
    for key in ("pid", "pgid", "boot_id", "start_time", "ps_identity"):
        if key in verify:
            handle[key] = verify[key]
        else:
            handle.pop(key, None)


def _ordinary_ship_red(ctx: Any, handle: dict[str, Any]) -> bool:
    """Back to the ship red's ordinary path once a verify phase is spent —
    or could not probe: ``verify_done`` keeps the outside-diff launch from
    refiring, so the branch is charged as today."""
    handle.pop("verify", None)
    handle["verify_done"] = True
    return poll(ctx, handle)


def _probe_red(v: dict[str, Any], ids: list[str]) -> bool:
    """True only when the probe's own log shows the SAME ids failing —
    a plain test failure is evidence. 'no tests collected', 'not found',
    a usage error, a subset/superset of the probed ids, or any other
    non-zero exit without matching summary lines is a probe failure,
    never evidence: the caller takes the ordinary red."""
    failed = set(failed_test_ids(_rs._read_log(v)))
    return bool(failed) and failed == set(ids)


def _start_main_verify(ctx: Any, handle: dict[str, Any], v: dict[str, Any]) -> bool:
    """Phase ``main`` of the outside-diff verify: cut ``slice-main-td<id>``
    — a detached ``origin/main`` worktree under the worktrees dir
    (``slice_root()/.claude/worktrees``, where the attempt clones live:
    :func:`_slice_ws.attempt_dir`), so its
    compose project carries the slice prefix and the stack sweep reaps
    it — and run the ids there. The worktree is dropped on every later
    exit path (:func:`_drop_main_verify`), and on a launch refused for
    capacity too (the caller defers attempt-free; review td702053).
    ``False`` → the ordinary
    red."""
    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    if todo_id <= 0:
        ctx.append_chunk(
            "job_event",
            f"land_slice: main probe skipped (no positive todo id: {todo_id})",
        )
        return False
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        ctx.append_chunk("job_event", f"land_slice: main probe skipped ({exc})")
        return False
    main_tree = _rs.worktrees_dir(root) / f"slice-main-td{todo_id}"
    project = _slice_stacks.project_for_tree(main_tree)
    # What this job is about to make goes on the job meta BEFORE the add:
    # a kill (or a crash) after this point must be able to remove it.
    ctx.set_meta(main_probe={"tree": str(main_tree), "stack": project})
    if not _clear_stale_main_probe(ctx, root, main_tree, project, todo_id):
        return False
    # Lands run in their own clones, so nothing fetches in the host checkout
    # any more: its ``origin/main`` is refreshed here from the hub. The hub's
    # main dates from this land's submit (a PR land never synced it), so it
    # is synced from GitHub first — the forced fetch below must not move the
    # checkout's ``origin/main`` backwards. When the sync cannot say (no
    # trusted url, GitHub unreadable) the fetch is skipped and the probe
    # tests the tip the checkout last had; the job event says so.
    synced: str | None = None
    try:
        main_url = _github_url(root)
        if main_url is not None:
            synced = _slice_ws.sync_hub(root, url=main_url, base="main")
    except Exception:
        log.warning("land_slice: hub sync before the main probe failed", exc_info=True)
    if synced is None:
        ctx.append_chunk(
            "job_event",
            "land_slice: main probe: the hub could not be synced from GitHub — "
            "probing the tip the host checkout's origin/main already has",
        )
    else:
        _git(
            [
                "fetch",
                "--quiet",
                "--no-tags",
                str(_slice_ws.hub_path(root)),
                "+refs/heads/main:refs/remotes/origin/main",
            ],
            root,
        )
    add = _git(["worktree", "add", "--detach", str(main_tree), "origin/main"], root)
    if add.returncode != 0:
        ctx.append_chunk(
            "job_event",
            "land_slice: main probe worktree add failed: "
            f"{(add.stderr or add.stdout).strip()[:200]}",
        )
        ctx.set_meta(main_probe=None)
        return False
    try:
        verify = _launch_verify(
            tree,
            [str(i) for i in (v.get("ids") or [])],
            phase="main",
            run_tree=main_tree,
            store=ctx.store,
        )
    except admission.Refused:
        # Capacity, not evidence: drop the throwaway tree and let the
        # caller defer attempt-free (review td702053).
        _git(["worktree", "remove", "--force", str(main_tree)], root)
        ctx.set_meta(main_probe=None)
        raise
    if verify is None:
        _git(["worktree", "remove", "--force", str(main_tree)], root)
        ctx.set_meta(main_probe=None)
        return False
    verify["main_tree"] = str(main_tree)
    _promote_verify(handle, verify)
    ctx.append_chunk(
        "job_event",
        "land_slice: ids still red standalone — probing "
        f"{', '.join(verify['ids'])} on origin/main ({main_tree.name})",
    )
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=verify["deadline"],
    )
    return True


def _clear_stale_main_probe(
    ctx: Any, root: Path, main_tree: Path, project: str, todo_id: int
) -> bool:
    """What ``_start_main_verify`` clears before its add, when a crashed
    run left ``slice-main-td<id>`` behind: ``worktree prune`` drops
    registrations whose dirs vanished; if the path is still a REGISTERED
    worktree it comes down through git; if it is an unregistered leftover
    directory it is deleted — only that exact validated path, never
    anything else. ``False`` → the ordinary red."""
    _git(["worktree", "prune"], root)
    listed = _git(["worktree", "list", "--porcelain"], root)
    registered = set()
    if listed.returncode == 0:
        registered = {
            ln.split(None, 1)[1].strip()
            for ln in (listed.stdout or "").splitlines()
            if ln.startswith("worktree ")
        }
    if str(main_tree) in registered or os.path.realpath(main_tree) in registered:
        if project.startswith(_slice_stacks.SLICE_PREFIX):
            _evict_stack(project, volumes=True)
        stale = _git(["worktree", "remove", "--force", str(main_tree)], root)
        if stale.returncode != 0:
            ctx.append_chunk(
                "job_event",
                "land_slice: stale main probe worktree remove failed: "
                f"{(stale.stderr or stale.stdout).strip()[:200]}",
            )
            return False
        ctx.append_chunk(
            "job_event",
            f"land_slice: removed stale main probe worktree {main_tree.name}",
        )
        return True
    if not main_tree.exists():
        return True
    # An unregistered leftover directory — debris git no longer knows
    # about. Deleted only when it is exactly the expected probe path.
    expected = _rs.worktrees_dir(root) / f"slice-main-td{todo_id}"
    if main_tree != expected:
        ctx.append_chunk(
            "job_event",
            f"land_slice: refusing to delete {main_tree} (not {expected})",
        )
        return False
    if project.startswith(_slice_stacks.SLICE_PREFIX):
        _evict_stack(project, volumes=True)
    try:
        shutil.rmtree(main_tree)
    except OSError as exc:
        ctx.append_chunk(
            "job_event",
            f"land_slice: stale main probe leftover remove failed: {exc}",
        )
        return False
    ctx.append_chunk(
        "job_event",
        f"land_slice: deleted stale main probe leftover {main_tree.name}",
    )
    return True


def _drop_main_verify(v: dict[str, Any]) -> list[str]:
    """Remove the ``slice-main-td<id>`` worktree and its compose stack —
    on EVERY exit path of phase ``main``. Notes, never raises."""
    notes: list[str] = []
    raw = str(v.get("main_tree") or "")
    if not raw:
        return notes
    main_tree = Path(raw)
    project = _slice_stacks.project_for_tree(main_tree)
    if project.startswith(_slice_stacks.SLICE_PREFIX):
        notes.append(
            f"main probe stack {project}: "
            + ("down" if _evict_stack(project, volumes=True) else "down FAILED")
        )
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        notes.append(f"main probe worktree remove skipped ({exc})")
        return notes
    rm = _git(["worktree", "remove", "--force", str(main_tree)], root)
    if rm.returncode == 0:
        notes.append(f"main probe worktree {main_tree.name}: removed")
    else:
        notes.append(
            "main probe worktree remove FAILED "
            f"({(rm.stderr or rm.stdout).strip()[:200]})"
        )
    return notes


def _poll_verify(ctx: Any, handle: dict[str, Any]) -> bool:
    """Detached verify half (gr573347 b): a ``checked selection is RED``
    whose failing ids are ALL outside the diff is re-run before the branch
    is charged — ``alone`` in the slice tree, then ``main`` on a
    throwaway ``origin/main`` worktree:

    * alone green → flaky or order-dependent: attempt-free
      (``red_class = "flaky outside the diff"``), the re-land tick takes
      it from there;
    * alone red + main red → not the branch's: the land gates on the
      checked selection MINUS the red-on-main ids (the ``deselect``
      phase — ``scripts/test --selected --deselect <id>…``, engine or
      not — skipped entirely when the selection IS the suite: the ids
      are then recorded as excluded instead, td602151) and relaunches
      ``--quick`` — a ``handoff``-style note names the ids on the todo;
    * alone red + main green → the branch's red after all: the ordinary
      red, attempt burned;
    * any launch or probe failure → the ordinary red — except a launch
      refused for capacity, which defers the land attempt-free (the
      host's moment, not the branch's; review td702053)."""
    v = handle["verify"]
    exit_code = _rs._read_exit(v)
    if exit_code is None and _process_alive(int(v.get("pid") or 0)):
        # The ship rule applies to a live probe too (td599058): every
        # phase runs ``scripts/test``, which queues inside
        # ``gate_slot_acquire`` — and the deselect phase IS a gate — so
        # a queued probe waits on the host, not on gate time. Fold the
        # new wait time into the phase's deadline — it rides the handle
        # (gr647913), counted from the probe's own launch — and
        # heartbeat the position.
        now_ts = _now().timestamp()
        seen = _phase_seen(v, now_ts)
        waiting = gate_slot_waiting(_rs._read_log(v))
        if waiting is not None:
            if v.get("gate_wait") != waiting:
                v["gate_wait"] = waiting
                ctx.append_chunk("job_event", f"land_slice: {waiting}")
            ctx.set_meta(
                compute_handle=handle,
                compute_handle_released=False,
                deadline=_wait_deadline(v, seen, now_ts),
            )
        else:
            v.pop("gate_wait", None)
            ctx.set_meta(compute_handle=handle, compute_handle_released=False)
        return False
    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    ids = [str(i) for i in (v.get("ids") or [])]
    phase = str(v.get("phase") or "")
    if phase == "alone":
        if exit_code == 0:
            number = _pr_param(params)
            default_branch = (
                f"slice/pr-{number}"
                if number is not None
                else _rs.slice_branch(todo_id)
            )
            branch = str(params.get("branch") or default_branch)
            ship_log = _rs._read_log(handle)
            cross = cross_line(ship_log) or CHECKED_RED_MARK
            ctx.append_chunk(
                "job_summary",
                f"land_slice td{todo_id}: RED — {cross}\n"
                f"subject: {handle.get('subject')}\nlog: {handle.get('log')}\n"
                f"verify: {', '.join(ids)} outside the diff, green "
                "standalone (flaky or order-dependent)",
            )
            _fail(
                ctx,
                todo_id,
                params.get("lane"),
                f"ship of td{todo_id} red: {cross} — outside-diff ids green standalone",
                handoff=(
                    f"land FAILED on {branch}: {cross}. The failing ids "
                    f"({', '.join(ids)}) are outside the diff and passed "
                    "standalone — flaky or order-dependent. Nothing to "
                    "rebuild; the lander re-lands."
                ),
                attempt_free="flaky outside the diff",
            )
            return True
        # A non-zero exit is only "red alone" when the probe's own
        # summary shows the named ids failing — anything else is a probe
        # failure and stays the ordinary red.
        if exit_code is not None and _probe_red(v, ids):
            try:
                if _start_main_verify(ctx, handle, v):
                    return False
            except admission.Refused as exc:
                return _defer_admission_refused(ctx, handle, "main probe", exc)
        return _ordinary_ship_red(ctx, handle)
    if phase == "deselect":
        # The checked selection minus the red-on-main ids (gr598110):
        # green means the land goes through ``--quick``; the engine
        # review reads the landed diff, not this gate. Any red or dead
        # probe is the ordinary red; a launch failure already fell back.
        if exit_code != 0:
            return _ordinary_ship_red(ctx, handle)
        return _land_without_main_reds(
            ctx,
            handle,
            params,
            ids,
            why="the checked selection minus them is green — the land "
            "goes through --quick",
        )
    # Phase "main" finished (or the probe died): the throwaway worktree
    # and its stack go away on every exit path — and the job meta forgets
    # the probe, so a later kill does not remove a tree already gone.
    for note in _drop_main_verify(v):
        ctx.append_chunk("job_event", f"land_slice: {note}")
    ctx.set_meta(main_probe=None)
    if phase != "main" or exit_code is None:
        return _ordinary_ship_red(ctx, handle)
    if exit_code == 0:
        handle["verify_note"] = (
            f"Outside-diff verify: {', '.join(ids)} are GREEN on "
            "origin/main — the red is this branch's."
        )
        return _ordinary_ship_red(ctx, handle)
    if not _probe_red(v, ids):
        # "red on main too" needs the same evidence: a non-zero probe that
        # never ran the named tests is a probe failure, never a verdict.
        return _ordinary_ship_red(ctx, handle)
    # gr598110 + td599058 + gr599427 + td602151: EVERY checked land whose
    # red is main's too goes through ``--quick``, engine or not
    # (``PRECIS_LAND_FULL_FALLBACK=off`` keeps the red a red for every
    # land — the gr599427 ruling dropped E1's "never lint" clause). When
    # the checked selection is a subset, a detached ``scripts/test
    # --selected --deselect <id>…`` first proves it green minus the
    # red-on-main ids, inside the 1 h wall. When the selection IS the
    # suite (FULL) no gate runs — the deselect run would BE the suite
    # and the 3 h full-gate budget is gone; the ids are recorded as
    # excluded on the todo and ``--quick`` lands, the release gate's CI
    # keeping the suite honest. An unreadable selector takes the FULL
    # path too (gr586292).
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    if full_fallback_flag() is None:
        return _ordinary_ship_red(ctx, handle)
    full = _checked_selection_full(tree)
    if full is None:
        ctx.append_chunk(
            "job_event",
            "land_slice: the selector was unreadable (timeout or error) — "
            "counted as FULL; the full suite is deferred to the deploy gate",
        )
    if full is not False:
        return _land_without_main_reds(
            ctx,
            handle,
            params,
            ids,
            why="the checked selection is the suite (FULL), so they are "
            "excluded from the gate — the land goes through --quick and "
            "the release gate keeps the suite",
        )
    argv = ["--selected", *[a for i in ids for a in ("--deselect", i)]]
    try:
        verify = _launch_verify(
            tree, ids, phase="deselect", run_tree=tree, argv=argv, store=ctx.store
        )
    except admission.Refused as exc:
        # The finished main probe's throwaway tree goes on every exit
        # path (its phase is done; the deselect gate never started) —
        # then the capacity refusal defers attempt-free.
        for note in _drop_main_verify(v):
            ctx.append_chunk("job_event", f"land_slice: {note}")
        ctx.set_meta(main_probe=None)
        return _defer_admission_refused(ctx, handle, "deselect", exc)
    if verify is None:
        return _ordinary_ship_red(ctx, handle)
    _promote_verify(handle, verify)
    ctx.append_chunk(
        "job_event",
        f"land_slice: {', '.join(ids)} red on origin/main too — running "
        "the checked selection minus them (scripts/test --selected "
        "--deselect); --quick lands on its green",
    )
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=verify["deadline"],
    )
    return False


def _land_without_main_reds(
    ctx: Any,
    handle: dict[str, Any],
    params: dict[str, Any],
    ids: list[str],
    *,
    why: str,
) -> bool:
    """Relaunch ``scripts/ship --quick`` once the main-red ``ids`` are
    out of the land's way — proven by a green deselect gate, or excluded
    because the checked selection IS the suite (td602151: no land runs
    the suite). The todo gets a ``handoff:`` note naming them FIRST —
    the land log dies with the worktree — then the same 1 h relaunch
    the FULL-selector fallback uses; no attempt is charged. A failed
    write is the ordinary red: ``--quick`` may not land with the
    exclusion unrecorded (td602837). ``why`` completes "the ids are
    not this branch's — {why}"."""
    todo_id = int(params.get("todo_id") or 0)
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    subject = str(handle.get("subject") or "")
    try:
        _rs._append_comment(
            ctx.store,
            todo_id,
            f"{_rs._LANDER_HANDOFF_PREFIX} {', '.join(ids)} are red on "
            f"origin/main too — not this branch's; {why}. "
            "The reds are main's to fix.",
        )
    except Exception:
        log.warning(
            "land_slice: main-red note on td%d failed — --quick not relaunched",
            todo_id,
            exc_info=True,
        )
        return _ordinary_ship_red(ctx, handle)
    ctx.append_chunk(
        "job_event",
        f"land_slice: {', '.join(ids)} red on origin/main too — {why}; "
        "relaunching scripts/ship --quick",
    )
    Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
    fresh = _launch_ship(ctx, tree, "--quick", subject, carry=handle)
    if fresh is None:
        return True
    handle.clear()
    handle.update(fresh)
    if fresh.get("not_before"):
        return False  # parked by the preflight; its deferral set the meta
    ctx.set_meta(
        compute_handle=fresh,
        compute_handle_released=False,
        deadline=fresh["deadline"],
    )
    return False


def _repo_handle(ctx: Any, handle: Any) -> bool:
    """Whether ``handle`` (or the job's params) belongs to ``repo_land``:
    its ``lander`` says so, or the handle's or the job's ``repo`` is not
    the default (the no-land handle carries ``repo`` but no ``lander``)."""
    if isinstance(handle, dict):
        if handle.get("lander") == "repo_land":
            return True
        if handle.get("repo") not in (None, _lanes.DEFAULT_REPO):
            return True
    meta = getattr(ctx, "meta", None) or {}
    return _rs.job_repo(meta) != _lanes.DEFAULT_REPO


def poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half: ``False`` while ship runs; then the land record
    and cleanup (green) or the red path."""
    if _repo_handle(ctx, handle):
        from precis.workers import repo_land

        return repo_land.poll(ctx, handle)
    if not isinstance(handle, dict):
        ctx.record_failure(f"land_slice: malformed handle {handle!r}")
        return True
    if handle.get("not_before"):
        return _relaunch_when_due(ctx, handle)
    if isinstance(handle.get("verify"), dict):
        return _poll_verify(ctx, handle)
    # submit's ``no_land`` sentinel (gr648125): the branch carried
    # nothing — no commit ahead, no net diff — so no ship ran; the
    # nothing-new record below closes the todo done the same way.
    no_land = bool(handle.get("no_land"))
    exit_code = 0 if no_land else _rs._read_exit(handle)
    if exit_code is None and _process_alive(int(handle.get("pid") or 0)):
        # gr598110 item 4: while ship queues inside gate_slot_acquire the
        # wait is real queueing on the host, not gate time — the wall
        # clock starts when the gate starts, so each poll that finds the
        # wait folds the new wait time into the deadline and heartbeats
        # the position line. The deadline rides the handle (gr647913):
        # a re-adopted job resumes its own count — started_at + wall +
        # the waits observed — instead of restarting the wall at the
        # re-adopt poll.
        now_ts = _now().timestamp()
        seen = _phase_seen(handle, now_ts)
        waiting = gate_slot_waiting(_rs._read_log(handle))
        if waiting is not None:
            if handle.get("gate_wait") != waiting:
                handle["gate_wait"] = waiting
                ctx.append_chunk("job_event", f"land_slice: {waiting}")
            ctx.set_meta(
                compute_handle=handle,
                compute_handle_released=False,
                deadline=_wait_deadline(handle, seen, now_ts),
            )
        else:
            handle.pop("gate_wait", None)
            # polled_at rides the handle on every live poll, so the
            # wait's start is bounded by the last seen instant, never by
            # a worker restart's re-adoption.
            ctx.set_meta(compute_handle=handle, compute_handle_released=False)
        return False
    ctx.set_meta(compute_handle_released=True)

    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    number = _pr_param(params)
    default_branch = (
        f"slice/pr-{number}" if number is not None else _rs.slice_branch(todo_id)
    )
    branch = str(params.get("branch") or default_branch)
    lane = params.get("lane")
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    subject = str(handle.get("subject") or "")
    log_text = _rs._read_log(handle)
    status = ship_status(log_text)
    green = exit_code == 0 and (status is None or status == "shipped")

    if not green:
        # A host-side line outranks the ✖: when docker itself is broken the ✖
        # that follows blames the branch's lockfile (jo553514).
        cross = (
            host_refusal_line(log_text)
            or cross_line(log_text)
            or (
                f"scripts/ship exit {'unknown' if exit_code is None else exit_code}"
                f"{f', SHIP_STATUS {status}' if status else ''}"
            )
        )
        reason = f"ship of td{todo_id} red: {cross}"
        handoff = (
            f"land FAILED on {branch}: {cross}. Fix it on the branch, commit, "
            "leave the tree clean; the lander re-runs scripts/ship."
        )
        # Gate output can echo credentials (env dumps, request URLs): mask
        # FIRST, then cut — a secret spanning the cut boundary can
        # otherwise leak a fragment. What rides the todo comment and the
        # job summary is already masked.
        masked_log = mask_secrets(log_text) or ""
        errors = gate_error_lines(masked_log)
        detail = errors or gate_red_excerpt(masked_log)
        if errors:
            handoff += f"\n\nThe gate's error lines:\n{errors}"
        elif detail:
            # Nothing parsed — the excerpt is all the next session gets;
            # the land log dies with the worktree (gr573347).
            handoff += f"\n\nThe gate's output before the verdict:\n{detail}"
        # gr599427 (Reto 2026-11-08): a checked land the selector refuses
        # with FULL relaunches through the same ``--quick`` lane every
        # land uses, engine diffs included — no land runs the full local
        # gate; the release gate on /go is GitHub CI.
        fallback = full_fallback_flag()
        if (
            fallback
            and FULL_SELECTOR_MARK in cross
            and str(handle.get("flag") or _planned_flag(ctx.meta, params)) != fallback
        ):
            # The lane is refused, not the branch: relaunch through the
            # fallback lane (Reto 2026-11-05, me570388). No attempt is
            # consumed and no builder is asked to fix what the ship said
            # about itself.
            Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
            ctx.append_chunk(
                "job_event",
                f"land_slice: {cross} — relaunching scripts/ship "
                "--quick (lint only; the release gate on /go runs the "
                "full suite)",
            )
            # Carried onto the fresh handle so the green path's todo
            # comment can say the checked land fell back (td573580).
            handle["fell_back_full"] = True
            fresh = _launch_ship(ctx, tree, fallback, subject, carry=handle)
            if fresh is None:
                return True
            handle.clear()
            handle.update(fresh)
            if fresh.get("not_before"):
                return False  # parked by the preflight; its deferral set the meta
            ctx.set_meta(
                compute_handle=fresh,
                compute_handle_released=False,
                deadline=fresh["deadline"],
            )
            return False
        note = handle.pop("verify_note", None)
        if note:
            handoff += f"\n\n{note}"
        # gr573347 b: a checked selection red only on test ids the diff
        # does not touch may not be the branch's — verify standalone,
        # then on origin/main, before charging an attempt. Any launch or
        # probe trouble falls through to the ordinary red.
        ids = failed_test_ids(log_text)
        if (
            handle.get("verify_done") is None
            and CHECKED_RED_MARK in cross
            and todo_id > 0
            and ids
            and len(outside_diff_ids(tree, ids)) == len(ids)
        ):
            try:
                verify = _launch_verify(
                    tree, ids, phase="alone", run_tree=tree, store=ctx.store
                )
            except admission.Refused as exc:
                return _defer_admission_refused(ctx, handle, "verify", exc)
            if verify is not None:
                _promote_verify(handle, verify)
                ctx.append_chunk(
                    "job_event",
                    "land_slice: checked selection red only on ids outside "
                    f"the diff — re-running {', '.join(ids)} standalone "
                    "before charging the branch",
                )
                ctx.set_meta(
                    compute_handle=handle,
                    compute_handle_released=False,
                    deadline=verify["deadline"],
                )
                return False
        side = refusal_side(cross)
        if side is not None and RELEASE_BUSY_MARK in str(cross):
            # Another release push holds ship's round lock: run again
            # in a minute, on this wait's own counter.
            waits = int(handle.get("release_busy") or 0) + 1
            if waits <= MAX_RELEASE_BUSY_WAITS:
                return _defer_to_main(
                    ctx,
                    handle,
                    cross,
                    waits,
                    side=side,
                    backoff_s=RELEASE_BUSY_BACKOFF_S,
                    counter="release_busy",
                )
            reason, handoff = _blocked_handoff(branch, waits, cross, side)
        elif side is not None:
            # Main's or the host's state, not the branch's: park and retry
            # later, up to the cap.
            refusals = int(handle.get("main_refusals") or 0) + 1
            if refusals <= MAX_MAIN_REFUSALS:
                return _defer_to_main(ctx, handle, cross, refusals, side=side)
            reason, handoff = _blocked_handoff(branch, refusals, cross, side)
        else:
            # A gate the VM killed is host-side too (gr570133): the land
            # defers and retries under `gate_kills` — its own counter, the
            # shorter kill backoff, and the sweep on relaunch — never an
            # attempt, never a handoff. A bare exit 137 (ship SIGKILLed,
            # no mark in its log) is the same kill and rides the same
            # counter. An exit-137 past the cap is still outside the
            # diff — no attempt is burned — but the container's own
            # mem_limit is also 137, so the branch's own memory may be at
            # fault: it does NOT re-land on its own
            # (``meta.land_again_cap`` sends the tick to the ask, keeping
            # the blocked handoff that names the VM's or the branch's
            # memory, gr570187). An xdist crash retries once, then falls
            # through to the ordinary red: a repeated crash is
            # deterministic — a branch problem — and the ``[gwN] node
            # down:`` line xdist emits for it is not an infra mark.
            kill_line = gate_kill_line(log_text, exit_code)
            red_class: str | None = None
            kills = int(handle.get("gate_kills") or 0) + 1
            if kill_line is not None and "exited 137 (SIGKILL)" not in kill_line:
                # A repeated worker crash is deterministic — a branch
                # problem — so ``red_class`` stays ``None`` and the
                # ordinary red below burns an attempt (main's rule).
                if kills <= 1:
                    return _defer_to_main(
                        ctx,
                        handle,
                        f"gate worker crashed mid-run (xdist): {kill_line}",
                        kills,
                        side="host",
                        backoff_s=gate_kill_backoff_s(),
                        counter="gate_kills",
                    )
            elif kill_line is not None or exit_code == 137:
                kill_cross = f"{GATE_KILL_CROSS}: {kill_line or cross}"
                if kills <= MAX_GATE_KILL_RETRIES:
                    return _defer_to_main(
                        ctx,
                        handle,
                        kill_cross,
                        kills,
                        side="host",
                        backoff_s=gate_kill_backoff_s(),
                        counter="gate_kills",
                    )
                reason, handoff = _blocked_handoff(branch, kills, kill_cross, "gate")
                red_class = "gate infrastructure"
            else:
                red_class = infra_red(log_text, exit_code)
            if red_class is not None:
                # Outside the diff (no attempt), but spent — the tick
                # goes to the ask rather than re-landing (gr570187).
                ctx.set_meta(land_again_cap=True)
        ctx.set_meta(exit_code=exit_code, ship_status=status)
        ctx.append_chunk(
            "job_summary",
            f"land_slice td{todo_id}: RED — {cross}\nsubject: {subject}\nlog: "
            f"{handle.get('log')}" + (f"\n{detail}" if detail else ""),
        )
        tail = masked_log[-_TAIL_CAP:].strip()
        if side is None and red_class is None and is_conflict_refusal(cross):
            # Before ``_fail`` drops the folder: the paths git names are
            # only in the clone's index (gr741695).
            _stamp_conflict(
                ctx, todo_id, tree, params, onto=land_target(ctx.store, params, todo_id)
            )
        _fail(
            ctx,
            todo_id,
            lane,
            reason + (f"\n--- log tail ---\n{tail}" if tail else ""),
            handoff=handoff,
            attempt_free=red_class if side is None else None,
        )
        return True

    # gr561142: ship's nothing-new guard squashes nothing and then names
    # main's existing head on its ``landed on main:`` line. That sha is
    # another land's; it must not reach the todo, the gate or the logbook.
    no_diff = no_land or nothing_new(log_text)
    onto = None if no_diff else land_target(ctx.store, params, todo_id)
    sha = None if no_diff else _landed_sha(log_text, tree, onto)
    if sha is None and not no_diff:
        ctx.record_failure(
            f"land_slice: ship of td{todo_id} exited 0 but no landed sha could "
            f"be read (log {handle.get('log')}); check git log origin/{onto or 'main'}"
        )
        return True
    # Ship pushed the launcher's private mirror, not GitHub: place the
    # refs ship moved onto the real origin under the launch-tip lease —
    # verified against the sha ship reported — before any land record
    # names their shas (gr659929).
    try:
        _root: Path | None = _rs.slice_root()
    except Exception:
        _root = None
    mirror_in_play = _mirror_in_play(_root, todo_id, handle, ctx.store)
    synced = _sync_to_origin(ctx, handle, params, branch, expected_sha=sha, onto=onto)
    if synced is not None:
        return synced
    return _landed_finish(
        ctx,
        handle,
        params,
        sha=sha,
        exit_code=exit_code,
        status=status,
        # A pin-bump land ran ship with land_notify held (td659522 F7).
        notify_deferred=mirror_in_play or _carries_pin(ctx.store, todo_id),
    )


def _carries_pin(store: Any, todo_id: int) -> bool:
    """Whether ``todo_id`` is a pin-bump node (its meta carries a
    ``pin_bump`` record). Fails closed — ``True`` when the store cannot
    say: this guards ship's land_notify close, and holding the notify for
    a plain slice only moves its marks to the lander's deferred write."""
    try:
        return _pin_record(store.get_ref(kind="todo", id=todo_id)) is not None
    except Exception:
        log.warning("land_slice: reading td%d's pin record failed", todo_id)
        return True


def _pin_finish(store: Any, todo_id: int, sha: str | None) -> str | None:
    """td659522 step 4: the ``land_slice`` a ``pin_bump`` mints lands the
    pin node's own branch — :func:`pin_bump.finish` is its closing tail,
    run right before this land's record: ``meta.pin_bump`` ``state:
    "landed"`` plus the batch's ``fixes`` gripe resolution (without it
    the record would sit ``handed`` forever and
    the ``pin_bump/stale-open:{todo}`` alert would fire). A plain slice
    carries no pin record and is skipped; a no-diff reland finishes at
    the record's own commit. Never raises — the land is real, the record
    is a report; ``finish``'s own ``landed`` stamp is idempotent. The
    node is not closed here or by :func:`_record_land`: the ``landed``
    record is what the dispatcher's next ``pin_bump`` resumes into the
    gate image wait (td659522 F7)."""
    try:
        ref = store.get_ref(kind="todo", id=todo_id)
        record = (getattr(ref, "meta", None) or {}).get("pin_bump")
        if not isinstance(record, dict):
            return None
        landed_sha = sha
        if landed_sha is None:
            from precis.workers import repo_land

            landed_sha = repo_land._hex(record.get("commit")) or None
        if landed_sha is None:
            return "pin: record present but no sha to finish at"
        from precis.workers.job_types import pin_bump

        pin_bump.finish(store, todo_id, landed_sha)
        return f"pin: record landed at {landed_sha[:9]}"
    except Exception:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: pin finish for td%d failed", todo_id, exc_info=True)
        return "pin: finish FAILED"


def _mint_skill_edit(store: Any, todo_id: int, sha: str) -> str:
    """Mint one follow-up per landed diff, atomically with the source receipt."""
    from precis.plans.nodes import finish_mint

    chunks = store.chunks.list_chunks_for_ref(todo_id)
    handoffs = [
        c.text
        for c in chunks
        if c.chunk_kind == "todo_comment" and c.text.startswith("handoff:")
    ]
    ids = _rs.skills_named(handoffs[-1] if handoffs else None)
    if ids is None:
        return "skills: not stated"
    if not ids:
        return "skills: none"
    plan_tag = _plan_tag(store, todo_id)
    with store.tx() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s FOR UPDATE", (todo_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"skill-edit source td{todo_id} disappeared")
        receipts = dict((row[0] or {}).get("skill_edit_jobs") or {})
        if sha in receipts:
            return f"skills: follow-up td{receipts[sha]} already minted"
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"Update skills after td{todo_id}",
            parent_id=todo_id,
            meta={
                "task_class": "skill",
                "op": "run_slice",
                "skill_ids": list(ids),
                "landed_sha": sha,
            },
            conn=conn,
        )
        store.add_tag(ref.id, Tag.closed("STATUS", "open"), set_by="system", conn=conn)
        store.add_tag(ref.id, Tag.open("skill-edit"), set_by="system", conn=conn)
        store.add_tag(
            ref.id, Tag.open(plan_tag or f"plan:{ref.id}"), set_by="system", conn=conn
        )
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=f"Read landed diff `git show {sha}` from td{todo_id}.\n"
                    f"Named skills: {', '.join(ids)}.\nRun each example on the live tool and correct drift. "
                    "Escalate governs:factory skills and missing rulings to Reto.",
                    meta={"chunk_kind": "todo_body"},
                )
            ],
            conn=conn,
        )
        receipts[sha] = ref.id
        store.stamp_ref_meta(todo_id, {"skill_edit_jobs": receipts}, conn=conn)
        finish_mint(store, ref.id, conn=conn)
    return f"skills: minted td{ref.id} for {', '.join(ids)}"


def _landed_finish(
    ctx: Any,
    handle: dict[str, Any],
    params: dict[str, Any],
    *,
    sha: str | None,
    exit_code: int | None,
    status: str | None,
    notify_deferred: bool = False,
) -> bool:
    """The green tail — the pin finish, the land record, the plan gate,
    the gripe rung, the logbook, cleanup, the PR close — shared by
    :func:`poll`'s ship-green path and :func:`_launch_ship`'s pre-ship
    already-landed finish (gr650484: ``status="already-landed"``, no
    ship log exists, ``exit_code`` is None). ``handle`` only needs
    ``worktree`` and ``subject`` (plus ``fell_back_full`` on the relaunch
    path).

    ``notify_deferred`` is the mirror lands' contract (review td660513
    finding 4): ship ran with ``PRECIS_LAND_NOTIFY=0`` so its
    land_notify wrote nothing — here, AFTER the origin sync verified
    the sha, the lander writes every handle the subject names."""
    todo_id = int(params.get("todo_id") or 0)
    number = _pr_param(params)
    default_branch = (
        f"slice/pr-{number}" if number is not None else _rs.slice_branch(todo_id)
    )
    branch = str(params.get("branch") or default_branch)
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    subject = str(handle.get("subject") or "")
    landed = f"landed {sha[:9]} — {subject}" if sha else NO_DIFF_NOTE
    summary = [f"land_slice td{todo_id}: {landed}"]
    store = ctx.store
    # The first write after the land: the task's build floor is this land's
    # job id, so the attempt refs below it are harmless from here on even if
    # the worker dies before the land record or the cleanup (the cleanup
    # stamps it again with the rest).
    floor = _land_floor(tree)
    if floor is not None:
        try:
            store.stamp_ref_meta(todo_id, {BUILD_FLOOR_KEY: floor})
        except Exception as exc:  # pragma: no cover — the cleanup stamps it again
            log.warning(
                "land_slice: build floor of td%d failed", todo_id, exc_info=True
            )
            summary.append(f"build floor: stamp FAILED ({exc})")
    if sha is not None:
        try:
            skill_note = _mint_skill_edit(store, todo_id, sha)
            summary.append(skill_note)
            if skill_note == "skills: not stated" and _newest_comment(
                store, todo_id
            ) != ("skills: not stated"):
                _rs._append_comment(store, todo_id, skill_note)
        except Exception as exc:
            log.warning(
                "land_slice: skill-edit mint for td%d failed", todo_id, exc_info=True
            )
            summary.append(f"skills: follow-up FAILED ({exc})")
    try:
        pin_note = _pin_finish(store, todo_id, sha)
        if pin_note is not None:
            summary.append(pin_note)
        wrote = _record_land(store, todo_id, sha, subject)
        summary.append(
            "todo: "
            + (", ".join(wrote) + " written" if wrote else "land_notify did it")
        )
        if handle.get("fell_back_full"):
            # td573580 review: a checked land the FULL selector sent
            # through --quick says so in plain words on the todo.
            _rs._append_comment(
                store,
                todo_id,
                "checked wanted, selector FULL, landed --quick — the "
                "release gate runs the suite",
            )
            summary.append("todo: checked→quick fallback noted")
    except Exception as exc:  # pragma: no cover — the land is real; report it
        log.warning("land_slice: land record on td%d failed", todo_id, exc_info=True)
        summary.append(f"todo: land record FAILED ({exc})")
    if notify_deferred and sha is not None:
        # Ship's land_notify and gripe-note were held
        # (PRECIS_LAND_NOTIFY=0 / PRECIS_GRIPE_NOTE=0): write the
        # subject's other handles now that origin holds the sha.
        try:
            summary += _deferred_notify(store, subject, sha, todo_id, tree)
        except Exception as exc:  # pragma: no cover — reported, land stands
            log.warning(
                "land_slice: deferred notify for td%d failed",
                todo_id,
                exc_info=True,
            )
            summary.append(f"deferred handles: notify FAILED ({exc})")
    gate_id = None
    # ``sha`` is what THIS land pushed; ``gate_sha`` is what the plan's
    # gate ends up holding — the recovered prior land's on a no-diff
    # reland (gr576154). The gripe verify comment and the PR close ride
    # ``gate_sha``: the work is on main under that sha whichever land
    # pushed it.
    gate_sha = sha
    try:
        gate_id = pending_gate(store, todo_id)
        if gate_id is None:
            summary.append("gate: no pending sha-in-prod gate blocked-by this todo")
        elif sha is None:
            # gr576154: a reland after a land that pushed but died before
            # its gate write squashes nothing — the todo's own landed:
            # comment still names the sha the gate waits on.
            prior = _landed_comment_sha(store, todo_id)
            if prior is not None:
                _write_gate(store, gate_id, prior, todo_id)
                gate_sha = prior
                summary.append(
                    f"gate td{gate_id}: sha-in-prod sha set to {prior[:9]} "
                    "(from the todo's landed comment — the reland squashed "
                    "nothing)"
                )
            else:
                summary.append(
                    f"gate td{gate_id}: left pending — no sha landed (the work "
                    "is already on main; the plan decides what the gate waits "
                    "for)"
                )
        else:
            _write_gate(store, gate_id, sha, todo_id)
            summary.append(f"gate td{gate_id}: sha-in-prod sha set to {sha[:9]}")
    except Exception as exc:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: gate write for td%d failed", todo_id, exc_info=True)
        summary.append(f"gate: write FAILED ({exc})")
    plan_tag = _plan_tag(store, todo_id)
    try:
        # gr561932: the land that fills the plan's gate is the fix; it moves
        # the gripe the plan is about to the verify rung, as a session
        # would. A brief node's land (a spec test) fills no gate and leaves
        # the gripe alone.
        if gate_id is not None and gate_sha is not None and plan_tag is not None:
            gripe_id = _plan_gripe(store, plan_tag)
            if gripe_id is not None:
                summary.append(
                    _notify_gripe(store, gripe_id, gate_sha, subject, todo_id)
                )
    except Exception as exc:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: gripe notify for td%d failed", todo_id, exc_info=True)
        summary.append(f"gripe: notify FAILED ({exc})")
    try:
        if plan_tag is not None:
            # gr548800: a landed slice is a quest-logbook milestone.
            root_id = int(plan_tag[5:])
            log_plan_event(
                store,
                todo_id,
                f"slice td{todo_id} {landed}",
                entry_type="milestone" if sha else "observation",
                root=root_id,
            )
    except Exception as exc:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: logbook write for td%d failed", todo_id, exc_info=True)
        summary.append(f"logbook: write FAILED ({exc})")
    cleanup = _cleanup(tree, branch, todo_id, store)
    summary += cleanup
    closed_pr = None
    try:
        closed_pr = _pr_to_close(store, dict(params), todo_id)
        if closed_pr is None:
            pass
        elif gate_sha is None:
            summary.append(f"PR #{closed_pr}: left open — nothing landed")
            closed_pr = None
        else:
            summary.append(_close_pr(store, closed_pr, todo_id, gate_sha))
    except Exception as exc:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: PR close for td%d failed", todo_id, exc_info=True)
        summary.append(f"PR close FAILED ({exc})")
    ctx.set_meta(
        exit_code=exit_code,
        ship_status=status or "shipped",
        land_subject=subject,
        landed_sha=sha,
        gate_id=gate_id,
        cleanup=cleanup,
        closed_pr=closed_pr,
    )
    ctx.append_chunk("job_summary", "\n".join(summary))
    ctx.set_status("succeeded")
    return True


def _push_branch(tree: Path, branch: str, sha: str | None = None) -> str:
    """Push ``<branch>`` to origin's hidden ``refs/factory/<branch>``
    (:data:`~precis.plans.templates.SLICE_REF_NS`) under the factory
    credential — until then the branch lives only on this host, so a
    killed land's commit would die with a worktree reap (gr598110,
    jo595597). Never ``refs/heads/``: GitHub would list the branch and
    offer a "Compare & pull request" for it (Reto 2026-11-15). Never from
    ``tree``'s own gitdir: the tree's config and hooks are
    branch-written, so a planted ``hooks/pre-push`` or a
    ``url.insteadOf`` redirect would meet the token (review td660513
    finding 2) — the push runs in a fresh ops repo that borrows the
    object store by alternates. One note for the job event; never
    raises.

    The push is lease-guarded (gr665602, after gr658662): a slice branch
    is factory-owned with one writer, so a rebased re-push of a slice
    already on origin must overwrite our own earlier push — a plain push
    is rejected non-fast-forward and burned the whole retry ladder. No
    launch tip of ``slice/<id>`` is recorded anywhere (the mirror's launch
    tips cover the land refs, not the slice branch), so the lease names
    the tip ``ls-remote`` reads immediately before the push: an empty
    expectation when the branch is not on origin yet. A move between that
    read and the push is refused (``lease refused``) and still fails the
    caller; an unreadable origin falls back to the plain push. The note
    carries ``(replaced)`` when the push overwrote a tip origin already
    held (an amended/rebased build whose earlier push is stale,
    gr663738).

    ``sha`` pushes that commit instead of the local branch tip: the
    park-save's WIP snapshot (:mod:`precis.workers.slice_park`) rides the
    same ref without moving the local branch."""
    ops: Path | None = None
    remote: dict[str, str] | None = None
    try:
        root = _rs.slice_root()
        url = _github_url(root)
        if url is None:
            return f"branch push of {branch}: skipped (no trusted origin url)"
        if sha is None:
            sha = (
                _git(["rev-parse", f"refs/heads/{branch}"], tree).stdout or ""
            ).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            return f"branch push of {branch}: FAILED (no local sha)"
        env = _factory_git.git_auth_env(base=land_env())
        gitdir = _worktree_gitdir(tree)
        common = _git_common_dir(gitdir, tree) if gitdir else None
        dirs = [common / "objects"] if common else []
        ops = _factory_git_mirror._ops_repo(
            _factory_git_mirror.mirror_root(root), *dirs, env=env
        )
        if ops is None:
            return f"branch push of {branch}: FAILED (no ops repo)"
        remote = _factory_git_mirror.ls_remote(
            url, (branch,), env=env, namespace=SLICE_REF_NS
        )
        lease: list[str] = []
        if remote is not None:
            lease = [
                f"--force-with-lease={SLICE_REF_NS}{branch}:{remote.get(branch, '')}"
            ]
        proc = _factory_git_mirror._run(
            [
                "git",
                *_factory_git_mirror.TRUSTED_ARGS,
                "-C",
                str(ops),
                "push",
                url,
                f"{sha}:{SLICE_REF_NS}{branch}",
                *lease,
            ],
            cwd=ops,
            env=_factory_git_mirror.trusted_env(env),
            timeout=120.0,
        )
    except Exception as exc:
        return f"branch push of {branch}: FAILED ({exc})"
    finally:
        _factory_git_mirror._drop_ops(ops)
    if proc.returncode == 0:
        prior = (remote or {}).get(branch, "")
        replaced = " (replaced)" if prior and prior != sha else ""
        return f"branch push of {branch}: on origin{replaced}"
    err_full = (proc.stderr or proc.stdout or "").strip()
    err = err_full[:200]
    # The "(stale info)" marker trails the full ref line — check the
    # UNTRUNCATED stderr or a long origin url pushes it past 200 chars
    # (real-git coverage, review td676510).
    if "stale info" in err_full:
        seen = (remote or {}).get(branch, "absent")[:9]
        return (
            f"branch push of {branch}: FAILED (lease refused — {branch} moved "
            f"on origin since {seen}: {err})"
        )
    return f"branch push of {branch}: FAILED ({err})"


#: The wall-deadline kill's timed-out verdict (gr647914): ``ssh_node``
#: merges these into the ``swept:wall-timeout`` terminalize write
#: (``JobTypeSpec.deadline_kill_meta``). ``attempt_free == "infra-land"``
#: keeps the kill off the attempt count — the deadline, not the diff,
#: ended the land — and ``land_again_cap`` parks the run at the plan
#: tick's "land again (no builder)" ask: the SIGTERMed ship may already
#: have merged, so nothing may auto re-land the possibly half-landed
#: branch. ``red_class`` is the distinct verdict the ask's wording reads.
DEADLINE_KILL_META: dict[str, Any] = {
    _rs.ATTEMPT_FREE_KEY: "infra-land",
    "red_class": "timed-out",
    "land_again_cap": True,
}


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: SIGTERM the live phase's process group —
    a running verify's during verify, ship's otherwise (a ship that
    already exited must never be signaled: the pgid may have been
    reused) — then drop the main-probe worktree and stack the job meta
    recorded (:func:`_start_main_verify` writes them before the add), so a
    killed land leaves neither behind. The branch is not pushed any more
    (gr741695; it was, so a worktree reap could not lose the land's
    commit — gr598110): the commit to land lives in the hub, and the
    land's clone holds nothing else. The clone itself is left for the
    host sweep, which settles a terminal land's folder as holding nothing
    (``run_slice``'s ``settle``) and drops it once no ship is behind it.
    A handle naming the worker's own pid or
    process group is never signaled (td652194: a pre-launch stamp once
    carried the worker's pid into an adopted handle)."""
    if _repo_handle(ctx, handle):
        from precis.workers import repo_land

        return repo_land.kill(ctx, handle)
    if isinstance(handle, dict):
        verify = handle.get("verify")
        live = verify if isinstance(verify, dict) else handle
        pgid = int(live.get("pgid") or live.get("pid") or 0)
        own = {os.getpid(), os.getpgrp()}
        if pgid in own or int(live.get("pid") or 0) in own:
            log.warning("land_slice: kill refused — handle names the worker (%d)", pgid)
        elif pgid > 1:
            try:
                _rs._signal_group(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                # The pgid now belongs to another user's process (reused
                # after ship exited): not ours to signal, so skip it.
                log.warning("land_slice: kill skipped — pgid %d is not ours", pgid)
    probe = (getattr(ctx, "meta", None) or {}).get("main_probe") or {}
    tree = str(probe.get("tree") or "")
    if not tree:
        return
    stack = str(probe.get("stack") or "")
    if stack.startswith(_slice_stacks.SLICE_PREFIX):
        _evict_stack(stack, volumes=True)
    try:
        root = _rs.slice_root()
    except RuntimeError:
        return
    _git(["worktree", "remove", "--force", tree], root)


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    validate_submit=validate_submit,
    submit=submit,
    poll=poll,
    reconcile=reconcile,
    kill=kill,
    deadline_kill_meta=DEADLINE_KILL_META,
    wall_seconds=wall_seconds,
)


__all__ = [
    "ALREADY_LANDED_WINDOW",
    "BUILD_REF_KEY",
    "CHECKED_RED_MARK",
    "COMPATIBLE_EXECUTORS",
    "CONFLICT_KEY",
    "CONFLICT_MARKS",
    "DEADLINE_KILL_META",
    "DESCRIPTION",
    "DRIFT_PROBE_TIMEOUT_S",
    "GATE_KILL_137_RE",
    "GATE_KILL_BACKOFF_S",
    "GATE_KILL_CROSS",
    "GATE_KILL_XDIST_RE",
    "HOST_REFUSAL_MARKS",
    "HOST_STATE_ENV_NAMES",
    "INDEX_LOCK_STALE_S",
    "INDEX_LOCK_WAIT_S",
    "LAND_LOCK_FILE",
    "LAND_LOCK_TODO_FILE",
    "MAIN_REFUSAL_BACKOFF_S",
    "MAIN_REFUSAL_MARKS",
    "MAX_GATE_KILL_RETRIES",
    "MAX_MAIN_REFUSALS",
    "NAME",
    "PARAMS_SCHEMA",
    "PREFLIGHT_HELPER_REL",
    "PREFLIGHT_LIB_REL",
    "REQUIRES",
    "SHIP_LOCK_DIR",
    "SPEC",
    "SUBJECT_CAP",
    "UNKNOWN_GATE_CROSS",
    "cross_line",
    "failed_test_ids",
    "gate_error_lines",
    "gate_kill_backoff_s",
    "gate_kill_line",
    "gate_red_excerpt",
    "host_refusal_line",
    "index_lock_stale_s",
    "index_lock_wait_s",
    "is_conflict_refusal",
    "is_host_refusal",
    "is_main_refusal",
    "kill",
    "land_target",
    "landed_short",
    "outside_diff_ids",
    "pending_gate",
    "poll",
    "reconcile",
    "refusal_backoff_s",
    "refusal_side",
    "ship_bin",
    "ship_python",
    "ship_status",
    "squash_subject",
    "submit",
    "validate_submit",
    "wall_seconds",
    "with_handle",
]
