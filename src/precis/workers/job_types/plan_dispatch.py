"""``plan_dispatch`` job_type — the dark-factory tick that drives plan subtrees.

Spec: ``docs/backlog/dark-factory.md`` § "Plans" → "The dispatcher tick" and
"Job lifecycle per task"; the kill switch is step 7's. A plan is a todo
subtree whose nodes are shaped by :mod:`precis.plans.nodes`. One pass:

0. **dead roots** — a plan root closed by hand (``STATUS`` terminal)
   drops out of ``_candidate_roots``' set with its whole subtree
   invisible to the tick — its open children would sit frozen forever:
   asks still offered, gates never judged (gr612091). The sweep closes
   every open descendant ``won't-do`` — a plan nested inside the dead
   subtree dies with it (self-rooted is not live-owned) — mirroring
   each close onto the loaded ``_Plan`` nodes so no later step this
   tick re-judges a node it just closed (td612465), and deferring a
   node with a live job to the next tick, the moot close's rule.
1. **gates** — every live, unblocked gate todo is evaluated
   (:func:`precis.plans.gates.evaluate`); the verdict lands on
   ``meta.gate_result = {open, why, at}`` (written when it changes) and an
   open gate goes ``STATUS:done``, which frees its ``blocked-by`` dependants.
   A release-pass approval ask (``meta.ask.kind == 'approval'``) is first
   offered to :func:`precis.plans.release.auto_answer_approval` — the
   ``release.auto_deploy`` standing approval answers ``deploy r<N>``
   itself once CI is recorded green on the cut's exact sha and
   ``release.round`` names an open round on it (td573809, me573231).
2. **loops** — once every current child of a loop is closed: ``until`` open
   or ``meta.loop.count`` (default 0) at ``max`` closes the loop; otherwise
   the body is re-minted under it (:func:`precis.plans.templates.mint_body`)
   and the count bumped. An open informational ask (``meta.ask.blocks ==
   'nothing'``) counts as closed here and in step 3: it is a record for the
   human, not a child still at work (gr589823). A ``waiting-for:`` tag on
   the loop itself parks it outright — no re-mint, no close (td610546).
   A body with no minter
   closes the loop with a comment naming the gap. A ``release-fix`` loop's automatic cap is
   :data:`_RELEASE_FIX_AUTO_CAP` (its node stamps no ``max``): the
   second red iteration files a ruling ask (naming the round, the head
   sha and the failing CI) and closes the loop — a third fix is a human
   call (me573231).
2a. **reviews** — a reviewer generator (``meta.generate.role ==
   'reviewer'``, ``from: 'td<id>'``) whose newest job succeeded is judged by
   its newest comment starting ``review:`` posted since that job was minted
   — a ``review:``-starting comment always wins — else its newest
   ``handoff:`` carrying a ``review: LAND|FIX|REVERT`` line anywhere in
   the message, markdown emphasis stripped (``**review: FIX — …**``
   mid-prose, gr568824: one verdict line is that verdict; verdict lines
   that disagree are malformed — retried, never resolved (td636385);
   REVERT never
   comes from a glued token — or a ``review: LAND|FIX`` token at a line
   start (markdown ``- ``/``* ``/``**``/``#``/``> `` markers may sit
   between the line start and the token) or right after sentence-final
   punctuation, outside backticks/parens — the verdict lines too, so a
   quoted grammar echo never turns a real line into a disagreement
   (td591267) — no
   ``<…>`` rest (a lane gluing
   the verdict to its narration, gr573564; on disagreement a reasoned
   FIX beats a LAND; REVERT never comes from a token). A LAND rest
   opening ``(clean`` or ``|`` is the grammar echo, and a placeholder
   ``review: FIX|REVERT — <…>`` template line anywhere in the comment
   suppresses every LAND in it. A job the seat voided — a ``void:``
   open tag on the job — is skipped here and in the attempt count
   (gr568824). Grammar, verdict word
   case-insensitive, any of ``—`` / ``–`` / ``-`` as the dash::

       review: LAND (read-only, tests not run)
       review: FIX — <the list of findings> (read-only, tests not run)
       review: REVERT — <why> (read-only, tests not run)

   The ``(read-only, tests not run)`` marker is stripped where it ends a
   line before matching — accepted, never required, so a forgotten
   marker never strands a review. A LAND is a diff read, never a test
   result. ``LAND`` closes the node. ``FIX`` mints one ``run_slice`` task
   ``Fix review findings for td<id>`` (body = the list) under the plan root
   and closes the node — the mint is stamped ``meta.fix_depth``, the
   count of consecutive FIX mints on the review→fix→land→review chain,
   read off the reviewed todo's own stamp so the dispatcher never
   re-walks ``meta.review.task`` backlinks (gr604629). Escalation is by
   round count: a fix minted at :data:`FIX_BRIEF_DEPTH` is born parked
   on ``waiting-for:brief`` with a design-call ask on the plan root
   (gr603118): a chain that deep needs the coordinator's ``brief:``
   comment, which a slice dispatched at mint would outrun (me570353).
   ``_step_brief_holds`` releases the hold on the coordinator's tag
   removal, a ``continue`` answer on the ask, or :data:`BRIEF_WAIT_CAP`
   of silence (the ask closes ``STATUS:auto-timeout``), and the node's
   first attempt starts one ladder rung up — swe-2-max on the devin
   table (:func:`_chain_floor`). A fix minted at
   :data:`FIX_ESCALATE_DEPTH` is born model-pinned to the ladder's top
   rung with an ``escalated: round <depth> of td<root>`` comment — no
   second design-call window; and at :data:`FIX_CHAIN_CAP` no fix mints
   at all: the root parks ``waiting-for:coordinator`` — a hold step 3
   honours and whose ``waiting-for:`` prefix keeps every descendant out
   of dispatch — and one ``ask-coordinator`` ask (``blocks:`` the root,
   so it counts as open work, not a record) collects the chain's
   complete findings and landed shas for one briefed builder. ``REVERT``
   mints one ``run_slice`` task ``Revert
   <sha> on main`` (the ``landed: <sha>`` comment of each reviewed node --
   ``td<id>`` itself, or its child todos when it has them, as a slicer
   does) under the root, reopens each reviewed node ``blocked-by`` that
   revert with a ``handoff: review: REVERT — <why>`` comment (the rebuild's
   brief), and leaves the reviewer open to review the rebuild; both get a
   fresh retry budget (``meta.rearmed_after_job``). Each ``sha-in-prod``
   gate of the plan on a reverted sha goes back to ``PENDING_SHA`` (reopened
   if it had opened) with a ``sha reset after review REVERT`` comment. A missing or malformed
   verdict reopens the node — the retry stays on the picked lane (a
   format miss never climbs the ladder, gr573564) — and counts against
   the retry cap; at cap step 5 sends the reviewer to rescue as cls
   ``review``. ``meta.review = {verdict, job, at,
   tests_run}`` records the job judged, so a pass never acts twice on one
   job; ``tests_run`` is always ``False`` — the reviewer is read-only —
   and only this step and its retry path read the record, so nothing
   reads a verdict as test evidence. A reviewer whose land targets ALL
   closed with no ``landed:`` comment never dispatches: the tick closes
   it ``won't-do`` with the dead target's other ``blocked-by``
   descendants (the prod gate, the validate — the same close a task
   moot runs, re-swept each pass while a straggler's live job defers
   it), so the root settles and no ask files (gr575912).
   **Verdicts** — a node carrying a ``no-op:``/``moot:``/``validated:``
   line on a ``handoff:``, posted by hand as a bare verdict comment
   (first line the verdict — a ``brief:`` or other prose quoting an
   example verdict line is never read, td604234), or in its newest
   failed job's event text is dropped before the moot close
   (gr602896): a job recorded ``failed`` is no bar — the verdict IS the
   result. The node closes done, a ``moot:`` also tagged
   ``verdict:moot`` so the moot close drops its chain this pass; a
   ``moot:`` pinned to a ``gr<N>`` other than the plan's gripe is never
   this plan's verdict, and nodes under a ``release-pass`` root keep
   the fix loop's own rulings. A job stamped
   ``human_only_hit`` keeps its ask — no verdict closes ahead of the
   human-only ruling, before or after the ask mints (td604234,
   td604695). An unverified or hand-posted moot has
   no sha for a ``resolved:`` — the plan's gripe gets
   ``<verdict> — plan dropped`` instead, so the record of why the plan
   went away survives.
3. **phases** — first reopens a generator that is ``STATUS:done`` while a
   child is still open (a generator's session mints children, it does not
   close the node; counted as ``reopened``). Then closes every node whose work
   is finished: a phase
   (``meta.auto_close``) or a generator with children, once every child is
   closed — unless the node itself carries a ``waiting-for:`` tag, a park
   that outranks its children's state (the FIX-chain cap's
   ``waiting-for:coordinator`` on the root, an unsettled mint); an
   ``analyst`` generator once its newest job succeeded and an
   ``analysis:`` comment was posted since (td543749); a mechanical task (``meta.op`` other than ``run_slice``) whose
   newest job succeeded; a ``run_slice`` task whose newest job is a
   succeeded ``land_slice`` (a succeeded build alone never closes it, and
   a build plus its red land count as one attempt). Deepest first, so
   nested phases cascade in one pass.
4. **dispatch** — each doable task (``node_kind`` task, a generator with no
   children yet, or a plain leaf under a plan root; ``STATUS:open``, no live
   ``blocked-by`` target on it or an ancestor — a terminal target stays
   live while its ``meta.pending_land`` holds, the dependant's worktree
   having to cut from a main carrying the build, and only the pre-land
   reviewer gating that land is exempt, gr659981 — no doable-exclusion
   tag other
   than ``child-failed:`` on it or an ancestor, no paused ancestor, no live
   child job, no
   ``claimed-by:`` tag, no mint pass still in flight, under its retry cap)
   gets one job parented to it — ``hold:no-dispatch`` rides the shared
   registry, so the coordinator's hold parks a plan node here exactly as
   it parks a gripe in ``gripe_dispatch`` (gr645524):
   ``meta.op`` resolved to ``{type, params}`` (a bare name is
   ``{type: name, params: {todo_id}}``), default ``run_slice {todo_id}``.
   A ``run_slice`` class with no enabled offering is blocked on one
   coordinator ask before any job is minted (gr611305). So is a
   ``run_slice`` leaf whose judged text — its body and newest
   ``brief:`` — names a deny path or is prod-graph only:
   work no builder lane may write skips the ladder and files one
   ``ask-coordinator`` ask with the brief attached (gr645163, Reto's
   2026-11-12 "escalate to next model" ruling — td643078/td638980's
   won't-do stalls and gr630346/gr630611's no-commit runs were this
   class). Generators never classify: they read source text, and a
   gripe or diff may name deny paths legitimately. A ``handoff:``
   never classifies either — it reports the attempt, and a denied
   run's own deny-path line is the event, not the target. The
   ``gripe-fix-direct`` instruction head and a pasted brief's
   deny-path/land-lint sections are stripped before the text is
   judged (:func:`precis.plans.route`). The newest brief and body are
   judged separately for deny paths, so body labels cannot hide an
   unlabelled brief's target (td651223); graph-only checks use both.
   Capacity and
   temporary lane holds still wait in ``run_slice``; answering the ask
   after enabling a lane or retargeting the class releases the node.
   The tick owns plan retries, so a retry drops the ``child-failed:`` tags
   the failure bubble left. **Re-land** (gr570187): a task whose newest
   job is a failed ``land_slice`` carrying ``attempt_free == "infra-land"``
   re-mints ``land_slice`` with the failed job's own params (idem
   ``land_slice:td<id>``) — no builder, no ladder rung, no review lane —
   up to :data:`LAND_AGAIN_CAP` consecutive infra lands, then the hygiene
   ask offers ``land again (no builder)``. **Mechanical reds** (gr604579,
   factory plan 3b): a builder task's newest failed attempt whose error
   text names a known mechanical cause — a stale or colliding migration
   number, an H2 chunk over 4000 chars, a skill file over its size cap, a
   registry name in no skill, a rebase/merge conflict, squawk, ruff
   (:func:`_mechanical_brief`) — gets the mapped fix posted as a
   ``brief:`` comment and retries on the SAME rung the last builder ran
   (:func:`_mech_pin`), never the ladder climb: a mechanical red does
   not need a stronger model, it needs the line fixed. The budget is
   two briefed retries of one signature (:data:`MECH_RETRY_CAP`,
   :func:`_mech_red`); the third red stamps
   ``meta.mech_retries.exhausted``, which step 5 reads as capped — the
   ask files with the signature named, no rescue pass in between. An
   unclassified red climbs the ladder exactly as before. **Order:**
   candidates from
   every plan root are
   taken together, sorted by (``dispatch_score`` descending, todo id
   ascending; gr604593); a node with no stamped score takes its nearest
   ancestor's, else :data:`DEFAULT_SCORE`, and a human-set ``prio``
   multiplies it (never the key). So a release-pass root stamped
   ``RELEASE_SCORE`` mints (and reaches the lanes) before a default
   gripe build, whichever root is older. A run_slice whose brief file
   set overlaps an in-flight land's is held until that land finishes
   (td603849 — the ``_inflight_land_ids``/``_brief_file_sets`` check).
   A reviewer's ``run_slice`` is pinned to
   ``pick_lane('review', exclude_vendor=<the builder lane's vendor>)``;
   while no other vendor's lane is enabled that falls back to the
   builder's lane and the action note says so. **Mint barrier**
   (td603429): a session mints each child with one ``put`` — the row
   commits open and linkless — and adds the ``blocked-by`` links in
   later calls, so ``put`` stamps ``waiting-for:mint``
   (:data:`precis.plans.nodes.MINT_HOLD_TAG`) on EVERY child landing
   inside a live plan subtree (:func:`in_live_plan`), unconditionally —
   no ancestry, job, lease or principal inference decides who is held:
   every inference the held-at-creation chain tried (gr590954;
   td601078, td601628, td602774) left a hole the next review found
   (td599743, td601023, td601626, td602770, td603427). The
   ``waiting-for:`` prefix keeps the linkless child out of dispatch
   while it still counts as open work — a loop does not close over
   it and a done generator reopens for it — until the
   minter finishes — the factory's own mint sequences end in
   :func:`precis.plans.nodes.finish_mint` as their last call (a test
   scans the source for every site), an interactive minter removes
   the tag once its links are settled — the runbook contract
   (me570515) enforced — a clean ``run_slice`` exit finishes the
   settled mints its job window produced
   (:func:`release_session_mints`, gr603945 — a generator's children
   would otherwise idle :data:`MINT_STALE` plus a tick on every run) —
   and a minter that dies or forgets leaves the hold to
   hygiene (step 5, gr574881).
5. **hygiene** — a ``claimed-by:`` tag older than 4 h on a todo with no live
   child job is released (``STATUS:open``); the same for a ``STATUS:fixing``
   gripe (lease past ``expires_at``, no live job under or ``fixes``-linked to
   it; ``release_claims``, back to ``STATUS:triaged`` with a comment — unless
   its newest comment is ``resolved:``, then only the lease goes; gr542086);
   a ``waiting-for:mint`` tag older than :data:`MINT_STALE` on a mint whose
   links or body settled is released — a dead minter cannot strand the child
   (gr574881) — while an orphan mint (no ``blocked-by``/``part-of`` edge, no
   body — its own orphan-ask edge is the ask's dedup mark, not a link) is
   not released and files one ask on the plan root instead;
   a task whose newest job ended
   (failed, or succeeded without closing it) with attempts at its retry
   cap is parked. The cap is an explicit ``meta.retries`` pin, else the
   node's lane-ladder length for a ``run_slice`` op — one rung per
   attempt, so the ladder is spent before the cap fires (td584381 S1a);
   a reviewer counts only the picked lane's rungs, a model-pinned op
   keeps :data:`DEFAULT_RETRIES`, and so does a ladder-less one. A
   ``rung_caps`` entry on the lane's ``llm_account.entitlement``
   (``{model id: usd}``, gr599486 — Reto's $25-per-node bound for a
   paid top rung like ``claude-fable-5-1-medium`` on devin) makes the
   rung spend-blocked once the node's recorded spend meets it: the
   spend is summed ``meta.usage`` over the node's job rows
   (``on_demand_usd``/``cost_usd`` direct, ``acus_consumed`` at the
   lane's ``entitlement.acu_usd`` rate, default
   :data:`DEFAULT_ACU_USD`), ``_escalate`` skips the rung like a
   backed-off lane, the retry-cap ladder loses it too, and an
   all-blocked tail caps the node with ``meta.spend = {usd, cap_usd,
   blocked, at}`` stamped; the rescue step names the cap. A climb ONTO
   a capped rung mints the job with ``params.budget_usd`` — the cap
   minus recorded spend — and ``run_slice`` kills the session once its
   live usage meets the bound, so an attempt starting under the cap
   cannot run past it (td601015). An uncapped
   rung is never spend-blocked. What
   parks it follows the failure: an
   exhausted ``run_slice`` node enters rescue —
   :func:`precis.workers.factory_rescue.enter_rescue` tags it
   ``rescue:<cls>`` (cls ``review`` for a reviewer, else ``builder``),
   files or joins the signature gripe, and the rescue pass owns what
   follows — one diagnose-and-re-arm, then its own ask on a second
   exhaustion (``meta.rescue.n >= 1``), the end of the autonomous road;
   answering that ask re-arms the node and clears the rescue tag in the
   same step (``_step_rearm``). A failed job stamped ``no_commit``
   (``run_slice``'s mark for a clean exit that wrote nothing — the
   builder's can't-finish, a won't-do with no verdict) burns an attempt
   like any red and climbs the ladder instead of capping at once
   (gr645163, the same ruling); the node the cap finally parks files
   the ``ask-coordinator`` ask, never rescue — the ladder is spent and
   td643078's hand-land was the coordinator's job all along. The cap
   reads ladder exhaustion, not just the count: a can't-finish whose
   newest attempt already ran the top rung — a fix born model-pinned
   at :data:`FIX_ESCALATE_DEPTH` starts there — is capped on the spot
   (td645566), not retried on the same rung the count still affords. A
   failed job stamped
   ``human_only_hit=[paths]`` (``run_slice`` writes
   it when the branch touches ``lanes.HUMAN_ONLY_PATHS``,
   ``land_slice`` when the landed diff touches the resolved deny set —
   gr645524; at the land every deny hit is a "no lane may write" hit)
   and a ``run_slice`` with no enabled fit lane still
   get the old ask, at once and naming the paths or the gap; a
   mechanical op (``meta.op`` other than ``run_slice``) asks too. The
   ask is one ask-user todo under the plan root (``meta.ask`` kind
   ``ruling``, tags ``ask-user`` + ``showstopper``) the task is
   ``blocked-by``. A failed job stamped ``meta.coordinator_ask``
   (``ci_red_digest``'s persistent infra red or refused rerun) gets an
   ``ask-coordinator``-tagged ask instead — a CI or credential fault is
   the coordinator's call (td612107 review). A land that died past its
   own gate-kill retries carries ``meta.land_again_cap`` and goes
   straight to that ask — the capped run is done, no auto re-land
   (gr570187). An answered re-land ask re-lands only when the answer
   names ``land again (no builder)`` and the task's brief is untouched;
   a rewritten brief — or any other answer — mints a builder on the
   task's new brief.
   Attempts count the ended jobs since the newest ask on
   the task (or the ``attempt:<n>`` tag ``run_slice`` writes, before any ask),
   so answering the ask re-arms the task — except a job carrying
   ``meta.host_refusal``: the lane's host refused the launch, so nothing
   was tried (gr571395); a job carrying ``meta.attempt_free`` failed for
   a reason outside the slice's diff (``"lane-wait"``, gr572118;
   ``"infra-land"``, gr570187) and likewise burned no attempt — a
   sustained run of the SAME free reason backs off exponentially from
   the newest failure's mint and parks behind a coordinator ask at
   :data:`FREE_RUN_CAP`, since the retry bound never sees it (gr662077);
   an
   operator-cancelled job is likewise free — its node re-queues on the
   next dispatch, but only when the job's type is on
   :data:`_REQUEUE_ON_CANCEL` (the default op plus confirmed
   read-only/idempotent ops). Any other cancelled type may carry side
   effects a kill did not roll back — a land killed mid-ship may
   already have merged, a ``pr_action`` may already have posted — so it
   keeps the "not redispatched" skip for the rescue pass (gr587772).
   A job still queued is never an
   attempt; one queued over 10 min whose ``ServiceSpec.placement``
   capabilities no live host advertises gets the note ``td<id>: job jo<N>
   queued <m> min — no host advertises {tokens}`` (td543917), and one
   queued over :data:`QUEUED_ASK_AFTER` also files ONE ruling ask on its
   node — provision a host, drop the node, or wait — deduped on
   ``meta.unplaced_ask`` and settled ``done``/``won't-do`` as the gap or
   the node closes (gr657333).

Plan roots: the ids named by ``plan:<id>`` tags on live todos, plus every
live todo whose meta makes it a gate / loop / generator / phase; a root
inside another root's subtree is folded into it. Closed roots are skipped.

**Kill switch.** ``PRECIS_FACTORY=off`` in the worker's env, a
``service_config`` row for ``plan_dispatch`` with ``prio = 0`` (any host), or
``params.dry_run`` makes the pass read-only: every step runs on the
in-memory model and counts what it would have done, nothing is written. The
job summary's first line is
``plan_dispatch: roots=N gates=o/c loops=minted/closed phases=closed
dispatched=N released=N asks=N dry_run=bool reopened=N reviews=N``; the
actions follow it. A ``release_cut`` node's dispatch refusal (a cut
held on the tip's reviews) is also one ``job_result`` line and a log
line, once per change of its text (``meta.cut_refusal_logged`` on the
node; gr651885) — the summary's notes are capped and nobody saw r291's.

**Arming** (an operator write, not done by any deploy) — the recurring todo
the minter turns into a job every 5 minutes, like ``graph_garden``::

    put(kind='todo', text='plan_dispatch tick (dark factory)',
        meta={'schedule': {'every': '5m'}, 'executor': 'job_inproc',
              'job_type': 'plan_dispatch'})

``dedupe_pending`` coalesces a tick whose previous run is still pending. The
pass and the job may run side by side: :func:`run_pass` takes a global
advisory lock for the whole tick and a second tick skips instead of racing it.
Workers never import handlers: every write here is a store call.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.alerts import raise_alert
from precis.errors import BadInput
from precis.plans import gates as _gates
from precis.plans import governs as _governs
from precis.plans import lanes as _lanes
from precis.plans import release as _release
from precis.plans import route as _route
from precis.plans import templates as _templates
from precis.plans.logbook import log_plan_event
from precis.plans.nodes import (
    MINT_HOLD_TAG,
    MOOT_TAG,
    any_verdict,
    finish_mint,
    generator_verdict,
    node_kind,
)
from precis.principal import worker_comment_meta
from precis.store._todo_sql import _DOABLE_EXCLUSION_TAGS
from precis.store.advisory import try_xact_advisory_lock
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import BLOCKER_TERMINAL_STATUSES, TERMINAL_TODO_STATUSES
from precis.todo.guards import check_ask_meta, release_claims
from precis.todo.views import _fetch_subtree
from precis.utils import dispatch_score as _dscore
from precis.workers.job_types import JobTypeSpec, _lane_decide, get_job_type

log = logging.getLogger(__name__)

NAME = "plan_dispatch"

#: Advisory-lock key serialising whole ticks (ascii "PLANTICK"; fits bigint).
#: Held as a transaction-scoped lock on a dedicated connection for the full
#: pass, so the job path and the worker-lane pass never mint or dispatch the
#: same thing twice.
TICK_LOCK_KEY: int = 0x504C414E5449434B
#: ``idle_in_transaction_session_timeout`` for the lock connection: longer than
#: any tick, so a tick that overruns loses the lock (the intended failure)
#: rather than pinning it forever.
TICK_LOCK_IDLE_TIMEOUT = "900s"
#: ``application_name`` of the lock connection in ``pg_stat_activity``.
TICK_LOCK_APP_NAME = "precis plan_tick lock"

#: ``service_config.service`` of the kill switch (a ``prio = 0`` row).
SERVICE = "plan_dispatch"

#: ``meta`` key of a ref's hidden comment numbers — the same string as
#: ``handlers._comment_timeline.HIDDEN_META_KEY`` (workers never import
#: handlers; a test pins the two equal).
_HIDDEN_META_KEY = "hidden_comments"

#: A ``claimed-by:`` tag older than this, with no live job, is released.
CLAIM_STALE = timedelta(hours=4)

#: A plan job queued longer than this with no host advertising its placement
#: capabilities gets a pass note (td543917).
QUEUED_NOTE_AFTER = timedelta(minutes=10)

#: A plan job queued longer than this with no live host advertising its
#: placement capabilities also files ONE coordinator ask on its node
#: (gr657333): the sweeper's ``scheduler/unschedulable`` warn alert is
#: the standing page, but a ``run_slice`` needing
#: claude_bin/repo_checkout/git still sat queued 51+ min with no
#: capable host and nobody asked. Deduped on :data:`_UNPLACED_ASK_KEY` —
#: a closed ask is the ruling, never re-minted.
QUEUED_ASK_AFTER = timedelta(minutes=30)

#: ``meta.unplaced_ask`` on the task node — ``{ask, at, job, gap}``
#: recording the ask its placement gap filed: the dedup mark plus the
#: heal check's handle (``_step_unplaced`` closes it ``done`` once a
#: live host advertises the tokens again, ``won't-do`` when the node
#: closes first).
_UNPLACED_ASK_KEY = "unplaced_ask"

#: Options of the ask an unplaced job files (gr657333) — a bounded
#: ruling, never delegated work (``todo.guards``'s ``meta.ask`` shape).
_UNPLACED_ASK_OPTIONS: list[str] = [
    "provision a host advertising the missing capability",
    "drop the node",
    "wait — the host comes back on its own",
]

#: A ``waiting-for:mint`` tag older than this is stale — the minter died,
#: errored or forgot between the ``put`` and its link-settling calls
#: (gr574881; the me570515 contract leaves removal to the minter, and the
#: rescue reads the held child as a quiet node, not a stall). A stale hold
#: on a settled mint — a ``blocked-by``/``part-of`` edge or a non-empty
#: body — is released; an unsettled orphan files an ask on its plan root.
MINT_STALE = timedelta(minutes=10)

#: The hold a fix node is minted with when its review→fix→land→review
#: chain reaches :data:`FIX_BRIEF_DEPTH` (gr603118): each fix in such
#: a chain patches the previous reviewer's latest hole and the next
#: reviewer finds the next one, while a fix dispatched at mint outruns
#: the coordinator's ``brief:`` comment — the only channel a design call
#: has to reach the slice (me570353). A ``waiting-for:`` prefix is a
#: doable exclusion, so the node parks until the coordinator removes the
#: tag (the posted brief rides the todo), the design-call ask's newest
#: ``answer:`` is ``continue``, or :data:`BRIEF_WAIT_CAP` of silence
#: passes and the ladder tries again anyway.
BRIEF_HOLD_TAG = "waiting-for:brief"

#: The chain depth that holds a minted fix node for the design call:
#: the first review fix dispatches at once; the fix of a fix waits.
#: Deeper generations do not wait again — they escalate by count
#: (gr604629): the design-call window happens once per chain.
FIX_BRIEF_DEPTH = 2

#: The chain depth that escalates the minted fix to the ladder's top
#: rung (gr604629, Reto 2026-11-09: escalation by count, not dollars):
#: a finding two rounds could not close goes to the strongest rung on
#: round three — the node is born ``meta.op.params.model``-pinned, so
#: ``pick_lane(..., model=)`` routes it and ``_escalate`` never climbs
#: it back down. It carries an ``escalated: round <depth> of td<root>``
#: comment instead of the brief hold.
FIX_ESCALATE_DEPTH = 3

#: The chain depth that stops minting (gr604629): a fourth-round fix on
#: the same finding is never minted — the plan root parks on
#: :data:`COORDINATOR_HOLD_TAG` and one ``ask-coordinator`` ask collects
#: the chain's findings and landed shas, so one briefed builder gets one
#: brief instead of a chain of partial fixes.
FIX_CHAIN_CAP = 4

#: The tag a FIX-chain-capped plan root parks on (gr604629): a
#: ``waiting-for:`` marker for the coordinator — no auto-release and no
#: step settles it; removing it is the human's call once the briefed
#: builder rides.
COORDINATOR_HOLD_TAG = "waiting-for:coordinator"

#: The routing tag of the ask the cap mints (gr604629): it rides the
#: ``ask-user`` channel like every ruling ask; this tag names the
#: coordinator as its audience.
COORDINATOR_ASK_TAG = "ask-coordinator"

#: Options of the ask a chain cap files under the plan root — the
#: coordinator's real answer is the briefed builder it mints outside
#: the chain, so the ask itself only needs the acknowledgement.
_COORDINATOR_ASK_OPTIONS: list[str] = [
    "acknowledged",
]

#: Silence past this lifts the brief hold — no brief and no answer came,
#: so the ladder continues and the design-call ask closes
#: ``STATUS:auto-timeout``.
BRIEF_WAIT_CAP = timedelta(minutes=60)

#: Options of the design-call ask a held fix node files: the only
#: releasing answer is ``continue`` (a different ruling — including
#: dropping the node — is a human close on the fix todo itself).
_BRIEF_ASK_OPTIONS: list[str] = [
    "continue",
]

#: ``meta.fix_depth`` on a minted fix node — the FIX-chain generation
#: :func:`_fix_depth` counts at mint. Stamped on every review fix
#: (depth 1 included) so the next mint reads the counter off the
#: reviewed todo instead of re-walking the ``meta.review.task``
#: backlinks (gr604629); :func:`_chain_floor` reads it for the ladder
#: escalation, ``meta.brief_wait`` for the hold's ask record.
FIX_DEPTH_KEY = "fix_depth"

#: ``meta.brief_wait`` on a held fix node — ``{ask, at, depth, ruling?}``:
#: the design-call ask's id (the step's lookup, since no ``blocked-by``
#: edge may tie it — removing the tag alone must release), the hold's
#: mint time and the chain depth; ``ruling`` is stamped once when the
#: ask closed on a non-``continue`` answer, so the kept hold is noted
#: once, not every pass.
BRIEF_WAIT_KEY = "brief_wait"

#: Default ``meta.retries`` of a task.
DEFAULT_RETRIES = 2

#: The job type a task without ``meta.op`` (and every generator) runs.
DEFAULT_OP = "run_slice"

#: The todo's comment chunk kind (``handlers.todo._COMMENT_KIND``; restated —
#: workers never import handlers).
_COMMENT_KIND = "todo_comment"
#: The gripe comment chunk kind (``handlers.gripe._COMMENT_KIND``; restated).
_GRIPE_COMMENT_KIND = "gripe_comment"

_JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed-out"})
_JOB_ENDED = frozenset({"succeeded", "failed", "timed-out"})
_ATTEMPT_RE = re.compile(r"^attempt:(\d+)$")

#: The ONLY cancelled-job types the tick re-queues blind
#: (:func:`_step_dispatch`): the default op plus ops confirmed
#: read-only/idempotent — ``ci_verdict_poll`` is one GitHub GET plus a
#: verdict row keyed per sha. Every other type may carry side effects a
#: kill cannot be assumed to have rolled back (a land may already have
#: merged, a ``pr_action`` may already have posted), so it keeps the
#: "not redispatched" skip for the rescue tick; an unregistered type
#: defaults to the same safe side (gr587772). ``handlers.job``'s
#: ``mode='retry'`` ack reads this set.
_REQUEUE_ON_CANCEL = frozenset({DEFAULT_OP, "ci_verdict_poll"})

#: ``run_slice.ATTEMPT_FREE_KEY``, restated — the dependency stays
#: one-way (a test pins the two spellings equal). A job carrying the key
#: failed for a reason outside the slice's diff and burned no attempt:
#: ``"lane-wait"`` (the lane never freed a slot, gr572118), ``"index-lock"``
#: (a live git's lock in the reused slice tree, gr650484), ``"infra-land"``
#: (the gate's own machinery killed the land, gr570187).
ATTEMPT_FREE_KEY = "attempt_free"

#: ``run_slice.HUMAN_ONLY_KEY``, restated — the dependency stays
#: one-way (a test pins the two spellings equal). The failed job lists
#: the ``lanes.HUMAN_ONLY_PATHS`` globs its branch touched (``run_slice``)
#: or the resolved deny set's globs (``land_slice``, gr645524 — a land
#: refusal there is the same "no lane may write" call); a stamped
#: failure is a human call — no lane may write those paths — so the
#: tick asks at once instead of climbing the ladder (td584381 S1a).
HUMAN_ONLY_KEY = "human_only_hit"

#: ``run_slice.NO_COMMIT_KEY``, restated — the same one-way rule. A
#: clean-exit failure that left no commit on the slice branch is the
#: builder's can't-finish: it burns an attempt and climbs the ladder
#: like any red; the cap — the ladder spent — files the coordinator
#: ask instead of entering rescue (gr645163, the 2026-11-12 "escalate
#: to next model" ruling).
NO_COMMIT_KEY = "no_commit"

#: ``run_slice.MOOT_VERIFIED_KEY``, restated — the same one-way rule. A
#: code task's VERIFIED ``moot:`` close carries ``{"sha", "line"}`` on
#: the succeeded job; :func:`_step_moot` resolves the plan's gripe on
#: that sha exactly as a land does (td584381 S1d).
MOOT_VERIFIED_KEY = "moot_verified"

#: ``ci_red_digest.COORDINATOR_ASK_KEY``, restated — the same one-way
#: rule (a test pins the two spellings equal). The digest stamps it on
#: the job a persistent infra red or a refused ``gh run rerun`` failed
#: (td612107 review): the retry-cap ask files on the
#: ``ask-coordinator`` channel — a CI or credential fault is the
#: coordinator's call, not another lane retry's.
COORDINATOR_ASK_KEY = "coordinator_ask"

#: ``run_slice.MODEL_RULE_KEY``, restated — the same one-way rule (a
#: test pins the two spellings equal). The failed job carries the
#: different-model refusal's reason (gr657521): the verdict is
#: deterministic — no ladder rung changes it — so the tick asks the
#: coordinator at once instead of spending the retry budget on
#: identical refusals.
MODEL_RULE_KEY = "model_rule_refusal"

#: Consecutive ``_notify_gripe`` raises on a verified task moot —
#: stamped on the moot node's meta by :func:`_step_moot` (td584381 S1d
#: review round 2). At :data:`_MOOT_STAMP_ASK_AFTER` the tick files one
#: ask on the plan root and parks the stamp: a silent ``res.notes``
#: loop would page nobody. The park lifts when the ask is answered or a
#: human clears the key.
MOOT_STAMP_KEY = "moot_stamp_failures"

#: Failed gripe-stamp ticks before the moot close parks behind an ask.
_MOOT_STAMP_ASK_AFTER = 3

#: Options of the ask a parked moot stamp files (review round 2).
_MOOT_STAMP_ASK_OPTIONS: list[str] = [
    "retry the gripe stamp",
    "won't do",
]

#: ``run_slice.VALIDATED_NOREPRO_KEY`` / ``run_slice.NOREPRO_WAIT_TAG``,
#: restated — the same one-way rule. The stamp a fix node's succeeded
#: ``validated:``-no-commit job carries, and the open tag that parks it
#: for :func:`_step_ci_rerun` (gr588028): a ``waiting-for:`` prefix is a
#: doable exclusion, so dispatch leaves the node to this step.
VALIDATED_NOREPRO_KEY = "validated_norepro"
NOREPRO_WAIT_TAG = "waiting-for:ci-rerun"

#: How long a parked fix node may wait on the rerun and its verdict
#: before the tick files the stall ask (gr588028 item 4): still parked,
#: never a second mint.
RERUN_WAIT_CAP = timedelta(hours=2)

#: Options of the ask a stalled rerun path files under the plan root —
#: it only RECORDS the stall (the loop already continued or still waits
#: without it), so the one option is the acknowledgement.
_RERUN_ASK_OPTIONS: list[str] = [
    "acknowledged",
]

#: Options of the ask an orphan mint files (gr574881) — nothing
#: automated answers it; the body carries the mechanics.
_ORPHAN_MINT_ASK_OPTIONS: list[str] = [
    "release the hold anyway",
    "won't do",
]

#: Consecutive infra-red lands (:func:`_infra_red_lands`) before the tick
#: parks the node behind a "land again" ask (gr570187): the re-land is
#: bounded so a broken gate environment cannot loop the lander forever.
LAND_AGAIN_CAP = 2

#: Consecutive identical ``attempt_free`` failures (:func:`_free_run`)
#: before the node reads as capped and parks behind a coordinator ask
#: (gr662077): free failures burn no attempt, so the retry bound never
#: sees them — an infra wait that never clears (a live git's
#: ``index.lock``, a lane that never frees) needs its own bound.
FREE_RUN_CAP = 6

#: Re-mints stay immediate below this run length — a transient infra
#: wait usually clears on the next tick; a sustained one backs off.
FREE_RUN_GRACE = 3

#: Backoff on a sustained identical ``attempt_free`` run (gr662077):
#: after run *k* the re-mint waits :data:`FREE_BACKOFF_BASE` ×
#: ``2 ** (k − FREE_RUN_GRACE)``, capped at :data:`FREE_BACKOFF_MAX`,
#: anchored to the newest failure's mint — submit-side free failures
#: die seconds after mint, so the anchor is close to the failure.
FREE_BACKOFF_BASE = timedelta(minutes=2)
FREE_BACKOFF_MAX = timedelta(minutes=30)

#: ``meta.spend`` on a task node — the dispatcher's spend record
#: ``{usd, cap_usd, blocked, at}`` (gr599486). ``blocked: True`` means
#: a paid rung's ``rung_caps`` cap refused the climb; the rescue step
#: and the ask path read the stamp to name the cap.
SPEND_KEY = "spend"

#: ACU→USD for ``meta.usage.acus_consumed`` when the lane's
#: ``entitlement.acu_usd`` names no rate — Devin's published list price
#: per ACU (~$2 on the metered plans, gr599486).
DEFAULT_ACU_USD = 2.0

#: Briefed same-rung retries one mechanical signature gets — two
#: briefed retries, the third red asks (gr604579, factory plan 3b): a
#: signature back a third time is not a brief problem, a human reads
#: it. The run counts consecutive same-signature failed attempts at the
#: node's job tail (:func:`_mech_red`).
MECH_RETRY_CAP = 2

#: ``meta.mech_retries`` on a task node — the dispatcher's mechanical-red
#: record ``{sig, n, job, at, exhausted?}`` (gr604579): the last
#: classified red's signature, the consecutive repeat count it stood at,
#: and the job it came from. ``exhausted: True`` marks the repeat past
#: :data:`MECH_RETRY_CAP` — what :func:`_capped` turns into the ask.
MECH_RETRIES_KEY = "mech_retries"

#: Mechanical red signatures (gr604579, factory plan 3b): ``(slug, line
#: pattern, the brief instruction)`` — the table of ship/lint reds a
#: coordinator used to brief by hand off the error line (me588390: a
#: ``brief:`` comment, never a cancel). The instruction's ``{paths}``
#: fills from the failure text (:func:`_conflict_paths`). Patterns match
#: one stripped line; ``_mech_hit`` scans the job's whole event text —
#: the ✖ of the skill classes is a generic "lint is RED" while the
#: decisive line sits in the log tail.
_MECHANICAL_REDS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "migration-number",
        re.compile(
            r"\bis numbered \d{3,4}\b.*\bnext free number\b"
            r"|migration number collision",
            re.IGNORECASE,
        ),
        "merge origin/main into the branch so its scripts/ship carries the "
        "land-time renumber (it moves a plain add to the next free number "
        "itself; a merge commit is fine); if the land still refuses, the "
        "migration is edited, duplicated or already on main: fix that",
    ),
    (
        "squawk",
        re.compile(r"squawk found migration hazards", re.IGNORECASE),
        "fix the named squawk rule on the new migration SQL",
    ),
    (
        "skill-chunk",
        re.compile(r"exceeds the chunk-size budget", re.IGNORECASE),
        "split the named H2 section(s) under 4000 chars",
    ),
    (
        "skill-size",
        re.compile(r"\bis \d+ bytes \(cap \d+\)", re.IGNORECASE),
        "move detail into a child skill or trim; the cap is in "
        "tests/test_skill_size.py",
    ),
    (
        "skill-vocab",
        re.compile(r"appears? in no skill", re.IGNORECASE),
        "mention the named registry value in the skill that owns it "
        "(tests/test_skill_vocabulary.py)",
    ),
    (
        "conflict",
        # git's own ``CONFLICT (content|add/add|modify/delete|…):`` line
        # or the lander's die phrases — nothing looser: a pytest tail
        # carrying the word "conflict" is not a merge conflict.
        re.compile(
            r"^CONFLICT \(|rebase conflict onto origin/main"
            r"|merge conflict integrating origin/main"
        ),
        "rebase on origin/main and resolve {paths}, keeping main's side "
        "for others' changes",
    ),
    (
        "ruff",
        re.compile(r"\.py:\d+:\d+: [A-Z]{1,4}\d{2,5} |\d+ files? would be reformatted"),
        "fix the named ruff rule (a reformat red just needs ruff format)",
    ),
)

#: Where a conflict's file list hides in the failure text: git's own
#: ``CONFLICT (content): Merge conflict in <path>`` lines, or a bare
#: ``CONFLICT <path>`` as the lander's red reason carries it.
_CONFLICT_PATH_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"Merge conflict in ([\w./-]+)"),
    re.compile(r"^CONFLICT\s+([\w./-]+\.\w+)\s*$", re.MULTILINE),
)

#: Dispatch score of a node with no stamped score on it or any ancestor
#: (:mod:`precis.utils.dispatch_score`, gr604593).
DEFAULT_SCORE = _dscore.DEFAULT_SCORE

#: A reviewer's verdict comment (module docstring, step 2a).
_VERDICT_RE = re.compile(
    r"review:\s*(LAND|FIX|REVERT)\b[\s:]*(?:[—–-]+\s*)?(.*)",
    re.IGNORECASE | re.DOTALL,
)
#: A verdict token for the hand-off fallback (gr573564, coordinator
#: ruling): only LAND or FIX — a REVERT is destructive and is never taken
#: from a token glued into narration; it needs the line-start comment.
_VERDICT_TOKEN_RE = re.compile(r"review:\s*(LAND|FIX)\b", re.IGNORECASE)
#: A placeholder template line (``review: FIX — <list>`` / ``REVERT —
#: <why>``) marks a grammar echo — it suppresses every LAND in the same
#: hand-off.
_VERDICT_TEMPLATE_RE = re.compile(r"review:\s*(?:FIX|REVERT)\b", re.IGNORECASE)
#: The ``[\s:]*`` + optional dashes lead ``_VERDICT_RE`` allows between
#: the verdict word and its rest — applied after a glued token.
_VERDICT_LEAD_RE = re.compile(r"[\s:]*(?:[—–-]+\s*)?")
#: The read-only reviewer's verdict-line marker (gr573555): stripped
#: where it ends a line (optional trailing ``.``, CRLF tolerated) before
#: matching — accepted, never required, and never the findings.
_READONLY_MARKER_RE = re.compile(
    re.escape(_templates.REVIEW_READONLY_MARKER) + r"\.?[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)
#: A ``review:`` verdict token anywhere; :func:`_verdict_lines` keeps the
#: line-start ones — including REVERT, which is only ever a verdict line
#: (never a fallback token). ``_*\b`` lets the closing ``__`` of an
#: underscore emphasis hug the word (``__review: LAND__`` — a bare ``\b``
#: never matches before ``_``, td636385); a glued ``LANDED``/``LAND_x``
#: still misses.
_VERDICT_LINE_RE = re.compile(r"review:\s*(LAND|FIX|REVERT)_*\b", re.IGNORECASE)
#: Sentence-final punctuation a glued token may directly follow.
_SENTENCE_END = frozenset(".!?")
#: Markdown line-start markers a fallback token may directly follow
#: (``- ``/``* `` bullets, ``**``/``~~`` emphasis, ``#`` heading,
#: ``> `` quote — gr568824's bold mid-brief verdict line).
_MD_MARKERS = " \t-*#>_~"
_LANDED_RE = re.compile(r"landed:\s*([0-9a-f]{7,40})\b", re.IGNORECASE)
_TD_FROM_RE = re.compile(r"\s*td(\d+)\s*")
_SHA_FROM_RE = re.compile(r"\s*sha:([0-9a-f]{7,40})\s*", re.IGNORECASE)
#: A ``gr<N>`` token a ``moot:`` verdict pins its claim to (gr602896:
#: one naming a gripe other than the plan's is never this plan's
#: verdict — the S1d rejection, kept for the trusted close).
_GRIPE_TOKEN_RE = re.compile(r"\bgr(\d+)\b", re.IGNORECASE)

#: Doable exclusions the tick honours: the shared registry minus
#: ``child-failed:`` — plan retries are the tick's, not the bubble's —
#: plus ``rescue:``: a node in rescue belongs to the rescue pass, never
#: to a build (td584381 S1a).
_EXCLUSIONS: tuple[str, ...] = tuple(
    t for t in _DOABLE_EXCLUSION_TAGS if t != "child-failed:"
) + ("rescue:",)

#: Executor preference when a job type is compatible with several.
_EXECUTOR_ORDER: tuple[str, ...] = (
    "job_inproc",
    "coordinator",
    "claude_inproc",
    "ssh_node",
)

#: ``meta.ask.context`` cap (``todo.guards._ASK_CONTEXT_CAP``).
_ASK_CONTEXT_CAP = 600
_ASK_OPTIONS: list[str] = [
    "retry with a different lane/model",
    "rewrite the brief",
    "won't do",
]
#: Automatic ``release-fix`` iterations the tick runs unattended
#: (me573231): two red fixes, then the exhausted-loop ask — a third fix
#: never starts by itself. A release-fix loop stamps no ``meta.loop.max``
#: (``nodes.TICK_CAPPED_BODIES``); one an older pass stamped is ignored.
#: The one number is ``templates.RELEASE_FIX_CAP``.
_RELEASE_FIX_AUTO_CAP = _templates.RELEASE_FIX_CAP
_LOOP_ASK_OPTIONS: list[str] = [
    "close this pass and let release_poll start a fresh one",
    "hand release with /go",
]
#: Action lines kept in the job summary.
_NOTES_CAP = 60

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Run the pass read-only: count and report, write nothing.
        "dry_run": {"type": "boolean"},
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "Dark-factory plan tick: evaluate gates, advance loops, act on reviewer "
    "verdicts (LAND/FIX/REVERT), close finished phases, mint one job per "
    "doable plan task in (dispatch_score desc, id) order (run_slice by default), "
    "release stale claims, enter rescue once a lane-run task's retry "
    "ladder is spent, and file an ask-user for a human-only hit or a "
    "missing lane. Read-only while PRECIS_FACTORY=off or service_config "
    "plan_dispatch prio 0."
)


# ── Model ──────────────────────────────────────────────────────────


@dataclass
class _Node:
    id: int
    kind: str  # 'todo' | 'job'
    parent_id: int | None
    title: str
    meta: dict[str, Any]
    status: str
    created_at: datetime
    prio: int | None
    depth: int
    root: int
    #: open tag value -> when it was set (todos and jobs).
    open_tags: dict[str, datetime] = field(default_factory=dict)
    children: list[int] = field(default_factory=list)


@dataclass
class _Blocker:
    id: int
    status: str
    is_ask: bool
    created_at: datetime
    #: ``meta.pending_land`` on the blocker (gr570506): a ``done`` build
    #: whose squash still waits on the pre-land review or whose held
    #: land is in flight — its terminal status does not put the code on
    #: main (gr659981). Read for blockers outside the loaded subtrees,
    #: where :meth:`_Plan._land_held` has no node meta to consult. The
    #: load query also sets it for a REVIEWER blocker — one closes
    #: ``done`` at LAND with no stamp of its own — when the review
    #: targets named by its ``meta.generate.from`` still carry the
    #: stamp, resolved in the store because those targets may sit
    #: outside the loaded subtrees (gr659981 review td660658).
    pending_land: bool = False


@dataclass
class _Plan:
    roots: list[int]
    nodes: dict[int, _Node]
    #: blocked todo id -> its ``blocked-by`` targets.
    blockers: dict[int, list[_Blocker]]

    def todos(self) -> list[_Node]:
        return sorted(
            (n for n in self.nodes.values() if n.kind == "todo"), key=lambda n: n.id
        )

    def child_todos(self, n: _Node) -> list[_Node]:
        return [self.nodes[c] for c in n.children if self.nodes[c].kind == "todo"]

    def jobs(self, n: _Node) -> list[_Node]:
        """Child jobs, oldest first."""
        return sorted(
            (self.nodes[c] for c in n.children if self.nodes[c].kind == "job"),
            key=lambda j: (j.created_at, j.id),
        )

    def live_job(self, n: _Node) -> bool:
        return any(j.status not in _JOB_TERMINAL for j in self.jobs(n))

    def status_of(self, b: _Blocker) -> str:
        node = self.nodes.get(b.id)
        return node.status if node is not None else b.status

    def ancestors(self, n: _Node) -> list[_Node]:
        out: list[_Node] = []
        seen = {n.id}
        cur = self.nodes.get(n.parent_id) if n.parent_id is not None else None
        while cur is not None and cur.id not in seen:
            out.append(cur)
            seen.add(cur.id)
            cur = self.nodes.get(cur.parent_id) if cur.parent_id is not None else None
        return out

    def _land_held(self, b: _Blocker) -> bool:
        """The blocker's code is not on main yet: ``meta.pending_land``
        on it — a build closed ``done`` with its squash held for the
        pre-land review, or that held land still in flight (gr659981) —
        or, the blocker being the review itself, on its targets (the
        reviewer closes ``done`` at LAND while the in-flight land has
        not recorded). The load-time :attr:`_Blocker.pending_land` is
        honored on the loaded path too: the load query resolves it in
        the store, so it sees a reviewer's held targets that sit
        outside the loaded subtrees — invisible to the in-memory
        :func:`_pending_targets` (gr659981 review td660658)."""
        node = self.nodes.get(b.id)
        if node is None:
            return b.pending_land
        if (
            isinstance(node.meta.get(_templates.PENDING_LAND_KEY), dict)
            or b.pending_land
        ):
            return True
        return _is_reviewer(node) and bool(_pending_targets(self, node))

    def blocked(self, n: _Node) -> bool:
        """A live ``blocked-by`` target on ``n`` or any ancestor in the
        plan — live meaning open, or terminal with its land still held
        (:meth:`_land_held`): the dependant's worktree must cut from a
        main carrying the build, so ``done`` alone never releases it
        (gr659981). The one exemption is ``n`` itself being the pre-land
        reviewer gating that land (:func:`_is_its_land_gate`): its LAND
        verdict is what mints the held ``land_slice``, so holding it
        would deadlock the chain."""
        for node in (n, *self.ancestors(n)):
            for b in self.blockers.get(node.id, []):
                if self.status_of(b) not in BLOCKER_TERMINAL_STATUSES:
                    return True
                if self._land_held(b) and not _is_its_land_gate(self, n, b):
                    return True
        return False

    def paused(self, n: _Node) -> bool:
        return any(a.status == "paused" for a in (n, *self.ancestors(n)))

    def open_asks(self, n: _Node) -> list[_Blocker]:
        return [
            b
            for b in self.blockers.get(n.id, [])
            if b.is_ask and self.status_of(b) not in BLOCKER_TERMINAL_STATUSES
        ]


def _closed(n: _Node) -> bool:
    return n.status in TERMINAL_TODO_STATUSES


def _dead_root_orphans(store: Any, live_ids: set[int]) -> list[tuple[int, int, str]]:
    """``(todo id, dead root id, its STATUS)`` of every open todo under a
    terminal plan root that no live plan owns (gr612091).

    Rootlikes are ``_candidate_roots``' two shapes — the self
    ``plan:<id>`` tag or plan-kind meta — held at ANY status. Descent
    starts at every terminal rootlike that is itself outside every live
    plan (``live_ids``, the loaded plans' node ids — a dead phase
    mid-plan belongs to its live plan, not to the sweep) and takes the
    WHOLE subtree: a live rootlike inside — a gate, a loop, a nested
    plan root — dies with the outer plan, it is only self-rooted, not
    live-owned. ``None``-ish statuses read as ``open``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta,
                   COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open'),
                   EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                        AND t.value = 'plan:' || r.ref_id
                   )
              FROM refs r
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND (EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                        AND t.value = 'plan:' || r.ref_id)
                    OR r.meta ?| array['gate','loop','generate','auto_close'])
            """,
        ).fetchall()
    dead = sorted(
        int(rid)
        for rid, meta, status, self_tag in rows
        if (self_tag or node_kind(dict(meta or {})) not in (None, "task"))
        and str(status) in TERMINAL_TODO_STATUSES
        and int(rid) not in live_ids
    )
    if not dead:
        return []
    with store.pool.connection() as conn:
        orphans = conn.execute(
            """
            WITH RECURSIVE down(ref_id, root, lvl) AS (
                SELECT r.ref_id, r.ref_id, 0 FROM refs r
                 WHERE r.ref_id = ANY(%s)
                UNION ALL
                SELECT c.ref_id, d.root, d.lvl + 1
                  FROM refs c JOIN down d ON c.parent_id = d.ref_id
                 WHERE c.retired_at IS NULL AND c.kind = 'todo' AND d.lvl < 25
            )
            SELECT d.ref_id, d.root,
                   COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                       WHERE rt.ref_id = d.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open')
              FROM down d
             WHERE d.ref_id <> d.root
             ORDER BY d.ref_id
            """,
            (dead,),
        ).fetchall()
    return [
        (int(rid), int(root), str(status or "open"))
        for rid, root, status in orphans
        if str(status or "open") not in TERMINAL_TODO_STATUSES
    ]


def _live_job_parents(store: Any, ids: list[int]) -> set[int]:
    """The todos in ``ids`` with at least one live child job."""
    if not ids:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT j.parent_id FROM refs j
             WHERE j.kind = 'job' AND j.retired_at IS NULL
               AND j.parent_id = ANY(%s)
               AND NOT EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                        AND t.value = ANY(%s))
            """,
            (list(ids), sorted(_JOB_TERMINAL)),
        ).fetchall()
    return {int(r[0]) for r in rows if r[0] is not None}


def _step_dead_roots(store: Any, plan: _Plan, w: _Writer, res: PassResult) -> None:
    """Close a terminal plan root's open children ``won't-do`` (gr612091,
    module docstring step 0): a won't-do'd or otherwise closed root is
    skipped by ``_candidate_roots``, so without the sweep its open
    children stay open forever — a release pass's approval ask still
    offered ``deploy r<N>`` after the pass died. A node with a live job
    defers to the next tick (the moot close's rule); dry_run counts and
    writes nothing. A victim that is also a loaded node — a plan nested
    inside the dead subtree dies with it, and its nodes sit in
    ``plan.nodes`` — is closed through that ``_Node``, not a stand-in:
    the loaded copy must carry the close or the tick's later steps
    re-judge it (a gate's ``done`` overwriting the won't-do, a loop
    re-minting, a leaf dispatching; td612465)."""
    victims = _dead_root_orphans(store, set(plan.nodes))
    if not victims:
        return
    busy = _live_job_parents(store, [v[0] for v in victims])
    closed = 0
    closed_ids: set[int] = set()
    for rid, root, status in victims:
        if rid in busy:
            res.notes.append(f"td{rid} has a live job; dead-root close deferred")
            continue
        n = plan.nodes.get(rid)
        if n is None:
            n = _Node(
                id=rid,
                kind="todo",
                parent_id=None,
                title="",
                meta={},
                status=status,
                created_at=datetime.now(UTC),
                prio=None,
                depth=0,
                root=root,
            )
        w.comment(
            n,
            f"plan_dispatch: plan closed — root td{root} is closed; "
            "this node goes won't-do with it",
        )
        w.set_status(n, "won't-do")
        closed += 1
        closed_ids.add(rid)
        res.notes.append(f"td{rid}: closed won't-do — plan root td{root} is closed")
    res.phases_closed += closed
    # A closed victim riding a live node's ``blocked-by`` list as an
    # unloaded target would read open to ``status_of`` for the rest of
    # the tick (the loaded-node case is mirrored by ``set_status``
    # above) — keep the snapshot truthful.
    for targets in plan.blockers.values():
        for b in targets:
            if b.id in closed_ids:
                b.status = "won't-do"


def _candidate_roots(conn: Any) -> list[int]:
    """Live, non-closed todos that root a plan (see the module docstring)."""
    ids: set[int] = set()
    for (value,) in conn.execute(
        """
        SELECT DISTINCT t.value
          FROM ref_tags rt JOIN tags t USING (tag_id)
          JOIN refs r ON r.ref_id = rt.ref_id
         WHERE t.namespace = 'OPEN' AND t.value LIKE 'plan:%%'
           AND r.kind = 'todo' AND r.retired_at IS NULL
        """
    ).fetchall():
        tail = str(value)[5:]
        if tail.isdigit():
            ids.add(int(tail))
    for ref_id, meta in conn.execute(
        """
        SELECT ref_id, meta FROM refs
         WHERE kind = 'todo' AND retired_at IS NULL
           AND meta ?| array['gate', 'loop', 'generate', 'auto_close']
        """
    ).fetchall():
        if node_kind(meta) not in (None, "task"):
            ids.add(int(ref_id))
    if not ids:
        return []
    rows = conn.execute(
        """
        SELECT r.ref_id FROM refs r
         WHERE r.ref_id = ANY(%s) AND r.kind = 'todo' AND r.retired_at IS NULL
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                 'open'
               ) <> ALL(%s)
         ORDER BY r.ref_id
        """,
        (sorted(ids), sorted(TERMINAL_TODO_STATUSES)),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _load(store: Any, roots: list[int] | None = None) -> _Plan:
    """The in-memory model of every plan subtree — or, ``roots`` given,
    of just those."""
    with store.pool.connection() as conn:
        candidates = _candidate_roots(conn) if roots is None else roots
    subtrees = {root: _fetch_subtree(store, root) for root in candidates}
    contained: set[int] = set()
    for root, rows in subtrees.items():
        contained.update(r["id"] for r in rows if r["id"] != root)
    roots = [r for r in candidates if r not in contained]
    placement: dict[int, tuple[int, int]] = {}  # id -> (root, depth)
    for root in roots:
        for row in subtrees[root]:
            placement.setdefault(row["id"], (root, int(row["depth"])))
    if not placement:
        return _Plan(roots=roots, nodes={}, blockers={})
    ids = sorted(placement)
    with store.pool.connection() as conn:
        ref_rows = conn.execute(
            """
            SELECT r.ref_id, r.kind, r.parent_id, r.title, r.meta, r.created_at,
                   r.prio,
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1)
              FROM refs r
             WHERE r.ref_id = ANY(%s) AND r.retired_at IS NULL
            """,
            (ids,),
        ).fetchall()
        tag_rows = conn.execute(
            """
            SELECT rt.ref_id, t.value, rt.created_at
              FROM ref_tags rt JOIN tags t USING (tag_id)
             WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN'
            """,
            (ids,),
        ).fetchall()
        link_rows = conn.execute(
            """
            SELECT l.src_ref_id, b.ref_id, b.created_at, b.meta ? 'ask',
                   EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = b.ref_id AND t.namespace = 'OPEN'
                              AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')),
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                   (
                     jsonb_typeof(b.meta->'pending_land') = 'object'
                     OR (
                       b.meta->'generate'->>'role' = 'reviewer'
                       AND EXISTS (
                         SELECT 1
                           FROM refs t
                          WHERE t.retired_at IS NULL
                            AND t.kind = 'todo'
                            AND jsonb_typeof(t.meta->'pending_land') = 'object'
                            AND (
                              (
                                t.ref_id::text = substring(
                                  b.meta->'generate'->>'from'
                                  from '^[[:space:]]*td([0-9]+)[[:space:]]*$')
                                AND NOT EXISTS (
                                  SELECT 1 FROM refs c
                                   WHERE c.parent_id = t.ref_id
                                     AND c.retired_at IS NULL
                                     AND c.kind = 'todo'
                                     AND NOT COALESCE(c.meta ? 'ask', FALSE)
                                )
                              )
                              OR (
                                t.parent_id::text = substring(
                                  b.meta->'generate'->>'from'
                                  from '^[[:space:]]*td([0-9]+)[[:space:]]*$')
                                AND NOT COALESCE(t.meta ? 'ask', FALSE)
                              )
                            )
                       )
                     )
                   ) AS pending_land
              FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE l.relation = 'blocked-by' AND l.src_ref_id = ANY(%s)
               AND b.retired_at IS NULL
            """,
            (ids,),
        ).fetchall()
    nodes: dict[int, _Node] = {}
    for ref_id, kind, parent_id, title, meta, created_at, prio, status in ref_rows:
        root, depth = placement[int(ref_id)]
        nodes[int(ref_id)] = _Node(
            id=int(ref_id),
            kind=str(kind),
            parent_id=int(parent_id) if parent_id is not None else None,
            title=str(title or ""),
            meta=dict(meta or {}),
            status=str(status or ("open" if kind == "todo" else "queued")),
            created_at=created_at,
            prio=int(prio) if prio is not None else None,
            depth=depth,
            root=root,
        )
    for ref_id, value, set_at in tag_rows:
        node = nodes.get(int(ref_id))
        if node is not None:
            node.open_tags[str(value)] = set_at
    for node in nodes.values():
        if node.parent_id is not None and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node.id)
    blockers: dict[int, list[_Blocker]] = {}
    for src, dst, created_at, has_ask, ask_tag, status, pending_land in link_rows:
        blockers.setdefault(int(src), []).append(
            _Blocker(
                id=int(dst),
                status=str(status or "open"),
                is_ask=bool(has_ask) or bool(ask_tag),
                created_at=created_at,
                pending_land=bool(pending_land),
            )
        )
    return _Plan(roots=roots, nodes=nodes, blockers=blockers)


# ── Writes (no-ops when read-only) ─────────────────────────────────


@dataclass
class PassResult:
    """What one pass did (or, read-only, would have done)."""

    dry_run: bool
    reason: str | None = None
    roots: int = 0
    gates_opened: int = 0
    gates_closed: int = 0
    loops_minted: int = 0
    loops_closed: int = 0
    phases_closed: int = 0
    dispatched: int = 0
    released: int = 0
    asks: int = 0
    #: Ladder-exhausted ``run_slice`` nodes handed to the rescue path
    #: (td584381 S1a).
    rescued: int = 0
    reopened: int = 0
    #: Reviewer verdicts acted on (LAND / FIX / REVERT).
    reviews: int = 0
    #: Why the pass did nothing (another tick held the lock); ``None`` = ran.
    skipped: str | None = None
    notes: list[str] = field(default_factory=list)
    #: Notes the tick job also records as ``job_result`` lines — a
    #: ``release_cut`` refusal, once per change (gr651885).
    logged: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "roots": self.roots,
            "gates_opened": self.gates_opened,
            "gates_closed": self.gates_closed,
            "loops_minted": self.loops_minted,
            "loops_closed": self.loops_closed,
            "phases_closed": self.phases_closed,
            "dispatched": self.dispatched,
            "released": self.released,
            "asks": self.asks,
            "rescued": self.rescued,
            "dry_run": self.dry_run,
            "reopened": self.reopened,
            "reviews": self.reviews,
        }
        if self.skipped:
            out["skipped"] = self.skipped
        return out

    def summary(self) -> str:
        if self.skipped:
            return f"plan_dispatch: skipped — {self.skipped}"
        return (
            f"plan_dispatch: roots={self.roots} "
            f"gates={self.gates_opened}/{self.gates_closed} "
            f"loops={self.loops_minted}/{self.loops_closed} "
            f"phases={self.phases_closed} dispatched={self.dispatched} "
            f"released={self.released} asks={self.asks} dry_run={self.dry_run} "
            f"reopened={self.reopened} reviews={self.reviews}"
        )

    def render(self) -> str:
        lines = [self.summary()]
        if self.reason:
            lines.append(f"read-only ({self.reason}): the counts are what it would do")
        shown = self.notes[:_NOTES_CAP]
        lines += [f"- {n}" for n in shown]
        if len(self.notes) > len(shown):
            lines.append(f"- … {len(self.notes) - len(shown)} more")
        return "\n".join(lines)


class _Writer:
    """Store writes for one pass; every method is a no-op when ``dry_run``."""

    def __init__(self, store: Any, dry_run: bool) -> None:
        self.store = store
        self.dry_run = dry_run

    def set_status(self, n: _Node, value: str) -> None:
        n.status = value
        if self.dry_run:
            return
        self.store.add_tag(
            n.id, Tag.closed("STATUS", value), set_by="system", replace_prefix=True
        )
        if value == "done":
            # Picks-7d accounting, as the todo handler's tag path writes it.
            self.store.append_event(n.id, source=NAME, event="status:done")

    def stamp_meta(self, n: _Node, updates: dict[str, Any]) -> None:
        n.meta.update(updates)
        if not self.dry_run:
            self.store.stamp_ref_meta(n.id, updates)

    def remove_tags(self, n: _Node, values: list[str]) -> None:
        for value in values:
            n.open_tags.pop(value, None)
            if not self.dry_run:
                self.store.remove_tag(n.id, Tag.open(value))

    def add_tag(self, n: _Node, value: str, at: datetime) -> None:
        """One open tag, mirrored on the in-memory node at ``at`` (its
        ``created_at`` once the write lands — the hold steps age tags off
        that clock)."""
        n.open_tags[value] = at
        if not self.dry_run:
            self.store.add_tag(n.id, Tag.open(value), set_by="system")

    def add_tags(self, n: _Node, values: list[str]) -> None:
        for value in values:
            n.open_tags.setdefault(value, datetime.now(UTC))
            if not self.dry_run:
                self.store.add_tag(n.id, Tag.open(value), set_by="system")

    def comment(self, n: _Node, text: str) -> None:
        if self.dry_run:
            return
        existing = list(self.store.chunks.list_chunks_for_ref(n.id))
        ord_ = max((int(c.ord) for c in existing), default=0) + 1
        self.store.chunks.insert_chunks(
            n.id,
            [
                ChunkInsert(
                    ord=ord_,
                    text=text,
                    meta={"chunk_kind": _COMMENT_KIND, **worker_comment_meta()},
                )
            ],
        )


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── (1) gates ──────────────────────────────────────────────────────


def _step_gates(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    for n in plan.todos():
        if node_kind(n.meta) != "gate" or _closed(n) or plan.paused(n):
            continue
        if plan.blocked(n):
            continue
        if not w.dry_run and (n.meta.get("ask") or {}).get("kind") == "approval":
            # The standing approval: the tick may post the 'deploy r<N>'
            # answer itself — before the answer gate below evaluates, so
            # the same tick can open it.
            _answered, note = _release.auto_answer_approval(store, n.id, now=now)
            if note:
                res.notes.append(note)
        verdict = _gates.evaluate(store, n.meta["gate"], todo_id=n.id, now=now)
        prev = n.meta.get("gate_result") or {}
        if not isinstance(prev, dict) or (prev.get("open"), prev.get("why")) != (
            verdict.open,
            verdict.why,
        ):
            w.stamp_meta(
                n,
                {
                    "gate_result": {
                        "open": verdict.open,
                        "why": verdict.why,
                        "at": _iso(now),
                    }
                },
            )
        if verdict.open:
            w.set_status(n, "done")
            res.gates_opened += 1
            res.notes.append(f"gate td{n.id} open: {verdict.why}")
        else:
            res.gates_closed += 1


# ── (2) loops ──────────────────────────────────────────────────────


def _close_loop(w: _Writer, res: PassResult, n: _Node, why: str) -> None:
    w.comment(n, f"plan_dispatch: loop closed — {why}")
    w.set_status(n, "done")
    res.loops_closed += 1
    res.notes.append(f"loop td{n.id} closed: {why}")


def _step_loops(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    for n in plan.todos():
        if node_kind(n.meta) != "loop" or _closed(n) or plan.paused(n):
            continue
        if plan.blocked(n):
            continue
        # A ``waiting-for:`` tag parks the loop itself — neither re-mint
        # nor close while it waits (the reach the phases step gives the
        # same tag on a phase or generator, td610546).
        if any(t.startswith("waiting-for:") for t in n.open_tags):
            continue
        # An informational ask (``blocks: "nothing"``) is a record, not a
        # gate: it does not hold the loop open (gr589823).
        if not all(_closed(c) or _informational_ask(c) for c in plan.child_todos(n)):
            continue
        spec = dict(n.meta.get("loop") or {})
        try:
            count = int(spec.get("count") or 0)
            cap = int(spec.get("max") or 0)
        except (TypeError, ValueError):
            _close_loop(w, res, n, f"meta.loop count/max unreadable: {spec!r}")
            continue
        body = spec.get("body")
        if body == "release-fix":
            # me573231: the automatic cap governs, not the stamped max.
            cap = _RELEASE_FIX_AUTO_CAP
        until = _gates.evaluate(store, spec.get("until") or {}, todo_id=n.id, now=now)
        if until.open:
            _close_loop(w, res, n, f"until is met ({until.why})")
            continue
        if count >= cap:
            if body == "release-fix" and not spec.get("exhausted_ask"):
                if w.dry_run:
                    res.notes.append(
                        f"loop td{n.id}: would file the exhausted release-fix ask"
                    )
                else:
                    ask_id = _mint_loop_ask(store, n, count, until.why)
                    w.stamp_meta(n, {"loop": {**spec, "exhausted_ask": ask_id}})
                    res.notes.append(
                        f"loop td{n.id}: {count} red release-fix iteration(s), "
                        f"asked in td{ask_id}"
                    )
                res.asks += 1
            _close_loop(w, res, n, f"max {cap} iteration(s) reached ({until.why})")
            continue
        if not isinstance(body, str) or body not in _templates.BODIES:
            _close_loop(
                w,
                res,
                n,
                f"loop body {body!r} has no minter in "
                f"precis.plans.templates.BODIES (known: {sorted(_templates.BODIES)}); "
                "register it, then reopen this loop",
            )
            continue
        if not w.dry_run:
            try:
                _templates.mint_body(store, n.id, body, iteration=count + 1)
            except BadInput as exc:
                _close_loop(w, res, n, f"loop body {body!r} refused: {exc.cause}")
                continue
        w.stamp_meta(n, {"loop": {**spec, "count": count + 1}})
        res.loops_minted += 1
        res.notes.append(f"loop td{n.id}: minted {body} iteration {count + 1}/{cap}")


# ── (2a) reviews ───────────────────────────────────────────────────


def _is_reviewer(n: _Node) -> bool:
    gen = n.meta.get("generate")
    return (
        node_kind(n.meta) == "generator"
        and isinstance(gen, dict)
        and gen.get("role") == "reviewer"
    )


def _review_from(n: _Node) -> int | None:
    """The ``td<id>`` a reviewer's ``meta.generate.from`` names."""
    m = _TD_FROM_RE.fullmatch(str((n.meta.get("generate") or {}).get("from") or ""))
    return int(m.group(1)) if m else None


def _review_sha(n: _Node) -> str | None:
    """The sha a sha-keyed reviewer's ``meta.generate.from`` names — a
    hand-landed engine commit with no linked todo (gr597625), ``None``
    for a todo-keyed reviewer."""
    m = _SHA_FROM_RE.fullmatch(str((n.meta.get("generate") or {}).get("from") or ""))
    return m.group(1).lower() if m else None


def _review_targets(plan: _Plan, n: _Node) -> list[_Node]:
    """The nodes whose land the reviewer judges: ``from`` itself, or its
    child todos (asks excluded) when it has them (a slicer's slices)."""
    target_id = _review_from(n)
    target = plan.nodes.get(target_id) if target_id is not None else None
    if target is None or target.kind != "todo":
        return []
    kids = [c for c in plan.child_todos(target) if "ask" not in c.meta]
    return kids or [target]


def _builder_vendors(
    plan: _Plan, n: _Node, lanes: tuple[_lanes.Lane, ...]
) -> tuple[str, ...]:
    """The vendors whose lanes ran a build job on the reviewed nodes, oldest
    job first (so ``[0]`` is the original builder's), each once.

    Every one of them is excluded from the review, not only the newest
    job's: td565332 (2026-11-05) read the newest job — sonnet's lint repair
    on top of codex's eight-file fix — as anthropic, and the "other vendor"
    pick sent the review to codex, which then reviewed its own build.

    Only jobs that left commits on the branch count (``run_slice.poll``
    stamps ``meta.branch_commits``): a claude-sub attempt that died with no
    commit before a codex build is not a builder, and counting it would make
    codex the "original builder"'s alternative and review its own work
    (BUILDER 2026-11-05). When no job carries the stamp (jobs before the
    field, or a reviewed node whose land came another way) every laned job
    counts."""
    laned: list[tuple[int, str, bool]] = []
    for target in _review_targets(plan, n):
        for j in plan.jobs(target):
            lane_name = j.meta.get("lane")
            if not lane_name:
                continue
            lane = next((ln for ln in lanes if ln.name == lane_name), None)
            if lane is None or not lane.vendor:
                continue
            laned.append((j.id, lane.vendor, bool(j.meta.get("branch_commits"))))
    builders = [row for row in laned if row[2]] or laned
    out: list[str] = []
    for _id, vendor, _committed in sorted(builders):
        if vendor not in out:
            out.append(vendor)
    return tuple(out)


def _comments(store: Any, todo_id: int) -> list[Any]:
    """The todo's comment chunks, newest first."""
    return sorted(
        (
            c
            for c in store.chunks.list_chunks_for_ref(todo_id)
            if c.chunk_kind == _COMMENT_KIND and c.text
        ),
        key=lambda c: (int(c.ord), int(c.id)),
        reverse=True,
    )


def _token_boundary(text: str, start: int) -> bool:
    """A fallback token counts only at a line start or directly after
    sentence-final punctuation (``…main.review: FIX — …``); a token
    mid-sentence (``I will post review: LAND if…``) is narration. Markdown
    line-start markers — ``- ``/``* `` bullets, ``**`` bold, ``#``
    headings, ``> `` quotes — may sit between the line start and the
    token."""
    i = start - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    if i < 0 or text[i] == "\n" or text[i] in _SENTENCE_END:
        return True
    j = i
    while j >= 0 and text[j] in _MD_MARKERS:
        j -= 1
    return j < 0 or text[j] == "\n"


def _token_enclosed(text: str, start: int) -> bool:
    """Inside backticks or parens the token quotes the grammar (the
    brief's ``review: LAND | review: FIX — <list>`` echo), it is not a
    verdict."""
    head = text[:start]
    return bool(head.count("`") % 2) or head.count("(") > head.count(")")


def _verdict_template(body: str) -> bool:
    """``body`` holds a placeholder ``review: FIX|REVERT — <…>`` template
    line — the grammar echoed back, which suppresses every LAND in the
    same hand-off."""
    for m in _VERDICT_TEMPLATE_RE.finditer(body):
        lead = _VERDICT_LEAD_RE.match(body, m.end())
        if body[lead.end() if lead else m.end() :].strip().startswith("<"):
            return True
    return False


def _verdict_candidates(body: str) -> list[tuple[str, str]]:
    """The surviving fallback ``(verdict, rest)`` tokens in ``body``, in
    order — the strict set the coordinator ruled on (a wrong LAND hides a
    bug, a wrong REVERT is destructive)."""
    out: list[tuple[str, str]] = []
    suppress_land = _verdict_template(body)
    for m in _VERDICT_TOKEN_RE.finditer(body):
        if not _token_boundary(body, m.start()) or _token_enclosed(body, m.start()):
            continue
        lead = _VERDICT_LEAD_RE.match(body, m.end())
        rest = body[lead.end() if lead else m.end() :].strip()
        if rest.startswith("<"):  # a grammar placeholder, not findings
            continue
        if m.group(1).upper() == "LAND" and (
            suppress_land
            or rest.lower().startswith("(clean")  # the grammar's own gloss
            or rest.startswith("|")  # echo of the ``A | B`` grammar line
        ):
            continue
        out.append((m.group(1).upper(), rest))
    return out


def _verdict_lines(body: str) -> list[tuple[str, str]]:
    """The ``(verdict word, rest)`` pairs of ``body``'s verdict LINES — a
    ``review:`` token at a line start, markdown ``- ``/``* ``/``**``/``#``/
    ``> `` markers allowed before it (the line-start half of
    :func:`_token_boundary`), outside backticks/parens like a fallback
    token (:func:`_token_enclosed`): inside a multiline span the token
    quotes the grammar, and an enclosed REVERT must never turn a real
    line into a disagreement (td591267). ``rest`` is that line's own text after the
    word and its lead, to end of line. A token glued mid-line after
    sentence-final punctuation is narration here, not a verdict line
    (gr574380)."""
    out: list[tuple[str, str]] = []
    for m in _VERDICT_LINE_RE.finditer(body):
        i = m.start() - 1
        while i >= 0 and body[i] in _MD_MARKERS:
            i -= 1
        if (i < 0 or body[i] == "\n") and not _token_enclosed(body, m.start()):
            end = body.find("\n", m.end())
            line = body[m.end() : end if end >= 0 else len(body)]
            lead = _VERDICT_LEAD_RE.match(line)
            rest = line[lead.end() if lead else 0 :].strip()
            # ``**review: FIX — x**``: the emphasis run that opened the
            # token closes the line — strip it (gr568824).
            prefix = body[i + 1 : m.start()].rstrip()
            emph = prefix[len(prefix.rstrip("*_~")) :]
            if emph:
                rest = rest.removesuffix(emph).rstrip()
            out.append((m.group(1).upper(), rest))
    return out


def parse_verdict(text: str) -> tuple[str, str] | None:
    """``(LAND|FIX|REVERT, rest)`` of one ``review:`` line, or ``None``
    when it is not a verdict. ``FIX`` and ``REVERT`` need a non-empty rest
    (the list / the why); a ``handoff:`` prefix is stripped first. Verdict
    lines that disagree are malformed — ``None``, a retried review, never
    a strictest pick (td636385); a placeholder ``<…>`` line among them is
    a grammar echo and just as malformed — a later ``review:`` line is
    never swallowed into
    the rest (gr574380); lines that agree keep it. A single verdict
    line ANYWHERE in the message — markdown emphasis stripped — is that
    verdict (gr568824: the bold mid-brief ``**review: FIX — …**``).
    When the text as a whole is no verdict line — a hand-off that glues
    the token to its narration (``… on landed main.review: FIX — …``,
    gr573564) — the fallback's surviving candidates rule: a FIX with
    findings wins over a LAND; REVERT never comes from a token; anything
    else is a miss."""
    body = text.strip()
    if body.lower().startswith("handoff:"):
        body = body[len("handoff:") :].strip()
    body = _READONLY_MARKER_RE.sub("", body)
    lines = _verdict_lines(body)
    words = {word for word, _ in lines}
    if len(words) > 1:
        # Disagreeing verdict lines are malformed — the review retries,
        # never resolved to a pick (td636385); a ``<…>`` placeholder
        # among them is a grammar echo and just as malformed.
        return None
    m = _VERDICT_RE.fullmatch(body)
    if m is not None:
        verdict, rest = m.group(1).upper(), m.group(2).strip()
        if verdict != "LAND" and not rest:
            return None
        return verdict, rest
    # A single verdict LINE anywhere in the message is that verdict —
    # the bold mid-brief ``**review: FIX — …**`` (gr568824), emphasis
    # already stripped by :func:`_verdict_lines`; a placeholder ``<…>``
    # rest is the grammar echoed, not findings, and a LAND glossed
    # ``(clean`` or continued ``|`` the same — all fall through.
    if len(words) == 1:
        word = next(iter(words))
        suppress_land = _verdict_template(body)
        for w, r in reversed(lines):
            if w != word or r.startswith("<"):
                continue
            if word == "LAND" and (
                suppress_land or r.lower().startswith("(clean") or r.startswith("|")
            ):
                continue
            if r or word == "LAND":
                return word, r
    candidates = _verdict_candidates(body)
    fixes = [rest for verdict, rest in candidates if verdict == "FIX" and rest]
    if fixes:
        return "FIX", fixes[-1]
    if candidates and all(verdict == "LAND" for verdict, _ in candidates):
        return "LAND", candidates[-1][1]
    return None


def _is_review_line(text: str) -> bool:
    """A comment the verdict is read from: one starting ``review:``, or a
    ``handoff:`` whose own body starts ``review:`` or carries a surviving
    fallback token (gr573564). Any other comment is never scanned — the
    dispatcher's own retry note quotes the grammar."""
    body = text.strip().lower()
    if body.startswith("review:"):
        return True
    if not body.startswith("handoff:"):
        return False
    inner = body[len("handoff:") :].strip()
    return (
        inner.startswith("review:")
        # A mid-message verdict LINE incl. REVERT (gr568824) — a
        # ``<…>`` placeholder line is the grammar echoed, not one.
        or any(not r.startswith("<") for _, r in _verdict_lines(inner))
        or bool(_verdict_candidates(inner))
    )


def _read_verdict(
    store: Any, todo_id: int, since: datetime
) -> tuple[tuple[str, str] | None, str | None]:
    """``(verdict, raw line)`` from the comments posted at or after
    ``since``; ``(None, None)`` when there is none, ``(None, line)`` when
    it is malformed. A comment STARTING ``review:`` — the reviewer's own
    verdict — always wins over a token glued inside a ``handoff:``, newest
    first within each class."""
    comments: list[Any] = []
    for c in _comments(store, todo_id):
        if c.created_at < since:
            break
        comments.append(c)
    for c in comments:
        text = str(c.text)
        if text.strip().lower().startswith("review:"):
            return parse_verdict(text), text.strip()
    for c in comments:
        text = str(c.text)
        if _is_review_line(text):
            return parse_verdict(text), text.strip()
    return None, None


def _landed_sha(store: Any, todo_id: int) -> str | None:
    """The sha of the todo's newest ``landed: <sha>`` comment."""
    for c in _comments(store, todo_id):
        m = _LANDED_RE.match(str(c.text).strip())
        if m:
            return m.group(1).lower()
    return None


def _first_line(text: str, cap: int = 120) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= cap else line[: cap - 1] + "…"


def _mint_task(
    store: Any, plan: _Plan, w: _Writer, root: int, title: str, body: str, now: datetime
) -> int:
    """Mint a ``run_slice`` task under the plan root (read-only: nothing) and
    add it to the in-memory plan, so the root phase does not close over it
    this pass. Returns its id (a negative stand-in when read-only)."""
    if w.dry_run:
        new_id = -1 - len([i for i in plan.nodes if i < 0])
    else:
        new_id = _templates.mint_plan_task(store, root, title, body)
    parent = plan.nodes.get(root)
    plan.nodes[new_id] = _Node(
        id=new_id,
        kind="todo",
        parent_id=root,
        title=title,
        meta={"op": DEFAULT_OP},
        status="open",
        created_at=now,
        prio=None,
        depth=(parent.depth + 1) if parent is not None else 1,
        root=root,
        open_tags={f"plan:{root}": now},
    )
    if parent is not None:
        parent.children.append(new_id)
    return new_id


def _fix_minter(plan: _Plan, todo_id: int) -> _Node | None:
    """The reviewer whose recorded FIX verdict minted ``todo_id`` — the
    ``meta.review.task`` backlink ``_step_reviews`` stamps — else
    ``None`` (``todo_id`` was not born a review fix)."""
    for m in plan.todos():
        rec = m.meta.get("review")
        if (
            _is_reviewer(m)
            and isinstance(rec, dict)
            and rec.get("verdict") == "FIX"
            and rec.get("task") == todo_id
        ):
            return m
    return None


def _fix_depth(plan: _Plan, n: _Node) -> int:
    """The FIX-chain generation of the fix node the reviewer ``n`` is
    about to mint (gr604629): the reviewed todo's own
    ``meta.fix_depth`` plus one — the counter rides the fix node's meta
    from its mint on, so the dispatcher never re-walks the
    ``meta.review.task`` backlinks. 1 when the ``generate.from`` target
    was not minted a review fix."""
    target_id = _review_from(n)
    target = plan.nodes.get(target_id) if target_id is not None else None
    prev = target.meta.get(FIX_DEPTH_KEY) if target is not None else None
    if isinstance(prev, bool) or not isinstance(prev, int):
        return 1
    return prev + 1


def _fix_lineage(plan: _Plan, n: _Node) -> list[int]:
    """The ``generate.from`` target ids of ``n``'s review→fix→land→review
    chain, oldest first — walked through the ``meta.review.task``
    backlinks for the ask and comment text that names the chain (the
    depth counter itself lives on the fix nodes' meta:
    :func:`_fix_depth`)."""
    lineage: list[int] = []
    cur = _review_from(n)
    seen: set[int] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        lineage.append(cur)
        minter = _fix_minter(plan, cur)
        if minter is None:
            break
        cur = _review_from(minter)
    lineage.reverse()
    return lineage


def _mint_brief_ask(store: Any, n: _Node, depth: int, lineage: str) -> int:
    """The design-call ask a held fix node files under the plan root
    (gr603118): the node waits on the ``waiting-for:brief`` TAG, not on
    this ask — no ``blocked-by`` edge, or removing the tag alone could
    never release it. Insert shape of :func:`_mint_orphan_ask`; the
    fix's ``meta.brief_wait.ask`` is the step's lookup."""
    context = (
        f"fix chain depth {depth} on {lineage}: the next fix waits for a "
        f"design call before a slice starts (fix td{n.id})"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_BRIEF_ASK_OPTIONS),
            "blocks": f"td{n.id}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    title = f"td{n.id}: design call needed — chain depth {depth} on {lineage}"
    body = (
        f"The review chain on {lineage} is {depth} FIXes deep on the same "
        "land lineage: each fix patched the previous reviewer's latest "
        "hole and the next reviewer found the next one — a design call "
        "is needed, and the only channel for it (a `brief:` comment on "
        "the fix node) never reaches a slice that dispatches at mint.\n\n"
        f"td{n.id} ({n.title.splitlines()[0] if n.title else ''}) is held "
        "on `waiting-for:brief` — no job mints while the tag is on. "
        "Either:\n"
        f"- post a `brief:` comment on td{n.id} carrying the design call "
        "and remove its `waiting-for:brief` tag — the next tick "
        "dispatches it, or\n"
        "- answer `continue` on this ask and close it — the ladder tries "
        "again with no brief.\n\n"
        f"Sixty minutes of silence releases the hold too: the ladder "
        "continues and this ask closes auto-timeout. To drop the fix "
        f"altogether, close td{n.id} STATUS:won't-do (this ask then "
        "closes moot)."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _chain_item(store: Any, plan: _Plan, todo_id: int) -> str:
    """One ``- td<id> <title> — landed: <sha>`` item of the cap ask's
    collected chain, followed by the item's COMPLETE text — a review
    fix's body is ``<findings>`` plus the "Findings of the review …"
    trailer, and the coordinator's one brief needs every finding, not a
    120-char head (td610546); a node carrying no ``meta.fix_depth`` was
    never minted a review fix, so its body shows as the brief."""
    node = plan.nodes.get(todo_id)
    title = node.title.splitlines()[0][:80] if node is not None and node.title else ""
    findings = ""
    for c in store.chunks.list_chunks_for_ref(todo_id):
        if c.chunk_kind == TODO_BODY_KIND and str(c.text or "").strip():
            findings = str(c.text).strip().rsplit("\n\nFindings of the review td", 1)[0]
            break
    sha = _landed_sha(store, todo_id)
    out = f"- td{todo_id} {title}".rstrip()
    out += f" — landed: {sha}" if sha else ""
    if findings:
        label = (
            "findings" if node is not None and node.meta.get(FIX_DEPTH_KEY) else "brief"
        )
        out += f"\n  {label}:\n" + "\n".join(
            f"  {line}" for line in findings.splitlines()
        )
    return out


def _mint_fix_chain_ask(
    store: Any,
    plan: _Plan,
    n: _Node,
    handle: str,
    findings: str,
    depth: int,
    lineage: list[int],
    now: datetime,
) -> int:
    """The ``ask-coordinator`` ask a FIX-chain cap files under the plan
    root (gr604629): the fourth-round fix is never minted — the ask
    collects the chain's findings and landed shas so one briefed builder
    gets one brief. ``blocks`` names the plan root, not ``"nothing"`` —
    an informational ask is a closed child to the phases step
    (gr589823), and this ask must hold the root open beside the
    ``waiting-for:coordinator`` tag until the coordinator answers; it
    also joins the in-memory plan like :func:`_mint_task`'s mints, or
    the same pass's phases step would close the root over a child it
    cannot see (td610546). Insert shape of :func:`_mint_brief_ask`."""
    context = (
        f"review-FIX chain capped at depth {depth} on plan td{n.root} "
        f"(review td{n.id} of {handle}); the chain is parked for a "
        "coordinator call"
    )
    title = f"td{n.root}: review-FIX chain capped at depth {depth} — a coordinator call"
    items = "\n".join(_chain_item(store, plan, i) for i in lineage)
    body = (
        f"The review→fix→land→review chain under plan td{n.root} reached "
        f"{depth} FIX verdicts on the same finding lineage: each fix "
        "patched the previous reviewer's latest hole and the next "
        "reviewer found another. The chain is capped — the next fix is "
        "not minted. The root is parked on `waiting-for:coordinator` "
        "and the chain's findings and lands are collected here so one "
        "builder gets one brief.\n\n"
        f"Chain, oldest first:\n{items or '- (chain unreadable)'}\n\n"
        f"Newest findings (review td{n.id} of {handle}):\n{findings}\n\n"
        "To resolve: mint one builder carrying the design call as its `brief:`, "
        "remove `waiting-for:coordinator` when the plan may proceed, and "
        "close this ask `acknowledged`."
    )
    return _mint_coordinator_ask(
        store, plan, n, title=title, body=body, context=context, now=now
    )


def _mint_coordinator_ask(
    store: Any,
    plan: _Plan,
    n: _Node,
    *,
    title: str,
    body: str,
    context: str,
    now: datetime,
    block_node: bool = False,
) -> int:
    """Insert a coordinator ask and expose it to later steps this pass.

    A task configuration ask also links the node ``blocked-by`` the ask.
    The dispatch tick's lock and that edge dedupe subsequent passes.
    FIX-chain asks retain the root-wide coordinator tag as their hold.
    """
    # A root task/generator must stay childless so it can dispatch after
    # repair; its ask sits alongside it, still linked as its blocker.
    parent_id = n.parent_id if block_node and n.id == n.root else n.root
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_COORDINATOR_ASK_OPTIONS),
            "blocks": f"td{n.id if block_node else n.root}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=parent_id,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in (
            COORDINATOR_ASK_TAG,
            "ask-user",
            "showstopper",
            f"plan:{n.root}",
        ):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        if block_node:
            store.add_link(
                src_ref_id=n.id, dst_ref_id=ref.id, relation="blocked-by", conn=conn
            )
        finish_mint(store, ref.id, conn=conn)
    ask_id = int(ref.id)
    parent = plan.nodes.get(parent_id) if parent_id is not None else None
    plan.nodes[ask_id] = _Node(
        id=ask_id,
        kind="todo",
        parent_id=parent_id,
        title=title,
        meta=meta,
        status="open",
        created_at=now,
        prio=None,
        depth=(parent.depth + 1) if parent is not None else 1,
        root=n.root,
        open_tags={
            v: now
            for v in (
                COORDINATOR_ASK_TAG,
                "ask-user",
                "showstopper",
                f"plan:{n.root}",
            )
        },
    )
    if parent is not None:
        parent.children.append(ask_id)
    if block_node:
        plan.blockers.setdefault(n.id, []).append(
            _Blocker(id=ask_id, status="open", is_ask=True, created_at=now)
        )
    return ask_id


def _reviewed_shas(store: Any, plan: _Plan, n: _Node) -> list[str]:
    """The landed shas the reviewer ``n`` judged: its targets' ``landed:``
    shas, else the sha a sha-keyed reviewer names."""
    shas = [s for t in _review_targets(plan, n) if (s := _landed_sha(store, t.id))]
    if not shas and (from_sha := _review_sha(n)) is not None:
        shas = [from_sha]
    return shas


def _release_scope_target(store: Any, shas: list[str]) -> str | None:
    """``release/r<N>`` when one of ``shas`` is inside the open release's
    scope — its cut range or a commit on the release branch
    (``release_poll._release_lands``, the deploy gate's own scope) —
    and that release still takes fixes (no deploy started); else
    ``None`` (the corrective lands on main). The deploy gate wants a
    FIX/REVERT's corrective ON the release branch (release-train T4),
    so a corrective minted for main would hold the deploy until a human
    answers. Any read fault reads ``None``."""
    if not shas:
        return None
    from precis.workers.job_types import release_poll as _rp

    try:
        root = _rp._open_pass_id(store)
        if root is None:
            return None
        rel = _release.release_of(store, root)
        if rel is None or _release.deploy_started(store, root) is not None:
            return None
        lands = _rp._release_lands(rel)
    except Exception:
        log.warning("plan_dispatch: release scope unreadable", exc_info=True)
        return None
    for land in lands:
        if any(
            land.sha.startswith(s.lower()) or s.lower().startswith(land.sha)
            for s in shas
        ):
            return _release.release_branch(str(rel["rN"]))
    return None


def _review_fix(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    handle: str,
    findings: str,
    now: datetime,
    lane_table: tuple[_lanes.Lane, ...],
) -> tuple[int | None, int | None]:
    """Mint the next ``Fix review findings`` task for the reviewer
    ``n``'s FIX verdict — or, the chain being capped, park the plan for
    the coordinator. ``(fix_id, cap_ask_id)``: exactly one is not
    ``None`` in a live pass (both ``None`` in a dry run that took the
    cap path)."""
    depth = _fix_depth(plan, n)
    if depth >= FIX_CHAIN_CAP:
        # gr604629: a fourth round on the same finding never mints — the
        # root parks waiting-for:coordinator and one ask collects the
        # chain's findings and shas for a single briefed builder.
        lineage = _fix_lineage(plan, n)
        chain = " → ".join(f"td{i}" for i in lineage)
        ask_id = (
            None
            if w.dry_run
            else _mint_fix_chain_ask(
                store, plan, n, handle, findings, depth, lineage, now
            )
        )
        root = plan.nodes.get(n.root)
        if root is not None:
            w.add_tag(root, COORDINATOR_HOLD_TAG, now)
            w.comment(
                root,
                f"plan_dispatch: review td{n.id} returned FIX #{depth} on "
                f"{chain} — the chain is capped: no fix mints, the root "
                "is parked waiting-for:coordinator and the chain rides "
                + (f"ask td{ask_id}." if ask_id is not None else "(dry run)."),
            )
        res.asks += 1
        res.notes.append(
            f"td{n.id}: FIX chain capped at depth {depth} on {chain} — "
            f"root parked waiting-for:coordinator"
            + (f", ask td{ask_id}" if ask_id is not None else "")
        )
        return None, ask_id
    intent = (
        f"`git show {handle}` is the intent"
        if _review_sha(n) is not None
        else f"{handle}'s brief is the intent"
    )
    target = _release_scope_target(store, _reviewed_shas(store, plan, n))
    if target is None:
        body = (
            f"{findings}\n\nFindings of the review td{n.id} of {handle}'s land on "
            f"main. Fix them on a fresh slice and land it; {intent}."
        )
    else:
        # release-train T3 review F2: the land is inside the open release,
        # whose deploy waits for this corrective on its own branch.
        body = (
            f"{findings}\n\nFindings of the review td{n.id} of {handle}'s land "
            f"inside {target}. Fix them on a fresh slice cut from "
            f"origin/{target}; the lander lands it onto {target}, never main; "
            f"{intent}.\nland target: {target}"
        )
    fix_id = _mint_task(
        store, plan, w, n.root, f"Fix review findings for {handle}", body, now
    )
    node = plan.nodes[fix_id]
    w.stamp_meta(node, {FIX_DEPTH_KEY: depth})
    if target is not None:
        w.stamp_meta(node, {_release.LAND_TARGET_KEY: target})
    if depth >= FIX_ESCALATE_DEPTH:
        # gr604629: two rounds could not close the finding — the third is
        # born model-pinned to the ladder's top rung (the ``model`` pin
        # routes through ``pick_lane`` and ``_escalate`` never climbs it
        # back down), not held for another design call.
        task_class = str(node.meta.get("task_class") or _DEFAULT_TASK_CLASS)
        rungs = _lanes.ladder(task_class, lanes=lane_table)
        top = rungs[-1] if rungs else None
        if top is not None:
            params = {"model": top.model} if top.model else {"lane": top.lane}
            w.stamp_meta(node, {"op": {"type": DEFAULT_OP, "params": params}})
        pin = (
            f"lane {top.lane} model {top.model or 'default'}"
            if top is not None
            else "unpinned — no enabled rung for its task class"
        )
        w.comment(
            node,
            f"plan_dispatch: escalated: round {depth} of td{n.root} — "
            f"pinned {pin}; two earlier rounds on the chain did not "
            "close the finding.",
        )
        res.notes.append(
            f"td{n.id}: minted td{fix_id} escalated: round {depth} of "
            f"td{n.root} ({pin})"
        )
        return fix_id, None
    if depth >= FIX_BRIEF_DEPTH:
        # gr603118: a chain this deep needs the coordinator's design call
        # before a slice starts — hold the mint behind waiting-for:brief.
        lineage = _fix_lineage(plan, n)
        chain = " → ".join(f"td{i}" for i in (*lineage, fix_id))
        w.add_tag(node, BRIEF_HOLD_TAG, now)
        ask_id = None if w.dry_run else _mint_brief_ask(store, node, depth, chain)
        w.stamp_meta(
            node,
            {BRIEF_WAIT_KEY: {"ask": ask_id, "at": _iso(now), "depth": depth}},
        )
        root = plan.nodes.get(n.root)
        if root is not None:
            w.comment(
                root,
                f"plan_dispatch: fix td{fix_id} is held waiting-for:brief — "
                f"chain depth {depth} on {chain}; a design call is needed "
                + (f"(ask td{ask_id})" if ask_id is not None else "(dry run)"),
            )
        res.asks += 1
        res.notes.append(
            f"td{n.id}: minted td{fix_id} held waiting-for:brief "
            f"(chain depth {depth} on {chain})"
        )
    return fix_id, None


def _review_revert(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    handle: str,
    why: str,
    now: datetime,
) -> int:
    """Mint the revert task and requeue each reviewed node behind it."""
    targets = _review_targets(plan, n)
    landed = [(t, _landed_sha(store, t.id)) for t in targets]
    shas = [sha for _t, sha in landed if sha]
    if not shas:
        # A sha-keyed reviewer names the commit to revert directly.
        from_sha = _review_sha(n)
        if from_sha is not None:
            shas = [from_sha]
    # release-train T3 review F2: a land inside the open release is
    # reverted on its release branch, where the deploy gate wants it.
    target = _release_scope_target(store, shas)
    where = target or "main"
    if len(shas) == 1:
        title = f"Revert {shas[0]} on {where}"
    elif shas:
        title = f"Revert {len(shas)} landed slices of {handle} on {where}"
    else:
        title = f"Revert the {handle} land on {where}"
    named = (
        ", ".join(f"`{s}`" for s in shas)
        if shas
        else f"the squash commit(s) naming {handle} (`git log --grep {handle} origin/main`)"
    )
    if target is None:
        body = (
            f"Revert {named} on main: `git revert --no-edit <sha>` for each, "
            "newest first; resolve conflicts against the current main; commit "
            "with a `revert(...)` subject and land it as a normal slice "
            f"(scripts/ship).\n\nWhy (review td{n.id}): {why}"
        )
    else:
        body = (
            f"Revert {named} on {target}: on a slice cut from origin/{target}, "
            "`git revert --no-edit <sha>` for each, newest first; commit with "
            f"a `revert(...)` subject; the lander lands it onto {target}, "
            f"never main.\nland target: {target}\n\nWhy (review td{n.id}): {why}"
        )
    revert_id = _mint_task(store, plan, w, n.root, title, body, now)
    if target is not None:
        w.stamp_meta(plan.nodes[revert_id], {_release.LAND_TARGET_KEY: target})
    requeue = [t for t, sha in landed if sha] or targets
    for t in requeue:
        claims = _claims(t)
        if claims:
            w.remove_tags(t, claims)
        # The floor is the newest ENDED job (td584381 S1d, the gr586688
        # rule): a still-queued job — a re-minted land that never ran —
        # is no re-arm boundary, and the ended land must be floored or
        # the phases backstop would close the todo under it.
        ended = [j for j in plan.jobs(t) if j.status in _JOB_ENDED]
        if ended:
            w.stamp_meta(t, {"rearmed_after_job": ended[-1].id})
        w.comment(
            t,
            f"handoff: review: REVERT — {why}\n\n(review td{n.id}; reverted by "
            f"td{revert_id} — rebuild after it lands, from the brief, with this "
            "finding in mind)",
        )
        if t.status != "open":
            w.set_status(t, "open")
        plan.blockers.setdefault(t.id, []).append(
            _Blocker(id=revert_id, status="open", is_ask=False, created_at=now)
        )
        if not w.dry_run:
            store.add_link(src_ref_id=t.id, dst_ref_id=revert_id, relation="blocked-by")
    if not requeue and _review_sha(n) is None:
        # A reviewer whose `from` target is not in this plan has no
        # landed todo to hold it through the revert: the reviewer
        # itself waits on the task — else it re-dispatches next tick
        # and re-reviews the same land in a loop (gr597625). A
        # sha-keyed reviewer closes at the verdict instead
        # (_step_reviews) — its land has no rebuild to re-read.
        if not w.dry_run:
            store.add_link(src_ref_id=n.id, dst_ref_id=revert_id, relation="blocked-by")
        plan.blockers.setdefault(n.id, []).append(
            _Blocker(id=revert_id, status="open", is_ask=False, created_at=now)
        )
    res.notes.append(
        f"td{n.id}: requeued {', '.join(f'td{t.id}' for t in requeue) or 'nothing'} "
        f"behind the revert"
    )
    _reset_gates(plan, w, res, n, shas)
    return revert_id


def _reset_gates(
    plan: _Plan, w: _Writer, res: PassResult, n: _Node, shas: list[str]
) -> None:
    """Put every ``sha-in-prod`` gate of ``n``'s plan whose sha is one of the
    reverted ``shas`` back to :data:`~precis.plans.templates.PENDING_SHA`, so
    it does not open (or stay open) on a sha that is no longer the fix; a
    gate already ``done`` is reopened. The rebuild's land fills it again."""
    for g in plan.todos():
        spec = g.meta.get("gate")
        if (
            g.root != n.root
            or node_kind(g.meta) != "gate"
            or not isinstance(spec, dict)
        ):
            continue
        args = spec.get("args")
        if spec.get("kind") != "sha-in-prod" or not isinstance(args, dict):
            continue
        sha = str(args.get("sha") or "").strip().lower()
        if not sha or sha == _templates.PENDING_SHA:
            continue
        if not any(s.startswith(sha) or sha.startswith(s) for s in shas):
            continue
        if _closed(g) and g.status != "done":
            continue
        w.stamp_meta(
            g, {"gate": {**spec, "args": {**args, "sha": _templates.PENDING_SHA}}}
        )
        w.comment(g, "sha reset after review REVERT")
        if g.status == "done":
            w.set_status(g, "open")
        res.notes.append(f"gate td{g.id}: sha {sha[:9]} reset after review REVERT")


def _todo_status(store: Any, todo_id: int) -> str:
    """A todo's STATUS value, ``"open"`` when it carries none — the
    fallback for a review target the loaded plan does not reach."""
    for t in store.tags_for(todo_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return "open"


def _pending_land(store: Any, todo_id: int) -> bool:
    """``meta.pending_land`` set on ``todo_id`` — its clean build is done
    with the land held for a pre-land review (gr570506), so a ``done``
    target with no ``landed:`` comment is NOT a dead land."""
    ref = store.get_ref(kind="todo", id=todo_id)
    return bool(
        ref is not None
        and isinstance((ref.meta or {}).get(_templates.PENDING_LAND_KEY), dict)
    )


def _pending_targets(plan: _Plan, n: _Node) -> list[_Node]:
    """The reviewer ``n``'s targets whose land it gates — the build todos
    carrying ``meta.pending_land`` (gr570506, pre-land review)."""
    return [
        t
        for t in _review_targets(plan, n)
        if isinstance(t.meta.get(_templates.PENDING_LAND_KEY), dict)
    ]


def _is_its_land_gate(plan: _Plan, n: _Node, b: _Blocker) -> bool:
    """``n`` is the reviewer whose verdict releases the land ``b``'s
    ``meta.pending_land`` holds (gr659981): the stamp blocks every
    dependant but this one — its LAND is what mints the held
    ``land_slice``, its FIX/REVERT what reopens the builder. ``from``
    names the held todo, or one of the review targets carries it (a
    slicer's slices). Only a pre-land reviewer (``meta.preland_review``)
    is exempt: a post-land reviewer judges the squash, so it waits."""
    if not _is_reviewer(n) or n.meta.get(_templates.PRELAND_REVIEW_KEY) is not True:
        return False
    return _review_from(n) == b.id or any(
        t.id == b.id for t in _review_targets(plan, n)
    )


def _dead_land_targets(plan: _Plan, store: Any, n: _Node) -> list[int]:
    """The reviewer ``n``'s land targets when EVERY one closed with no
    ``landed:`` comment — the review has nothing left to judge
    (gr575912). ``[]`` while any target is still live or landed one: a
    ``won't-do`` blocker is terminal, so without this check a build the
    coordinator killed frees its review for dispatch anyway.

    Targets are ``generate.from``'s todo — its non-ask children when it
    has them (a slicer's slices) — falling back to the non-ask
    ``blocked-by`` edges when ``from`` resolves to no plan todo (an
    engine-review node points out of its plan subtree)."""
    pairs: list[tuple[int, str]] = [(t.id, t.status) for t in _review_targets(plan, n)]
    if not pairs:
        fid = _review_from(n)
        if fid is not None:
            node = plan.nodes.get(fid)
            blocker = next(
                (b for b in plan.blockers.get(n.id, []) if b.id == fid), None
            )
            pairs = [
                (
                    fid,
                    node.status
                    if node is not None
                    else plan.status_of(blocker)
                    if blocker is not None
                    else _todo_status(store, fid),
                )
            ]
        else:
            pairs = [
                (b.id, plan.status_of(b))
                for b in plan.blockers.get(n.id, [])
                if not b.is_ask
            ]
    dead = [
        tid
        for tid, status in pairs
        if status in BLOCKER_TERMINAL_STATUSES
        and _landed_sha(store, tid) is None
        and not _pending_land(store, tid)
    ]
    return dead if pairs and len(dead) == len(pairs) else []


def _close_dead_chain(
    plan: _Plan, w: _Writer, res: PassResult, n: _Node, dead: list[int]
) -> None:
    """Close reviewer ``n`` and the rest of the dead land chain ``won't-do``
    (gr575912): the dead target's other ``blocked-by`` descendants — the
    prod gate, the validate — starve with the review, so the pass runs
    the same close a task moot runs (``_step_moot``) and the root can
    settle. A node carrying a live job defers: the sweep re-runs every
    pass while the targets stay dead (``_step_reviews`` runs it for a
    closed reviewer too), so the straggler closes once its job ends
    instead of holding the root open forever."""
    why = (
        f"review target {', '.join(f'td{t}' for t in dead)} closed with no "
        "`landed:` comment — nothing landed to review"
    )
    if not _closed(n):
        w.comment(n, f"plan_dispatch: {why}")
        w.set_status(n, "won't-do")
        res.phases_closed += 1
        res.notes.append(f"td{n.id}: review closed won't-do — {why}")
    starved = 0
    for tid in dead:
        target = plan.nodes.get(tid)
        if target is None:
            continue
        for m in _moot_descendants(plan, target):
            if m.id == n.id or _closed(m):
                continue
            if plan.live_job(m):
                res.notes.append(f"td{m.id} has a live job; dead-chain close deferred")
                continue
            w.comment(m, f"plan_dispatch: {why}; its land chain is dead")
            w.set_status(m, "won't-do")
            res.phases_closed += 1
            starved += 1
    if starved:
        res.notes.append(
            f"td{n.id}: {starved} dead-chain node(s) closed won't-do with it"
        )


def _mint_preland_land(
    store: Any, w: _Writer, res: PassResult, n: _Node, t: _Node
) -> int:
    """The pre-land LAND (gr570506): mint the ``land_slice`` the build's
    clean exit held back — the params ``run_slice`` stamped in
    ``meta.pending_land``, the usual ``land_slice:td<id>`` idem key.
    ``meta.pending_land`` STAYS stamped until ``land_slice`` itself lands
    (``_record_land`` clears it): it is the marker keeping the done,
    not-yet-landed target out of ``_dead_land_targets`` while the land
    is in flight, and the stamp ``land_slice`` reads to admit a closed
    todo. Returns the job id."""
    params = dict(t.meta[_templates.PENDING_LAND_KEY])
    job_id = _mint_job(
        store, t, "land_slice", "ssh_node", params, idem_key=f"land_slice:td{t.id}"
    )
    w.comment(
        t,
        f"plan_dispatch: review td{n.id} LAND — the held land mints now "
        f"(job:{job_id}); pre-land review (gr570506).",
    )
    res.notes.append(
        f"td{t.id}: pre-land LAND — land_slice job:{job_id} minted (review td{n.id})"
    )
    return job_id


def _review_back_to_builder(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    kind: str,
    rest: str,
) -> None:
    """A pre-land FIX/REVERT (gr570506, Reto 2026-11-12: "if it says no,
    the work goes back to the builder"). Nothing landed, so no fix or
    revert task mints: each held target's ``meta.pending_land`` clears,
    it reopens with the findings as its ``handoff:`` — the next build's
    brief — and the reviewer stays open for the rebuild (its
    ``blocked-by`` edge re-arms while the target is open and releases
    when the next build defers again)."""
    targets = _pending_targets(plan, n)
    for t in targets:
        w.stamp_meta(t, {_templates.PENDING_LAND_KEY: None})
        claims = _claims(t)
        if claims:
            w.remove_tags(t, claims)
        # The rejected build burns no attempt (``_review_revert``'s
        # floor): the rebuild dispatches on a fresh budget.
        ended = [j for j in plan.jobs(t) if j.status in _JOB_ENDED]
        if ended:
            w.stamp_meta(t, {"rearmed_after_job": ended[-1].id})
        w.comment(
            t,
            f"handoff: review: {kind} — {rest}\n\n(review td{n.id}, "
            "pre-land: the branch is still on origin — rebuild on it "
            "from the brief with this finding in mind)",
        )
        if t.status != "open":
            w.set_status(t, "open")
    res.notes.append(
        f"td{n.id}: review {kind} pre-land — back to the builder: "
        + ", ".join(f"td{t.id}" for t in targets)
    )


def _step_reviews(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    now: datetime,
    lane_table: tuple[_lanes.Lane, ...],
    rules: dict[str, dict[str, Any]] | None = None,
) -> None:
    for n in plan.todos():
        if not _is_reviewer(n) or plan.paused(n):
            continue
        if plan.child_todos(n) or plan.live_job(n):
            continue
        dead = _dead_land_targets(plan, store, n)
        if dead:
            _close_dead_chain(plan, w, res, n, dead)
            continue
        if _closed(n):
            continue
        jobs = plan.jobs(n)
        if not jobs or jobs[-1].status != "succeeded":
            continue
        newest = jobs[-1]
        if _voided(newest):
            # The seat voided the run by hand (gr568824): it counts for
            # nothing — drop its dead claim and reopen so the node
            # re-dispatches, and never read its comments as a verdict.
            w.remove_tags(n, _claims(n))
            if n.status != "open":
                w.set_status(n, "open")
            res.notes.append(f"td{n.id}: review job:{newest.id} voided")
            continue
        seen = n.meta.get("review")
        if isinstance(seen, dict) and seen.get("job") == newest.id:
            continue
        verdict, raw = _read_verdict(store, n.id, newest.created_at)
        claims = _claims(n)
        if claims:
            w.remove_tags(n, claims)
        # The reviewer is read-only: every verdict is a diff read, never
        # test evidence — the dispatcher stamps that fact itself.
        record: dict[str, Any] = {
            "job": newest.id,
            "at": _iso(now),
            "tests_run": False,
        }
        target_id = _review_from(n)
        handle = _review_sha(n) or (f"td{target_id if target_id is not None else n.id}")
        if verdict is None:
            what = (
                f"a malformed verdict {_first_line(raw)!r}"
                if raw
                else "no `review:` comment"
            )
            job_type_, params_ = _resolve_op(n)
            cap = _retry_cap(
                n, _cap_rungs(plan, lane_table, n, job_type_, params_, rules)
            )
            w.stamp_meta(n, {"review": {**record, "verdict": None}})
            w.comment(
                n,
                f"plan_dispatch: job:{newest.id} succeeded with {what}; want "
                f"{_templates.REVIEW_GRAMMAR}. Retrying (retry cap {cap}), "
                "then rescue.",
            )
            if n.status != "open":
                w.set_status(n, "open")
            res.notes.append(f"td{n.id}: review job:{newest.id} left {what}")
            continue
        kind, rest = verdict
        record["verdict"] = kind
        marker = _templates.REVIEW_READONLY_MARKER
        pending = _pending_targets(plan, n)
        preland = bool(pending) and len(pending) == len(_review_targets(plan, n))
        if kind == "LAND":
            for t in pending:
                _mint_preland_land(store, w, res, n, t)
            w.set_status(n, "done")
            res.notes.append(f"td{n.id}: review LAND for {handle} {marker}; closed")
        elif preland:
            # gr570506 (Reto 2026-11-12): a FIX/REVERT on a branch that
            # has not landed sends the work back to the builder — nothing
            # is on main to fix or revert — and the reviewer stays open
            # for the rebuild it then re-gates.
            _review_back_to_builder(store, plan, w, res, n, kind, rest)
            w.stamp_meta(n, {"rearmed_after_job": newest.id})
            if n.status != "open":
                w.set_status(n, "open")
            res.notes.append(
                f"td{n.id}: review {kind} for {handle} {marker}; "
                "pre-land — back to the builder, reviewer stays open"
            )
        elif kind == "FIX":
            fix_id, cap_ask = _review_fix(
                store, plan, w, res, n, handle, rest, now, lane_table
            )
            if fix_id is not None:
                record["task"] = fix_id
            if cap_ask is not None:
                record["cap_ask"] = cap_ask
            w.set_status(n, "done")
            res.notes.append(
                f"td{n.id}: review FIX for {handle} {marker}; "
                + (
                    f"minted td{fix_id}, closed"
                    if fix_id is not None
                    else "chain capped — parked for the coordinator, closed"
                )
            )
        else:
            revert_id = _review_revert(store, plan, w, res, n, handle, rest, now)
            record["task"] = revert_id
            if _review_sha(n) is not None:
                # A sha-keyed land has no rebuild to re-read: the
                # reviewer closes like a FIX — the gate tracks the
                # revert task it minted (gr597625). Kept open it would
                # re-dispatch once the revert lands, re-review the same
                # static commit, REVERT again and mint the same revert
                # in a loop (a second `git revert` re-applies it).
                w.set_status(n, "done")
                res.notes.append(
                    f"td{n.id}: review REVERT for {handle} {marker}; "
                    f"minted td{revert_id}, closed"
                )
            else:
                w.stamp_meta(n, {"rearmed_after_job": newest.id})
                if n.status != "open":
                    w.set_status(n, "open")
                res.notes.append(
                    f"td{n.id}: review REVERT for {handle} {marker}; "
                    f"minted td{revert_id}, "
                    "reviewer stays open for the rebuild"
                )
        w.stamp_meta(n, {"review": record})
        res.reviews += 1


# ── (3) phases ─────────────────────────────────────────────────────


def _op_type(n: _Node) -> str:
    op = n.meta.get("op")
    if isinstance(op, dict):
        return str(op.get("type") or DEFAULT_OP)
    if isinstance(op, str) and op.strip():
        return op.strip()
    return DEFAULT_OP


def _step_reopen(plan: _Plan, w: _Writer, res: PassResult) -> None:
    """A generator tagged ``STATUS:done`` (its session's own close) while a
    child todo is open goes back to ``STATUS:open``: it closes as a phase
    once every child is closed, and until then must keep blocking whatever is
    ``blocked-by`` it. An open informational ask (``blocks: "nothing"``) is a
    record for the human and never reopens it (gr589823)."""
    for n in plan.todos():
        if node_kind(n.meta) != "generator" or n.status != "done":
            continue
        open_kids = [
            c
            for c in plan.child_todos(n)
            if not _closed(c) and not _informational_ask(c)
        ]
        if not open_kids:
            continue
        w.set_status(n, "open")
        res.reopened += 1
        res.notes.append(
            f"generator td{n.id} reopened: {len(open_kids)} child(ren) still open"
        )


def _step_rearm(plan: _Plan, w: _Writer, res: PassResult) -> None:
    """An answered retry-cap ask re-arms its node with a fresh budget whatever
    ran between the ask and its answer (gr561075). :func:`_attempts` counts
    the ended jobs after the newest ask; five hand-run lands between a cap
    ask and its ``retry`` once kept the node capped and minted a second ask
    the moment the first closed. The first tick that sees the newest ask
    terminal stamps ``meta.rearmed_after_job`` with the newest ended job
    (the floor the review-REVERT path already uses) and records which ask
    did it (``meta.rearmed_by_ask``), so the stamp happens once per ask —
    except when that newest ended job is a succeeded ``land_slice``: the
    slice already landed, there is nothing to retry, and flooring the
    todo on its own land would hold it open for a duplicate build of
    landed work — ``_step_phases``' backstop closes it this pass instead
    (gr586688 review)."""
    for n in plan.todos():
        if _closed(n) or not _dispatchable(plan, n):
            continue
        asks = [b for b in plan.blockers.get(n.id, []) if b.is_ask]
        if not asks:
            continue
        newest_ask = max(asks, key=lambda b: (b.created_at, b.id))
        if plan.status_of(newest_ask) not in BLOCKER_TERMINAL_STATUSES:
            continue
        if n.meta.get("rearmed_by_ask") == newest_ask.id:
            continue
        ended = [j for j in plan.jobs(n) if j.status in _JOB_ENDED]
        # A succeeded ``land_slice`` as the newest ended job means the
        # slice already landed — nothing to retry: no floor, and the
        # phases backstop closes the todo this pass. Everything else the
        # re-arm writes (``rearmed_by_ask`` here, the answer record and
        # the ``rescue:`` clear below) still applies — safe: with no
        # floor the todo is done before dispatch would ever see it.
        landed = bool(
            ended
            and ended[-1].status == "succeeded"
            and ended[-1].meta.get("job_type") == "land_slice"
        )
        stamp: dict[str, Any] = {"rearmed_by_ask": newest_ask.id}
        if ended and not landed:
            stamp["rearmed_after_job"] = ended[-1].id
        # A re-land ask's answer decides what the next dispatch may be
        # (gr570187): "land again (no builder)" over an untouched brief
        # re-lands the old branch; a rewritten brief — or any other
        # answer — mints a builder on the task's brief. A non-reland ask
        # clears the record so a fresh infra-land run re-lands freely.
        ask_node = plan.nodes.get(newest_ask.id)
        opts = (
            (ask_node.meta.get("ask") or {}).get("options")
            if ask_node is not None
            else None
        )
        if isinstance(opts, list) and _RELAND_ASK_OPTIONS[0] in opts:
            stamp["rearmed_answer"] = (
                _release._newest_answer(w.store, newest_ask.id) or ""
            )
            stamp["rearmed_ask_at"] = newest_ask.created_at.isoformat()
        else:
            stamp["rearmed_answer"] = None
            stamp["rearmed_ask_at"] = None
        # A node whose rescue ended in the rescue pass's own ask: the
        # human's answer IS the clearing (td584381 S1a F1) — the
        # ``rescue:<cls>`` tag would otherwise keep it excluded from
        # dispatch and hygiene forever. ``meta.rescue.n`` stays: a
        # second exhaustion re-enters rescue and asks the human again.
        # ``rescue_answered`` records the episode for the rescue pass
        # (gr598110): a re-tag of the answered class with no newer job
        # re-arms instead of re-asking.
        cleared = [v for v in n.open_tags if v.startswith("rescue:")]
        if cleared:
            stamp["rescue_answered"] = {
                "cls": cleared[0][len("rescue:") :],
                "ask": newest_ask.id,
                "job": ended[-1].id if ended else None,
            }
        w.stamp_meta(n, stamp)
        if cleared:
            w.remove_tags(n, cleared)
            if n.status == "doing":
                w.set_status(n, "open")
        # gr598110: the re-arm must also drop the dead ``claimed-by:``
        # lease — ``_step_dispatch`` skips every claimed node, so a
        # re-armed node whose newest job had ended sat claimed until
        # hygiene's CLAIM_STALE wait, and the stall watchdog re-tagged it
        # (td598406 asked the same question again on the re-tag).
        released: list[str] = []
        if not plan.live_job(n):
            released = _claims(n)
            if released:
                w.remove_tags(n, released)
                if n.status == "doing":
                    w.set_status(n, "open")
        res.notes.append(
            f"td{n.id}: ask td{newest_ask.id} answered; retry budget reset"
            + (f" after job:{ended[-1].id}" if ended and not landed else "")
            + (
                f"; newest job:{ended[-1].id} already landed — no floor"
                if landed
                else ""
            )
            + ("; rescue tag cleared" if cleared else "")
            + (f"; released {', '.join(released)}" if released else "")
        )


def _moot_descendants(plan: _Plan, n: _Node) -> list[_Node]:
    """The open todos under ``n.root`` ``blocked-by``-dependent on ``n``,
    transitively — the review / wait-for-prod / validate chain a task
    moot closes (td584381 S1d review). A closed link in the chain still
    transmits: the closure walks through it, it is just not re-closed."""
    dependents: dict[int, set[int]] = {}
    for src, targets in plan.blockers.items():
        for b in targets:
            dependents.setdefault(b.id, set()).add(src)
    seen = {n.id}
    frontier = [n.id]
    out: list[_Node] = []
    while frontier:
        cur = frontier.pop()
        for dep_id in dependents.get(cur, ()):
            if dep_id in seen:
                continue
            seen.add(dep_id)
            m = plan.nodes.get(dep_id)
            if m is None or m.kind != "todo" or m.root != n.root:
                continue
            frontier.append(dep_id)
            if not _closed(m):
                out.append(m)
    return sorted(out, key=lambda m: m.id)


def _verdict_comment(
    store: Any, todo_id: int, *, since: datetime | None = None
) -> tuple[str, str] | None:
    """``(kind, why)`` of the verdict the todo's comments carry
    (gr602896): a ``handoff:`` — prefix stripped first, the line read
    by :func:`precis.plans.nodes.any_verdict` — or a comment that IS
    the verdict: ``no-op:``/``moot:``/``validated:`` on its first
    non-empty line, posted by hand
    (:func:`precis.plans.nodes.generator_verdict`). Any other comment
    never counts (td604234): a ``brief:`` quoting the grammar — "if the
    fix is already on main, answer ``moot: <why>``" — carries the
    verdict-shaped line as an example, not a ruling, and reading it
    dropped work no job ever ran. Comments older than ``since`` (the
    newest attempt job's ``created_at``) never count — an earlier
    attempt's verdict is not this one's; a comment whose ``created_at``
    cannot be read fails closed like :func:`_handoff_verdict`. Newest
    first, first hit wins."""
    for c in _comments(store, todo_id):
        created = getattr(c, "created_at", None)
        if since is not None and (not isinstance(created, datetime) or created < since):
            continue
        body = str(c.text).strip()
        hit = (
            any_verdict(body[len("handoff:") :])
            if body.lower().startswith("handoff:")
            else generator_verdict(body)
        )
        if hit is not None:
            return hit
    return None


def _foreign_verdict(store: Any, n: _Node, verdict: tuple[str, str]) -> bool:
    """The ``moot:`` verdict pins its claim to a ``gr<N>`` that is NOT
    the plan's gripe (gr602896): it is another plan's statement, never
    this one's — the drop must not read it."""
    kind_, why = verdict
    if kind_ != "moot":
        return False
    named = _GRIPE_TOKEN_RE.search(why)
    if named is None:
        return False
    from precis.workers.job_types import land_slice as _land_slice

    try:
        gid = _land_slice._plan_gripe(store, f"plan:{n.root}")
    except Exception:  # an unresolvable gripe proves no conflict
        log.warning("plan_dispatch: plan gripe lookup for td%s raised", n.root)
        return False
    return gid is not None and int(named.group(1)) != gid


def _step_verdicts(store: Any, plan: _Plan, w: _Writer, res: PassResult) -> None:
    """A node carrying a verdict is dropped here, ahead of the moot
    close (gr602896): a ``no-op:``/``moot:``/``validated:`` line on a
    ``handoff:``, posted by hand as a bare verdict comment (its first
    line is the verdict — a ``brief:`` or other prose quoting an
    example verdict line is never read, td604234), or in the newest
    failed job's event text — a job recorded ``failed`` is no bar, the
    run_slice that read the verdict as a failure was the bug (jo602411:
    four escalation artefacts for a plan that should have dropped).
    The node closes done — its verdict IS the result — a ``moot:``
    also tagged ``verdict:moot`` for ``_step_moot``'s plan drop. A live
    job defers to the next tick; a node under a ``release-pass`` root
    keeps the fix loop's own rulings; a ``moot:`` pinned to another
    gripe is never this plan's. A job stamped ``human_only_hit`` keeps
    its ask (td604234): the denied run's ``handoff:`` still posts the
    session's result — ``attempt FAILED, touched deny_write_paths: …``
    over a ``moot:`` line — and its log tail carries it too, but no
    verdict closes ahead of the human-only ruling. The ask minted ON
    the hit must not disarm the guard: it bounds ``_attempt_jobs`` at
    its mint, hiding the denied job from ``_human_hit`` the pass after
    it files, so the stamp is read off the job the verdict would come
    from — until a newer job exists (td604695)."""
    for n in plan.todos():
        if _closed(n) or n.id == n.root or _is_ask_node(n):
            continue
        if plan.live_job(n):
            continue
        root = plan.nodes.get(n.root)
        if root is not None and "release-pass" in root.open_tags:
            continue
        ended = _attempt_jobs(plan, n)
        last = ended[-1] if ended else (plan.jobs(n)[-1] if plan.jobs(n) else None)
        # The stamp bars the verdict read on the job the verdict would
        # come from (td604234): the ask minted ON the hit bounds
        # ``_attempt_jobs`` at its mint, so ``_human_hit`` goes blind to
        # the denied job the pass after the ask files — and stays blind
        # after the answer re-arms, until a newer job runs (td604695).
        if last is not None and last.meta.get(HUMAN_ONLY_KEY):
            continue
        verdict = _verdict_comment(
            store, n.id, since=last.created_at if last is not None else None
        )
        if verdict is None and last is not None and last.status == "failed":
            verdict = any_verdict(_job_event_text(store, last.id))
        if verdict is None or _foreign_verdict(store, n, verdict):
            continue
        kind_, why = verdict
        w.set_status(n, "done")
        if kind_ == "moot":
            w.add_tags(n, [MOOT_TAG])
        w.comment(
            n,
            f"plan_dispatch: closed on verdict `{kind_}: {why[:300]}`",
        )
        res.notes.append(
            f"td{n.id}: verdict `{kind_}: {why[:200]}` — closed done"
            + (", tagged for the moot close" if kind_ == "moot" else "")
        )


def _is_ask_node(m: _Node) -> bool:
    """An ask todo (``meta.ask`` or the ``ask-user`` tag): a question to a
    human, not build work the moot close waits on."""
    return isinstance(m.meta.get("ask"), dict) or any(
        t.startswith("ask-user") for t in m.open_tags
    )


def _informational_ask(m: _Node) -> bool:
    """An ask with ``meta.ask.blocks == "nothing"``: a record for the
    human, not a gate — it never holds a phase, generator or loop open
    (gr589823). An ask that names a todo handle in ``blocks`` still does."""
    ask = m.meta.get("ask")
    return isinstance(ask, dict) and ask.get("blocks") == "nothing"


def _gripe_verdict_note(store: Any, gid: int, n: _Node, mv_line: Any) -> str | None:
    """gr602896: an unverified or hand-posted ``moot:`` carries no sha a
    ``resolved:`` could land on — the gripe gets the verdict line and
    ``— plan dropped`` instead, the record of why its plan went away.
    Written once (a newest comment already carrying the note means the
    last tick wrote it): the close's own order — note before the tag
    drops — makes a mid-write raise safe to retry. Returns the note for
    ``res.notes``, ``None`` when already written."""
    comments = [
        c
        for c in store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_comment" and c.text
    ]
    newest_text = (
        str(max(comments, key=lambda c: (int(c.ord), int(c.id))).text)
        if comments
        else ""
    )
    if "plan dropped" in newest_text:
        return None
    verdict = _verdict_comment(store, n.id)
    line = str(mv_line or "") or (
        f"{verdict[0]}: {verdict[1]}" if verdict else f"td{n.id} reported `moot:`"
    )
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
                ord=ord_,
                text=f"{line[:300]} — plan dropped (td{n.id})",
                meta={"chunk_kind": "gripe_comment"},
            )
        ],
    )
    return f"gripe gr{gid}: verdict noted — {line[:120]}"


def _step_moot(store: Any, plan: _Plan, w: _Writer, res: PassResult) -> None:
    """A node closed on a ``moot:`` verdict carries ``MOOT_TAG``
    (gr548813): the plan's reason is gone (its gripe already resolved,
    its section deleted). Since td584381 S1d a CODE task can carry it
    too — ``run_slice`` only tags one whose ``moot:`` claim VERIFIED on
    the worker host and stamps the job's ``meta.moot_verified =
    {sha, line}``; a generator's first-line ``moot:`` still rides on
    trust (no record).

    A GENERATOR moot keeps the whole-plan close: every open node of the
    plan and its root go won't-do. A TASK moot is scoped (review round
    1): only the moot node's ``blocked-by`` descendants — review,
    wait-for-prod, validate — close won't-do, then the root, and only
    when no other open work node under the root remains (a second open
    build refuses with ``plan td<root> not closed: td<N> still open``);
    a node with a live job is never closed (``td<N> has a live job;
    moot close deferred`` — the tag stays and the next tick retries).
    The gripe stamp happens only once the root closed, and BEFORE the
    tag drops: ``_notify_gripe`` is idempotent, so a raise leaves the
    tag for the next tick instead of losing the resolution. The retry
    is bounded (review round 2): ``meta.moot_stamp_failures`` counts
    consecutive raises — at ``_MOOT_STAMP_ASK_AFTER`` one ask files on
    the plan root (:func:`_mint_stamp_ask`) and the stamp parks until
    the ask is answered or the key is cleared. A verified sha resolves
    the plan's gripe exactly as a land does (the same
    ``resolved: <sha>`` + ``STATUS:verify`` ``land_slice`` writes);
    one alert on the root tells a human the plan went away."""
    from precis.workers.job_types import land_slice as _land_slice

    for n in plan.todos():
        if MOOT_TAG not in n.open_tags:
            continue
        why = f"td{n.id} reported `moot:` (its handoff comment has the why)"
        jobs = plan.jobs(n)
        mv = jobs[-1].meta.get(MOOT_VERIFIED_KEY) if jobs else None
        sha: str | None = None
        mv_line: Any = None
        if isinstance(mv, dict) and mv.get("sha"):
            sha, mv_line = str(mv["sha"]), mv.get("line")
        gid = _land_slice._plan_gripe(store, f"plan:{n.root}")
        stamped = False
        if node_kind(n.meta) == "generator":
            # Whole-plan close (gr548813, unchanged): every open node of
            # the plan and its root go won't-do.
            closed = 0
            for m in plan.todos():
                if m.root != n.root or m.id == n.id or _closed(m):
                    continue
                w.comment(m, f"plan_dispatch: plan closed — {why}")
                w.set_status(m, "won't-do")
                closed += 1
            res.phases_closed += closed
            res.notes.append(
                f"plan td{n.root} closed as won't-do: {why}; {closed} node(s)"
            )
        else:
            # A task moot is scoped to its own chain (review round 1):
            # its ``blocked-by`` descendants close won't-do — a node
            # with a live job defers to the next tick — then the root
            # once no other open work node under it remains.
            closed = 0
            for m in _moot_descendants(plan, n):
                if plan.live_job(m):
                    res.notes.append(f"td{m.id} has a live job; moot close deferred")
                    continue
                w.comment(m, f"plan_dispatch: plan closed — {why}")
                w.set_status(m, "won't-do")
                closed += 1
            res.phases_closed += closed
            open_work = [
                m
                for m in plan.todos()
                if m.root == n.root
                and m.id not in (n.id, n.root)
                and not _closed(m)
                and not _is_ask_node(m)
            ]
            if open_work:
                res.notes.append(
                    f"plan td{n.root} not closed: td{open_work[0].id} still open"
                )
                continue
            root = plan.nodes.get(n.root)
            if root is not None and not _closed(root) and plan.live_job(root):
                res.notes.append(f"td{root.id} has a live job; moot close deferred")
                continue
            if w.dry_run:
                if sha is not None and gid is not None:
                    res.notes.append(
                        f"would resolve gripe gr{gid} on verified moot sha {sha[:9]}"
                    )
                elif gid is not None:
                    res.notes.append(f"would note the moot verdict on gr{gid}")
                continue
            if sha is not None and gid is not None:
                # The verified moot IS the fix's land — resolve the
                # plan's gripe on its sha exactly as ``land_slice``
                # would (td584381 S1d). Stamped BEFORE the root closes
                # (review round 1): a raise leaves the root open — the
                # plan stays a candidate — and ``MOOT_TAG`` for the next
                # tick's retry instead of losing the resolution. The
                # retry is bounded (review round 2): consecutive raises
                # count on ``meta.moot_stamp_failures``; at
                # ``_MOOT_STAMP_ASK_AFTER`` one ask files on the root and
                # the stamp parks — a silent res.notes loop would page
                # nobody. The park lifts when that ask is answered or a
                # human clears the meta key.
                fails_raw = n.meta.get(MOOT_STAMP_KEY)
                try:
                    fails = int(fails_raw or 0)
                except (TypeError, ValueError):
                    fails = 0
                if fails >= _MOOT_STAMP_ASK_AFTER:
                    root_node = plan.nodes.get(n.root)
                    if root_node is not None and plan.open_asks(root_node):
                        res.notes.append(
                            f"td{n.id}: gripe stamp parked after {fails} "
                            f"failed ticks — asked on td{n.root}"
                        )
                        continue
                    # The parked ask was answered (or never filed): the
                    # human's ruling re-arms the stamp retry.
                    w.stamp_meta(n, {MOOT_STAMP_KEY: 0})
                    fails = 0
                try:
                    res.notes.append(
                        _land_slice._notify_gripe(
                            store, gid, sha, str(mv_line or n.title)[:200], n.id
                        )
                    )
                    stamped = True
                    if fails:
                        w.stamp_meta(n, {MOOT_STAMP_KEY: 0})
                except Exception as exc:
                    fails += 1
                    w.stamp_meta(n, {MOOT_STAMP_KEY: fails})
                    res.notes.append(
                        f"td{n.id}: gripe stamp failed ({exc}); moot close deferred"
                    )
                    root_node = plan.nodes.get(n.root)
                    if (
                        fails >= _MOOT_STAMP_ASK_AFTER
                        and root_node is not None
                        and not plan.open_asks(root_node)
                    ):
                        ask_id = _mint_stamp_ask(
                            w.store, n, gid, sha, type(exc).__name__
                        )
                        res.asks += 1
                        res.notes.append(
                            f"td{n.id}: gripe stamp raised {fails} ticks "
                            f"running — asked in td{ask_id}"
                        )
                    continue
            elif gid is not None:
                # gr602896: no verified sha — the gripe still gets the
                # record (``<verdict> — plan dropped``). Best-effort:
                # a note that cannot write must not hold the drop.
                try:
                    note = _gripe_verdict_note(store, gid, n, mv_line)
                    if note:
                        res.notes.append(note)
                except Exception as exc:
                    res.notes.append(f"td{n.id}: gripe verdict note failed ({exc})")
            if root is not None and not _closed(root):
                w.comment(root, f"plan_dispatch: plan closed — {why}")
                w.set_status(root, "won't-do")
                closed += 1
                res.phases_closed += 1
            res.notes.append(
                f"plan td{n.root} closed as won't-do: {why}; {closed} node(s)"
            )
        if w.dry_run:
            if sha is not None and gid is not None:
                res.notes.append(
                    f"would resolve gripe gr{gid} on verified moot sha {sha[:9]}"
                )
            elif gid is not None:
                res.notes.append(f"would note the moot verdict on gr{gid}")
            continue
        if not stamped and sha is not None and gid is not None:
            # Generator path: the close ran above, so a raise here is
            # not retried — the plan is gone from the candidates. Kept
            # for parity; the task path stamps before its root close.
            try:
                res.notes.append(
                    _land_slice._notify_gripe(
                        store, gid, sha, str(mv_line or n.title)[:200], n.id
                    )
                )
            except Exception as exc:
                res.notes.append(
                    f"td{n.id}: gripe stamp failed ({exc}); moot close deferred"
                )
                continue
        elif gid is not None and sha is None:
            # gr602896: no verified sha — the gripe still gets the
            # record (``<verdict> — plan dropped``). Best-effort: a note
            # that cannot write must not hold the drop.
            try:
                note = _gripe_verdict_note(store, gid, n, mv_line)
                if note:
                    res.notes.append(note)
            except Exception as exc:
                res.notes.append(f"td{n.id}: gripe verdict note failed ({exc})")
        w.remove_tags(n, [MOOT_TAG])
        root = plan.nodes.get(n.root)
        raise_alert(
            store,
            source=NAME,
            fingerprint=f"plan-moot:{n.root}",
            title=f"plan td{n.root} closed as moot: {root.title if root else ''}".strip(),
            detail=f"{why}; {closed} open node(s) set to won't-do.",
            severity="info",
            subject_ref_id=n.root,
        )


def _analysis_posted(plan: _Plan, store: Any, n: _Node) -> bool:
    """An ``analyst`` generator (td543749) whose newest job succeeded and
    left an ``analysis:`` comment (bare or as the ``handoff:``) since it
    was minted: its work is done."""
    gen = n.meta.get("generate")
    if not isinstance(gen, dict) or gen.get("role") != "analyst":
        return False
    jobs = plan.jobs(n)
    if not jobs or jobs[-1].status != "succeeded":
        return False
    for c in _comments(store, n.id):
        if c.created_at < jobs[-1].created_at:
            break
        body = str(c.text).strip().lower()
        if body.startswith("handoff:"):
            body = body[len("handoff:") :].strip()
        if body.startswith(_templates.ANALYSIS_PREFIX):
            return True
    return False


def _step_phases(plan: _Plan, w: _Writer, res: PassResult) -> None:
    # A root whose plan still carries an open ``MOOT_TAG`` is never
    # auto-closed here: ``_step_moot`` keeps the tag (and the root open)
    # when the gripe stamp fails so the next tick can retry — a
    # ``done`` root would drop the whole plan out of the candidates and
    # the retained tag could never fire (td584381 review round 1).
    mooted_roots = {n.root for n in plan.todos() if MOOT_TAG in n.open_tags}
    for n in sorted(plan.todos(), key=lambda x: (-x.depth, x.id)):
        if _closed(n) or n.status == "paused":
            continue
        kind = node_kind(n.meta)
        kids = plan.child_todos(n)
        if n.id == n.root and n.root in mooted_roots:
            continue
        if kind in ("phase", "generator"):
            if kind == "generator" and plan.live_job(n):
                continue
            # A ``waiting-for:`` tag parks the node itself whatever its
            # children's state — the FIX-chain cap's
            # :data:`COORDINATOR_HOLD_TAG` waits on the coordinator, an
            # unsettled mint on its minter — so "every child closed"
            # never means finished while it waits (td610546).
            if any(t.startswith("waiting-for:") for t in n.open_tags):
                continue
            # An informational ask (``blocks: "nothing"``) is a record, not
            # a gate: it stays open for the human and does not hold the
            # phase (gr589823).
            # A build whose land is held (``meta.pending_land``, gr570506)
            # is done but not landed: the plan stays open through the
            # land and any recovery rebuild, until ``_record_land``
            # clears the stamp.
            if any(
                isinstance(c.meta.get(_templates.PENDING_LAND_KEY), dict) for c in kids
            ):
                continue
            if kids and all(_closed(c) or _informational_ask(c) for c in kids):
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(f"{kind} td{n.id} closed: every child is closed")
            elif not kids and _analysis_posted(plan, w.store, n):
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(f"analyst td{n.id} closed: analysis: comment posted")
        elif kind == "task" and not kids:
            # A run_slice task closes through its land: the newest job is
            # then a succeeded land_slice (its ship closes the todo first;
            # this is the backstop). A succeeded run_slice alone never does.
            jobs = plan.jobs(n)
            if _op_type(n) == DEFAULT_OP and not (
                jobs and jobs[-1].meta.get("job_type") == "land_slice"
            ):
                continue
            # ``meta.rearmed_after_job`` — the floor ``_attempts`` honours,
            # stamped by a review REVERT or an answered ask — supersedes
            # the jobs at or below it: a newest job still under the floor
            # is the OLD land, and the todo stays open for its rebuild
            # (gr586688).
            floor = _rearm_floor(n)
            if jobs and jobs[-1].status == "succeeded":
                job_type = jobs[-1].meta.get("job_type") or _op_type(n)
                if floor is not None and jobs[-1].id <= floor:
                    res.notes.append(
                        f"task td{n.id} held open: newest {job_type} "
                        f"job:{jobs[-1].id} is at or below re-arm floor {floor}"
                    )
                    continue
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(
                    f"task td{n.id} closed: {job_type} job:{jobs[-1].id} succeeded"
                )


# ── (4) dispatch ───────────────────────────────────────────────────


def _dispatchable(plan: _Plan, n: _Node) -> bool:
    """Whether ``n`` is a node the tick runs jobs for (by shape only)."""
    if "ask" in n.meta:
        return False
    kind = node_kind(n.meta)
    kids = plan.child_todos(n)
    if kind == "task" or kind == "generator":
        return not kids
    return kind is None and not kids and n.id not in plan.roots


def _excluded(plan: _Plan, n: _Node) -> bool:
    """A doable-exclusion tag on ``n`` or any ancestor: a park rides the
    whole subtree — the root's ``waiting-for:coordinator`` holds every
    descendant out of dispatch, not just its own (td610546) — the same
    reach :meth:`_Plan.paused` and :meth:`_Plan.blocked` already give a
    ``STATUS:paused`` or a ``blocked-by`` target."""
    for node in (n, *plan.ancestors(n)):
        for value in node.open_tags:
            for t in _EXCLUSIONS:
                if (t.endswith(":") and value.startswith(t)) or value == t:
                    return True
    return False


def _node_repos(plan: _Plan, n: _Node) -> list[str]:
    """The ``repo:`` values governing ``n``: its own, else the nearest
    tagged ancestor's (the reach of :func:`_excluded`)."""
    for node in (n, *plan.ancestors(n)):
        repos = _lanes.repo_tags(node.open_tags)
        if repos:
            return repos
    return []


def _claims(n: _Node) -> list[str]:
    return [v for v in n.open_tags if v.startswith("claimed-by:")]


def _plan_root_of(store: Any, ref_id: int) -> int | None:
    """The live plan root ``ref_id`` sits under (itself included), or
    ``None`` outside a live plan: the highest live ancestor carrying the
    self ``plan:<id>`` tag or plan-kind meta — :func:`_candidate_roots`'
    two root shapes — so a root inside another root's subtree folds into
    the outer."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE walk(ref_id, parent_id, meta, lvl) AS (
                SELECT ref_id, parent_id, meta, 0
                  FROM refs WHERE ref_id = %s
                            AND kind = 'todo' AND retired_at IS NULL
                UNION ALL
                SELECT r.ref_id, r.parent_id, r.meta, w.lvl + 1
                  FROM refs r
                  JOIN walk w ON r.ref_id = w.parent_id
                 WHERE r.kind = 'todo' AND r.retired_at IS NULL
                   AND w.lvl < 25
            )
            SELECT w.ref_id, w.meta,
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = w.ref_id AND t.namespace = 'STATUS'
                     LIMIT 1),
                   EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = w.ref_id AND t.namespace = 'OPEN'
                        AND t.value = 'plan:' || w.ref_id
                   )
              FROM walk w ORDER BY w.lvl DESC
            """,
            (ref_id,),
        ).fetchall()
    for rid, meta, status, self_root in rows:
        if str(status or "open") in TERMINAL_TODO_STATUSES:
            continue
        if self_root or node_kind(dict(meta or {})) in (
            "gate",
            "loop",
            "generator",
            "phase",
        ):
            return int(rid)
    return None


def in_live_plan(store: Any, ref_id: int) -> bool:
    """Whether ``ref_id`` sits under a live plan subtree — the whole of
    the put-side mint barrier's test (td603429): every child a
    ``TodoHandler.put`` creates under one is born held
    (``waiting-for:mint``), unconditionally — no ancestry, job, lease
    or principal inference, each of which left a hole a review found
    (td599743, td601023, td601626, td602770, td603427). The minter's
    explicit completion releases the hold:
    :func:`precis.plans.nodes.finish_mint` for the factory's own mint
    sequences, an untag for an interactive minter, hygiene for a
    minter that never came back (gr574881)."""
    return _plan_root_of(store, ref_id) is not None


def _attempt_tag(n: _Node) -> int:
    best = 0
    for value in n.open_tags:
        m = _ATTEMPT_RE.match(value)
        if m:
            best = max(best, int(m.group(1)))
    return best


def _retries(n: _Node) -> int:
    raw = n.meta.get("retries", DEFAULT_RETRIES)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETRIES


def _voided(j: _Node) -> bool:
    """A job the seat voided by hand — a ``void:`` open tag on the job
    (``tag(kind='job', id=<jo>, add=['void:<why>'])``, gr568824): the run
    happened, the outcome is ignored — a same-vendor review, a dry run —
    so it burns no attempt and its comments are never judged."""
    return any(t.startswith("void:") for t in j.open_tags)


def _no_attempt(j: _Node) -> bool:
    """An ended job that burned no attempt: the lane's host refused the
    launch (``meta.host_refusal``, gr571395), it failed outside the
    diff's control (``meta.attempt_free``: a lane that never freed a slot,
    gr572118; a land the gate infrastructure killed, gr570187), or the
    seat voided it (:func:`_voided`, gr568824)."""
    return bool(
        j.meta.get("host_refusal") or j.meta.get(ATTEMPT_FREE_KEY) or _voided(j)
    )


def _attempt_jobs(plan: _Plan, n: _Node) -> list[_Node]:
    """The ended jobs counting as attempts on ``n``, oldest first —
    what :func:`_attempts` counts."""
    asks = [b for b in plan.blockers.get(n.id, []) if b.is_ask]
    since = max((b.created_at for b in asks), default=None)
    floor = _rearm_floor(n)
    jobs = plan.jobs(n)
    return [
        j
        for i, j in enumerate(jobs)
        # Ended only: a job still queued — never claimed, e.g. waiting for
        # a host with its placement capabilities (td543917) — is no attempt.
        if j.status in _JOB_ENDED
        and not _no_attempt(j)
        and (since is None or j.created_at > since)
        and (floor is None or j.id > floor)
        # A build that handed off to the lander is one attempt with its
        # land_slice, not two.
        and not (
            j.status == "succeeded"
            and j.meta.get("job_type") == DEFAULT_OP
            and any(k.meta.get("job_type") == "land_slice" for k in jobs[i + 1 :])
        )
    ]


def _attempts(plan: _Plan, n: _Node) -> int:
    """Ended jobs since the newest ask on ``n`` (all of them, floored by the
    ``attempt:<n>`` tag, when there was never an ask). A REVERT verdict and
    an answered ask (:func:`_step_rearm`) re-arm a node: only jobs after
    ``meta.rearmed_after_job`` count. A job carrying ``meta.host_refusal``
    or ``meta.attempt_free`` never counts: nothing on the diff was tried
    (:func:`_no_attempt`)."""
    ended = _attempt_jobs(plan, n)
    asks = [b for b in plan.blockers.get(n.id, []) if b.is_ask]
    if not asks and _rearm_floor(n) is None:
        return max(len(ended), _attempt_tag(n))
    return len(ended)


def _human_hit(plan: _Plan, n: _Node) -> list[str]:
    """The ``lanes.HUMAN_ONLY_PATHS`` paths the newest attempt's branch
    touched, or ``[]`` — the job-meta stamp :data:`HUMAN_ONLY_KEY`
    ``run_slice`` writes (td584381 S1a). Only the newest attempt job's
    stamp counts: a stamped job below the re-arm floor or the newest ask
    was already answered for."""
    ended = _attempt_jobs(plan, n)
    if not ended or ended[-1].status != "failed":
        return []
    hits = ended[-1].meta.get(HUMAN_ONLY_KEY)
    if isinstance(hits, list):
        return sorted(str(p) for p in hits)
    return [str(hits)] if hits else []


def _no_commit(plan: _Plan, n: _Node) -> bool:
    """The newest attempt ended ``failed`` carrying ``run_slice``'s
    :data:`NO_COMMIT_KEY` stamp — a clean exit that left no commit on
    ``slice/<id>``, the builder's can't-finish (td584381 S1a F4). The
    attempt climbs the ladder like any red; only the cap — the ladder
    spent — routes the node to the coordinator ask (gr645163)."""
    ended = _attempt_jobs(plan, n)
    return bool(
        ended and ended[-1].status == "failed" and ended[-1].meta.get(NO_COMMIT_KEY)
    )


def _coordinator_hit(plan: _Plan, n: _Node) -> str | None:
    """The ``ci_red_digest.COORDINATOR_ASK_KEY`` stamp's reason on the
    newest attempt job — a persistent infra red or a refused rerun,
    whose cap ask routes to the coordinator (td612107 review). Only the
    newest attempt's stamp counts (``_human_hit``'s scope rule): a
    stamped job below the re-arm floor or the newest ask was already
    answered for."""
    ended = _attempt_jobs(plan, n)
    if not ended or ended[-1].status != "failed":
        return None
    why = ended[-1].meta.get(COORDINATOR_ASK_KEY)
    return str(why) if why else None


def _model_rule_hit(plan: _Plan, n: _Node) -> str | None:
    """The ``run_slice.MODEL_RULE_KEY`` stamp's reason on the newest
    attempt job — a submit refusal on the test session's
    different-model rule (gr657521). Only the newest attempt's stamp
    counts (``_human_hit``'s scope rule): a stamped job below the
    re-arm floor or the newest ask was already answered for."""
    ended = _attempt_jobs(plan, n)
    if not ended or ended[-1].status != "failed":
        return None
    why = ended[-1].meta.get(MODEL_RULE_KEY)
    return str(why) if why else None


def _rearm_floor(n: _Node) -> int | None:
    raw = n.meta.get("rearmed_after_job")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _infra_red_lands(plan: _Plan, n: _Node) -> int:
    """The run of consecutive failed/timed-out ``land_slice`` jobs marked
    ``attempt_free == "infra-land"`` at the tail of ``n``'s jobs, counting
    only jobs after ``meta.rearmed_after_job`` (an answered ask re-arms,
    gr570187). A land red on the diff itself — or any other job — ends the
    run: the node goes back through a build, not another land. A
    wall-deadline kill carries ``STATUS:timed-out`` instead of ``failed``
    (td648501) but is the same infra-class red."""
    floor = _rearm_floor(n)
    count = 0
    for j in reversed(plan.jobs(n)):
        if floor is not None and j.id <= floor:
            break
        if not (
            j.status in ("failed", "timed-out")
            and j.meta.get("job_type") == "land_slice"
            and j.meta.get(ATTEMPT_FREE_KEY) == "infra-land"
        ):
            break
        count += 1
    return count


def _free_run(plan: _Plan, n: _Node) -> tuple[str | None, int]:
    """The consecutive run of identical ``attempt_free`` reasons on
    failed/timed-out jobs at the tail of ``n``'s jobs — ``(reason,
    count)``, ``(None, 0)`` when the newest job is not a free failure.
    Floored by ``meta.rearmed_after_job`` like :func:`_infra_red_lands`:
    an answered ask re-arms the run. A different reason, a real attempt,
    a success or a non-ended job ends it (gr662077)."""
    floor = _rearm_floor(n)
    reason: str | None = None
    count = 0
    for j in reversed(plan.jobs(n)):
        if floor is not None and j.id <= floor:
            break
        if j.status not in ("failed", "timed-out"):
            break
        r = j.meta.get(ATTEMPT_FREE_KEY)
        if not r:
            break
        if reason is None:
            reason = str(r)
        elif str(r) != reason:
            break
        count += 1
    return reason, count


def _free_done(plan: _Plan, n: _Node) -> str | None:
    """The identical ``attempt_free`` reason whose consecutive run hit
    :data:`FREE_RUN_CAP` — what :func:`_capped` turns into the
    coordinator ask — or ``None`` while under the cap (gr662077)."""
    reason, run = _free_run(plan, n)
    return reason if reason is not None and run >= FREE_RUN_CAP else None


def _brief_rewritten(store: Any, todo_id: int, since: datetime) -> bool:
    """A ``brief:``/``handoff:`` comment on the task newer than ``since``
    (the re-land ask's mint): the brief changed, so a builder on the new
    brief — not a re-land of the old branch — is what the answer means."""
    for c in _comments(store, todo_id):
        if c.created_at <= since:
            break
        if str(c.text).lstrip().lower().startswith(("brief:", "handoff:")):
            return True
    return False


def _reland_params(plan: _Plan, n: _Node, store: Any) -> dict[str, Any] | None:
    """The failed land job's own ``params`` when the tick should re-mint
    ``land_slice`` — the newest job is an infra-land failure and the run
    is under :data:`LAND_AGAIN_CAP` — else ``None``. The lander re-runs
    ship on the same branch; no builder is asked to fix what the gate's
    environment broke. On the first tick after an answered re-land ask
    (the newest ended job is still ``meta.rearmed_after_job``) the
    re-land fires only when the answer names ``land again (no builder)``
    AND the brief is untouched — a rewritten brief mints a builder on
    the new brief instead (gr570187)."""
    jobs = plan.jobs(n)
    if not jobs:
        return None
    j = jobs[-1]
    if not (
        j.status in ("failed", "timed-out")
        and j.meta.get("job_type") == "land_slice"
        and j.meta.get(ATTEMPT_FREE_KEY) == "infra-land"
    ):
        return None
    if _infra_red_lands(plan, n) >= LAND_AGAIN_CAP:
        return None
    if n.meta.get("rearmed_ask_at") and _rearm_floor(n) == j.id:
        answer = str(n.meta.get("rearmed_answer") or "").strip().lower()
        if not answer.startswith("land again"):
            return None
        try:
            since = datetime.fromisoformat(str(n.meta["rearmed_ask_at"]))
        except ValueError:  # a stamp we cannot read must not park the land
            since = None
        if since is not None and _brief_rewritten(store, n.id, since):
            return None
    params = j.meta.get("params")
    return dict(params) if isinstance(params, dict) else None


def _land_again_capped(plan: _Plan, n: _Node) -> bool:
    """The infra-land run is done on its own: ``LAND_AGAIN_CAP``
    consecutive infra-red lands, or the newest job stamped
    ``meta.land_again_cap`` by the lander itself (a capped exit-137 —
    its gate-kill retries are spent, so the run goes to the ask, not to
    another automatic land, gr570187). The mark only counts past
    ``meta.rearmed_after_job``: an answered "land again" re-arms it."""
    floor = _rearm_floor(n)
    if _infra_red_lands(plan, n) >= LAND_AGAIN_CAP:
        return True
    jobs = plan.jobs(n)
    j = jobs[-1] if jobs else None
    return bool(
        j is not None
        and j.status in ("failed", "timed-out")
        and j.meta.get("job_type") == "land_slice"
        and j.meta.get(ATTEMPT_FREE_KEY) == "infra-land"
        and j.meta.get("land_again_cap")
        and (floor is None or j.id > floor)
    )


# ── mechanical-red briefs (gr604579) ─────────────────────────


def _conflict_paths(text: str) -> str:
    """The conflicted paths the failure text names, or the generic
    phrase — what ``{paths}`` in the conflict instruction fills with."""
    seen: list[str] = []
    for pat in _CONFLICT_PATH_RES:
        for path in pat.findall(text):
            if path not in seen:
                seen.append(path)
    if not seen:
        return "the conflicted paths"
    out = ", ".join(seen[:6])
    return out + (" …" if len(seen) > 6 else "")


def _mechanical_brief(error_line: str | None) -> str | None:
    """The ``brief:`` fix one error line maps to (gr604579) — the
    instruction a coordinator used to write by hand — or ``None`` when
    no known signature matches. Pure: one stripped line in, the
    instruction out; a ``{paths}`` hole stays literal for the caller to
    fill (:func:`_mech_hit` fills it from the whole event text)."""
    if not error_line:
        return None
    for _slug, pat, brief in _MECHANICAL_REDS:
        if pat.search(error_line):
            return brief
    return None


def _mech_hit(text: str) -> tuple[str, str, str] | None:
    """``(signature slug, brief instruction, decisive line)`` for the
    LAST line of ``text`` carrying a known mechanical signature — the
    whole job event text is scanned, since the skill classes' ✖ is a
    generic "lint is RED" and the decisive line sits in the log tail;
    the tail-most match wins so a stray early line can't shadow the
    real cause. The instruction comes through
    :func:`_mechanical_brief`, its ``{paths}`` filled from the whole
    text — a multi-file conflict names every file."""
    found: tuple[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for slug, pat, _brief in _MECHANICAL_REDS:
            if pat.search(line):
                found = (slug, line)
                break
    if found is None:
        return None
    slug, line = found
    brief = _mechanical_brief(line)
    if brief is None:  # the winning line classified a moment ago
        return None
    if "{paths}" in brief:
        brief = brief.replace("{paths}", _conflict_paths(text))
    return slug, brief, line


def _mech_red(
    plan: _Plan, store: Any, n: _Node
) -> tuple[str, str, str, int, int] | None:
    """The newest attempt job's mechanical red, or ``None``.

    Returns ``(slug, brief, line, repeats, job_id)`` — the signature,
    its instruction, the decisive line, how many consecutive failed
    attempt jobs at the tail classify to the SAME signature (1 = this
    red is new), and the job it came from. Older jobs' event text is
    read lazily backwards only until the count is settled: a different
    or unclassified red, or a non-failed job, breaks the run. The count
    lives in the job rows, not a stamp — an answered ask's re-arm floor
    already scopes :func:`_attempt_jobs`, so the budget resets itself.
    """
    ended = _attempt_jobs(plan, n)
    if not ended or ended[-1].status != "failed":
        return None
    last = ended[-1]
    hit = _mech_hit(_job_event_text(store, last.id))
    if hit is None:
        return None
    slug, brief, line = hit
    repeats = 1
    for j in reversed(ended[:-1]):
        if repeats > MECH_RETRY_CAP or j.status != "failed":
            break
        prev = _mech_hit(_job_event_text(store, j.id))
        if prev is None or prev[0] != slug:
            break
        repeats += 1
    return slug, brief, line, repeats, int(last.id)


def _mech_pin(
    plan: _Plan,
    w: _Writer,
    n: _Node,
    rungs: tuple[_lanes.Rung, ...],
    params: dict[str, Any],
    rules: dict[str, dict[str, Any]] | None,
) -> bool:
    """Pin ``params`` to the ladder rung the newest builder job ran on —
    a mechanical red retries where it failed, not a rung up (gr604579:
    a 4000-char skill chunk does not need swe-2-max). ``False`` when the
    op already pins, no builder job names a rung, that rung is off
    today's ladder, sits on a lane a host refusal backed off (gr571395
    — :func:`_escalate` owns the skip), or its ``rung_caps`` cap is
    already spent — the retry then climbs through :func:`_escalate` as
    before, carrying the brief the caller posted."""
    if params.get("lane") or params.get("model"):
        return False
    lane = model = None
    for j in reversed(plan.jobs(n)):
        if str(j.meta.get("job_type") or DEFAULT_OP) != DEFAULT_OP:
            continue
        lane = j.meta.get("lane") or (j.meta.get("params") or {}).get("lane")
        model = j.meta.get("model") or (j.meta.get("params") or {}).get("model")
        break
    if not lane:
        return False
    idx = _lanes.rung_index(rungs, str(lane), str(model) if model else None)
    if idx is None:
        return False
    target = rungs[idx]
    if target.lane in _lanes.lane_backoffs(w.store):
        return False
    cap_usd = _rung_cap_usd(target, rules) if rules else None
    spend = _node_spend_usd(plan, n, rules) if rules else 0.0
    if cap_usd is not None and spend >= cap_usd:
        return False
    params["lane"] = target.lane
    if target.model is not None:
        params["model"] = target.model
    if cap_usd is not None:
        # Same allowance ``_escalate`` stamps onto a capped rung — the
        # pinned retry must not outrun the node's USD bound (td601015).
        w.stamp_meta(
            n,
            {
                SPEND_KEY: {
                    "usd": round(spend, 4),
                    "cap_usd": cap_usd,
                    "blocked": False,
                    "at": _iso(datetime.now(UTC)),
                }
            },
        )
        params["budget_usd"] = round(cap_usd - spend, 4)
    return True


def _mech_done(plan: _Plan, n: _Node) -> str | None:
    """The exhausted mechanical signature — ``meta.mech_retries``
    stamped ``exhausted`` on the node's NEWEST attempt job — or
    ``None``. A re-arm (``rearmed_after_job``) or any newer attempt
    retires the stamp, so the next red starts a fresh count
    (gr604579)."""
    stamp = n.meta.get(MECH_RETRIES_KEY)
    if not isinstance(stamp, dict) or not stamp.get("exhausted"):
        return None
    ended = _attempt_jobs(plan, n)
    if ended and ended[-1].id == stamp.get("job"):
        return str(stamp.get("sig") or "mechanical")
    return None


# ── per-node spend caps (gr599486) ─────────────────────────────


def _usd_number(v: Any) -> float | None:
    """``float(v)`` when ``v`` is a finite number, else ``None`` — a
    bool or a non-number never counts toward spend or caps."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _spend_rules(store: Any) -> dict[str, dict[str, Any]]:
    """Per-lane spend rules from ``llm_account.entitlement`` (gr599486):
    ``{lane: {"caps": {model: usd}, "acu_usd": rate}}``. ``rung_caps``
    maps a model id to the per-node USD cap for attempts pinned to it
    (Reto's $25 for ``claude-fable-5-1-medium`` on devin, billed to
    Devin credits); ``acu_usd`` is the lane's ACU→USD rate for
    ``meta.usage.acus_consumed``. A lane with neither has no spend rule.
    Read at call time through the accounts module attribute — the same
    seam ``lanes.load_lanes``'s tests patch."""
    from precis.utils.llm import accounts

    out: dict[str, dict[str, Any]] = {}
    for a in accounts.list_accounts(store):
        ent = a.entitlement if isinstance(a.entitlement, dict) else {}
        caps: dict[str, float] = {}
        raw = ent.get("rung_caps")
        if isinstance(raw, dict):
            for model, usd in raw.items():
                v = _usd_number(usd)
                if v is not None and v > 0:
                    caps[str(model)] = v
        rate = _usd_number(ent.get("acu_usd"))
        if caps or (rate is not None and rate > 0):
            out[str(a.name)] = {
                "caps": caps,
                "acu_usd": (rate if rate is not None and rate > 0 else DEFAULT_ACU_USD),
            }
    return out


def _job_spend_usd(meta: dict[str, Any], rules: dict[str, dict[str, Any]]) -> float:
    """USD the job row's ``meta.usage`` reports having cost —
    ``on_demand_usd``/``cost_usd`` direct, ``acus_consumed`` at the
    job's lane's ``acu_usd`` rate (gr599486)."""
    usage = meta.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    total = 0.0
    for key in ("on_demand_usd", "cost_usd"):
        v = _usd_number(usage.get(key))
        if v is not None and v > 0:
            total += v
    acus = _usd_number(usage.get("acus_consumed"))
    if acus is not None and acus > 0:
        lane = str(meta.get("lane") or "")
        total += acus * float(rules.get(lane, {}).get("acu_usd", DEFAULT_ACU_USD))
    return total


def _node_spend_usd(plan: _Plan, n: _Node, rules: dict[str, dict[str, Any]]) -> float:
    """The node's recorded spend — summed over every job row parented
    to it, ended or live (a live job's reported usage is real spend)."""
    return sum(_job_spend_usd(j.meta, rules) for j in plan.jobs(n))


def _rung_cap_usd(rung: _lanes.Rung, rules: dict[str, dict[str, Any]]) -> float | None:
    """The per-node USD cap a ``rung_caps`` entry puts on this rung's
    model — ``None`` for an uncapped rung, which is never
    spend-blocked."""
    if rung.model is None:
        return None
    v = rules.get(rung.lane, {}).get("caps", {}).get(rung.model)
    return float(v) if v is not None else None


def _climb_start(
    plan: _Plan, n: _Node, rungs: tuple[_lanes.Rung, ...], attempts: int
) -> int:
    """The ladder index the next attempt climbs from — one rung above
    where the newest REAL attempt ran (a no-attempt job is no rung to
    climb from, gr571395), else the attempt count, floored at the top.
    The ``_escalate`` floor, factored so :func:`_spend_hit` reads the
    same tail the climb does."""
    ended = [j for j in plan.jobs(n) if j.status in _JOB_ENDED and not _no_attempt(j)]
    prev = (
        _lanes.rung_index(
            rungs, ended[-1].meta.get("lane"), ended[-1].meta.get("model")
        )
        if ended
        else None
    )
    return min(prev + 1 if prev is not None else attempts, len(rungs) - 1)


def _chain_floor(n: _Node) -> int:
    """The ladder rung a deep fix node's first attempt starts on
    (gr603118): a chain at :data:`FIX_BRIEF_DEPTH` or deeper begins one
    rung up — swe-2-max on the devin table — since the foot rung's model
    produced every partial fix in the chain. 0 for anything shallower
    (an untagged first fix climbs from the foot as before)."""
    depth = n.meta.get(FIX_DEPTH_KEY)
    if isinstance(depth, bool) or not isinstance(depth, int):
        return 0
    return 1 if depth >= FIX_BRIEF_DEPTH else 0


def _spend_hit(
    plan: _Plan,
    n: _Node,
    rungs: tuple[_lanes.Rung, ...],
    rules: dict[str, dict[str, Any]],
) -> tuple[float, float] | None:
    """``(spend, cap_usd)`` when every rung at or above the node's
    climb floor carries a ``rung_caps`` cap its recorded spend already
    meets — the ladder is spent on dollars, not retries (gr599486).
    ``None`` when no caps apply or an affordable rung remains."""
    if not rungs or not rules:
        return None
    spend = _node_spend_usd(plan, n, rules)
    if spend <= 0:
        return None
    start = _climb_start(plan, n, rungs, _attempts(plan, n))
    caps = [_rung_cap_usd(rungs[i], rules) for i in range(start, len(rungs))]
    if any(c is None or spend < c for c in caps):
        return None
    return spend, min(c for c in caps if c is not None)


def _task_rungs(
    lane_table: tuple[_lanes.Lane, ...], n: _Node, params: dict[str, Any]
) -> tuple[_lanes.Rung, ...]:
    """The ladder ``n`` climbs: every enabled fit rung for its task
    class, narrowed to the lane its ``meta.op`` pins — the same
    narrowing :func:`_escalate` applies (td584381 S1a)."""
    task_class = str(n.meta.get("task_class") or _DEFAULT_TASK_CLASS)
    rungs = _lanes.ladder(task_class, lanes=lane_table)
    pinned = str(params["lane"]) if params.get("lane") else None
    if pinned:
        rungs = tuple(r for r in rungs if r.lane == pinned)
    return rungs


def _cap_rungs(
    plan: _Plan,
    lane_table: tuple[_lanes.Lane, ...],
    n: _Node,
    job_type: str,
    params: dict[str, Any],
    rules: dict[str, dict[str, Any]] | None = None,
) -> tuple[_lanes.Rung, ...]:
    """The ladder the retry cap counts, in one place (td584381 S1a
    F2/F3): ``()`` for a non-run_slice op or a model-pinned one —
    ``_escalate`` never climbs those, so the cap falls back to
    :data:`DEFAULT_RETRIES`. A reviewer climbs inside the lane its
    newest attempt ran on (``_review_lane``'s pick): its cap is that
    lane's rung count, never the full review-class table's. A rung a
    ``rung_caps`` cap refuses at the node's recorded spend is out of
    the budget too — the spend cap shrinks the ladder the retry cap
    counts (gr599486)."""
    if job_type != DEFAULT_OP or "model" in params:
        return ()
    rungs = _task_rungs(lane_table, n, params)
    if _is_reviewer(n) and not params.get("lane"):
        ended = _attempt_jobs(plan, n)
        last = ended[-1] if ended else None
        # ``meta.lane`` is stamped at submit; the job's own params carry
        # the pick before that (and always for a minted-but-unrun job).
        lane = (
            last.meta.get("lane") or (last.meta.get("params") or {}).get("lane")
            if last is not None
            else None
        )
        if lane is not None:
            rungs = tuple(r for r in rungs if r.lane == lane)
    if rules:
        spend = _node_spend_usd(plan, n, rules)
        if spend > 0:
            rungs = tuple(
                r for r in rungs if (c := _rung_cap_usd(r, rules)) is None or spend < c
            )
    return rungs


def _retry_cap(n: _Node, rungs: tuple[_lanes.Rung, ...]) -> int:
    """The attempts budget (td584381 S1a): an explicit ``meta.retries``
    pins the number; else the node climbs its whole lane ladder — one
    rung per attempt — before the cap fires. No ladder at all keeps
    :data:`DEFAULT_RETRIES`, the no-lane case that still mints the ask."""
    if n.meta.get("retries") is not None:
        return _retries(n)
    return len(rungs) or DEFAULT_RETRIES


def _ladder_spent(plan: _Plan, n: _Node, rungs: tuple[_lanes.Rung, ...]) -> bool:
    """The newest real attempt already ran the ladder's top rung — no
    climb is left, so the ladder is spent whatever the attempt count
    says (td645566): a fix born model-pinned at
    :data:`FIX_ESCALATE_DEPTH` starts there and its ``cap`` is
    :data:`DEFAULT_RETRIES`, so the first failure retries the same top
    rung the count still affords. ``False`` when there is no ladder or
    the rung cannot be read — the attempt count decides as before."""
    if not rungs:
        return False
    ended = _attempt_jobs(plan, n)
    if not ended:
        return False
    last = ended[-1]
    return (
        _lanes.rung_index(rungs, last.meta.get("lane"), last.meta.get("model"))
        == len(rungs) - 1
    )


def _capped(
    plan: _Plan,
    n: _Node,
    cap: int,
    *,
    rungs: tuple[_lanes.Rung, ...] = (),
    spend_capped: bool = False,
) -> bool:
    jobs = plan.jobs(n)
    # The newest job need only be TERMINAL: a cancelled job burns no
    # attempt itself (:func:`_attempt_jobs` skips it), but the attempts
    # below it still cap — an operator's cancel is a free re-queue,
    # never a bypass of the retry budget (gr587772).
    if not jobs or jobs[-1].status not in _JOB_TERMINAL:
        return False
    # A human-only hit caps at once: no lane may write those paths, so
    # no rung and no rescue can fix the failure (td584381 S1a).
    if _human_hit(plan, n):
        return True
    # A different-model refusal caps at once: it is a routing verdict,
    # not a lane failure — the next rung hits the same refusal
    # (gr657521).
    if _model_rule_hit(plan, n) is not None:
        return True
    if _land_again_capped(plan, n):
        return True
    # A mechanical signature whose brief budget is spent counts as
    # capped: the third repeat goes to the ask, not to a third same-rung
    # retry (gr604579).
    if _mech_done(plan, n) is not None:
        return True
    # Identical ``attempt_free`` failures burn no attempt, so the retry
    # cap below never sees them; the run has its own bound — the
    # coordinator ask, not another doomed mint (gr662077).
    if _free_done(plan, n) is not None:
        return True
    # A spend cap is spent money, not retries — every rung left for the
    # node sits over its per-node USD bound (gr599486).
    if spend_capped:
        return True
    attempts = _attempts(plan, n)
    if attempts <= 0:
        # With ``meta.retries: 0`` a no-attempt job (a host refusal)
        # must not mint the cap ask — nothing was tried (gr571395).
        return False
    if attempts >= cap:
        return True
    # A can't-finish whose newest attempt already ran the top rung has
    # no climb left — it IS the cap (gr645163's "only the top rung's
    # failure files the coordinator ask"), even when the count still
    # affords a same-rung retry (td645566).
    return _no_commit(plan, n) and _ladder_spent(plan, n, rungs)


def _resolve_op(n: _Node) -> tuple[str, dict[str, Any]]:
    """``meta.op`` as ``(job_type, params)``."""
    op = n.meta.get("op")
    if isinstance(op, dict):
        job_type = str(op.get("type") or DEFAULT_OP)
        params = dict(op.get("params") or {})
        if job_type == DEFAULT_OP:
            params.setdefault("todo_id", n.id)
        return job_type, params
    if isinstance(op, str) and op.strip():
        return op.strip(), {"todo_id": n.id}
    return DEFAULT_OP, {"todo_id": n.id}


def _params_problem(schema: dict[str, Any], params: dict[str, Any]) -> str | None:
    """The submit path's static check: required keys, no unknown keys."""
    missing = [k for k in schema.get("required", []) if k not in params]
    if missing:
        return f"params miss {missing}"
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(params) - set(schema.get("properties", {})))
        if unknown:
            return f"params carry unknown keys {unknown}"
    return None


#: ``meta.task_class`` of a plain build node (mirrors ``run_slice``'s default).
_DEFAULT_TASK_CLASS = "code"


def _escalate(
    w: _Writer,
    plan: _Plan,
    n: _Node,
    params: dict[str, Any],
    lane_table: tuple[_lanes.Lane, ...],
    rules: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Pin a retried ``run_slice`` one rung up the cross-lane ladder.

    Reto's ruling (td559457, 2026-11-03): when a slice fails, go up the
    chain before asking — free vendor lanes first, then the Claude
    subscription's models. The chain is :func:`precis.plans.lanes.ladder`
    for the node's task class (every enabled fit lane in table order, its
    models from the default upward). The retry climbs one rung above where
    the newest ended attempt ran (its job's ``meta.lane``/``meta.model``);
    when that is unknown, attempt *k* takes rung *k* from the foot. Floored
    at the top rung. A node whose ``meta.op`` already pins ``model`` is left
    alone (gr549046: the triage's pick wins); one that pins ``lane`` climbs
    within that lane only. Rungs on lanes a host refusal backed off are
    skipped — an escalated retry would park there, not build (gr571395);
    the floor is read on the FULL ladder so a backed-off lane never drops
    the retry below the last real attempt's rung, and when nothing free
    sits at or above the floor the node parks (lane-wait) rather than
    climbing down. A rung a ``rung_caps`` cap refuses at the node's
    recorded spend is skipped the same way (gr599486): the all-blocked
    tail was already caught by :func:`_spend_hit`'s cap, so a pick that
    runs out here means a backed-off lane shares the tail — the note
    names both. A climb onto a capped rung stamps ``meta.spend`` so the
    node carries its spend record (``blocked: False`` = headroom left)
    and mints the job with ``params.budget_usd`` = the remaining
    allowance — the bound ``run_slice`` enforces mid-run (td601015).

    Returns ``(go, note, stamp)``: ``go=False`` parks the node this tick —
    no job is minted — ``note`` is the action line for the summary, and
    ``stamp`` is the decide() shadow record the minted job carries on
    ``meta.decision`` (td572552 S4b — ``None`` under the default legacy
    mode, and always ``None`` when the climb was skipped).
    """
    if "model" in params:
        return True, None, None
    # A reviewer whose newest job left no readable verdict retries on the
    # lane ``_review_lane`` picked: a format miss says nothing about the
    # lane's strength, and the climb pinned swe-2-max while the verdict
    # itself was lost to a parse miss (gr573564). The attempt still
    # counts, so the retry cap and its ask are unchanged.
    seen = n.meta.get("review")
    jobs = plan.jobs(n)
    if (
        _is_reviewer(n)
        and isinstance(seen, dict)
        and seen.get("verdict") is None
        and jobs
        and seen.get("job") == jobs[-1].id
    ):
        return True, None, None
    attempts = _attempts(plan, n)
    floor = _chain_floor(n)
    if attempts <= 0 and floor <= 0:
        return True, None, None
    rungs = _task_rungs(lane_table, n, params)
    if not rungs:
        return True, None, None
    start = max(_climb_start(plan, n, rungs, attempts), min(floor, len(rungs) - 1))
    backed = set(_lanes.lane_backoffs(w.store))
    spend = _node_spend_usd(plan, n, rules) if rules else 0.0
    spend_blocked = frozenset(
        i
        for i, r in enumerate(rungs)
        if (c := _rung_cap_usd(r, rules or {})) is not None and spend >= c
    )
    idx = next(
        (
            i
            for i in range(start, len(rungs))
            if rungs[i].lane not in backed and i not in spend_blocked
        ),
        None,
    )
    if idx is None:
        blocked_lanes = sorted({r.lane for r in rungs[start:]})
        note = (
            f"td{n.id}: lane-wait — every rung from {start + 1}/{len(rungs)} "
            f"up sits on a backed-off lane ({', '.join(blocked_lanes)}); the "
            "retry waits, it never climbs down"
        )
        capped = sorted(
            {rungs[i].model or "default" for i in spend_blocked if i >= start}
        )
        if capped:
            note += (
                f"; rung(s) {', '.join(capped)} also sit over the node's "
                f"${spend:.2f} recorded spend"
            )
        return False, note, None
    target = rungs[idx]
    cap_usd = _rung_cap_usd(target, rules) if rules else None
    if cap_usd is not None:
        w.stamp_meta(
            n,
            {
                SPEND_KEY: {
                    "usd": round(spend, 4),
                    "cap_usd": cap_usd,
                    "blocked": False,
                    "at": _iso(datetime.now(UTC)),
                }
            },
        )
        # The remaining allowance rides the minted job: ``run_slice``
        # kills the session once its live usage meets it, so an attempt
        # that started under the cap cannot run past it (td601015).
        params["budget_usd"] = round(cap_usd - spend, 4)
    params["lane"] = target.lane
    if target.model is not None:
        params["model"] = target.model
    model_bit = f" model {target.model}" if target.model else ""
    if attempts <= 0:
        head = (
            f"td{n.id}: chain depth {n.meta.get(FIX_DEPTH_KEY)} starts one "
            f"rung up on lane {target.lane}"
        )
    else:
        head = f"td{n.id}: retry {attempts} climbs to lane {target.lane}"
    stamp = _lane_decide.shadow_stamp(
        w.store,
        task_class=str(n.meta.get("task_class") or _DEFAULT_TASK_CLASS),
        table=lane_table,
        site="escalate",
        picked=(target.lane, target.model),
        rungs=rungs,
        chosen_rung=idx,
        required_capability=params.get("lane_capability"),
        code_only=str(n.meta.get("task_class") or _DEFAULT_TASK_CLASS)
        == _DEFAULT_TASK_CLASS,
    )
    return True, f"{head}{model_bit} (rung {idx + 1}/{len(rungs)})", stamp


def _pick_executor(compatible: frozenset[str]) -> str:
    for name in _EXECUTOR_ORDER:
        if name in compatible:
            return name
    return sorted(compatible)[0]


#: Job types a plan root's ``meta.claim_band`` stamp rides onto — the
#: build and its land; the claim order reads it from the job row
#: (``executors/_common._CLASS_RANK_CASE``).
_BAND_JOB_TYPES = ("run_slice", "land_slice")


def _root_claim_band(store: Any, root_id: int) -> str | None:
    """The plan root's ``meta.claim_band`` (the waiver gripe's chain
    stamps ``release``), else ``None``. Unreadable reads as no band."""
    try:
        ref = store.get_ref(kind="todo", id=root_id)
    except Exception:
        return None
    band = (ref.meta or {}).get(_dscore.CLAIM_BAND_META_KEY) if ref else None
    return band if band == _dscore.RELEASE_CLAIM_BAND else None


def _mint_job(
    store: Any,
    n: _Node,
    job_type: str,
    executor: str,
    params: dict[str, Any],
    *,
    idem_key: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> int:
    from precis.workers._mint import mint_job_once

    stale = [v for v in n.open_tags if v.startswith("child-failed:")]
    meta_extra = dict(meta_extra or {})
    band = _root_claim_band(store, n.root) if job_type in _BAND_JOB_TYPES else None
    if band is not None:
        meta_extra[_dscore.CLAIM_BAND_META_KEY] = band
    with store.tx() as conn:
        job_id, _reused = mint_job_once(
            store,
            conn=conn,
            job_type=job_type,
            executor=executor,
            title=f"{job_type} td{n.id}: {n.title.splitlines()[0][:70] if n.title else ''}",
            params=params,
            parent_id=n.id,
            idem_key=idem_key or f"{NAME}:td{n.id}",
            prio=n.prio,
            meta_extra={"plan_root": n.root, **meta_extra},
        )
        for value in stale:
            store.remove_tag(n.id, Tag.open(value), conn=conn)
        store.append_event(
            n.id,
            source=NAME,
            event="job-minted",
            payload={"job_id": job_id, "job_type": job_type, "executor": executor},
            conn=conn,
        )
    for value in stale:
        n.open_tags.pop(value, None)
    return job_id


def _stamped_score(node: _Node) -> float | None:
    """``meta.dispatch_score`` on one node, or ``None``."""
    raw = node.meta.get(_dscore.SCORE_META_KEY)
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _effective_score(plan: _Plan, n: _Node) -> float:
    """``n``'s dispatch score — the machine queue order (gr604593).

    The nearest ancestor-chain ``meta.dispatch_score`` (the plan root's
    stamp — inherited from the gripe, the PR class or the release class
    at mint — wins over a child's), else :data:`DEFAULT_SCORE`, times
    :func:`~precis.utils.dispatch_score.prio_multiplier` of the nearest
    human-set ``prio`` up the chain (a judgement multiplies the score;
    it is never the key), times the ``showstopper`` lift when the node
    or an ancestor carries the tag. Holds (``blocked-by``,
    ``waiting-for:``, exclusion tags) still gate dispatch outright —
    the score orders only what they let through."""
    base = DEFAULT_SCORE
    stamped = False
    prio: int | None = None
    show = False
    for node in (n, *plan.ancestors(n)):
        if prio is None and node.prio is not None:
            prio = node.prio
        if "showstopper" in node.open_tags:
            show = True
        if not stamped and (s := _stamped_score(node)) is not None:
            base, stamped = s, True
    score = base * _dscore.prio_multiplier(prio)
    return score * _dscore.SHOWSTOPPER_MULT if show else score


def _review_lane(
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    lane_table: tuple[_lanes.Lane, ...],
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """``(go, lane name, stamp)`` for a reviewer's ``run_slice``: a lane of
    a vendor that did not build what it reviews; ``go=False`` when every
    fit lane is full. ``stamp`` is the decide() shadow record for the
    pick — ``meta.decision`` on the minted job (td572552 S4b; ``None``
    under the default legacy mode).

    Three steps, each a pick over a table shrunk by hand (never
    ``exclude_vendor``, whose own fallback could hand back an excluded
    vendor): every vendor that built on the node is out; if that leaves no
    enabled fit lane, only the original builder's vendor is out (a codex
    build repaired by sonnet is reviewed by claude-sub, not by codex); only
    when even that leaves nothing does the full table apply, with the note.
    Codex-built fixes get a Claude review (Reto's yes to 27b357ee5, via
    BUILDER 2026-11-05)."""
    cls = _templates.REVIEW_TASK_CLASS
    vendors = _builder_vendors(plan, n, lane_table)
    steps: list[tuple[frozenset[str], str]] = [
        (frozenset(vendors), "every build vendor"),
        (frozenset(vendors[:1]), "the original builder's vendor"),
        (frozenset(), ""),
    ]
    for excluded, label in steps:
        table = tuple(ln for ln in lane_table if ln.vendor not in excluded)
        if not any(ln.enabled and not ln.parked and ln.fits(cls) for ln in table):
            continue  # nothing of another vendor is enabled: next step
        lane = _lanes.pick_lane(w.store, cls, lanes=table)
        if lane is None:
            # Fit lanes of the allowed vendors exist but are full: wait,
            # do not bypass the rule.
            res.notes.append(
                f"td{n.id}: review not dispatched — every lane fit for {cls!r} "
                f"outside {label or 'the table'} is at its cap"
            )
            return False, None, None
        if vendors and lane.vendor == vendors[0]:
            # Step three only: the original builder's own vendor reviews.
            res.notes.append(
                f"td{n.id}: review on lane {lane.name} shares the builder's vendor "
                f"{lane.vendor!r} (no other vendor's lane is enabled; the "
                "cross-vendor rule holds once one is)"
            )
        stamp = _lane_decide.shadow_stamp(
            w.store,
            task_class=cls,
            table=lane_table,
            site="review",
            # The model submit resolves for an unpinned pick — the
            # lane's default — so the executed side compares
            # like-for-like with decide()'s model-level choice.
            picked=(lane.name, lane.default_model),
            exclude_vendor=sorted(excluded) or None,
        )
        return True, lane.name, stamp
    res.notes.append(f"td{n.id}: review not dispatched — no enabled lane fits {cls!r}")
    return False, None, None


# ── (4a) the validated non-repro rerun (gr588028) ──────────────────


def _digest_of(plan: _Plan, n: _Node) -> _Node | None:
    """The live ``ci_red_digest`` todo ``n`` is ``blocked-by`` — the
    release-fix loop's digest node, re-armed when a rerun still reads
    red (``ci_red_digest._fix_targets`` read the other way)."""
    for b in plan.blockers.get(n.id, []):
        node = plan.nodes.get(b.id)
        if node is None or node.kind != "todo":
            continue
        op = node.meta.get("op")
        if op == "ci_red_digest" or (
            isinstance(op, dict) and op.get("type") == "ci_red_digest"
        ):
            return node
    return None


def _mint_rerun_ask(store: Any, n: _Node, why: str, continued: str) -> int | None:
    """The ONE ask a stalled rerun path files under the plan root
    (gr588028 item 4, review round 1): it RECORDS the stall — the loop
    already continued (or still waits) without it — so it is
    ``blocks: "nothing"`` with no ``blocked-by`` edge, and the only
    option is ``acknowledged``. ``why`` names the error TYPE only,
    never internals. Insert shape of :func:`_mint_loop_ask`; the
    ``ci_rerun_stalled`` marker on the fix node is the dedupe."""
    context = (
        f"the validated-flake rerun stalled: {why} (fix td{n.id}, plan td{n.root})"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_RERUN_ASK_OPTIONS),
            "blocks": "nothing",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    title = f"plan td{n.root}: the validated-flake rerun stalled ({why[:80]})"
    body = (
        f"A `validated:` non-repro fix session on td{n.id} parked the node "
        f"for a one-shot rerun of the red check.yml run's failed legs; the "
        f"rerun path stalled: {why}. {continued} This ask only records "
        "the stall — close it to acknowledge."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _rerun_stall(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    why: str,
    *,
    unpark: bool = True,
    marker: str = "",
) -> None:
    """One stall comment on the fix node + the informational root ask
    (:func:`_mint_rerun_ask` — it blocks nothing; the loop continues or
    waits regardless), then — unless ``unpark`` is false — drop
    :data:`NOREPRO_WAIT_TAG` and re-arm the digest so the release-fix
    loop continues as it does today (gr588028 item 4: the stall never
    retries the rerun itself). The >2h wait-cap call keeps the park;
    ``marker`` (the run id, or a sha key when none is known) is stamped
    as ``meta.ci_rerun_stalled`` and is the dedupe — a still-parked node
    never files twice for the same stall."""
    if marker and n.meta.get("ci_rerun_stalled") == marker:
        res.notes.append(f"td{n.id}: rerun stall already recorded — {why}")
    else:
        continued = (
            "The red-fix loop has continued as before (the digest "
            "re-armed, or no rerun fired)."
            if unpark
            else "The fix node is still parked on the in-flight rerun; "
            "closing the ask changes nothing about the wait."
        )
        ask_id = None if w.dry_run else _mint_rerun_ask(store, n, why, continued)
        if ask_id is not None or w.dry_run:
            w.comment(
                n,
                f"validated: rerun path stalled — {why}; the plan root's "
                "ask records it.",
            )
        if marker:
            w.stamp_meta(n, {"ci_rerun_stalled": marker})
    if unpark:
        w.remove_tags(n, [NOREPRO_WAIT_TAG])
        digest = _digest_of(plan, n)
        if digest is not None and not plan.live_job(digest):
            ended = [j for j in plan.jobs(digest) if j.status in _JOB_ENDED]
            if digest.status != "open":
                w.set_status(digest, "open")
            if ended:
                # The same floor ``_step_rearm`` and ``_review_revert``
                # stamp: without it ``_step_phases``' backstop re-closes
                # the digest next pass on its OLD succeeded job and no
                # fresh ``ci_red_digest`` — no fresh ``brief:`` — mints.
                w.stamp_meta(digest, {"rearmed_after_job": ended[-1].id})
    res.notes.append(f"td{n.id}: rerun stalled — {why}")


def _step_ci_rerun(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    """The ``validated:`` non-repro path (gr588028), for every fix node
    ``run_slice.poll`` parked on :data:`NOREPRO_WAIT_TAG`: mint the
    ``ci_rerun`` job once per run id (idem ``ci_rerun:<run_id>`` — the
    once-per-run rule also lives in :func:`_release.ci_rerun_fired`),
    then the plan's own ``ci_verdict_poll`` records the rerun's verdict
    (no second reader). Green closes the node moot-style so the loop's
    poll re-reads the run; red re-arms the digest — the loop continues
    as today. Anything blocking files one stall comment and the deduped
    root ask, never a retry. Runs between :func:`_step_phases` and
    :func:`_step_dispatch` — the park tag's ``waiting-for:`` prefix keeps
    the node out of dispatch while this step owns it."""
    for n in plan.todos():
        if NOREPRO_WAIT_TAG not in n.open_tags or _closed(n):
            continue
        jobs = plan.jobs(n)
        reruns = [j for j in jobs if j.meta.get("job_type") == "ci_rerun"]
        polls = [
            j
            for j in jobs
            if j.meta.get("job_type") == "ci_verdict_poll" and j.meta.get("rerun_of")
        ]
        live = [j for j in (*reruns, *polls) if j.status not in _JOB_TERMINAL]
        rel = _release.release_of(store, n.id) or {}
        sha = str(rel.get("sha") or "")
        rng = str(rel.get("range") or sha)
        if live:
            pass  # the rerun or its poll is in flight — the wait cap below
        elif polls and polls[-1].status == "succeeded":
            verdict = _gates._newest_verdict(store, sha, rng) or {}
            run_id = str(polls[-1].meta.get("rerun_of") or "?")
            if verdict.get("verdict") == "green":
                w.comment(n, f"validated: flake — rerun of run {run_id} green")
                w.remove_tags(n, [NOREPRO_WAIT_TAG])
                w.set_status(n, "done")
                res.notes.append(
                    f"td{n.id}: rerun of run {run_id} recorded green; fix "
                    "closed, the poll re-reads the run"
                )
            else:
                _rerun_stall(
                    store,
                    plan,
                    w,
                    res,
                    n,
                    f"rerun of run {run_id} still red — the loop "
                    "continues as today (the digest re-arms)",
                    marker=run_id,
                )
            continue
        elif polls:
            _rerun_stall(
                store,
                plan,
                w,
                res,
                n,
                f"the rerun's verdict poll {polls[-1].status}",
                marker=str(polls[-1].meta.get("rerun_of") or "poll"),
            )
            continue
        elif reruns and reruns[-1].status in _JOB_TERMINAL:
            rerun = reruns[-1]
            if rerun.status == "succeeded":
                rid: str | None = str(
                    (rerun.meta.get("ci_rerun") or {}).get("run_id") or ""
                )
                if w.dry_run:
                    res.notes.append(
                        f"td{n.id}: would mint ci_verdict_poll for run {rid}"
                    )
                    continue
                job_id = _mint_job(
                    store,
                    n,
                    "ci_verdict_poll",
                    "job_inproc",
                    {"todo_id": n.id},
                    idem_key=f"{NAME}:ci_verdict_poll:td{n.id}",
                    meta_extra={
                        ATTEMPT_FREE_KEY: "ci-rerun",
                        "rerun_of": rid,
                        # The freshness baseline: the poll reads a
                        # verdict as the rerun's only when the run shows
                        # a higher run_attempt (gr588028 review, F4).
                        "rerun_attempt_before": (rerun.meta.get("ci_rerun") or {}).get(
                            "rerun_attempt_before"
                        ),
                    },
                )
                res.notes.append(
                    f"td{n.id}: rerun of run {rid} accepted — minted "
                    f"ci_verdict_poll job:{job_id} to record the verdict"
                )
            else:
                _rerun_stall(
                    store,
                    plan,
                    w,
                    res,
                    n,
                    f"the ci_rerun job {rerun.status} — the rerun was "
                    "refused or the run was not found",
                    marker=str(
                        (rerun.meta.get("ci_rerun") or {}).get("run_id")
                        or sha
                        or "refused"
                    ),
                )
            continue
        else:
            # Mint phase: no rerun job yet.
            verdict = _gates._newest_verdict(store, sha, rng) or {}
            if verdict.get("verdict") == "green":
                # Green already (a hand rerun, a misread digest) — never
                # rerun a run that is not red.
                w.comment(
                    n,
                    "validated: flake — the newest verdict on the cut is "
                    "already green; no rerun needed",
                )
                w.remove_tags(n, [NOREPRO_WAIT_TAG])
                w.set_status(n, "done")
                res.notes.append(
                    f"td{n.id}: verdict already green; closed without a rerun"
                )
                continue
            rid = _release.newest_red_run(store, sha) if sha else None
            if not rid:
                _rerun_stall(
                    store,
                    plan,
                    w,
                    res,
                    n,
                    "no red verdict with a run id on the cut sha to rerun",
                    marker=f"no-red:{sha or 'no-cut'}",
                )
                continue
            if _release.ci_rerun_fired(store, rid):
                # Defensive — run_slice's spent ruling never parks on a
                # fired run; a tag placed by hand un-parks to the plain
                # no_commit path.
                w.remove_tags(n, [NOREPRO_WAIT_TAG])
                res.notes.append(
                    f"td{n.id}: ci_rerun already fired for run {rid}; "
                    "un-parked to the ordinary path"
                )
                continue
            if w.dry_run:
                res.notes.append(f"td{n.id}: would mint ci_rerun for run {rid}")
                continue
            # The run id is never a param — ``ci_rerun`` resolves the
            # cut's newest red verdict row itself (a supplied
            # ``params.run_id`` is refused outright).
            params: dict[str, Any] = {"todo_id": n.id}
            host = _release.pinned_host(store, n.id)
            if host:
                params["target_node"] = host
            job_id = _mint_job(
                store,
                n,
                "ci_rerun",
                "ssh_node",
                params,
                idem_key=f"ci_rerun:{rid}",
                meta_extra={ATTEMPT_FREE_KEY: "ci-rerun"},
            )
            res.notes.append(
                f"td{n.id}: minted ci_rerun job:{job_id} for run {rid} "
                "(the failed legs re-fire once)"
            )
            continue
        # Still in flight — only the >2h wait is a stall. The marker
        # dedupes it: a live job does not re-ask each tick.
        tagged = n.open_tags.get(NOREPRO_WAIT_TAG)
        if tagged is not None and now - tagged > RERUN_WAIT_CAP:
            live_run = str(
                (live[-1].meta.get("rerun_of") or "")
                or (live[-1].meta.get("ci_rerun") or {}).get("run_id")
                or "in-flight"
            )
            _rerun_stall(
                store,
                plan,
                w,
                res,
                n,
                "over two hours waiting on the rerun and its verdict",
                unpark=False,
                marker=live_run,
            )


# ── (4b) the design-call hold on a deep fix chain (gr603118) ───────


def _step_brief_holds(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    """Settle every ``waiting-for:brief`` hold (:data:`BRIEF_HOLD_TAG`,
    minted by :func:`_review_fix` at :data:`FIX_BRIEF_DEPTH`) and the
    design-call ask it left. Release paths:

    * the coordinator removed the tag — the posted ``brief:`` rides the
      todo, and the ask, still open, closes ``done`` as moot;
    * the ask closed with ``answer: continue`` — the tick drops the tag
      and the ladder tries again;
    * the tag outlived :data:`BRIEF_WAIT_CAP` while the ask was still
      open — silence, so the tag drops, the ask closes
      ``auto-timeout`` and the ladder continues;
    * a closed ask with a different (or no) answer is a ruling the hold
      keeps — stamped once on ``meta.brief_wait.ruling`` so the note is
      not repeated; only a hand tag-removal or closing the node ends it;
    * a closed node's ask, still open, closes ``won't-do`` as moot.

    Runs between :func:`_step_ci_rerun` and :func:`_step_dispatch` so a
    released node dispatches in the same pass."""
    for n in plan.todos():
        bw = n.meta.get(BRIEF_WAIT_KEY)
        tag_at = n.open_tags.get(BRIEF_HOLD_TAG)
        if tag_at is None and not isinstance(bw, dict):
            continue
        ask_id = bw.get("ask") if isinstance(bw, dict) else None
        ask = plan.nodes.get(ask_id) if isinstance(ask_id, int) else None
        if _closed(n):
            if ask is not None and not _closed(ask):
                w.comment(
                    ask,
                    f"plan_dispatch: td{n.id} is closed — the design call "
                    "it waited on is moot.",
                )
                w.set_status(ask, "won't-do")
                res.notes.append(
                    f"td{ask.id}: design-call ask closed won't-do — td{n.id} is closed"
                )
            continue
        if tag_at is None:
            # The hold was lifted by hand — the coordinator's brief path.
            # The ask it left, still open, records that and closes.
            if ask is not None and not _closed(ask):
                w.comment(
                    ask,
                    f"plan_dispatch: td{n.id}'s waiting-for:brief was "
                    "removed — the brief path released it; this ask is "
                    "moot.",
                )
                w.set_status(ask, "done")
                res.notes.append(
                    f"td{ask.id}: design-call ask closed — td{n.id}'s "
                    "hold was lifted by hand"
                )
            continue
        if ask is not None and not _closed(ask):
            if now - tag_at <= BRIEF_WAIT_CAP:
                continue  # still inside the design-call window
            minutes = int((now - tag_at).total_seconds() // 60)
            w.remove_tags(n, [BRIEF_HOLD_TAG])
            w.comment(
                ask,
                f"plan_dispatch: {minutes} min with no brief and no "
                "answer — the hold lifts, the ladder continues; this "
                "ask closes timed out.",
            )
            w.set_status(ask, "auto-timeout")
            w.comment(
                n,
                "plan_dispatch: waiting-for:brief lifted — the "
                "design-call window passed in silence; the node "
                "dispatches on the ladder.",
            )
            res.released += 1
            res.notes.append(
                f"td{n.id}: brief hold timed out at {minutes}m — "
                "released, the ladder continues"
            )
            continue
        if ask is not None:
            # The ask closed with the tag still on: only ``continue``
            # releases; any other ruling (or none) keeps the hold.
            if isinstance(bw, dict) and bw.get("ruling"):
                continue
            answer = (_release._newest_answer(store, ask.id) or "").strip()
            if answer.lower().startswith("continue"):
                w.remove_tags(n, [BRIEF_HOLD_TAG])
                w.comment(
                    n,
                    f"plan_dispatch: waiting-for:brief lifted — ask "
                    f"td{ask.id} answered 'continue'.",
                )
                res.released += 1
                res.notes.append(
                    f"td{n.id}: brief hold released — ask td{ask.id} answered continue"
                )
            else:
                if isinstance(bw, dict):
                    w.stamp_meta(
                        n,
                        {
                            BRIEF_WAIT_KEY: {
                                **bw,
                                "ruling": answer[:80] or "(closed, no answer)",
                            }
                        },
                    )
                res.notes.append(
                    f"td{n.id}: design-call ask td{ask.id} closed "
                    f"{(answer[:80] or 'with no answer')!r} — hold kept; "
                    "remove waiting-for:brief to release it"
                )
            continue
        # No ask on record (a hand-placed tag, or the ask retired): the
        # silence cap still applies.
        if now - tag_at > BRIEF_WAIT_CAP:
            w.remove_tags(n, [BRIEF_HOLD_TAG])
            res.released += 1
            res.notes.append(
                f"td{n.id}: waiting-for:brief outlived "
                f"{int(BRIEF_WAIT_CAP.total_seconds() // 60)}m with no "
                "ask on record — released"
            )


def _inflight_land_ids(plan: _Plan) -> list[int]:
    """Todo ids with a live ``land_slice`` job — the slices currently
    landing (td603849)."""
    return [
        n.id
        for n in plan.todos()
        if any(
            j.status not in _JOB_TERMINAL and j.meta.get("job_type") == "land_slice"
            for j in plan.jobs(n)
        )
    ]


def _brief_file_sets(store: Any, ids: list[int]) -> dict[int, frozenset[str]]:
    """``{todo id: file keys}`` from each todo's body chunks plus its
    ``brief:``/``handoff:`` comments — one query for the handful of ids
    the overlap check needs."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, text FROM chunks
             WHERE ref_id = ANY(%s)
               AND (chunk_kind = %s
                    OR (chunk_kind = %s
                        AND (starts_with(lower(ltrim(text)), 'brief:')
                             OR starts_with(lower(ltrim(text)), 'handoff:'))))
            """,
            (ids, TODO_BODY_KIND, _COMMENT_KIND),
        ).fetchall()
    texts: dict[int, list[str]] = {}
    for ref_id, text in rows:
        texts.setdefault(int(ref_id), []).append(str(text or ""))
    return {i: _dscore.brief_files("\n".join(texts.get(i, ()))) for i in ids}


def _block_lane_gap(
    plan: _Plan, w: _Writer, res: PassResult, n: _Node, why: str
) -> None:
    """Hold one task on a coordinator ask; its edge prevents re-minting."""
    now = datetime.now(UTC)
    if w.dry_run:
        # Mirror the hold so hygiene cannot report a second ask this pass.
        plan.blockers.setdefault(n.id, []).append(
            _Blocker(id=-n.id, status="open", is_ask=True, created_at=now)
        )
        res.notes.append(f"td{n.id}: would block and ask coordinator — {why}")
    else:
        ask_id = _mint_coordinator_ask(
            w.store,
            plan,
            n,
            title=f"td{n.id}: {why}",
            body=(
                f"Task td{n.id} is blocked: {why}. No slice job is minted. "
                "Repair the lane configuration or the task's class, then close "
                "this ask `acknowledged` to release the task."
            ),
            context=why,
            now=now,
            block_node=True,
        )
        w.comment(n, f"plan_dispatch: blocked — {why}; ask td{ask_id}")
        res.notes.append(f"td{n.id}: blocked — {why}; asked in td{ask_id}")
    res.asks += 1


def _route_text(store: Any, n: _Node) -> tuple[str, str | None]:
    """The body and newest coordinator ``brief:`` comment (gr645163).
    Keep them separate: the brief is an instruction in its own right,
    outside the body's labelled paragraphs and write set (td651223).
    Worker-stamped comments (``principal:
    worker:*`` — the mechanical-red briefs, ``handoff:`` reports and
    lander notes plan_dispatch/run_slice post) never classify: they
    report what happened, and a denied attempt's own deny-path line or
    a mechanical brief's ``scripts/ship`` mention is the event, not
    the target. A hidden comment is stale by the hider's ruling and is
    never the brief either (gr653037). ``route.strip_direct_head``
    removes the ``gripe-fix-direct`` instruction head for the same
    reason."""
    from precis.workers.job_types import run_slice as _rs

    chunks = _rs._todo_chunks(store, n.id)
    parts = [str(c.text) for c in chunks if c.chunk_kind == TODO_BODY_KIND and c.text]
    hidden = set(n.meta.get(_HIDDEN_META_KEY) or ())
    brief = next(
        (
            str(c.text).lstrip()
            for c in sorted(chunks, key=lambda c: int(c.ord), reverse=True)
            if c.chunk_kind == _rs._COMMENT_KIND
            and int(c.ord) not in hidden
            and not str((c.meta or {}).get("principal", "")).startswith("worker:")
            and str(c.text or "").lstrip().lower().startswith("brief:")
        ),
        None,
    )
    if brief is not None:
        # The ``brief:`` label is the comment's type, not instruction
        # text: judged with the label still on it, a handle-only brief
        # carries one letter token too many and never classifies
        # (td645566).
        brief = brief[len("brief:") :].lstrip()
    return "\n\n".join(parts), brief


def _block_route(
    plan: _Plan, w: _Writer, res: PassResult, n: _Node, why: str, text: str
) -> None:
    """Hold one run_slice-bound node on a coordinator ask — the mint-time
    route of gr645163: a brief no builder lane may run (it names a deny
    path, or the fix is prod-graph only) mints no job and burns no
    build; the ``blocked-by`` edge keeps later passes from re-minting or
    re-asking. The ask's body carries the brief itself — the ruling's
    "with the diff/brief attached"."""
    now = datetime.now(UTC)
    if w.dry_run:
        # Mirror the hold so hygiene cannot report a second ask this pass.
        plan.blockers.setdefault(n.id, []).append(
            _Blocker(id=-n.id, status="open", is_ask=True, created_at=now)
        )
        res.notes.append(f"td{n.id}: would ask coordinator — brief {why}")
    else:
        ask_id = _mint_coordinator_ask(
            w.store,
            plan,
            n,
            title=f"td{n.id}: {why} — a coordinator call",
            body=(
                f"Task td{n.id} routes to the coordinator at mint: the "
                f"brief {why}. No builder lane may do the work, so no "
                "run_slice job was minted — the builder that ran it would "
                "refuse as won't-do or end with no commit (gr645163, the "
                "td643078/td638980 stalls). Land it by hand, mint a "
                "builder outside the factory's deny rules, or drop the "
                f"node; closing this ask `acknowledged` re-arms td{n.id} "
                "and it routes again while the text still names a denied "
                "path.\n\n---\n\n" + text.strip()[:4000]
            ),
            context=why,
            now=now,
            block_node=True,
        )
        w.comment(
            n, f"plan_dispatch: routes to the coordinator — {why}; ask td{ask_id}"
        )
        res.notes.append(
            f"td{n.id}: routes to the coordinator — {why}; asked in td{ask_id}"
        )
    res.asks += 1


#: The release_cut node's last logged refusal (:func:`_log_cut_refusal`).
CUT_REFUSAL_LOGGED_KEY = "cut_refusal_logged"


def _log_cut_refusal(w: _Writer, res: PassResult, n: _Node, note: str | None) -> None:
    """Log a ``release_cut`` node's dispatch refusal once per change
    (gr651885): r291's re-cut hold sat two hours as a pass note under
    the summary cap that no one read. A changed ``note`` is stamped on the
    node (``meta.cut_refusal_logged``), logged and queued for the tick
    job's ``job_result`` line (:attr:`PassResult.logged`); the same note
    on a later pass is not repeated, and a dispatch (``note`` ``None``)
    drops the stamp so the next refusal logs again. A dry run logs and
    stamps nothing."""
    if w.dry_run or note == n.meta.get(CUT_REFUSAL_LOGGED_KEY):
        return
    w.stamp_meta(n, {CUT_REFUSAL_LOGGED_KEY: note})
    if note is not None:
        res.logged.append(note)
        log.info("plan_dispatch: %s", note)


def _step_dispatch(
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    lane_table: tuple[_lanes.Lane, ...],
    rules: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> None:
    if now is None:
        now = datetime.now(UTC)
    held_ids = _inflight_land_ids(plan)
    candidates = [
        n
        for n in sorted(plan.todos(), key=lambda x: (-_effective_score(plan, x), x.id))
        if _dispatchable(plan, n) and n.status == "open"
    ]
    # td603849 file-overlap hold: a run_slice whose brief file set
    # overlaps an in-flight land's scores below it until that land
    # finishes — i.e. held this pass, never outranking the land (two
    # slices rebuilding the same file re-conflicted at the mint-barrier
    # land). The held land's own todo is never a candidate (it has a
    # live job).
    overlap_files = (
        _brief_file_sets(w.store, held_ids + [n.id for n in candidates])
        if held_ids
        else {}
    )
    held: dict[int, int] = {}
    if held_ids:
        held_sets = {i: overlap_files.get(i) or frozenset() for i in held_ids}
        for n in candidates:
            if _resolve_op(n)[0] != DEFAULT_OP:
                continue
            mine = overlap_files.get(n.id) or frozenset()
            for hid, hfiles in held_sets.items():
                if hid != n.id and mine and mine & hfiles:
                    held[n.id] = hid
                    break
    # gr657066: governs:factory skills join the routing set — a brief
    # naming one routes to the coordinator like any deny-path text. The
    # filesystem variant failing open is fine here (the run/land gates
    # re-resolve at the base and fail closed).
    deny = _lanes.deny_paths_for(w.store) + _governs.governed_skill_paths()
    for n in candidates:
        if _excluded(plan, n) or _claims(n) or plan.live_job(n):
            continue
        # td659522 step 0: every op on a node tagged for another repo is
        # skipped before any routing, escalation or ask — the builder,
        # reviewer and lander all assume a precis worktree.
        repo_problem = _lanes.repo_refusal(_node_repos(plan, n), w.store)
        if repo_problem is not None:
            res.notes.append(f"td{n.id}: not dispatched — {repo_problem}")
            continue
        if n.id in held:
            res.notes.append(
                f"td{n.id}: file-overlap hold — brief names files in "
                f"td{held[n.id]}'s in-flight land; scored below it until "
                "that land finishes"
            )
            continue
        job_type, params = _resolve_op(n)
        # td659522 step 1: a build in another repo carries it on the job;
        # a default-repo node gets no key, so its params are unchanged.
        node_deny = deny
        if job_type == DEFAULT_OP:
            tagged = _node_repos(plan, n)
            if tagged and tagged[0] != _lanes.DEFAULT_REPO:
                params["repo"] = tagged[0]
                profile = _lanes.repo_profile(tagged[0])
                if profile is not None:
                    # The precis deny set (and its governed skills) is no
                    # reason to route a brief about another repo.
                    node_deny = (
                        profile.deny_paths
                        if profile.deny_paths is not None
                        else _lanes.deny_paths_for(w.store)
                    )
        if plan.blocked(n) or plan.paused(n):
            continue
        reland = _reland_params(plan, n, w.store)
        if job_type == DEFAULT_OP and reland is None:
            task_class = str(n.meta.get("task_class") or _DEFAULT_TASK_CLASS)
            gap = _lanes.missing_class_reason(lane_table, task_class)
            if gap is not None:
                _block_lane_gap(plan, w, res, n, gap)
                continue
            # gr645163 (Reto 2026-11-12, "escalate to next model"): work
            # no builder lane may write — the judged text names a deny
            # path, or the fix is prod-graph only — skips the builders
            # and files one coordinator ask at mint. Generators never
            # classify: they read source text, and a gripe or diff may
            # name deny paths legitimately.
            if node_kind(n.meta) != "generator":
                body, brief = _route_text(w.store, n)
                routed = _route.coordinator_route(body, node_deny, brief=brief)
                if routed is not None:
                    text = "\n\n".join(part for part in (body, brief) if part)
                    _block_route(plan, w, res, n, routed, text)
                    continue
        rungs = _task_rungs(lane_table, n, params) if job_type == DEFAULT_OP else ()
        cap = _retry_cap(n, _cap_rungs(plan, lane_table, n, job_type, params, rules))
        spend_hit = _spend_hit(plan, n, rungs, rules) if rules else None
        if spend_hit is not None:
            w.stamp_meta(
                n,
                {
                    SPEND_KEY: {
                        "usd": round(spend_hit[0], 4),
                        "cap_usd": spend_hit[1],
                        "blocked": True,
                        "at": _iso(datetime.now(UTC)),
                    }
                },
            )
        if _capped(plan, n, cap, rungs=rungs, spend_capped=spend_hit is not None):
            continue
        jobs = plan.jobs(n)
        newest = jobs[-1] if jobs else None
        requeued_cancel: int | None = None
        if newest is not None and newest.status == "cancelled":
            if newest.meta.get("job_type") not in _REQUEUE_ON_CANCEL:
                # The cancel the tick leaves alone: an op off the
                # re-queue allowlist may carry side effects a kill did
                # not roll back — a land killed mid-ship may already
                # have merged, a release step or ``pr_action`` may
                # already have fired — so a blind re-run is never safe
                # (gr587772). The rescue tick's ``cancelled`` class owns
                # the node.
                res.notes.append(
                    f"td{n.id}: newest job:{newest.id} was cancelled; not redispatched"
                )
                continue
            # gr587772: every guard above already passed, so the cancel
            # is a free re-queue — dispatch exactly as for an
            # ``attempt_free`` job (the job burns no attempt: it is not
            # in ``_JOB_ENDED``). Stopping the node stays a tag's job:
            # ``halt:``/``waiting-for:``, ``STATUS:paused``, won't-do.
            # The note lands at the mint below: a node that parks on the
            # way (no fit review lane, a refused submit) was not
            # re-queued this pass.
            requeued_cancel = newest.id
        free_reason = newest.meta.get(ATTEMPT_FREE_KEY) if newest else None
        if free_reason and newest is not None:
            # gr662077: identical ``attempt_free`` failures burn no
            # attempt, so the retry bound never trips — a held
            # ``index.lock`` re-minted a doomed job every tick (~140 in
            # 3h on td656030). The first repeats re-queue at once — a
            # transient wait usually clears on the next tick — but a
            # sustained run of the SAME reason backs off exponentially
            # from the newest failure's mint, and at
            # :data:`FREE_RUN_CAP` ``_free_done`` reads as capped and the
            # hygiene step asks the coordinator.
            _reason, run = _free_run(plan, n)
            if run >= FREE_RUN_GRACE:
                ready = newest.created_at + min(
                    FREE_BACKOFF_MAX,
                    FREE_BACKOFF_BASE * 2 ** (run - FREE_RUN_GRACE),
                )
                if ready > now:
                    res.notes.append(
                        f"td{n.id}: held — '{free_reason}' free failure "
                        f"x{run} in a row; next dispatch at {_iso(ready)}"
                    )
                    continue
            res.notes.append(f"td{n.id}: re-queued after {free_reason}, not an attempt")
        if reland is not None:
            # An infra-red land re-runs ship on the same branch with the
            # failed job's own params — no builder, no escalate, no review
            # lane (gr570187).
            job_type, params = "land_slice", reland
        decision_stamp: dict[str, Any] | None = None
        if _is_reviewer(n) and job_type == DEFAULT_OP and "lane" not in params:
            go, lane_name, decision_stamp = _review_lane(plan, w, res, n, lane_table)
            if not go:
                continue
            params["lane"] = lane_name
        mech_pinned = False
        if (
            job_type == DEFAULT_OP
            and reland is None
            and requeued_cancel is None
            and not _is_reviewer(n)
        ):
            # gr604579 (factory plan 3b): a red whose error line names a
            # mechanical cause gets the fix as a ``brief:`` comment and
            # retries on the SAME rung — not the lane-ladder climb that
            # used to land a 4000-char skill chunk on swe-2-max. Two
            # briefed retries, the third red asks instead:
            # ``meta.mech_retries.exhausted`` makes the node read as
            # capped to the hygiene step below.
            mech = _mech_red(plan, w.store, n)
            if mech is not None:
                slug, brief, line, repeats, last_job = mech
                stamp = {
                    "sig": slug,
                    "n": repeats,
                    "job": last_job,
                    "at": _iso(datetime.now(UTC)),
                }
                if repeats > MECH_RETRY_CAP:
                    w.stamp_meta(n, {MECH_RETRIES_KEY: {**stamp, "exhausted": True}})
                    res.notes.append(
                        f"td{n.id}: mechanical red '{slug}' back for repeat "
                        f"{repeats} — the brief budget is spent; parked "
                        "for the cap ask"
                    )
                    continue
                prev = n.meta.get(MECH_RETRIES_KEY)
                w.stamp_meta(n, {MECH_RETRIES_KEY: stamp})
                # One brief per red job: a mint held this pass (lane-wait,
                # a params problem, a validate_submit refusal) or an
                # attempt_free newest job re-fires this block on the same
                # failed attempt next tick — the stamp says the brief is
                # already up, so it does not re-post.
                if not (isinstance(prev, dict) and prev.get("job") == last_job):
                    w.comment(
                        n,
                        f"brief: {brief} — mechanical red on jo{last_job}: "
                        f"{line[:200]}",
                    )
                mech_pinned = _mech_pin(plan, w, n, rungs, params, rules)
                res.notes.append(
                    f"td{n.id}: mechanical red '{slug}' (repeat "
                    f"{repeats}/{MECH_RETRY_CAP}) — briefed, retry "
                    + (
                        "on the same rung"
                        if mech_pinned
                        else "climbs the ladder (last rung unpinnable)"
                    )
                )
        if job_type == DEFAULT_OP and not mech_pinned:
            go, climbed, esc_stamp = _escalate(w, plan, n, params, lane_table, rules)
            if climbed:
                res.notes.append(climbed)
            if esc_stamp is not None:
                decision_stamp = esc_stamp
            if not go:
                continue
        if (
            job_type
            in (
                "release_deploy",
                "release_cut",
                "ci_red_digest",
                "ci_rerun",
                "release_confirm",
            )
            and "target_node" not in params
        ):
            # The deploy, the cut, the red-run digest and the runtime
            # confirm run where the cut ran (meta.release.host, td543856,
            # td543855, td573431, td573432); the first cut has none and
            # stays unpinned.
            host = _release.pinned_host(w.store, n.id)
            if host:
                params["target_node"] = host
        spec = get_job_type(job_type)
        if spec is None:
            res.notes.append(
                f"td{n.id}: meta.op job_type {job_type!r} is not registered"
            )
            continue
        problem = _params_problem(spec.params_schema, params)
        if problem is None and not w.dry_run and spec.validate_submit is not None:
            problem = spec.validate_submit(w.store, gripe_id=None, params=params)
        note = f"td{n.id}: {job_type} not dispatched — {problem}" if problem else None
        if job_type == "release_cut":
            _log_cut_refusal(w, res, n, note)
        if note:
            res.notes.append(note)
            continue
        executor = _pick_executor(spec.compatible_executors)
        if w.dry_run:
            res.notes.append(f"td{n.id}: would dispatch {job_type} on {executor}")
        else:
            job_id = _mint_job(
                w.store,
                n,
                job_type,
                executor,
                params,
                # The re-land shares run_slice's own land-mint key
                # (``land_slice:td<id>``), not the tick's — one lander per
                # slice, however it was minted.
                idem_key=(f"land_slice:td{n.id}" if reland is not None else None),
                # The decide() shadow beside the dispatch-time pick
                # (td572552 S4b) — run_slice.submit nests it under its
                # own submit record.
                meta_extra={"decision": decision_stamp}
                if decision_stamp is not None
                else None,
            )
            if requeued_cancel is not None:
                res.notes.append(
                    f"td{n.id}: newest job:{requeued_cancel} was cancelled; "
                    "re-queued, not an attempt"
                )
            res.notes.append(
                f"td{n.id}: dispatched {job_type} job:{job_id} on {executor}"
            )
        res.dispatched += 1


# ── (5) hygiene ────────────────────────────────────────────────────


def _job_event_text(store: Any, job_id: int) -> str:
    """The newest ``job_event`` chunk's text (the job's failure reason)."""
    try:
        chunks = [
            c
            for c in store.chunks.list_chunks_for_ref(job_id)
            if c.chunk_kind == "job_event" and c.text
        ]
    except Exception:  # a missing tail must not stop the ask
        log.warning("plan_dispatch: reading job:%d chunks failed", job_id)
        return ""
    if not chunks:
        return ""
    return str(max(chunks, key=lambda c: (c.ord, c.id)).text).strip()


def _failure_tail(store: Any, job_id: int, limit: int) -> str:
    """The tail of the job's newest ``job_event`` chunk (its failure reason)."""
    return _job_event_text(store, job_id)[-limit:]


#: A session's own no-commit verdict — the first log line starting
#: ``moot:`` or ``no-op:`` (td584381 S1a F4), carried into the rescue
#: evidence or the ask so a human or the diagnose pass sees the why.
_MOOT_OR_NOOP_RE = re.compile(r"^\s*(?:moot|no-op)\s*:", re.IGNORECASE)


def _verdict_line(text: str) -> str | None:
    """The first ``moot:``/``no-op:`` line in a job's event text, ≤300
    chars, or ``None`` (td584381 S1a F4)."""
    for ln in text.splitlines():
        if _MOOT_OR_NOOP_RE.match(ln):
            return ln.strip()[:300]
    return None


def _handoff_verdict(
    store: Any, todo_id: int, *, since: datetime | None = None
) -> str | None:
    """The ``moot:``/``no-op:`` line of the single NEWEST ``handoff:``
    comment (the ``handoff:`` prefix stripped first, the line read by
    :func:`_verdict_line`), or ``None`` (td584381 S1d). Scoped to the
    newest attempt (review round 1): older handoffs are never consulted
    — an earlier attempt's ``moot:`` cannot be read as this attempt's —
    and ``since`` (the newest attempt job's ``created_at``) rejects a
    ``handoff:`` older than it. The session's verdict survives on the
    todo when the job's event text never carried it — a JSON-log
    adapter parses the session's result out of the raw log, so the log
    tail alone can miss it."""
    newest = next(
        (
            c
            for c in _comments(store, todo_id)
            if str(c.text).strip().lower().startswith("handoff:")
        ),
        None,
    )
    if newest is None:
        return None
    created = getattr(newest, "created_at", None)
    # Fail closed (review round 2): with a ``since`` in hand a handoff
    # whose ``created_at`` cannot be read is NOT this attempt's — an
    # unprovable scope is no scope.
    if since is not None and (not isinstance(created, datetime) or created < since):
        return None
    text = str(newest.text).strip()
    return _verdict_line(text[len("handoff:") :])


#: Options of the ask a node parked at :data:`LAND_AGAIN_CAP` infra-red
#: lands gets (gr570187): the default retry ladder cannot help — nothing
#: on the diff failed.
_RELAND_ASK_OPTIONS: list[str] = [
    "land again (no builder)",
    "rewrite the brief",
    "won't do",
]

#: Options of the ask a human-only hit mints (td584381 S1a): no lane may
#: write those paths — the options are the human edit landing, a new
#: brief that stays off them, or dropping the node.
_HUMAN_ASK_OPTIONS: list[str] = [
    "a human made the edit",
    "rewrite the brief",
    "won't do",
]


def _mint_ask(
    store: Any,
    plan: _Plan,
    n: _Node,
    attempts: int,
    cap: int,
    *,
    relands: int = 0,
    human: list[str] | None = None,
    lane_gap: str | None = None,
    mech: str | None = None,
) -> int:
    newest = plan.jobs(n)[-1]
    # A deadline-killed land (``red_class == "timed-out"``,
    # ``land_slice.DEADLINE_KILL_META`` — gr647914) gets the same re-land
    # ask with its own wording: the ship may already have merged, so the
    # answer must weigh a half-landed run, not gate infrastructure.
    timed_out = newest.meta.get("red_class") == "timed-out"
    if relands:
        head = (
            f"job:{newest.id} {newest.status} — the land was killed at the "
            "wall-clock deadline"
            if timed_out
            else f"job:{newest.id} {newest.status} — {relands} land(s) in a row "
            "died outside the diff (gate infrastructure)"
        )
        title = (
            f"td{n.id}: the land timed out mid-ship: land again, "
            "rewrite the brief, or drop it?"
            if timed_out
            else f"td{n.id}: land failed outside the diff {relands} times: "
            "land again, rewrite the brief, or drop it?"
        )
        options = list(_RELAND_ASK_OPTIONS)
    elif human:
        head = (
            f"job:{newest.id} {newest.status} — the branch touched "
            "human-only path(s): " + ", ".join(f"`{p}`" for p in human)
        )
        title = (
            f"td{n.id}: the diff touched human-only path(s) — "
            "human edit, new brief, or drop it?"
        )
        options = list(_HUMAN_ASK_OPTIONS)
    elif mech:
        head = (
            f"job:{newest.id} {newest.status} — the same mechanical red "
            f"('{mech}') came back past its {MECH_RETRY_CAP} briefed "
            f"retries ({attempts} attempts)"
        )
        title = (
            f"td{n.id}: mechanical red keeps coming back — retry, "
            "rewrite the brief, or drop it?"
        )
        options = list(_ASK_OPTIONS)
    else:
        head = (
            f"job:{newest.id} {newest.status} after {attempts} attempt(s) "
            f"(retry cap {cap})"
        )
        title = f"td{n.id} hit its retry cap: retry, rewrite the brief, or drop it?"
        options = list(_ASK_OPTIONS)
        # A release cut that died on a refusal line it could not classify
        # (``release_cut.poll``, ``meta.cut_refusal``): quote the line, the
        # log tail below may have scrolled past it.
        refusal = newest.meta.get("cut_refusal")
        if isinstance(refusal, dict) and refusal.get("line"):
            head = f"{head}; cut refused: {str(refusal['line'])[:300]}"
    if lane_gap:
        head = f"{head}; {lane_gap}"
    tail = _failure_tail(store, newest.id, _ASK_CONTEXT_CAP - len(head) - 3)
    context = f"{head}: {tail}" if tail else head
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": options,
            "blocks": f"td{n.id}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    if relands:
        if timed_out:
            # gr647914: the SIGTERM may have raced the merge — the answer
            # weighs a possibly half-landed run, never an automatic one.
            verdict = (
                "The ship was killed at the wall deadline and may already "
                "have merged — check the `landed:` comment or origin/main "
                "before re-landing."
            )
        elif newest.meta.get("land_again_cap"):
            # A capped exit-137 (the land's own gate-kill retries spent):
            # land_slice's blocked-handoff wording — the 137 can be the
            # container's mem_limit on the branch's own memory, so the
            # ask must not clear the branch.
            verdict = (
                "The cause is the docker VM's memory or the branch's own "
                "memory use — a memory hog on the branch is the builder's "
                "to fix; otherwise an operator frees the docker VM."
            )
        else:
            verdict = "The branch is not at fault, so no builder was minted."
        body = (
            f"The plan tick stopped re-landing td{n.id} "
            f"({n.title.splitlines()[0] if n.title else ''}): {head}. "
            f"{verdict} Answer with "
            "the option you pick and close this todo; 'land again (no "
            "builder)' re-arms the re-land, 'rewrite the brief' rebuilds "
            f"from the new brief on td{n.id} (a `brief:` comment or an "
            f"edited body). Pick 'won't do' by closing "
            f"td{n.id} STATUS:won't-do."
        )
    elif human:
        body = (
            f"The plan tick stopped dispatching td{n.id} "
            f"({n.title.splitlines()[0] if n.title else ''}): {head} — "
            f"a human must edit {', '.join(f'`{p}`' for p in human)}; "
            "no lane may write those paths, so no retry and no rescue "
            "can fix this. Answer 'a human made the edit' once the edit "
            f"is in and close this todo — closing re-arms td{n.id} with a "
            "fresh retry budget; 'rewrite the brief' rebuilds on a new "
            "brief (a `brief:` comment or an edited body); pick "
            f"'won't do' by closing td{n.id} STATUS:won't-do."
        )
    else:
        gap = (
            f" {lane_gap} — a lane must come back before a retry or a rescue can run."
            if lane_gap
            else ""
        )
        body = (
            f"The plan tick stopped dispatching td{n.id} ({n.title.splitlines()[0] if n.title else ''}): "
            f"{head}.{gap} Answer with the option you pick and close this todo; closing "
            f"it re-arms td{n.id} (a fresh retry budget). Pick 'won't do' by closing "
            f"td{n.id} STATUS:won't-do."
        )
        if newest.meta.get(NO_COMMIT_KEY):
            # The session's own read on a no-commit exit — carried so a
            # human (or S1d) acts on the why, not just the what
            # (td584381 S1a F4).
            vline = _verdict_line(_job_event_text(store, newest.id))
            if vline is None:
                # The log tail missed it (a JSON-log adapter parses the
                # result out of the raw log) — the posted ``handoff:``
                # comment still carries it (td584381 S1d); scoped to the
                # newest attempt's handoff.
                vline = _handoff_verdict(store, n.id, since=newest.created_at)
            if vline is not None:
                body += f" The session left no commit; its verdict: {vline}."
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=n.id, dst_ref_id=ref.id, relation="blocked-by", conn=conn
        )
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _mint_stamp_ask(store: Any, n: _Node, gid: int, sha: str, err: str) -> int:
    """The ONE ask a persistently failing verified-moot gripe stamp
    files on the plan root (td584381 S1d review round 2):
    ``_notify_gripe`` raised :data:`_MOOT_STAMP_ASK_AFTER` ticks in a
    row, so the retry becomes a human call — a silent ``res.notes``
    loop would page nobody. ``err`` is the exception TYPE only (the
    message may carry internals). Insert shape of :func:`_mint_ask`;
    the ``blocked-by`` lands on the ROOT — ``_Plan.open_asks`` then
    dedupes the ask and :func:`_step_moot` reads the park off it."""
    context = (
        f"moot verified but the gripe stamp fails: {err} "
        f"(moot td{n.id}, plan td{n.root}, gripe gr{gid}, sha {sha})"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_MOOT_STAMP_ASK_OPTIONS),
            "blocks": f"td{n.root}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    title = (
        f"plan td{n.root}: verified moot td{n.id} cannot stamp gr{gid} — "
        "retry or drop the plan?"
    )
    body = (
        f"moot verified but the gripe stamp fails: {err}. The tick "
        f"verified the `moot:` claim of td{n.id} (sha {sha}) and closed "
        f"its chain won't-do, but resolving the plan's gripe gr{gid} on "
        f"that sha raised {_MOOT_STAMP_ASK_AFTER} ticks running — plan "
        f"td{n.root} stays open and the stamp is not retried while this "
        "ask is open. Answer and close this todo to re-arm the stamp "
        f"retry (or clear td{n.id}'s meta.{MOOT_STAMP_KEY} to retry at "
        f"once); pick 'won't do' by closing td{n.root} STATUS:won't-do."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=n.root, dst_ref_id=ref.id, relation="blocked-by", conn=conn
        )
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _mint_loop_ask(store: Any, n: _Node, count: int, why: str) -> int:
    """The ruling ask an exhausted ``release-fix`` loop files (me573231):
    ``count`` automatic iterations ended red — named with the round, the
    head sha and the failing CI — and a further fix never starts by
    itself. Insert shape of :func:`_mint_ask`, minus the ``blocked-by``
    link (the ask parks the whole pass, not one node). ``blocks`` names the
    pass root, not ``"nothing"``: an informational ask no longer holds its
    root open (gr589823), and this one must — the parking is the point."""
    rel = _release.release_of(store, n.id)
    rn = str((rel or {}).get("rN") or "?")
    sha12 = str((rel or {}).get("sha") or "")[: _release.LANDED_SHA_LEN] or "?"
    context = f"release {rn} at {sha12}: {why}"[:_ASK_CONTEXT_CAP]
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_LOOP_ASK_OPTIONS),
            "blocks": f"td{n.root}",
            "context": context,
        }
    }
    check_ask_meta(meta)
    title = (
        f"release {rn}: {count} fix iteration(s) ended red — "
        "close this pass or hand the release to a human?"
    )
    branch = _release.release_branch(rn) if rn != "?" else "the release branch"
    body = (
        f"The {rn} pass's release-fix loop minted {count} fix iteration(s) "
        f"on {branch} and every one ended red ({why}). No further iteration "
        f"is minted: {_RELEASE_FIX_AUTO_CAP} run unattended, the next is a "
        "human call (me573231). release_poll mints no fresh pass while this "
        "pass's root stays open, and this ask holds the root open until it is "
        f"closed. Fix forward on {branch} by hand — a fix written on a tree cut "
        f"from origin/{branch}, landed with `scripts/ship --onto {branch}`, "
        "or a fix main already has cherry-picked onto it — then push; the "
        "release head's own check.yml run is the verdict. Or answer 'hand "
        "release with /go' to run the release yourself. Main gets the fixes "
        "when the release merges back after its deploy."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _mint_orphan_ask(store: Any, n: _Node, minutes: int) -> int:
    """The ONE ask an orphan mint files on its plan root (gr574881): the
    ``waiting-for:mint`` hold outlived :data:`MINT_STALE` and the mint
    never settled — no ``blocked-by``/``part-of`` edge, no body — so its
    minter died, errored or forgot between the ``put`` and the link
    calls. The hold stays (an empty task released would dispatch into
    the void); the ask parks it for a human. Insert shape of
    :func:`_mint_ask`; the ``blocked-by`` edge lands on the orphan so
    :func:`_step_mint_holds` never asks twice — :func:`_mint_settled`
    reads the options/blocks-signed edge as that dedup mark, not a
    settled link, so a mint the human then settles still releases."""
    context = (
        f"waiting-for:mint held {minutes}m with no links and no body — "
        f"orphan mint td{n.id} under plan td{n.root}"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_ORPHAN_MINT_ASK_OPTIONS),
            "blocks": f"td{n.id}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    title = (
        f"td{n.id}: orphan mint held {minutes}m — settle it, release it, or drop it?"
    )
    body = (
        f"td{n.id} ({n.title.splitlines()[0] if n.title else ''}) was minted "
        f"inside plan td{n.root} with waiting-for:mint and its minter never "
        f"settled it — {minutes} min silent, no blocked-by/part-of link, no "
        "body. The tick keeps the hold: released as-is the empty task would "
        "dispatch into the void. To keep the mint, settle it — once a link "
        "or a body lands the tick releases the hold itself. To dispatch it "
        "as-is ('release the hold anyway'), remove the waiting-for:mint tag "
        f"and close this todo. Pick 'won't do' by closing td{n.id} "
        "STATUS:won't-do."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=n.id, dst_ref_id=ref.id, relation="blocked-by", conn=conn
        )
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _stale_gripe_claims(
    conn: Any, now: datetime, *, gripe_id: int | None = None
) -> dict[int, list[str]]:
    """``{gripe id: [expired claimed-by: values]}`` for live ``STATUS:fixing``
    gripes with no live job under them (a child, or a ``fixes``-linked job).

    A lease is expired once its ``expires_at`` (stamped at claim and every
    re-claim, ``stamp_claim_lease``) has passed; a legacy row without one is
    judged by its tag ``created_at`` against :data:`CLAIM_STALE`.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id, t.value, rt.created_at, rt.expires_at
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE r.kind = 'gripe' AND r.retired_at IS NULL
           AND (%s::bigint IS NULL OR r.ref_id = %s::bigint)
           AND t.namespace = 'OPEN' AND t.value LIKE 'claimed-by:%%'
           AND EXISTS (SELECT 1 FROM ref_tags s JOIN tags st USING (tag_id)
                        WHERE s.ref_id = r.ref_id AND st.namespace = 'STATUS'
                          AND st.value = 'fixing')
           AND NOT EXISTS (
                 SELECT 1 FROM refs j
                  WHERE j.kind = 'job' AND j.retired_at IS NULL
                    AND (j.parent_id = r.ref_id OR EXISTS (
                          SELECT 1 FROM links l
                           WHERE l.src_ref_id = j.ref_id
                             AND l.dst_ref_id = r.ref_id
                             AND l.relation = 'fixes'))
                    AND NOT EXISTS (
                          SELECT 1 FROM ref_tags js JOIN tags jt USING (tag_id)
                           WHERE js.ref_id = j.ref_id AND jt.namespace = 'STATUS'
                             AND jt.value = ANY(%s)))
         ORDER BY r.ref_id, t.value
        """,
        (gripe_id, gripe_id, sorted(_JOB_TERMINAL)),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for ref_id, value, created_at, expires_at in rows:
        lapsed = (
            expires_at <= now
            if expires_at is not None
            else now - created_at > CLAIM_STALE
        )
        if lapsed:
            out.setdefault(int(ref_id), []).append(str(value))
    return out


def _release_gripe(store: Any, gripe_id: int, now: datetime) -> list[str] | None:
    """Drop an expired gripe's lease and return it to ``triaged`` — the
    handler's lease-drop rule (``GripeHandler._claim_status``): a newest
    ``resolved:`` comment means the work is done, so the status stays.
    Re-checked under the row lock; ``None`` when the lease was refreshed or
    the gripe moved on since the scan."""
    with store.tx() as conn:
        conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (gripe_id,))
        held = _stale_gripe_claims(conn, now, gripe_id=gripe_id).get(gripe_id)
        if not held:
            return None
        row = conn.execute(
            "SELECT c.text FROM chunks c WHERE c.ref_id = %s "
            "AND c.chunk_kind = %s ORDER BY c.ord DESC LIMIT 1",
            (gripe_id, _GRIPE_COMMENT_KIND),
        ).fetchone()
        resolved = bool(row) and str(row[0]).lstrip().startswith("resolved:")
        release_claims(conn, gripe_id)
        if resolved:
            return held
        store.add_tag(
            gripe_id,
            Tag.closed("STATUS", "triaged"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        (ord_,) = conn.execute(
            "SELECT coalesce(max(ord), 0) + 1 FROM chunks WHERE ref_id = %s",
            (gripe_id,),
        ).fetchone()
        holder = ", ".join(v.removeprefix("claimed-by:") for v in held)
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=int(ord_),
                    text=(
                        f"lease expired after {int(CLAIM_STALE.total_seconds() // 3600)}"
                        f" h; back to triaged (held by {holder})"
                    ),
                    meta={"chunk_kind": _GRIPE_COMMENT_KIND},
                )
            ],
            conn=conn,
        )
    return held


def _step_gripe_leases(store: Any, w: _Writer, res: PassResult, now: datetime) -> None:
    """gr542086: a ``STATUS:fixing`` gripe whose ``claimed-by:`` lease lapsed
    with no live job goes back to ``triaged`` (counted in ``released``)."""
    with store.pool.connection() as conn:
        stale = _stale_gripe_claims(conn, now)
    for gripe_id, held in stale.items():
        if not w.dry_run and _release_gripe(store, gripe_id, now) is None:
            continue
        res.released += 1
        res.notes.append(f"gr{gripe_id}: released expired {', '.join(held)}")


def _mint_unplaced_ask(
    store: Any, n: _Node, j: _Node, gap: frozenset[str], minutes: int
) -> int:
    """The ONE ruling ask a placement gap files on its node (gr657333):
    the job's ``ServiceSpec.placement`` tokens no live host advertises
    together — the claim gate holds it until one does, so the ask is the
    escalation, not a block: no ``blocked-by`` edge (the queued job still
    claims the moment a host turns up; :data:`_UNPLACED_ASK_KEY` on the
    node's meta is the dedup mark instead). Insert shape of
    :func:`_mint_orphan_ask`, parked on the plan root."""
    job_type = str(j.meta.get("job_type") or "job")
    gap_s = ", ".join(sorted(gap))
    context = (
        f"job jo{j.id} ({job_type}) queued {minutes}m — no live host "
        f"advertises {{{gap_s}}} together (td{n.id}, plan td{n.root})"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_UNPLACED_ASK_OPTIONS),
            "blocks": f"td{n.id}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    title = (
        f"td{n.id}: job jo{j.id} unplaceable {minutes}m — "
        "provision a host, wait, or drop the node?"
    )
    body = (
        f"td{n.id}'s job jo{j.id} ({job_type}) has sat queued {minutes} min: "
        f"its placement capabilities {{{gap_s}}} are advertised by no live "
        f"host together, so the claim gate holds it. To fix the gap, "
        "provision a host advertising the tokens (pick the first option and "
        "close this todo done). To keep waiting — the host comes back on its "
        "own — pick 'wait' and close this todo done; the job claims the "
        "moment one turns up. To give up on the work, pick 'drop the node' "
        f"and close td{n.id} STATUS:won't-do (this ask then closes won't-do "
        "with it). The sweeper's scheduler/unschedulable alert is the "
        "standing page meanwhile."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _step_unplaced(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    """td543917: a note per plan job queued past :data:`QUEUED_NOTE_AFTER`
    whose placement capabilities (``ServiceSpec.placement``) no live host
    advertises together — the claim gate holds it until one does. The
    sweeper's ``scheduler/unschedulable:<id>`` alert is the page.

    gr657333: past :data:`QUEUED_ASK_AFTER` the node also files ONE ruling
    ask (:func:`_mint_unplaced_ask`), deduped on ``meta.unplaced_ask`` —
    a closed ask is the ruling, never re-minted. The heal check settles
    it: the node closes first → the ask closes ``won't-do``; the gap
    heals or the job leaves ``queued`` → ``done``."""
    from precis.workers.executors._common import (
        advertised_by_host,
        placement_gap,
        placement_requires,
    )

    waiting = [
        (n, j)
        for n in plan.todos()
        for j in plan.jobs(n)
        if j.status == "queued"
        and now - j.created_at > QUEUED_NOTE_AFTER
        and placement_requires(j.meta)
    ]
    marked = [
        (n, mark)
        for n in plan.todos()
        if isinstance((mark := n.meta.get(_UNPLACED_ASK_KEY)), dict)
    ]
    if not waiting and not marked:
        return
    with store.pool.connection() as conn:
        advertised = advertised_by_host(conn)
    for n, mark in marked:
        ask_id = mark.get("ask")
        ask = plan.nodes.get(ask_id) if isinstance(ask_id, int) else None
        if ask is None or _closed(ask):
            continue  # answered (or gone) — the ruling stands; never re-mint
        if _closed(n):
            w.comment(
                ask,
                f"plan_dispatch: td{n.id} is closed — the placement ruling "
                "it waited on is moot.",
            )
            w.set_status(ask, "won't-do")
            res.notes.append(
                f"td{ask.id}: unplaced ask closed won't-do — td{n.id} is closed"
            )
            continue
        # The ask is about the node's gap, not the recorded job: a
        # re-minted job inheriting the same gap keeps it open.
        still_blocked = any(
            j.status == "queued"
            and placement_requires(j.meta)
            and placement_gap(j.meta, advertised)
            for j in plan.jobs(n)
        )
        if still_blocked:
            continue
        w.comment(
            ask,
            f"plan_dispatch: td{n.id}'s placement gap closed (job jo{mark.get('job')} "
            "claims or ended) — this ask is moot.",
        )
        w.set_status(ask, "done")
        res.notes.append(
            f"td{ask.id}: unplaced ask closed — td{n.id}'s placement gap healed"
        )
    for n, j in waiting:
        gap = placement_gap(j.meta, advertised)
        if not gap:
            continue
        minutes = int((now - j.created_at).total_seconds() // 60)
        res.notes.append(
            f"td{n.id}: job jo{j.id} queued {minutes} min — no host advertises "
            f"{{{', '.join(sorted(gap))}}}"
        )
        if (
            now - j.created_at <= QUEUED_ASK_AFTER
            or _UNPLACED_ASK_KEY in n.meta
            or _closed(n)
        ):
            continue
        if w.dry_run:
            res.asks += 1
            res.notes.append(
                f"td{n.id}: would ask — job jo{j.id} unplaceable {minutes}m "
                f"({{{', '.join(sorted(gap))}}})"
            )
            continue
        ask_id = _mint_unplaced_ask(store, n, j, gap, minutes)
        w.stamp_meta(
            n,
            {
                _UNPLACED_ASK_KEY: {
                    "ask": ask_id,
                    "at": _iso(now),
                    "job": j.id,
                    "gap": sorted(gap),
                }
            },
        )
        res.asks += 1
        res.notes.append(
            f"td{n.id}: job jo{j.id} unplaceable {minutes}m, asked in td{ask_id}"
        )
        log_plan_event(
            w.store,
            n.id,
            f"ask td{ask_id} raised (job jo{j.id} unplaceable {minutes}m; "
            f"gap {{{', '.join(sorted(gap))}}})",
            root=n.root,
        )


def _mint_linked(store: Any, ref_id: int) -> bool:
    """A ``blocked-by``/``part-of`` edge touches the mint. The edge to
    the mint's own orphan ask — options/blocks-signed by
    :func:`_mint_orphan_ask` — is the ask's dedup mark, not a settled
    link: the mint behind it is still the empty task the ask parks
    (td601973)."""
    with store.pool.connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM links l WHERE l.relation IN ('blocked-by', 'part-of') "
                "AND (l.src_ref_id = %s OR l.dst_ref_id = %s) "
                "AND NOT (l.relation = 'blocked-by' AND l.src_ref_id = %s "
                "AND EXISTS (SELECT 1 FROM refs b WHERE b.ref_id = l.dst_ref_id "
                "AND b.retired_at IS NULL AND b.meta->'ask'->>'blocks' = %s "
                "AND b.meta->'ask'->'options' = %s::jsonb)) LIMIT 1",
                (
                    ref_id,
                    ref_id,
                    ref_id,
                    f"td{ref_id}",
                    json.dumps(list(_ORPHAN_MINT_ASK_OPTIONS)),
                ),
            ).fetchone()
            is not None
        )


def _mint_has_body(store: Any, ref_id: int) -> bool:
    """A non-empty ``TODO_BODY_KIND`` chunk on the mint — the minter's
    brief landed."""
    return any(
        c.chunk_kind == TODO_BODY_KIND and str(c.text or "").strip()
        for c in store.chunks.list_chunks_for_ref(ref_id)
    )


def _mint_settled(store: Any, ref_id: int) -> bool:
    """Whether a held mint's put→link session settled: a ``blocked-by``
    or ``part-of`` edge touches it (:func:`_mint_linked`), or it carries
    a non-empty body (:func:`_mint_has_body`) — the hold is then a
    leftover, not a guard (gr574881)."""
    return _mint_linked(store, ref_id) or _mint_has_body(store, ref_id)


def _mint_finished(store: Any, ref_id: int) -> bool:
    """The job-end release rule (gr603945): the mint is provably written
    — a non-empty body AND a ``blocked-by``/``part-of`` edge
    (:func:`_mint_has_body` and :func:`_mint_linked`). Stricter than
    :func:`_mint_settled`'s either/or: a sibling session's mint inside
    the same job window can still be half-written — a body waiting on
    its links — and its hold stays the minter's, or the sweep's, to
    lift."""
    return _mint_linked(store, ref_id) and _mint_has_body(store, ref_id)


def release_session_mints(store: Any, todo_id: int, since: datetime) -> list[int]:
    """td603429 follow-up (gr603945): ``run_slice`` calls this at a clean
    job end — the session's mints are done being written, so every todo
    under the session todo's plan root created in the job window
    (``refs.created_at >= since``) that still carries
    ``waiting-for:mint`` and is fully settled (:func:`_mint_finished`)
    gets :func:`finish_mint` now instead of idling :data:`MINT_STALE`
    plus a tick for the sweep. ``since`` is the job's ``created_at`` —
    mints older than the window are not this session's. A hand-put leaf
    (title-only, no body, no link) keeps the hold: the orphan ask is
    intended, not a bug (:func:`_mint_orphan_ask`). Returns the released
    ids."""
    root = _plan_root_of(store, todo_id)
    if root is None:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE walk(ref_id, lvl) AS (
                SELECT %s::bigint, 0
                UNION ALL
                SELECT r.ref_id, w.lvl + 1
                  FROM refs r
                  JOIN walk w ON r.parent_id = w.ref_id
                 WHERE r.kind = 'todo' AND r.retired_at IS NULL
                   AND w.lvl < 25
            )
            SELECT r.ref_id FROM refs r JOIN walk w USING (ref_id)
             WHERE r.retired_at IS NULL AND r.created_at >= %s
               AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                              AND t.value = %s)
             ORDER BY r.ref_id
            """,
            (root, since, MINT_HOLD_TAG),
        ).fetchall()
    released: list[int] = []
    for (rid,) in rows:
        rid = int(rid)
        if _mint_finished(store, rid) and finish_mint(store, rid):
            released.append(rid)
    return released


def _step_mint_holds(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    """gr574881: a ``waiting-for:mint`` hold past :data:`MINT_STALE` on a
    mint whose links or body settled (:func:`_mint_settled`) is released —
    the me570515 contract leaves removal to the minter, and a minter that
    died, errored or forgot would hold the child forever. This is the
    fallback: a clean ``run_slice`` exit already finishes its job window's
    fully-settled mints (:func:`release_session_mints`, gr603945). An
    unsettled orphan mints one ask on its plan root instead
    (:func:`_mint_orphan_ask`); the ask's ``blocked-by`` edge dedupes the
    ASK decision only — a mint settled past its ask (or behind a
    pre-existing ask dependency) still releases (td601973)."""
    for n in plan.todos():
        if _closed(n):
            continue
        stamped = n.open_tags.get(MINT_HOLD_TAG)
        if stamped is None or now - stamped <= MINT_STALE:
            continue
        minutes = int((now - stamped).total_seconds() // 60)
        if _mint_settled(store, n.id):
            w.remove_tags(n, [MINT_HOLD_TAG])
            res.released += 1
            res.notes.append(f"mint hold released: td{n.id} (minter silent {minutes}m)")
            continue
        if any(b.is_ask for b in plan.blockers.get(n.id, [])):
            continue  # still an orphan — the ask is already filed
        if w.dry_run:
            res.asks += 1
            res.notes.append(
                f"td{n.id}: would ask — orphan mint (minter silent {minutes}m)"
            )
            continue
        ask_id = _mint_orphan_ask(store, n, minutes)
        res.asks += 1
        res.notes.append(
            f"td{n.id}: orphan mint (minter silent {minutes}m), asked in td{ask_id}"
        )
        log_plan_event(
            w.store,
            n.id,
            f"ask td{ask_id} raised (orphan mint td{n.id}; minter silent {minutes}m)",
            root=n.root,
        )


def _in_rescue(n: _Node) -> bool:
    """An open ``rescue:<cls>`` tag — the rescue pass owns the node."""
    return any(v.startswith("rescue:") for v in n.open_tags)


def _rescue_node(
    plan: _Plan, w: _Writer, res: PassResult, n: _Node, cap: int, attempts: int
) -> None:
    """A ladder-exhausted ``run_slice`` node enters the rescue path
    (td584381 S1a) instead of the cap ask: ``enter_rescue`` tags it once,
    files or joins the signature gripe, and the rescue pass owns what
    follows — one diagnose-and-re-arm, then the rescue pass's own ask on
    a second exhaustion (``meta.rescue.n >= 1``), the end of the
    autonomous road."""
    from precis.workers import factory_rescue as _rescue

    cls = "review" if _is_reviewer(n) else "builder"
    ended = _attempt_jobs(plan, n)
    last = ended[-1] if ended else plan.jobs(n)[-1]
    text = _job_event_text(w.store, last.id)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # The signature key's error line is the failure's own first line —
    # skipping the ``run_slice:`` preamble and the ``--- log tail ---``
    # separator, else every same-class failure collapses into one
    # signature gripe. The preamble stays the fallback.
    error_line = next(
        (ln for ln in lines if not ln.lstrip().startswith(("run_slice:", "---"))),
        lines[0] if lines else None,
    )
    rungs_tried = " → ".join(
        f"{j.meta.get('lane')}/{j.meta.get('model') or 'default'}" for j in ended
    )
    step = (
        f"retry cap {cap}: the lane ladder is spent "
        f"({attempts} attempts; rungs tried: {rungs_tried or 'unrecorded'})"
    )
    spend = n.meta.get(SPEND_KEY)
    if isinstance(spend, dict) and isinstance(spend.get("cap_usd"), (int, float)):
        # The per-node USD cap stopped the climb — name it (gr599486).
        step += (
            f"; the per-node spend cap ${float(spend['cap_usd']):.2f} for a "
            f"paid rung held — ${float(spend.get('usd') or 0):.2f} recorded "
            "spent on the node's job rows"
        )
    verdict = _verdict_line(text)
    if verdict is None:
        # The log tail missed it — the posted ``handoff:`` comment still
        # carries the verdict (td584381 S1d); the log tail keeps
        # precedence when it has one, and only the newest attempt's
        # ``handoff:`` counts (``last`` is that attempt's job).
        verdict = _handoff_verdict(w.store, n.id, since=last.created_at)
    if verdict is not None:
        # The session's own read on a no-commit exit — what a human or
        # the diagnose task acts on (td584381 S1a F4).
        step += f"; the session's verdict: {verdict}"
    commits = last.meta.get("branch_commits") or []
    head = str(commits[0]).split()[0] if commits and str(commits[0]).split() else None
    try:
        r = _rescue.enter_rescue(
            w.store,
            n.id,
            cls,
            _rescue.RescueEvidence(
                step=step,
                job=f"jo{last.id}",
                log_tail=text,
                error_line=error_line[:300] if error_line else None,
                # ``run_slice.slice_branch``'s spelling, restated — the
                # dependency stays one-way.
                branch=f"slice/{n.id}",
                head_sha=head,
            ),
        )
    except Exception as exc:  # one failing node must not stop the pass
        log.warning("plan_dispatch: rescue of td%d raised", n.id, exc_info=True)
        res.notes.append(f"td{n.id}: rescue failed ({type(exc).__name__}: {exc})")
        return
    if r.tagged:
        res.rescued += 1
        res.notes.append(
            f"td{n.id}: lane ladder spent ({attempts} attempts, cap {cap}); "
            f"entered rescue ({cls}, gripe gr{r.gripe_id})"
        )
        log_plan_event(
            w.store,
            n.id,
            f"node td{n.id} entered rescue ({cls}) at retry cap "
            f"({attempts} attempts, cap {cap})",
            entry_type="observation",
            root=n.root,
        )
    else:
        res.notes.append(f"td{n.id}: rescue skipped ({r.skipped})")


def _step_hygiene(
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    now: datetime,
    lane_table: tuple[_lanes.Lane, ...],
    rules: dict[str, dict[str, Any]] | None = None,
) -> None:
    _step_gripe_leases(w.store, w, res, now)
    _step_unplaced(w.store, plan, w, res, now)
    _step_mint_holds(w.store, plan, w, res, now)
    for n in plan.todos():
        if _closed(n):
            continue
        stale = [v for v in _claims(n) if now - n.open_tags[v] > CLAIM_STALE]
        if stale and not plan.live_job(n):
            w.remove_tags(n, stale)
            if n.status == "doing":
                w.set_status(n, "open")
            res.released += 1
            res.notes.append(f"td{n.id}: released stale {', '.join(stale)}")
    for n in plan.todos():
        if _closed(n) or not _dispatchable(plan, n) or _in_rescue(n):
            continue
        job_type, params = _resolve_op(n)
        rungs = _task_rungs(lane_table, n, params) if job_type == DEFAULT_OP else ()
        cap = _retry_cap(n, _cap_rungs(plan, lane_table, n, job_type, params, rules))
        spend_hit = _spend_hit(plan, n, rungs, rules) if rules else None
        if spend_hit is not None:
            w.stamp_meta(
                n,
                {
                    SPEND_KEY: {
                        "usd": round(spend_hit[0], 4),
                        "cap_usd": spend_hit[1],
                        "blocked": True,
                        "at": _iso(now),
                    }
                },
            )
        if not _capped(plan, n, cap, rungs=rungs, spend_capped=spend_hit is not None):
            continue
        if plan.open_asks(n):
            continue
        attempts = _attempts(plan, n)
        human = _human_hit(plan, n)
        land_capped = _land_again_capped(plan, n)
        mech_sig = _mech_done(plan, n)
        coord = _coordinator_hit(plan, n)
        model_rule = _model_rule_hit(plan, n)
        no_commit = _no_commit(plan, n)
        free_sig = _free_done(plan, n)
        lane_gap: str | None = None
        if (
            not human
            and not land_capped
            and mech_sig is None
            and coord is None
            and model_rule is None
            and free_sig is None
            and job_type == DEFAULT_OP
            and not rungs
        ):
            task_class = str(n.meta.get("task_class") or _DEFAULT_TASK_CLASS)
            pinned = params.get("lane")
            lane_gap = (
                f"the op's pinned lane {pinned!r} is not enabled"
                if pinned
                else f"no enabled lane fits task class {task_class!r}"
            )
        rescue = (
            coord is None
            and model_rule is None
            and mech_sig is None
            and free_sig is None
            and not human
            and not land_capped
            and not no_commit
            and job_type == DEFAULT_OP
            and lane_gap is None
        )
        if w.dry_run:
            what = (
                f"enter rescue ({'review' if _is_reviewer(n) else 'builder'})"
                if rescue
                else f"file a coordinator ask (retry cap {cap})"
                if coord is not None
                or no_commit
                or model_rule is not None
                or free_sig is not None
                else f"file an ask (retry cap {cap})"
            )
            res.notes.append(f"td{n.id}: would {what}")
            if rescue:
                res.rescued += 1
            else:
                res.asks += 1
            continue
        if rescue:
            _rescue_node(plan, w, res, n, cap, attempts)
            continue
        if coord is not None:
            # A persistent infra red or a refused ``gh run rerun`` — the
            # stamp :data:`COORDINATOR_ASK_KEY` — is the coordinator's
            # call, never a lane retry's (td612107 review). The
            # ``blocked-by`` edge re-arms the node on the answer.
            ask_id = _mint_coordinator_ask(
                w.store,
                plan,
                n,
                title=(
                    f"td{n.id}: infra-blocked at retry cap {cap} — a coordinator call"
                ),
                body=(
                    f"The plan tick stopped dispatching td{n.id} "
                    f"({n.title.splitlines()[0] if n.title else ''}): "
                    f"{coord}. A run still infra past its parks, or a "
                    "`gh run rerun` the credential refused, is the "
                    "coordinator's call — a hand rerun, a push or an "
                    "Actions-write credential unblocks the loop; no lane "
                    "retry can. Close this ask `acknowledged` once the "
                    f"unblock is in — closing re-arms td{n.id} with a "
                    "fresh retry budget so a re-digest reads the run's "
                    "real verdict."
                ),
                context=coord,
                now=now,
                block_node=True,
            )
        elif model_rule is not None:
            # gr657521: the test session's different-model rule refused
            # the launch — a routing verdict, not a lane failure. Every
            # rung hits the same refusal, so the stamp caps the ladder
            # on the first attempt and the call goes to the
            # coordinator: pin ``params.model``/``params.lane`` to a
            # model outside the run's code sessions', record the code
            # session's model when an unresolved default was the
            # holdup, or drop the node. The ``blocked-by`` edge re-arms
            # the node on the answer.
            ask_id = _mint_coordinator_ask(
                w.store,
                plan,
                n,
                title=(
                    f"td{n.id}: the different-model rule refuses the "
                    "launch — a coordinator call"
                ),
                body=(
                    f"The plan tick stopped dispatching td{n.id} "
                    f"({n.title.splitlines()[0] if n.title else ''}): "
                    f"{model_rule} The refusal is deterministic — every "
                    "ladder rung hits it, so no retry can change the "
                    "verdict. A coordinator call: pin `params.model` "
                    "(and a lane that runs it) to a model the run's "
                    "code sessions did not use, record the code "
                    "session's model when an unresolvable account "
                    "default was the holdup, or drop the node — "
                    f"closing this ask `acknowledged` re-arms td{n.id} "
                    "with a fresh retry budget."
                ),
                context=model_rule,
                now=now,
                block_node=True,
            )
        elif no_commit:
            # gr645163: the ladder is spent and the newest attempt still
            # ended with no commit and no verdict — a builder's
            # can't-finish. No rung is left to teach it and a rescue
            # cycle only re-arms the same wall; the call is the
            # coordinator's (a hand land, a re-briefed mint outside the
            # deny rules, a drop) — the td643078/td638980 shape.
            ended = [j for j in plan.jobs(n) if j.status in _JOB_ENDED]
            last = ended[-1].id if ended else 0
            ask_id = _mint_coordinator_ask(
                w.store,
                plan,
                n,
                title=(
                    f"td{n.id}: the ladder spent, still no commit — a coordinator call"
                ),
                body=(
                    f"The plan tick stopped dispatching td{n.id} "
                    f"({n.title.splitlines()[0] if n.title else ''}): "
                    f"{attempts} attempt{'s' if attempts != 1 else ''} spent "
                    f"the lane ladder and the "
                    f"newest (jo{last}) still ended with no commit and no "
                    "verdict — a builder that can't finish (gr645163, "
                    'Reto 2026-11-12, "escalate to next model"). The '
                    "newest handoff comment on the todo carries the "
                    "session's own account. A coordinator call: land it "
                    "by hand, mint a builder outside the factory's deny "
                    "rules, re-brief the node, or drop it — closing this "
                    f"ask `acknowledged` re-arms td{n.id} with a fresh "
                    "retry budget."
                ),
                context=f"no-commit failure at retry cap {cap}",
                now=now,
                block_node=True,
            )
        elif free_sig is not None:
            # gr662077: the node's last :data:`FREE_RUN_CAP` jobs all
            # died on the same ``attempt_free`` reason — an infra wait
            # no retry changes (a live git's ``index.lock`` in the slice
            # worktree, a lane that never frees). Backoff already gave
            # the holder its window; finding and clearing it is the
            # coordinator's call. The ``blocked-by`` edge re-arms the
            # node on the answer.
            ask_id = _mint_coordinator_ask(
                w.store,
                plan,
                n,
                title=(
                    f"td{n.id}: '{free_sig}' failure x{FREE_RUN_CAP} "
                    "in a row — a coordinator call"
                ),
                body=(
                    f"The plan tick stopped dispatching td{n.id} "
                    f"({n.title.splitlines()[0] if n.title else ''}): "
                    f"its last {FREE_RUN_CAP} jobs all failed outside "
                    f"the diff with attempt_free='{free_sig}' — an "
                    "infra wait that never cleared (gr662077). No "
                    "ladder rung or same-rung retry changes it: find "
                    "and clear the holder — a live git's index.lock in "
                    "the slice worktree, a lane that never frees, a "
                    "dead rerun path — or drop the node. Closing this "
                    f"ask `acknowledged` re-arms td{n.id} with a fresh "
                    "retry budget."
                ),
                context=(f"attempt_free '{free_sig}' x{FREE_RUN_CAP} consecutive"),
                now=now,
                block_node=True,
            )
        else:
            ask_id = _mint_ask(
                w.store,
                plan,
                n,
                attempts,
                cap,
                relands=_infra_red_lands(plan, n) if land_capped else 0,
                human=human,
                lane_gap=lane_gap,
                mech=mech_sig,
            )
        why = (
            "infra red routed to coordinator"
            if coord is not None
            else "different-model refusal"
            if model_rule is not None
            else f"mechanical red loop ({mech_sig})"
            if mech_sig
            else "human-only hit"
            if human
            else "re-land cap hit"
            if land_capped
            else "can't finish — ladder spent"
            if no_commit
            else f"identical '{free_sig}' wait x{FREE_RUN_CAP}"
            if free_sig
            else "no enabled lane"
            if lane_gap
            else "retry cap hit"
        )
        res.notes.append(f"td{n.id}: {why}, asked in td{ask_id}")
        # gr548800: the park and the ask are quest-logbook events.
        log_plan_event(
            w.store,
            n.id,
            f"node td{n.id} parked ({why}; {attempts} attempts, retry cap {cap})",
            entry_type="observation",
            root=n.root,
        )
        log_plan_event(
            w.store,
            n.id,
            f"ask td{ask_id} raised (blocks td{n.id})",
            root=n.root,
        )
        res.asks += 1


# ── Pass + dispatch ────────────────────────────────────────────────


def read_only_reason(store: Any, params: dict[str, Any] | None = None) -> str | None:
    """Why this pass must not write, or ``None`` when it may."""
    if os.environ.get("PRECIS_FACTORY", "").strip().lower() == "off":
        return "PRECIS_FACTORY=off"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT host FROM service_config WHERE service = %s AND prio = 0 LIMIT 1",
            (SERVICE,),
        ).fetchone()
    if row is not None:
        return f"service_config {SERVICE} prio 0 on host {row[0]}"
    if (params or {}).get("dry_run"):
        return "params.dry_run"
    return None


def run_pass(
    store: Any,
    *,
    dry_run: bool = False,
    reason: str | None = None,
    now: datetime | None = None,
) -> PassResult:
    """One dispatcher pass over every plan subtree (module docstring).

    The whole tick runs under :data:`TICK_LOCK_KEY`; when another tick holds
    it this returns a ``skipped`` result and touches nothing.
    """
    now = now or datetime.now(UTC)
    res = PassResult(dry_run=dry_run, reason=reason)
    # The lock sits on a dedicated connection whose open transaction does no
    # writes: it exists only to pin one backend (pgbouncer-safe) and hold the
    # transaction-scoped lock while the steps use their own connections.
    with try_xact_advisory_lock(
        store.dsn,
        TICK_LOCK_KEY,
        idle_timeout=TICK_LOCK_IDLE_TIMEOUT,
        application_name=TICK_LOCK_APP_NAME,
    ) as got:
        if not got:
            res.skipped = "another tick holds the lock"
            return res
        plan = _load(store)
        res.roots = len(plan.roots)
        lane_table = _lanes.load_lanes(store)
        rules = _spend_rules(store)
        w = _Writer(store, dry_run)
        _step_dead_roots(store, plan, w, res)
        _step_gates(store, plan, w, res, now)
        _step_loops(store, plan, w, res, now)
        _step_reviews(store, plan, w, res, now, lane_table, rules)
        _step_reopen(plan, w, res)
        _step_rearm(plan, w, res)
        _step_verdicts(store, plan, w, res)
        _step_moot(store, plan, w, res)
        _step_phases(plan, w, res)
        _step_ci_rerun(store, plan, w, res, now)
        _step_brief_holds(store, plan, w, res, now)
        _step_dispatch(plan, w, res, lane_table, rules, now)
        _step_hygiene(plan, w, res, now, lane_table, rules)
    return res


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: one pass, summary on the job."""
    del spec
    params = dict(ctx.meta.get("params") or {})
    reason = read_only_reason(ctx.store, params)
    res = run_pass(ctx.store, dry_run=reason is not None, reason=reason)
    ctx.set_meta(plan_dispatch=res.counts())
    ctx.append_chunk("job_summary", res.render())
    for line in res.logged:
        ctx.append_chunk("job_result", line)
    log.info("%s", res.summary())


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=DESCRIPTION,
    dispatch=_dispatch,
    # A whole-graph pass: a second run while one is pending adds nothing.
    dedupe_pending=True,
    # It reads live plan state at claim time; nothing enqueued rots.
    queue_ttl_exempt=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "CLAIM_STALE",
    "DEFAULT_RETRIES",
    "MINT_STALE",
    "NAME",
    "PARAMS_SCHEMA",
    "QUEUED_ASK_AFTER",
    "QUEUED_NOTE_AFTER",
    "SERVICE",
    "SPEC",
    "PassResult",
    "load",
    "parse_verdict",
    "read_only_reason",
    "run_pass",
]
